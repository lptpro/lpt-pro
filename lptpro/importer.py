# SPDX-License-Identifier: GPL-3.0-or-later
"""Add photos from a folder or zip files on the PC (for example a download from iCloud).

The files are handed to the same copy engine as the iPhone (CopyWorker): each one is
copied into the backup folder's date structure, verified, skipped if it's already there,
and never overwrites anything. The source files are only read, never moved or deleted.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import os
import threading
import zipfile

from .device import HASH_HEAD, Stat
from .media import MediaItem, kind_of


# Windows "files on demand" (used by iCloud for Windows and similar): a file can be a
# placeholder that only downloads its content when opened.
_OFFLINE = 0x1000
_RECALL_ON_OPEN = 0x40000
_PINNED = 0x80000
_UNPINNED = 0x100000
_RECALL_ON_DATA_ACCESS = 0x400000


def cloud_state(path: str) -> str:
    """"preview" (placeholder: the original is still in the cloud), "kept" (always kept on this
    device), or "local". Only looks at the file's attributes, so nothing is downloaded."""
    try:
        attrs = os.stat(path).st_file_attributes        # Windows only
    except (OSError, AttributeError):
        return "local"
    if attrs & (_RECALL_ON_DATA_ACCESS | _RECALL_ON_OPEN | _OFFLINE):
        return "preview"
    return "kept" if attrs & _PINNED else "local"


def free_up_space(path: str) -> bool:
    """Windows "Free up space" for one file: the cloud copy stays, the PC copy goes back to
    being a preview. The same as right-click -> Free up space; nothing is deleted anywhere."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        attrs = k32.GetFileAttributesW(str(path))
        if attrs == 0xFFFFFFFF:
            return False
        return bool(k32.SetFileAttributesW(str(path), (attrs | _UNPINNED) & ~_PINNED))
    except Exception:
        return False


def looks_like_cloud_folder(path: str) -> bool:
    """A folder kept in sync with a cloud service (deleting there deletes in the cloud too)."""
    parts = [x.lower().replace(" ", "") for x in os.path.abspath(path).replace("\\", "/").split("/")]
    return any(x.startswith(("icloudphotos", "iclouddrive", "onedrive")) for x in parts)


def _inside(path: str, folder: str) -> bool:
    try:
        return os.path.commonpath([os.path.abspath(path), os.path.abspath(folder)]) == os.path.abspath(folder)
    except ValueError:
        return False


def scan_folder(root: str, backup_root: str | None = None) -> list[MediaItem]:
    """Photos and videos anywhere under `root` (paths relative to it, like "/sub/IMG.HEIC")."""
    items = []
    for d, dirs, files in os.walk(root):
        dirs[:] = sorted(x for x in dirs if not x.startswith("."))
        if backup_root and _inside(d, backup_root):
            dirs[:] = []
            continue
        for f in sorted(files):
            kind = kind_of(f)
            if not kind or f.startswith("."):
                continue
            full = os.path.join(d, f)
            try:
                st = os.stat(full)
            except OSError:
                continue
            rel = "/" + os.path.relpath(full, root).replace(os.sep, "/")
            it = MediaItem(path=rel, name=f, size=st.st_size,
                           date=dt.datetime.fromtimestamp(st.st_mtime), kind=kind)
            state = cloud_state(full)
            if state != "local":
                it.extra["cloud_state"] = state
            items.append(it)
    return items


class ZipSource:
    """Read-only access to photos inside zip files, with the same calls as an iPhone.
    Paths look like "/<zip number>/<path inside the zip>"."""

    def __init__(self, zips: list[str]):
        self.zips = [zipfile.ZipFile(z) for z in zips]
        self.names = [os.path.basename(z) for z in zips]
        self.name = ", ".join(self.names)
        self._locks = [threading.Lock() for _ in zips]
        self._open: dict[int, tuple[int, object]] = {}
        self._next = 1
        self._lock = threading.Lock()

    connected = True

    def close(self):
        for z in self.zips:
            z.close()

    def _split(self, path: str) -> tuple[int, str]:
        n, member = path.lstrip("/").split("/", 1)
        return int(n), member

    def items(self) -> list[MediaItem]:
        out = []
        for n, z in enumerate(self.zips):
            for info in z.infolist():
                base = info.filename.rsplit("/", 1)[-1]
                kind = kind_of(base)
                if info.is_dir() or not kind or base.startswith(".") or "__MACOSX" in info.filename:
                    continue
                try:
                    when = dt.datetime(*info.date_time)
                except ValueError:
                    when = dt.datetime(2000, 1, 1)
                out.append(MediaItem(path=f"/{n}/{info.filename}", name=base, size=info.file_size,
                                     date=when, kind=kind))
        return out

    def stat(self, path: str) -> Stat:
        n, member = self._split(path)
        info = self.zips[n].getinfo(member)
        when = dt.datetime(*info.date_time)
        return Stat(info.file_size, when, when, False, None)

    def open(self, path: str) -> int:
        n, member = self._split(path)
        with self._locks[n]:
            f = self.zips[n].open(member)
        with self._lock:
            h = self._next
            self._next += 1
            self._open[h] = (n, f)
        return h

    def read(self, handle: int, size: int) -> bytes:
        n, f = self._open[handle]
        with self._locks[n]:
            return f.read(size)

    def pread(self, handle: int, size: int, offset: int) -> bytes:
        n, f = self._open[handle]
        with self._locks[n]:
            f.seek(offset)
            return f.read(size)

    def close_file(self, handle: int) -> None:
        with self._lock:
            entry = self._open.pop(handle, None)
        if entry:
            entry[1].close()

    def read_all(self, path: str, limit: int | None = None) -> bytes:
        h = self.open(path)
        try:
            return self.read(h, -1 if limit is None else limit)
        finally:
            self.close_file(h)

    def head_hash(self, path: str, size: int) -> bytes:
        return hashlib.sha1(self.read_all(path, limit=min(HASH_HEAD, size))).digest()

    def full_hash(self, path: str) -> bytes | None:
        return None      # the copy is checked against the bytes read while copying
