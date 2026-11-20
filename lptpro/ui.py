# SPDX-License-Identifier: GPL-3.0-or-later
"""The main window. Only drawing and user input happen on this thread."""
from __future__ import annotations

import datetime as dt
import logging
import os
import shutil
import sys
import tempfile
import time

from PySide6.QtCore import (QAbstractListModel, QDir, QEvent, QMimeData, QModelIndex, QObject, QPoint, QRect,
                            QSize, Qt, QThread, QTimer, QUrl, Signal, QStandardPaths)
from PySide6.QtGui import (QAction, QColor, QDesktopServices, QDrag, QFont, QFontMetrics, QPainter,
                           QPainterPath, QPen, QPixmap, QPolygon, QBrush)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox,
                               QFileDialog, QFileSystemModel, QFrame, QHBoxLayout, QLabel, QListView,
                               QMainWindow, QMessageBox, QProgressBar, QPushButton, QSizePolicy, QSlider,
                               QSplitter, QStyle, QStyledItemDelegate, QTreeView, QVBoxLayout, QWidget,
                               QLineEdit, QAbstractSpinBox, QDialog, QStackedWidget)

from . import APP_LONG_NAME, APP_NAME, __version__, settings
from .backup_index import BackupIndex
from .device import FolderBackend
from .jobs import (AuditWorker, BackupWatcher, CaptureDateReader, ConnectAndScan, CopyWorker,
                   LibraryReader, ThumbLoader)
from . import heic
from .preview import PreviewWindow, clock
from .videothumbs import VideoThumbnailer
from .media import TEMPLATES, MediaItem

log = logging.getLogger("transfer")
MIME = "application/x-lptpro-paths"
ACCENT = QColor("#4C9AFF")


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024


def human_time(s: float) -> str:
    s = int(s)
    if s >= 3600:
        return f"{s // 3600}h {s % 3600 // 60}m"
    if s >= 60:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s}s"


def app_data_dir() -> str:
    p = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
    os.makedirs(p, exist_ok=True)
    return p


# ===========================================================================
# Grid model: date headers + items, with our own selection set
# ===========================================================================
class GridModel(QAbstractListModel):
    selection_changed = Signal()

    def __init__(self):
        super().__init__()
        self.items: list[MediaItem] = []
        self.rows: list[tuple] = []       # ("h", label, [items]) or ("i", item)
        self.row_of: dict[str, int] = {}
        self.selected: set[str] = set()
        self.thumbs: dict[str, QPixmap] = {}
        self.requested: set[str] = set()
        self.show = "all"         # all | phone | icloud | missing | fav
        self.current = None       # the item open in the viewer ("you are here" outline)
        self.stars = False        # Album mode: show the favorite star on tiles
        self.waiting = False      # True until it's known which items are in Recently Deleted
        self.hide_live = False
        self.hide_deleted = False
        self.month = 0      # 0 = all months
        self.year = 0       # 0 = all years
        self.kind = "all"
        self.by_path: dict[str, MediaItem] = {}

    def set_items(self, items):
        self.items = items
        self.by_path = {i.path: i for i in items}
        self.selected &= set(self.by_path)
        self.rebuild()

    def visible(self, it: MediaItem) -> bool:
        if self.kind != "all" and it.kind != self.kind:
            return False
        if self.waiting:
            return False
        cloud = bool(it.extra.get("cloud"))
        if self.show == "missing" and (it.backed_up is True or cloud):
            return False
        if self.show == "phone" and cloud:
            return False
        if self.show == "icloud" and not (cloud and it.backed_up is not True):
            return False
        if self.show == "fav" and not it.extra.get("fav"):
            return False
        if self.hide_live and it.extra.get("live"):
            return False
        if self.hide_deleted and it.extra.get("trashed"):
            return False
        if self.year and it.date.year != self.year:
            return False
        if self.month and it.date.month != self.month:
            return False
        return True

    def rebuild(self):
        self.beginResetModel()
        self.rows, self.row_of = [], {}
        cur_day, cur = None, None
        for it in self.items:
            if not self.visible(it):
                continue
            day = it.date.date()
            if day != cur_day:
                cur_day = day
                cur = []
                self.rows.append(("h", f"{it.date:%A, %B} {it.date.day}, {it.date:%Y}", cur))
            cur.append(it)
            self.row_of[it.path] = len(self.rows)
            self.rows.append(("i", it))
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        if role == Qt.UserRole:
            return self.rows[index.row()]
        return None

    def flags(self, index):
        return Qt.ItemIsEnabled

    def refresh_path(self, path):
        r = self.row_of.get(path)
        if r is not None:
            ix = self.index(r)
            self.dataChanged.emit(ix, ix)

    def refresh_all(self):
        if self.rows:
            self.dataChanged.emit(self.index(0), self.index(len(self.rows) - 1))

    # -- selection -------------------------------------------------------
    def toggle(self, paths, on=None):
        for p in paths:
            want = (p not in self.selected) if on is None else on
            (self.selected.add if want else self.selected.discard)(p)
        self.refresh_all()
        self.selection_changed.emit()

    def select_only(self, paths):
        self.selected = set(paths)
        self.refresh_all()
        self.selection_changed.emit()

    def select_visible(self):
        self.selected |= {r[1].path for r in self.rows if r[0] == "i"}
        self.refresh_all()
        self.selection_changed.emit()

    def clear_selection(self):
        self.selected.clear()
        self.refresh_all()
        self.selection_changed.emit()


