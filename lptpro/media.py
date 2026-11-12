# SPDX-License-Identifier: GPL-3.0-or-later
"""Media items, phone scanning, capture-date reading and file-time fixing."""
from __future__ import annotations

import datetime as dt
import os
import struct
import sys
from dataclasses import dataclass, field

PHOTO_EXT = {".heic", ".heif", ".jpg", ".jpeg", ".png", ".dng", ".gif", ".webp", ".tif", ".tiff"}
VIDEO_EXT = {".mov", ".mp4", ".m4v", ".avi", ".3gp"}
SIDECAR_EXT = {".aae"}
MEDIA_EXT = PHOTO_EXT | VIDEO_EXT


@dataclass
class MediaItem:
    path: str            # path on the phone, e.g. /DCIM/100APPLE/IMG_0001.HEIC
    name: str
    size: int
    date: dt.datetime    # phone file date (close to capture time); used for grouping
    kind: str            # "photo" | "video"
    backed_up: bool | None = None   # None = not checked yet
    sidecar: str | None = None      # matching .AAE edit file on the phone, if any
    extra: dict = field(default_factory=dict)

    @property
    def stem(self) -> str:
        return os.path.splitext(self.name)[0]

    @property
    def ext(self) -> str:
        return os.path.splitext(self.name)[1].lower()


def kind_of(name: str) -> str | None:
    ext = os.path.splitext(name)[1].lower()
    if ext in PHOTO_EXT:
        return "photo"
    if ext in VIDEO_EXT:
        return "video"
    return None


def scan_phone(device, progress=None, cancelled=lambda: False) -> list[MediaItem]:
    """List every photo/video under /DCIM. Read-only."""
    folders = []
    for d in device.listdir("/DCIM"):
        if d.startswith("."):
            continue
        p = f"/DCIM/{d}"
        try:
            if device.stat(p).is_dir:
                folders.append(p)
        except Exception:
            continue
    folders.sort()

    paths, sidecars = [], {}
    for i, folder in enumerate(folders):
        if cancelled():
            return []
        for n in device.listdir(folder):
            if n.startswith("."):
                continue
            ext = os.path.splitext(n)[1].lower()
            full = f"{folder}/{n}"
            if ext in MEDIA_EXT:
                paths.append(full)
            elif ext in SIDECAR_EXT:
                # IMG_E1234.AAE / IMG_1234.AAE -> keyed by folder + stem
                stem = os.path.splitext(n)[0].replace("IMG_E", "IMG_")
                sidecars[(folder, stem.upper())] = full
        if progress:
            progress(f"Reading folders on the iPhone… {i + 1}/{len(folders)}")

    items: list[MediaItem] = []
    step = 256
    for i in range(0, len(paths), step):
        if cancelled():
            return []
        chunk = paths[i : i + step]
        for p, st in zip(chunk, device.stat_many(chunk)):
            if st is None or st.is_dir:
                continue
            name = p.rsplit("/", 1)[1]
            folder = p.rsplit("/", 1)[0]
            it = MediaItem(p, name, st.size, st.birthtime or st.mtime, kind_of(name))
            if st.blocks == 0 and st.size > 4096:
                # a size but no storage used on the phone: the original is most likely only
                # in iCloud ("Optimize iPhone Storage"). Confirmed before it's trusted.
                it.extra["cloud_hint"] = True
                it.extra["no_storage"] = True
            it.sidecar = sidecars.get((folder, os.path.splitext(name)[0].upper()))
            items.append(it)
        if progress:
            progress(f"Reading the camera roll… {min(i + step, len(paths)):,}/{len(paths):,}")

    mark_live_photos(items)
    items.sort(key=lambda x: (x.date, x.name), reverse=True)
    return items


def mark_live_photos(items: list[MediaItem]) -> None:
    """A Live Photo is a photo + a short video with the same name in the same folder
    (IMG_1234.HEIC + IMG_1234.MOV). Mark the video half as extra["live"]."""
    photos = {(i.path.rsplit("/", 1)[0], i.stem.upper()): i.path for i in items if i.kind == "photo"}
    for i in items:
        if i.kind == "video":
            photo = photos.get((i.path.rsplit("/", 1)[0], i.stem.upper()))
            if photo:
                i.extra["live"] = True
                i.extra["live_photo"] = photo   # its tile can use the photo's thumbnail


