# SPDX-License-Identifier: GPL-3.0-or-later
"""Background workers. The UI thread never touches the phone or the disk."""
from __future__ import annotations

import collections
import hashlib
import io
import logging
import os
import sys
import threading
import time

from PySide6.QtCore import QThread, Signal

from . import _OLD_TEMP_DIR_NAME, TEMP_DIR_NAME
from .backup_index import hide_folder
from .device import DeviceError, DeviceGone, is_no_resources, is_not_on_phone
from . import heic
from .media import TEMPLATES, MediaItem, capture_date, capture_date_f, scan_phone, set_file_times

log = logging.getLogger("transfer")


def background_priority(level: str = "background") -> None:
    """Make the CURRENT thread give way to video playback and everything on screen.

    "background": lowest CPU priority, and on Windows also low disk / memory priority
                  (previews, backup check, folder watching).
    "below":      a little below normal (copying: still quick, but never in the way).
    """
    try:
        if sys.platform == "win32":
            import ctypes
            k32 = ctypes.windll.kernel32
            h = k32.GetCurrentThread()
            if level == "background":
                k32.SetThreadPriority(h, 0x00010000)   # THREAD_MODE_BACKGROUND_BEGIN
            else:
                k32.SetThreadPriority(h, -1)           # THREAD_PRIORITY_BELOW_NORMAL
        else:
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 15 if level == "background" else 5)
    except Exception:
        pass


class ConnectAndScan(QThread):
    status = Signal(str)
    connected = Signal(str)          # device label
    failed = Signal(str)
    scanned = Signal(list)           # list[MediaItem]

    def __init__(self, device):
        super().__init__()
        self.device = device
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            self.status.emit("Connecting to iPhone… (unlock it and tap Trust if asked)")
            self.device.connect()
            label = self.device.name + (f" · iOS {self.device.ios}" if self.device.ios and self.device.ios != "demo" else "")
            self.connected.emit(label)
            items = scan_phone(self.device, progress=self.status.emit, cancelled=lambda: self._cancel)
            confirm_cloud_hints(self.device, items)
            if not self._cancel:
                self.scanned.emit(items)
        except DeviceError as e:
            self.failed.emit(str(e))
        except Exception as e:
            log.exception("connect/scan failed")
            if is_no_resources(e):
                self.failed.emit("The iPhone’s file sharing has stopped responding (it ran out of "
                                 "resources). Restart the iPhone (turn it off and on again), unlock "
                                 "it, then press Retry. Nothing on the iPhone was changed.")
                return
            self.failed.emit(f"Lost contact with the iPhone ({type(e).__name__}). Check the cable, unlock it, and press Retry.")


