# SPDX-License-Identifier: GPL-3.0-or-later
"""Video tiles: grab one frame (and the length) from each video, straight off the phone.

A PhoneFile is a read-only, seekable QIODevice backed by the phone, so Qt's video
engine (FFmpeg, signed by The Qt Company) reads only the few megabytes it needs for
the first frames instead of the whole video. One video at a time, newest request
first, paused while the preview window is playing something.
"""
from __future__ import annotations

import collections
import logging
import os

from PySide6.QtCore import QBuffer, QIODevice, QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QImage, QTransform

from .media import MediaItem

log = logging.getLogger("transfer")

try:
    from PySide6.QtMultimedia import QMediaPlayer, QVideoFrame, QVideoSink
    HAVE_VIDEO = True
except Exception:
    HAVE_VIDEO = False

GRAB_AFTER_US = 0         # first frame: reads the least from the phone
TIMEOUT_MS = 12_000


class PhoneFile(QIODevice):
    """Read-only random access to a file on the phone (or demo folder)."""

    def __init__(self, device, path: str, size: int, parent=None):
        super().__init__(parent)
        self.device, self.path, self._size = device, path, size
        self._h = None

    def open(self, mode=QIODevice.ReadOnly) -> bool:
        try:
            self._h = self.device.open(self.path)
        except Exception:
            log.debug("could not open %s", self.path, exc_info=True)
            return False
        return super().open(QIODevice.ReadOnly)

    def isSequential(self) -> bool:
        return False

    def size(self) -> int:
        return self._size

    def bytesAvailable(self) -> int:
        return max(0, self._size - self.pos()) + super().bytesAvailable()

    def readData(self, maxlen: int):
        n = min(maxlen, self._size - self.pos())
        if n <= 0 or self._h is None:
            return b""
        try:
            return self.device.pread(self._h, n, self.pos())
        except Exception:
            log.debug("read failed on %s", self.path, exc_info=True)
            return None  # -> error for the reader

    def writeData(self, data) -> int:
        return -1  # never writes

    def close(self):
        if self._h is not None:
            self.device.close_file(self._h)
            self._h = None
        super().close()


def frame_to_image(frame) -> QImage:
    """QVideoFrame -> upright QImage (applies the video's rotation / mirroring)."""
    img = frame.toImage()
    if img.isNull():
        return img
    try:
        angle = int(frame.rotation().value) if hasattr(frame.rotation(), "value") else int(frame.rotation())
    except Exception:
        angle = 0
    stored_landscape = frame.width() >= frame.height()
    already = (img.width() >= img.height()) != stored_landscape  # toImage() rotated it for us
    if angle in (90, 180, 270) and not already and not (angle == 180 and _qt_rotates_180()):
        img = img.transformed(QTransform().rotate(angle))
    try:
        if frame.mirrored() and not already:
            img = img.mirrored(True, False)
    except Exception:
        pass
    return img


def _qt_rotates_180() -> bool:
    # From Qt 6.8 QVideoFrame::toImage() applies the frame's own transformation.
    from PySide6 import QtCore
    major, minor = (int(x) for x in QtCore.qVersion().split(".")[:2])
    return (major, minor) >= (6, 8)


class _Slot:
    """One video being turned into a tile: its own silent player, sink and timer."""

    def __init__(self, owner):
        self.cur: MediaItem | None = None
        self.dev: PhoneFile | None = None
        self.frames = 0
        self.wanted = False
        self.timer = QTimer(owner, singleShot=True, interval=TIMEOUT_MS)
        self.timer.timeout.connect(lambda: owner._finish(self, None))
        self.player = QMediaPlayer(owner)        # no audio output -> silent
        self.sink = QVideoSink(owner)
        self.player.setVideoSink(self.sink)
        self.sink.videoFrameChanged.connect(lambda f: owner._on_frame(self, f), Qt.QueuedConnection)
        self.player.errorOccurred.connect(lambda *_: owner._finish(self, None))