# ===========================================================================
# Tile painting
# ===========================================================================
class TileDelegate(QStyledItemDelegate):
    def __init__(self, view, model, request_thumb):
        super().__init__(view)
        self.view, self.model, self.request_thumb = view, model, request_thumb
        self.tile = 128

    def star_rect(self, r: QRect) -> QRect:
        """Where the favorite star sits on a tile drawn in rect r (bottom-left corner)."""
        return QRect(r.x() + 9, r.y() + self.tile - 31, 22, 22)

    @staticmethod
    def _star(rect: QRect) -> QPolygon:
        import math
        cx, cy = rect.center().x() + 0.5, rect.center().y() + 0.5
        outer, inner = rect.width() / 2, rect.width() / 4.6
        pts = []
        for k in range(10):
            a = -math.pi / 2 + k * math.pi / 5
            rad = outer if k % 2 == 0 else inner
            pts.append(QPoint(round(cx + rad * math.cos(a)), round(cy + rad * math.sin(a))))
        return QPolygon(pts)

    def sizeHint(self, option, index):
        row = index.data(Qt.UserRole)
        if row[0] == "h":
            return QSize(max(200, self.view.viewport().width() - 12), 44)
        return QSize(self.tile, self.tile + 22)

    def paint(self, p: QPainter, option, index):
        row = index.data(Qt.UserRole)
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        r = option.rect
        if row[0] == "h":
            self._paint_header(p, r, row)
        else:
            self._paint_tile(p, r, row[1])
        p.restore()

    def _paint_header(self, p, r, row):
        _, label, items = row
        f = QFont(self.view.font())
        f.setPointSizeF(f.pointSizeF() + 1.5)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor("#e6e8eb"))
        x = r.x() + 2
        p.drawText(QRect(x, r.y() + 14, r.width(), 24), Qt.AlignLeft | Qt.AlignVCenter, label)
        w = QFontMetrics(f).horizontalAdvance(label)
        small = QFont(self.view.font())
        p.setFont(small)
        p.setPen(QColor("#9aa3ad"))
        cnt = f"{len(items)} item{'s' if len(items) != 1 else ''}"
        p.drawText(QRect(x + w + 12, r.y() + 14, 200, 24), Qt.AlignLeft | Qt.AlignVCenter, cnt)
        cw = QFontMetrics(small).horizontalAdvance(cnt)
        all_sel = all(i.path in self.model.selected for i in items)
        p.setPen(QColor("#8cc0ff"))
        p.drawText(QRect(x + w + 24 + cw, r.y() + 14, 200, 24), Qt.AlignLeft | Qt.AlignVCenter,
                   "Unselect day" if all_sel else "Select day")

    def _paint_tile(self, p, r, it: MediaItem):
        t = self.tile
        box = QRect(r.x() + 3, r.y() + 3, t - 6, t - 6)
        sel = it.path in self.model.selected
        path = QPainterPath()
        path.addRoundedRect(box, 8, 8)

        if it.backed_up is True:
            p.setOpacity(0.5)
        pm = self.model.thumbs.get(it.path)
        if pm is None and it.path not in self.model.requested:
            self.model.requested.add(it.path)
            self.request_thumb(it)
        p.setClipPath(path)
        if pm is not None and not pm.isNull():
            s = pm.size().scaled(box.size(), Qt.KeepAspectRatioByExpanding)
            src_w = pm.width() * box.width() / s.width()
            src_h = pm.height() * box.height() / s.height()
            src = QRect(int((pm.width() - src_w) / 2), int((pm.height() - src_h) / 2), int(src_w), int(src_h))
            p.drawPixmap(box, pm, src)
        else:
            p.fillRect(box, QColor("#2b3038" if it.kind == "photo" else "#33303a"))
            if pm is None:   # still being prepared
                p.setPen(QColor("#6b7480"))
                p.drawText(box.adjusted(0, 0, 0, -6) if it.kind == "photo" else box.adjusted(0, 40, 0, 0),
                           Qt.AlignCenter, "Loading…")
        p.setClipping(False)

        if it.kind == "video":
            c = box.center()
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, 110))
            p.drawEllipse(c, 17, 17)
            p.setBrush(QColor("#ffffff"))
            p.drawPolygon(QPolygon([QPoint(c.x() - 5, c.y() - 8), QPoint(c.x() - 5, c.y() + 8), QPoint(c.x() + 8, c.y())]))
            dur = it.extra.get("duration")
            label = clock(dur) if dur else it.ext.upper().lstrip(".")
            self._badge(p, box, ("LIVE · " + label) if it.extra.get("live") else label, right=True)
        p.setOpacity(1.0)

        if it.extra.get("trashed"):
            # in the iPhone's Recently Deleted album (shown only when not hidden)
            self._tag(p, box, "DELETED · ON PC" if it.backed_up is True else "DELETED",
                      QColor("#3a1616"), QColor("#ff9b9b"))
        elif it.backed_up is True:
            self._tag(p, box, "ON PC · JPEG" if it.extra.get("as_jpeg") else "ON PC",
                      QColor("#0e2b1d"), QColor("#8fe0b5"))
        elif it.extra.get("cloud"):
            # the original isn't on the phone (Optimize iPhone Storage): honest label
            self._tag(p, box, "iCLOUD ONLY", QColor("#0f2440"), QColor("#8cc0ff"))

        # check circle
        cr = QRect(box.right() - 26, box.top() + 6, 20, 20)
        p.setPen(QPen(QColor("#ffffff"), 2))
        p.setBrush(ACCENT if sel else QColor(0, 0, 0, 70))
        p.drawEllipse(cr)
        if self.model.stars:
            star = self._star(self.star_rect(r))
            if it.extra.get("fav"):
                p.setPen(QPen(QColor(0, 0, 0, 120), 1.2))
                p.setBrush(QColor("#ffc94a"))
            else:
                p.setPen(QPen(QColor(255, 255, 255, 170), 1.6))
                p.setBrush(QColor(0, 0, 0, 60))
            p.drawPolygon(star)
        if it.path == self.model.current and not sel:
            p.setPen(QPen(QColor("#ffffff"), 2, Qt.DashLine))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(box.adjusted(-1, -1, 1, 1), 9, 9)
        if sel:
            p.setPen(QPen(QColor("#ffffff"), 2.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.drawPolyline(QPolygon([QPoint(cr.x() + 5, cr.y() + 10), QPoint(cr.x() + 9, cr.y() + 14), QPoint(cr.x() + 15, cr.y() + 6)]))
            p.setPen(QPen(ACCENT, 3))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(box.adjusted(-1, -1, 1, 1), 9, 9)

        p.setPen(QColor("#b8bfc8"))
        fm = QFontMetrics(self.view.font())
        name = fm.elidedText(it.name, Qt.ElideMiddle, t - 8)
        p.drawText(QRect(r.x() + 4, box.bottom() + 4, t - 8, 18), Qt.AlignLeft | Qt.AlignVCenter, name)

    def _badge(self, p, box, text, right=True):
        f = QFont(self.view.font())
        f.setPointSizeF(max(7.0, f.pointSizeF() - 2))
        f.setBold(True)
        p.setFont(f)
        w = QFontMetrics(f).horizontalAdvance(text) + 10
        rr = QRect(box.right() - w - 6, box.bottom() - 22, w, 16)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 150))
        p.drawRoundedRect(rr, 4, 4)
        p.setPen(QColor("#ffffff"))
        p.drawText(rr, Qt.AlignCenter, text)
        p.setFont(self.view.font())

    def _tag(self, p, box, text, bg, fg):
        f = QFont(self.view.font())
        f.setPointSizeF(max(7.0, f.pointSizeF() - 2))
        f.setBold(True)
        p.setFont(f)
        w = QFontMetrics(f).horizontalAdvance(text) + 10
        rr = QRect(box.left() + 6, box.top() + 6, w, 16)
        p.setPen(Qt.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(rr, 4, 4)
        p.setPen(fg)
        p.drawText(rr, Qt.AlignCenter, text)
        p.setFont(self.view.font())


class ShiftController(QObject):
    """Multi-Select with the Shift key.

    * Tap Shift            -> Multi-Select on (sticky). Tap again -> off.
    * Hold Shift and click -> multi-select while held; releasing does NOT flip the mode.
    * Shift used for typing (Shift+letter) or held a while -> doesn't flip the mode either.
    """
    changed = Signal()
    TAP_SECONDS = 0.4

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.sticky = False
        self.held = False
        self._t = 0.0
        self._used = False
        QApplication.instance().installEventFilter(self)

    @property
    def active(self) -> bool:
        return self.sticky or self.held

    def note_click(self):
        if self.held:
            self._used = True

    def set_sticky(self, on: bool):
        if self.sticky != on:
            self.sticky = on
            self.changed.emit()

    def eventFilter(self, obj, e):
        t = e.type()
        if t in (QEvent.KeyPress, QEvent.KeyRelease) and QApplication.activeWindow() is self.window:
            if e.key() == Qt.Key_Shift and not e.isAutoRepeat():
                if isinstance(QApplication.focusWidget(), (QLineEdit, QAbstractSpinBox)):
                    return False   # typing (e.g. renaming a folder)
                if t == QEvent.KeyPress:
                    self.held, self._t, self._used = True, time.monotonic(), False
                else:
                    self.held = False
                    if time.monotonic() - self._t < self.TAP_SECONDS and not self._used:
                        self.sticky = not self.sticky
                self.changed.emit()
            elif t == QEvent.KeyPress and self.held:
                self._used = True   # Shift+other key: not a tap
        elif t == QEvent.ApplicationDeactivate and self.held:
            self.held = False       # released while another app had focus
            self.changed.emit()
        return False


def follow_viewer(model, grid, it):
    """The viewer moved to another item: outline it in the grid and scroll to it. The selection
    follows only when at most one item is selected, so a careful multi-selection is never lost."""
    model.current = it.path
    if len(model.selected) <= 1 and it.path not in model.selected:
        model.select_only([it.path])
    row = model.row_of.get(it.path)
    if row is not None:
        grid.scrollTo(model.index(row), QAbstractItemView.EnsureVisible)
    model.refresh_all()


class GridView(QListView):
    open_item = Signal(object)
    star_clicked = Signal(object)     # Album mode: the favorite star on a tile was clicked

    def __init__(self, model: GridModel):
        super().__init__()
        self.m = model
        self.setModel(model)
        self.setViewMode(QListView.IconMode)
        self.setResizeMode(QListView.Adjust)
        self.setMovement(QListView.Static)
        self.setWrapping(True)
        self.setUniformItemSizes(False)
        self.setLayoutMode(QListView.Batched)
        self.setBatchSize(400)
        self.setSpacing(4)
        self.setSelectionMode(QAbstractItemView.NoSelection)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.verticalScrollBar().setSingleStep(24)
        self.setDragEnabled(True)
        self._press = None
        self._band = None
        self._rubber = None
        self._scroll_timer = QTimer(self, interval=30)
        self._scroll_timer.timeout.connect(self._auto_scroll)
        self.multi = lambda: False        # set by the window (Shift controller)
        self.on_click = lambda: None
        self.allow_drag = True            # drag selected tiles out (to copy them)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.scheduleDelayedItemsLayout()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._press = (e.position().toPoint(), self.indexAt(e.position().toPoint()))
        super().mousePressEvent(e)

    # -- drag: from a SELECTED tile = drag the selection to copy it;
    #          from anywhere else   = draw a box to select (rubber band)
    def _offset(self) -> QPoint:
        return QPoint(self.horizontalOffset(), self.verticalOffset())

    def _starts_on_selected(self, ix) -> bool:
        if not ix.isValid():
            return False
        row = ix.data(Qt.UserRole)
        return row[0] == "i" and row[1].path in self.m.selected

    def mouseMoveEvent(self, e):
        p = e.position().toPoint()
        if self._band:
            self._band["cursor"] = p
            self._update_band()
            return
        if self._press and (e.buttons() & Qt.LeftButton):
            pos, ix = self._press
            if (p - pos).manhattanLength() > QApplication.startDragDistance():
                if self.allow_drag and self._starts_on_selected(ix):
                    self._press = None
                    self._start_drag()
                else:
                    self._begin_band(pos, p)
                return
        super().mouseMoveEvent(e)

    def _begin_band(self, origin, cursor):
        self._press = None
        additive = self.multi()          # Shift held or Multi-Select on: add to what's selected
        off = self._offset()
        # where every tile is, in scroll-independent coordinates (measured once per drag)
        rects = []
        for r, row in enumerate(self.m.rows):
            if row[0] == "i":
                vr = self.visualRect(self.m.index(r)).translated(off)
                rects.append((vr.left(), vr.top(), vr.right(), vr.bottom(), row[1].path))
        self._band = {"origin": origin + off, "cursor": cursor, "rects": rects,
                      "base": set(self.m.selected) if additive else set(), "hit": None}
        if self._rubber is None:
            from PySide6.QtWidgets import QRubberBand
            self._rubber = QRubberBand(QRubberBand.Rectangle, self.viewport())
        self._rubber.show()
        self._scroll_timer.start()
        self.on_click()   # Shift used for this: releasing it shouldn't flip the mode
        self._update_band()

    def _update_band(self):
        b = self._band
        off = self._offset()
        cur = b["cursor"] + off
        rect = QRect(b["origin"], cur).normalized()
        self._rubber.setGeometry(rect.translated(-off).intersected(self.viewport().rect().adjusted(0, 0, 1, 1)))
        l, t, r_, btm = rect.left(), rect.top(), rect.right(), rect.bottom()
        hit = {path for (x0, y0, x1, y1, path) in b["rects"] if x0 <= r_ and x1 >= l and y0 <= btm and y1 >= t}
        if hit != b["hit"]:
            b["hit"] = hit
            self.m.selected = b["base"] | hit
            self.m.refresh_all()
            self.m.selection_changed.emit()

    def _auto_scroll(self):
        """While drawing a box near the top/bottom edge, scroll so it can grow."""
        if not self._band:
            self._scroll_timer.stop()
            return
        y, h = self._band["cursor"].y(), self.viewport().height()
        edge = 40
        step = 0
        if y < edge:
            step = -max(4, (edge - y) // 2)
        elif y > h - edge:
            step = max(4, (y - (h - edge)) // 2)
        if step:
            sb = self.verticalScrollBar()
            sb.setValue(sb.value() + step)
            self._update_band()

    def _end_band(self):
        self._scroll_timer.stop()
        if self._rubber:
            self._rubber.hide()
        self._band = None

    def mouseReleaseEvent(self, e):
        if self._band and e.button() == Qt.LeftButton:
            self._end_band()
            return
        if self._press and e.button() == Qt.LeftButton:
            pos, ix = self._press
            self._press = None
            if not ix.isValid():
                if self.m.selected:
                    self.m.select_only([])                             # click on a blank area
            else:
                self.on_click()
                row = ix.data(Qt.UserRole)
                if row[0] == "i" and self.m.stars and \
                        self.itemDelegate().star_rect(self.visualRect(ix)).adjusted(-3, -3, 3, 3).contains(pos):
                    self.star_clicked.emit(row[1])
                    super().mouseReleaseEvent(e)
                    return
                if row[0] == "h":
                    paths = [i.path for i in row[2]]
                    all_sel = all(p in self.m.selected for p in paths)
                    if self.multi():
                        self.m.toggle(paths, on=not all_sel)          # add/remove this day
                    else:
                        self.m.select_only([] if all_sel and len(self.m.selected) == len(paths) else paths)
                elif self.multi():
                    self.m.toggle([row[1].path])                       # add / remove this one
                else:
                    self.m.select_only([row[1].path])                  # just this one
        super().mouseReleaseEvent(e)

    def mouseDoubleClickEvent(self, e):
        ix = self.indexAt(e.position().toPoint())
        if ix.isValid():
            row = ix.data(Qt.UserRole)
            if row[0] == "i":
                if self.multi():
                    self.m.toggle([row[1].path])  # undo the toggle from the first click
                self.open_item.emit(row[1])

    def _start_drag(self):
        md = QMimeData()
        md.setData(MIME, "\n".join(sorted(self.m.selected)).encode())
        drag = QDrag(self)
        drag.setMimeData(md)
        n = len(self.m.selected)
        pm = QPixmap(150, 34)
        pm.fill(Qt.transparent)
        qp = QPainter(pm)
        qp.setRenderHint(QPainter.Antialiasing)
        qp.setBrush(ACCENT)
        qp.setPen(Qt.NoPen)
        qp.drawRoundedRect(pm.rect(), 8, 8)
        qp.setPen(QColor("#0b1320"))
        f = QFont(self.font())
        f.setBold(True)
        qp.setFont(f)
        qp.drawText(pm.rect(), Qt.AlignCenter, f"Copy {n} item{'s' if n != 1 else ''}")
        qp.end()
        drag.setPixmap(pm)
        drag.exec(Qt.CopyAction)


class FolderTree(QTreeView):
    dropped = Signal(str, list)

    def __init__(self):
        super().__init__()
        self.fs = QFileSystemModel(self)
        self.fs.setFilter(QDir.Dirs | QDir.NoDotAndDotDot)
        self.fs.setNameFilters(["[!.]*"])          # not LPT Pro's own .lptpro bookkeeping folders
        self.fs.setNameFilterDisables(False)
        self.fs.setReadOnly(False)
        self.setModel(self.fs)
        for c in (1, 2, 3):
            self.hideColumn(c)
        self.setHeaderHidden(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setEditTriggers(QAbstractItemView.EditKeyPressed | QAbstractItemView.SelectedClicked)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)

    def _menu(self, pos):
        """Right-click a folder -> Show Folder (opens it in Explorer / the file manager)."""
        from PySide6.QtWidgets import QMenu
        ix = self.indexAt(pos)
        if not ix.isValid():
            return
        path = self.fs.filePath(ix)
        m = QMenu(self)
        m.addAction("Show Folder", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(path)))
        m.exec(self.viewport().mapToGlobal(pos))

    def set_root(self, path):
        self.fs.setRootPath(path)
        self.setRootIndex(self.fs.index(path))
        self.clearSelection()

    def dragEnterEvent(self, e):
        e.acceptProposedAction() if e.mimeData().hasFormat(MIME) else e.ignore()

    def dragMoveEvent(self, e):
        ix = self.indexAt(e.position().toPoint())
        if e.mimeData().hasFormat(MIME) and ix.isValid():
            self.setCurrentIndex(ix)
            e.acceptProposedAction()
        else:
            e.ignore()

    def dropEvent(self, e):
        ix = self.indexAt(e.position().toPoint())
        if ix.isValid() and e.mimeData().hasFormat(MIME):
            paths = bytes(e.mimeData().data(MIME)).decode().split("\n")
            self.dropped.emit(self.fs.filePath(ix), [p for p in paths if p])
            e.acceptProposedAction()


class DropZone(QLabel):
    dropped = Signal(list)

    def __init__(self):
        super().__init__("Drag items here to copy\ninto your backup folder")
        self.setAlignment(Qt.AlignCenter)
        self.setAcceptDrops(True)
        self.setObjectName("dropzone")
        self.setMinimumHeight(84)

    def dragEnterEvent(self, e):
        if e.mimeData().hasFormat(MIME):
            self.setProperty("hot", True)
            self.style().polish(self)
            e.acceptProposedAction()

    def dragLeaveEvent(self, e):
        self.setProperty("hot", False)
        self.style().polish(self)

    def dropEvent(self, e):
        self.setProperty("hot", False)
        self.style().polish(self)
        paths = bytes(e.mimeData().data(MIME)).decode().split("\n")
        self.dropped.emit([p for p in paths if p])
        e.acceptProposedAction()


class OpenWorker(QThread):
    """Double-click on an item that's only on the phone: pull to temp and open."""
    done = Signal(str)

    def __init__(self, device, item, dest):
        super().__init__()
        self.device, self.item, self.dest = device, item, dest

    def run(self):
        try:
            data = self.device.read_all(self.item.path)
            with open(self.dest, "wb") as f:
                f.write(data)
            self.done.emit(self.dest)
        except Exception:
            log.exception("open failed")
            self.done.emit("")


# ===========================================================================
# Main window
# ===========================================================================
def plain_reason(raw: str) -> str:
    """Turn a technical error into something anyone can act on."""
    r = raw.lower()
    if "verification failed" in r or "size mismatch" in r:
        return ("The copy didn’t match the original on the iPhone, so it wasn’t kept. "
                "Try again; this is usually a one-off.")
    if "no_resources" in r:
        return ("The iPhone couldn’t hand this file over (its file sharing was busy). Try again; "
                "if it keeps happening, restart the iPhone.")
    if "object_not_found" in r or "no such file" in r and "dcim" in r:
        return "The file is no longer on the iPhone (it may have been deleted)."
    if "perm_denied" in r:
        return ("The iPhone refused to hand this one over. That happens when its Photos app is still "
                "working on it (an edit, or syncing). Open it once in Photos on the iPhone, or restart "
                "the iPhone, then try again.")
    if "no space" in r or "errno 28" in r or "disk full" in r:
        return "The PC’s drive is full. Free some space, then try again."
    if "permission" in r or "access is denied" in r or "errno 13" in r or "being used by another process" in r:
        return ("Windows didn’t let the app save the file there: the folder may be read-only, or "
                "the file was in use (for example by OneDrive). Try again, or pick another folder.")
    if "disconnected" in r or "not connected" in r:
        return "The iPhone was disconnected. Plug it back in and try again."
    return "Something went wrong copying this file. Try again; if it keeps failing, send the log."


class ProblemsDialog(QDialog):
    """The files that failed, in plain words, with 'Try again' and easy access to the log."""

    def __init__(self, window, problems: dict, log_path: str):
        super().__init__(window)
        self.setWindowTitle("Problems")
        self.resize(820, 420)
        self.window_ = window
        v = QVBoxLayout(self)
        n = len(problems)
        v.addWidget(QLabel(f"{n:,} file{'s' if n != 1 else ''} couldn’t be copied. Nothing half-copied "
                           f"was kept, and nothing on the iPhone was changed.", objectName="auditTitle"))
        from PySide6.QtWidgets import QHeaderView, QTableWidget, QTableWidgetItem
        t = QTableWidget(n, 3, self)
        t.setHorizontalHeaderLabels(["File", "Taken", "What happened"])
        t.verticalHeader().hide()
        t.setEditTriggers(QAbstractItemView.NoEditTriggers)
        t.setSelectionBehavior(QAbstractItemView.SelectRows)
        t.setWordWrap(True)
        for row, (path, (item, raw)) in enumerate(sorted(problems.items(), key=lambda kv: kv[1][0].date, reverse=True)):
            t.setItem(row, 0, QTableWidgetItem(item.name))
            t.setItem(row, 1, QTableWidgetItem(f"{item.date:%Y-%m-%d %H:%M}"))
            cell = QTableWidgetItem(plain_reason(raw))
            cell.setToolTip(raw)              # the technical detail, on hover
            t.setItem(row, 2, cell)
        h = t.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.Stretch)
        QTimer.singleShot(0, t.resizeRowsToContents)   # after the columns have their final widths
        v.addWidget(t, 1)
        row = QHBoxLayout()
        retry = QPushButton("Try these again", objectName="primary")
        retry.clicked.connect(lambda: (self.accept(), window.retry_problems()))
        retry.setEnabled(window.connected and not window.copying)
        open_log = QPushButton("Open the log")
        open_log.setToolTip("Opens lptpro.log, the full technical record (handy to send for help).")
        open_log.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(log_path)))
        show_folder = QPushButton("Show log folder")
        show_folder.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(log_path))))
        clear = QPushButton("Clear list")
        clear.clicked.connect(lambda: (window.clear_problems(), self.accept()))
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        for b in (retry, open_log, show_folder):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(clear)
        row.addWidget(close)
        v.addLayout(row)