# ---------------------------------------------------------------------------
# Capture date from the file itself
# ---------------------------------------------------------------------------
HEIF_EXT = {".heic", ".heif", ".hif"}


def heif_exif(path: str) -> bytes | None:
    """Raw EXIF (TIFF bytes) from a HEIC without decoding the image. Pure Python."""
    from .heic import read_info

    with open(path, "rb") as f:
        return read_info(f).exif


def _parse_exif_dt(s) -> dt.datetime | None:
    if not s:
        return None
    if isinstance(s, bytes):
        s = s.decode(errors="ignore")
    s = str(s).strip().strip("\x00")
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(s[:19], fmt)
        except ValueError:
            pass
    return None


def photo_capture_date(path: str, ext: str | None = None) -> dt.datetime | None:
    try:
        from PIL import Image

        ext = ext or os.path.splitext(path)[1].lower()
        if ext in HEIF_EXT:
            raw = heif_exif(path)
            if not raw:
                return None
            exif = Image.Exif()
            exif.load(raw)
        else:
            with Image.open(path) as im:
                exif = im.getexif()
        try:
            sub = exif.get_ifd(0x8769)
        except Exception:
            sub = {}
        return _parse_exif_dt(sub.get(36867) or sub.get(36868) or exif.get(306))
    except Exception:
        return None


_QT_EPOCH = dt.datetime(1904, 1, 1, tzinfo=dt.timezone.utc)


def video_capture_date(path: str) -> dt.datetime | None:
    """Read the creation time from a .mov/.mp4 (Apple creationdate key, else mvhd)."""
    try:
        with open(path, "rb") as f:
            return video_capture_date_f(f)
    except Exception:
        return None


def video_capture_date_f(f) -> dt.datetime | None:
    """Same, from any seekable file-like object (e.g. a phone reader)."""
    try:
        if True:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            moov = _find_atom(f, 0, size, b"moov")
            if not moov:
                return None
            start, end = moov
            apple = _apple_creationdate(f, start, end)
            if apple:
                return apple
            mvhd = _find_atom(f, start, end, b"mvhd")
            if not mvhd:
                return None
            f.seek(mvhd[0])
            version = f.read(1)[0]
            f.read(3)
            secs = struct.unpack(">Q", f.read(8))[0] if version == 1 else struct.unpack(">I", f.read(4))[0]
            if secs == 0:
                return None
            utc = _QT_EPOCH + dt.timedelta(seconds=secs)
            return utc.astimezone().replace(tzinfo=None)
    except Exception:
        return None


def video_duration_ms(f) -> int:
    """Length of a .mov/.mp4 from its header (moov/mvhd), without decoding anything.
    `f` is any seekable file-like object (a phone reader only fetches the bytes it needs)."""
    try:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        moov = _find_atom(f, 0, size, b"moov")
        if not moov:
            return 0
        mvhd = _find_atom(f, moov[0], moov[1], b"mvhd")
        if not mvhd:
            return 0
        f.seek(mvhd[0])
        version = f.read(1)[0]
        f.read(3)
        if version == 1:
            f.read(16)
            scale, dur = struct.unpack(">IQ", f.read(12))
        else:
            f.read(8)
            scale, dur = struct.unpack(">II", f.read(8))
        return int(dur * 1000 / scale) if scale else 0
    except Exception:
        return 0


def _iter_atoms(f, start, end):
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        hdr = f.read(8)
        if len(hdr) < 8:
            return
        size, typ = struct.unpack(">I4s", hdr)
        hlen = 8
        if size == 1:
            size = struct.unpack(">Q", f.read(8))[0]
            hlen = 16
        elif size == 0:
            size = end - pos
        if size < hlen:
            return
        yield typ, pos + hlen, pos + size
        pos += size


def _find_atom(f, start, end, name):
    for typ, s, e in _iter_atoms(f, start, end):
        if typ == name:
            return s, e
    return None


