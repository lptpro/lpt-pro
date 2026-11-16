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
    def _payload(self, page: bytes, off: int) -> bytes:
        size, i = _varint(page, off)
        _rowid, i = _varint(page, i)
        u = self.usable
        x = u - 35
        if size <= x:
            return page[i:i + size]
        m = ((u - 12) * 32 // 255) - 23
        k = m + (size - m) % (u - 4)
        local = k if k <= x else m
        out = bytearray(page[i:i + local])
        nxt = struct.unpack(">I", page[i + local:i + local + 4])[0]
        while nxt and len(out) < size:
            ov = self.page(nxt)
            nxt = struct.unpack(">I", ov[:4])[0]
            out += ov[4:u]
        return bytes(out[:size])

    def rows(self, root: int):
        """Every record of the table whose b-tree starts at page `root`."""
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
                        yield _record(self._payload(pg, off))
            level = children

    def table(self, *names: str):
        """(column names, root page) of the first table found with one of these names."""
        wanted = [n.upper() for n in names]
        found = {}
        for rec in self.rows(1):                       # sqlite_master
            if len(rec) >= 5 and rec[0] == "table" and isinstance(rec[1], str) and rec[1].upper() in wanted:
                found[rec[1].upper()] = (_columns(rec[4]), rec[3])
        for n in wanted:
            if n in found:
                return found[n]
        raise Unavailable("the iPhone's Photos library has an unknown layout")


# ---------------------------------------------------------------------------
def recently_deleted(dev) -> set[str]:
    """Upper-case phone paths ("/DCIM/100APPLE/IMG_0001.HEIC") of items in Recently Deleted.

    Raises Unavailable if the list can't be read; callers then treat nothing as deleted."""
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
        i_state = cols.index("ZTRASHEDSTATE") if "ZTRASHEDSTATE" in cols else None
        i_date = cols.index("ZTRASHEDDATE") if "ZTRASHEDDATE" in cols else None
        if i_state is None and i_date is None:
            raise Unavailable("the iPhone's Photos library has an unknown layout")
        out, total = set(), 0
        for rec in r.rows(root):
            total += 1
            get = lambda i: rec[i] if i is not None and i < len(rec) else None
            trashed = get(i_state) == 1 if i_state is not None else get(i_date) is not None
            d, n = get(i_dir), get(i_name)
            if trashed and isinstance(d, str) and isinstance(n, str):
                out.add(("/" + d.strip("/") + "/" + n).upper())
        log.info("Recently Deleted: %d of %d library items (read %.1f MB in %d requests)",
                 len(out), total, r.bytes_read / 1e6, r.requests)
        return out
    except Unavailable:
        raise
    except Exception as e:
        log.warning("Recently Deleted list unreadable", exc_info=True)
        raise Unavailable(f"the iPhone's Photos library couldn't be read ({type(e).__name__})")
    finally:
        r.close()
