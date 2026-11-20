# SPDX-License-Identifier: GPL-3.0-or-later
"""Album mode: browse the backup folder on the PC, no iPhone needed.

The same grid, previews, photo viewer and video player as for the iPhone, pointed at the
backup folder (through FolderBackend). Previews are kept in the app's data folder so the
next visit is instant. Right-click a tile for "Show in folder".
"""
from __future__ import annotations

import calendar
import datetime as dt
import logging
import os
import subprocess
import sys

from PySide6.QtCore import QThread, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox, QFrame, QHBoxLayout,
                               QLabel, QMenu, QPushButton, QSlider, QVBoxLayout, QWidget)

from .device import FolderBackend
from .importer import scan_folder
from .jobs import CaptureDateReader, ThumbLoader
from .media import mark_live_photos
from .videothumbs import VideoThumbnailer

log = logging.getLogger("transfer")


def show_in_folder(path: str) -> None:
    """Open the file manager with this file selected."""
    path = os.path.abspath(path)
    try:
        if sys.platform == "win32":
            subprocess.Popen(f'explorer /select,"{os.path.normpath(path)}"')
            return
        if sys.platform.startswith("linux"):
            uri = QUrl.fromLocalFile(path).toString()
            r = subprocess.run(["gdbus", "call", "--session", "--dest", "org.freedesktop.FileManager1",
                                "--object-path", "/org/freedesktop/FileManager1",
                                "--method", "org.freedesktop.FileManager1.ShowItems", f"['{uri}']", ""],
                               capture_output=True, timeout=5)
            if r.returncode == 0:
                return
    except Exception:
        log.debug("show in folder failed", exc_info=True)
    QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(path)))   # at least open the folder


class _Scan(QThread):
    done = Signal(object)

    def __init__(self, root):
        super().__init__()
        self.root = root

    def run(self):
        items = scan_folder(self.root)
        mark_live_photos(items)
        self.done.emit(items)


