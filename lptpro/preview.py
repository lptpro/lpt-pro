# SPDX-License-Identifier: GPL-3.0-or-later
"""Preview window: full-size photos (HEIC shown like any JPEG) and a built-in video player.

Opened by double-clicking a tile. Left/Right arrows step through the grid,
Space plays/pauses, F toggles full screen, Esc closes.

Everything slow (reading from the phone, decoding HEIC) happens on a worker
thread. Videos that are only on the phone are copied into the app's own preview
cache first (with progress), then played from there. Nothing on the phone and
nothing in the backup folder is ever changed by previewing.
"""
from __future__ import annotations

import hashlib
import logging
import os
import sys
import time

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QSize, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QImage, QImageReader, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QSlider,
                               QStackedWidget, QStyle, QStyleOptionSlider, QVBoxLayout, QWidget)

from . import heic, settings
from .device import DeviceGone
from .media import MediaItem

log = logging.getLogger("transfer")

try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PySide6.QtMultimediaWidgets import QVideoWidget
    HAVE_VIDEO = True
except Exception:  # PySide6-Addons missing
    HAVE_VIDEO = False

CACHE_LIMIT = 3 * 1024 ** 3   # keep at most ~3 GB of previewed videos


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024


def clock(ms: int) -> str:
    s = max(0, ms // 1000)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


# ===========================================================================
# Loader (worker thread)
# ===========================================================================
class PreviewLoader(QThread):
    image = Signal(int, QImage)
    video = Signal(int, str)
    progress = Signal(int, int)       # token, percent
    failed = Signal(int, str)

    def __init__(self, device, item: MediaItem, local: str | None, cache_dir: str, token: int):
        super().__init__()
        self.device, self.item, self.local, self.cache_dir, self.token = device, item, local, cache_dir, token
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        it = self.item
        try:
            if it.kind == "video":
                self.video.emit(self.token, self._video_file())
            else:
                self.image.emit(self.token, self._photo())
        except DeviceGone:
            self.failed.emit(self.token, "The iPhone is disconnected. Plug it back in to see this item.")
        except heic.HeicUnavailable as e:
            self.failed.emit(self.token, str(e))
        except _Cancelled:
            pass
        except Exception as e:
            from .device import is_not_on_phone
            if is_not_on_phone(e, it):
                self.failed.emit(self.token, "This one’s full-size original is only in iCloud "
                                             "(“Optimize iPhone Storage”), so the iPhone can’t hand it over.\n"
                                             "The tile shows the small preview the phone keeps.")
                return
            log.exception("preview failed for %s", it.path)
            from .ui import plain_reason
            self.failed.emit(self.token, f"Couldn’t show this item.\n\n{plain_reason(str(e))}\n\n"
                                         f"Details: {type(e).__name__}: {str(e)[:200]}")

    def _bytes(self) -> bytes:
        if self.local and os.path.isfile(self.local):
            with open(self.local, "rb") as f:
                return f.read()
        return self.device.read_all(self.item.path)

    def _photo(self) -> QImage:
        data = self._bytes()
        if self.item.ext in heic.HEIF_EXT and not (self.local or "").lower().endswith((".jpg", ".jpeg")):
            img, _ = heic.decode(data, max_side=8192)   # full detail for zooming
            img = img.convert("RGB")
            q = QImage(img.tobytes("raw", "RGB"), img.width, img.height, img.width * 3, QImage.Format_RGB888)
            return q.copy()  # own the pixels
        buf = QBuffer()
        buf.setData(QByteArray(data))
        buf.open(QIODevice.ReadOnly)
        r = QImageReader(buf)
        r.setAutoTransform(True)  # EXIF rotation
        q = r.read()
        if q.isNull():
            raise ValueError(r.errorString())
        return q

    def _video_file(self) -> str:
        if self.local and os.path.isfile(self.local):
            return self.local
        it = self.item
        os.makedirs(self.cache_dir, exist_ok=True)
        key = hashlib.sha1(f"{it.path}|{it.size}".encode()).hexdigest()[:16]
        dest = os.path.join(self.cache_dir, f"{key}{it.ext}")
        if os.path.isfile(dest) and os.path.getsize(dest) == it.size:
            os.utime(dest)  # mark as recently used
            return dest
        _trim_cache(self.cache_dir, keep_free=it.size)
        tmp = dest + ".part"
        h = self.device.open(it.path)
        try:
            with open(tmp, "wb") as out:
                done, last = 0, -1
                while done < it.size:
                    if self._cancel:
                        raise _Cancelled()
                    chunk = self.device.read(h, min(4 * 1024 * 1024, it.size - done))
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    pct = int(done * 100 / max(1, it.size))
                    if pct != last:
                        self.progress.emit(self.token, pct)
                        last = pct
        except BaseException:
            self.device.close_file(h)
            if os.path.exists(tmp):
                os.remove(tmp)
            raise
        self.device.close_file(h)
        if os.path.getsize(tmp) != it.size:
            os.remove(tmp)
            raise IOError("incomplete read")
        os.replace(tmp, dest)
        return dest


class _Cancelled(Exception):
    pass


def _trim_cache(folder: str, keep_free: int = 0) -> None:
    """Delete the oldest previewed videos from the app's OWN cache folder."""
    try:
        files = [os.path.join(folder, n) for n in os.listdir(folder)]
        files = [(os.path.getmtime(p), os.path.getsize(p), p) for p in files if os.path.isfile(p)]
    except OSError:
        return
    total = sum(s for _, s, _ in files) + keep_free
    for _, s, p in sorted(files):
        if total <= CACHE_LIMIT:
            break
        try:
            os.remove(p)
            total -= s
        except OSError:
            pass


# ===========================================================================
# Widgets
# ===========================================================================
class ClickSlider(QSlider):
    """A timeline you can click anywhere on to jump there (and keep dragging)."""

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self.maximum() > self.minimum():
            opt = QStyleOptionSlider()
            self.initStyleOption(opt)
            groove = self.style().subControlRect(QStyle.CC_Slider, opt, QStyle.SC_SliderGroove, self)
            handle = self.style().subControlRect(QStyle.CC_Slider, opt, QStyle.SC_SliderHandle, self)
            if not handle.contains(e.position().toPoint()):
                x = int(e.position().x()) - groove.x() - handle.width() // 2
                span = max(1, groove.width() - handle.width())
                v = QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), x, span)
                self.setValue(v)
                self.sliderMoved.emit(v)
        super().mousePressEvent(e)   # the handle is under the mouse now: dragging continues


