"""Apex PDF - PDF reader and editor for Windows.

Read, search, annotate, fill & sign forms, edit text and manage pages.
GUI: PySide6 (Qt).  PDF engine: PyMuPDF (see pdf_core.py).
"""

from __future__ import annotations

import os
import sys

import fitz
from PySide6.QtCore import (QBuffer, QByteArray, QEvent, QIODevice, QPoint, QPointF,
                            QRectF, QSettings, QSize, QStandardPaths, Qt,
                            QTimer, Signal)
from PySide6.QtGui import (QAction, QColor, QIcon, QImage, QKeySequence,
                           QPainter, QPainterPath, QPen, QPixmap)
from PySide6.QtPrintSupport import QPrintDialog, QPrinter
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox,
                               QColorDialog, QComboBox, QDialog,
                               QDialogButtonBox, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QHBoxLayout, QInputDialog, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem,
                               QMainWindow, QMenu, QMessageBox, QPushButton,
                               QScrollArea, QSpinBox, QSplitter, QToolBar,
                               QVBoxLayout, QWidget)

# allow "python app/main.py" and PyInstaller
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pdf_core import PdfDocument, parse_page_ranges, rgb, to_page_rect  # noqa: E402

APP_NAME = "Apex PDF"
ZOOM_STEPS = [0.25, 0.33, 0.5, 0.67, 0.75, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75,
              2.0, 2.5, 3.0, 4.0]

TOOLS = [
    # key, label, tip, shortcut
    ("hand", "✋ Hand", "Move around the page (drag)", "H"),
    ("select", "⬚ Select text", "Drag over text to copy it", "S"),
    ("highlight", "🖍 Highlight", "Drag over text to highlight it", "Ctrl+Shift+H"),
    ("underline", "U̲ Underline", "Drag over text to underline it", "Ctrl+Shift+U"),
    ("strike", "S̶ Strike", "Drag over text to strike it out", "Ctrl+Shift+S"),
    ("ink", "✏ Draw", "Draw freehand", "D"),
    ("rect", "▭ Box", "Draw a rectangle", "B"),
    ("textbox", "T Text box", "Drag an area, then type text", "T"),
    ("note", "💬 Note", "Click to add a sticky note", "N"),
    ("edit", "✎ Edit text", "Click on existing text to change it", "E"),
    ("sign", "✍ Sign", "Drag the area where your signature goes", "G"),
    ("image", "🖼 Image", "Drag an area, then pick an image (stamp, logo)", "I"),
    ("eraser", "⌫ Delete mark", "Click an annotation to delete it", "X"),
    ("whiteout", "⬜ White-out", "Drag an area to erase it permanently", "W"),
]
DRAG_TOOLS = {"select", "highlight", "underline", "strike", "rect", "textbox",
              "sign", "image", "whiteout"}