class AlbumView(QWidget):
    """The backup folder as a browsable album."""

    def __init__(self, window, model_cls, view_cls, delegate_cls, data_dir):
        super().__init__()
        self.window_ = window
        self.root = None
        self.device = None
        self.thumbs = None
        self.vthumbs = None
        self.dates = None
        self.preview = None
        self.stale = True
        self.cache_dir = os.path.join(data_dir, "album-previews")
        self.model = model_cls()
        self._build(view_cls, delegate_cls)

    # -- layout ------------------------------------------------------------------------
    def _build(self, view_cls, delegate_cls):
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        head = QFrame(objectName="audit")
        hh = QHBoxLayout(head)
        hh.setContentsMargins(16, 10, 16, 10)
        col = QVBoxLayout()
        self.title = QLabel("Album", objectName="auditTitle")
        self.subtitle = QLabel("", objectName="muted")
        col.addWidget(self.title)
        col.addWidget(self.subtitle)
        hh.addLayout(col, 1)
        refresh = QPushButton("Refresh")
        refresh.setToolTip("Look at the backup folder again (after adding or moving files)")
        refresh.clicked.connect(lambda: self.load(force=True))
        hh.addWidget(refresh)
        v.addWidget(head)

        bar = QFrame(objectName="filterbar")
        fh = QHBoxLayout(bar)
        fh.setContentsMargins(16, 8, 16, 8)
        self.kind_group = QButtonGroup(self)
        for i, (label, key) in enumerate((("All", "all"), ("Photos", "photo"), ("Videos", "video"))):
            b = QPushButton(label, objectName="chip")
            b.setCheckable(True)
            b.setChecked(i == 0)
            b.setProperty("kind", key)
            self.kind_group.addButton(b)
            fh.addWidget(b)
        self.kind_group.buttonClicked.connect(self.on_filter)
        fh.addSpacing(12)
        self.month_box = QComboBox()
        self.month_box.addItem("All months", 0)
        for m in range(1, 13):
            self.month_box.addItem(f"{m} - {calendar.month_name[m]}", m)
        self.year_box = QComboBox()
        self.year_box.addItem("All years", 0)
        w = max(self.month_box.fontMetrics().horizontalAdvance(t) for t in ("12 - September", "2026  (12,345)")) + 48
        for box in (self.month_box, self.year_box):
            box.setMinimumWidth(w)
            box.currentIndexChanged.connect(self.on_filter)
            fh.addWidget(box)
        fh.addSpacing(12)
        self.hide_live = QCheckBox("Hide Live Photo videos")
        self.hide_live.toggled.connect(self.on_filter)
        fh.addWidget(self.hide_live)
        fh.addStretch(1)
        fh.addWidget(QLabel("Size", objectName="muted"))
        self.size_slider = QSlider(Qt.Horizontal)
        self.size_slider.setRange(96, 260)
        self.size_slider.setValue(int(self.window_.settings.value("tile", 140)))
        self.size_slider.setFixedWidth(120)
        self.size_slider.valueChanged.connect(self._on_size)
        fh.addWidget(self.size_slider)
        v.addWidget(bar)

        self.grid = view_cls(self.model)
        self.delegate = delegate_cls(self.grid, self.model, self.request_thumb)
        self.delegate.tile = self.size_slider.value()
        self.grid.setItemDelegate(self.delegate)
        self.grid.open_item.connect(self.open_item)
        self.grid.setContextMenuPolicy(Qt.CustomContextMenu)
        self.grid.customContextMenuRequested.connect(self._menu)
        self.empty = QLabel("", objectName="empty")
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.hide()
        body = QWidget()
        bv = QVBoxLayout(body)
        bv.setContentsMargins(12, 0, 12, 0)
        bv.addWidget(self.grid, 1)
        bv.addWidget(self.empty, 1)
        v.addWidget(body, 1)

    # -- loading ---------------------------------------------------------------------
    def load(self, force=False):
        root = self.window_.backup_root
        if not root or not os.path.isdir(root):
            self._message("Choose a backup folder first (top right), then come back here.")
            return
        if not force and not self.stale and root == self.root:
            self._resume()
            return
        self._stop_workers()
        self.root, self.stale = root, False
        self.device = FolderBackend(root)
        self.device.name = "Backup folder"
        self.title.setText("Album")
        self.subtitle.setText(f"Looking through {root}…")
        self._message("Looking through your backup folder…")
        self._scan = _Scan(root)
        self._scan.done.connect(self._on_scanned)
        self._scan.start()

    def _on_scanned(self, items):
        if self.sender() is not getattr(self, "_scan", None):
            return
        items.sort(key=lambda x: (x.date, x.name), reverse=True)
        n_photo = sum(1 for i in items if i.kind == "photo")
        self.subtitle.setText(f"{len(items):,} items · {n_photo:,} photos · {len(items) - n_photo:,} videos"
                              f" · {self.root}")
        self.model.thumbs.clear()
        self.model.requested.clear()
        self._fill_years(items)
        self.model.set_items(items)
        self.on_filter()
        if not items:
            self._message("No photos or videos in the backup folder yet.")
            return
        os.makedirs(self.cache_dir, exist_ok=True)
        self.thumbs = ThumbLoader(self.device, self.cache_dir)
        self.thumbs.ready.connect(self.on_thumb)
        self.thumbs.needs_decode.connect(self._on_needs_decode)
        self.vthumbs = VideoThumbnailer(self.device, self.thumbs._cache_path, self)
        self.vthumbs.ready.connect(self.on_thumb)
        self.thumbs.start()
        self.thumbs.prefetch(items)
        self.vthumbs.prefetch([])
        self.dates = CaptureDateReader(self.device, items)
        self._changed = 0
        self.dates.found.connect(self._on_dates)
        self.dates.finished.connect(self._on_dates_done)
        self.dates.start()
        log.info("album: %d items in %s", len(items), self.root)

    def _on_dates(self, batch):
        for path, when in batch:
            it = self.model.by_path.get(path)
            if it is not None:
                it.date = when
                self._changed += 1

    def _on_dates_done(self):
        if self.sender() is not self.dates or not self._changed:
            return
        items = sorted(self.model.items, key=lambda x: (x.date, x.name), reverse=True)
        self._fill_years(items)
        self.model.set_items(items)
        self.on_filter()

    def _fill_years(self, items):
        years = sorted({i.date.year for i in items}, reverse=True)
        keep = self.year_box.currentData() if self.year_box.count() > 1 else None
        self.year_box.blockSignals(True)
        self.year_box.clear()
        self.year_box.addItem("All years", 0)
        for y in years:
            self.year_box.addItem(f"{y}  ({sum(1 for i in items if i.date.year == y):,})", y)
        want = keep if keep in years or keep == 0 else 0
        self.year_box.setCurrentIndex(max(0, self.year_box.findData(want)))
        self.year_box.blockSignals(False)

    # -- grid ------------------------------------------------------------------------
    def on_filter(self, *_):
        b = self.kind_group.checkedButton()
        self.model.kind = b.property("kind") if b else "all"
        self.model.hide_live = self.hide_live.isChecked()
        self.model.month = self.month_box.currentData() or 0
        self.model.year = self.year_box.currentData() or 0
        self.model.rebuild()
        if self.model.items and not self.model.rows:
            self._message("Nothing matches this filter.")
        elif self.model.items:
            self.empty.hide()
            self.grid.show()

    def _message(self, text):
        self.empty.setText(text)
        self.empty.show()
        self.grid.hide()

    def _on_size(self, v):
        self.delegate.tile = v
        self.grid.scheduleDelayedItemsLayout()

    def request_thumb(self, it):
        if self.thumbs:
            self.thumbs.request(it)

    def _on_needs_decode(self, it, wanted):
        if self.vthumbs:
            (self.vthumbs.request if wanted else self.vthumbs.add_prefetch)(it)

    def on_thumb(self, path, data, duration=0):
        it = self.model.by_path.get(path)
        if duration and it:
            it.extra["duration"] = duration
        pm = QPixmap()
        if data:
            pm.loadFromData(data, "JPG")
        self.model.thumbs[path] = pm
        self.model.refresh_path(path)

    def local(self, it) -> str:
        return os.path.join(self.root, it.path.lstrip("/").replace("/", os.sep))

    # -- actions ---------------------------------------------------------------------
    def _menu(self, pos):
        ix = self.grid.indexAt(pos)
        if not ix.isValid():
            return
        row = self.model.rows[ix.row()]
        if row[0] != "i":
            return
        it = row[1]
        m = QMenu(self)
        m.addAction("Open", lambda: self.open_item(it))
        m.addAction("Show in folder", lambda: show_in_folder(self.local(it)))
        m.addAction("Open in another app", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(self.local(it))))
        m.exec(self.grid.viewport().mapToGlobal(pos))

    def open_item(self, it):
        from .preview import PreviewWindow
        if self.preview is None:
            self.preview = PreviewWindow(
                self.device, os.path.join(self.cache_dir, "videos"),
                items_provider=lambda: [r[1] for r in self.model.rows if r[0] == "i"],
                local_provider=self.local,
                open_external=lambda x: QDesktopServices.openUrl(QUrl.fromLocalFile(self.local(x))),
                on_busy=self._preview_busy)
        self.preview.device = self.device
        self.preview.show_item(it)
        self.preview.show()
        self.preview.raise_()
        self.preview.activateWindow()

    def _preview_busy(self, busy):
        if self.thumbs:
            self.thumbs.pause_prefetch(busy)
        if self.vthumbs:
            self.vthumbs.pause(busy)
        if self.dates:
            self.dates.pause(busy)

    # -- lifecycle -----------------------------------------------------------------------
    def _resume(self):
        if self.thumbs:
            self.thumbs.pause_prefetch(False)
        if self.vthumbs:
            self.vthumbs.pause_prefetch(False)

    def pause(self):
        """Leaving Album mode: let the iPhone work have the PC."""
        if self.thumbs:
            self.thumbs.pause_prefetch(True)
        if self.vthumbs:
            self.vthumbs.pause_prefetch(True)
        if self.preview:
            self.preview.close()

    def _stop_workers(self):
        if self.vthumbs:
            self.vthumbs.stop()
        if self.thumbs:
            self.thumbs.stop()
            self.thumbs.wait(3000)
        if self.dates and self.dates.isRunning():
            self.dates.cancel()
            self.dates.wait(3000)
        self.thumbs = self.vthumbs = self.dates = None

    def shutdown(self):
        self._stop_workers()
        if self.preview:
            self.preview.close()
