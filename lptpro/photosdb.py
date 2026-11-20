# SPDX-License-Identifier: GPL-3.0-or-later
"""Which photos are in the iPhone's "Recently Deleted" album.

A deleted photo stays in the camera roll (DCIM) for about 30 days; only the iPhone's
Photos library database (/PhotoData/Photos.sqlite) knows it was deleted. That file can be
over a gigabyte, so it is not copied. This module reads just the parts it needs, read-only,
straight from the phone:

- a small pure-Python reader for the SQLite file format (tables only),
- plus the database's write-ahead log (-wal), where recent changes such as a deletion
  a minute ago live until the phone folds them into the main file.

Pages are fetched in a few large requests (neighbouring pages together), never one small
request per page, so the phone's file service isn't flooded.
"""
from __future__ import annotations

import datetime as dt
import logging
import struct

log = logging.getLogger("transfer")

DB_PATH = "/PhotoData/Photos.sqlite"
READ_CHUNK = 1024 * 1024          # bytes per request to the phone
MAX_WAL = 128 * 1024 * 1024       # larger logs are not read (the list is then unavailable)
MAX_TOTAL = 400 * 1024 * 1024     # give up rather than read most of a huge library
GAP_PAGES = 16                    # neighbouring pages closer than this are fetched together


class Unavailable(Exception):
    """The list can't be read; nothing is treated as deleted."""


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------
def _varint(b: bytes, i: int) -> tuple[int, int]:
    v = 0
    for k in range(8):
        c = b[i + k]
        v = (v << 7) | (c & 0x7F)
        if c < 0x80:
            return v, i + k + 1
    return (v << 8) | b[i + 8], i + 9


def _record(payload: bytes) -> list:
    hdr_len, i = _varint(payload, 0)
    types = []
    while i < hdr_len:
        t, i = _varint(payload, i)
        types.append(t)
    out, p = [], hdr_len
    for t in types:
        if t == 0:
            out.append(None)
        elif 1 <= t <= 6:
            n = (1, 2, 3, 4, 6, 8)[t - 1]
            out.append(int.from_bytes(payload[p:p + n], "big", signed=True))
            p += n
        elif t == 7:
            out.append(struct.unpack(">d", payload[p:p + 8])[0])
            p += 8
        elif t in (8, 9):
            out.append(t - 8)
        elif t >= 12:
            n = (t - 12) // 2
            raw = payload[p:p + n]
            out.append(raw.decode("utf-8", "replace") if t % 2 else raw)
            p += n
        else:
            out.append(None)
    return out