class VideoThumbnailer(QObject):
    """Makes video tiles, SLOTS videos at a time. On-screen requests come first (newest
    first); the rest of the camera roll is prepared in the background ("prefetch")."""

    ready = Signal(str, bytes, int)   # phone path, JPEG bytes (b"" if none), duration ms
    prefetch_progress = Signal(int, int)   # done, total
    SLOTS = 3

    def __init__(self, device, cache_path, parent=None):
        super().__init__(parent)
        self.device, self.cache_path = device, cache_path   # cache_path(item) -> .jpg path
        self._stack: list[MediaItem] = []
        self._queued: set[str] = set()
        self._paused = False
        self._pf: collections.deque = collections.deque()
        self._pf_all: set = set()
        self._pf_done: set = set()
        self._pf_paused = False
        self.slots = [_Slot(self) for _ in range(self.SLOTS)] if HAVE_VIDEO else []
        self.player = self.slots[0].player if self.slots else None

    def _busy_with(self, path):
        return next((sl for sl in self.slots if sl.cur is not None and sl.cur.path == path), None)

    # -- queue ------------------------------------------------------------------
    def request(self, item: MediaItem):
        sl = self._busy_with(item.path)
        if sl is not None:
            sl.wanted = True      # being prepared in the background right now: show it when done
            return
        if item.path in self._queued:
            self._stack = [x for x in self._stack if x.path != item.path]
        self._queued.add(item.path)
        self._stack.append(item)          # newest (on screen now) first
        if len(self._stack) > 300:
            for old in self._stack[:-300]:
                self._queued.discard(old.path)
            self._stack = self._stack[-300:]
        QTimer.singleShot(0, self._next)

    def prefetch(self, items):
        """Prepare tiles for all these videos in the background (newest first)."""
        self._pf = collections.deque(i for i in items if i.kind == "video")
        self._pf_all = {i.path for i in self._pf}
        self._pf_done = set()
        self._pf_paused = False
        self.prefetch_progress.emit(0, len(self._pf_all))
        QTimer.singleShot(0, self._next)

    def add_prefetch(self, item):
        """One more video to prepare in the background (no iPhone thumbnail for it)."""
        if item.path in self._pf_all:
            return
        self._pf.append(item)
        self._pf_all.add(item.path)
        self.prefetch_progress.emit(len(self._pf_done), len(self._pf_all))
        QTimer.singleShot(0, self._next)

    def pause_prefetch(self, on: bool):
        self._pf_paused = on
        if not on:
            QTimer.singleShot(0, self._next)

    def _mark_done(self, item, wanted=False):
        if item.path in self._pf_all and item.path not in self._pf_done:
            self._pf_done.add(item.path)
            n, total = len(self._pf_done), len(self._pf_all)
            if n % 5 == 0 or n == total or wanted:
                self.prefetch_progress.emit(n, total)

    def pause(self, on: bool):
        self._paused = on
        if on:
            # give the CPU to the preview player; retry these later
            for sl in self.slots:
                if sl.cur is not None:
                    item, wanted = sl.cur, sl.wanted
                    self._cleanup(sl)
                    if wanted:
                        self._queued.add(item.path)
                        self._stack.append(item)
                    else:
                        self._pf.appendleft(item)
        else:
            QTimer.singleShot(0, self._next)

    def stop(self):
        self._stack.clear()
        self._queued.clear()
        self._pf.clear()
        for sl in self.slots:
            self._cleanup(sl)

    def _next(self):
        while not self._paused:
            free = next((sl for sl in self.slots if sl.cur is None), None)
            if free is None or not self._start_one(free):
                return

    def _start_one(self, sl) -> bool:
        """Start the next video in this slot. False = nothing more to do right now."""
        if self._stack:
            item, wanted = self._stack.pop(), True
            self._queued.discard(item.path)
            cp = self.cache_path(item)
            if os.path.exists(cp):   # prepared in the background meanwhile
                try:
                    with open(cp, "rb") as f:
                        cached = f.read()
                    if cached:
                        self.ready.emit(item.path, cached, cached_duration(cp))
                        return True
                except OSError:
                    pass
            if self._busy_with(item.path):
                self._busy_with(item.path).wanted = True
                return True
        else:
            # background: skip ones already prepared (a few per pass)
            item = None
            for _ in range(50):
                if self._pf_paused or not self._pf:
                    return False
                cand = self._pf.popleft()
                if cand.path in self._pf_done or os.path.exists(self.cache_path(cand)) \
                        or self._busy_with(cand.path):
                    self._mark_done(cand)
                    continue
                item = cand
                break
            if item is None:
                QTimer.singleShot(0, self._next)
                return False
            wanted = False
        if not HAVE_VIDEO or not self.device.connected:
            if wanted:
                self.ready.emit(item.path, b"", 0)
                return True
            self._pf.appendleft(item)   # try again when the phone is back
            self._pf_paused = True
            return False
        sl.cur, sl.frames, sl.wanted = item, 0, wanted
        sl.dev = PhoneFile(self.device, item.path, item.size, self)
        if not sl.dev.open():
            self._finish(sl, None)
            return True
        sl.player.setSourceDevice(sl.dev, QUrl.fromLocalFile("/" + item.name))
        sl.player.play()
        sl.timer.start()
        return True

    def _on_frame(self, sl, frame):
        if sl.cur is None or not frame.isValid():
            return
        sl.frames += 1
        if frame.startTime() >= GRAB_AFTER_US or sl.frames >= 20:
            self._finish(sl, frame_to_image(frame))

    def _finish(self, sl, img: QImage | None):
        item = sl.cur
        if item is None:
            return
        wanted = sl.wanted
        duration = sl.player.duration()
        self._cleanup(sl)
        data = b""
        if img is not None and not img.isNull():
            img = img.scaled(320, 320, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            buf = QBuffer()
            buf.open(QIODevice.WriteOnly)
            img.save(buf, "JPG", 82)
            data = bytes(buf.data())
            try:
                from .jobs import write_atomic
                cp = self.cache_path(item)
                write_atomic(cp + ".dur", str(duration).encode())   # length first, then the picture
                write_atomic(cp, data)
            except OSError:
                pass
        else:
            log.info("no video frame for %s", item.path)
        if wanted:
            self.ready.emit(item.path, data, duration)
        self._mark_done(item, wanted)
        QTimer.singleShot(0, self._next)

    def _cleanup(self, sl):
        sl.timer.stop()
        sl.cur = None
        sl.player.stop()
        sl.player.setSource(QUrl())
        if sl.dev is not None:
            sl.dev.close()
            sl.dev.deleteLater()
            sl.dev = None


def cached_duration(cache_file: str) -> int:
    try:
        with open(cache_file + ".dur") as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0