def resource_path(rel: str) -> str:
    """Path to a bundled file (works from source and inside the PyInstaller exe)."""
    base = getattr(sys, "_MEIPASS", os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    return os.path.join(base, rel)


def app_icon() -> QIcon:
    for name in ("assets/apexpdf.ico", "assets/apexpdf.png"):
        p = resource_path(name)
        if os.path.exists(p):
            return QIcon(p)
    return QIcon()


def app_data_dir() -> str:
    d = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
    os.makedirs(d, exist_ok=True)
    return d


def signature_path() -> str:
    return os.path.join(app_data_dir(), "signature.png")


def qcolor_to_pdf(c: QColor) -> tuple[float, float, float]:
    return (c.redF(), c.greenF(), c.blueF())


def pixmap_to_qpixmap(pix: fitz.Pixmap, dpr: float) -> QPixmap:
    img = QImage(pix.samples, pix.width, pix.height, pix.stride,
                 QImage.Format.Format_RGB888).copy()
    qp = QPixmap.fromImage(img)
    qp.setDevicePixelRatio(dpr)
    return qp


# ==========================================================================
# Page view: shows one page, handles tools
# ==========================================================================
class PageView(QWidget):
    rectDrawn = Signal(QRectF)          # display coords in PDF points
    pointClicked = Signal(QPointF)
    inkDrawn = Signal(list)             # [[(x, y), ...], ...]

    def __init__(self, scroll: QScrollArea):
        super().__init__()
        self.scroll = scroll
        self.pixmap: QPixmap | None = None
        self.zoom = 1.0
        self.tool = "hand"
        self.ink_color = QColor(220, 0, 0)
        self._drag_start: QPointF | None = None
        self._drag_now: QPointF | None = None
        self._strokes: list[list[tuple[float, float]]] = []
        self._hand_start: QPoint | None = None
        self.search_rects: list[QRectF] = []
        self.current_hit: QRectF | None = None
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def set_page(self, pixmap: QPixmap, zoom: float):
        self.pixmap = pixmap
        self.zoom = zoom
        size = pixmap.deviceIndependentSize().toSize()
        self.setFixedSize(size)
        self.update()

    def set_tool(self, tool: str):
        self.tool = tool
        self._strokes = []
        cursors = {
            "hand": Qt.CursorShape.OpenHandCursor,
            "select": Qt.CursorShape.IBeamCursor,
            "edit": Qt.CursorShape.IBeamCursor,
            "note": Qt.CursorShape.PointingHandCursor,
            "eraser": Qt.CursorShape.PointingHandCursor,
        }
        self.setCursor(cursors.get(tool, Qt.CursorShape.CrossCursor))

    def _pdf(self, p: QPointF) -> tuple[float, float]:
        return (p.x() / self.zoom, p.y() / self.zoom)

    # ---- mouse -----------------------------------------------------------
    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return
        pos = e.position()
        if self.tool == "hand":
            self._hand_start = e.globalPosition().toPoint()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        elif self.tool in DRAG_TOOLS:
            self._drag_start = pos
            self._drag_now = pos
        elif self.tool == "ink":
            self._strokes.append([self._pdf(pos)])
        elif self.tool in ("note", "edit", "eraser"):
            x, y = self._pdf(pos)
            self.pointClicked.emit(QPointF(x, y))

    def mouseMoveEvent(self, e):
        pos = e.position()
        if self.tool == "hand" and self._hand_start is not None:
            delta = e.globalPosition().toPoint() - self._hand_start
            self._hand_start = e.globalPosition().toPoint()
            h = self.scroll.horizontalScrollBar()
            v = self.scroll.verticalScrollBar()
            h.setValue(h.value() - delta.x())
            v.setValue(v.value() - delta.y())
        elif self._drag_start is not None:
            self._drag_now = pos
            self.update()
        elif self.tool == "ink" and self._strokes and e.buttons() & Qt.MouseButton.LeftButton:
            self._strokes[-1].append(self._pdf(pos))
            self.update()

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return
        if self.tool == "hand":
            self._hand_start = None
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        elif self._drag_start is not None:
            r = QRectF(self._drag_start, e.position()).normalized()
            self._drag_start = self._drag_now = None
            self.update()
            if r.width() > 3 and r.height() > 3:
                z = self.zoom
                self.rectDrawn.emit(QRectF(r.x() / z, r.y() / z,
                                           r.width() / z, r.height() / z))
        elif self.tool == "ink":
            strokes = self.take_strokes()
            if strokes:
                self.inkDrawn.emit(strokes)

    def take_strokes(self) -> list:
        s = [st for st in self._strokes if len(st) > 1]
        self._strokes = []
        self.update()
        return s

    # ---- painting --------------------------------------------------------
    def paintEvent(self, _):
        p = QPainter(self)
        if self.pixmap:
            p.drawPixmap(0, 0, self.pixmap)
        z = self.zoom
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        # search results
        for r in self.search_rects:
            p.fillRect(QRectF(r.x() * z, r.y() * z, r.width() * z, r.height() * z),
                       QColor(255, 200, 0, 90))
        if self.current_hit is not None:
            r = self.current_hit
            p.setPen(QPen(QColor(255, 120, 0), 2))
            p.drawRect(QRectF(r.x() * z, r.y() * z, r.width() * z, r.height() * z))
        # drag rectangle
        if self._drag_start is not None and self._drag_now is not None:
            r = QRectF(self._drag_start, self._drag_now).normalized()
            p.setPen(QPen(QColor(0, 120, 215), 1, Qt.PenStyle.DashLine))
            p.fillRect(r, QColor(0, 120, 215, 40))
            p.drawRect(r)
        # ink in progress
        if self._strokes:
            p.setPen(QPen(self.ink_color, 2))
            for st in self._strokes:
                for (x1, y1), (x2, y2) in zip(st, st[1:]):
                    p.drawLine(QPointF(x1 * z, y1 * z), QPointF(x2 * z, y2 * z))
        p.end()


# ==========================================================================
# Dialogs
# ==========================================================================
class SignaturePad(QWidget):
    def __init__(self):
        super().__init__()
        self.paths: list[QPainterPath] = []
        self.setMinimumSize(500, 200)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def mousePressEvent(self, e):
        path = QPainterPath(e.position())
        self.paths.append(path)

    def mouseMoveEvent(self, e):
        if self.paths and e.buttons() & Qt.MouseButton.LeftButton:
            self.paths[-1].lineTo(e.position())
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), Qt.GlobalColor.white)
        p.setPen(QPen(QColor(200, 200, 200), 1, Qt.PenStyle.DashLine))
        y = int(self.height() * 0.75)
        p.drawLine(20, y, self.width() - 20, y)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(10, 30, 120), 3, Qt.PenStyle.SolidLine,
                      Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        for path in self.paths:
            p.drawPath(path)
        p.end()

    def clear(self):
        self.paths = []
        self.update()

    def to_png(self) -> bytes | None:
        if not self.paths:
            return None
        bounds = QRectF()
        for path in self.paths:
            bounds = bounds.united(path.boundingRect())
        bounds.adjust(-6, -6, 6, 6)
        scale = 3  # high resolution
        img = QImage(int(bounds.width() * scale), int(bounds.height() * scale),
                     QImage.Format.Format_ARGB32)
        img.fill(Qt.GlobalColor.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.scale(scale, scale)
        p.translate(-bounds.topLeft())
        p.setPen(QPen(QColor(10, 30, 120), 3, Qt.PenStyle.SolidLine,
                      Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        for path in self.paths:
            p.drawPath(path)
        p.end()
        return qimage_to_png(img)


def qimage_to_png(img: QImage) -> bytes:
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    return bytes(ba.data())


class SignatureDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Create your signature")
        self.png: bytes | None = None
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Sign with the mouse, touchpad or pen:"))
        self.pad = SignaturePad()
        lay.addWidget(self.pad)
        row = QHBoxLayout()
        clear = QPushButton("Clear")
        clear.clicked.connect(self.pad.clear)
        load = QPushButton("Use an image file instead…")
        load.clicked.connect(self._load)
        row.addWidget(clear)
        row.addWidget(load)
        row.addStretch()
        lay.addLayout(row)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save |
                              QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _load(self):
        f, _ = QFileDialog.getOpenFileName(self, "Signature image", "",
                                           "Images (*.png *.jpg *.jpeg *.bmp)")
        if f:
            with open(f, "rb") as fh:
                self.png = fh.read()
            self.accept()

    def _save(self):
        self.png = self.pad.to_png()
        if not self.png:
            QMessageBox.warning(self, APP_NAME, "Please sign first.")
            return
        self.accept()


class EditTextDialog(QDialog):
    def __init__(self, text: str, size: float, color: tuple, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit text")
        self.color = QColor.fromRgbF(*color)
        lay = QFormLayout(self)
        self.edit = QLineEdit(text)
        self.edit.selectAll()
        self.edit.setMinimumWidth(420)
        lay.addRow("Text:", self.edit)
        self.size = QDoubleSpinBox()
        self.size.setRange(2, 200)
        self.size.setDecimals(1)
        self.size.setValue(size)
        lay.addRow("Font size:", self.size)
        self.color_btn = QPushButton()
        self._paint_btn()
        self.color_btn.clicked.connect(self._pick)
        lay.addRow("Colour:", self.color_btn)
        info = QLabel("The new text uses the closest standard font "
                      "(Helvetica, Times or Courier).\nLeave empty to delete the text.")
        info.setStyleSheet("color: gray")
        lay.addRow(info)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                              QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addRow(bb)

    def _paint_btn(self):
        self.color_btn.setStyleSheet(f"background:{self.color.name()}; min-width:60px")

    def _pick(self):
        c = QColorDialog.getColor(self.color, self)
        if c.isValid():
            self.color = c
            self._paint_btn()


class FormDialog(QDialog):
    def __init__(self, fields: list[dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Fill form")
        self.resize(560, 600)
        self.inputs: dict[int, QWidget] = {}
        self.fields = {f["xref"]: f for f in fields}
        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        form = QFormLayout(inner)
        for f in fields:
            t = f["type"]
            label = f"p{f['page'] + 1} · {f['name']}"
            if t in (fitz.PDF_WIDGET_TYPE_CHECKBOX, fitz.PDF_WIDGET_TYPE_RADIOBUTTON):
                w = QCheckBox()
                w.setChecked(f["value"] not in (None, "", "Off", False))
            elif t in (fitz.PDF_WIDGET_TYPE_COMBOBOX, fitz.PDF_WIDGET_TYPE_LISTBOX):
                w = QComboBox()
                w.setEditable(t == fitz.PDF_WIDGET_TYPE_COMBOBOX)
                for c in f["choices"]:
                    w.addItem(c[1] if isinstance(c, (list, tuple)) else str(c))
                w.setCurrentText(str(f["value"] or ""))
            elif t == fitz.PDF_WIDGET_TYPE_TEXT:
                w = QLineEdit(str(f["value"] or ""))
            else:
                continue  # buttons, signature fields
            self.inputs[f["xref"]] = w
            form.addRow(label, w)
        if not self.inputs:
            form.addRow(QLabel("This PDF has no fillable fields.\n"
                               "Use the Text box tool to type on the page instead."))
        scroll.setWidget(inner)
        outer.addWidget(scroll)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                              QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        outer.addWidget(bb)

    def values(self) -> dict[int, object]:
        out = {}
        for xref, w in self.inputs.items():
            old = self.fields[xref]["value"]
            if isinstance(w, QCheckBox):
                new = w.isChecked()
                if new != (old not in (None, "", "Off", False)):
                    out[xref] = new
            elif isinstance(w, QComboBox):
                if w.currentText() != str(old or ""):
                    out[xref] = w.currentText()
            elif isinstance(w, QLineEdit):
                if w.text() != str(old or ""):
                    out[xref] = w.text()
        return out


# ==========================================================================
# Main window
# ==========================================================================
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings("Apex", "ApexPDF")
        self.pdf: PdfDocument | None = None
        self.index = 0
        self.zoom = 1.0
        self.fit_width = True
        self.color = QColor(255, 230, 0)       # highlight colour
        self.pen_color = QColor(220, 0, 0)     # draw / box colour
        self.text_color = QColor(0, 0, 0)
        self.search_hits: list[tuple[int, fitz.Rect]] = []
        self.search_pos = -1
        self._thumb_queue: list[int] = []
        self._building_thumbs = False

        self.setWindowTitle(APP_NAME)
        self.resize(1300, 900)
        self.setAcceptDrops(True)

        # central area
        self.scroll = QScrollArea()
        self.scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.scroll.setStyleSheet("QScrollArea { background: #525659; }")
        self.view = PageView(self.scroll)
        self.view.rectDrawn.connect(self.on_rect)
        self.view.pointClicked.connect(self.on_click)
        self.view.inkDrawn.connect(self.on_ink)
        self.scroll.setWidget(self.view)
        self.scroll.viewport().installEventFilter(self)

        self.thumbs = QListWidget()
        self.thumbs.setViewMode(QListWidget.ViewMode.ListMode)
        self.thumbs.setIconSize(QSize(120, 160))
        self.thumbs.setSpacing(4)
        self.thumbs.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.thumbs.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.thumbs.setMinimumWidth(170)
        self.thumbs.currentRowChanged.connect(self.on_thumb_row)
        self.thumbs.model().rowsMoved.connect(self.on_thumbs_moved)

        split = QSplitter()
        split.addWidget(self.thumbs)
        split.addWidget(self.scroll)
        split.setStretchFactor(1, 1)
        split.setSizes([180, 1100])
        self.setCentralWidget(split)

        self._build_actions()
        self._build_menus()
        self._build_toolbars()
        self.page_label = QLabel()
        self.statusBar().addPermanentWidget(self.page_label)
        self.set_tool("hand")
        self.update_ui()

    # ------------------------------------------------------------------
    # Actions / menus / toolbars
    # ------------------------------------------------------------------
    def _act(self, text, slot, shortcut=None, tip=None, checkable=False):
        a = QAction(text, self)
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        if tip:
            a.setStatusTip(tip)
            a.setToolTip(f"{tip} ({shortcut})" if isinstance(shortcut, str) else tip)
        a.setCheckable(checkable)
        if slot:
            a.triggered.connect(slot)
        return a

    def _build_actions(self):
        S = QKeySequence.StandardKey
        self.a_open = self._act("📂 Open…", self.open_dialog, S.Open, "Open a PDF")
        self.a_save = self._act("💾 Save", self.save, S.Save, "Save")
        self.a_save_as = self._act("Save as…", self.save_as, S.SaveAs)
        self.a_print = self._act("🖨 Print…", self.print_doc, S.Print, "Print")
        self.a_close = self._act("Close", self.close_doc, S.Close)
        self.a_quit = self._act("Exit", self.close, S.Quit)
        self.a_undo = self._act("↶ Undo", self.undo, S.Undo, "Undo")
        self.a_redo = self._act("↷ Redo", self.redo, S.Redo, "Redo")
        self.a_find = self._act("🔍 Find…", self.find, S.Find, "Search text")
        self.a_find_next = self._act("Find next", lambda: self.step_hit(1), "F3")
        self.a_find_prev = self._act("Find previous", lambda: self.step_hit(-1), "Shift+F3")
        self.a_copy_page_text = self._act("Copy all text of page", self.copy_page_text)
        self.a_zoom_in = self._act("➕", lambda: self.step_zoom(1), S.ZoomIn, "Zoom in")
        self.a_zoom_out = self._act("➖", lambda: self.step_zoom(-1), S.ZoomOut, "Zoom out")
        self.a_fit = self._act("↔ Fit width", self.set_fit_width, "Ctrl+0", "Fit page width")
        self.a_actual = self._act("100%", lambda: self.set_zoom(1.0), "Ctrl+1")
        self.a_prev = self._act("◀", lambda: self.go(self.index - 1), "PgUp", "Previous page")
        self.a_next = self._act("▶", lambda: self.go(self.index + 1), "PgDown", "Next page")
        self.a_first = self._act("First page", lambda: self.go(0), "Home")
        self.a_last = self._act("Last page", lambda: self.go(self.pdf.page_count - 1 if self.pdf else 0), "End")
        self.a_goto = self._act("Go to page…", self.goto_dialog, "Ctrl+G")

        # annotate / sign / forms
        self.a_color_hl = self._act("Highlight colour…", lambda: self.pick_color("hl"))
        self.a_color_pen = self._act("Pen / box colour…", lambda: self.pick_color("pen"))
        self.a_color_text = self._act("Text colour…", lambda: self.pick_color("text"))
        self.a_new_sig = self._act("Create / change signature…", self.create_signature)
        self.a_forms = self._act("📝 Fill form…", self.fill_form, "Ctrl+Shift+F",
                                 "Fill the form fields of this PDF")
        self.a_flatten = self._act("Flatten (make permanent)", self.flatten,
                                   tip="Make annotations and form entries non-editable")

        # pages
        self.a_rot_r = self._act("⟳ Rotate right", lambda: self.rotate(90), "Ctrl+R", "Rotate page(s) right")
        self.a_rot_l = self._act("⟲ Rotate left", lambda: self.rotate(-90), "Ctrl+Shift+R", "Rotate page(s) left")
        self.a_del_pages = self._act("🗑 Delete page(s)", self.delete_pages, "Ctrl+Delete", "Delete selected pages")
        self.a_move_up = self._act("Move page up", lambda: self.move(-1), "Ctrl+Up")
        self.a_move_down = self._act("Move page down", lambda: self.move(1), "Ctrl+Down")
        self.a_blank = self._act("Insert blank page", self.insert_blank)
        self.a_dup = self._act("Duplicate page", self.duplicate_page)
        self.a_ins_pdf = self._act("Insert / merge PDF…", self.insert_pdf, tip="Add pages from another PDF")
        self.a_ins_img = self._act("Insert image as page…", self.insert_image_page)
        self.a_extract = self._act("Extract pages…", self.extract_pages, tip="Save some pages as a new PDF")
        self.a_split = self._act("Split every N pages…", self.split)

        self.tool_actions: dict[str, QAction] = {}
        for key, label, tip, sc in TOOLS:
            a = self._act(label, lambda checked=False, k=key: self.set_tool(k), sc, tip, True)
            self.tool_actions[key] = a

    def _build_menus(self):
        mb = self.menuBar()
        m = mb.addMenu("&File")
        m.addActions([self.a_open])
        self.recent_menu = m.addMenu("Open recent")
        self._fill_recent()
        m.addSeparator()
        m.addActions([self.a_save, self.a_save_as, self.a_print])
        m.addSeparator()
        m.addActions([self.a_close, self.a_quit])

        m = mb.addMenu("&Edit")
        m.addActions([self.a_undo, self.a_redo])
        m.addSeparator()
        m.addActions([self.a_find, self.a_find_next, self.a_find_prev, self.a_copy_page_text])

        m = mb.addMenu("&View")
        m.addActions([self.a_zoom_in, self.a_zoom_out, self.a_fit, self.a_actual])
        m.addSeparator()
        m.addActions([self.a_first, self.a_prev, self.a_next, self.a_last, self.a_goto])

        m = mb.addMenu("&Tools")
        for a in self.tool_actions.values():
            m.addAction(a)
        m.addSeparator()
        m.addActions([self.a_color_hl, self.a_color_pen, self.a_color_text])

        m = mb.addMenu("Fill && &Sign")
        m.addActions([self.a_forms, self.tool_actions["sign"], self.a_new_sig,
                      self.tool_actions["textbox"], self.a_flatten])

        m = mb.addMenu("&Pages")
        m.addActions([self.a_rot_r, self.a_rot_l, self.a_del_pages, self.a_move_up,
                      self.a_move_down])
        m.addSeparator()
        m.addActions([self.a_blank, self.a_dup, self.a_ins_pdf, self.a_ins_img])
        m.addSeparator()
        m.addActions([self.a_extract, self.a_split])

        m = mb.addMenu("&Help")
        self.a_about = self._act("About", self.about)
        m.addAction(self.a_about)

    def _build_toolbars(self):
        tb = QToolBar("Main")
        tb.setMovable(False)
        self.addToolBar(tb)
        tb.addActions([self.a_open, self.a_save, self.a_print])
        tb.addSeparator()
        tb.addActions([self.a_undo, self.a_redo])
        tb.addSeparator()
        tb.addAction(self.a_prev)
        self.page_box = QSpinBox()
        self.page_box.setMinimum(1)
        self.page_box.setKeyboardTracking(False)
        self.page_box.valueChanged.connect(lambda v: self.go(v - 1))
        tb.addWidget(self.page_box)
        self.page_total = QLabel(" / 0 ")
        tb.addWidget(self.page_total)
        tb.addAction(self.a_next)
        tb.addSeparator()
        tb.addActions([self.a_zoom_out])
        self.zoom_box = QComboBox()
        self.zoom_box.setEditable(True)
        self.zoom_box.addItems([f"{int(z * 100)}%" for z in ZOOM_STEPS])
        self.zoom_box.setMinimumWidth(80)
        self.zoom_box.lineEdit().returnPressed.connect(self._zoom_from_box)
        self.zoom_box.activated.connect(lambda _: self._zoom_from_box())
        tb.addWidget(self.zoom_box)
        tb.addActions([self.a_zoom_in, self.a_fit])
        tb.addSeparator()
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search…")
        self.search_box.setMaximumWidth(220)
        self.search_box.returnPressed.connect(self.run_search)
        tb.addWidget(self.search_box)
        tb.addActions([self.a_find_prev, self.a_find_next])
        tb.addSeparator()
        tb.addActions([self.a_forms, self.a_rot_r, self.a_del_pages])

        tb2 = QToolBar("Tools")
        tb2.setMovable(False)
        self.addToolBarBreak()
        self.addToolBar(tb2)
        for a in self.tool_actions.values():
            tb2.addAction(a)
        tb2.addSeparator()
        self.color_btn = QPushButton("Colours")
        self.color_btn.setToolTip("Highlight / pen / text colours")
        cmenu = QMenu(self)
        cmenu.addActions([self.a_color_hl, self.a_color_pen, self.a_color_text])
        self.color_btn.setMenu(cmenu)
        tb2.addWidget(self.color_btn)
        self.text_size = QSpinBox()
        self.text_size.setRange(6, 72)
        self.text_size.setValue(12)
        self.text_size.setPrefix("Text ")
        self.text_size.setSuffix(" pt")
        tb2.addWidget(self.text_size)

    # ------------------------------------------------------------------
    # File handling
    # ------------------------------------------------------------------
    def _fill_recent(self):
        self.recent_menu.clear()
        files = [f for f in (self.settings.value("recent", []) or []) if os.path.exists(f)]
        for f in files[:10]:
            a = self.recent_menu.addAction(os.path.basename(f))
            a.setToolTip(f)
            a.triggered.connect(lambda checked=False, p=f: self.open_file(p))
        self.recent_menu.setEnabled(bool(files))

    def _add_recent(self, path):
        files = [f for f in (self.settings.value("recent", []) or []) if f != path]
        files.insert(0, path)
        self.settings.setValue("recent", files[:10])
        self._fill_recent()

    def maybe_save(self) -> bool:
        if not self.pdf or not self.pdf.modified:
            return True
        r = QMessageBox.question(
            self, APP_NAME, "Save changes to this PDF?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard |
            QMessageBox.StandardButton.Cancel)
        if r == QMessageBox.StandardButton.Save:
            return self.save()
        return r == QMessageBox.StandardButton.Discard

    def open_dialog(self):
        f, _ = QFileDialog.getOpenFileName(self, "Open PDF",
                                           self.settings.value("lastdir", ""),
                                           "PDF files (*.pdf);;All files (*)")
        if f:
            self.open_file(f)

    def open_file(self, path: str):
        if not self.maybe_save():
            return
        try:
            pdf = PdfDocument(path)
        except Exception as ex:  # noqa: BLE001
            QMessageBox.critical(self, APP_NAME, f"Cannot open file:\n{ex}")
            return
        if pdf.needs_password():
            while True:
                pw, ok = QInputDialog.getText(self, APP_NAME, "This PDF is protected. Password:",
                                              QLineEdit.EchoMode.Password)
                if not ok:
                    pdf.close()
                    return
                if pdf.authenticate(pw):
                    break
        if self.pdf:
            self.pdf.close()
        self.pdf = pdf
        self.settings.setValue("lastdir", os.path.dirname(path))
        self._add_recent(path)
        self.index = 0
        self.search_hits = []
        self.fit_width = True
        self.reload(rebuild_thumbs=True)

    def save(self) -> bool:
        if not self.pdf:
            return False
        if not self.pdf.path:
            return self.save_as()
        try:
            self.pdf.save()
        except Exception as ex:  # noqa: BLE001
            QMessageBox.critical(self, APP_NAME, f"Save failed:\n{ex}\n\nTry 'Save as' "
                                 "with another name (the file may be open in another program).")
            return False
        self.statusBar().showMessage("Saved", 3000)
        self.update_ui()
        return True

    def save_as(self) -> bool:
        if not self.pdf:
            return False
        f, _ = QFileDialog.getSaveFileName(self, "Save PDF as", self.pdf.path or "",
                                           "PDF files (*.pdf)")
        if not f:
            return False
        if not f.lower().endswith(".pdf"):
            f += ".pdf"
        try:
            self.pdf.save(f)
        except Exception as ex:  # noqa: BLE001
            QMessageBox.critical(self, APP_NAME, f"Save failed:\n{ex}")
            return False
        self._add_recent(f)
        self.update_ui()
        return True

    def close_doc(self):
        if not self.maybe_save():
            return
        if self.pdf:
            self.pdf.close()
        self.pdf = None
        self.thumbs.clear()
        self.view.pixmap = None
        self.view.setFixedSize(0, 0)
        self.update_ui()

    def closeEvent(self, e):
        if self.maybe_save():
            e.accept()
        else:
            e.ignore()

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        for url in e.mimeData().urls():
            p = url.toLocalFile()
            if p.lower().endswith(".pdf"):
                self.open_file(p)
                break

    def print_doc(self):
        if not self.pdf:
            return
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        printer.setFromTo(1, self.pdf.page_count)
        dlg = QPrintDialog(printer, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        first = max(printer.fromPage(), 1) - 1
        last = (printer.toPage() or self.pdf.page_count) - 1
        painter = QPainter()
        if not painter.begin(printer):
            return
        dpi = min(printer.resolution(), 300)
        for i in range(first, last + 1):
            if i > first:
                printer.newPage()
            pix = self.pdf.render(i, dpi / 72)
            img = QImage(pix.samples, pix.width, pix.height, pix.stride,
                         QImage.Format.Format_RGB888)
            area = painter.viewport()
            size = img.size().scaled(area.size(), Qt.AspectRatioMode.KeepAspectRatio)
            painter.setViewport(area.x(), area.y(), size.width(), size.height())
            painter.setWindow(img.rect())
            painter.drawImage(0, 0, img)
            painter.setViewport(area)
        painter.end()

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------
    def reload(self, rebuild_thumbs=False, thumbs_for: list[int] | None = None):
        if not self.pdf:
            self.update_ui()
            return
        self.index = max(0, min(self.index, self.pdf.page_count - 1))
        self.render_page()
        if rebuild_thumbs:
            self.build_thumbs()
        elif thumbs_for:
            for i in thumbs_for:
                self._render_thumb(i)
        self.update_ui()

    def render_page(self):
        if not self.pdf:
            return
        page = self.pdf.page(self.index)
        if self.fit_width:
            avail = self.scroll.viewport().width() - 30
            self.zoom = max(0.2, min(5.0, avail / page.rect.width))
        dpr = self.devicePixelRatioF()
        pix = self.pdf.render(self.index, self.zoom * dpr)
        self.view.set_page(pixmap_to_qpixmap(pix, dpr), self.zoom)
        self.view.search_rects = [QRectF(r.x0, r.y0, r.width, r.height)
                                  for i, r in self.search_hits if i == self.index]
        cur = None
        if 0 <= self.search_pos < len(self.search_hits):
            i, r = self.search_hits[self.search_pos]
            if i == self.index:
                cur = QRectF(r.x0, r.y0, r.width, r.height)
        self.view.current_hit = cur
        self.view.update()
        self.zoom_box.setEditText(f"{round(self.zoom * 100)}%")

    def build_thumbs(self):
        self.thumbs.blockSignals(True)
        self.thumbs.clear()
        for i in range(self.pdf.page_count):
            it = QListWidgetItem(str(i + 1))
            it.setSizeHint(QSize(150, 170))
            self.thumbs.addItem(it)
        self.thumbs.setCurrentRow(self.index)
        self.thumbs.blockSignals(False)
        self._thumb_queue = list(range(self.pdf.page_count))
        if not self._building_thumbs:
            self._building_thumbs = True
            QTimer.singleShot(0, self._thumb_step)

    def _thumb_step(self):
        if not self.pdf or not self._thumb_queue:
            self._building_thumbs = False
            return
        for _ in range(4):
            if not self._thumb_queue:
                break
            self._render_thumb(self._thumb_queue.pop(0))
        QTimer.singleShot(0, self._thumb_step)

    def _render_thumb(self, i: int):
        if not self.pdf or i >= self.thumbs.count() or i >= self.pdf.page_count:
            return
        page = self.pdf.page(i)
        z = 150 / max(page.rect.width, page.rect.height)
        pix = self.pdf.render(i, z)
        self.thumbs.item(i).setIcon(QIcon(pixmap_to_qpixmap(pix, 1.0)))

    def update_ui(self):
        has = self.pdf is not None
        n = self.pdf.page_count if has else 0
        for a in self.findChildren(QAction):
            if a in (self.a_open, self.a_quit, self.a_about) or a.parent() is not self:
                continue
            a.setEnabled(has)
        self.recent_menu.setEnabled(True)
        if has:
            self.a_undo.setEnabled(self.pdf.can_undo())
            self.a_redo.setEnabled(self.pdf.can_redo())
            self.a_prev.setEnabled(self.index > 0)
            self.a_next.setEnabled(self.index < n - 1)
            self.page_box.blockSignals(True)
            self.page_box.setMaximum(n)
            self.page_box.setValue(self.index + 1)
            self.page_box.blockSignals(False)
            name = os.path.basename(self.pdf.path) if self.pdf.path else "Untitled"
            star = " •" if self.pdf.modified else ""
            self.setWindowTitle(f"{name}{star} - {APP_NAME}")
            self.page_label.setText(f"Page {self.index + 1} of {n}   |   {round(self.zoom * 100)}%")
            if self.thumbs.currentRow() != self.index:
                self.thumbs.blockSignals(True)
                self.thumbs.setCurrentRow(self.index)
                self.thumbs.blockSignals(False)
        else:
            self.setWindowTitle(APP_NAME)
            self.page_label.setText("Open a PDF (Ctrl+O) or drop one here")
        self.page_total.setText(f" / {n} ")
        self.page_box.setEnabled(has)
        self.zoom_box.setEnabled(has)
        self.search_box.setEnabled(has)

    def go(self, i: int):
        if not self.pdf:
            return
        i = max(0, min(i, self.pdf.page_count - 1))
        if i == self.index and self.view.pixmap is not None:
            return
        self.commit_ink()
        self.index = i
        self.render_page()
        self.scroll.verticalScrollBar().setValue(0)
        self.update_ui()

    def goto_dialog(self):
        if not self.pdf:
            return
        n, ok = QInputDialog.getInt(self, APP_NAME, "Go to page:", self.index + 1, 1,
                                    self.pdf.page_count)
        if ok:
            self.go(n - 1)

    def on_thumb_row(self, row: int):
        if row >= 0:
            self.go(row)

    def set_zoom(self, z: float):
        self.fit_width = False
        self.zoom = max(0.1, min(8.0, z))
        self.render_page()
        self.update_ui()

    def set_fit_width(self):
        self.fit_width = True
        self.render_page()
        self.update_ui()

    def step_zoom(self, d: int):
        cur = self.zoom
        if d > 0:
            nxt = next((z for z in ZOOM_STEPS if z > cur + 0.01), ZOOM_STEPS[-1])
        else:
            nxt = next((z for z in reversed(ZOOM_STEPS) if z < cur - 0.01), ZOOM_STEPS[0])
        self.set_zoom(nxt)

    def _zoom_from_box(self):
        t = self.zoom_box.currentText().replace("%", "").strip()
        try:
            self.set_zoom(float(t) / 100)
        except ValueError:
            pass

    def eventFilter(self, obj, e):
        # Ctrl + mouse wheel = zoom; wheel at page end = next page
        if obj is self.scroll.viewport() and e.type() == QEvent.Type.Wheel and self.pdf:
            if e.modifiers() & Qt.KeyboardModifier.ControlModifier:
                self.step_zoom(1 if e.angleDelta().y() > 0 else -1)
                return True
            v = self.scroll.verticalScrollBar()
            dy = e.angleDelta().y()
            if dy < 0 and v.value() >= v.maximum() and self.index < self.pdf.page_count - 1:
                self.go(self.index + 1)
                return True
            if dy > 0 and v.value() <= v.minimum() and self.index > 0:
                self.go(self.index - 1)
                v.setValue(v.maximum())
                return True
        if obj is self.scroll.viewport() and e.type() == QEvent.Type.Resize and self.fit_width and self.pdf:
            QTimer.singleShot(0, self.render_page)
        return super().eventFilter(obj, e)

    # ------------------------------------------------------------------
    # Search / copy
    # ------------------------------------------------------------------
    def find(self):
        self.search_box.setFocus()
        self.search_box.selectAll()

    def run_search(self):
        if not self.pdf:
            return
        text = self.search_box.text().strip()
        if not text:
            self.search_hits = []
            self.render_page()
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.search_hits = self.pdf.search(text)
        finally:
            QApplication.restoreOverrideCursor()
        if not self.search_hits:
            self.statusBar().showMessage(f"'{text}' not found", 4000)
            self.search_pos = -1
            self.render_page()
            return
        # first hit at or after current page
        self.search_pos = next((k for k, (i, _) in enumerate(self.search_hits)
                                if i >= self.index), 0)
        self.show_hit()

    def step_hit(self, d: int):
        if not self.search_hits:
            self.run_search()
            return
        self.search_pos = (self.search_pos + d) % len(self.search_hits)
        self.show_hit()

    def show_hit(self):
        i, r = self.search_hits[self.search_pos]
        self.index = i
        self.render_page()
        self.update_ui()
        self.scroll.ensureVisible(int(r.x0 * self.zoom), int(r.y0 * self.zoom), 50, 120)
        self.statusBar().showMessage(f"Result {self.search_pos + 1} of {len(self.search_hits)}", 4000)

    def copy_page_text(self):
        if self.pdf:
            QApplication.clipboard().setText(self.pdf.page(self.index).get_text())
            self.statusBar().showMessage("Page text copied", 3000)

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------
    def set_tool(self, key: str):
        self.commit_ink()
        for k, a in self.tool_actions.items():
            a.setChecked(k == key)
        self.view.set_tool(key)
        self.view.ink_color = self.pen_color
        tip = next(t[2] for t in TOOLS if t[0] == key)
        self.statusBar().showMessage(tip)

    def commit_ink(self):
        # strokes are saved as soon as the mouse is released; drop any leftover
        self.view.take_strokes()

    def on_ink(self, strokes: list):
        if self.pdf and strokes:
            self.pdf.ink(self.index, strokes, qcolor_to_pdf(self.pen_color), 2)
            self.after_edit()

    def pick_color(self, which: str):
        cur = {"hl": self.color, "pen": self.pen_color, "text": self.text_color}[which]
        c = QColorDialog.getColor(cur, self)
        if not c.isValid():
            return
        if which == "hl":
            self.color = c
        elif which == "pen":
            self.pen_color = c
            self.view.ink_color = c
        else:
            self.text_color = c

    def after_edit(self, rebuild_thumbs=False):
        self.reload(rebuild_thumbs=rebuild_thumbs,
                    thumbs_for=None if rebuild_thumbs else [self.index])

    def _fitz_rect(self, r: QRectF) -> fitz.Rect:
        return fitz.Rect(r.x(), r.y(), r.x() + r.width(), r.y() + r.height())

    def on_rect(self, r: QRectF):
        if not self.pdf:
            return
        tool = self.view.tool
        rect = self._fitz_rect(r)
        i = self.index
        try:
            if tool == "select":
                page = self.pdf.page(i)
                text = page.get_textbox(to_page_rect(page, rect)).strip()
                if text:
                    QApplication.clipboard().setText(text)
                    self.statusBar().showMessage(f"Copied: {text[:80]}", 4000)
                else:
                    self.statusBar().showMessage("No text there (scanned page?)", 4000)
                return
            if tool == "highlight":
                n = self.pdf.highlight(i, rect, qcolor_to_pdf(self.color))
            elif tool == "underline":
                n = self.pdf.underline(i, rect, qcolor_to_pdf(self.pen_color))
                if not n:
                    self.statusBar().showMessage("No text there", 3000)
                    return
            elif tool == "strike":
                n = self.pdf.strikeout(i, rect, qcolor_to_pdf(self.pen_color))
                if not n:
                    self.statusBar().showMessage("No text there", 3000)
                    return
            elif tool == "rect":
                self.pdf.rectangle(i, rect, qcolor_to_pdf(self.pen_color))
            elif tool == "textbox":
                text, ok = QInputDialog.getMultiLineText(self, "Text box", "Text:")
                if not ok or not text.strip():
                    return
                self.pdf.text_box(i, rect, text, self.text_size.value(),
                                  qcolor_to_pdf(self.text_color))
            elif tool == "sign":
                png = self.load_signature()
                if not png:
                    return
                self.pdf.insert_image(i, rect, png)
            elif tool == "image":
                f, _ = QFileDialog.getOpenFileName(self, "Image", "",
                                                   "Images (*.png *.jpg *.jpeg *.bmp *.gif)")
                if not f:
                    return
                with open(f, "rb") as fh:
                    self.pdf.insert_image(i, rect, fh.read())
            elif tool == "whiteout":
                r2 = QMessageBox.question(
                    self, APP_NAME, "Permanently erase everything in this area?\n"
                    "(You can still use Undo before saving.)")
                if r2 != QMessageBox.StandardButton.Yes:
                    return
                self.pdf.erase_area(i, rect)
        except Exception as ex:  # noqa: BLE001
            QMessageBox.critical(self, APP_NAME, f"That did not work:\n{ex}")
            return
        self.after_edit()

    def on_click(self, p: QPointF):
        if not self.pdf:
            return
        x, y = p.x(), p.y()
        i = self.index
        tool = self.view.tool
        try:
            if tool == "note":
                text, ok = QInputDialog.getMultiLineText(self, "Note", "Note text:")
                if not ok or not text.strip():
                    return
                self.pdf.note(i, x, y, text)
            elif tool == "eraser":
                if not self.pdf.delete_annot_at(i, x, y):
                    self.statusBar().showMessage(
                        "No annotation here. To remove page content, use White-out.", 4000)
                    return
            elif tool == "edit":
                span = self.pdf.span_at(i, x, y)
                if not span:
                    self.statusBar().showMessage(
                        "No editable text here (it may be an image or a scan).", 4000)
                    return
                dlg = EditTextDialog(span["text"], span["size"], rgb(span["color"]), self)
                if dlg.exec() != QDialog.DialogCode.Accepted:
                    return
                new = dlg.edit.text()
                if new == span["text"] and abs(dlg.size.value() - span["size"]) < 0.05 \
                        and dlg.color == QColor.fromRgbF(*rgb(span["color"])):
                    return
                self.pdf.replace_span_text(i, span, new, dlg.size.value(),
                                           qcolor_to_pdf(dlg.color))
        except Exception as ex:  # noqa: BLE001
            QMessageBox.critical(self, APP_NAME, f"That did not work:\n{ex}")
            return
        self.after_edit()

    # ------------------------------------------------------------------
    # Signature & forms
    # ------------------------------------------------------------------
    def load_signature(self) -> bytes | None:
        p = signature_path()
        if os.path.exists(p):
            with open(p, "rb") as f:
                return f.read()
        return self.create_signature()

    def create_signature(self) -> bytes | None:
        dlg = SignatureDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.png:
            return None
        with open(signature_path(), "wb") as f:
            f.write(dlg.png)
        self.statusBar().showMessage("Signature saved. Use ✍ Sign and drag where it goes.", 5000)
        return dlg.png

    def fill_form(self):
        if not self.pdf:
            return
        fields = self.pdf.form_fields()
        dlg = FormDialog(fields, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        vals = dlg.values()
        if vals:
            n = self.pdf.set_form_values(vals)
            self.statusBar().showMessage(f"{n} field(s) filled", 4000)
            self.after_edit(rebuild_thumbs=True)

    def flatten(self):
        if not self.pdf:
            return
        r = QMessageBox.question(self, APP_NAME,
                                 "Make all annotations and form entries permanent?\n"
                                 "They will no longer be editable.")
        if r == QMessageBox.StandardButton.Yes:
            try:
                self.pdf.flatten_forms()
            except Exception as ex:  # noqa: BLE001
                QMessageBox.critical(self, APP_NAME, f"Flatten failed:\n{ex}")
                return
            self.after_edit(rebuild_thumbs=True)

    # ------------------------------------------------------------------
    # Pages
    # ------------------------------------------------------------------
    def selected_pages(self) -> list[int]:
        rows = sorted({self.thumbs.row(it) for it in self.thumbs.selectedItems()})
        return rows or [self.index]

    def rotate(self, deg: int):
        pages = self.selected_pages()
        for k, i in enumerate(pages):
            self.pdf.rotate_page(i, deg)
        self.after_edit(rebuild_thumbs=False)
        for i in pages:
            self._render_thumb(i)

    def delete_pages(self):
        pages = self.selected_pages()
        if len(pages) >= self.pdf.page_count:
            QMessageBox.warning(self, APP_NAME, "You can't delete every page.")
            return
        txt = ", ".join(str(p + 1) for p in pages)
        if QMessageBox.question(self, APP_NAME, f"Delete page(s) {txt}?") != \
                QMessageBox.StandardButton.Yes:
            return
        self.pdf.delete_pages(pages)
        self.index = min(pages[0], self.pdf.page_count - 1)
        self.after_edit(rebuild_thumbs=True)

    def move(self, d: int):
        dest = self.index + d
        if not 0 <= dest < self.pdf.page_count:
            return
        self.pdf.move_page(self.index, dest)
        self.index = dest
        self.after_edit(rebuild_thumbs=True)

    def on_thumbs_moved(self, _parent, start, end, _dest_parent, row):
        if not self.pdf:
            return
        if start != end:
            QTimer.singleShot(0, lambda: self.after_edit(rebuild_thumbs=True))
            return
        dest = row if row < start else row - 1
        if dest == start:
            return
        self.pdf.move_page(start, dest)
        self.index = dest
        QTimer.singleShot(0, lambda: self.after_edit(rebuild_thumbs=True))

    def insert_blank(self):
        self.pdf.insert_blank(self.index)
        self.index += 1
        self.after_edit(rebuild_thumbs=True)

    def duplicate_page(self):
        self.pdf.duplicate_page(self.index)
        self.index += 1
        self.after_edit(rebuild_thumbs=True)

    def insert_pdf(self):
        f, _ = QFileDialog.getOpenFileName(self, "PDF to insert", "", "PDF files (*.pdf)")
        if not f:
            return
        where = QMessageBox.question(
            self, APP_NAME, f"Insert after page {self.index + 1}?\n"
            "(No = add at the end)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No |
            QMessageBox.StandardButton.Cancel)
        if where == QMessageBox.StandardButton.Cancel:
            return
        after = self.index if where == QMessageBox.StandardButton.Yes else self.pdf.page_count - 1
        try:
            n = self.pdf.insert_pdf(f, after)
        except Exception as ex:  # noqa: BLE001
            QMessageBox.critical(self, APP_NAME, f"Cannot insert:\n{ex}")
            return
        self.index = after + 1
        self.statusBar().showMessage(f"{n} page(s) inserted", 4000)
        self.after_edit(rebuild_thumbs=True)

    def insert_image_page(self):
        f, _ = QFileDialog.getOpenFileName(self, "Image to add as a page", "",
                                           "Images (*.png *.jpg *.jpeg *.bmp *.tif *.tiff)")
        if not f:
            return
        try:
            self.pdf.insert_image_page(f, self.index)
        except Exception as ex:  # noqa: BLE001
            QMessageBox.critical(self, APP_NAME, f"Cannot insert image:\n{ex}")
            return
        self.index += 1
        self.after_edit(rebuild_thumbs=True)

    def extract_pages(self):
        sel = self.selected_pages()
        default = ",".join(str(p + 1) for p in sel)
        text, ok = QInputDialog.getText(self, "Extract pages",
                                        "Pages to extract (e.g. 1-3, 5, 8-):", text=default)
        if not ok:
            return
        try:
            pages = parse_page_ranges(text, self.pdf.page_count)
        except ValueError as ex:
            QMessageBox.warning(self, APP_NAME, str(ex))
            return
        f, _ = QFileDialog.getSaveFileName(self, "Save extracted pages", "", "PDF files (*.pdf)")
        if not f:
            return
        if not f.lower().endswith(".pdf"):
            f += ".pdf"
        self.pdf.extract_pages(pages, f)
        self.statusBar().showMessage(f"{len(pages)} page(s) saved to {f}", 5000)

    def split(self):
        n, ok = QInputDialog.getInt(self, "Split", "Pages per file:", 1, 1,
                                    max(1, self.pdf.page_count))
        if not ok:
            return
        f, _ = QFileDialog.getSaveFileName(self, "Base file name", "", "PDF files (*.pdf)")
        if not f:
            return
        paths = self.pdf.split_every(n, f)
        QMessageBox.information(self, APP_NAME, f"Created {len(paths)} file(s):\n" +
                                "\n".join(os.path.basename(p) for p in paths[:20]))

    # ------------------------------------------------------------------
    def undo(self):
        if self.pdf and self.pdf.undo():
            self.after_edit(rebuild_thumbs=True)

    def redo(self):
        if self.pdf and self.pdf.redo():
            self.after_edit(rebuild_thumbs=True)

    def about(self):
        QMessageBox.about(self, APP_NAME,
                          f"<b>{APP_NAME}</b><br>PDF reader &amp; editor.<br><br>"
                          f"PyMuPDF {fitz.VersionBind}<br>"
                          "Open, search, annotate, fill &amp; sign, edit text, manage pages.")


def main():
    if sys.platform == "win32":
        try:  # own taskbar icon instead of the generic Python one
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Apex.ApexPDF")
        except Exception:  # noqa: BLE001
            pass
    app = QApplication(sys.argv)
    app.setApplicationName("ApexPDF")
    app.setOrganizationName("Apex")
    app.setWindowIcon(app_icon())
    w = MainWindow()
    w.show()
    if len(sys.argv) > 1 and os.path.exists(sys.argv[1]):
        w.open_file(sys.argv[1])
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