class MainWindow(QMainWindow):
    device_lost = Signal()   # emitted from the device thread when the cable is pulled

    def __init__(self, device):
        super().__init__()
        self.device = device
        self.preview: PreviewWindow | None = None
        self.manual_disconnect = False
        self._pending_disconnect = False
        self.device_lost.connect(self.on_device_lost)
        device.on_disconnect = self.device_lost.emit
        self.settings = settings()
        self.data_dir = app_data_dir()
        self.problems: dict = {}          # phone path -> (MediaItem, raw reason)
        self.backup_root = self.settings.value("backup_root", "") or ""
        self.workers: list[QThread] = []
        self.copy_worker: CopyWorker | None = None
        self.audit_worker: AuditWorker | None = None
        self.scan_worker = None
        self.connected = False
        self.local_of: dict[str, str] = {}   # phone path -> PC path, when known

        self.setWindowTitle(APP_NAME)
        self.resize(1400, 880)
        self.model = GridModel()
        self.model.selection_changed.connect(self.update_copy_button)
        # Previews are temporary, for this session only: deleted on unplug / disconnect /
        # close, and prepared again the next time the phone is connected.
        root = os.path.join(tempfile.gettempdir(), "LPTPro-previews")
        shutil.rmtree(root, ignore_errors=True)            # leftovers from a crash
        for old in ("thumbs", "preview-cache"):            # caches kept by test builds
            shutil.rmtree(os.path.join(self.data_dir, old), ignore_errors=True)
        self.session_dir = os.path.join(root, f"session-{os.getpid()}")
        self.thumbs = ThumbLoader(device, os.path.join(self.session_dir, "tiles"))
        self.thumbs.ready.connect(self.on_thumb)
        self.thumbs.note.connect(self.show_note)
        self.thumbs.needs_decode.connect(self._on_needs_decode)
        self.vthumbs = VideoThumbnailer(device, self.thumbs._cache_path, self)
        self.vthumbs.ready.connect(self.on_thumb)
        self.thumbs.prefetch_progress.connect(lambda d, t: self._on_pv_progress("photo", d, t))
        self.vthumbs.prefetch_progress.connect(lambda d, t: self._on_pv_progress("video", d, t))
        self.thumbs.start()
        self._video_playing = False
        self._health = QTimer(self, interval=15000)
        self._health.timeout.connect(self._log_health)
        self._health.start()
        self.watcher = BackupWatcher(device)
        self.watcher.results.connect(self.on_watch_results)
        self.watcher.start()

        self._build_ui()
        self.model.hide_live = self.hide_live.isChecked()
        self.shift = ShiftController(self)
        self.shift.changed.connect(self._render_multi)
        self.grid.multi = lambda: self.shift.active
        self.grid.on_click = self.shift.note_click
        self._apply_backup_root()
        self.retry_timer = QTimer(self)
        self.retry_timer.setInterval(5000)
        self.retry_timer.timeout.connect(self._auto_retry)
        QTimer.singleShot(150, self.connect_device)

    # -- layout -------------------------------------------------------------
    def _build_ui(self):
        root = QWidget()
        v = QVBoxLayout(root)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # top bar
        top = QFrame(objectName="topbar")
        th = QHBoxLayout(top)
        th.setContentsMargins(16, 10, 16, 10)
        title = QLabel(APP_NAME, objectName="title")
        title.setToolTip(APP_LONG_NAME)
        version = QLabel(f"v{__version__}", objectName="version")
        version.setToolTip(f"{APP_LONG_NAME}, version {__version__}")
        self.device_label = QLabel("Looking for iPhone…", objectName="device")
        self.retry_btn = QPushButton("Retry")
        self.retry_btn.clicked.connect(self.connect_device)
        self.retry_btn.hide()
        self.disconnect_btn = QPushButton("Disconnect")
        self.disconnect_btn.setToolTip("Stop talking to the iPhone so it's safe to unplug")
        self.disconnect_btn.clicked.connect(lambda: self.disconnect_device())
        self.disconnect_btn.hide()
        self.backup_btn = QPushButton("Choose backup folder…")
        self.backup_btn.clicked.connect(self.choose_backup_root)
        logo = QLabel()
        _ico = QApplication.windowIcon()
        if not _ico.isNull():
            logo.setPixmap(_ico.pixmap(26, 26))
            th.addWidget(logo)
            th.addSpacing(6)
        th.addWidget(title)
        th.addSpacing(6)
        th.addWidget(version, 0, Qt.AlignBottom)
        th.addSpacing(16)
        th.addWidget(self.device_label)
        th.addWidget(self.retry_btn)
        th.addWidget(self.disconnect_btn)
        th.addSpacing(12)
        self.mode_group = QButtonGroup(self)
        for label, key, tip in (("iPhone", "phone", "What\u2019s on the iPhone, and backing it up"),
                                ("Album", "album", "Browse your backup on this PC (no iPhone needed)")):
            b = QPushButton(label, objectName="chip")
            b.setCheckable(True)
            b.setChecked(key == "phone")
            b.setProperty("mode", key)
            b.setToolTip(tip)
            self.mode_group.addButton(b)
            th.addWidget(b)
        self.mode_group.buttonClicked.connect(lambda b: self.switch_mode(b.property("mode")))
        self.album = None
        self.problems_btn = QPushButton("", objectName="problems")
        self.problems_btn.setToolTip("Files that couldn’t be copied, and why")
        self.problems_btn.clicked.connect(self.show_problems)
        self.problems_btn.hide()
        th.addSpacing(8)
        th.addWidget(self.problems_btn)
        th.addStretch(1)
        th.addWidget(QLabel("Backup folder:", objectName="muted"))
        th.addWidget(self.backup_btn)
        v.addWidget(top)

        # audit banner
        self.audit = QFrame(objectName="audit")
        ah = QHBoxLayout(self.audit)
        ah.setContentsMargins(16, 10, 16, 10)
        col = QVBoxLayout()
        self.audit_title = QLabel("Backup check", objectName="auditTitle")
        self.audit_bar = QProgressBar(objectName="auditBar")
        self.audit_bar.setTextVisible(False)
        self.audit_bar.setFixedHeight(8)
        self.audit_text = QLabel("", objectName="muted")
        col.addWidget(self.audit_title)
        col.addWidget(self.audit_bar)
        col.addWidget(self.audit_text)
        ah.addLayout(col, 1)
        self.add_btn = QPushButton("Add downloaded photos\u2026")
        self.add_btn.setToolTip("Photos downloaded from iCloud, or from an old phone or camera: LPT Pro copies\n"
                                "them into your backup folder, sorted by the date each was taken, and skips\n"
                                "any that are already there. The downloaded files aren\u2019t changed.")
        self.add_btn.clicked.connect(self.add_downloaded)
        self.library_missing = []          # library items whose originals aren't files on this iPhone
        self.recheck_btn = QPushButton("Recheck")
        self.recheck_btn.clicked.connect(self.start_audit)
        self.backup_missing_btn = QPushButton("Back up what’s missing", objectName="orange")
        self.backup_missing_btn.clicked.connect(self.backup_missing)
        self.backup_missing_btn.setEnabled(False)
        ah.addWidget(self.recheck_btn)
        ah.addWidget(self.add_btn)
        ah.addWidget(self.backup_missing_btn)
        phone_page = QWidget()
        pl = QVBoxLayout(phone_page)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(0)
        self.pages = QStackedWidget()
        self.pages.addWidget(phone_page)
        pl.addWidget(self.audit)

        # filter row
        bar = QFrame(objectName="filterbar")
        rows = QVBoxLayout(bar)
        rows.setContentsMargins(16, 8, 16, 6)
        rows.setSpacing(6)
        fh = QHBoxLayout()
        sh = QHBoxLayout()            # second row: the show / hide choices
        rows.addLayout(fh)
        rows.addLayout(sh)
        self.show_box = QComboBox()
        for label, key in (("Everything", "all"), ("On this iPhone", "phone"),
                           ("Only in iCloud", "icloud"), ("Not backed up yet", "missing")):
            self.show_box.addItem(label, key)
        self.show_box.setToolTip("Everything: all items.\n"
                                 "On this iPhone: items stored on the phone (these can be backed up).\n"
                                 "Only in iCloud: items whose originals are only in iCloud (bring these down from iCloud).\n"
                                 "Not backed up yet: items on the phone that aren\u2019t in your backup folder.")
        self.show_box.currentIndexChanged.connect(self.on_filter)
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
        # quick jump: month + year
        import calendar
        self.month_box = QComboBox()
        self.month_box.addItem("All months", 0)
        for m in range(1, 13):
            self.month_box.addItem(f"{m} - {calendar.month_name[m]}", m)
        self.month_box.setToolTip("Show one month")
        self.year_box = QComboBox()
        self.year_box.addItem("All years", 0)
        self.year_box.setToolTip("Show one year")
        self.month_box.currentIndexChanged.connect(self.on_filter)
        self.year_box.currentIndexChanged.connect(self.on_filter)
        # same width for both, wide enough for "2026  (12,345)"
        w = max(self.month_box.fontMetrics().horizontalAdvance(t) for t in ("12 - September", "2026  (12,345)")) + 48
        for box in (self.month_box, self.year_box):
            box.setMinimumWidth(w)
            box.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        fh.addWidget(self.month_box)
        fh.addWidget(self.year_box)
        sh.addWidget(QLabel("Show", objectName="muted"))
        sh.addWidget(self.show_box)
        sh.addSpacing(16)
        self.hide_live = QCheckBox("Hide Live Photo videos")
        self.hide_live.setToolTip("The short video half of a Live Photo (same name as a photo, e.g.\n"
                                  "IMG_1234.HEIC + IMG_1234.MOV). They're still copied with their photo\n"
                                  "when \u201cKeep Live Photo pairs together\u201d is on.")
        self.hide_live.setChecked(self.settings.value("hide_live", False, type=bool))
        self.hide_live.toggled.connect(lambda v: (self.settings.setValue("hide_live", v), self.on_filter()))
        sh.addWidget(self.hide_live)
        sh.addSpacing(16)
        self.hide_deleted = QCheckBox("Hide Recently Deleted")
        self.hide_deleted.setToolTip("Photos and videos you deleted on the iPhone stay in its \u201cRecently Deleted\u201d\n"
                                     "album for about 30 days. They're hidden here while this is ticked.")
        self.hide_deleted.setChecked(self.settings.value("hide_deleted", True, type=bool))
        self.hide_deleted.toggled.connect(lambda v: (self.settings.setValue("hide_deleted", v), self.on_filter(),
                                                     self._render_icloud_count()))
        sh.addWidget(self.hide_deleted)
        sh.addSpacing(16)
        sh.addStretch(1)
        fh.addStretch(1)
        fh.addWidget(QLabel("Size", objectName="muted"))
        self.size_slider = QSlider(Qt.Horizontal)
        self.size_slider.setRange(96, 260)
        self.size_slider.setValue(int(self.settings.value("tile", 140)))
        self.size_slider.setFixedWidth(120)
        self.size_slider.valueChanged.connect(self.on_tile_size)
        fh.addWidget(self.size_slider)
        sel_all = QPushButton("Select all")
        sel_all.clicked.connect(self.model.select_visible)
        clear = QPushButton("Clear")
        clear.clicked.connect(self.model.clear_selection)
        fh.addWidget(sel_all)
        fh.addWidget(clear)
        pl.addWidget(bar)

        # grid + right panel
        split = QSplitter(Qt.Horizontal)
        self.grid = GridView(self.model)
        self.delegate = TileDelegate(self.grid, self.model, self.request_thumb)
        self.delegate.tile = self.size_slider.value()
        self.grid.setItemDelegate(self.delegate)
        self.grid.open_item.connect(self.open_item)
        self.empty_label = QLabel("", objectName="empty")
        self.empty_label.setAlignment(Qt.AlignCenter)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(12, 0, 0, 0)
        lv.addWidget(self.grid)
        lv.addWidget(self.empty_label)
        # first-time setup card (what to install / start, per operating system)
        self.setup_card = QFrame(objectName="setupCard")
        sc = QVBoxLayout(self.setup_card)
        sc.setContentsMargins(28, 24, 28, 24)
        sc.setSpacing(10)
        self.setup_title = QLabel("", objectName="auditTitle")
        self.setup_text = QLabel("", objectName="muted")
        self.setup_text.setWordWrap(True)
        self.setup_buttons = QHBoxLayout()
        self.setup_note = QLabel("The app keeps checking and connects by itself.", objectName="muted")
        sc.addWidget(self.setup_title)
        sc.addWidget(self.setup_text)
        sc.addLayout(self.setup_buttons)
        sc.addWidget(self.setup_note)
        self.setup_card.setMaximumWidth(620)
        self.setup_card.hide()
        lv.addWidget(self.setup_card, 0, Qt.AlignCenter)
        split.addWidget(left)

        panel = QFrame(objectName="panel")
        pv = QVBoxLayout(panel)
        pv.setContentsMargins(16, 16, 16, 16)
        pv.setSpacing(10)
        pv.addWidget(QLabel("COPY TO", objectName="section"))
        self.dest_label = QLabel("", objectName="dest")
        self.dest_label.setWordWrap(True)
        pv.addWidget(self.dest_label)
        self.drop = DropZone()
        self.drop.dropped.connect(lambda paths: self.start_copy(paths, self.backup_root))
        pv.addWidget(self.drop)

        fl = QHBoxLayout()
        fl.addWidget(QLabel("FOLDERS", objectName="section"))
        fl.addStretch(1)
        nf = QPushButton("+ New folder")
        nf.clicked.connect(self.new_folder)
        fl.addWidget(nf)
        pv.addLayout(fl)
        self.tree = FolderTree()
        self.tree.dropped.connect(lambda dest, paths: self.start_copy(paths, dest, template="No subfolders"))
        pv.addWidget(self.tree, 1)
        hint = QLabel("Drop on a folder to copy straight into it.", objectName="muted")
        hint.setWordWrap(True)
        pv.addWidget(hint)

        pv.addWidget(QLabel("ORGANIZE INTO", objectName="section"))
        self.template = QComboBox()
        self.template.addItems(list(TEMPLATES))
        self.template.setCurrentText(self.settings.value("template", "Year / Month"))
        self.template.currentTextChanged.connect(lambda t: self.settings.setValue("template", t))
        pv.addWidget(self.template)

        self.opt_dates = QCheckBox("Set file dates from capture date")
        self.opt_pairs = QCheckBox("Keep Live Photo pairs together")
        self.opt_skip = QCheckBox("Skip items already backed up")
        self.opt_skip_deleted = QCheckBox("Skip Recently Deleted")
        self.opt_skip_deleted.setToolTip("\u201cBack up what\u2019s missing\u201d leaves out photos and videos that are in the\n"
                                         "iPhone's Recently Deleted album. Items you select yourself are always copied.")
        self.opt_jpeg = QCheckBox("Save HEIC photos as JPEG")
        self.opt_keep_heic = QCheckBox("   …and keep the original HEIC too")
        self.opt_keep_heic.setToolTip("The JPEG is for everyday use. The HEIC is the untouched original "
                                      "from the phone (full quality, HDR, depth).")
        for cb, key, default in ((self.opt_dates, "opt_dates", True), (self.opt_pairs, "opt_pairs", True),
                                 (self.opt_skip, "opt_skip", True), (self.opt_skip_deleted, "opt_skip_deleted", True),
                                 (self.opt_jpeg, "opt_jpeg", True), (self.opt_keep_heic, "opt_keep_heic", False)):
            cb.setChecked(self.settings.value(key, default, type=bool))
            cb.toggled.connect(lambda val, k=key: self.settings.setValue(k, val))
            pv.addWidget(cb)
        self.opt_keep_heic.setEnabled(self.opt_jpeg.isChecked())
        self.opt_jpeg.toggled.connect(self.opt_keep_heic.setEnabled)
        self.opt_skip_deleted.toggled.connect(
            lambda *_: self.update_audit_summary(final=not (self.audit_worker and self.audit_worker.isRunning())))

        self.copy_btn = QPushButton("Select items to copy", objectName="primary")
        self.copy_btn.setMinimumHeight(44)
        self.copy_btn.clicked.connect(lambda: self.start_copy(sorted(self.model.selected), self.backup_root))
        pv.addWidget(self.copy_btn)
        panel.setMinimumWidth(290)
        panel.setMaximumWidth(380)
        split.addWidget(panel)
        split.setStretchFactor(0, 1)
        pl.addWidget(split, 1)
        v.addWidget(self.pages, 1)

        # status bar
        sb = QFrame(objectName="statusbar")
        sh = QHBoxLayout(sb)
        sh.setContentsMargins(16, 8, 16, 8)
        self.status_label = QLabel("")
        self.progress = QProgressBar(objectName="copyBar")
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(8)
        self.progress.hide()
        self.speed_label = QLabel("", objectName="muted")
        self.pause_btn = QPushButton("Pause")
        self.pause_btn.clicked.connect(self.toggle_pause)
        self.cancel_btn = QPushButton("Cancel", objectName="danger")
        self.cancel_btn.clicked.connect(self.cancel_copy)
        self.pause_btn.hide()
        self.cancel_btn.hide()
        self.free_label = QLabel("", objectName="muted")
        # background preview preparation, with progress in the status bar
        self.pv_label = QLabel("", objectName="muted")
        self.pv_bar = QProgressBar(objectName="previewBar")
        self.pv_bar.setTextVisible(False)
        self.pv_bar.setFixedSize(140, 6)
        self.pv_label.hide()
        self.pv_bar.hide()
        self._pv = {"photo": (0, 0), "video": (0, 0)}
        self.dates_label = QLabel("", objectName="muted")
        self.dates_label.setToolTip("Reading the date each photo and video was taken, so days and years "
                                    "match reality (the iPhone's own file dates can be much later).")
        self.dates_label.hide()
        self.dates_reader = None
        self.deleted_reader = None
        self._after_library_started = False
        self._scan_no = 0
        self._pv_tick = QTimer(self, interval=500)     # keep the count fresh while loading
        self._pv_tick.timeout.connect(lambda: self._render_pv())
        self._pv_hide = QTimer(self, singleShot=True, interval=4000)
        self._pv_hide.timeout.connect(lambda: (self.pv_label.hide(), self.pv_bar.hide()))
        self.multi_label = QLabel("", objectName="multi")
        self.multi_label.setToolTip("Tap Shift to turn Multi-Select on or off.\n"
                                    "Or hold Shift while clicking.")
        self.multi_label.hide()
        sh.addWidget(self.multi_label)
        sh.addWidget(self.status_label)
        sh.addWidget(self.progress, 1)
        sh.addWidget(self.speed_label)
        sh.addWidget(self.pause_btn)
        sh.addWidget(self.cancel_btn)
        sh.addStretch(0)
        sh.addWidget(self.dates_label)
        sh.addSpacing(12)
        sh.addWidget(self.pv_label)
        sh.addWidget(self.pv_bar)
        sh.addSpacing(16)
        sh.addWidget(self.free_label)
        v.addWidget(sb)
        self.setCentralWidget(root)
        self._show_empty("Plug in your iPhone and unlock it.")
        self.update_copy_button()

    # -- helpers ------------------------------------------------------------
    def _show_empty(self, text):
        self.empty_label.setText(text)
        self.empty_label.setVisible(bool(text))
        self.grid.setVisible(not text)

    def _apply_backup_root(self):
        if getattr(self, "album", None) is not None:
            self.album.stale = True                  # the backup changed: look again next time
            if self.pages.currentWidget() is self.album:
                self.album.load()
        if self.backup_root and os.path.isdir(self.backup_root):
            self.backup_btn.setText(self.backup_root)
            self.dest_label.setText(self.backup_root)
            self.tree.set_root(self.backup_root)
            try:
                du = shutil.disk_usage(self.backup_root)
                self.free_label.setText(f"PC: {human_bytes(du.free)} free of {human_bytes(du.total)}")
            except OSError:
                pass
        else:
            self.backup_btn.setText("Choose backup folder…")
            self.dest_label.setText("No backup folder chosen yet.")

    def choose_backup_root(self):
        start = self.backup_root or QStandardPaths.writableLocation(QStandardPaths.PicturesLocation)
        d = QFileDialog.getExistingDirectory(self, "Where should your iPhone backup go?", start)
        if d:
            self.backup_root = os.path.normpath(d)
            self.settings.setValue("backup_root", self.backup_root)
            self._apply_backup_root()
            self.watcher.reset()
            self.local_of.clear()
            for it in self.model.items:
                it.backed_up = None
                it.extra.pop("as_jpeg", None)
            self.rebuild_grid()
            self.start_audit()

    def new_folder(self):
        ix = self.tree.currentIndex()
        base = self.tree.fs.filePath(ix) if ix.isValid() else self.backup_root
        if not base or not os.path.isdir(base):
            QMessageBox.information(self, "New folder", "Choose a backup folder first.")
            return
        name, k = "New folder", 2
        while os.path.exists(os.path.join(base, name)):
            name = f"New folder ({k})"
            k += 1
        new_ix = self.tree.fs.mkdir(self.tree.fs.index(base), name)
        if new_ix.isValid():
            if base != self.backup_root:
                self.tree.expand(self.tree.fs.index(base))
            self.tree.setCurrentIndex(new_ix)
            self.tree.edit(new_ix)

    def set_status(self, text):
        self.status_label.setText(text)

    # -- device ---------------------------------------------------------------
    def connect_device(self, quiet=False):
        """quiet: a background check (every few seconds while no iPhone is connected); the
        screen only changes once an iPhone actually answers."""
        if self.scan_worker and self.scan_worker.isRunning():
            return
        # quick local check first: if the iPhone USB service isn't there, say what to do
        # right away instead of waiting for a connection that can't happen
        from .device import AfcBackend
        if isinstance(self.device, AfcBackend):
            from . import setup_check
            steps = setup_check.check()
            if steps:
                self.on_connect_failed(steps[0].title)
                return
        self.manual_disconnect = False
        self._quiet = quiet
        if not quiet:
            self.retry_btn.hide()
            self.device_label.setText("Connecting…")
        w = ConnectAndScan(self.device)
        if quiet:
            w.status.connect(lambda m: None if m.startswith("Connecting") else self.set_status(m))
        else:
            w.status.connect(self.set_status)
        w.connected.connect(self.on_connected)
        w.failed.connect(self.on_connect_failed)
        w.scanned.connect(self.on_scanned)
        self.scan_worker = w
        w.start()

    def _auto_retry(self):
        if not self.connected and not self.manual_disconnect:
            self.connect_device(quiet=True)

    def on_connected(self, label):
        self._last_fail = None
        self.setup_card.hide()
        self.connected = True
        self.retry_timer.stop()
        self.device_label.setText(f"●  {label}")
        self.device_label.setProperty("ok", True)
        self.device_label.style().polish(self.device_label)
        self.device_name = label.split(" · ")[0]
        self.retry_btn.hide()
        self.disconnect_btn.show()
        # tiles that came up empty while the phone was away: ask again
        for p, pm in list(self.model.thumbs.items()):
            if pm.isNull():
                self.model.thumbs.pop(p, None)
                self.model.requested.discard(p)
        self.model.requested -= {p for p in self.model.requested if p not in self.model.thumbs}
        self.model.refresh_all()
        self.update_copy_button()

    def _show_setup(self, step):
        """Show a setup step (title, explanation, buttons) in place of the photos."""
        from . import setup_check  # noqa: F401
        self.setup_title.setText(step.title)
        self.setup_text.setText(step.text)
        while self.setup_buttons.count():
            w = self.setup_buttons.takeAt(0).widget()
            if w:
                w.hide()
                w.setParent(None)      # gone at once (not just later)
                w.deleteLater()
        for label, kind, payload in step.actions:
            b = QPushButton(label, objectName="primary" if kind in ("url", "run") else "")
            b.clicked.connect(lambda _=False, k=kind, p=payload, btn=b: self._setup_action(k, p, btn))
            self.setup_buttons.addWidget(b)
        self.setup_buttons.addStretch(1)
        self.empty_label.hide()
        self.grid.hide()
        self.setup_card.show()

    def _setup_action(self, kind, payload, button):
        if kind == "copy":
            QApplication.clipboard().setText(payload)
            button.setText("Copied ✓")
        elif kind == "url":
            QDesktopServices.openUrl(QUrl(payload))
        elif kind == "run":
            import subprocess
            try:
                subprocess.Popen(payload)          # e.g. pkexec asks for the password itself
                button.setText("Starting…")
                QTimer.singleShot(4000, self.connect_device)
            except Exception as e:
                button.setText("Couldn’t start it")
                log.warning("setup action %s failed: %s", payload, e)

    def on_connect_failed(self, msg):
        from . import setup_check
        from .device import AfcBackend
        steps = setup_check.check() if isinstance(self.device, AfcBackend) else []
        if steps:
            log.info("setup needed: %s", steps[0].title)
        elif "Trust" in msg:
            steps = [setup_check.trust_step()]
        self.connected = False
        if getattr(self, "_quiet", False):
            # a background check found nothing new: leave the screen exactly as it is
            title = steps[0].title if steps else msg
            if title == getattr(self, "_last_fail", None):
                self.retry_timer.start()
                return
        self._last_fail = steps[0].title if steps else msg
        self.device_label.setText("iPhone is disconnected" if "disconnected" in msg else "Not connected")
        self.device_label.setProperty("ok", False)
        self.device_label.style().polish(self.device_label)
        self.disconnect_btn.hide()
        self.retry_btn.setText("Retry")
        self.retry_btn.show()
        if steps:
            self._show_setup(steps[0])
            self.set_status(steps[0].title)
            self.retry_timer.start()     # keeps checking; connects as soon as it can
            return
        self.setup_card.hide()
        self._show_empty(msg)
        self.set_status(msg)
        if "disconnected" in msg or "Trust" in msg:
            self.retry_timer.start()

    def _fill_years(self, items):
        """Year list = every year on the phone (newest first); default = this year."""
        years = sorted({i.date.year for i in items}, reverse=True)
        keep = self.year_box.currentData() if self.year_box.count() > 1 else None
        self.year_box.blockSignals(True)
        self.year_box.clear()
        self.year_box.addItem("All years", 0)
        for y in years:
            n = sum(1 for i in items if i.date.year == y)
            self.year_box.addItem(f"{y}  ({n:,})", y)
        want = keep if keep in years or keep == 0 else (
            dt.date.today().year if dt.date.today().year in years else (years[0] if years else 0))
        self.year_box.setCurrentIndex(max(0, self.year_box.findData(want)))
        self.year_box.blockSignals(False)
        self.model.year = self.year_box.currentData() or 0

    def on_scanned(self, items):
        self._fill_years(items)
        # fresh connection: previews are prepared again (the old temp ones were deleted)
        self.model.thumbs.clear()
        self.model.requested.clear()
        n_photo = sum(1 for i in items if i.kind == "photo")
        # Until it's known which items are in Recently Deleted, show nothing (when they're to be
        # hidden), so a deleted photo never flashes up on screen.
        self.model.waiting = bool(items) and self.hide_deleted.isChecked()
        self.model.set_items(items)
        self.rebuild_grid()
        if not items:
            self._show_empty("No photos or videos found on this iPhone.")
        self.set_status(f"{len(items):,} items on iPhone · {n_photo:,} photos · {len(items) - n_photo:,} videos")
        self.start_audit()
        self._after_library_started = False
        self._scan_no += 1
        self._start_deleted()          # first: the previews and dates wait for it (see _after_library)
        no = self._scan_no             # safety net if the phone never answers
        QTimer.singleShot(180_000, lambda: no == self._scan_no and self._after_library())

    # -- audit ----------------------------------------------------------------
    def make_index(self):
        if not hasattr(self, "_index") or self._index.roots != [os.path.abspath(self.backup_root)]:
            self._index = BackupIndex([self.backup_root], os.path.join(self.data_dir, "fingerprints.json"))
            self.watcher.reset()
            self.watcher.set_index(self._index)
        return self._index

    def start_audit(self):
        if not self.model.items:
            return
        if not self.backup_root or not os.path.isdir(self.backup_root):
            self.audit_title.setText("Choose a backup folder to see what’s already backed up")
            self.audit_text.setText("Pick the folder where your iPhone photos live (or should live) on this PC.")
            return
        if self.audit_worker and self.audit_worker.isRunning():
            self.audit_worker.cancel()
            self.audit_worker.wait(3000)
        self.audit_title.setText("Checking what’s already backed up…")
        w = AuditWorker(self.device, self.make_index(), self.model.items)
        w.status.connect(self.audit_text.setText)
        w.results.connect(self.on_audit_results)
        w.finished_ok.connect(self.on_audit_done)
        self.audit_worker = w
        w.start()
        self.update_audit_summary()   # the backup button is usable straight away

    def on_audit_results(self, batch):
        for path, match in batch:
            it = self.model.by_path.get(path)
            if it:
                local = self.local_of.get(path)
                if match:
                    self._mark_on_pc(it, match)
                elif it.backed_up is True and local and os.path.exists(local):
                    pass   # copied meanwhile (the check looked before the copy got there)
                else:
                    self._mark_not_on_pc(it)
                self.model.refresh_path(path)
        self.update_audit_summary()

    def on_audit_done(self):
        self.update_audit_summary(final=True)
        self._match_library_items()
        self._run_extra_audit()
        if self.model.show != "all":
            self.rebuild_grid()

    def _skips_deleted(self, it) -> bool:
        return bool(it.extra.get("trashed")) and it.backed_up is not True and self.opt_skip_deleted.isChecked()

    def update_audit_summary(self, final=False):
        trashed = [i for i in self.model.items if self._skips_deleted(i)]
        items = [i for i in self.model.items if not self._skips_deleted(i)]
        done = sum(1 for i in items if i.backed_up is True)
        cloud = [i for i in items if i.backed_up is not True and i.extra.get("cloud")]
        missing = [i for i in items if i.backed_up is False and not i.extra.get("cloud")]
        lib_only = 0      # library items are in the grid now (as iCloud-only items)
        checked = done + len(missing) + sum(1 for i in cloud if i.backed_up is False)
        self.audit_bar.setMaximum(max(1, len(items)))
        self.audit_bar.setValue(done)
        if final or checked == len(items):
            n_icloud = len(cloud) + lib_only
            except_ = [f"{n_icloud:,} only in iCloud"] if n_icloud else []
            if trashed:
                except_.append(f"{len(trashed):,} in Recently Deleted")
            if missing:
                self.audit_title.setText(f"{len(missing):,} items are not backed up yet")
            elif except_:
                self.audit_title.setText("Everything on this iPhone is backed up ✓ "
                                         f"(except {' and '.join(except_)})")
            else:
                self.audit_title.setText("Everything on this iPhone is backed up ✓")
            self.audit_text.setText(f"{done:,} backed up · {len(missing):,} not backed up"
                                    + (f" · {len(cloud) + lib_only:,} only in iCloud (not stored on this iPhone)"
                                       if cloud or lib_only else "")
                                    + (f" · {len(trashed):,} in Recently Deleted (skipped)" if trashed else "")
                                    + " · matched by content, so renamed or re-sorted files still count")
        # Usable while the check is still running: the copy re-checks every item against the
        # backup folder right before copying it and skips what's already there.
        todo = [i for i in items if i.backed_up is not True and not i.extra.get("cloud")]
        unchecked = len(items) - checked
        checking = bool(self.audit_worker and self.audit_worker.isRunning()) and unchecked > 0
        self.backup_missing_btn.setEnabled(bool(todo) and not self.copying and self.connected)
        self.add_btn.setEnabled(not self.copying)
        if not todo:
            self.backup_missing_btn.setText("All backed up")
            self.backup_missing_btn.setToolTip("")
        elif checking:
            self.backup_missing_btn.setText(f"Back up what’s missing · still checking {unchecked:,}")
            self.backup_missing_btn.setToolTip(
                "You can start now. Items already in your backup folder are skipped\n"
                "as the copy reaches them, so nothing is copied twice.")
        else:
            self.backup_missing_btn.setText(f"Back up the {len(missing):,} missing · "
                                            f"{human_bytes(sum(i.size for i in missing))}")
            self.backup_missing_btn.setToolTip("")
        self._render_icloud_count()

    # -- grid -----------------------------------------------------------------
    def on_filter(self, *_):
        b = self.kind_group.checkedButton()
        self.model.kind = b.property("kind") if b else "all"
        self.model.show = self.show_box.currentData() or "all"
        self.model.hide_live = self.hide_live.isChecked()
        self.model.hide_deleted = self.hide_deleted.isChecked()
        self.model.month = self.month_box.currentData() or 0
        self.model.year = self.year_box.currentData() or 0
        self.hide_live.setVisible(self.model.kind != "photo")
        self.rebuild_grid()

    def rebuild_grid(self):
        self.model.rebuild()
        if not self.model.items:
            return
        if self.model.waiting:
            self._show_empty("Getting ready\u2026  (checking which photos are in Recently Deleted)")
        elif not self.model.rows:
            self._show_empty({"missing": "Everything on this iPhone is backed up \u2713",
                              "icloud": "Nothing here is only in iCloud \u2713"}.get(
                                  self.model.show, "Nothing matches this filter."))
        else:
            self._show_empty("")

    def on_tile_size(self, v):
        self.delegate.tile = v
        self.settings.setValue("tile", v)
        self.grid.scheduleDelayedItemsLayout()

    def request_thumb(self, it: MediaItem):
        # Every tile first tries the iPhone's own thumbnail; videos without one are handed
        # to the video tile maker (needs_decode) by the worker.
        self.thumbs.request(it)

    def _on_needs_decode(self, it, wanted):
        if wanted:
            self.vthumbs.request(it)
        else:
            self.vthumbs.add_prefetch(it)

    # -- Recently Deleted -------------------------------------------------------------
    def _start_deleted(self):
        if self.deleted_reader is not None:
            try:
                self.deleted_reader.done.disconnect()
            except (RuntimeError, TypeError):
                pass
        self.hide_deleted.setText("Hide Recently Deleted")
        self.hide_deleted.setEnabled(True)
        self.library_missing = []
        r = LibraryReader(self.device, [i.path for i in self.model.items])
        r.done.connect(self._on_deleted)
        self.deleted_reader = r
        r.start()

    def _on_deleted(self, lib, missing, reason):
        if self.sender() is not self.deleted_reader:
            return                                   # from an earlier connection
        paths = lib.trashed if lib is not None else None
        on_phone, cloud = missing if lib is not None else ([], [])
        self._add_library_items(cloud, on_phone)
        for it in self.model.items:
            it.extra.pop("trashed", None)
        if paths is None:
            note = (f"Couldn\u2019t read the iPhone\u2019s Recently Deleted list ({reason}), "
                    "so nothing is treated as deleted.")
            self.opt_skip_deleted.setToolTip(note)
            self.hide_deleted.setToolTip(note)
            self.hide_deleted.setEnabled(False)
        else:
            for it in self.model.items:
                photo = it.extra.get("live_photo")   # a Live Photo's video goes with its photo
                if it.path.upper() in paths or (photo and photo.upper() in paths):
                    it.extra["trashed"] = True
        n = sum(1 for i in self.model.items if i.extra.get("trashed"))
        if paths is not None:
            self.hide_deleted.setText(f"Hide Recently Deleted ({n:,})")
        self._render_icloud_count()
        self._after_library()

    def _after_library(self):
        """The Photos library has been read (or couldn't be): show the grid, then prepare
        previews and dates, which had waited so the library read went first."""
        self.model.waiting = False
        self.on_filter()
        self.update_audit_summary(final=not (self.audit_worker and self.audit_worker.isRunning()))
        if self._after_library_started or not self.model.items:
            return
        self._after_library_started = True
        self._start_prefetch(self.model.items)
        self._start_dates([i for i in self.model.items if not i.extra.get("library_only")])

    def _add_library_items(self, assets, on_phone=()):
        """Items in the Photos library outside the camera roll. Those whose original is stored on
        the iPhone are backed up like any other; the rest are shown with the iPhone's preview and
        tagged iCLOUD ONLY (they can't be copied from the phone)."""
        from .media import kind_of
        if not assets and not on_phone:
            return
        known = {i.path.upper() for i in self.model.items}
        new, stored = [], []
        for a, st in on_phone:
            if a.path in known:
                continue
            name = a.original_name or a.name
            kind = a.kind or kind_of(name) or "photo"
            it = MediaItem(path=a.phone_path, name=name, size=st.size, date=a.date or st.mtime, kind=kind)
            it.extra["library"] = True
            stored.append(it)
        new += stored
        for a in assets:
            if a.path in known:
                continue
            name = a.original_name or a.name
            kind = a.kind or kind_of(name) or "photo"
            it = MediaItem(path=a.phone_path, name=name, size=a.original_size,
                           date=a.date or dt.datetime(2000, 1, 1), kind=kind)
            it.extra.update(cloud=True, library_only=True)
            if kind == "video" and a.duration_ms:
                it.extra["duration"] = a.duration_ms
            new.append(it)
        if not new:
            return
        log.info("showing %d library items: %d stored on the iPhone, %d only in iCloud",
                 len(new), len(stored), len(new) - len(stored))
        items = sorted(self.model.items + new, key=lambda x: (x.date, x.name), reverse=True)
        self._fill_years(items)
        self.model.set_items(items)
        self._match_library_items()
        if stored:
            self._extra_audit = getattr(self, "_extra_audit", []) + stored
            self._run_extra_audit()

    def _run_extra_audit(self):
        """Check items found after the main backup check started (once it has finished)."""
        pending = getattr(self, "_extra_audit", [])
        if not pending or (self.audit_worker and self.audit_worker.isRunning()):
            return
        if not self.backup_root or not os.path.isdir(self.backup_root):
            return
        self._extra_audit = []
        w = AuditWorker(self.device, self.make_index(), pending)
        w.results.connect(self.on_audit_results)
        w.finished_ok.connect(self.on_audit_done)
        self.audit_worker = w
        w.start()

    def _match_library_items(self):
        """Recognise iCloud-only items already in the backup folder (same name and size),
        e.g. downloaded from icloud.com. Needs the backup check's index."""
        if self.audit_worker and self.audit_worker.isRunning():
            return                                   # done again when the check finishes
        index = getattr(self, "_index", None)
        for it in self.model.items:
            if not it.extra.get("library_only") or it.backed_up is True:
                continue
            match = index.find_by_name_size(it.name, it.size) if (index and it.size) else None
            if match:
                self._mark_on_pc(it, match)
            else:
                self._mark_not_on_pc(it)
        self._render_icloud_count()
        self.update_audit_summary(final=True)

    def _icloud_items(self):
        return [i for i in self.model.items if i.extra.get("cloud") and i.backed_up is not True
                and not self._skips_deleted(i)]

    def _render_icloud_count(self):
        n = len(self._icloud_items())
        items = self.model.items
        hidden = self.hide_deleted.isChecked()
        items = [i for i in items if not (hidden and i.extra.get("trashed"))]   # match what's shown
        on_phone = sum(1 for i in items if not i.extra.get("cloud"))
        missing = sum(1 for i in items if i.backed_up is False and not i.extra.get("cloud")
                      and not self._skips_deleted(i))
        for key, label, count in (("phone", "On this iPhone", on_phone), ("icloud", "Only in iCloud", n),
                                  ("missing", "Not backed up yet", missing)):
            idx = self.show_box.findData(key)
            self.show_box.setItemText(idx, f"{label} ({count:,})" if items else label)

    # -- dates taken ---------------------------------------------------------------
    def _start_dates(self, items):
        if self.dates_reader and self.dates_reader.isRunning():
            self.dates_reader.cancel()
            self.dates_reader.wait(3000)
        r = CaptureDateReader(self.device, items)
        if not r.total:
            return
        self._dates_changed = 0
        r.found.connect(self._on_dates)
        r.progress.connect(lambda d, t: (self.dates_label.setText(f"Dates {d * 100 // max(1, t)}%"),
                                         self.dates_label.show()))
        r.finished.connect(self._on_dates_done)
        self.dates_reader = r
        self.dates_label.setText("Dates 0%")
        self.dates_label.show()
        r.start()
        if self.copying:
            r.pause(True)

    def _on_dates(self, batch):
        for path, when in batch:
            it = self.model.by_path.get(path)
            if it is not None:
                it.extra.setdefault("file_date", it.date)
                it.date = when
                self._dates_changed += 1

    def _on_dates_done(self):
        self.dates_label.hide()
        log.info("dates taken: %d items had a different date than the iPhone's file date",
                 self._dates_changed)
        if not self._dates_changed:
            return
        # regroup once, by the date each item was taken
        items = sorted(self.model.items, key=lambda x: (x.date, x.name), reverse=True)
        self._fill_years(items)
        self.model.set_items(items)
        self.rebuild_grid()

    def _start_prefetch(self, items):
        self._pv_hide.stop()
        self._pv_tick.start()
        self._pv_t0 = time.monotonic()
        self.thumbs.prefetch(items)       # all items (photos and videos)
        self.vthumbs.prefetch([])         # filled with the videos that need decoding
        if self.copying:
            self._pause_prefetch(True)

    def _pause_prefetch(self, on: bool):
        self.thumbs.pause_prefetch(on)
        self.vthumbs.pause_prefetch(on)
        if self.dates_reader and self.dates_reader.isRunning():
            self.dates_reader.pause(on)
        self._render_pv()

    def _on_pv_progress(self, kind, done, total):
        self._render_pv()

    def _live_pv_counts(self):
        """Real counts, read straight from both workers (their own reports are throttled)."""
        with self.thumbs._cv:
            pd, pt = len(self.thumbs._pf_done), len(self.thumbs._pf_all)
        vd, vt = len(self.vthumbs._pf_done), len(self.vthumbs._pf_all)
        self._pv = {"photo": (pd, pt), "video": (vd, vt)}

    def _render_pv(self):
        # "photo" = the main preview worker (now every item); "video" = videos it handed on
        # because the iPhone had no thumbnail for them (counted there instead)
        self._live_pv_counts()
        (pd, pt), (vd, vt) = self._pv["photo"], self._pv["video"]
        done, total = pd + vd, pt
        if not total:
            return
        tip = (f"{pd + vd:,} of {pt:,} ready" + (f" · {vt:,} videos needed a frame decoded ({vd:,} done)" if vt else "")
               + "\nPreviews are temporary and cleared when the iPhone is disconnected.")
        self.pv_label.setToolTip(tip)
        self.pv_bar.setToolTip(tip)
        self.pv_bar.setMaximum(total)
        self.pv_bar.setValue(done)
        if done >= total:
            self.pv_label.setText("Previews ready ✓")
            self._pv_hide.start()
            if self._pv_tick.isActive():
                self._pv_tick.stop()
                t = self.thumbs
                log.info("previews ready: %d items in %.0f s; %d from the iPhone's own thumbnails, "
                         "%d without one (%d videos decoded)", total, time.monotonic() - self._pv_t0,
                         len(t._pt_from_phone), len(t._pt_without), vt)
        else:
            self._pv_hide.stop()
            if self.copying or not self.connected:
                # short text: during a copy the copy's own progress bar needs the room
                self.pv_label.setText(f"Previews paused · {done:,}/{total:,}")
                self.pv_label.setToolTip(tip + ("\nPaused so the copy gets the full USB speed."
                                                if self.copying else "\nPaused until the iPhone is connected."))
            else:
                self.pv_label.setText(f"Loading previews… {done:,} of {total:,}")
        self.pv_label.show()
        self.pv_bar.show()

    def _log_health(self):
        """Every 15 s: how busy the app and the PC are, and what the app is doing.
        Goes to lptpro.log only, to track down stutter."""
        try:
            import psutil
        except Exception:
            return
        p = getattr(self, "_proc", None)
        if p is None:
            p = self._proc = psutil.Process()
            p.cpu_percent(None)
            psutil.cpu_percent(None)
            return
        cores = psutil.cpu_count() or 1
        doing = [n for n, on in (("video playing", self._video_playing),
                                 ("copying", self.copying),
                                 ("backup check", bool(self.audit_worker and self.audit_worker.isRunning())),
                                 ("previews", self._pv["photo"][0] + self._pv["video"][0]
                                  < self._pv["photo"][1] + self._pv["video"][1])) if on]
        phone = ""
        if hasattr(self.device, "open_file_count"):
            n = self.device.open_file_count
            phone = f"; files open on iPhone: {n}"
            if n >= 20:
                phone += " (oldest: " + ", ".join(self.device.oldest_open_files()) + ")"
            if self.device.session_resets:
                phone += f"; fresh sessions: {self.device.session_resets}"
        log.info("health: app %.0f%% of the PC's processor, whole PC %.0f%%, memory %d MB, %d threads; %s%s",
                 p.cpu_percent(None) / cores, psutil.cpu_percent(None), p.memory_info().rss // 2**20,
                 p.num_threads(), ", ".join(doing) or "idle", phone)

    def _render_multi(self):
        if self.shift.sticky:
            self.multi_label.setText("●  Multi-Select is on (Shift)")
        elif self.shift.held:
            self.multi_label.setText("●  Multi-Select (holding Shift)")
        self.multi_label.setVisible(self.shift.active)

    def _preview_busy(self, playing: bool):
        """While a video plays, background thumbnail work waits, so playback stays smooth."""
        self.thumbs.pause(playing)
        self.vthumbs.pause(playing)
        if self.audit_worker and self.audit_worker.isRunning():
            self.audit_worker.pause(playing)
        if self.dates_reader and self.dates_reader.isRunning():
            self.dates_reader.pause(playing or self.copying)
        self._video_playing = playing

    def on_thumb(self, path, data, duration=0):
        if duration:
            it = self.model.by_path.get(path)
            if it:
                it.extra["duration"] = duration
        pm = QPixmap()
        if data:
            pm.loadFromData(data, "JPG")
        self.model.thumbs[path] = pm
        self.model.refresh_path(path)

    def update_copy_button(self):
        sel = [self.model.by_path[p] for p in self.model.selected if p in self.model.by_path]
        if not sel:
            self.copy_btn.setText("Select items to copy")
            self.copy_btn.setEnabled(False)
        else:
            self.copy_btn.setText(f"Copy {len(sel):,} item{'s' if len(sel) != 1 else ''} · {human_bytes(sum(i.size for i in sel))}")
            self.copy_btn.setEnabled(not self.copying and self.connected)

    def open_item(self, it: MediaItem):
        if self.preview is None:
            self.preview = PreviewWindow(
                self.device, os.path.join(self.session_dir, "videos"),
                items_provider=lambda: [r[1] for r in self.model.rows if r[0] == "i"],
                local_provider=lambda x: (self.local_of.get(x.path)
                                          if self.local_of.get(x.path) and os.path.exists(self.local_of[x.path])
                                          else None),
                open_external=self.open_external,
                on_busy=self._preview_busy,
                on_current=lambda x: follow_viewer(self.model, self.grid, x))
        self.preview.show_item(it)
        self.preview.show()
        self.preview.raise_()
        self.preview.activateWindow()

    def open_external(self, it: MediaItem):
        """Open in the PC's default app (pulls a temporary copy if it's only on the phone)."""
        local = self.local_of.get(it.path)
        if local and os.path.exists(local):
            QDesktopServices.openUrl(QUrl.fromLocalFile(local))
            return
        if it.extra.get("library_only"):
            self.explain_icloud_library()
            return
        dest = os.path.join(tempfile.gettempdir(), "LPTPro-preview")
        os.makedirs(dest, exist_ok=True)
        self.set_status(f"Opening {it.name}…")
        w = OpenWorker(self.device, it, os.path.join(dest, it.name))
        w.done.connect(lambda p: QDesktopServices.openUrl(QUrl.fromLocalFile(p)) if p else self.set_status("Couldn’t open that item."))
        self.workers.append(w)
        w.start()

    # -- copying ----------------------------------------------------------------
    @property
    def copying(self):
        return self.copy_worker is not None and self.copy_worker.isRunning()

    def backup_missing(self):
        # everything not yet confirmed as on the PC (including items the check hasn't reached:
        # the copy skips any that turn out to be backed up already)
        todo = [i.path for i in self.model.items if i.backed_up is not True and not i.extra.get("cloud")
                and not self._skips_deleted(i)]
        cloud = self._icloud_items()
        if cloud and not self._confirm_icloud_skip(len(todo), len(cloud)):
            return
        if todo:
            self.start_copy(todo, self.backup_root, force_skip=True)

    def _confirm_icloud_skip(self, n_copy, n_cloud) -> bool:
        """Before a backup: say plainly that iCloud-only items will be skipped."""
        box = QMessageBox(self)
        box.setWindowTitle("Some items are only in iCloud")
        box.setIcon(QMessageBox.Information)
        box.setText(f"{n_cloud:,} photos and videos are only in iCloud, so they can\u2019t be copied "
                    f"from the iPhone and will be skipped."
                    + (f"\n\nThe other {n_copy:,} will be backed up now." if n_copy else ""))
        go = box.addButton(f"Back up the {n_copy:,}" if n_copy else "OK", QMessageBox.AcceptRole)
        help_ = box.addButton("What can I do?", QMessageBox.HelpRole)
        if n_copy:
            box.addButton(QMessageBox.Cancel)
        box.setDefaultButton(go)
        box.exec()
        if box.clickedButton() is help_:
            self.explain_icloud_library()
            return False
        return box.clickedButton() is go and n_copy > 0

    def start_copy(self, paths, dest_root, template=None, force_skip=False):
        if not self.connected:
            QMessageBox.information(self, "iPhone not connected", "Plug in your iPhone and unlock it first.")
            return
        if self.copying:
            QMessageBox.information(self, "Copy in progress", "A copy is already running. It’ll be ready for more when it finishes.")
            return
        if not dest_root or not os.path.isdir(dest_root):
            self.choose_backup_root()
            dest_root = self.backup_root
            if not dest_root:
                return
        items = [self.model.by_path[p] for p in paths if p in self.model.by_path]
        if self.opt_pairs.isChecked():
            items = self._with_live_pairs(items)
        if not items:
            return
        w = CopyWorker(self.device, self.make_index(), items, dest_root,
                       template or self.template.currentText(),
                       set_dates=self.opt_dates.isChecked(), include_aae=False,
                       skip_backed_up=self.opt_skip.isChecked() or force_skip,
                       heic_to_jpeg=self.opt_jpeg.isChecked(),
                       keep_heic=self.opt_keep_heic.isChecked())
        w.progress.connect(self.on_copy_progress)
        w.item_done.connect(self.on_item_copied)
        w.item_failed.connect(self.on_item_failed)
        w.finished_all.connect(self.on_copy_finished)
        w.device_lost.connect(self.on_copy_lost)
        w.stopped_failing.connect(self.on_copy_stopped_failing)
        w.item_cloud.connect(lambda p: (self.model.refresh_path(p), self.update_audit_summary()))
        w.note.connect(self.show_note)
        self.copy_worker = w
        self.progress.show()
        self.pause_btn.show()
        self.cancel_btn.show()
        self.pause_btn.setText("Pause")
        self.update_copy_button()
        self.backup_missing_btn.setEnabled(False)
        w.start()
        self._pause_prefetch(True)   # the copy gets the full USB speed

    def _with_live_pairs(self, items):
        by_stem = {}
        for it in self.model.items:
            by_stem.setdefault((it.path.rsplit("/", 1)[0], it.stem.upper()), []).append(it)
        out, seen = [], set()
        for it in items:
            for x in [it] + by_stem.get((it.path.rsplit("/", 1)[0], it.stem.upper()), []):
                if x.path not in seen:
                    seen.add(x.path)
                    out.append(x)
        return out

    def on_copy_progress(self, done, total, name, speed, eta):
        self.progress.setMaximum(max(1, total))
        self.progress.setValue(done)
        pct = int(done * 100 / max(1, total))
        if name:
            self.set_status(f"Copying {done + 1:,} of {total:,} · {name}")
            self.speed_label.setText(f"{pct}% · {human_bytes(speed)}/s · {human_time(eta)} left" if speed else f"{pct}%")
            self.setWindowTitle(f"{pct}% copying · {APP_NAME}")

    def _mark_not_on_pc(self, it):
        it.backed_up = False
        it.extra.pop("as_jpeg", None)
        self.local_of.pop(it.path, None)
        self.watcher.remove_target(it.path)

    def on_watch_results(self, batch):
        """Files changed in the backup folder while the app was open (e.g. deleted)."""
        gone = []
        for path, match in batch:
            it = self.model.by_path.get(path)
            if not it:
                continue
            if match:
                self._mark_on_pc(it, match)      # another copy still exists (or it was renamed)
            else:
                self._mark_not_on_pc(it)
                gone.append(it.name)
            self.model.refresh_path(path)
        if gone:
            names = ", ".join(gone[:3]) + (f" and {len(gone) - 3:,} more" if len(gone) > 3 else "")
            self.set_status(f"No longer on the PC: {names}. Marked as not backed up.")
            if self.model.show != "all":
                self.rebuild_grid()
        self.update_audit_summary(final=True)

    def changeEvent(self, e):
        super().changeEvent(e)
        if e.type() == QEvent.ActivationChange and self.isActiveWindow():
            self.watcher.poke()   # back from Explorer? check right away

    def _mark_on_pc(self, it, local):
        it.backed_up = True
        self.local_of[it.path] = local
        self.watcher.set_target(it, local)
        it.extra["as_jpeg"] = (it.ext in heic.HEIF_EXT and local.lower().endswith((".jpg", ".jpeg")))

    # -- problems ---------------------------------------------------------------
    def on_item_failed(self, path, why):
        log.warning("failed %s: %s", path, why)
        it = self.model.by_path.get(path)
        if it is not None:
            self.problems[path] = (it, why)
            self._render_problems()

    def _render_problems(self):
        n = len(self.problems)
        self.problems_btn.setText(f"⚠  {n:,} problem{'s' if n != 1 else ''}")
        self.problems_btn.setVisible(n > 0)

    def show_problems(self):
        log_path = os.path.join(self.data_dir, "lptpro.log")
        ProblemsDialog(self, dict(self.problems), log_path).open()

    def retry_problems(self):
        paths = list(self.problems)
        self.clear_problems()
        if paths:
            self.start_copy(paths, self.backup_root, force_skip=True)

    def clear_problems(self):
        self.problems.clear()
        self._render_problems()

    def on_item_copied(self, phone_path, local):
        if self.problems.pop(phone_path, None) is not None:
            self._render_problems()
        self.local_of[phone_path] = local
        it = self.model.by_path.get(phone_path)
        if it:
            self._mark_on_pc(it, local)
            self.model.selected.discard(phone_path)
            self.model.refresh_path(phone_path)
        self.update_audit_summary()

    def on_copy_finished(self, copied, skipped, failed, cancelled):
        self.progress.hide()
        self.pause_btn.hide()
        self.cancel_btn.hide()
        self.speed_label.setText("")
        self.setWindowTitle(APP_NAME)
        parts = [f"{copied:,} copied and verified"]
        if skipped:
            parts.append(f"{skipped:,} already backed up")
        cloud = self.copy_worker.cloud_skipped if self.copy_worker is not None else 0
        if cloud:
            parts.append(f"{cloud:,} only in iCloud (skipped)")
        if failed:
            parts.append(f"{failed:,} failed (see Problems)")
        self.set_status(("Stopped — " if cancelled else "Done — ") + ", ".join(parts))
        self.copy_worker = None
        self._pause_prefetch(False)
        self._apply_backup_root()
        if self.model.show != "all":
            self.rebuild_grid()
        self.update_copy_button()
        self.update_audit_summary(final=True)
        QApplication.alert(self)
        if not cancelled and (copied or failed):
            self._show_done(copied, skipped, cloud, failed)
        if self._pending_disconnect:
            self._pending_disconnect = False
            self.disconnect_device(ask=False)

    def toggle_pause(self):
        if not self.copy_worker:
            return
        paused = self.pause_btn.text() == "Pause"
        self.copy_worker.pause(paused)
        self.pause_btn.setText("Resume" if paused else "Pause")
        if paused:
            self.set_status(self.status_label.text().replace("Copying", "Paused at"))

    def cancel_copy(self):
        if self.copy_worker:
            self.copy_worker.cancel()
            self.set_status("Stopping after the current file…")

    # -- disconnecting ---------------------------------------------------------------
    def show_note(self, text):
        self.set_status(text)
        box = QMessageBox(QMessageBox.Information, APP_NAME, text, QMessageBox.Ok, self)
        box.open()  # non-blocking

    def _wipe_session_previews(self):
        """Delete this session's temporary previews (the app's own temp files only)."""
        shutil.rmtree(self.session_dir, ignore_errors=True)

    def _stop_phone_work(self):
        self.vthumbs.stop()
        self._scan_no += 1
        if self.model.waiting:
            self.model.waiting = False
            self.rebuild_grid()
        if self.deleted_reader is not None:
            try:
                self.deleted_reader.done.disconnect()
            except (RuntimeError, TypeError):
                pass
        if self.dates_reader and self.dates_reader.isRunning():
            self.dates_reader.cancel()
        self.thumbs.pause_prefetch(True)
        QTimer.singleShot(500, self._wipe_session_previews)   # after workers let go
        QTimer.singleShot(0, self._render_pv)   # show "paused"
        for w in (self.audit_worker, self.scan_worker):
            if w and w.isRunning():
                w.cancel()
        if self.preview:
            self.preview.stop_all()

    def disconnect_device(self, ask=True):
        """The 'eject' button. The app only ever reads from the iPhone, so there's nothing to
        flush on the phone; this stops all phone work and closes the session cleanly."""
        if self.copying:
            if ask:
                r = QMessageBox.question(self, "Copy in progress",
                                         "A copy is running. Stop it and disconnect?\n\n"
                                         "Files already copied and verified are kept. The file in "
                                         "progress is discarded, so nothing half-copied is left behind.")
                if r != QMessageBox.Yes:
                    return
            self._pending_disconnect = True
            self.copy_worker.cancel()
            self.set_status("Stopping the copy, then disconnecting…")
            return
        self.manual_disconnect = True
        self.retry_timer.stop()
        self._stop_phone_work()
        self.device.close()
        self.connected = False
        name = getattr(self, "device_name", "the iPhone")
        self.device_label.setText("Disconnected · safe to unplug")
        self.device_label.setProperty("ok", False)
        self.device_label.style().polish(self.device_label)
        self.disconnect_btn.hide()
        self.retry_btn.setText("Connect")
        self.retry_btn.show()
        self.set_status(f"Disconnected from {name}. It’s safe to unplug. Nothing on the iPhone was changed.")
        self.update_copy_button()

    def on_device_lost(self):
        """The cable was pulled (or the phone stopped answering)."""
        if not self.connected:
            return
        self.connected = False
        self._stop_phone_work()
        self.device_label.setText("iPhone unplugged")
        self.device_label.setProperty("ok", False)
        self.device_label.style().polish(self.device_label)
        self.disconnect_btn.hide()
        self.retry_btn.setText("Reconnect")
        self.retry_btn.show()
        if not self.copying:
            self.set_status("The iPhone was unplugged. Nothing on it was changed. Plug it back in to continue.")
        self.retry_timer.start()

    # -- adding downloaded photos -----------------------------------------------------
    def add_downloaded(self):
        if self.copying:
            QMessageBox.information(self, "Copy in progress", "A copy is already running. It\u2019ll be ready for more when it finishes.")
            return
        if not self.backup_root or not os.path.isdir(self.backup_root):
            self.choose_backup_root()
            if not self.backup_root:
                return
        n_cloud = len(self._icloud_items())
        box = QMessageBox(self)
        box.setWindowTitle("Add downloaded photos")
        box.setIcon(QMessageBox.Information)
        box.setText("Got photos from iCloud, an old phone or a camera? LPT Pro copies them into your "
                    "backup folder, sorted by the date each one was taken. Every copy is checked, and "
                    "photos already in your backup are skipped. The downloaded files aren\u2019t changed."
                    + (f"\n\n{n_cloud:,} photos on this iPhone are only in iCloud. Once you\u2019ve added "
                       f"them here, they\u2019re marked ON PC." if n_cloud else ""))
        folder_b = box.addButton("Choose a folder\u2026", QMessageBox.AcceptRole)
        zips_b = box.addButton("Choose zip files\u2026", QMessageBox.AcceptRole)
        how_b = box.addButton("How do I download from iCloud?", QMessageBox.HelpRole)
        box.addButton(QMessageBox.Cancel)
        box.setDefaultButton(folder_b)
        box.exec()
        clicked = box.clickedButton()
        if clicked is how_b:
            self.explain_icloud_library()
        elif clicked is folder_b:
            src = QFileDialog.getExistingDirectory(self, "Folder with the downloaded photos")
            if src:
                self._import_folder(src)
        elif clicked is zips_b:
            files, _ = QFileDialog.getOpenFileNames(self, "Zip files with the downloaded photos", "",
                                                    "Zip files (*.zip)")
            if files:
                self._import_zips(files)

    def _import_folder(self, src):
        from .importer import _inside, scan_folder
        if _inside(src, self.backup_root) or _inside(self.backup_root, src):
            QMessageBox.information(self, "Add downloaded photos",
                                    "That folder is (or contains) your backup folder. Pick the folder "
                                    "with the downloaded photos instead.")
            return
        self.set_status("Looking for photos and videos in that folder\u2026")
        QApplication.processEvents()
        items = scan_folder(src, self.backup_root)
        self._start_import(FolderBackend(src), items, src, src)

    def _import_zips(self, files):
        from .importer import ZipSource
        try:
            source = ZipSource(files)
        except Exception as e:
            QMessageBox.warning(self, "Add downloaded photos", f"Couldn\u2019t open the zip file:\n{e}")
            return
        self._start_import(source, source.items(), os.path.dirname(files[0]), ", ".join(source.names))

    def _start_import(self, source, items, folder, label):
        if not items:
            QMessageBox.information(self, "Add downloaded photos", f"No photos or videos were found in {label}.")
            return
        log.info("adding %d downloaded items from %s", len(items), label)
        self._start_copy_job(source, items, folder, self.backup_root, self.make_index(),
                             self.template.currentText(), self.opt_jpeg.isChecked(),
                             self.opt_keep_heic.isChecked(), "import")

    def start_export(self, source, items, dest, organize, jpeg):
        """Album mode: copy favorites to a separate folder (for a photo book or prints)."""
        if self.copying:
            QMessageBox.information(self, "Copy in progress", "A copy is already running. It\u2019ll be ready for more when it finishes.")
            return
        os.makedirs(dest, exist_ok=True)
        index = BackupIndex([dest], os.path.join(self.data_dir, "export-fingerprints.json"))
        index.build()                      # what's there already (exporting again adds only new ones)
        log.info("exporting %d favorites to %s", len(items), dest)
        self._start_copy_job(source, items, dest, dest, index,
                             self.template.currentText() if organize else "No subfolders", jpeg, False, "export")

    def _start_copy_job(self, source, items, folder, dest_root, index, template, jpeg, keep_heic, kind):
        w = CopyWorker(source, index, items, dest_root, template,
                       set_dates=self.opt_dates.isChecked(), include_aae=False, skip_backed_up=True,
                       heic_to_jpeg=jpeg, keep_heic=keep_heic)
        w.progress.connect(self.on_copy_progress)
        w.item_failed.connect(lambda path, why: log.warning("%s failed %s: %s", kind, path, why))
        w.note.connect(self.show_note)
        w.finished_all.connect(lambda c, s, f, x: self._on_import_finished(source, folder, c, s, f, x, kind))
        self.copy_worker = w
        self.progress.show()
        self.pause_btn.show()
        self.cancel_btn.show()
        self.pause_btn.setText("Pause")
        self.update_copy_button()
        self.backup_missing_btn.setEnabled(False)
        self.add_btn.setEnabled(False)
        w.start()
        self._pause_prefetch(True)

    def _on_import_finished(self, source, folder, copied, skipped, failed, cancelled, kind="import"):
        self.progress.hide()
        self.pause_btn.hide()
        self.cancel_btn.hide()
        self.speed_label.setText("")
        self.setWindowTitle(APP_NAME)
        self.copy_worker = None
        self.add_btn.setEnabled(True)
        if hasattr(source, "close") and not isinstance(source, FolderBackend):
            source.close()
        self._pause_prefetch(False)
        export = kind == "export"
        parts = [f"{copied:,} {'exported' if export else 'added'} and verified"]
        if skipped:
            parts.append(f"{skipped:,} were already {'exported' if export else 'in your backup'}")
        if failed:
            parts.append(f"{failed:,} failed (see the log)")
        self.set_status(("Stopped — " if cancelled else "Done — ") + ", ".join(parts))
        if export:
            self.update_copy_button()
            self.update_audit_summary(final=True)
            QApplication.alert(self)
            if not cancelled:
                self._show_done(copied, skipped, 0, failed,
                                note=("Your favorites are ready in that folder, for example to upload "
                                      "for a photo book or prints." if not failed else
                                      "Some favorites couldn\u2019t be copied; see the log."),
                                folder=folder, folder_label="Show exported folder")
            return
        self._apply_backup_root()
        # what was just added may be photos the iPhone only has in iCloud: recognise them
        if getattr(self, "_index", None) is not None:
            self._index.build()
            self._match_library_items()
        self.update_copy_button()
        self.update_audit_summary(final=True)
        QApplication.alert(self)
        if not cancelled:
            self._show_done(copied, skipped, 0, failed,
                            note=("All of them are safely in your backup now. You can delete the "
                                  "downloaded files if you like." if not failed else
                                  "Keep the downloaded files until the failed ones are sorted out."),
                            folder=folder)

    def explain_icloud_library(self):
        n = len(self._icloud_items())
        win = sys.platform == "win32"
        msg = (f"{n:,} photos and videos on this iPhone are only in iCloud. Their full-size "
               f"originals aren\u2019t stored on the phone (\u201cOptimize iPhone Storage\u201d is on): "
               f"the iPhone just keeps the small preview you see here, and downloads the original "
               f"when you open it. Nothing is lost, but they are NOT in your backup yet.\n\n"
               f"The easiest way, whatever space your iPhone has:\n"
               f"1.  On this PC, go to icloud.com/photos and sign in.\n"
               f"2.  Select the photos (click one, then Shift-click another to select a range).\n"
               f"3.  Click Download and choose \u201cUnmodified Original\u201d.\n"
               f"4.  Back in LPT Pro, press \u201cAdd downloaded photos\u2026\u201d and pick the "
               f"download folder. LPT Pro sorts them into your backup and marks them ON PC.\n"
               + ("\nOr install Apple\u2019s free iCloud for Windows app, which can download the whole "
                  "library to this PC.\n" if win else "")
               + "\nIf your iPhone has lots of free space, you can also switch it to Settings \u2192 "
               "Photos \u2192 \u201cDownload and Keep Originals\u201d.")
        QMessageBox(QMessageBox.Information, "Only in iCloud", msg, QMessageBox.Ok, self).open()

    def on_copy_stopped_failing(self, n, reason):
        msg = (f"The copy stopped because {n} files in a row couldn’t be copied, even after "
               f"reconnecting to the iPhone.\n\nReason: {reason}\n\n"
               f"Everything copied before that was verified and is safe. Nothing half-copied was "
               f"kept, and nothing on the iPhone was changed.\n\nTry: unplug the iPhone, plug it "
               f"back in, unlock it, then press “Back up what’s missing” again.")
        self.set_status(f"Stopped — {n} files in a row failed (see log)")
        QMessageBox(QMessageBox.Warning, "Copy stopped", msg, QMessageBox.Ok, self).open()

    def on_copy_lost(self, copied):
        msg = (f"The iPhone was unplugged during the copy.\n\n"
               f"{copied:,} file{'s' if copied != 1 else ''} were copied and verified before that and are safe "
               f"in your backup folder. The file that was in progress was discarded, so nothing "
               f"half-copied is left behind. Nothing on the iPhone was changed.\n\n"
               f"Plug it back in and press “Back up what’s missing” to carry on.")
        self.set_status(msg.split("\n")[0])
        box = QMessageBox(QMessageBox.Warning, "iPhone unplugged", msg, QMessageBox.Ok, self)
        box.open()

    def switch_mode(self, mode):
        """iPhone mode (what's on the phone, backing up) or Album mode (the backup on this PC)."""
        if mode == "album":
            if self.album is None:
                from .album import AlbumView
                self.album = AlbumView(self, GridModel, GridView, TileDelegate, self.data_dir)
                self.pages.addWidget(self.album)
            self.pages.setCurrentWidget(self.album)
            if not self.copying:
                self._pause_prefetch(True)          # the album gets the PC's attention
            self.album.load()
        else:
            if self.album is not None:
                self.album.pause()
            self.pages.setCurrentIndex(0)
            if not self.copying:
                self._pause_prefetch(False)
        for b in self.mode_group.buttons():
            b.setChecked(b.property("mode") == mode)

    def closeEvent(self, e):
        if self.copying:
            r = QMessageBox.question(self, "Copy in progress",
                                     "A copy is still running. Stop it and quit?\n(Files already copied are kept.)")
            if r != QMessageBox.Yes:
                e.ignore()
                return
        was_connected = self.connected
        notice = self._closing_notice()
        step = lambda text: (notice.label.setText(text), QApplication.processEvents())

        # 1. ask everything to stop at once, then wait for all of it together
        step("Stopping background work…")
        if self.album is not None:
            self.album.shutdown()
        if self.copying:
            self.copy_worker.cancel()
        for w in (self.audit_worker, self.scan_worker, self.dates_reader):
            if w and w.isRunning():
                w.cancel()
        if self.preview:
            self.preview.stop_all()
            self.preview.close()
        self.vthumbs.stop()
        self.thumbs.stop()
        self.watcher.stop()
        for w, ms in ((self.copy_worker, 15000), (self.audit_worker, 3000), (self.scan_worker, 3000),
                      (self.dates_reader, 3000),
                      (self.thumbs, 3000), (self.watcher, 3000)):
            if w is not None:
                w.wait(ms)

        # 2. end the session with the phone (read-only: nothing to flush on it)
        step("Disconnecting from your iPhone…")
        self.device.on_disconnect = None
        self.device.close()

        # 3. this session's temporary previews
        step("Clearing temporary previews…")
        self._wipe_session_previews()
        notice.close()

        if was_connected and self.device.kind == "iphone":
            name = getattr(self, "device_name", "your iPhone")
            QMessageBox.information(self, "Disconnected",
                                    f"Disconnected from {name}.\n\nIt’s safe to unplug. "
                                    f"Nothing on the iPhone was changed.")
        e.accept()

    def _show_done(self, copied, skipped, cloud, failed, note=None, folder=None,
                   folder_label="Show downloaded folder"):
        """Big 'Done!' card when a copy finishes, readable from across the room."""
        from PySide6.QtWidgets import QDialog
        old = getattr(self, "_done_card", None)
        self._done_card = None
        if old is not None:
            try:
                old.close()
            except RuntimeError:
                pass                     # already closed (and deleted) by OK
        ok = not failed
        d = QDialog(self, Qt.Dialog | Qt.FramelessWindowHint)
        d.setObjectName("doneCard")
        d.setProperty("ok", ok)
        d.setAttribute(Qt.WA_DeleteOnClose)
        v = QVBoxLayout(d)
        v.setContentsMargins(48, 36, 48, 32)
        v.setSpacing(10)
        mark = QLabel("✓" if ok else "!", objectName="doneMark")
        mark.setProperty("ok", ok)
        mark.setAlignment(Qt.AlignCenter)
        mark.setFixedSize(120, 120)
        title = QLabel("Done!", objectName="doneTitle")
        title.setAlignment(Qt.AlignCenter)
        warn = QLabel(f"{failed:,} file{'s' if failed != 1 else ''} need{'' if failed != 1 else 's'} your attention",
                      objectName="doneWarn")
        warn.setAlignment(Qt.AlignCenter)
        warn.setVisible(not ok)
        lines = [f"{copied:,} file{'s' if copied != 1 else ''} copied and verified"]
        if skipped:
            lines.append(f"{skipped:,} were already backed up")
        if cloud:
            lines.append(f"{cloud:,} only in iCloud (not on this iPhone)")
        if failed:
            lines.append(f"{failed:,} couldn’t be copied" + (" – see the log" if note else " – see Problems"))
        if note:
            lines.append(note)
        elif not failed:
            lines.append("Nothing on the iPhone was changed.")
        text = QLabel("\n".join(lines), objectName="doneText")
        text.setAlignment(Qt.AlignCenter)
        v.addWidget(mark, 0, Qt.AlignHCenter)
        v.addSpacing(6)
        v.addWidget(title)
        v.addWidget(warn)
        v.addWidget(text)
        v.addSpacing(14)
        row = QHBoxLayout()
        row.addStretch(1)
        if failed and not note:
            prob = QPushButton("View problems", objectName="orange")
            prob.clicked.connect(lambda: (d.close(), self.show_problems()))
            row.addWidget(prob)
        if folder:
            show = QPushButton(folder_label)
            show.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(folder)))
            row.addWidget(show)
        okb = QPushButton("OK", objectName="primary")
        okb.setMinimumWidth(110)
        okb.setDefault(True)
        okb.clicked.connect(d.close)
        row.addWidget(okb)
        row.addStretch(1)
        v.addLayout(row)
        d.setMinimumWidth(460)
        self.setWindowTitle(("✓ Done – " if ok else "Done – ") + APP_NAME)
        d.finished.connect(lambda *_: self.copying or self.setWindowTitle(APP_NAME))
        self._done_card = d
        d.destroyed.connect(lambda *_: getattr(self, "_done_card", None) is d and setattr(self, "_done_card", None))
        d.adjustSize()
        d.move(self.geometry().center() - d.rect().center())
        d.open()
        okb.setFocus()
        return d

    def _closing_notice(self):
        """Small 'Please wait' box shown the moment the app is closed."""
        from PySide6.QtWidgets import QDialog
        d = QDialog(self, Qt.Dialog | Qt.FramelessWindowHint)
        d.setObjectName("closing")
        d.setModal(True)
        v = QVBoxLayout(d)
        v.setContentsMargins(24, 20, 24, 20)
        title = QLabel("Please wait – closing safely", objectName="auditTitle")
        d.label = QLabel("", objectName="muted")
        bar = QProgressBar(objectName="previewBar")
        bar.setRange(0, 0)              # moving "busy" bar
        bar.setTextVisible(False)
        bar.setFixedHeight(6)
        for wdg in (title, d.label, bar):
            v.addWidget(wdg)
        d.setFixedWidth(360)
        d.show()
        d.move(self.geometry().center() - d.rect().center())
        QApplication.processEvents()
        return d