def _columns(sql: str) -> list[str]:
    """Column names, in order, from a CREATE TABLE statement."""
    body = sql[sql.index("(") + 1: sql.rindex(")")]
    parts, depth, cur = [], 0, ""
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    cols = []
    for part in parts:
        words = part.strip().split()
        if not words:
            continue
        if words[0].upper() in ("CONSTRAINT", "PRIMARY", "UNIQUE", "CHECK", "FOREIGN"):
            continue
        cols.append(words[0].strip('"`[]').upper())
    return cols


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------
class _Reader:
    def __init__(self, dev, path: str):
        self.dev = dev
        self.bytes_read = 0
        self.requests = 0
        self.h = dev.open(path)
        try:
            head = self._pread(self.h, 100, 0)
            if head[:16] != b"SQLite format 3\x00":
                raise Unavailable("not a database file")
            ps = struct.unpack(">H", head[16:18])[0]
            self.ps = 65536 if ps == 1 else ps
            self.usable = self.ps - head[20]
            self.wal = self._load_wal(path + "-wal")
        except Exception:
            self.close()
            raise
        self.cache: dict[int, bytes] = {}

    def close(self):
        if self.h is not None:
            try:
                self.dev.close_file(self.h)
            except Exception:
                pass
            self.h = None

    def _pread(self, h, size, offset) -> bytes:
        out = bytearray()
        while size > 0:
            n = min(size, READ_CHUNK)
            b = self.dev.pread(h, n, offset)
            self.requests += 1
            if not b:
                break
            out += b
            offset += len(b)
            size -= len(b)
            self.bytes_read += len(b)
            if self.bytes_read > MAX_TOTAL:
                raise Unavailable("the Photos library is too large to read quickly")
        return bytes(out)

    def _load_wal(self, path: str) -> dict:
        try:
            size = self.dev.stat(path).size
        except Exception:
            return {}
        if size < 32:
            return {}
        if size > MAX_WAL:
            raise Unavailable("the Photos library has too many unsaved changes right now")
        h = self.dev.open(path)
        try:
            data = self._pread(h, size, 0)
        finally:
            try:
                self.dev.close_file(h)
            except Exception:
                pass
        magic, _ver, ps, _seq, s1, s2 = struct.unpack(">IIIIII", data[:24])
        if magic not in (0x377F0682, 0x377F0683) or ps != self.ps:
            return {}
        frame = 24 + ps
        committed, pending = {}, {}
        off = 32
        while off + frame <= len(data):
            pgno, commit, f1, f2 = struct.unpack(">IIII", data[off:off + 16])
            if (f1, f2) != (s1, s2):
                break                       # left over from an older log
            pending[pgno] = off + 24
            if commit:
                committed.update(pending)
                pending = {}
            off += frame
        return {p: data[o:o + ps] for p, o in committed.items()}

    # -- pages ------------------------------------------------------------------
    def page(self, n: int) -> bytes:
        if n in self.wal:
            return self.wal[n]
        if n not in self.cache:
            self.prefetch([n])
        return self.cache[n]

    def prefetch(self, pages) -> None:
        need = sorted({p for p in pages if p not in self.wal and p not in self.cache})
        i = 0
        while i < len(need):
            start = end = need[i]
            i += 1
            while i < len(need) and need[i] - end <= GAP_PAGES and (need[i] - start + 1) * self.ps <= 8 * READ_CHUNK:
                end = need[i]
                i += 1
            blob = self._pread(self.h, (end - start + 1) * self.ps, (start - 1) * self.ps)
            for p in range(start, end + 1):
                chunk = blob[(p - start) * self.ps:(p - start + 1) * self.ps]
                if len(chunk) == self.ps:
                    self.cache[p] = chunk

    # -- tables -------------------------------------------------------------------
    def _payload(self, page: bytes, off: int) -> tuple[int, bytes]:
        size, i = _varint(page, off)
        rowid, i = _varint(page, i)
        u = self.usable
        x = u - 35
        if size <= x:
            return rowid, page[i:i + size]
        m = ((u - 12) * 32 // 255) - 23
        k = m + (size - m) % (u - 4)
        local = k if k <= x else m
        out = bytearray(page[i:i + local])
        nxt = struct.unpack(">I", page[i + local:i + local + 4])[0]
        while nxt and len(out) < size:
            ov = self.page(nxt)
            nxt = struct.unpack(">I", ov[:4])[0]
            out += ov[4:u]
        return rowid, bytes(out[:size])

    def rows(self, root: int, with_rowid: bool = False):
        """Every record of the table whose b-tree starts at page `root` (with its row id)."""
        level = [root]
        while level:
            self.prefetch(level)
            children, leaves = [], []
            for n in level:
                pg = self.page(n)
                h = 100 if n == 1 else 0
                kind = pg[h]
                cells = struct.unpack(">H", pg[h + 3:h + 5])[0]
                if kind == 0x05:                       # interior table page
                    ptrs = h + 12
                    for c in range(cells):
                        off = struct.unpack(">H", pg[ptrs + 2 * c:ptrs + 2 * c + 2])[0]
                        children.append(struct.unpack(">I", pg[off:off + 4])[0])
                    children.append(struct.unpack(">I", pg[h + 8:h + 12])[0])
                elif kind == 0x0D:                     # leaf table page
                    leaves.append(n)
                else:
                    raise Unavailable("unexpected database layout")
            if leaves:
                self.prefetch(leaves)
                for n in leaves:
                    pg = self.page(n)
                    h = 100 if n == 1 else 0
                    cells = struct.unpack(">H", pg[h + 3:h + 5])[0]
                    for c in range(cells):
                        off = struct.unpack(">H", pg[h + 8 + 2 * c:h + 10 + 2 * c])[0]
                        rowid, payload = self._payload(pg, off)
                        yield (rowid, _record(payload)) if with_rowid else _record(payload)
            level = children

    def table(self, *names: str, required: bool = True):
        """(column names, root page) of the first table found with one of these names."""
        wanted = [n.upper() for n in names]
        found = {}
        for rec in self.rows(1):                       # sqlite_master
            if len(rec) >= 5 and rec[0] == "table" and isinstance(rec[1], str) and rec[1].upper() in wanted:
                found[rec[1].upper()] = (_columns(rec[4]), rec[3])
        for n in wanted:
            if n in found:
                return found[n]
        if not required:
            return None
        raise Unavailable("the iPhone's Photos library has an unknown layout")


# ---------------------------------------------------------------------------
APPLE_EPOCH = 978307200          # 2001-01-01 in Unix time (the library's clock)


class Asset:
    __slots__ = ("path", "phone_path", "directory", "name", "kind", "trashed", "hidden", "date",
                 "duration_ms", "original_name", "original_size")

    def __init__(self, directory, name, kind, trashed, hidden, created=None, duration=None):
        self.directory, self.name = directory, name
        self.phone_path = "/" + directory.strip("/") + "/" + name
        self.path = self.phone_path.upper()
        self.kind, self.trashed, self.hidden = kind, trashed, hidden
        self.date = None
        if isinstance(created, (int, float)):
            try:
                self.date = dt.datetime.fromtimestamp(created + APPLE_EPOCH)
            except (OverflowError, OSError, ValueError):
                pass
        self.duration_ms = int(duration * 1000) if isinstance(duration, (int, float)) and duration > 0 else 0
        self.original_name = None      # e.g. IMG_1234.HEIC (the library's own file name can be an ID)
        self.original_size = 0


class Library:
    """What the iPhone's Photos library lists (including items whose originals are only in iCloud)."""

    def __init__(self, assets: list[Asset]):
        self.assets = assets
        self.trashed = {a.path for a in assets if a.trashed}

    def not_in(self, paths_on_phone: set[str]) -> list[Asset]:
        """Library items (not deleted) that aren't among these camera-roll files."""
        return [a for a in self.assets if not a.trashed and a.path not in paths_on_phone
                and not _not_library(a.directory)]


def _not_library(directory: str) -> bool:
    """Items listed in the Photos library that aren't part of it: Shared Albums, and
    "Shared with You" photos from Messages (kept in one-character folders like "5" or "D")."""
    d = directory.strip("/")
    return "CLOUDSHARING" in d.upper() or ("/" not in d and len(d) <= 2)


def split_on_phone(dev, assets: list["Asset"]) -> tuple[list[tuple["Asset", object]], list["Asset"]]:
    """Library items outside the camera roll whose full original IS stored on the iPhone (these
    can be backed up like any other) versus those that are only in iCloud."""
    on_phone, cloud = [], []
    stats = dev.stat_many([a.phone_path for a in assets]) if assets else []
    for a, st in zip(assets, stats):
        if st is not None and not st.is_dir and st.size > 0 and (st.blocks is None or st.blocks > 0):
            on_phone.append((a, st))
        else:
            cloud.append(a)
    return on_phone, cloud


def read_library(dev) -> Library:
    """Read the iPhone's Photos library list. Raises Unavailable if it can't be read."""
    try:
        dev.stat(DB_PATH)
    except Exception:
        raise Unavailable("the iPhone's Photos library isn't readable")
    try:
        r = _Reader(dev, DB_PATH)
    except Unavailable:
        raise
    except Exception as e:
        raise Unavailable(f"the iPhone's Photos library couldn't be opened ({type(e).__name__})")
    try:
        cols, root = r.table("ZASSET", "ZGENERICASSET")
        try:
            i_dir, i_name = cols.index("ZDIRECTORY"), cols.index("ZFILENAME")
        except ValueError:
            raise Unavailable("the iPhone's Photos library has an unknown layout")
        idx = lambda c: cols.index(c) if c in cols else None
        i_state, i_tdate, i_kind, i_hidden = idx("ZTRASHEDSTATE"), idx("ZTRASHEDDATE"), idx("ZKIND"), idx("ZHIDDEN")
        i_created, i_dur, i_added = idx("ZDATECREATED"), idx("ZDURATION"), idx("ZADDEDDATE")
        if i_state is None and i_tdate is None:
            raise Unavailable("the iPhone's Photos library has an unknown layout")
        assets, total, by_pk = [], 0, {}
        for pk, rec in r.rows(root, with_rowid=True):
            total += 1
            get = lambda i: rec[i] if i is not None and i < len(rec) else None
            d, n = get(i_dir), get(i_name)
            if not (isinstance(d, str) and isinstance(n, str)):
                continue
            trashed = get(i_state) == 1 if i_state is not None else get(i_tdate) is not None
            kind = {0: "photo", 1: "video"}.get(get(i_kind))
            created = get(i_created)
            if not isinstance(created, (int, float)):
                created = get(i_added)            # no "date taken": when it was added to the library
            a = Asset(d, n, kind, trashed, get(i_hidden) == 1, created, get(i_dur))
            assets.append(a)
            by_pk[pk] = a
        # the original file name and size (used to recognise copies downloaded from iCloud)
        extra = r.table("ZADDITIONALASSETATTRIBUTES", required=False)
        if extra:
            ecols, eroot = extra
            j = lambda c: ecols.index(c) if c in ecols else None
            j_asset, j_name, j_size = j("ZASSET"), j("ZORIGINALFILENAME"), j("ZORIGINALFILESIZE")
            if j_asset is not None and j_name is not None:
                for rec in r.rows(eroot):
                    get = lambda i: rec[i] if i is not None and i < len(rec) else None
                    a = by_pk.get(get(j_asset))
                    if a is not None and isinstance(get(j_name), str):
                        a.original_name = get(j_name)
                        if isinstance(get(j_size), int):
                            a.original_size = get(j_size)
        lib = Library(assets)
        log.info("Photos library: %d items (%d with a file name), %d in Recently Deleted "
                 "(read %.1f MB in %d requests)", total, len(assets), len(lib.trashed),
                 r.bytes_read / 1e6, r.requests)
        return lib
    except Unavailable:
        raise
    except Exception as e:
        log.warning("Photos library unreadable", exc_info=True)
        raise Unavailable(f"the iPhone's Photos library couldn't be read ({type(e).__name__})")
    finally:
        r.close()


def preview_candidates(path: str, kind: str = "photo", live_photo: str | None = None) -> list[str]:
    """Where the iPhone may keep its own small preview of an item (most likely first).

    Camera-roll items:  /PhotoData/Thumbnails/V2/DCIM/100APPLE/IMG_0001.HEIC/5005.JPG
    iCloud items:       /PhotoData/Thumbnails/V2/PhotoData/CPLAssets/group1/<ID>.HEIC/5005.JPG
    """
    roots = [path]
    if not path.upper().startswith("/PHOTODATA/") and not path.upper().startswith("/DCIM/"):
        roots.append("/PhotoData" + path)        # library folders may be stored without the prefix
    out = [f"/PhotoData/Thumbnails/V2{r}/5005.JPG" for r in roots]
    if kind == "video":
        out += [f"/PhotoData/Thumbnails/VideoKeyFrames{r}/LocalVideoKeyFrame.jpg" for r in roots]
        if live_photo:
            out.append(f"/PhotoData/Thumbnails/V2{live_photo}/5005.JPG")
    return out


def preview_folders(path: str) -> list[str]:
    """Folders to look inside when the usual preview file name isn't there."""
    roots = [path]
    if not path.upper().startswith("/PHOTODATA/") and not path.upper().startswith("/DCIM/"):
        roots.append("/PhotoData" + path)
    return [f"/PhotoData/Thumbnails/V2{r}" for r in roots]


def read_preview(dev, path: str, kind: str = "photo", live_photo: str | None = None,
                 look_inside: bool = False) -> tuple[bytes | None, str | None]:
    """(JPEG bytes, where it was found) of the iPhone's own preview, or (None, None)."""
    for p in preview_candidates(path, kind, live_photo):
        try:
            data = dev.read_all(p)
        except Exception as e:
            if type(e).__name__ == "DeviceGone":
                raise
            continue
        if data:
            return data, p
    if look_inside:
        for folder in preview_folders(path):
            try:
                names = [n for n in dev.listdir(folder) if n.upper().endswith((".JPG", ".JPEG"))]
            except Exception as e:
                if type(e).__name__ == "DeviceGone":
                    raise
                continue
            for n in sorted(names, reverse=True):          # 5005 > 5003: the larger ones first
                try:
                    data = dev.read_all(f"{folder}/{n}")
                except Exception:
                    continue
                if data:
                    return data, f"{folder}/{n}"
    return None, None


def recently_deleted(dev) -> set[str]:
    """Upper-case phone paths ("/DCIM/100APPLE/IMG_0001.HEIC") of items in Recently Deleted."""
    return read_library(dev).trashed


def where_they_live(dev, assets: list[Asset], sample: int = 40) -> list[str]:
    """Read-only look at library items outside the camera roll: grouped by folder, and for a
    few of each, whether the original file is on the phone (and really uses storage)."""
    groups: dict[str, list[Asset]] = {}
    for a in assets:
        top = "/".join(a.directory.strip("/").split("/")[:2]) or "(none)"
        groups.setdefault(top, []).append(a)
    lines = []
    for top, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        some = items[:: max(1, len(items) // sample)][:sample]
        stats = dev.stat_many(["/" + a.directory.strip("/") + "/" + a.name for a in some])
        found = [s for s in stats if s is not None]
        real = [s for s in found if (s.blocks is None or s.blocks > 0) and s.size > 0]
        kinds = {k: sum(1 for a in items if a.kind == k) for k in ("photo", "video")}
        where, n_prev = {}, 0
        for a in some[:10]:
            data, at = read_preview(dev, a.phone_path, a.kind or "photo", look_inside=True)
            if data:
                n_prev += 1
                key = at.replace(a.phone_path, "{item}") if a.phone_path in at else at.rsplit("/", 2)[0] + "/…"
                where[key] = where.get(key, 0) + 1
        example = f"; e.g. {some[0].directory}/{some[0].name[:8]}…" if some else ""
        lines.append(f"{top}: {len(items):,} items ({kinds['photo']:,} photos, {kinds['video']:,} videos); "
                     f"of {len(some)} checked, {len(found)} have a file on the iPhone, {len(real)} with the "
                     f"full original stored; previews found for {n_prev} of {min(10, len(some))}"
                     + (f" at {', '.join(where)}" if where else "") + example)
    for ln in lines:
        log.info("library outside the camera roll: %s", ln)
    return lines