def _apple_creationdate(f, start, end):
    """moov/meta keys+ilst: com.apple.quicktime.creationdate (has the real local time)."""
    meta = _find_atom(f, start, end, b"meta")
    if not meta:
        return None
    keys_at = _find_atom(f, meta[0], meta[1], b"keys")
    ilst_at = _find_atom(f, meta[0], meta[1], b"ilst")
    if not keys_at or not ilst_at:
        return None
    f.seek(keys_at[0] + 4)
    count = struct.unpack(">I", f.read(4))[0]
    names = []
    for _ in range(count):
        ksize = struct.unpack(">I", f.read(4))[0]
        f.read(4)
        names.append(f.read(ksize - 8))
    try:
        idx = names.index(b"com.apple.quicktime.creationdate") + 1
    except ValueError:
        return None
    for typ, s, e in _iter_atoms(f, ilst_at[0], ilst_at[1]):
        if struct.unpack(">I", typ)[0] == idx:
            data = _find_atom(f, s, e, b"data")
            if data:
                f.seek(data[0] + 8)
                txt = f.read(data[1] - data[0] - 8).decode(errors="ignore")
                try:  # e.g. 2026-10-21T16:12:05-0700
                    t = dt.datetime.strptime(txt[:24], "%Y-%m-%dT%H:%M:%S%z")
                    return t.replace(tzinfo=None)  # wall-clock time where it was shot
                except ValueError:
                    return _parse_exif_dt(txt.replace("T", " "))
    return None


def capture_date_f(f, name: str) -> dt.datetime | None:
    """The date a photo/video was TAKEN, read from a seekable file-like object (only the
    few KB of metadata are read; works on a phone reader without copying the file)."""
    k = kind_of(name)
    ext = os.path.splitext(name)[1].lower()
    try:
        if k == "video":
            return video_capture_date_f(f)
        if k == "photo":
            from PIL import Image
            if ext in HEIF_EXT:
                from .heic import read_info
                raw = read_info(f).exif
                if not raw:
                    return None
                exif = Image.Exif()
                exif.load(raw)
            else:
                f.seek(0)
                with Image.open(f) as im:
                    exif = im.getexif()
            try:
                sub = exif.get_ifd(0x8769)
            except Exception:
                sub = {}
            return _parse_exif_dt(sub.get(36867) or sub.get(36868) or exif.get(306))
    except Exception:
        return None
    return None


def capture_date(path: str, name: str | None = None) -> dt.datetime | None:
    """name = the original file name, for temp files like IMG_0001.HEIC.part."""
    name = name or path
    k = kind_of(name)
    if k == "photo":
        return photo_capture_date(path, os.path.splitext(name)[1].lower())
    if k == "video":
        return video_capture_date(path)
    return None


# ---------------------------------------------------------------------------
# Setting file dates (modified + Windows "Date created")
# ---------------------------------------------------------------------------
def set_file_times(path: str, when: dt.datetime) -> None:
    ts = when.timestamp()
    os.utime(path, (ts, ts))
    if sys.platform == "win32":
        _set_windows_creation_time(path, ts)


def _set_windows_creation_time(path: str, ts: float) -> None:
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.windll.kernel32
    k32.CreateFileW.restype = wintypes.HANDLE
    FILE_WRITE_ATTRIBUTES = 0x100
    OPEN_EXISTING = 3
    h = k32.CreateFileW(path, FILE_WRITE_ATTRIBUTES, 0x7, None, OPEN_EXISTING, 0x80, None)
    if h in (None, wintypes.HANDLE(-1).value):
        return
    try:
        ft = int(ts * 10_000_000) + 116444736000000000
        ctime = wintypes.FILETIME(ft & 0xFFFFFFFF, ft >> 32)
        k32.SetFileTime(h, ctypes.byref(ctime), None, None)
    finally:
        k32.CloseHandle(h)


# ---------------------------------------------------------------------------
# Folder templates
# ---------------------------------------------------------------------------
TEMPLATES = {
    "Year / Month": lambda d: os.path.join(f"{d:%Y}", f"{d:%m} - {d:%B}"),
    "Year / Month / Day": lambda d: os.path.join(f"{d:%Y}", f"{d:%m} - {d:%B}", f"{d:%Y-%m-%d}"),
    "Year": lambda d: f"{d:%Y}",
    "Year-Month-Day": lambda d: f"{d:%Y-%m-%d}",
    "No subfolders": lambda d: "",
}