STYLE = """
* { font-family: "Segoe UI", "Inter", "Noto Sans", sans-serif; font-size: 10pt; }
QMainWindow, QWidget { background: #15171b; color: #e6e8eb; }
QLabel, QCheckBox { background: transparent; }
#topbar, #statusbar, #panel { background: #1b1e23; }
#topbar { border-bottom: 1px solid #2a2e35; }
#statusbar { border-top: 1px solid #2a2e35; }
#panel { border-left: 1px solid #2a2e35; }
#stage, QVideoWidget { background: #000000; }
QToolButton#more { background: #1b1e23; color: #e6e8eb; border: 1px solid #2a2e35; border-radius: 6px;
                   padding: 4px 10px; font-size: 13pt; }
QToolButton#more::menu-indicator { image: none; }
QDialog#closing { background: #1b1e23; border: 1px solid #2a2e35; border-radius: 10px; }
QPushButton#problems { background: #3a2a12; color: #F2994A; border: 1px solid #6b4a1c; font-weight: 700; }
QPushButton#problems:hover { background: #4a3516; }
QFrame#setupCard { background: #1b1e23; border: 1px solid #2a2e35; border-radius: 12px; }
QDialog#doneCard { background: #1b1e23; border: 2px solid #2e7d4f; border-radius: 16px; }
QDialog#doneCard[ok="false"] { border-color: #6b4a1c; }
#doneMark { background: #27ae60; color: #ffffff; border-radius: 60px; font-size: 54pt; font-weight: 700; }
#doneMark[ok="false"] { background: #F2994A; }
#doneTitle { font-size: 26pt; font-weight: 700; color: #ffffff; }
#doneWarn { color: #F2994A; font-size: 13pt; font-weight: 700; }
#doneText { color: #c3c9d1; font-size: 11pt; }
#version { color: #6b7480; font-size: 9pt; padding-bottom: 2px; }
#multi { color: #7fd6a8; font-weight: 700; padding-right: 12px; }
#sideArrow { background: #000000; color: #c3c9d1; border: none; border-radius: 0;
             font-size: 34pt; font-weight: 300; padding: 0; }
#sideArrow:hover { background: #15171b; color: #ffffff; }
#sideArrow:disabled { color: #2a2e35; }
#previewBar { background: #2a2e35; border: none; border-radius: 3px; }
#previewBar::chunk { background: #4C9AFF; border-radius: 3px; }
#title { font-size: 12pt; font-weight: 700; }
#device { color: #b8bfc8; }
#device[ok="true"] { color: #7fd6a8; font-weight: 600; }
#muted, #empty { color: #9aa3ad; }
#empty { font-size: 12pt; }
#section { color: #9aa3ad; font-size: 8pt; font-weight: 700; letter-spacing: 1px; }
#dest { font-weight: 600; }
#audit { background: #1b1e23; border-bottom: 1px solid #2a2e35; }
#auditTitle { font-weight: 700; font-size: 11pt; }
#auditBar { background: #3a2a1c; border: 0; border-radius: 4px; }
#auditBar::chunk { background: #3dbb7f; border-radius: 4px; }
#copyBar { background: #2a2e35; border: 0; border-radius: 4px; }
#copyBar::chunk { background: #4C9AFF; border-radius: 4px; }
#filterbar { border-bottom: 1px solid #2a2e35; }
QPushButton { background: #23272e; border: 1px solid #3a414d; border-radius: 7px; padding: 6px 12px; font-weight: 600; }
QPushButton:hover { background: #2b3038; }
QPushButton:disabled { color: #6b7380; }
QPushButton#primary { background: #4C9AFF; color: #0b1320; border: 0; font-size: 11pt; }
QPushButton#primary:disabled { background: #2c3a52; color: #8b94a0; }
QPushButton#orange { background: #f2994a; color: #1f1206; border: 0; padding: 8px 16px; }
QPushButton#orange:disabled { background: #3a2f25; color: #8b94a0; }
QPushButton#danger { background: #2b1f20; border-color: #5a3a3a; color: #f2b8b5; }
QPushButton#chip { border-radius: 14px; padding: 4px 14px; }
QPushButton#chip:checked { background: #e6e8eb; color: #15171b; }
QLabel#dropzone { border: 2px dashed #3d4552; border-radius: 10px; color: #b8bfc8; background: #1b1e23; }
QLabel#dropzone[hot="true"] { border-color: #4C9AFF; color: #e6e8eb; background: #1f2a3a; }
QTreeView, QComboBox { background: #121418; border: 1px solid #2a2e35; border-radius: 6px; }
QTreeView::item:selected { background: #2c3a52; }
QListView { background: #15171b; border: 0; }
QCheckBox { spacing: 8px; }
QCheckBox::indicator { width: 14px; height: 14px; border: 2px solid #6b7480; border-radius: 4px;
                       background: #15171b; }
QCheckBox::indicator:hover { border-color: #9aa3ad; }
QCheckBox::indicator:checked { background: #4C9AFF; border-color: #4C9AFF; }
QCheckBox::indicator:disabled { border-color: #2a2e35; background: #1b1e23; }
QScrollBar:vertical { background: #15171b; width: 12px; }
QScrollBar::handle:vertical { background: #3a414d; border-radius: 5px; min-height: 40px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
QSplitter::handle { background: #2a2e35; width: 1px; }
"""
