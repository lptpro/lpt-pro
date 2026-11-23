# SPDX-License-Identifier: GPL-3.0-or-later
"""Album mode: browse the backup folder on the PC, no iPhone needed.

The same grid, previews, photo viewer and video player as for the iPhone, pointed at the
backup folder (through FolderBackend). Previews are kept in the app's data folder so the
next visit is instant. Right-click a tile for "Show in folder".
"""
from __future__ import annotations

import calendar
import json
import logging
import os
import shutil
import subprocess
import sys

from PySide6.QtCore import QStandardPaths, QThread, QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QDialog, QFileDialog, QFrame,
                               QHBoxLayout, QLabel, QMenu, QMessageBox, QPushButton, QSlider,
                               QVBoxLayout, QWidget)

from . import BACKUP_META_DIR
from .backup_index import hide_folder

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


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024


class Favorites:
    """The favorites list, kept in the backup folder's hidden bookkeeping folder so it travels
    with the backup. Marking a favorite copies nothing."""

    def __init__(self, root):
        self.path = os.path.join(root, BACKUP_META_DIR, "favorites.json")
        try:
            with open(self.path, encoding="utf-8") as f:
                self.items = set(json.load(f).get("items", []))
        except (OSError, ValueError):
            self.items = set()

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            hide_folder(os.path.dirname(self.path))
            with open(self.path + ".tmp", "w", encoding="utf-8") as f:
                json.dump({"version": 1, "items": sorted(self.items)}, f, indent=0)
            os.replace(self.path + ".tmp", self.path)
        except OSError:
            log.exception("could not save favorites")


