# SPDX-License-Identifier: GPL-3.0-or-later
"""Knows what is already in the backup folder(s).

Matching is by CONTENT, not by name: an item counts as backed up if some file
in a backup folder has the same size AND the same SHA-1 of its first 64 KB.
The phone-side hash is computed on the phone itself, so checking 20,000 items
moves almost nothing over USB. Renamed or re-sorted files still match.

HEIC photos saved as JPEG can't match by content, so each backup folder keeps a
small record (.lptpro/converted.json) of "this phone original -> this
JPEG". It only counts while that JPEG still exists.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading

from . import _OLD_BACKUP_META_DIR, BACKUP_META_DIR
from .device import HASH_HEAD
from .media import MEDIA_EXT

log = logging.getLogger("transfer")


def hide_folder(path: str) -> None:
    """Windows: mark one of LPT Pro's own bookkeeping folders as hidden (other systems
    already hide names starting with a dot)."""
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x2)
        except Exception:
            pass


def local_head_hash(path: str, size: int) -> bytes:
    with open(path, "rb") as f:
        return hashlib.sha1(f.read(min(HASH_HEAD, size))).digest()


class BackupIndex:
    def __init__(self, roots: list[str], cache_file: str | None = None) -> None:
        self.roots = [os.path.abspath(r) for r in roots if r]
        self.cache_file = cache_file
        self._lock = threading.Lock()
        self.by_size: dict[int, list[str]] = {}
        self._fp_cache: dict[str, tuple[float, int, str]] = {}  # path -> (mtime, size, hexhash)
        # phone size -> {phone head hash: {"path": abs JPEG path, "size": JPEG size, "head": JPEG head hash}}
        self.converted: dict[int, dict[str, dict]] = {}
        self._ledger_dirty = False
        self._load_cache()

    # -- persistence of local fingerprints (makes re-checks fast) ----------
    def _load_cache(self):
        if self.cache_file and os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                self._fp_cache = {k: tuple(v) for k, v in raw.items()}
            except Exception:
                self._fp_cache = {}

    def save_cache(self):
        if not self.cache_file:
            return
        try:
            os.makedirs(os.path.dirname(self.cache_file), exist_ok=True)
            tmp = self.cache_file + ".tmp"
            with self._lock:
                data = dict(self._fp_cache)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, self.cache_file)
        except Exception:
            pass

    # -- indexing -----------------------------------------------------------
    def build(self, progress=None, cancelled=lambda: False) -> int:
        by_size: dict[int, list[str]] = {}
        count = 0
        for root in self.roots:
            if not os.path.isdir(root):
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                if cancelled():
                    return count
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for n in filenames:
                    if os.path.splitext(n)[1].lower() not in MEDIA_EXT:
                        continue
                    p = os.path.join(dirpath, n)
                    try:
                        size = os.path.getsize(p)
                    except OSError:
                        continue
                    by_size.setdefault(size, []).append(p)
                    count += 1
                    if progress and count % 500 == 0:
                        progress(f"Indexing your backup folder… {count:,} files")
        converted: dict[int, dict[str, dict]] = {}
        for root in self.roots:
            for key, val in self._read_ledger(root).items():
                size, _, h = key.partition(":")
                e = dict(val) if isinstance(val, dict) else {"path": val}   # older format: a bare path
                e["path"] = os.path.join(root, e["path"])
                converted.setdefault(int(size), {})[h] = e
        with self._lock:
            self.by_size = by_size
            # keep entries made in this session that aren't saved to disk yet
            for size, d in self.converted.items():
                for h, e in d.items():
                    converted.setdefault(size, {})[h] = e
            self.converted = converted
        return count

    # -- converted-to-JPEG record -------------------------------------------
    @staticmethod
    def _ledger_path(root: str) -> str:
        meta = os.path.join(root, BACKUP_META_DIR)
        old = os.path.join(root, _OLD_BACKUP_META_DIR)
        if not os.path.exists(meta) and os.path.isdir(old):
            try:
                os.rename(old, meta)       # folder name used by test builds
            except OSError:
                return os.path.join(old, "converted.json")
        return os.path.join(meta, "converted.json")

    def _read_ledger(self, root: str) -> dict[str, str]:
        try:
            with open(self._ledger_path(root), "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def add_converted(self, phone_size: int, phone_head_hash: bytes, local_path: str,
                      original_name: str | None = None) -> None:
        p = os.path.abspath(local_path)
        try:
            jsize = os.path.getsize(p)
            entry = {"path": p, "size": jsize, "head": local_head_hash(p, jsize).hex()}
        except OSError:
            entry = {"path": p}
        if original_name:
            entry["name"] = original_name      # lets iCloud-only items find their JPEG by name
        with self._lock:
            self.converted.setdefault(phone_size, {})[phone_head_hash.hex()] = entry
            self._ledger_dirty = True

    def is_converted(self, local_path: str) -> bool:
        p = os.path.abspath(local_path)
        with self._lock:
            return any(p == e["path"] for d in self.converted.values() for e in d.values())

    def save_ledger(self) -> None:
        """Write the record into each backup folder (atomically). Never touches media files."""
        with self._lock:
            if not self._ledger_dirty:
                return
            items = [(s, h, e) for s, d in self.converted.items() for h, e in d.items()]
            self._ledger_dirty = False
        for root in self.roots:
            mine = {f"{s}:{h}": dict(e, path=os.path.relpath(e["path"], root)) for s, h, e in items
                    if os.path.commonpath([os.path.abspath(e["path"]), root]) == root}
            if not mine:
                continue
            path = self._ledger_path(root)
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                hide_folder(os.path.dirname(path))
                with open(path + ".tmp", "w", encoding="utf-8") as f:
                    json.dump(mine, f, indent=0)
                os.replace(path + ".tmp", path)
            except OSError:
                log.exception("could not save converted record in %s", root)

    def add(self, path: str) -> None:
        try:
            size = os.path.getsize(path)
        except OSError:
            return
        with self._lock:
            lst = self.by_size.setdefault(size, [])
            if path not in lst:
                lst.append(path)

    def refresh_dir(self, folder: str) -> None:
        """Bring one folder up to date (files deleted, renamed or added there)."""
        folder = os.path.abspath(folder)
        present: dict[str, int] = {}
        try:
            for n in os.listdir(folder):
                if os.path.splitext(n)[1].lower() in MEDIA_EXT:
                    p = os.path.join(folder, n)
                    try:
                        present[p] = os.path.getsize(p)
                    except OSError:
                        pass
        except OSError:
            pass  # folder itself is gone
        with self._lock:
            for size, lst in list(self.by_size.items()):
                keep = [p for p in lst if os.path.dirname(p) != folder or p in present]
                if keep:
                    self.by_size[size] = keep
                else:
                    del self.by_size[size]
            for p, size in present.items():
                lst = self.by_size.setdefault(size, [])
                if p not in lst:
                    lst.append(p)

    def _fp(self, path: str, size: int) -> str | None:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return None
        with self._lock:
            c = self._fp_cache.get(path)
        if c and c[0] == mtime and c[1] == size:
            return c[2]
        try:
            h = local_head_hash(path, size).hex()
        except OSError:
            return None
        with self._lock:
            self._fp_cache[path] = (mtime, size, h)
        return h

    @staticmethod
    def _stem_key(name: str) -> str:
        """IMG_1234.HEIC, img_1234 1.heic, IMG_1234 (1).HEIC -> "img_1234" """
        import re
        stem = os.path.splitext(os.path.basename(name))[0].lower()
        return re.sub(r"(\s*\(\d+\)|\s+\d+)$", "", stem)

    def find_by_name_size(self, name: str, size: int) -> str | None:
        """For originals that are only in iCloud (nothing on the phone to fingerprint):
        a backup file with the same name and exactly the same size."""
        key = self._stem_key(name)
        with self._lock:
            candidates = list(self.by_size.get(size, ()))
        for p in candidates:
            if self._stem_key(p) == key and os.path.isfile(p):
                return p
        with self._lock:
            conv = list(self.converted.get(size, {}).values())
        for e in conv:                          # saved as JPEG: the record knows the original name
            if e.get("name") and self._stem_key(e["name"]) == key and os.path.isfile(e["path"]):
                return e["path"]
        return None

    _FAMILY = {".jpeg": ".jpg", ".heif": ".heic", ".hif": ".heic", ".m4v": ".mp4", ".tiff": ".tif"}

    @classmethod
    def _family(cls, name: str) -> str:
        ext = os.path.splitext(name)[1].lower()
        return cls._FAMILY.get(ext, ext)

    @staticmethod
    def _same_moment(path: str, when) -> bool:
        """The file's date (LPT Pro sets it to the date taken) matches `when`: exactly, or off by
        whole quarter hours (a different time zone), up to a day."""
        try:
            diff = abs(os.path.getmtime(path) - when.timestamp())
        except (OSError, ValueError, OverflowError, AttributeError):
            return False
        if diff > 26 * 3600:
            return False
        r = diff % 900
        return r <= 2 or r >= 898

    def find_icloud(self, name: str, size: int, when=None) -> str | None:
        """An iCloud-only item in the backup: same name and size, or (whatever the file is called,
        e.g. after a download from iCloud) the same exact size, file type and date taken."""
        if not size:
            return None
        hit = self.find_by_name_size(name, size)
        if hit or when is None:
            return hit
        fam = self._family(name)
        with self._lock:
            candidates = list(self.by_size.get(size, ()))
            conv = list(self.converted.get(size, {}).values())
        for p in candidates:
            if self._family(p) == fam and os.path.isfile(p) and self._same_moment(p, when):
                return p
        for e in conv:                       # saved as JPEG: the record keeps the original's size
            p = e.get("path", "")
            if fam in (".heic",) and os.path.isfile(p) and self._same_moment(p, when):
                return p
        return None

    def find_match(self, size: int, phone_head_hash) -> str | None:
        """phone_head_hash is a callable so the phone is only asked when needed."""
        with self._lock:
            candidates = list(self.by_size.get(size, ()))
            conv = dict(self.converted.get(size, {}))
        if not candidates and not conv:
            return None  # nothing that size on the PC -> definitely not backed up
        want = phone_head_hash().hex()
        if want in conv:
            e = conv[want]
            if os.path.isfile(e["path"]):
                return e["path"]
            # the JPEG was renamed or moved inside the backup: find it by its own content
            if "size" in e:
                with self._lock:
                    jc = list(self.by_size.get(e["size"], ()))
                for p in jc:
                    if self._fp(p, e["size"]) == e["head"]:
                        with self._lock:
                            e["path"] = p
                            self._ledger_dirty = True
                        return p
        for p in candidates:
            if self._fp(p, size) == want:
                return p
        return None
