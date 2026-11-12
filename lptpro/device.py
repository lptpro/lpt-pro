# SPDX-License-Identifier: GPL-3.0-or-later
"""Device access layer.

Everything that talks to the iPhone lives here. The rest of the app only uses
the small synchronous interface below, so it never has to care that
pymobiledevice3 is asyncio-based.

Two backends share one interface:
  * AfcBackend    - a real iPhone over USB (Apple File Conduit, read-only use)
  * FolderBackend - a local folder that pretends to be a phone (demo/testing)

This app NEVER writes to or deletes anything on the phone. The backends only
expose read operations.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import datetime as dt
import hashlib
import os
import logging
import threading
import time
from dataclasses import dataclass


log = logging.getLogger("transfer")


class DeviceError(Exception):
    """User-presentable device problem."""


def is_no_resources(e: BaseException) -> bool:
    """The phone answered "NO_RESOURCES". Either the original is only in iCloud (for a file
    that uses no storage on the phone), or the phone's file service is worn out (then even
    listing folders fails, until the iPhone is restarted)."""
    return "NO_RESOURCES" in str(e)


def is_not_on_phone(e: BaseException, item=None) -> bool:
    """True only when this refusal means "original only in iCloud": the error is
    NO_RESOURCES AND the phone reported that the file uses no storage."""
    return is_no_resources(e) and item is not None and bool(item.extra.get("no_storage"))


class DeviceGone(DeviceError):
    """The iPhone was unplugged (or stopped answering) part-way through."""


_GONE_NAMES = {"ConnectionTerminatedError", "NoDeviceConnectedError", "ConnectionFailedError",
               "DeviceNotFoundError", "MuxException"}


def _is_disconnect(e: BaseException) -> bool:
    return isinstance(e, (ConnectionError, EOFError)) or type(e).__name__ in _GONE_NAMES


@dataclass
class Stat:
    size: int
    mtime: dt.datetime
    birthtime: dt.datetime
    is_dir: bool
    blocks: int | None = None     # storage the file really uses on the phone (0 = only in iCloud)


HASH_HEAD = 64 * 1024  # bytes used for the quick fingerprint


class AfcBackend:
    """Real iPhone over USB. Runs its own asyncio loop on a background thread.

    All public methods are synchronous and thread-safe: any worker thread can
    call them; the calls are funnelled into the device loop. AFC itself
    supports concurrent requests, so several workers can share one connection.
    """

    kind = "iphone"

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="device-loop", daemon=True)
        self._thread.start()
        self._lockdown = None
        self._afc = None
        self._lost = False
        self._open_files: dict[int, tuple] = {}
        self._files_lock = threading.Lock()
        self._reset_lock = threading.Lock()
        self._close_failures = 0
        self.session_resets = 0
        self.on_disconnect = None  # callback(), called from the device thread when unplugged
        self.name = "iPhone"
        self.ios = ""
        self.model = ""

    # -- plumbing ---------------------------------------------------------
    def _run(self, coro, timeout: float = 120):
        if self._afc is not None and self._lost:
            coro.close()
            raise DeviceGone("The iPhone was disconnected.")
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return fut.result(timeout)
        except concurrent.futures.TimeoutError:
            fut.cancel()
            self._mark_lost()
            raise DeviceGone("The iPhone stopped responding.")
        except DeviceError:
            raise
        except Exception as e:
            # Only a phone that WAS connected can be "lost". While connecting, let the real
            # error through so connect() can explain it (e.g. nothing plugged in).
            if self._afc is not None and _is_disconnect(e):
                self._mark_lost()
                raise DeviceGone("The iPhone was disconnected.") from e
            raise

    def _mark_lost(self):
        if self._afc is not None and not self._lost:
            self._lost = True
            cb = self.on_disconnect
            if cb:
                try:
                    cb()
                except Exception:
                    pass

    # -- connection -------------------------------------------------------
    def connect(self) -> None:
        try:
            from pymobiledevice3.lockdown import create_using_usbmux
            from pymobiledevice3.services.afc import AfcService
            from pymobiledevice3 import exceptions as ex
        except ImportError as e:  # pragma: no cover
            raise DeviceError(f"pymobiledevice3 is not installed ({e}).")

        if self._afc is not None:
            self.close()  # reconnecting: drop the old session first
        self._lost = False
        with self._files_lock:
            self._open_files.clear()

        async def _connect():
            ld = await create_using_usbmux(autopair=True, pair_timeout=60)
            afc = AfcService(lockdown=ld)
            await afc.__aenter__()
            return ld, afc

        async def _watch(afc):
            # Wakes up the moment the USB connection goes away (cable pulled).
            try:
                await afc.wait_terminated()
            except Exception:
                pass
            if self._afc is afc:
                self._mark_lost()

        try:
            self._lockdown, self._afc = self._run(_connect(), timeout=90)
        except ex.NoDeviceConnectedError:
            raise DeviceError("iPhone is disconnected. Plug it in with a cable and unlock it.")
        except (ex.PairingDialogResponsePendingError, ex.NotTrustedError, ex.NotPairedError):
            raise DeviceError("Unlock your iPhone and tap “Trust” on the phone, then try again.")
        except ex.UserDeniedPairingError:
            raise DeviceError("The iPhone said “Don’t Trust”. Unplug, plug back in, and choose Trust.")
        except ex.PasswordRequiredError:
            raise DeviceError("Unlock your iPhone (enter the passcode), then try again.")
        except ex.ConnectionFailedToUsbmuxdError:
            import sys as _sys
            if _sys.platform.startswith("linux"):
                raise DeviceError(
                    "The iPhone USB service (usbmuxd) isn’t running. In a terminal run:  "
                    "sudo apt install usbmuxd   — then unplug the iPhone, plug it back in, "
                    "unlock it and press Retry. (If it still fails: sudo systemctl start usbmuxd)"
                )
            raise DeviceError(
                "Apple’s device service isn’t running. Install Apple’s iPhone driver "
                "(the free “Apple Devices” app from the Microsoft Store) and try again."
            )
        except DeviceError:
            raise
        except Exception as e:  # anything else: keep the real message for the log
            raise DeviceError(f"Couldn’t connect to the iPhone: {type(e).__name__}: {e}")

        asyncio.run_coroutine_threadsafe(_watch(self._afc), self._loop)
        info = self._lockdown.short_info
        self.name = info.get("DeviceName") or "iPhone"
        self.ios = info.get("ProductVersion") or ""
        try:
            self.model = self._lockdown.display_name or info.get("ProductType") or ""
        except Exception:
            self.model = info.get("ProductType") or ""

    def close(self) -> None:
        """Politely end the session with the phone. Read-only, so there is nothing to flush."""
        afc, self._afc = self._afc, None  # stop the unplug watcher from reporting this as a loss

        async def _close():
            if afc is not None:
                try:
                    await afc.aclose()
                except Exception:
                    pass
            if self._lockdown is not None:
                try:
                    await self._lockdown.close()
                except Exception:
                    pass

        try:
            asyncio.run_coroutine_threadsafe(_close(), self._loop).result(10)
        except Exception:
            pass
        self._lockdown = None

    @property
    def connected(self) -> bool:
        return self._afc is not None and not self._lost

    # -- read-only file API ---------------------------------------------
    @property
    def _svc(self):
        if self._afc is None or self._lost:
            raise DeviceGone("The iPhone is not connected.")
        return self._afc

    def listdir(self, path: str) -> list[str]:
        return [n for n in self._run(self._svc.listdir(path)) if n not in (".", "..")]

    @staticmethod
    def _to_stat(st: dict) -> Stat:
        return Stat(
            size=int(st.get("st_size", 0)),
            mtime=st.get("st_mtime"),
            birthtime=st.get("st_birthtime") or st.get("st_mtime"),
            is_dir=st.get("st_ifmt") == "S_IFDIR",
            blocks=int(st["st_blocks"]) if "st_blocks" in st else None,
        )

    def stat(self, path: str) -> Stat:
        return self._to_stat(self._run(self._svc.stat(path)))

    def stat_many(self, paths: list[str], batch: int = 64) -> list[Stat | None]:
        """Stat many files concurrently (much faster than one round trip each)."""

        async def _one(p):
            try:
                return self._to_stat(await self._svc.stat(p))
            except Exception:
                return None

        async def _all(chunk):
            return await asyncio.gather(*(_one(p) for p in chunk))

        out: list[Stat | None] = []
        for i in range(0, len(paths), batch):
            out.extend(self._run(_all(paths[i : i + batch])))
        return out

    # -- open-file bookkeeping -------------------------------------------------
    # The phone allows only so many open files per session. Every open is tracked; if the
    # phone still says "no resources", a fresh session makes it release every slot.
    def _track(self, h, path):
        with self._files_lock:
            self._open_files[h] = (path, time.monotonic())

    @property
    def open_file_count(self) -> int:
        with self._files_lock:
            return len(self._open_files)

    def oldest_open_files(self, n=3):
        with self._files_lock:
            items = sorted(self._open_files.values(), key=lambda v: v[1])[:n]
        now = time.monotonic()
        return [f"{p} ({now - t:.0f} s)" for p, t in items]

    def reset_session(self, reason: str = "") -> bool:
        """Open a fresh file session with the phone (it then releases every file slot the old
        session held). Read-only; nothing on the phone changes. Handles from the old session
        become invalid, so callers retry what they were doing."""
        with self._reset_lock:
            if self._afc is None or self._lost:
                return False
            log.warning("starting a fresh session with the iPhone (%s); %d files were open",
                        reason or "asked", self.open_file_count)
            from pymobiledevice3.services.afc import AfcService
            old = self._afc

            async def _new():
                afc = AfcService(lockdown=self._lockdown)
                await afc.__aenter__()
                return afc

            try:
                new = asyncio.run_coroutine_threadsafe(_new(), self._loop).result(30)
            except Exception:
                log.exception("could not start a fresh session")
                return False
            self._afc = new
            with self._files_lock:
                self._open_files.clear()

            async def _close_old():
                try:
                    await old.aclose()
                except Exception:
                    pass

            asyncio.run_coroutine_threadsafe(_close_old(), self._loop)
            self.session_resets += 1
            return True

    @staticmethod
    def _no_resources(e) -> bool:
        return is_no_resources(e)

    def open(self, path: str) -> int:
        # "NO_RESOURCES" here can mean the original is only in iCloud (Optimize iPhone
        # Storage); it is passed on as is and the caller decides (see is_not_on_phone()).
        h = self._run(self._svc.fopen(path, "r"))
        self._track(h, path)
        return h

    def read(self, handle: int, size: int) -> bytes:
        return self._run(self._svc.fread(handle, size))

    def pread(self, handle: int, size: int, offset: int) -> bytes:
        """Read `size` bytes at `offset` (random access; used for streaming video)."""
        return self._run(self._svc.pread(handle, size, offset))

    def close_file(self, handle: int) -> None:
        with self._files_lock:
            known = self._open_files.pop(handle, None)
        if known is None:
            return   # from an earlier session (already released by the phone)
        try:
            self._run(self._svc.fclose(handle), timeout=30)
        except Exception as e:
            # don't hide it: a close the phone didn't accept keeps one of its file slots used
            self._close_failures += 1
            if self._close_failures <= 5:
                log.warning("could not close %s on the iPhone: %s: %s", known[0], type(e).__name__, e)

    def read_all(self, path: str, limit: int | None = None) -> bytes:
        h = self.open(path)
        try:
            if limit is not None:
                return self.read(h, limit)
            chunks = []
            while True:
                b = self.read(h, 4 * 1024 * 1024)
                if not b:
                    break
                chunks.append(b)
                if len(b) < 4 * 1024 * 1024:
                    break
            return b"".join(chunks)
        finally:
            self.close_file(h)

    def head_hash(self, path: str, size: int) -> bytes:
        """SHA-1 of the first 64 KB, computed HERE from an ordinary read.

        (The phone can compute it itself (GET_FILE_HASH_RANGE), but on iOS 27 thousands
        of those requests left the phone's file service "out of resources" until the
        phone was restarted, so the app never asks the phone to hash anything.)"""
        n = min(HASH_HEAD, size)
        if n == 0:
            return hashlib.sha1(b"").digest()
        return hashlib.sha1(self.read_all(path, limit=n)).digest()

    def full_hash(self, path: str) -> bytes | None:
        """Not used on a real iPhone: asking the phone to hash files wore out its file
        service on iOS 27 (see head_hash). Copies are verified from ordinary reads."""
        return None


class FolderBackend:
    """A local folder that behaves like a phone. Used for --demo and testing.

    Point it at a folder containing DCIM/100APPLE/... (or any folder of
    photos - it will be presented as DCIM/100APPLE).
    """

    kind = "folder"

    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(root)
        self.name = f"Demo phone ({os.path.basename(self.root)})"
        self.ios = "demo"
        self.model = "Folder"
        self._files: dict[int, object] = {}
        self._next = 1
        self._lock = threading.Lock()
        self._flat = not os.path.isdir(os.path.join(self.root, "DCIM"))

    on_disconnect = None
    _gone = False

    def connect(self) -> None:
        if not os.path.isdir(self.root):
            raise DeviceError(f"Demo folder not found: {self.root}")
        self._gone = False

    def close(self) -> None:
        pass

    def unplug(self) -> None:
        """Testing: behave like a cable that was just pulled."""
        self._gone = True
        if self.on_disconnect:
            self.on_disconnect()

    @property
    def connected(self) -> bool:
        return not self._gone

    def _check(self):
        if self._gone:
            raise DeviceGone("The iPhone was disconnected.")

    def _local(self, path: str) -> str:
        rel = path.lstrip("/")
        if self._flat:
            if rel in ("DCIM", "DCIM/"):
                return None  # virtual
            if rel.startswith("DCIM/100APPLE"):
                rel = rel[len("DCIM/100APPLE") :].lstrip("/")
        return os.path.join(self.root, rel)

    def listdir(self, path: str) -> list[str]:
        self._check()
        if self._flat and path.rstrip("/") == "/DCIM":
            return ["100APPLE"]
        return sorted(os.listdir(self._local(path)))

    def stat(self, path: str) -> Stat:
        self._check()
        if self._flat and path.rstrip("/") == "/DCIM":
            now = dt.datetime.now()
            return Stat(0, now, now, True)
        st = os.stat(self._local(path))
        m = dt.datetime.fromtimestamp(st.st_mtime)
        return Stat(st.st_size, m, m, os.path.isdir(self._local(path)), getattr(st, "st_blocks", None))

    def stat_many(self, paths: list[str], batch: int = 64) -> list[Stat | None]:
        out = []
        for p in paths:
            try:
                out.append(self.stat(p))
            except OSError:
                out.append(None)
        return out

    def open(self, path: str) -> int:
        self._check()
        f = open(self._local(path), "rb")
        with self._lock:
            h = self._next
            self._next += 1
            self._files[h] = f
        return h

    def read(self, handle: int, size: int) -> bytes:
        self._check()
        return self._files[handle].read(size)

    def pread(self, handle: int, size: int, offset: int) -> bytes:
        self._check()
        with self._lock:
            f = self._files[handle]
            f.seek(offset)
            return f.read(size)

    def close_file(self, handle: int) -> None:
        with self._lock:
            f = self._files.pop(handle, None)
        if f:
            f.close()

    def read_all(self, path: str, limit: int | None = None) -> bytes:
        self._check()
        with open(self._local(path), "rb") as f:
            return f.read(limit) if limit is not None else f.read()

    def head_hash(self, path: str, size: int) -> bytes:
        self._check()
        return hashlib.sha1(self.read_all(path, limit=min(HASH_HEAD, size))).digest()

    def full_hash(self, path: str) -> bytes | None:
        self._check()
        h = hashlib.sha1()
        with open(self._local(path), "rb") as f:
            for b in iter(lambda: f.read(4 * 1024 * 1024), b""):
                h.update(b)
        return h.digest()
