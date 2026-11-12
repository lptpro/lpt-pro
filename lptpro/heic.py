# SPDX-License-Identifier: GPL-3.0-or-later
"""HEIC/HEIF decoding without any third-party DLL on Windows.

Windows: uses Windows' own image decoder (WIC) through ctypes. WIC is part of
Windows and its HEIF support comes from Microsoft's "HEIF Image Extensions" +
"HEVC Video Extensions" (or another installed HEIC codec).
Everything it loads is signed, so Smart App Control has nothing to block.

Linux: uses pillow-heif (there is no Smart App Control on Linux).

Also here: a small pure-Python HEIF container reader for the things a decoder
may not hand back: rotation/mirroring, the colour profile (iPhones shoot in
Display P3) and the raw EXIF block, so a converted JPEG keeps all of it.
"""
from __future__ import annotations

import io
import logging
import os
import struct
import sys
import threading

from .media import _find_atom, _iter_atoms

log = logging.getLogger("transfer")

HEIF_EXT = {".heic", ".heif", ".hif"}


class HeicUnavailable(Exception):
    """No HEIC decoder on this PC (message is user-presentable)."""


# ===========================================================================
# Container reading (pure Python)
# ===========================================================================
class HeifInfo:
    def __init__(self):
        self.width = self.height = 0       # stored size (before rotation)
        self.transforms: list[tuple] = []  # ("rot", degrees_ccw) / ("mirror", axis), in order
        self.icc: bytes | None = None
        self.exif: bytes | None = None     # TIFF bytes (starting "II*\0" / "MM\0*")


def read_info(f) -> HeifInfo:
    info = HeifInfo()
    f.seek(0, os.SEEK_END)
    size = f.tell()
    meta = _find_atom(f, 0, size, b"meta")
    if not meta:
        return info
    ms, me = meta[0] + 4, meta[1]

    primary = None
    pitm = _find_atom(f, ms, me, b"pitm")
    if pitm:
        f.seek(pitm[0])
        ver = f.read(1)[0]
        f.read(3)
        primary = int.from_bytes(f.read(2 if ver == 0 else 4), "big")

    # -- item properties of the primary image
    iprp = _find_atom(f, ms, me, b"iprp")
    if iprp and primary is not None:
        ipco = _find_atom(f, iprp[0], iprp[1], b"ipco")
        props = list(_iter_atoms(f, ipco[0], ipco[1])) if ipco else []
        wanted: list[int] = []
        for typ, s, e in _iter_atoms(f, iprp[0], iprp[1]):
            if typ != b"ipma":
                continue
            f.seek(s)
            ver = f.read(1)[0]
            flags = int.from_bytes(f.read(3), "big")
            for _ in range(struct.unpack(">I", f.read(4))[0]):
                item = int.from_bytes(f.read(2 if ver < 1 else 4), "big")
                idx = []
                for _ in range(f.read(1)[0]):
                    v = int.from_bytes(f.read(2), "big") & 0x7FFF if flags & 1 else f.read(1)[0] & 0x7F
                    idx.append(v)
                if item == primary:
                    wanted = idx
        for i in wanted:
            if not 1 <= i <= len(props):
                continue
            typ, s, e = props[i - 1]
            f.seek(s)
            if typ == b"ispe":
                f.read(4)
                info.width, info.height = struct.unpack(">II", f.read(8))
            elif typ == b"irot":
                info.transforms.append(("rot", (f.read(1)[0] & 3) * 90))
            elif typ == b"imir":
                info.transforms.append(("mirror", f.read(1)[0] & 1))
            elif typ == b"colr":
                kind = f.read(4)
                if kind in (b"prof", b"rICC"):
                    info.icc = f.read(e - s - 4)

    # -- EXIF item (find it in iinf, its bytes via iloc)
    try:
        info.exif = _exif_item(f, ms, me)
    except Exception:
        log.debug("no exif in heif", exc_info=True)
    return info