def confirm_cloud_hints(device, items) -> int:
    """Items that use no storage on the phone are probably only in iCloud. Check a few by
    trying to read one byte (read-only); trust the hint only if they really can't be read."""
    hinted = [i for i in items if i.extra.get("cloud_hint")]
    if not hinted:
        return 0
    tried = refused = 0
    for it in hinted[:: max(1, len(hinted) // 3)][:3]:
        tried += 1
        try:
            device.read_all(it.path, limit=1)
        except DeviceGone:
            raise
        except Exception as e:
            refused += is_not_on_phone(e, it)
    # control: an ordinary file must read fine at the same moment, or the refusals above
    # mean the phone's file service is in trouble, not that those originals are in iCloud
    healthy = True
    for it in [i for i in items if not i.extra.get("cloud_hint")][:2]:
        try:
            device.read_all(it.path, limit=1)
        except DeviceGone:
            raise
        except Exception:
            healthy = False
    trusted = tried > 0 and refused == tried and healthy
    for it in hinted:
        it.extra.pop("cloud_hint", None)
        if trusted:
            it.extra["cloud"] = True
    log.info("iCloud check: %d items use no storage on the iPhone; %d of %d tried could not be read; "
             "ordinary files %s -> %s", len(hinted), refused, tried, "read fine" if healthy else "FAILED too",
             "marked as only in iCloud" if trusted else "not trusted (ignored)")
    return len(hinted) if trusted else 0


class AuditWorker(QThread):
    status = Signal(str)
    results = Signal(list)    # list[(phone path, matching PC path or "")]
    finished_ok = Signal()

    def __init__(self, device, index, items: list[MediaItem]):
        super().__init__()
        self.device, self.index, self.items = device, index, list(items)
        self._cancel = False
        self._go = threading.Event()
        self._go.set()

    def cancel(self):
        self._cancel = True
        self._go.set()

    def pause(self, on: bool):
        (self._go.clear if on else self._go.set)()

    def run(self):
        background_priority()
        try:
            self.index.build(progress=self.status.emit, cancelled=lambda: self._cancel)
            batch, total = [], len(self.items)
            for i, it in enumerate(self.items):
                if self._cancel:
                    return
                self._go.wait()   # paused while a video plays
                try:
                    if it.extra.get("cloud"):
                        match = self.index.find_by_name_size(it.name, it.size)
                    else:
                        match = self.index.find_match(it.size, lambda it=it: self.device.head_hash(it.path, it.size))
                except DeviceGone:
                    if batch:
                        self.results.emit(batch)
                    self.status.emit("Backup check paused: the iPhone was disconnected.")
                    return
                except Exception as e:
                    if is_not_on_phone(e, it):      # only in iCloud: compare by name and size
                        it.extra["cloud"] = True
                        match = self.index.find_by_name_size(it.name, it.size)
                    else:
                        log.exception("audit failed for %s", it.path)
                        match = None
                batch.append((it.path, match or ""))
                if len(batch) >= 200 or i == total - 1:
                    self.results.emit(batch)
                    batch = []
                    self.status.emit(f"Checking what’s already backed up… {i + 1:,}/{total:,}")
            self.index.save_cache()
            self.finished_ok.emit()
        except Exception as e:
            log.exception("audit failed")
            self.status.emit(f"Backup check stopped: {e}")


class CopyWorker(QThread):
    """Copies items phone -> PC. Verified, never overwrites, never deletes anything."""

    progress = Signal(int, int, str, float, float)   # done, total, current name, bytes/sec, eta sec
    item_done = Signal(str, str)                     # phone path, local path
    item_failed = Signal(str, str)                   # phone path, reason
    finished_all = Signal(int, int, int, bool)       # copied, skipped, failed, cancelled
    device_lost = Signal(int)                         # copied-and-verified count when the phone went away
    stopped_failing = Signal(int, str)                # files failed in a row, last reason
    item_cloud = Signal(str)                          # phone path: original only in iCloud (skipped)
    note = Signal(str)                                # one-off user notice (e.g. no HEIC decoder)

    CHUNK = 4 * 1024 * 1024

    def __init__(self, device, index, items: list[MediaItem], dest_root: str, template: str,
                 set_dates=True, include_aae=False, skip_backed_up=True,
                 heic_to_jpeg=False, keep_heic=False):
        super().__init__()
        self.device, self.index = device, index
        self.items = list(items)
        self.dest_root = dest_root
        self.template = TEMPLATES.get(template, TEMPLATES["Year / Month"])
        self.set_dates, self.include_aae, self.skip_backed_up = set_dates, include_aae, skip_backed_up
        self.heic_to_jpeg, self.keep_heic = heic_to_jpeg, keep_heic
        self.cloud_skipped = 0
        self._heic_warned = False
        self.kept_as_heic = 0
        self._pause = threading.Event()
        self._pause.set()
        self._cancel = False

    def pause(self, on: bool):
        (self._pause.clear if on else self._pause.set)()

    def cancel(self):
        self._cancel = True
        self._pause.set()

    def run(self):
        background_priority("below")
        total = len(self.items)
        total_bytes = sum(i.size for i in self.items) or 1
        copied = skipped = failed = 0
        moved = 0
        t0 = time.monotonic()
        tmp_dir = os.path.join(self.dest_root, TEMP_DIR_NAME)
        os.makedirs(tmp_dir, exist_ok=True)
        hide_folder(tmp_dir)
        for d in (tmp_dir, os.path.join(self.dest_root, _OLD_TEMP_DIR_NAME)):
            _clear_partials(d)    # leftovers from a copy that was cut off last time
        try:
            os.rmdir(os.path.join(self.dest_root, _OLD_TEMP_DIR_NAME))
        except OSError:
            pass
        lost = False
        in_a_row, last_reason, retried, streak = 0, "", set(), []

        for n, it in enumerate(self.items):
            if self._cancel:
                break
            self._pause.wait()
            elapsed = max(time.monotonic() - t0, 0.001)
            speed = moved / elapsed
            eta = (total_bytes - moved) / speed if speed > 0 else 0
            self.progress.emit(n, total, it.name, speed, eta)

            if it.extra.get("cloud"):
                self.cloud_skipped += 1          # only in iCloud: nothing on the phone to copy
                self.item_cloud.emit(it.path)
                continue
            try:
                if self.skip_backed_up:
                    m = self.index.find_match(it.size, lambda it=it: self.device.head_hash(it.path, it.size))
                    if m:
                        skipped += 1
                        moved += it.size
                        self.item_done.emit(it.path, m)
                        in_a_row, streak = 0, []
                        continue
                local = self._copy_one(it, tmp_dir, lambda b: None)
                if local is None:  # cancelled mid-file
                    break
                moved += it.size
                copied += 1
                self.index.add(local)
                self.item_done.emit(it.path, local)
                if self.include_aae and it.sidecar:
                    self._copy_sidecar(it, local)
            except DeviceGone:
                # Cable pulled: stop at once. The file in progress was never moved into
                # the backup folder, so there is nothing half-written to clean up there.
                log.warning("iPhone disconnected during copy at %s", it.path)
                lost = True
                break
            except Exception as e:
                if is_not_on_phone(e, it):
                    # not a failure of the copy: the original isn't on the phone (iCloud only)
                    log.info("only in iCloud, skipped: %s", it.path)
                    it.extra["cloud"] = True
                    self.cloud_skipped += 1
                    self.item_cloud.emit(it.path)
                    continue
                log.exception("copy failed: %s", it.path)
                in_a_row += 1
                last_reason = str(e)
                streak.append(it)
                failed += 1
                # Several failures in a row usually mean the phone connection is in a bad
                # state, not that the files are bad: start a fresh session and retry them.
                if in_a_row == 3 and hasattr(self.device, "reset_session") and it.path not in retried \
                        and self.device.reset_session(f"{in_a_row} copies failed in a row"):
                    ok_again = 0
                    for again in streak:
                        retried.add(again.path)
                        try:
                            local = self._copy_one(again, tmp_dir, lambda b: None)
                        except DeviceGone:
                            lost = True
                            break
                        except Exception as e2:
                            last_reason = str(e2)
                            continue
                        if local is not None:
                            failed -= 1
                            ok_again += 1
                            moved += again.size
                            copied += 1
                            self.index.add(local)
                            self.item_done.emit(again.path, local)
                    if lost:
                        break
                    if ok_again:
                        log.info("fresh session worked: %d of %d failed files copied on retry", ok_again, len(streak))
                        in_a_row, streak = 0, []
                        continue
                self.item_failed.emit(it.path, last_reason)
                if in_a_row >= 5:
                    log.warning("copy stopped: %d files in a row failed (%s)", in_a_row, last_reason)
                    self.stopped_failing.emit(in_a_row, last_reason)
                    self._cancel = True
                    break
                continue
            in_a_row, streak = 0, []

        _clear_partials(tmp_dir)
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass
        self.index.save_cache()
        self.index.save_ledger()
        self.progress.emit(total, total, "", 0, 0)
        if lost:
            self.device_lost.emit(copied)
        self.finished_all.emit(copied, skipped, failed, self._cancel or lost)

    # -- one file -----------------------------------------------------------
    def _copy_one(self, it: MediaItem, tmp_dir: str, on_bytes) -> str | None:
        tmp = os.path.join(tmp_dir, it.name + ".part")
        try:
            return self._copy_one_inner(it, tmp)
        finally:
            for t in (tmp, tmp + ".jpg"):
                if os.path.exists(t):
                    os.remove(t)  # our own temp file, never the user's

    def _copy_one_inner(self, it: MediaItem, tmp: str) -> str | None:
        sha = hashlib.sha1()
        h = self.device.open(it.path)
        try:
            with open(tmp, "wb") as out:
                remaining = it.size
                while remaining > 0:
                    if self._cancel:
                        return None
                    self._pause.wait()
                    chunk = self.device.read(h, min(self.CHUNK, remaining))
                    if not chunk:
                        break
                    out.write(chunk)
                    sha.update(chunk)
                    remaining -= len(chunk)
                out.flush()
                os.fsync(out.fileno())
        finally:
            self.device.close_file(h)

        # -- verify: size, then phone-side hash vs. a fresh re-read from disk
        if os.path.getsize(tmp) != it.size:
            raise IOError("Size mismatch after copy - file not saved")
        disk = hashlib.sha1()
        with open(tmp, "rb") as f:
            for b in iter(lambda: f.read(self.CHUNK), b""):
                disk.update(b)
        # 1) every byte as it arrived from the phone == a fresh re-read of the saved file
        phone = self.device.full_hash(it.path) or sha.digest()
        if disk.digest() != phone:
            raise IOError("Verification failed (data differs from the phone) - file not saved")
        # 2) read the start and the end of the file from the phone AGAIN and compare: catches
        #    a short or misaligned transfer without asking the phone to compute anything
        self._spot_check(it, tmp)

        # -- where it goes, using the date stored inside the file
        when = capture_date(tmp, it.name) or it.date
        folder = os.path.join(self.dest_root, self.template(when))
        os.makedirs(folder, exist_ok=True)

        # -- HEIC -> JPEG, made from the VERIFIED original (never from unverified data)
        if self.heic_to_jpeg and it.ext in heic.HEIF_EXT:
            jpeg_dest = self._save_as_jpeg(it, tmp, folder, when)
            if jpeg_dest and not self.keep_heic:
                return jpeg_dest
            # keep_heic, or conversion impossible: the original is saved as well / instead

        return self._place(tmp, folder, it.name, disk.digest(), when)

    SPOT = 64 * 1024

    def _spot_check(self, it: MediaItem, local: str) -> None:
        n = min(self.SPOT, it.size)
        if n == 0:
            return
        with open(local, "rb") as f:
            head = f.read(n)
            f.seek(max(0, it.size - n))
            tail = f.read(n)
        if self.device.read_all(it.path, limit=n) != head:
            raise IOError("Verification failed (start of file differs from the phone) - file not saved")
        if it.size > n:
            h = self.device.open(it.path)
            try:
                phone_tail = self.device.pread(h, n, it.size - n)
            finally:
                self.device.close_file(h)
            if phone_tail != tail:
                raise IOError("Verification failed (end of file differs from the phone) - file not saved")

    def _place(self, src: str, folder: str, name: str, digest: bytes, when) -> str:
        dest = self._unique(folder, name, digest)
        if dest is None:  # identical file already there
            return os.path.join(folder, name)
        os.replace(src, dest)
        if self.set_dates:
            try:
                set_file_times(dest, when)
            except Exception:
                log.exception("could not set dates on %s", dest)
        return dest

    def _save_as_jpeg(self, it: MediaItem, verified_heic: str, folder: str, when) -> str | None:
        try:
            with open(verified_heic, "rb") as f:
                data = f.read()
            jpeg = heic.to_jpeg(data)
        except heic.HeicUnavailable as e:
            if not self._heic_warned:
                self._heic_warned = True
                self.note.emit(str(e) + " Until then, HEIC photos are saved as HEIC.")
            self.kept_as_heic += 1
            return None
        except Exception:
            log.exception("HEIC conversion failed for %s; keeping the original", it.path)
            self.kept_as_heic += 1
            return None
        # check the JPEG really opens before it counts
        from PIL import Image
        with Image.open(io.BytesIO(jpeg)) as im:
            im.load()
        tmp = verified_heic + ".jpg"
        with open(tmp, "wb") as out:
            out.write(jpeg)
            out.flush()
            os.fsync(out.fileno())
        dest = self._place(tmp, folder, it.stem + ".JPG", hashlib.sha1(jpeg).digest(), when)
        from .backup_index import local_head_hash
        self.index.add_converted(it.size, local_head_hash(verified_heic, it.size), dest)
        return dest

    def _unique(self, folder: str, name: str, digest: bytes) -> str | None:
        """Never overwrite. Same name + same content -> None (already there)."""
        stem, ext = os.path.splitext(name)
        cand, k = os.path.join(folder, name), 1
        while os.path.exists(cand):
            try:
                h = hashlib.sha1()
                with open(cand, "rb") as f:
                    for b in iter(lambda: f.read(self.CHUNK), b""):
                        h.update(b)
                if h.digest() == digest:
                    return None
            except OSError:
                pass
            cand = os.path.join(folder, f"{stem} ({k}){ext}")
            k += 1
        return cand

    def _copy_sidecar(self, it: MediaItem, local_media: str):
        try:
            data = self.device.read_all(it.sidecar)
            name = os.path.splitext(os.path.basename(local_media))[0] + ".AAE"
            p = os.path.join(os.path.dirname(local_media), name)
            if not os.path.exists(p):
                with open(p, "wb") as f:
                    f.write(data)
        except Exception:
            log.exception("sidecar copy failed for %s", it.path)


class BackupWatcher(QThread):
    """Keeps "ON PC" honest while the app is open.

    Every few seconds (and right away when the app window becomes active again) it
    looks at the folders that hold the backed-up copies. Only folders whose
    "last changed" time moved are looked into, so this costs almost nothing.
    If a copy was deleted (or moved away), the item is checked again by content:
    another copy elsewhere in the backup still counts; otherwise it's marked
    "not backed up". No file handles are kept open, so folders can still be
    renamed or deleted freely in Explorer.
    """

    results = Signal(list)     # [(phone path, PC path or "")], only for items that changed
    INTERVAL = 3.0

    def __init__(self, device):
        super().__init__()
        self.device = device
        self.index = None
        self._targets: dict[str, tuple] = {}   # phone path -> (PC path, size, MediaItem)
        self._mtimes: dict[str, int | None] = {}
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = False

    # called from the window (UI thread)
    def set_index(self, index):
        with self._lock:
            self.index = index

    def set_target(self, item: MediaItem, local: str):
        with self._lock:
            self._targets[item.path] = (os.path.abspath(local), item.size, item)

    def remove_target(self, phone_path: str):
        with self._lock:
            self._targets.pop(phone_path, None)

    def reset(self):
        with self._lock:
            self._targets.clear()
            self._mtimes.clear()

    def poke(self):
        self._wake.set()

    def stop(self):
        self._stop = True
        self._wake.set()

    @staticmethod
    def _mtime(d):
        try:
            return os.stat(d).st_mtime_ns
        except OSError:
            return None

    def run(self):
        background_priority()
        while not self._stop:
            self._wake.wait(self.INTERVAL)
            self._wake.clear()
            if self._stop:
                return
            try:
                self._check()
            except Exception:
                log.exception("backup watcher")

    def _check(self):
        with self._lock:
            targets = dict(self._targets)
            index = self.index
        if not targets or index is None:
            return
        dirs = {os.path.dirname(t[0]) for t in targets.values()}
        changed = set()
        for d in dirs:
            m = self._mtime(d)
            with self._lock:
                old = self._mtimes.get(d, "new")
                self._mtimes[d] = m
            if old != "new" and old != m:
                changed.add(d)
            elif old == "new" and m is None:
                changed.add(d)          # folder already gone
        if not changed:
            return
        for d in changed:
            index.refresh_dir(d)
        missing = [(pp, t) for pp, t in targets.items()
                   if os.path.dirname(t[0]) in changed and not os.path.isfile(t[0])]
        if not missing:
            return
        # Something disappeared. It may just have been moved to another folder of the
        # backup, so re-list the backup (names and sizes only, quick) before deciding.
        index.build()
        out = []
        for phone_path, (local, size, item) in missing:
            try:
                if item.extra.get("cloud"):     # nothing on the phone to compare with
                    match = index.find_by_name_size(item.name, item.size)
                else:
                    match = index.find_match(size, lambda it=item: self.device.head_hash(it.path, it.size))
            except Exception:
                match = None   # can't confirm (e.g. phone unplugged): don't claim it's backed up
            out.append((phone_path, match or ""))
            with self._lock:
                if match:
                    self._targets[phone_path] = (os.path.abspath(match), size, item)
                else:
                    self._targets.pop(phone_path, None)
        index.save_ledger()
        if out:
            log.info("backup watcher: %d item(s) changed on the PC", len(out))
            self.results.emit(out)


class LibraryReader(QThread):
    """Reads the iPhone's Photos library (read-only): which items are in Recently Deleted,
    and which library items aren't files on this iPhone (originals only in iCloud).
    Emits done(Library, [Asset not on the phone], "") or done(None, [], plain reason)."""

    done = Signal(object, object, str)

    def __init__(self, device, paths_on_phone):
        super().__init__()
        self.device = device
        self.paths = {p.upper() for p in paths_on_phone}

    def run(self):
        background_priority()
        from .photosdb import Unavailable, read_library, where_they_live
        try:
            lib = read_library(self.device)
        except Unavailable as e:
            log.info("Photos library not available: %s", e)
            self.done.emit(None, [], str(e))
            return
        except Exception as e:
            log.warning("Photos library failed", exc_info=True)
            self.done.emit(None, [], f"couldn't be read ({type(e).__name__})")
            return
        missing = lib.not_in(self.paths)
        try:
            where_they_live(self.device, missing)
        except Exception:
            log.debug("library look-around failed", exc_info=True)
        self.done.emit(lib, missing, "")


class CaptureDateReader(QThread):
    """Reads the date each photo/video was TAKEN (from its own metadata) so the grid, the
    days and the Year list match reality. The iPhone's file date can be much later: for
    older photos it's often the day they arrived on this phone (e.g. a restore in 2018).
    Only a few KB per file are read; nothing is decoded. Low priority, pausable."""

    found = Signal(list)          # [(phone path, datetime)] for dates that differ
    progress = Signal(int, int)   # done, total
    WORKERS = 4

    def __init__(self, device, items):
        super().__init__()
        self.device = device
        self.todo = collections.deque(i for i in items if not i.extra.get("cloud"))
        self.total = len(self.todo)
        self.done = 0
        self._lock = threading.Lock()
        self._go = threading.Event()
        self._go.set()
        self._stop = False

    def pause(self, on: bool):
        (self._go.clear if on else self._go.set)()

    def cancel(self):
        self._stop = True
        self._go.set()

    def run(self):
        out: list = []
        out_lock = threading.Lock()

        def flush(force=False):
            with out_lock:
                if out and (force or len(out) >= 300):
                    batch = list(out)
                    out.clear()
                else:
                    batch = None
            if batch:
                self.found.emit(batch)

        def work():
            background_priority()
            while not self._stop:
                self._go.wait()
                with self._lock:
                    if not self.todo:
                        return
                    it = self.todo.popleft()
                try:
                    r = heic.PhoneReader(self.device, it.path, it.size, block=16 * 1024)
                    try:
                        when = capture_date_f(r, it.name)
                    finally:
                        r.close()
                    if when and abs((when - it.date).total_seconds()) > 60:
                        with out_lock:
                            out.append((it.path, when))
                except DeviceGone:
                    self._stop = True
                    return
                except Exception:
                    log.debug("no capture date for %s", it.path, exc_info=True)
                with self._lock:
                    self.done += 1
                    n = self.done
                if n % 200 == 0:
                    self.progress.emit(n, self.total)
                    flush()

        threads = [threading.Thread(target=work, daemon=True) for _ in range(self.WORKERS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        flush(force=True)
        self.progress.emit(self.done, self.total)


def write_atomic(path: str, data: bytes) -> None:
    """Write a preview so that other threads only ever see the complete file:
    write under a temporary name, then rename it into place in one step."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{threading.get_ident()}.tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def _clear_partials(tmp_dir: str) -> None:
    """Remove this app's own unfinished temp files (*.part) from its hidden temp folder."""
    try:
        for n in os.listdir(tmp_dir):
            if n.endswith((".part", ".part.jpg")):
                try:
                    os.remove(os.path.join(tmp_dir, n))
                except OSError:
                    pass
    except OSError:
        pass


class ThumbLoader(QThread):
    """Makes photo thumbnails, cached on disk.

    Two queues: tiles on screen right now (newest request first), and a background
    "prefetch" of the whole camera roll (newest photo first), which only runs when
    nothing is on-screen-waiting and isn't paused (it pauses during copies).
    """

    ready = Signal(str, bytes, int)   # phone path, JPEG bytes, video duration ms (0 = n/a)
    note = Signal(str)                # one-off user notice
    prefetch_progress = Signal(int, int)   # done, total
    needs_decode = Signal(object, bool)    # video with no iPhone thumbnail: (item, on screen?)

    # The iPhone keeps its own ready-made thumbnails (seen on iOS 27):
    #   /PhotoData/Thumbnails/V2/DCIM/162APPLE/IMG_2611.HEIC/5005.JPG          photos (and videos)
    #   /PhotoData/Thumbnails/VideoKeyFrames/DCIM/100APPLE/IMG_0028.MOV/LocalVideoKeyFrame.jpg
    PHONE_THUMBS = ("/PhotoData/Thumbnails/V2{path}/5005.JPG",)
    PHONE_KEYFRAMES = ("/PhotoData/Thumbnails/VideoKeyFrames{path}/LocalVideoKeyFrame.jpg",)

    def __init__(self, device, cache_dir: str):
        super().__init__()
        self.device, self.cache_dir = device, cache_dir
        os.makedirs(cache_dir, exist_ok=True)
        self._stack: list = []
        self._queued: set = set()
        self._cv = threading.Condition()
        self._stop = False
        self._noted = False
        self._paused = False
        self._pf: collections.deque = collections.deque()
        self._pf_all: set = set()
        self._pf_done: set = set()
        self._pf_paused = False
        self._pt_lock = threading.Lock()
        self._pt_ok = self._pt_miss = 0          # tries (for switching the lookup off)
        self._pt_from_phone: set = set()        # items whose tile came from the iPhone
        self._pt_without: set = set()           # items the iPhone had no thumbnail for
        self._pt_off = False

    def prefetch(self, items):
        """Prepare previews for all these photos in the background."""
        with self._cv:
            self._pf = collections.deque(items)
            self._pf_all = {i.path for i in self._pf}
            self._pf_done = set()
            self._pf_paused = False
            self._cv.notify_all()
        self.prefetch_progress.emit(0, len(self._pf_all))

    def pause_prefetch(self, on: bool):
        with self._cv:
            self._pf_paused = on
            self._cv.notify_all()

    def pause(self, on: bool):
        with self._cv:
            self._paused = on
            self._cv.notify_all()

    def request(self, item: MediaItem):
        with self._cv:
            if item.path in self._queued:
                # bump to the top: it's on screen right now
                try:
                    self._stack.remove(item)
                except ValueError:
                    pass
            self._queued.add(item.path)
            self._stack.append(item)
            if len(self._stack) > 400:  # forget stale requests from fast scrolling
                for old in self._stack[:-400]:
                    self._queued.discard(old.path)
                self._stack = self._stack[-400:]
            self._cv.notify()

    def stop(self):
        with self._cv:
            self._stop = True
            self._cv.notify_all()

    def _cache_path(self, it: MediaItem) -> str:
        key = hashlib.sha1(f"{it.path}|{it.size}".encode()).hexdigest()
        return os.path.join(self.cache_dir, key + ".jpg")

    WORKERS = 4   # previews made in parallel (the phone answers several requests at once)

    def run(self):
        extra = [threading.Thread(target=self._loop, name=f"thumbs-{k}", daemon=True)
                 for k in range(1, self.WORKERS)]
        for t in extra:
            t.start()
        self._loop()
        for t in extra:
            t.join(3)

    def _phone_thumb(self, it) -> bytes | None:
        """The iPhone's own JPEG thumbnail for this item, or None. Read-only."""
        if self._pt_off:
            return None
        paths = [t.format(path=it.path) for t in self.PHONE_THUMBS]
        if it.kind == "video":
            paths += [t.format(path=it.path) for t in self.PHONE_KEYFRAMES]
            if it.extra.get("live_photo"):   # Live Photo video: its photo's thumbnail
                paths.append(self.PHONE_THUMBS[0].format(path=it.extra["live_photo"]))
        for p in paths:
            try:
                data = self.device.read_all(p)
            except DeviceGone:
                raise
            except Exception:
                continue
            if data:
                with self._pt_lock:
                    self._pt_ok += 1
                    self._pt_from_phone.add(it.path)
                    if self._pt_ok == 1:
                        log.info("using the iPhone's own thumbnails (first: %s, %d KB)", p, len(data) // 1024)
                return data
        with self._pt_lock:
            self._pt_miss += 1
            self._pt_without.add(it.path)
            if self._pt_ok == 0 and self._pt_miss >= 40 and not self._pt_off:
                self._pt_off = True   # this phone doesn't have them: stop asking
                log.info("no iPhone thumbnails found after %d tries; making previews from the files", self._pt_miss)
        return None

    def _loop(self):
        from PIL import Image, ImageOps
        background_priority()

        while True:
            with self._cv:
                while not self._stop and (self._paused or (
                        not self._stack and (self._pf_paused or not self._pf))):
                    self._cv.wait()
                if self._stop:
                    return
                if self._stack:
                    it, wanted = self._stack.pop(), True
                else:
                    it, wanted = self._pf.popleft(), False
            cp = self._cache_path(it)
            duration = 0
            gone = False
            handed = False
            try:
                cached = b""
                if os.path.exists(cp):
                    with open(cp, "rb") as f:
                        cached = f.read()
                if not wanted and (cached or it.path in self._pf_done):
                    pass  # already prepared earlier: nothing to do
                elif cached:
                    data = cached
                    if it.kind == "video":
                        from .videothumbs import cached_duration
                        duration = cached_duration(cp)
                else:
                    data = b""
                    im = None
                    phone = self._phone_thumb(it)
                    if phone:
                        # the iPhone's own JPEG thumbnail: nothing to decode but a small JPEG
                        src = Image.open(io.BytesIO(phone))
                        src.draft("RGB", (640, 640))
                        im = ImageOps.exif_transpose(src)
                        if it.kind == "video" and it.extra.get("library_only"):
                            duration = it.extra.get("duration", 0)   # from the Photos library
                        elif it.kind == "video":
                            from .media import video_duration_ms
                            r = heic.PhoneReader(self.device, it.path, it.size, block=16 * 1024)
                            try:
                                duration = video_duration_ms(r)
                            finally:
                                r.close()
                            write_atomic(cp + ".dur", str(duration).encode())
                    elif it.extra.get("library_only"):
                        pass   # only in iCloud and no preview on the phone: nothing to read
                    elif it.kind == "video":
                        # no iPhone thumbnail: the video tile maker decodes a frame instead
                        handed = True
                        self.needs_decode.emit(it, wanted)
                    elif it.kind == "photo":
                        if it.ext in heic.HEIF_EXT:
                            # fast: the small preview stored inside the HEIC (~2-5 % of the file)
                            im, _n = heic.fast_thumbnail(self.device, it.path, it.size)
                            if im is None:   # no built-in preview: decode the whole photo
                                im, _ = heic.decode(self.device.read_all(it.path), max_side=512)
                        else:
                            raw = self.device.read_all(it.path)
                            src = Image.open(io.BytesIO(raw))  # noqa
                            src.draft("RGB", (640, 640))  # JPEG: decode at reduced size, much faster
                            im = ImageOps.exif_transpose(src)
                    if im is not None:
                        with im:
                            im.thumbnail((320, 320))
                            buf = io.BytesIO()
                            im.convert("RGB").save(buf, "JPEG", quality=82)
                            data = buf.getvalue()
                        write_atomic(cp, data)
                if wanted and not handed:
                    self.ready.emit(it.path, data, duration)
            except DeviceGone:
                # phone unplugged: leave the tile empty (asked for again on reconnect) and
                # stop background preparation until the phone is back
                gone = True
                with self._cv:
                    self._pf_paused = True
                    if not wanted:
                        self._pf.appendleft(it)
            except heic.HeicUnavailable as e:
                if not self._noted:
                    self._noted = True
                    self.note.emit(str(e))
                if wanted:
                    self.ready.emit(it.path, b"", 0)
            except Exception:
                log.debug("thumb failed for %s", it.path, exc_info=True)
                if wanted:
                    self.ready.emit(it.path, b"", 0)
            finally:
                with self._cv:
                    if wanted:
                        self._queued.discard(it.path)
                    progress = None
                    if not gone and not handed and it.path in self._pf_all and it.path not in self._pf_done:
                        self._pf_done.add(it.path)
                        n, total = len(self._pf_done), len(self._pf_all)
                        if n % 10 == 0 or n == total or wanted:
                            progress = (n, total)
                if progress:
                    self.prefetch_progress.emit(*progress)