class ExportDialog(QDialog):
    """Where and how to export the favorites; shows the space it will take."""

    def __init__(self, parent, items, backup_root, settings):
        super().__init__(parent)
        self.setWindowTitle("Export favorites")
        self.items, self.backup_root, self.settings = items, backup_root, settings
        self.total = sum(i.size for i in items)
        pictures = QStandardPaths.writableLocation(QStandardPaths.PicturesLocation) or os.path.expanduser("~")
        self.dest = settings.value("export_dir", os.path.join(pictures, "LPT Pro Favorites"))
        v = QVBoxLayout(self)
        v.setContentsMargins(20, 18, 20, 16)
        v.setSpacing(10)
        head = QLabel(f"Copy your {len(items):,} favorites ({human_bytes(self.total)}) into a separate folder, "
                      f"for example to upload for a photo book or prints. Your backup isn\u2019t changed, and "
                      f"favorites already exported before are skipped.")
        head.setWordWrap(True)
        v.addWidget(head)
        row = QHBoxLayout()
        self.dest_label = QLabel("", objectName="muted")
        self.dest_label.setWordWrap(True)
        change = QPushButton("Change\u2026")
        change.clicked.connect(self._choose)
        row.addWidget(self.dest_label, 1)
        row.addWidget(change)
        v.addLayout(row)
        self.by_date = QCheckBox("Organize by date (folders by year and month)")
        self.by_date.setChecked(settings.value("export_by_date", True, type=bool))
        self.jpeg = QCheckBox("Save HEIC photos as JPEG (most photo-book and print sites need JPEG)")
        self.jpeg.setChecked(settings.value("export_jpeg", True, type=bool))
        v.addWidget(self.by_date)
        v.addWidget(self.jpeg)
        self.space = QLabel("")
        self.space.setWordWrap(True)
        v.addWidget(self.space)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        self.go = QPushButton("Export", objectName="primary")
        self.go.setDefault(True)
        self.go.clicked.connect(self._accept)
        buttons.addWidget(cancel)
        buttons.addWidget(self.go)
        v.addLayout(buttons)
        self.setMinimumWidth(560)
        self._update()

    def _choose(self):
        d = QFileDialog.getExistingDirectory(self, "Export favorites to", os.path.dirname(self.dest))
        if d:
            self.dest = d
            self._update()

    def _free(self) -> int | None:
        probe = self.dest
        while probe and not os.path.exists(probe):
            parent = os.path.dirname(probe)
            if parent == probe:
                break
            probe = parent
        try:
            return shutil.disk_usage(probe).free
        except OSError:
            return None

    def _bad_place(self) -> bool:
        a, b = os.path.abspath(self.dest), os.path.abspath(self.backup_root)
        try:
            return os.path.commonpath([a, b]) in (a, b)
        except ValueError:
            return False

    def _update(self):
        self.dest_label.setText(f"Folder:  {self.dest}")
        free = self._free()
        if self._bad_place():
            self.space.setText("\u26a0 That folder is inside (or contains) your backup folder. Pick a "
                               "folder outside it, so the copies aren\u2019t mistaken for backups.")
            self.go.setEnabled(False)
            return
        if free is not None and free < self.total:
            self.space.setText(f"\u26a0 Not enough space there: this needs {human_bytes(self.total)}, "
                               f"and only {human_bytes(free)} is free.")
            self.go.setEnabled(False)
            return
        self.space.setText(f"This makes copies, so it uses up to {human_bytes(self.total)} of extra space"
                           + (f" ({human_bytes(free)} free on that drive)." if free is not None else "."))
        self.go.setEnabled(True)

    def _accept(self):
        self.settings.setValue("export_dir", self.dest)
        self.settings.setValue("export_by_date", self.by_date.isChecked())
        self.settings.setValue("export_jpeg", self.jpeg.isChecked())
        self.accept()


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
        # "-2": previews made before 0.23.1 could show a frame of the wrong video; start fresh
        self.cache_dir = os.path.join(data_dir, "album-previews-2")
        shutil.rmtree(os.path.join(data_dir, "album-previews"), ignore_errors=True)
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
        self.clear_btn = QPushButton("Clear favorites")
        self.clear_btn.setToolTip("Remove all the stars (no photos are deleted)")
        self.clear_btn.clicked.connect(self.clear_favorites)
        self.clear_btn.setEnabled(False)
        hh.addWidget(self.clear_btn)
        self.export_btn = QPushButton("Export favorites\u2026", objectName="orange")
        self.export_btn.setToolTip("Copy your favorites into a separate folder (for a photo book or prints)")
        self.export_btn.clicked.connect(self.export_favorites)
        self.export_btn.setEnabled(False)
        hh.addWidget(self.export_btn)
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
        fh.addWidget(QLabel("Show", objectName="muted"))
        self.show_box = QComboBox()
        self.show_box.addItem("Everything", "all")
        self.show_box.addItem("Favorites", "fav")
        self.show_box.currentIndexChanged.connect(self.on_filter)
        fh.addWidget(self.show_box)
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
        self.grid.star_clicked.connect(lambda it: self.toggle_favorite([it]))
        self.grid.allow_drag = False
        shift = getattr(self.window_, "shift", None)
        if shift is not None:                       # Shift multi-select, as in iPhone mode
            self.grid.multi = lambda: shift.active
            self.grid.on_click = shift.note_click
        self.model.stars = True
        self.favorites = None
        QShortcut(QKeySequence("F"), self, activated=self._favorite_key)
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
        self.favorites = Favorites(self.root)
        for it in items:
            if it.path in self.favorites.items:
                it.extra["fav"] = True
        self._fill_years(items)
        self.model.set_items(items)
        self._render_fav_count()
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
        self.model.show = self.show_box.currentData() or "all"
        self.model.rebuild()
        if self.model.items and not self.model.rows:
            self._message("No favorites yet. Press F (or click the \u2605 on a photo) to mark one."
                          if self.model.show == "fav" else "Nothing matches this filter.")
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
        targets = self._targets(it)
        m = QMenu(self)
        m.addAction("Open", lambda: self.open_item(it))
        fav = all(i.extra.get("fav") for i in targets)
        label = ("Remove from favorites" if fav else "Add to favorites") + (
            f"  ({len(targets):,} selected)" if len(targets) > 1 else "") + "\tF"
        m.addAction(label, lambda: self.toggle_favorite(targets))
        m.addAction("Show in folder", lambda: show_in_folder(self.local(it)))
        m.exec(self.grid.viewport().mapToGlobal(pos))

    def _targets(self, it=None):
        """What an action applies to: the selection if `it` is part of it, else just `it`."""
        sel = [self.model.by_path[p] for p in self.model.selected if p in self.model.by_path]
        if it is None:
            if sel:
                return sel
            cur = self.model.by_path.get(self.model.current)
            return [cur] if cur else []
        return sel if it.path in self.model.selected and sel else [it]

    def _favorite_key(self):
        targets = self._targets()
        if targets:
            self.toggle_favorite(targets)

    def toggle_favorite(self, items):
        """Mark (or unmark) as favorites. Nothing is copied; the list is saved right away."""
        if not items or self.favorites is None:
            return
        on = not all(i.extra.get("fav") for i in items)
        for it in items:
            it.extra["fav"] = on
            (self.favorites.items.add if on else self.favorites.items.discard)(it.path)
        self.favorites.save()
        self._render_fav_count()
        if self.model.show == "fav":
            self.on_filter()
        else:
            self.model.refresh_all()

    def _render_fav_count(self):
        n = sum(1 for i in self.model.items if i.extra.get("fav"))
        self.show_box.setItemText(self.show_box.findData("fav"), f"Favorites ({n:,})")
        self.export_btn.setEnabled(n > 0)
        self.clear_btn.setEnabled(n > 0)
        self.export_btn.setText(f"Export {n:,} favorites\u2026" if n else "Export favorites\u2026")

    def clear_favorites(self):
        favs = [i for i in self.model.items if i.extra.get("fav")]
        if not favs or self.favorites is None:
            return
        box = QMessageBox(self)
        box.setWindowTitle("Clear favorites")
        box.setIcon(QMessageBox.Question)
        box.setText(f"Clear all {len(favs):,} favorites?\n\nThis only removes the stars. No photos are "
                    f"deleted, and copies you already exported stay where they are.")
        clear = box.addButton("Clear", QMessageBox.DestructiveRole)
        box.addButton(QMessageBox.Cancel)
        box.setDefaultButton(QMessageBox.Cancel)
        box.exec()
        if box.clickedButton() is not clear:
            return
        for it in favs:
            it.extra["fav"] = False
        self.favorites.items.clear()
        self.favorites.save()
        self._render_fav_count()
        if self.model.show == "fav":
            self.on_filter()
        else:
            self.model.refresh_all()

    def export_favorites(self):
        favs = [i for i in self.model.items if i.extra.get("fav")]
        if not favs:
            return
        d = ExportDialog(self, favs, self.root, self.window_.settings)
        if d.exec() == QDialog.Accepted:
            self.window_.start_export(self.device, favs, d.dest, d.by_date.isChecked(), d.jpeg.isChecked())

    def open_item(self, it):
        from .preview import PreviewWindow
        if self.preview is None:
            self.preview = PreviewWindow(
                self.device, os.path.join(self.cache_dir, "videos"),
                items_provider=lambda: [r[1] for r in self.model.rows if r[0] == "i"],
                local_provider=self.local,
                open_external=lambda x: QDesktopServices.openUrl(QUrl.fromLocalFile(self.local(x))),
                on_busy=self._preview_busy,
                on_current=self._follow,
                on_favorite=lambda x: self.toggle_favorite([x]))
        self.preview.device = self.device
        self.preview.show_item(it)
        self.preview.show()
        self.preview.raise_()
        self.preview.activateWindow()

    def _follow(self, it):
        from .ui import follow_viewer
        follow_viewer(self.model, self.grid, it)

    def _preview_busy(self, busy):
        if self.thumbs:
            self.thumbs.pause_prefetch(busy)
        if self.vthumbs:
            self.vthumbs.pause(busy)          # no second video decoder while one plays
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