def _exif_item(f, ms, me) -> bytes | None:
    iinf = _find_atom(f, ms, me, b"iinf")
    iloc = _find_atom(f, ms, me, b"iloc")
    if not iinf or not iloc:
        return None
    f.seek(iinf[0])
    ver = f.read(1)[0]
    f.read(3)
    f.read(2 if ver == 0 else 4)
    exif_id = None
    for typ, s, e in _iter_atoms(f, f.tell(), iinf[1]):
        if typ != b"infe":
            continue
        f.seek(s)
        iver = f.read(1)[0]
        f.read(3)
        if iver < 2:
            continue
        item_id = int.from_bytes(f.read(2 if iver == 2 else 4), "big")
        f.read(2)
        if f.read(4) == b"Exif":
            exif_id = item_id
            break
    if exif_id is None:
        return None

    f.seek(iloc[0])
    ver = f.read(1)[0]
    f.read(3)
    a, b = f.read(2)
    off_sz, len_sz, base_sz = a >> 4, a & 15, b >> 4
    idx_sz = (b & 15) if ver in (1, 2) else 0

    def rd(n):
        return int.from_bytes(f.read(n), "big") if n else 0

    for _ in range(rd(2 if ver < 2 else 4)):
        item_id = rd(2 if ver < 2 else 4)
        method = (rd(2) & 15) if ver in (1, 2) else 0
        rd(2)
        base = rd(base_sz)
        extents = []
        for _ in range(rd(2)):
            rd(idx_sz)
            extents.append((base + rd(off_sz), rd(len_sz)))
        if item_id == exif_id:
            if method != 0:
                return None
            here = f.tell()
            data = b""
            for off, ln in extents:
                f.seek(off)
                data += f.read(ln)
            f.seek(here)
            if len(data) < 4:
                return None
            skip = struct.unpack(">I", data[:4])[0]
            return data[4 + skip:]
    return None