class ArrowButton(QPushButton):
    """Tall side button with a big drawn chevron."""

    def __init__(self, direction: int):
        super().__init__()
        self.direction = direction
        self.setObjectName("sideArrow")
        self.setAttribute(Qt.WA_Hover)

    def paintEvent(self, e):
        from PySide6.QtGui import QColor, QPainter, QPen
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        hover = self.underMouse() and self.isEnabled()
        p.fillRect(self.rect(), QColor("#15171b" if hover else "#000000"))
        color = QColor("#ffffff" if hover else "#aab2bc") if self.isEnabled() else QColor("#2a2e35")
        p.setPen(QPen(color, 4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        cx, cy, k = self.width() / 2, self.height() / 2, 18
        d = self.direction
        from PySide6.QtCore import QPointF
        p.drawPolyline([QPointF(cx + d * -k / 2 + d * 0, cy - k * 1.4 / 1), QPointF(cx + d * k / 2, cy),
                        QPointF(cx + d * -k / 2, cy + k * 1.4)])
        p.end()


class ImageView(QWidget):
    """Photo viewer: fits the window; mouse wheel zooms around the cursor; drag to pan
    when zoomed; double-click toggles fit <-> 100 % at the clicked spot."""

    MAX_SCALE = 8.0          # up to 800 % of the real pixels

    def __init__(self):
        super().__init__()
        self.setMinimumSize(200, 150)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.setMouseTracking(False)
        self._pm = QPixmap()
        self._fit_cache = QPixmap()
        self._scale = 1.0        # image pixels -> widget pixels
        self._ox = self._oy = 0.0
        self._fitted = True
        self._drag = None
        self._badge_until = 0.0
        self._badge_timer = QTimer(self, singleShot=True, interval=1300)
        self._badge_timer.timeout.connect(self.update)

    # -- public ---------------------------------------------------------------
    def set_image(self, q: QImage | None):
        self._pm = QPixmap.fromImage(q) if q is not None else QPixmap()
        self._fit_cache = QPixmap()
        self.fit()

    def fit(self):
        self._fitted = True
        self._apply_fit()
        self.unsetCursor()
        self.update()

    @property
    def zoom_percent(self) -> int:
        return round(self._scale * 100)

    # -- geometry -------------------------------------------------------------
    def _fit_scale(self) -> float:
        if self._pm.isNull():
            return 1.0
        return min(self.width() / self._pm.width(), self.height() / self._pm.height())

    def _apply_fit(self):
        self._scale = self._fit_scale()
        self._center()

    def _center(self):
        self._ox = (self.width() - self._pm.width() * self._scale) / 2
        self._oy = (self.height() - self._pm.height() * self._scale) / 2

    def _clamp(self):
        w, h = self._pm.width() * self._scale, self._pm.height() * self._scale
        self._ox = (self.width() - w) / 2 if w <= self.width() else min(0, max(self.width() - w, self._ox))
        self._oy = (self.height() - h) / 2 if h <= self.height() else min(0, max(self.height() - h, self._oy))

    def _zoom_to(self, new_scale: float, cx: float, cy: float):
        fit = self._fit_scale()
        new_scale = max(fit, min(self.MAX_SCALE, new_scale))
        if abs(new_scale - fit) < 1e-6:
            self.fit()
            self._show_badge()
            return
        # keep the image point under the cursor exactly where it is
        ix, iy = (cx - self._ox) / self._scale, (cy - self._oy) / self._scale
        self._scale = new_scale
        self._ox, self._oy = cx - ix * new_scale, cy - iy * new_scale
        self._fitted = False
        self._clamp()
        self.setCursor(Qt.OpenHandCursor)
        self._show_badge()
        self.update()

    def _show_badge(self):
        self._badge_until = time.monotonic() + 1.2
        self._badge_timer.start()

    # -- events ---------------------------------------------------------------
    def wheelEvent(self, e):
        if self._pm.isNull():
            return
        steps = e.angleDelta().y() / 120.0
        if not steps:
            return
        p = e.position()
        self._zoom_to(self._scale * (1.25 ** steps), p.x(), p.y())

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and not self._fitted:
            self._drag = (e.position(), self._ox, self._oy)
            self.setCursor(Qt.ClosedHandCursor)
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._drag:
            start, ox, oy = self._drag
            d = e.position() - start
            self._ox, self._oy = ox + d.x(), oy + d.y()
            self._clamp()
            self.update()

    def mouseReleaseEvent(self, e):
        if self._drag:
            self._drag = None
            self.setCursor(Qt.OpenHandCursor)

    def mouseDoubleClickEvent(self, e):
        if self._pm.isNull():
            return
        if self._fitted:
            p = e.position()
            self._zoom_to(1.0 / self.devicePixelRatioF(), p.x(), p.y())   # real pixels
        else:
            self.fit()
            self._show_badge()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit_cache = QPixmap()
        if self._fitted:
            self._apply_fit()
        else:
            self._clamp()

    def paintEvent(self, e):
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QColor, QPainter
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#000000"))
        if self._pm.isNull():
            return
        target = QRectF(self._ox, self._oy, self._pm.width() * self._scale, self._pm.height() * self._scale)
        if self._fitted:
            # sharp, pre-scaled copy for the normal "fit" view
            dpr = self.devicePixelRatioF()
            want = QSize(round(target.width() * dpr), round(target.height() * dpr))
            if self._fit_cache.isNull() or self._fit_cache.size() != want:
                self._fit_cache = self._pm.scaled(want, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
                self._fit_cache.setDevicePixelRatio(dpr)
            p.drawPixmap(target.topLeft(), self._fit_cache)
        else:
            p.setRenderHint(QPainter.SmoothPixmapTransform, self._scale < 2.0)
            # draw only the visible part (fast even for very large photos)
            vis = target.intersected(QRectF(self.rect()))
            src = QRectF((vis.x() - self._ox) / self._scale, (vis.y() - self._oy) / self._scale,
                         vis.width() / self._scale, vis.height() / self._scale)
            p.drawPixmap(vis, self._pm, src)
        if time.monotonic() < self._badge_until:
            txt = "Fit" if self._fitted else f"{self.zoom_percent} %"
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, 170))
            r = self.rect().adjusted(self.width() // 2 - 44, self.height() - 52, -(self.width() // 2 - 44), -16)
            p.drawRoundedRect(r, 8, 8)
            p.setPen(QColor("#ffffff"))
            p.drawText(r, Qt.AlignCenter, txt)
        p.end()


class PreviewWindow(QWidget):
    def __init__(self, device, cache_dir: str, items_provider, local_provider, open_external, on_busy=None):
        super().__init__(None, Qt.Window)
        self.on_busy = on_busy or (lambda busy: None)   # pause background work while a video plays
        self.settings = settings()
        self.device, self.cache_dir = device, cache_dir
        self.items_provider = items_provider        # () -> list[MediaItem] in grid order
        self.local_provider = local_provider        # (MediaItem) -> PC path or None
        self.open_external = open_external          # (MediaItem) -> None
        self.item: MediaItem | None = None
        self.loader: PreviewLoader | None = None
        self._old_loaders: list[PreviewLoader] = []
        self._token = 0
        self._before_full = Qt.WindowNoState
        self.setWindowTitle("Preview")
        self.resize(1200, 820)
        self.setObjectName("preview")
        self._build()

    # -- layout -------------------------------------------------------------
    def _build(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        top = QFrame(objectName="topbar")
        th = QHBoxLayout(top)
        th.setContentsMargins(14, 8, 14, 8)
        self.prev_btn = QPushButton("◀")
        self.next_btn = QPushButton("▶")
        for b in (self.prev_btn, self.next_btn):
            b.setFixedWidth(40)
            b.setFocusPolicy(Qt.NoFocus)
        self.prev_btn.clicked.connect(lambda: self.step(-1))
        self.next_btn.clicked.connect(lambda: self.step(1))
        col = QVBoxLayout()
        col.setSpacing(0)
        self.name_label = QLabel("", objectName="title")
        self.info_label = QLabel("", objectName="muted")
        col.addWidget(self.name_label)
        col.addWidget(self.info_label)
        self.external_btn = QPushButton("Open in another app")
        self.external_btn.clicked.connect(lambda: self.item and self.open_external(self.item))
        th.addWidget(self.prev_btn)
        th.addWidget(self.next_btn)
        th.addSpacing(10)
        th.addLayout(col, 1)
        self.full_btn = QPushButton("Full screen")
        self.full_btn.setToolTip("Full screen  (Tab or F)\nPress Tab, F or Esc to come back")
        self.full_btn.setFocusPolicy(Qt.NoFocus)
        self.full_btn.clicked.connect(self.toggle_full)
        th.addWidget(self.full_btn)
        th.addWidget(self.external_btn)
        if sys.platform == "win32":
            th.addWidget(self._make_settings_button())
        v.addWidget(top)

        self.stack = QStackedWidget()
        self.stack.setObjectName("stage")
        self.image_view = ImageView()
        self.message = QLabel("", objectName="empty")
        self.message.setAlignment(Qt.AlignCenter)
        self.message.setWordWrap(True)
        self.stack.addWidget(self.image_view)   # 0
        self.stack.addWidget(self.message)      # 1
        self.player = None
        self.video_view = None
        self._pres = None            # presentation measurement (Smooth display only)
        import threading
        self._pres_lock = threading.Lock()
        self._display_widget = None
        if HAVE_VIDEO:
            self.audio = QAudioOutput(self)
            self.player = QMediaPlayer(self)
            self.player.setAudioOutput(self.audio)
            self._install_display(self.settings.value("video_display", "quick"))
            # Position is polled a few times a second instead of on every frame:
            # fewer trips into Python keeps the video smooth.
            self.tick = QTimer(self, interval=200)
            self.tick.timeout.connect(lambda: self._on_position(self.player.position()))
            self.player.durationChanged.connect(self._on_duration)
            self.player.playbackStateChanged.connect(self._on_state)
            self.player.errorOccurred.connect(self._on_error)
        # big arrows on the left and right sides (beside the picture, never covering it)
        stage = QHBoxLayout()
        stage.setContentsMargins(0, 0, 0, 0)
        stage.setSpacing(0)
        self.side_prev = ArrowButton(-1)
        self.side_next = ArrowButton(1)
        for b, d, tip in ((self.side_prev, -1, "Previous (← key)"), (self.side_next, 1, "Next (→ key)")):
            b.setFixedWidth(64)
            b.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
            b.setToolTip(tip)
            b.setFocusPolicy(Qt.NoFocus)
            b.clicked.connect(lambda _=False, d=d: self.step(d))
        stage.addWidget(self.side_prev)
        stage.addWidget(self.stack, 1)
        stage.addWidget(self.side_next)
        v.addLayout(stage, 1)

        # video controls
        self.controls = QFrame(objectName="statusbar")
        ch = QHBoxLayout(self.controls)
        ch.setContentsMargins(14, 6, 14, 6)
        self.play_btn = QPushButton("▶  Play")
        self.play_btn.setFixedWidth(96)
        self.play_btn.clicked.connect(self.toggle_play)
        self.seek = ClickSlider(Qt.Horizontal)
        self.seek.sliderMoved.connect(lambda ms: self.player and self.player.setPosition(ms))
        self.seek.sliderPressed.connect(lambda: self.player and self.player.setPosition(self.seek.value()))
        self.time_label = QLabel("0:00 / 0:00", objectName="muted")
        self.time_label.setMinimumWidth(96)
        self.time_label.setAlignment(Qt.AlignCenter)
        self.loop_btn = QPushButton("⟳  Loop", objectName="chip")
        self.loop_btn.setCheckable(True)
        self.loop_btn.setToolTip("Play the video over and over")
        self.loop_btn.setChecked(self.settings.value("loop", True, type=bool))
        self.loop_btn.toggled.connect(self._on_loop)
        self.mute_btn = QPushButton("🔊")
        self.mute_btn.setFixedWidth(40)
        self.mute_btn.setCheckable(True)
        self.mute_btn.setToolTip("Mute")
        self.mute_btn.toggled.connect(self._on_mute)
        self.volume = QSlider(Qt.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setFixedWidth(110)
        self.volume.setToolTip("Volume")
        self.volume.setValue(int(self.settings.value("volume", 80)))
        self.volume.valueChanged.connect(self._on_volume)
        ch.addWidget(self.play_btn)
        ch.addWidget(self.seek, 1)
        ch.addWidget(self.time_label)
        ch.addWidget(self.loop_btn)
        ch.addSpacing(6)
        ch.addWidget(self.mute_btn)
        ch.addWidget(self.volume)
        v.addWidget(self.controls)
        self.controls.hide()
        # keyboard always goes to the window (arrows = next/prev, Space = play/pause)
        for wdg in (self.play_btn, self.seek, self.loop_btn, self.mute_btn, self.volume, self.external_btn):
            wdg.setFocusPolicy(Qt.NoFocus)
        if self.player:
            self._on_volume(self.volume.value())
            self._on_loop(self.loop_btn.isChecked())

        self._keys = {Qt.Key_Left: lambda: self.step(-1), Qt.Key_Right: lambda: self.step(1),
                      Qt.Key_Space: self.toggle_play, Qt.Key_Escape: self._escape,
                      Qt.Key_F: self.toggle_full, Qt.Key_Tab: self.toggle_full,
                      Qt.Key_0: lambda: self.image_view.fit()}
        for key, fn in self._keys.items():
            QShortcut(QKeySequence(key), self, activated=fn)

    def _make_quick_output(self):
        """Video drawn by Qt Quick on its own render thread (smoother than a widget)."""
        from PySide6.QtCore import QObject
        from PySide6.QtGui import QColor
        from PySide6.QtQuick import QQuickView
        view = QQuickView()
        view.setColor(QColor("black"))
        view.setResizeMode(QQuickView.SizeRootObjectToView)
        view.setSource(QUrl.fromLocalFile(os.path.join(os.path.dirname(__file__), "video.qml")))
        if view.status() != QQuickView.Ready or view.rootObject() is None:
            raise RuntimeError("; ".join(e.toString() for e in view.errors()) or "QML failed")
        out = view.rootObject().findChild(QObject, "out")
        sink = out.property("videoSink")
        container = QWidget.createWindowContainer(view, self)
        container.setFocusPolicy(Qt.NoFocus)
        self._quick_container = container
        view.installEventFilter(self)            # keys pressed while the video has focus
        self.video_view = view
        # presentation measurement: how many screen refreshes each video frame stays up
        import threading
        self._pres_lock = threading.Lock()
        self._pres = None
        # Queued: these run on the main thread, in the order the events happened. (A direct
        # connection would make Qt's video threads wait for Python, which can deadlock.)
        sink.videoFrameChanged.connect(self._pres_frame, Qt.QueuedConnection)
        view.frameSwapped.connect(self._pres_swap, Qt.QueuedConnection)
        return out, sink

    def _install_display(self, mode: str):
        """Put the video display ("quick" = Smooth, "widget" = Standard) in place.
        Can be called while a video is open: the switch happens at once."""
        old = self._display_widget
        showing_video = self.stack.currentIndex() == 2
        if self.video_view is not None:
            self.video_view.rootObject().setProperty("steady", False)
        self.video_view, self._pres = None, None
        output = sink = None
        if mode == "quick":
            try:
                output, sink = self._make_quick_output()
                widget = self._quick_container
            except Exception:
                log.exception("Qt Quick video display unavailable; using the standard one")
                mode = "widget"
        if output is None:
            self.video_widget = QVideoWidget()
            widget, output, sink = self.video_widget, self.video_widget, self.video_widget.videoSink()
        self.video_display = mode
        self.player.setVideoOutput(output)
        sink.videoFrameChanged.connect(self._on_frame)
        if old is not None:
            self.stack.removeWidget(old)
            old.deleteLater()
        self.stack.insertWidget(2, widget)          # page 2 = video
        self._display_widget = widget
        if showing_video:
            self.stack.setCurrentIndex(2)
            self._stats_start()
            self._on_state(self.player.playbackState())   # re-applies the steady redraws
        log.info("video display: %s", mode)

    def eventFilter(self, obj, e):
        from PySide6.QtCore import QEvent
        if obj is self.video_view and e.type() == QEvent.KeyPress:
            fn = self._keys.get(e.key())
            if fn:
                fn()
                return True
        return super().eventFilter(obj, e)

    def show_item_info(self):
        it = self.item
        if it is None:
            return
        where = "On PC" if self.local_provider(it) else "Only on iPhone"
        self.info_label.setText(f"{it.date:%A, %B %d, %Y  %H:%M} · {human_bytes(it.size)} · {where}")

    def _make_settings_button(self):
        """⋯ : graphics chip + video engine (for trying out smoother playback)."""
        from PySide6.QtGui import QAction, QActionGroup
        from PySide6.QtWidgets import QMenu, QToolButton
        btn = QToolButton(objectName="more")
        btn.setText("⋯")
        btn.setToolTip("Playback settings")
        btn.setPopupMode(QToolButton.InstantPopup)
        btn.setFocusPolicy(Qt.NoFocus)
        menu = QMenu(btn)

        def group(title, key, default, options):
            menu.addSection(title)
            g = QActionGroup(menu)
            cur = self.settings.value(key, default)
            for value, label in options:
                a = QAction(label, menu, checkable=True)
                a.setChecked(value == cur)
                a.triggered.connect(lambda _=False, v=value: self._set_restart_setting(key, v))
                g.addAction(a)
                menu.addAction(a)

        group("Graphics chip", "gpu_choice", "auto",
              [("auto", "Automatic (Windows decides)"), ("power", "Built-in graphics"),
               ("performance", "Graphics card")])
        group("Video engine", "video_engine", "ffmpeg",
              [("ffmpeg", "Qt / FFmpeg"), ("windows", "Windows (Media Foundation)")])
        group("Video decoding", "video_decoding", "software",
              [("software", "Processor"), ("hardware", "Graphics card")])
        group("Video display", "video_display", "quick",
              [("quick", "Smooth (own drawing thread)"), ("widget", "Standard")])
        menu.addSection("Video display switches at once; the others from the next start")
        btn.setMenu(menu)
        return btn

    def _set_restart_setting(self, key, value):
        self.settings.setValue(key, value)
        if key == "video_display" and self.player is not None:
            self._install_display(value)          # this one applies right away
            self._show_toast("Video display switched.")
            return
        log.info("playback setting %s = %s (from next start)", key, value)
        self._show_toast("Saved. Close and reopen the app to use it.")

    def _show_toast(self, text):
        self.info_label.setText(text)
        QTimer.singleShot(4000, lambda: self.item and self.show_item_info())

    # -- playback measurements (written to lptpro.log) ----------------------
    def _stats_start(self):
        self._st = {"n": 0, "first": None, "last": None, "gap": 0.0, "reports": 0, "t0": time.monotonic()}
        if self.video_view is not None:
            with self._pres_lock:
                self._pres = {"pending": False, "last": None, "gaps": [], "skipped": 0}

    # presentation counters (run on the main thread via queued signals)
    def _pres_frame(self, frame):
        p = self._pres
        if p is None:
            return
        with self._pres_lock:
            if p["pending"]:
                p["skipped"] += 1        # a newer frame arrived before the last one was shown
            p["pending"] = True

    def _pres_swap(self):
        p = self._pres
        if p is None:
            return
        now = time.monotonic()
        with self._pres_lock:
            if p["pending"]:            # this redraw put a new video frame on screen
                if p["last"] is not None:
                    p["gaps"].append(now - p["last"])
                p["last"], p["pending"] = now, False

    def _pres_report(self) -> str:
        if self._pres is None:
            return ""
        with self._pres_lock:
            gaps, skipped = self._pres["gaps"], self._pres["skipped"]
            self._pres.update(gaps=[], skipped=0)
        if not gaps:
            return ""
        import collections
        hz = self.screen().refreshRate() if self.screen() else 60.0
        c = collections.Counter(max(0, round(g * hz)) for g in gaps)   # in screen refreshes
        total = len(gaps)
        parts = ", ".join(f"{k} refreshes {100 * v / total:.0f}%" for k, v in sorted(c.items()))
        return f" | screen {hz:.0f} Hz, new frame drawn every: {parts}; {skipped} frames never drawn"

    def _on_frame(self, frame):
        st = getattr(self, "_st", None)
        if not st or st["reports"] >= 3 or not self.player or \
                self.player.playbackState() != QMediaPlayer.PlayingState:
            return
        now = time.monotonic()
        if st["last"] is not None:
            st["gap"] = max(st["gap"], now - st["last"])
        else:
            st["first"] = now
            st["fmt"] = f"{frame.width()}x{frame.height()} {frame.pixelFormat().name}"
        st["last"] = now
        st["n"] += 1
        if now - st["first"] >= 5.0:
            fps = (st["n"] - 1) / (now - st["first"])
            meta_fps = None
            try:
                from PySide6.QtMultimedia import QMediaMetaData
                meta_fps = self.player.metaData().value(QMediaMetaData.VideoFrameRate)
            except Exception:
                pass
            log.info("playback %s: %.1f frames/s shown (video says %s), longest gap %d ms, %s%s",
                     self.item.name if self.item else "?", fps, meta_fps, st["gap"] * 1000, st.get("fmt"),
                     self._pres_report())
            if st["reports"] + 1 >= 3 and self._pres is not None:
                self._pres = None   # stop measuring
            st.update(n=0, first=None, last=None, gap=0.0, reports=st["reports"] + 1)

    # -- navigation -----------------------------------------------------------
    def show_item(self, it: MediaItem):
        self.item = it
        self._token += 1
        if self.player:
            self.player.stop()
            self.player.setSource(QUrl())
        if self.loader and self.loader.isRunning():
            self.loader.cancel()
            self._old_loaders.append(self.loader)
        self._old_loaders = [l for l in self._old_loaders if l.isRunning()]

        local = self.local_provider(it)
        where = "On PC" if local else "Only on iPhone"
        self.name_label.setText(it.name)
        self.show_item_info()
        self.setWindowTitle(f"{it.name} · Preview")
        self.controls.setVisible(it.kind == "video" and self.player is not None)
        self.seek.setRange(0, 0)
        self.time_label.setText("0:00 / 0:00")

        if it.kind == "video" and self.player is None:
            self._show_message("Video playback isn’t installed. Use “Open in another app”.")
            return
        self._show_message("Getting it from the iPhone…" if not local else "Loading…")
        self.loader = PreviewLoader(self.device, it, local, self.cache_dir, self._token)
        self.loader.image.connect(self._on_image)
        self.loader.video.connect(self._on_video)
        self.loader.progress.connect(self._on_progress)
        self.loader.failed.connect(self._on_failed)
        self.loader.start()
        self._update_nav()

    def step(self, d: int):
        items = self.items_provider()
        if not items or self.item is None:
            return
        try:
            i = next(k for k, x in enumerate(items) if x.path == self.item.path)
        except StopIteration:
            i = 0
        j = i + d
        if 0 <= j < len(items):
            self.show_item(items[j])

    def _update_nav(self):
        items = self.items_provider()
        paths = [x.path for x in items]
        i = paths.index(self.item.path) if self.item and self.item.path in paths else -1
        self.prev_btn.setEnabled(i > 0)
        self.next_btn.setEnabled(0 <= i < len(items) - 1)
        self.side_prev.setEnabled(i > 0)
        self.side_next.setEnabled(0 <= i < len(items) - 1)

    # -- loader results -------------------------------------------------------
    def _show_message(self, text):
        self.message.setText(text)
        self.stack.setCurrentIndex(1)

    def _on_progress(self, token, pct):
        if token == self._token:
            self._show_message(f"Getting the video from the iPhone… {pct}%")

    def _on_failed(self, token, msg):
        if token == self._token:
            self._show_message(msg)

    def _on_image(self, token, q):
        if token != self._token:
            return
        self.image_view.set_image(q)
        self.stack.setCurrentIndex(0)

    def _on_video(self, token, path):
        if token != self._token or not self.player:
            return
        self.stack.setCurrentIndex(2)
        self._stats_start()
        self.player.setSource(QUrl.fromLocalFile(path))
        self.player.play()

    # -- player ------------------------------------------------------------------
    def toggle_play(self):
        if not self.player or not self.item or self.item.kind != "video":
            return
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def _on_state(self, state):
        playing = state == QMediaPlayer.PlayingState
        if self.video_view is not None and self.video_view.rootObject() is not None:
            # steady redraws only while playing (no extra work when paused or closed)
            self.video_view.rootObject().setProperty("steady", playing)
        # measure playing time only: a pause must not count as a long gap between frames
        st = getattr(self, "_st", None)
        if st is not None:
            st.update(n=0, first=None, last=None, gap=0.0)
        if self._pres is not None:
            with self._pres_lock:
                self._pres.update(pending=False, last=None)
        self.play_btn.setText("❚❚  Pause" if playing else "▶  Play")
        (self.tick.start if playing else self.tick.stop)()
        self._on_position(self.player.position())
        self.on_busy(playing)

    def _on_position(self, ms):
        if not self.seek.isSliderDown():
            self.seek.setValue(ms)
        self.time_label.setText(f"{clock(ms)} / {clock(self.player.duration())}")

    def _on_duration(self, ms):
        self.seek.setRange(0, ms)
        self.time_label.setText(f"{clock(self.player.position())} / {clock(ms)}")

    def _on_mute(self, muted):
        self.audio.setMuted(muted)
        self.mute_btn.setText("🔇" if muted else "🔊")
        self.mute_btn.setToolTip("Unmute" if muted else "Mute")

    def _on_volume(self, v):
        # perceptual curve: the slider feels even from quiet to loud
        self.audio.setVolume((v / 100.0) ** 2)
        self.settings.setValue("volume", v)
        if v > 0 and self.mute_btn.isChecked():
            self.mute_btn.setChecked(False)

    def _on_loop(self, on):
        self.player.setLoops(QMediaPlayer.Infinite if on else 1)
        self.settings.setValue("loop", on)

    def _on_error(self, *_):
        if self.player and self.player.error() != QMediaPlayer.NoError:
            log.warning("video playback error: %s", self.player.errorString())
            self._show_message("This video can’t be played here (" + (self.player.errorString() or "unknown format")
                               + ").\nUse “Open in another app”.")

    def changeEvent(self, e):
        super().changeEvent(e)
        from PySide6.QtCore import QEvent
        if e.type() == QEvent.WindowStateChange and hasattr(self, "full_btn"):
            full = self.isFullScreen()
            self.full_btn.setText("Exit full screen" if full else "Full screen")
            self.full_btn.setToolTip("Back to the window  (Tab, F or Esc)" if full
                                     else "Full screen  (Tab or F)\nPress Tab, F or Esc to come back")

    def toggle_full(self):
        """Full screen <-> exactly how the window was before (maximized or normal)."""
        if self.isFullScreen():
            if self._before_full & Qt.WindowMaximized:
                self.showMaximized()
            else:
                self.showNormal()
        else:
            self._before_full = self.windowState()
            self.showFullScreen()

    def _escape(self):
        if self.isFullScreen():
            self.toggle_full()     # first Esc leaves full screen
        else:
            self.close()

    def stop_all(self):
        if self.player:
            self.player.stop()
            self.player.setSource(QUrl())
        self.on_busy(False)
        for l in [self.loader, *self._old_loaders]:
            if l and l.isRunning():
                l.cancel()
                l.wait(3000)

    def closeEvent(self, e):
        self.stop_all()
        if self.isFullScreen():
            self.showNormal()
        super().closeEvent(e)