# ===========================================================================
# Fast tile previews: read only the small preview image stored INSIDE the file
# ===========================================================================
class PhoneReader:
    """Read-only, file-like access to a phone file that fetches only the bytes asked for
    (in 64 KB blocks), so a few KB of a 4 MB photo can be read without the rest."""

    BLOCK = 64 * 1024

    def __init__(self, device, path: str, size: int, block: int | None = None):
        self.device, self.size = device, size
        if block:
            self.BLOCK = block
        self.h = device.open(path)
        self.pos = 0
        self.blocks: dict[int, bytes] = {}
        self.bytes_read = 0

    def close(self):
        self.device.close_file(self.h)

    def seek(self, pos, whence=0):
        self.pos = pos if whence == 0 else (self.pos + pos if whence == 1 else self.size + pos)
        return self.pos

    def tell(self):
        return self.pos

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.size - self.pos
        n = max(0, min(n, self.size - self.pos))
        out = bytearray()
        while n > 0:
            b = self.pos // self.BLOCK
            if b not in self.blocks:
                # fetch this block and, if more is needed, the following ones in one go
                count = max(1, -(-(self.pos % self.BLOCK + n) // self.BLOCK))
                data = self.device.pread(self.h, count * self.BLOCK, b * self.BLOCK)
                self.bytes_read += len(data)
                for k in range(count):
                    self.blocks[b + k] = data[k * self.BLOCK:(k + 1) * self.BLOCK]
            blk = self.blocks[b]
            off = self.pos % self.BLOCK
            chunk = blk[off:off + n]
            if not chunk:
                break
            out += chunk
            self.pos += len(chunk)
            n -= len(chunk)
        return bytes(out)


def _box(t: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + t + payload


def _fullbox(t: bytes, version: int, payload: bytes) -> bytes:
    return _box(t, bytes([version, 0, 0, 0]) + payload)


def embedded_thumbnail(f):
    """Find the primary image's built-in thumbnail. Returns (kind, data, info) where kind is
    "heif" (a tiny stand-alone HEIC holding just the thumbnail) or "jpeg", or None."""
    f.seek(0, os.SEEK_END)
    size = f.tell()
    meta = _find_atom(f, 0, size, b"meta")
    if not meta:
        return None
    ms, me = meta[0] + 4, meta[1]
    info = read_info(f)
    pitm = _find_atom(f, ms, me, b"pitm")
    f.seek(pitm[0])
    v = f.read(1)[0]
    f.read(3)
    primary = int.from_bytes(f.read(2 if v == 0 else 4), "big")

    # thumbnails point at the primary image through iref/'thmb'
    thumbs = []
    iref = _find_atom(f, ms, me, b"iref")
    if iref:
        f.seek(iref[0])
        v = f.read(1)[0]
        f.read(3)
        idsz = 2 if v == 0 else 4
        for typ, s0, e0 in _iter_atoms(f, f.tell(), iref[1]):
            if typ != b"thmb":
                continue
            f.seek(s0)
            src = int.from_bytes(f.read(idsz), "big")
            for _ in range(int.from_bytes(f.read(2), "big")):
                if int.from_bytes(f.read(idsz), "big") == primary:
                    thumbs.append(src)
    if not thumbs:
        return None

    # item types
    types = {}
    iinf = _find_atom(f, ms, me, b"iinf")
    f.seek(iinf[0])
    v = f.read(1)[0]
    f.read(3)
    f.read(2 if v == 0 else 4)
    for typ, s0, e0 in _iter_atoms(f, f.tell(), iinf[1]):
        if typ != b"infe":
            continue
        f.seek(s0)
        iv = f.read(1)[0]
        f.read(3)
        if iv < 2:
            continue
        iid = int.from_bytes(f.read(2 if iv == 2 else 4), "big")
        f.read(2)
        types[iid] = f.read(4)

    # properties of each thumbnail
    iprp = _find_atom(f, ms, me, b"iprp")
    ipco = _find_atom(f, iprp[0], iprp[1], b"ipco")
    props = list(_iter_atoms(f, ipco[0], ipco[1]))
    assoc = {}
    for typ, s0, e0 in _iter_atoms(f, iprp[0], iprp[1]):
        if typ != b"ipma":
            continue
        f.seek(s0)
        v = f.read(1)[0]
        flags = int.from_bytes(f.read(3), "big")
        for _ in range(struct.unpack(">I", f.read(4))[0]):
            iid = int.from_bytes(f.read(2 if v < 1 else 4), "big")
            idx = []
            for _ in range(f.read(1)[0]):
                idx.append(int.from_bytes(f.read(2), "big") & 0x7FFF if flags & 1 else f.read(1)[0] & 0x7F)
            assoc[iid] = idx

    def raw_prop(i):
        typ, s0, e0 = props[i - 1]
        hdr = 8 if e0 - s0 + 8 < 2 ** 32 else 16
        f.seek(s0 - hdr)
        return typ, f.read(e0 - s0 + hdr)

    best = None
    for tid in thumbs:
        hvcc = ispe = None
        w = h = 0
        own = []
        for i in assoc.get(tid, []):
            if 1 <= i <= len(props):
                typ, raw = raw_prop(i)
                if typ == b"hvcC":
                    hvcc = raw
                elif typ == b"ispe":
                    ispe = raw
                    w, h = struct.unpack(">II", raw[12:20])
                elif typ == b"irot":
                    own.append(("rot", (raw[8] & 3) * 90))
                elif typ == b"imir":
                    own.append(("mirror", raw[8] & 1))
        if types.get(tid) in (b"hvc1", b"jpeg") and (best is None or w > best[3]):
            best = (tid, hvcc, ispe, w, h, own)
    if best is None:
        return None
    tid, hvcc, ispe, tw, th, own = best
    # Which rotation applies to the thumbnail: its own if it has one; otherwise the main
    # image's, as long as the thumbnail is stored the same way round as the main image.
    if own:
        info.thumb_transforms = own
    elif tw and th and info.width and info.height and (tw >= th) != (info.width >= info.height):
        info.thumb_transforms = []          # stored already turned
    else:
        info.thumb_transforms = info.transforms

    data = _item_data(f, ms, me, tid)
    if not data:
        return None
    if types[tid] == b"jpeg":
        return "jpeg", data, info
    if not hvcc or not ispe:
        return None
    # a minimal stand-alone HEIC containing only the thumbnail
    ftyp = _box(b"ftyp", b"heic" + b"\0\0\0\0" + b"mif1heic")
    hdlr = _fullbox(b"hdlr", 0, b"\0" * 4 + b"pict" + b"\0" * 12 + b"\0")
    pitm_b = _fullbox(b"pitm", 0, struct.pack(">H", 1))
    infe = _fullbox(b"infe", 2, struct.pack(">HH", 1, 0) + b"hvc1" + b"\0")
    iinf_b = _fullbox(b"iinf", 0, struct.pack(">H", 1) + infe)
    ipma = _fullbox(b"ipma", 0, struct.pack(">IHB", 1, 1, 2) + bytes([0x81, 0x02]))
    iprp_b = _box(b"iprp", _box(b"ipco", hvcc + ispe) + ipma)

    def iloc_b(offset):
        return _fullbox(b"iloc", 0, bytes([0x44, 0x00]) + struct.pack(">HHHHII", 1, 1, 0, 1, offset, len(data)))

    meta_len = len(_fullbox(b"meta", 0, hdlr + pitm_b + iloc_b(0) + iinf_b + iprp_b))
    offset = len(ftyp) + meta_len + 8
    meta_b = _fullbox(b"meta", 0, hdlr + pitm_b + iloc_b(offset) + iinf_b + iprp_b)
    return "heif", ftyp + meta_b + _box(b"mdat", data), info


def _item_data(f, ms, me, item) -> bytes | None:
    iloc = _find_atom(f, ms, me, b"iloc")
    if not iloc:
        return None
    f.seek(iloc[0])
    ver = f.read(1)[0]
    f.read(3)
    a, b = f.read(2)
    off_sz, len_sz, base_sz = a >> 4, a & 15, b >> 4
    idx_sz = (b & 15) if ver in (1, 2) else 0

    def rd(n):
        return int.from_bytes(f.read(n), "big") if n else 0

    for _ in range(rd(2 if ver < 2 else 4)):
        iid = rd(2 if ver < 2 else 4)
        method = (rd(2) & 15) if ver in (1, 2) else 0
        rd(2)
        base = rd(base_sz)
        extents = []
        for _ in range(rd(2)):
            rd(idx_sz)
            extents.append((base + rd(off_sz), rd(len_sz)))
        if iid == item:
            if method != 0:
                return None
            out = b""
            for off, ln in extents:
                f.seek(off)
                out += f.read(ln)
            return out
    return None


_mini_logged = False
_mini_ok = True   # switched off for the session if the decoder dislikes the mini files


def fast_thumbnail(device, path: str, size: int):
    """Upright PIL image from the photo's built-in thumbnail, reading ~2 % of the file.
    Returns (image, bytes_read) or (None, bytes_read) if there is no usable thumbnail."""
    global _mini_ok
    from PIL import Image

    r = PhoneReader(device, path, size)
    try:
        found = embedded_thumbnail(r)
        if not found:
            return None, r.bytes_read
        kind, data, info = found
        if kind == "jpeg":
            img = Image.open(io.BytesIO(data)).convert("RGB")
        else:
            if not _mini_ok:
                return None, r.bytes_read
            try:
                img, _ = decode(data)
            except HeicUnavailable:
                raise
            except Exception:
                log.info("built-in HEIC thumbnail could not be decoded; using full photos for tiles",
                         exc_info=True)
                _mini_ok = False
                return None, r.bytes_read
        # the thumbnail is stored like the main image: apply the main image's rotation
        img = _apply_transforms(img, getattr(info, "thumb_transforms", info.transforms))
        global _mini_logged
        if not _mini_logged:
            _mini_logged = True
            log.info("HEIC tile shortcut works: %s read %d KB of %d KB", path, r.bytes_read // 1024, size // 1024)
        return img, r.bytes_read
    finally:
        r.close()


def exif_with_orientation_1(tiff: bytes) -> bytes:
    """Same EXIF bytes, with Orientation set to 1 (pixels are already upright)."""
    b = bytearray(tiff)
    try:
        bo = "<" if b[:2] == b"II" else ">"
        ifd = struct.unpack(bo + "I", b[4:8])[0]
        n = struct.unpack(bo + "H", b[ifd:ifd + 2])[0]
        for k in range(n):
            e = ifd + 2 + 12 * k
            tag, typ = struct.unpack(bo + "HH", b[e:e + 4])
            if tag == 0x0112 and typ == 3:
                b[e + 8:e + 10] = struct.pack(bo + "H", 1)
    except Exception:
        pass
    return bytes(b)


# ===========================================================================
# Decoders
# ===========================================================================
def backend_name() -> str:
    return "Windows image decoder" if sys.platform == "win32" else "libheif"


def decode(data: bytes, max_side: int | None = None):
    """HEIC bytes -> upright PIL RGB image (plus its HeifInfo)."""
    from PIL import Image

    info = read_info(io.BytesIO(data))
    if sys.platform == "win32":
        img, applied = _wic_decode(data, info, max_side)
        if not applied:
            img = _apply_transforms(img, info.transforms)
    else:
        try:
            import pillow_heif
        except ImportError:
            raise HeicUnavailable("HEIC support is missing (pip install pillow-heif).")
        hf = pillow_heif.open_heif(data, convert_hdr_to_8bit=True)  # applies rotation itself
        img = hf.to_pillow().convert("RGB")
        if max_side:
            img.thumbnail((max_side, max_side), Image.LANCZOS)
    return img, info


def to_jpeg(data: bytes, quality: int = 92) -> bytes:
    """Full-size JPEG with the original EXIF (dates, GPS, camera) and colour profile."""
    img, info = decode(data)
    kw = {"quality": quality, "subsampling": 0 if quality >= 95 else 2}
    if info.icc:
        kw["icc_profile"] = info.icc
    elif img.info.get("icc_profile"):
        kw["icc_profile"] = img.info["icc_profile"]
    if info.exif:
        kw["exif"] = b"Exif\x00\x00" + exif_with_orientation_1(info.exif)
    buf = io.BytesIO()
    img.save(buf, "JPEG", **kw)
    return buf.getvalue()


def _apply_transforms(img, transforms):
    from PIL import Image

    for kind, v in transforms:
        if kind == "rot" and v:
            img = img.rotate(v, expand=True)  # HEIF irot is anti-clockwise, same as PIL
        elif kind == "mirror":
            img = img.transpose(Image.FLIP_LEFT_RIGHT if v == 0 else Image.FLIP_TOP_BOTTOM)
    return img


# --- Windows Imaging Component via ctypes ----------------------------------
_wic_applies: bool | None = None   # learned: does WIC already rotate HEIC pixels?
_tls = threading.local()


def _wic_decode(data: bytes, info: HeifInfo, max_side: int | None):
    import ctypes
    import uuid
    from ctypes import POINTER, byref, c_double, c_uint, c_ulong, c_void_p
    from PIL import Image

    global _wic_applies
    HRESULT = ctypes.HRESULT

    class GUID(ctypes.Structure):
        _fields_ = [("b", ctypes.c_ubyte * 16)]

    def guid(s):
        g = GUID()
        ctypes.memmove(g.b, uuid.UUID(s).bytes_le, 16)
        return g

    def method(obj, index, *argtypes):
        vtbl = ctypes.cast(obj, POINTER(POINTER(c_void_p))).contents
        proto = ctypes.WINFUNCTYPE(HRESULT, c_void_p, *argtypes)
        fn = proto(vtbl[index])
        return lambda *args: fn(obj, *args)

    def release(obj):
        if obj:
            vtbl = ctypes.cast(obj, POINTER(POINTER(c_void_p))).contents
            ctypes.WINFUNCTYPE(c_ulong, c_void_p)(vtbl[2])(obj)

    ole32 = ctypes.windll.ole32
    if not getattr(_tls, "com", False):
        ole32.CoInitializeEx.restype = ctypes.c_long
        ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED; fine if already initialised
        _tls.com = True

    held = []
    try:
        factory = c_void_p()
        ole32.CoCreateInstance.restype = HRESULT
        ole32.CoCreateInstance(byref(guid("cacaf262-9370-4615-a13b-9f5539da4c0a")), None, 1,
                               byref(guid("ec5ec8a9-c395-4314-9c77-54d7a935ff70")), byref(factory))
        held.append(factory)

        shlwapi = ctypes.windll.shlwapi
        shlwapi.SHCreateMemStream.restype = c_void_p
        shlwapi.SHCreateMemStream.argtypes = [ctypes.c_char_p, c_uint]
        stream = c_void_p(shlwapi.SHCreateMemStream(data, len(data)))
        if not stream:
            raise MemoryError("could not create memory stream")
        held.append(stream)

        decoder = c_void_p()
        try:
            # IWICImagingFactory::CreateDecoderFromStream(stream, vendor, options, out)
            method(factory, 4, c_void_p, c_void_p, c_uint, POINTER(c_void_p))(stream, None, 0, byref(decoder))
        except OSError as e:
            raise HeicUnavailable(
                "Windows can't read HEIC photos yet. Install “HEVC Video Extensions” "
                "(and “HEIF Image Extensions”) from the Microsoft Store, then restart the app."
            ) from e
        held.append(decoder)

        frame = c_void_p()
        method(decoder, 13, c_uint, POINTER(c_void_p))(0, byref(frame))   # GetFrame(0)
        held.append(frame)

        w, h = c_uint(), c_uint()
        method(frame, 3, POINTER(c_uint), POINTER(c_uint))(byref(w), byref(h))  # GetSize
        w, h = w.value, h.value

        # Did WIC already apply the HEIF rotation? Learn it from a 90/270 image.
        rot = sum(v for k, v in info.transforms if k == "rot") % 360
        applied = _wic_applies if _wic_applies is not None else True
        if rot in (90, 270) and info.width and info.height and info.width != info.height:
            applied = (w, h) == (info.height, info.width)
            _wic_applies = applied

        # Convert to plain 8-bit colour FIRST, then shrink. (Shrinking the decoder's raw
        # output first could blow out the highlights of HDR iPhone photos in the tiles.)
        conv = c_void_p()
        method(factory, 10, POINTER(c_void_p))(byref(conv))                  # CreateFormatConverter
        held.append(conv)
        bgr24 = guid("6fddc324-4e03-4bfe-b185-3d77768dc90c")
        method(conv, 8, c_void_p, POINTER(GUID), c_uint, c_void_p, c_double, c_uint)(
            frame, byref(bgr24), 0, None, 0.0, 0)                            # Initialize
        source = conv
        if max_side and max(w, h) > max_side:
            k = max_side / max(w, h)
            sw, sh = max(1, round(w * k)), max(1, round(h * k))
            scaler = c_void_p()
            method(factory, 11, POINTER(c_void_p))(byref(scaler))            # CreateBitmapScaler
            held.append(scaler)
            method(scaler, 8, c_void_p, c_uint, c_uint, c_uint)(conv, sw, sh, 3)  # Initialize, Fant
            source, w, h = scaler, sw, sh

        stride = w * 3
        buf = (ctypes.c_ubyte * (stride * h))()
        method(source, 7, c_void_p, c_uint, c_uint, POINTER(ctypes.c_ubyte))(None, stride, len(buf), buf)  # CopyPixels
        img = Image.frombuffer("RGB", (w, h), bytes(buf), "raw", "BGR", stride, 1)
        return img, applied
    finally:
        for o in reversed(held):
            release(o)
