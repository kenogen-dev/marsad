"""The Chain screen: a rig drawn on a grid.

Drag a part from the list onto the grid. To wire, drag from a part's dot, or from
one of the rings that appear round a part when the pointer is over it, onto another
part; a wire leaves from whichever side faces the part it goes to. Cables (coax,
jumpers, USB) live on the wires: drop one from the Cables list onto a wire, or click
a wire to give it cables. Drag a part's
bottom-right corner to resize it. Click a part to edit it; Delete takes the selected
part or wire out of the drawing. Tidy arranges the rig; Undo and Redo step through
every change. Boxes can be stacked (Arrange menu, right-click, Ctrl+] and friends) and
grouped (Ctrl+G): a click on a grouped box takes the whole group, a second click the one
box. Stacking and groups are drawing only, like enclosures. Nothing reaches chain.json until Save, and then only through the service.

Relay 005 made it a diagram editor: copy, cut, paste and duplicate, arrow-key nudges, smart
guides while dragging, align and distribute, lock, wheel zoom and Space+drag panning, a
shortcut sheet (F1, drawn from SHORTCUTS), a PNG picture on request, a Checks strip, power
sources and power wires, the owner's own parts library (My parts) and a nominal gain/loss
estimate per path. Power, figures, locks and the library are drawing only: they live in
chain_layout.json beside the rigs, and the path walk that makes the recorded text never sees them.
"""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path

from PySide6.QtCore import (QEasingCurve, QLineF, QMimeData, QParallelAnimationGroup, QPoint,
                            QPointF, QPropertyAnimation, QRectF, QSettings, QSize, Qt, QTimer, QVariantAnimation,
                            Signal)
from PySide6.QtGui import (QAction, QActionGroup, QBrush, QColor, QCursor, QDrag, QFont, QFontMetricsF, QIcon,
                           QImage, QKeySequence, QTransform,
                           QPainter, QPainterPath, QPainterPathStroker, QPen, QPixmap, QShortcut)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QColorDialog, QComboBox, QDialog,
                               QSizePolicy, QToolButton, QWidgetAction,
                               QFileDialog, QFrame,
                               QGraphicsEllipseItem, QGraphicsItem, QGraphicsLineItem,
                               QGraphicsObject, QGraphicsPathItem, QGraphicsRectItem, QGraphicsScene, QGraphicsView,
                               QGridLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMenu, QMessageBox, QPushButton, QScrollArea,
                               QSpinBox, QSplitter, QStyle, QStyledItemDelegate, QVBoxLayout, QWidget)

from marsad import chain as engine_chain

from . import geometry as G
from . import rig as R
from . import theme as T
from .service import Service, ServiceError

MIME = "application/x-marsad-part"
PORT_R = 6
GRIP = 14                      # px, the resize corner
UNDO_MAX = 200
DERIVED = ("text", "tail", "receiver", "source", "needs_confirm")
# The palette's order, as the design lists it; types not named here follow.
PALETTE_ORDER = ["antenna", "probe", "load", "adapter", "dcblock", "attenuator", "bulkhead_sma",
                 "coax", "filter", "lna", "jumper", R.SPLITTER, "sdr", "usb", "bulkhead_usb",
                 "isolator", "pc", "custom"]
LENGTH_TYPES = {"coax", "jumper", "usb"}
# Colour coding: what each kind of part does. Custom parts stay neutral. Relay 006: each section
# of the Parts list has a hue of its own (SMA parts were sharing the cables' blue before).
CATEGORY = {"antenna": "source", "probe": "source", "load": "source",
            "adapter": "sma", "dcblock": "sma", "attenuator": "sma", "bulkhead_sma": "sma",
            "coax": "cable", "jumper": "cable",
            "filter": "condition", "lna": "condition",
            R.SPLITTER: "split", "sdr": "radio", R.ENCLOSURE: "enclosure",
            "usb": "digital", "bulkhead_usb": "digital", "isolator": "digital", "pc": "digital"}
CATEGORY_NAMES = [("source", "Sources"), ("sma", "SMA adapters, blocks, pads, bulkheads"),
                  ("condition", "Filters and amplifiers"), ("split", "Splitters"),
                  ("radio", "Radios"), ("digital", "USB parts and USB cables"),
                  ("enclosure", "Enclosures (drawing only)")]
PORT_HIT = 11                  # px (scene), how near a connection point a press must be to start a wire
PIPE_W = 22                    # px, an adapter's pipe
MATE_TOL = 22                  # px (scene), how near two connection points must be dropped to mate
MAGNET = 30                    # px (scene), how near a dragged part's point is pulled onto a matching point
CLICK_TOL = 4                  # px (screen): a press and release closer than this is a click, not a drag
GRID_DEFAULTS = {"style": "dots", "major": True, "dotted": False}
CABLE_MIME_PREFIX = "cable:"
FRAGMENT_MIME = "application/x-marsad-rig-fragment"
# Every keyboard and mouse shortcut, in one table: the help sheet (F1) is drawn from it.
# How each is wired: "window" = a window shortcut, "grid" = handled by the grid itself (so a
# text box on the card keeps its own Ctrl+C or arrow keys), "menu" = on a menu entry,
# "mouse" = a pointer gesture. The window check script tests that every key listed works.
SHORTCUTS = [
    ("Editing", "Ctrl+Z", "Undo", "window"),
    ("Editing", "Ctrl+Y   ·   Ctrl+Shift+Z", "Redo", "window"),
    ("Editing", "Ctrl+C", "Copy the selected boxes, with the wires between them", "grid"),
    ("Editing", "Ctrl+X", "Cut: copy, then take them out of the drawing", "grid"),
    ("Editing", "Ctrl+V", "Paste at the pointer, or beside the originals", "grid"),
    ("Editing", "Ctrl+D", "Duplicate", "grid"),
    ("Editing", "Delete   ·   Backspace", "Take the selection out of the drawing (Undo brings it back)", "grid"),
    ("Selecting", "Drag on empty grid", "Select everything inside a rectangle", "mouse"),
    ("Selecting", "Ctrl+click   ·   Shift+click", "Add a box to the selection, or take it out", "mouse"),
    ("Selecting", "Ctrl+A", "Select everything", "grid"),
    ("Selecting", "Esc", "Clear the selection; stop drawing a wire", "grid"),
    ("Moving", "Arrow keys", "Nudge the selection one grid square", "grid"),
    ("Moving", "Shift + arrow keys", "Nudge five grid squares", "grid"),
    ("Moving", "Ctrl+Shift+L", "Lock or unlock the selection", "window"),
    ("Arranging", "Ctrl+Shift+]", "Bring to front", "window"),
    ("Arranging", "Ctrl+]", "Bring forward", "window"),
    ("Arranging", "Ctrl+[", "Send backward", "window"),
    ("Arranging", "Ctrl+Shift+[", "Send to back", "window"),
    ("Arranging", "Ctrl+G", "Group", "window"),
    ("Arranging", "Ctrl+Shift+G", "Ungroup", "window"),
    ("Arranging", "Arrange menu", "Align left, centre, right, top, middle, bottom; distribute", "menu"),
    ("Parts", "Click a part", "Open its card: name, note, fields, colour, shape, text position", "mouse"),
    ("Parts", "Drag a part", "Move it (a drag opens nothing)", "mouse"),
    ("Parts", "Right-click a part", "The format and arrange menu: text position, shape, colour, stacking, align, "
                                    "lock, groups, copy and paste", "mouse"),
    ("Wiring", "Drag from a dot", "Wire it to the part you let go over (from an input dot too)", "mouse"),
    ("Wiring", "Drop an SMA part on a dot", "Mate it directly, port to port, with no wire", "mouse"),
    ("View", "Wheel", "Zoom round the pointer", "mouse"),
    ("View", "Shift + wheel", "Scroll up and down", "mouse"),
    ("View", "Space + drag   ·   middle-button drag", "Pan", "mouse"),
    ("View", "Ctrl++   ·   Ctrl+-", "Zoom in, zoom out", "menu"),
    ("View", "Ctrl+0", "Zoom to fit", "menu"),
    ("View", "Ctrl+2", "Zoom to the selection", "menu"),
    ("View", "Ctrl+1", "Actual size", "menu"),
    ("Help", "F1", "This sheet", "window"),
]


def cable_colour(type_: str | None) -> QColor:
    return T.color("cat_digital") if type_ == "usb" else T.color("cat_cable")


def palette_colour(cat: str | None, on: bool = True) -> QColor:
    """The colour of a Parts-list section or a part's kind, worked out when it is painted (never
    stored), so turning colour coding off and on can never leave a stale colour behind."""
    if not on or not cat:
        return T.color("muted")
    if cat == R.POWER:
        return T.color("power")
    if cat in ("coax", "jumper", "usb"):
        return cable_colour(cat)
    return category_colour(cat)


def cable_summary(c: dict) -> str:
    bits = [str(c.get("name") or "").strip() or c.get("type", "cable")]
    if c.get("length") not in (None, ""):
        bits.append(str(c["length"]))
    return " · ".join(bits)


def route_path(r: tuple) -> QPainterPath:
    """A geometry.route result as a path; right-angle corners are rounded a little."""
    if r[0] == "cubic":
        raise ValueError("cubic routes need their end points")
    pts = [QPointF(*p) for p in r[1]]
    path = QPainterPath(pts[0])
    for i in range(1, len(pts) - 1):
        prev, here, nxt = pts[i - 1], pts[i], pts[i + 1]
        d1 = QLineF(here, prev)
        d2 = QLineF(here, nxt)
        rad = min(10.0, d1.length() / 2, d2.length() / 2)
        path.lineTo(d1.pointAt(rad / d1.length()) if d1.length() else here)
        path.quadTo(here, d2.pointAt(rad / d2.length()) if d2.length() else here)
    path.lineTo(pts[-1])
    return path


def wire_path(a: tuple, b: tuple, style: str, side_b: str | None = None) -> QPainterPath:
    r = G.route(a, b, style, side_b)
    if r[0] == "cubic":
        path = QPainterPath(QPointF(a[0], a[1]))
        path.cubicTo(QPointF(*r[1]), QPointF(*r[2]), QPointF(b[0], b[1]))
        return path
    return route_path(r)


def _when(stamp: str | None) -> str:
    return stamp.replace("T", " ")[:16] if stamp else ""


def prefs() -> QSettings:
    """The window's own preferences (grid look, panel sizes), in one small file in
    the user's home folder, never in the registry and never in the station."""
    return QSettings(str(Path.home() / ".marsad_desktop.ini"), QSettings.Format.IniFormat)


def category_colour(cat: str | None) -> QColor:
    if cat == "radio":
        return T.color("survey")
    return T.color("cat_" + cat) if cat else T.color("muted")


def not_yet_a_drag(item, ev) -> bool:
    """True while the pointer has moved less than CLICK_TOL from where the button went down: the box
    stays where it is, so a slightly shaky click is still a click and never nudges anything."""
    at = getattr(item, "_press_at", None)
    if at is None:
        return False
    if (ev.screenPos() - at).manhattanLength() < CLICK_TOL:
        return True
    item._press_at = None                       # past the threshold once: from here on it is a drag
    return False


def is_click(item, ev) -> bool:
    """A left press and release that never got as far as a drag."""
    at = getattr(item, "_press_at", "dragged")
    item._press_at = None
    return at not in (None, "dragged") and ev.button() == Qt.MouseButton.LeftButton


def card_place(part: tuple, card: tuple, view: tuple, gap: int = 16) -> tuple[int, int]:
    """Where the edit card goes (relay 007): beside the part, never over it, so the owner sees what
    he is editing. Right of it if there is room, else left, else below, else above; inside the grid.
    If no side has room, the side that covers least of the part."""
    (px, py, pw, ph), (cw, ch), (vw, vh) = part, card, view
    m = 8

    def clamp(x, y):
        return min(max(m, x), max(m, vw - cw - m)), min(max(m, y), max(m, vh - ch - m))

    tries = [clamp(px + pw + gap, py), clamp(px - gap - cw, py), clamp(px, py + ph + gap), clamp(px, py - gap - ch)]

    def covered(pos):
        x, y = pos
        ox = max(0, min(x + cw, px + pw) - max(x, px))
        oy = max(0, min(y + ch, py + ph) - max(y, py))
        return ox * oy

    for pos in tries:
        if covered(pos) == 0:
            return int(pos[0]), int(pos[1])
    return tuple(int(v) for v in min(tries, key=covered))


def node_look(node: dict, screen) -> tuple:
    """A part's colours, (fill, edge, kind label, name, note), from the current settings: the
    owner's own colour first (text light or dark, whichever keeps 4.5:1), else the radio's role,
    else its kind's colour as a tint when colour coding is on, else plain."""
    p = node["part"]
    own = node.get("colour")
    if own and T.is_hex(own):
        main, soft = T.text_on(own)
        return QColor(own), QColor(T.mix(own, main, 0.35)), QColor(soft), QColor(main), QColor(soft)
    if p["type"] == "sdr":
        role = screen.role_of(p)
        if role == "recorder":
            return T.color("recorder_bg"), T.color("recorder"), T.color("recorder_light"), T.color("text"), \
                T.color("muted")
        return T.color("survey_bg"), T.color("survey"), T.color("survey_light"), T.color("text"), T.color("muted")
    cat = CATEGORY.get(p["type"])
    if screen.colour_on and cat:
        cat_hex = T.C["cat_" + cat]
        fill = T.mix(T.C["raised"], cat_hex, 0.12)
        return (QColor(fill), T.color("line_strong"), QColor(T.readable_on(cat_hex, fill)), T.color("text"),
                QColor(T.readable_on(T.C["muted"], fill)))
    return T.color("raised"), T.color("line_strong"), T.color("muted"), T.color("text"), T.color("muted")


def box_colour_menu(menu: QMenu, screen, ids: list[str]) -> None:
    """The Colour submenu: palette swatches, a custom colour, and back to the kind colour."""
    sub = Menu(menu)
    sub.setTitle("Colour")
    sub.setToolTipsVisible(True)
    for name, hexv in T.SWATCHES:
        a = sub.addAction(dot_icon(QColor(hexv)), name)
        a.triggered.connect(lambda _=False, h=hexv: screen.set_colour(ids, h))
    sub.addSeparator()
    a = sub.addAction("Custom colour…")
    a.triggered.connect(lambda: screen.pick_colour(ids))
    a = sub.addAction("Use kind colour")
    a.setEnabled(any(R.box(screen.rig, i) and R.box(screen.rig, i).get("colour") for i in ids))
    a.setToolTip("Nothing selected has a colour of its own" if not a.isEnabled() else
                 "Go back to the colour of its kind")
    a.triggered.connect(lambda: screen.set_colour(ids, None))
    menu.addMenu(sub)


def dot_icon(colour: QColor) -> QIcon:
    pm = QPixmap(12, 12)
    pm.fill(Qt.GlobalColor.transparent)
    qp = QPainter(pm)
    qp.setRenderHint(QPainter.RenderHint.Antialiasing)
    qp.setPen(Qt.PenStyle.NoPen)
    qp.setBrush(colour)
    qp.drawEllipse(1, 1, 10, 10)
    qp.end()
    return QIcon(pm)


# ---------------------------------------------------------------- menus (relay 005)
class Menu(QMenu):
    """A menu that draws its own rows: the label on the left, the shortcut right-aligned
    in its own muted column, headings as quiet small capitals and separators between groups.
    (A style sheet can only colour label and shortcut alike, so the rows are painted here;
    the sheet still sets their size.) Entries show their tooltips: a disabled one says why."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.setToolTipsVisible(True)
        # Faintly see-through (relay 007): the window draws its own rounded, 92 % opaque body. Where
        # Windows will not compose a translucent popup, Qt paints it on black and it simply looks solid.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setWindowFlag(Qt.WindowType.NoDropShadowWindowHint)

    def add_heading(self, text: str) -> QAction:
        if self.actions():
            self.addSeparator()
        a = QAction(text, self)
        a.setEnabled(False)
        a.setData("heading")
        self.addAction(a)
        return a

    def paintEvent(self, ev):
        qp = QPainter(self)
        qp.setRenderHint(QPainter.RenderHint.Antialiasing)
        qp.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        qp.fillRect(self.rect(), Qt.GlobalColor.transparent)
        qp.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        qp.setPen(QPen(T.color("line_strong"), T.BORDER))
        qp.setBrush(T.color("menu", T.FLOAT_ALPHA))
        qp.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 6, 6)
        pad = T.SPACE * 2
        for a in self.actions():
            r = self.actionGeometry(a)
            if not a.isVisible() or not r.isValid():
                continue
            if a.isSeparator():
                qp.setPen(QPen(T.color("lines"), T.BORDER))
                y = r.center().y()
                qp.drawLine(r.left() + T.SPACE + 2, y, r.right() - T.SPACE - 2, y)
                continue
            label, _, keys = a.text().partition("\t")
            label = label.replace("&&", "\0").replace("&", "").replace("\0", "&")
            if a.data() == "heading":
                qp.setPen(T.color("muted"))
                qp.setFont(T.ui_font(7.5, QFont.Weight.DemiBold))
                qp.drawText(r.adjusted(pad, 2, -pad, 0), int(Qt.AlignmentFlag.AlignVCenter), label.upper())
                continue
            keys = keys or a.shortcut().toString(QKeySequence.SequenceFormat.NativeText)
            if a is self.activeAction() and a.isEnabled():
                qp.setPen(Qt.PenStyle.NoPen)
                qp.setBrush(T.color("menu_hover"))
                qp.drawRoundedRect(QRectF(r).adjusted(T.SPACE - 2, 1, -(T.SPACE - 2), -1), 5, 5)
            on = a.isEnabled()
            qp.setFont(self.font())
            qp.setPen(T.color("text") if on else T.color("faint"))
            qp.drawText(r.adjusted(T.SPACE * 4 + 2, 0, -pad, 0),
                        int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), label)
            if keys:
                qp.setPen(T.color("muted") if on else T.color("faint"))
                qp.drawText(r.adjusted(0, 0, -pad, 0),
                            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight), keys)
            if a.isCheckable() and a.isChecked():
                qp.setPen(QPen(T.color("survey"), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                cy = r.center().y()
                x0 = r.left() + T.SPACE + 5
                qp.drawPolyline([QPointF(x0, cy), QPointF(x0 + 4, cy + 4), QPointF(x0 + 11, cy - 4)])
            if a.menu() is not None:
                qp.setPen(T.color("muted"))
                qp.drawText(r.adjusted(0, 0, -T.SPACE, 0),
                            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight), "›")
        qp.end()


# ---------------------------------------------------------------- titles and the text-position picker (relay 008)
class ElidedLabel(QLabel):
    """A one-line title that shortens with an ellipsis when its room is narrow, never pushing into
    or under the controls beside it; the whole text is then in its tooltip."""

    def __init__(self, text: str = "", tip: str = ""):
        super().__init__()
        self._full = ""
        self._tip = tip
        self.setMinimumWidth(10)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setText(text)

    def setText(self, text: str):
        self._full = text or ""
        self.updateGeometry()
        self.update()
        self._refresh_tip()

    def text(self) -> str:
        return self._full

    def _refresh_tip(self):
        cut = self.fontMetrics().horizontalAdvance(self._full) > self.contentsRect().width()
        self.setToolTip(self._full if cut and self._full else self._tip)

    def sizeHint(self):
        return QSize(self.fontMetrics().horizontalAdvance(self._full) + 4, super().sizeHint().height())

    def minimumSizeHint(self):
        return QSize(10, super().minimumSizeHint().height())

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._refresh_tip()

    def paintEvent(self, ev):
        qp = QPainter(self)
        qp.setFont(self.font())
        qp.setPen(self.palette().color(self.foregroundRole()))
        r = self.contentsRect()
        qp.drawText(r, int(self.alignment() | Qt.AlignmentFlag.AlignVCenter),
                    self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideRight, r.width()))
        qp.end()


class PosPicker(QWidget):
    """Nine small squares, three by three: where a node's words sit (relay 008)."""
    chosen = Signal(str)

    def __init__(self, current: str, parent=None):
        super().__init__(parent)
        lay = QGridLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(3)
        self.buttons = {}
        for k, code in enumerate(R.TEXT_POS):
            b = QToolButton()
            b.setObjectName("PosCell")
            b.setCheckable(True)
            b.setChecked(code == current)
            b.setFixedSize(20, 16)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.setToolTip(f"Text {R.TEXT_POS_NAMES[code[0]]} {R.TEXT_POS_NAMES[code[1]]}")
            b.setStyleSheet(f"QToolButton#PosCell {{ background: {T.C['bar']}; border: 1px solid {T.C['line_strong']};"
                            f" border-radius: 3px; }} QToolButton#PosCell:hover {{ border-color: {T.C['text2']}; }}"
                            f" QToolButton#PosCell:checked {{ background: {T.C['survey']}; border-color: {T.C['survey']}; }}")
            b.clicked.connect(lambda _=False, c=code: self.chosen.emit(c))
            lay.addWidget(b, k // 3, k % 3)
            self.buttons[code] = b
        lay.setColumnStretch(3, 1)


# ---------------------------------------------------------------- shapes and handles (relay 007)
_OUTLINES: dict = {}


def outline_path(shape: str, w: float, h: float) -> QPainterPath:
    """A node's outline in its own box, made once per shape and size (painting asks constantly)."""
    key = (shape, round(w), round(h))
    path = _OUTLINES.get(key)
    if path is None:
        path = QPainterPath()
        r6 = round(T.RADIUS * 0.75)
        poly = G.outline(shape, w, h)
        if shape == "circle":
            path.addEllipse(QRectF(0.5, 0.5, w - 1, h - 1))
        elif shape == "pill":
            path.addRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), h / 2, h / 2)
        elif poly:
            path.moveTo(*poly[0])
            for pt in poly[1:]:
                path.lineTo(*pt)
            path.closeSubpath()
        elif shape == "rect":
            path.addRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 3, 3)
        else:                                   # rounded, and anything else
            path.addRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), r6, r6)
        _OUTLINES[key] = path
    return path


def text_box(shape: str, w: float, h: float) -> QRectF:
    """Where a node's words go inside its shape (left, top, width): inside the shape's widest part."""
    if shape == "triangle":
        return QRectF(10, 0, w * 0.56, h)
    if shape == "circle":
        return QRectF(w * 0.16, 0, w * 0.68, h)
    if shape == "hexagon":
        i = min(w * 0.2, h * 0.5)
        return QRectF(i * 0.7, 0, w - i * 1.4, h)
    if shape == "pill":
        return QRectF(min(h / 2, w / 4) * 0.7 + 4, 0, w - min(h / 2, w / 4) * 1.4 - 8, h)
    if shape == "trapezoid":
        return QRectF(w * 0.12, 0, w * 0.82, h)
    return QRectF(12, 0, w - 20, h)


HANDLE_SIZE = 7
HANDLE_CURSOR = {"tl": Qt.CursorShape.SizeFDiagCursor, "br": Qt.CursorShape.SizeFDiagCursor,
                 "tr": Qt.CursorShape.SizeBDiagCursor, "bl": Qt.CursorShape.SizeBDiagCursor,
                 "t": Qt.CursorShape.SizeVerCursor, "b": Qt.CursorShape.SizeVerCursor,
                 "l": Qt.CursorShape.SizeHorCursor, "r": Qt.CursorShape.SizeHorCursor}


def handle_points(w: float, h: float) -> dict:
    """Where the eight handles are drawn. The edge handles sit a quarter of the way along their edge,
    clear of the middle, where a connection point usually is (a dot always wins over a handle)."""
    return {"tl": QPointF(0, 0), "t": QPointF(w / 4, 0), "tr": QPointF(w, 0), "r": QPointF(w, h / 4),
            "br": QPointF(w, h), "b": QPointF(w * 3 / 4, h), "bl": QPointF(0, h), "l": QPointF(0, h * 3 / 4)}


def handle_at(local: QPointF, w: float, h: float, tol: float = 7) -> str | None:
    """Which of the eight resize handles a point in a node's box is on: the corners first, then
    the edges (a strip `tol` wide along each, away from the middle where a dot may sit)."""
    for name, c in handle_points(w, h).items():
        if len(name) == 2 and abs(local.x() - c.x()) <= tol and abs(local.y() - c.y()) <= tol:
            return name
    x, y = local.x(), local.y()
    if -tol <= y <= tol and tol < x < w - tol and abs(x - w / 2) > PORT_HIT:
        return "t"
    if h - tol <= y <= h + tol and tol < x < w - tol and abs(x - w / 2) > PORT_HIT:
        return "b"
    if -tol <= x <= tol and tol < y < h - tol and abs(y - h / 2) > PORT_HIT:
        return "l"
    if w - tol <= x <= w + tol and tol < y < h - tol and abs(y - h / 2) > PORT_HIT:
        return "r"
    return None


def draw_handles(qp: QPainter, w: float, h: float):
    qp.setPen(QPen(T.color("survey"), 1.2))
    qp.setBrush(T.color("background"))
    for c in handle_points(w, h).values():
        qp.drawRect(QRectF(c.x() - HANDLE_SIZE / 2, c.y() - HANDLE_SIZE / 2, HANDLE_SIZE, HANDLE_SIZE))


# ---------------------------------------------------------------- line icons (relay 005)
def kind_path(kind: str) -> QPainterPath:
    """A simple line drawing of a kind of part, in a 16 x 16 box, for the Parts list."""
    p = QPainterPath()

    def line(*pts):
        p.moveTo(*pts[0])
        for q in pts[1:]:
            p.lineTo(*q)

    if kind in ("antenna", "ant_whip", "ant_active", "ant_custom"):
        line((8, 15), (8, 6)); line((3, 2), (8, 6), (13, 2))
        if kind == "ant_whip":
            p.clear(); line((8, 15), (8, 1)); line((5, 15), (11, 15))
        elif kind == "ant_active":
            p.addRect(QRectF(5, 11, 6, 4))
    elif kind == "ant_dipole":
        line((1, 8), (7, 8)); line((9, 8), (15, 8)); line((7, 8), (7, 14)); line((9, 8), (9, 14))
    elif kind == "ant_discone":
        line((3, 2), (13, 2)); line((8, 2), (8, 6)); line((8, 6), (2, 15)); line((8, 6), (14, 15)); line((2, 15), (14, 15))
    elif kind in ("ant_yagi", "ant_lpda"):
        line((1, 8), (15, 8))
        for k, x in enumerate((3, 6, 9, 12)):
            d = (6 - k) if kind == "ant_lpda" else 5
            line((x, 8 - d), (x, 8 + d))
    elif kind == "ant_patch":
        p.addRect(QRectF(2, 2, 12, 12)); p.addRect(QRectF(5, 5, 6, 6))
    elif kind == "ant_helix":
        for k in range(4):
            p.addEllipse(QRectF(2 + k * 3, 3, 4, 10))
    elif kind == "ant_dish":
        p.moveTo(3, 2); p.cubicTo(12, 4, 12, 12, 3, 14); line((3, 8), (11, 8)); line((6, 15), (9, 11))
    elif kind == "ant_loop":
        p.addEllipse(QRectF(3, 1, 10, 10)); line((8, 11), (8, 15))
    elif kind == "probe":
        p.addEllipse(QRectF(3, 1.5, 10, 10)); line((8, 11.5), (8, 15))
    elif kind == "load":
        line((8, 1), (8, 4), (5, 5.5), (11, 7.5), (5, 9.5), (11, 11.5), (8, 12.5), (8, 15))
    elif kind in ("adapter", "bulkhead_sma", "bulkhead_usb"):
        p.addRect(QRectF(2, 5, 5, 6)); p.addRect(QRectF(9, 5, 5, 6))
        if kind != "adapter":
            line((8, 1), (8, 15))
    elif kind == "dcblock":
        line((1, 8), (6, 8)); line((6, 3), (6, 13)); line((10, 3), (10, 13)); line((10, 8), (15, 8))
    elif kind == "filter":
        line((1, 4), (5, 4)); p.moveTo(5, 4); p.cubicTo(7, 4, 7, 13, 8, 13); p.cubicTo(9, 13, 9, 4, 11, 4)
        line((11, 4), (15, 4))
    elif kind == "lna":
        line((3, 2), (14, 8), (3, 14), (3, 2))
    elif kind == "attenuator":
        line((1, 8), (3, 8), (5, 4), (7, 12), (9, 4), (11, 12), (13, 8), (15, 8))
    elif kind in ("coax", "jumper"):
        if kind == "coax":
            p.addEllipse(QRectF(2, 2, 12, 12)); p.addEllipse(QRectF(6, 6, 4, 4))
        else:
            p.moveTo(1, 12); p.cubicTo(5, 1, 11, 15, 15, 4)
    elif kind == R.SPLITTER:
        line((1, 8), (7, 8)); line((7, 8), (14, 3)); line((7, 8), (14, 13))
    elif kind == "sdr":
        p.addRoundedRect(QRectF(1.5, 3.5, 13, 9), 2, 2); p.moveTo(4, 8); p.cubicTo(6, 4, 7, 12, 9, 8)
        p.cubicTo(10, 6, 11, 10, 12, 8)
    elif kind == "usb":
        line((8, 15), (8, 2)); line((5, 5), (8, 2), (11, 5)); line((8, 11), (4, 8)); line((8, 9), (12, 7))
    elif kind == "isolator":
        p.addEllipse(QRectF(1.5, 4.5, 6, 7)); p.addEllipse(QRectF(8.5, 4.5, 6, 7))
    elif kind == "pc":
        p.addRoundedRect(QRectF(1.5, 2, 13, 9), 1.5, 1.5); line((8, 11), (8, 14)); line((5, 14.5), (11, 14.5))
    elif kind == R.ENCLOSURE:
        p.addRoundedRect(QRectF(1.5, 3, 13, 10), 3, 3); line((5, 3), (5, 1.5), (11, 1.5), (11, 3))
    elif kind == R.POWER or kind in R.POWER_KINDS:
        line((9.5, 1), (4, 9), (8, 9), (6.5, 15), (12, 6.5), (8, 6.5), (9.5, 1))
    elif kind == "lock":
        p.addRoundedRect(QRectF(3, 7, 10, 7.5), 1.5, 1.5); p.moveTo(5.5, 7); p.lineTo(5.5, 5)
        p.cubicTo(5.5, 1.5, 10.5, 1.5, 10.5, 5); p.lineTo(10.5, 7)
    else:                                       # custom and anything new
        p.addRoundedRect(QRectF(2.5, 2.5, 11, 11), 2.5, 2.5); p.addEllipse(QRectF(6.5, 6.5, 3, 3))
    return p


def tool_path(name: str) -> QPainterPath:
    """The toolbar's line icons, in a 16 x 16 box, in the same hand as the Parts list's."""
    p = QPainterPath()

    def line(*pts):
        p.moveTo(*pts[0])
        for q in pts[1:]:
            p.lineTo(*q)

    if name == "rig":
        p.addEllipse(QRectF(1, 6, 4, 4)); p.addEllipse(QRectF(11, 1.5, 4, 4)); p.addEllipse(QRectF(11, 10.5, 4, 4))
        line((5, 8), (11, 3.5)); line((5, 8), (11, 12.5))
    elif name == "new":
        line((8, 2), (8, 14)); line((2, 8), (14, 8))
    elif name == "rename":
        line((3, 13), (3.5, 10.5), (11, 3), (13, 5), (5.5, 12.5), (3, 13)); line((9.5, 4.5), (11.5, 6.5))
    elif name == "more":
        for x in (3, 8, 13):
            p.addEllipse(QRectF(x - 1, 7, 2, 2))
    elif name in ("undo", "redo"):
        p.moveTo(4, 7); p.cubicTo(6, 3, 13, 3, 13, 9); p.cubicTo(13, 12, 10, 13, 8, 13)
        line((4, 3), (4, 7), (8, 7))
        if name == "redo":
            p = QTransform(-1, 0, 0, 1, 16, 0).map(p)
    elif name == "tidy":
        line((2, 4), (14, 4)); line((2, 8), (10, 8)); line((2, 12), (12, 12))
    elif name == "arrange":
        p.addRoundedRect(QRectF(2, 2, 8, 8), 1.5, 1.5); p.addRoundedRect(QRectF(6, 6, 8, 8), 1.5, 1.5)
    elif name == "grid":
        for x in (3, 8, 13):
            for y in (3, 8, 13):
                p.addEllipse(QRectF(x - 0.9, y - 0.9, 1.8, 1.8))
    elif name == "parts":
        p.addRoundedRect(QRectF(1.5, 4, 8, 8), 2, 2); p.addEllipse(QRectF(10, 5, 5, 5))
    elif name == "wires":
        p.moveTo(1, 12); p.cubicTo(6, 12, 9, 4, 15, 4); p.addEllipse(QRectF(0, 11, 2, 2)); p.addEllipse(QRectF(14, 3, 2, 2))
    elif name == "zoom":
        p.addEllipse(QRectF(2, 2, 9, 9)); line((10, 10), (14.5, 14.5))
    elif name == "picture":
        p.addRoundedRect(QRectF(1.5, 3, 13, 10), 1.5, 1.5); line((2, 12), (6, 7), (9, 10), (11, 8), (14, 11))
    elif name == "help":
        p.addEllipse(QRectF(1.5, 1.5, 13, 13)); p.moveTo(6, 6); p.cubicTo(6, 3.5, 10.5, 3.5, 10.5, 6.2)
        p.cubicTo(10.5, 8, 8, 8, 8, 10); p.addEllipse(QRectF(7.4, 11.6, 1.2, 1.2))
    elif name == "clear":
        line((3, 4), (13, 4)); line((6, 4), (6, 2.5), (10, 2.5), (10, 4)); line((4.5, 4), (5.5, 14), (10.5, 14), (11.5, 4))
    elif name == "save":
        line((8, 2), (8, 10)); line((5, 7), (8, 10), (11, 7)); line((2, 10), (2, 14), (14, 14), (14, 10))
    elif name == "use":
        line((4, 2.5), (13, 8), (4, 13.5), (4, 2.5))
    elif name == "text":
        line((2, 4), (14, 4)); line((2, 8), (14, 8)); line((2, 12), (9, 12))
    elif name == "output":
        p.addRoundedRect(QRectF(1.5, 2, 13, 12), 1.5, 1.5); line((9.5, 2), (9.5, 14)); line((11, 5.5), (13, 5.5))
        line((11, 8), (13, 8))
    elif name == "history":
        p.addEllipse(QRectF(2, 2, 12, 12)); line((8, 4.5), (8, 8), (10.5, 9.5))
    elif name == "archive":
        p.addRect(QRectF(1.5, 3, 13, 3.5)); line((2.5, 6.5), (2.5, 14), (13.5, 14), (13.5, 6.5)); line((6, 9), (10, 9))
    else:
        p.addRect(QRectF(3, 3, 10, 10))
    return p


def tool_icon(name: str, colour: str = "text2") -> QIcon:
    """A toolbar icon with its normal and disabled looks (the disabled one dimmed, never hidden)."""
    icon = QIcon()
    for mode, c in ((QIcon.Mode.Normal, T.color(colour)), (QIcon.Mode.Disabled, T.color("faint")),
                    (QIcon.Mode.Active, T.color("text"))):
        pm = QPixmap(32, 32)
        pm.setDevicePixelRatio(2.0)
        pm.fill(Qt.GlobalColor.transparent)
        qp = QPainter(pm)
        qp.setRenderHint(QPainter.RenderHint.Antialiasing)
        qp.setPen(QPen(c, 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        qp.drawPath(tool_path(name))
        qp.end()
        icon.addPixmap(pm, mode)
    return icon


_ICON_CACHE: dict = {}


def kind_pixmap(kind: str, colour: QColor, size: int = 16, dpr: float = 1.0) -> QPixmap:
    """The line icon as a pixmap, drawn once per kind, colour and screen scale."""
    key = (kind, colour.name(QColor.NameFormat.HexArgb), size, round(dpr, 2))
    pm = _ICON_CACHE.get(key)
    if pm is None:
        pm = QPixmap(int(size * dpr), int(size * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.GlobalColor.transparent)
        qp = QPainter(pm)
        qp.setRenderHint(QPainter.RenderHint.Antialiasing)
        qp.scale(size / 16, size / 16)
        qp.setPen(QPen(colour, 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        qp.setBrush(Qt.BrushStyle.NoBrush)
        qp.drawPath(kind_path(kind))
        qp.end()
        _ICON_CACHE[key] = pm
    return pm


ROLE_KIND = Qt.ItemDataRole.UserRole + 1      # "heading" or "item"
ROLE_ICON = Qt.ItemDataRole.UserRole + 2      # which line icon
ROLE_CAT = Qt.ItemDataRole.UserRole + 3       # the colour category; the colour itself is worked out when painted


class TileDelegate(QStyledItemDelegate):
    """Draws the Parts list: entries as tiles on their own surface with a line icon in their
    colour-coding colour, a hover and a pressed state; headings as quiet labels, not tiles."""

    def __init__(self, lst: "PartsList"):
        super().__init__(lst)
        self.lst = lst

    def sizeHint(self, opt, index):
        w = max(60, self.lst.viewport().width() - 2)      # never wider than the list: text wraps, no sideways scroll
        if index.data(ROLE_KIND) == "heading":
            return QSize(w, 22 if index.row() == 0 else 30)
        if index.data(ROLE_KIND) == "hint":       # as tall as its wrapped text, never cut off
            fm = QFontMetricsF(T.ui_font(8.5))
            r = fm.boundingRect(QRectF(0, 0, w - 6, 1000), int(Qt.TextFlag.TextWordWrap),
                                index.data(Qt.ItemDataRole.DisplayRole) or "")
            return QSize(w, int(r.height()) + 10)
        return QSize(w, 34)

    def paint(self, qp: QPainter, opt, index):
        r = QRectF(opt.rect)
        qp.save()
        qp.setRenderHint(QPainter.RenderHint.Antialiasing)
        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        if index.data(ROLE_KIND) == "hint":
            qp.setPen(T.color("muted"))
            qp.setFont(T.ui_font(8.5))
            qp.drawText(r.adjusted(2, 4, -4, 0), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
                        | int(Qt.TextFlag.TextWordWrap), text)
            qp.restore()
            return
        if index.data(ROLE_KIND) == "heading":
            x0 = 2
            cat = index.data(ROLE_CAT)
            if cat and self.lst.colour_on:            # the section's own colour, as a short bar
                qp.setPen(Qt.PenStyle.NoPen)
                qp.setBrush(palette_colour(cat))
                qp.drawRoundedRect(QRectF(r.left() + 2, r.bottom() - 15, 3, 10), 1.5, 1.5)
                x0 = 10
            qp.setPen(T.color("muted"))
            qp.setFont(T.ui_font(7.5, QFont.Weight.DemiBold))
            qp.drawText(r.adjusted(x0, 0, 0, -4), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom), text)
            qp.restore()
            return
        tile = r.adjusted(0, 2, -2, -2)
        hover = bool(opt.state & QStyle.StateFlag.State_MouseOver)
        pressed = self.lst.pressed_row == index.row()
        fill = T.color("tile_pressed" if pressed else ("tile_hover" if hover else "tile"))
        qp.setBrush(fill)
        qp.setPen(QPen(T.color("survey") if pressed else (T.color("line_strong") if hover else T.color("tile_edge")),
                       T.BORDER))
        rad = round(T.RADIUS * 0.5)
        qp.drawRoundedRect(tile.adjusted(0.5, 0.5, -0.5, -0.5), rad, rad)
        colour = palette_colour(index.data(ROLE_CAT), self.lst.colour_on)
        dpr = self.lst.devicePixelRatioF()
        pm = kind_pixmap(index.data(ROLE_ICON) or "custom", colour, 16, dpr)
        qp.drawPixmap(QPointF(tile.left() + 10, tile.center().y() - 8), pm)
        qp.setPen(T.color("text"))
        qp.setFont(T.ui_font(9.5))
        tr = tile.adjusted(34, 0, -8, 0)
        qp.drawText(tr, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                    qp.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, int(tr.width())))
        qp.restore()


# ---------------------------------------------------------------- canvas items
class PartItem(QGraphicsObject):
    moved = Signal()

    def __init__(self, screen: "ChainScreen", node: dict):
        super().__init__()
        self.screen, self.node = screen, node
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsMovable
                      | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
                      | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges)
        self.setAcceptHoverEvents(True)
        self.setPos(node["x"], node["y"])
        self.setZValue(2)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.hover = False
        self.hot_port = None             # the connection point under the pointer, drawn larger
        self._resize = None
        self._press = None
        if R.is_locked(node):
            self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)

    # geometry
    def wh(self) -> tuple[int, int]:
        return G.size(self.node)

    def boundingRect(self) -> QRectF:
        w, h = self.wh()
        m = PORT_R + 10                  # room for the shadow, the glow and a grown dot
        return QRectF(-m, -m, w + 2 * m, h + 2 * m)

    def power_point(self) -> QPointF | None:
        a = self.screen.power_map.get(self.node["id"])
        return QPointF(a[0], a[1]) if a else None

    def shape(self) -> QPainterPath:
        """The body plus a generous circle round every connection point, so a press near a dot
        always reaches this part (and then the dot wins over the body: mousePressEvent)."""
        w, h = self.wh()
        p = QPainterPath()
        # Winding fill: where a dot's circle overlaps the body it stays inside (the default odd-even
        # rule makes every overlap a hole, which is how a press on a dot fell through to a wire).
        p.setFillRule(Qt.FillRule.WindingFill)
        shape = R.shape_of(self.node)
        if shape == "pipe":                      # an adapter is only where its pipe is
            st = QPainterPathStroker()
            st.setWidth(PIPE_W + 8)
            st.setCapStyle(Qt.PenCapStyle.RoundCap)
            st.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            p.addPath(st.createStroke(self.pipe_path()))
        else:
            p.addPath(outline_path(shape, w, h))
        if self.isSelected():                    # the resize handles reach a little outside the shape
            for c in handle_points(w, h).values():
                p.addRect(QRectF(c.x() - 7, c.y() - 7, 14, 14))
        here = self.pos()
        for (x, y) in self._dots():
            p.addEllipse(QPointF(x, y) - here, PORT_HIT, PORT_HIT)
        return p

    def pipe_path(self) -> QPainterPath:
        """An adapter drawn as a pipe joining its connection points through its middle: a barrel,
        an elbow (90°), a T or a U (180°), whichever its points make."""
        w, h = self.wh()
        here = self.pos()
        pts = [QPointF(a[0], a[1]) - here for k, a in self.screen.anchor_map.items() if k[0] == self.node["id"]]
        c = QPointF(w / 2, h / 2)
        path = QPainterPath()
        if len(pts) == 2 and abs(pts[0].x() - pts[1].x()) < 1:          # both on one side: a U
            a, b = pts
            far = QPointF(w * 0.78 if a.x() < w / 2 else w * 0.22, 0)
            path.moveTo(a)
            path.lineTo(far.x(), a.y())
            path.lineTo(far.x(), b.y())
            path.lineTo(b)
            return path
        if len(pts) == 2 and (abs(pts[0].y() - pts[1].y()) < 1 or abs(pts[0].x() - pts[1].x()) < 1):
            path.moveTo(pts[0])
            path.lineTo(pts[1])
            return path
        for q in pts or [QPointF(0, h / 2), QPointF(w, h / 2)]:
            path.moveTo(q)
            path.lineTo(c)
        return path

    def _dots(self) -> list:
        """Scene points of this part's connection points and its power input."""
        nid = self.node["id"]
        out = [(a[0], a[1]) for k, a in self.screen.anchor_map.items() if k[0] == nid]
        pp = self.screen.power_map.get(nid)
        if pp:
            out.append((pp[0], pp[1]))
        return out

    def _anchor(self, kind: str, port: int) -> QPointF | None:
        a = self.screen.anchor_map.get((self.node["id"], kind, port))
        return QPointF(a[0], a[1]) if a else None

    def in_port(self) -> QPointF | None:
        return self._anchor("in", 0)

    def out_port(self, i: int) -> QPointF:
        return self._anchor("out", i) or self.mapToScene(QPointF(self.wh()[0], self.wh()[1] / 2))

    def free_outputs(self) -> list[int]:
        used = {w["port"] for w in self.screen.rig["wires"] if w["from"] == self.node["id"]}
        return [i for i in range(R.out_ports(self.node)) if i not in used]

    def _rings(self) -> list[QPointF]:
        """Scene points of the hover rings: the middle of each side, where a wire can
        be started from the first free output, unless a dot is already there."""
        if not self.free_outputs():
            return []
        dots = [QPointF(a[0], a[1]) for k, a in self.screen.anchor_map.items() if k[0] == self.node["id"]]
        out = []
        for s in G.SIDES:
            x, y = G.mid_side(self.node, s)
            p = QPointF(x, y)
            if all((p - d).manhattanLength() > PORT_R * 2.5 for d in dots):
                out.append(p)
        return out

    def port_at(self, scene_pt: QPointF) -> tuple | None:
        """The connection point a press at scene_pt starts a wire from: ("out", port) or
        ("in", 0), the nearest within PORT_HIT; or a hover ring (the first free output)."""
        best, best_d = None, PORT_HIT
        for (nid, kind, port), (x, y, _s) in self.screen.anchor_map.items():
            if nid != self.node["id"] or (nid, kind, port) in self.screen.mated_keys:
                continue                  # a mated point is a joint: clicking it selects the mating
            d = QLineF(QPointF(x, y), scene_pt).length()
            if d <= best_d:
                best, best_d = (kind, port), d
        if best is not None:
            return best
        if self.hover or self.isSelected():
            free = self.free_outputs()
            if free and any(QLineF(r, scene_pt).length() <= PORT_HIT for r in self._rings()):
                return ("out", free[0])
        return None

    def on_power_input(self, scene_pt: QPointF) -> bool:
        pp = self.power_point()
        return pp is not None and QLineF(pp, scene_pt).length() <= PORT_HIT

    def on_grip(self, scene_pt: QPointF) -> str | None:
        """The resize handle under a point: any corner or edge of a selected part (relay 007)."""
        if not self.isSelected() or R.is_locked(self.node):
            return None
        w, h = self.wh()
        return handle_at(self.mapFromScene(scene_pt), w, h)

    # look
    def _tint(self):
        """(fill, edge, kind-label colour, name colour, note colour), worked out on every paint
        from the current settings: the owner's own colour first, else the kind colour when colour
        coding is on, else plain. Text keeps 4.5:1 against whatever it sits on."""
        return node_look(self.node, self.screen)

    def paint(self, qp: QPainter, opt, widget=None):
        p = self.node["part"]
        w, h = self.wh()
        fill, edge, small, main, soft = self._tint()
        sel = self.isSelected()
        target = self.screen.drop_target == self.node["id"]
        shape = R.shape_of(self.node)
        qp.setRenderHint(QPainter.RenderHint.Antialiasing)
        own = bool(self.node.get("colour"))
        if sel or target:
            edge = T.color("survey")
            if not own:                                    # the owner's own colour stays visible when selected
                fill, main, soft = T.color("survey_sel"), T.color("text"), T.color("muted")
                small = QColor(T.readable_on(small.name(), T.C["survey_sel"]))
        elif self.hover:
            edge = T.color("text2") if not own else QColor(main)
            if not own:
                fill = QColor(fill).lighter(112)
        if shape == "pipe":
            self._paint_pipe(qp, fill, edge, main, small, sel or target)
        else:
            body = outline_path(shape, w, h)
            qp.setPen(Qt.PenStyle.NoPen)
            for dy, _spread, alpha in T.SHADOW_LAYERS[:2]:      # a soft shadow: the outline, offset
                qp.setBrush(T.color("shadow", alpha))
                qp.drawPath(body.translated(0, dy))
            if sel or target:                              # a faint outer glow
                qp.setBrush(Qt.BrushStyle.NoBrush)
                for width, alpha in ((12, 0.07), (7, 0.13)):
                    qp.setPen(QPen(T.color("survey", alpha if sel else alpha * 1.6), width))
                    qp.drawPath(body)
            qp.setBrush(fill)
            qp.setPen(QPen(edge, T.SELECTED_BORDER if (sel or target) else T.BORDER))
            qp.drawPath(body)
            self._paint_words(qp, shape, w, h, small, main, soft, own, widget)
        if R.is_locked(self.node):
            dpr = widget.devicePixelRatioF() if widget is not None else 2.0
            qp.drawPixmap(QPointF(w - 20, 6), kind_pixmap("lock", QColor(soft) if own else T.color("lock"), 14, dpr))
        # connection points: a filled dot is a male SMA port (a pin), a ring a female one (a
        # socket), a plain dot a port whose gender is not set. A mated point is drawn by its joint.
        here = self.pos()
        for key, (x, y, _side) in self.screen.anchor_map.items():
            nid, kind_, port = key
            if nid != self.node["id"] or key in self.screen.mated_keys:
                continue
            wf = self.screen.wiring_from
            hot = key == self.hot_port or (wf == (nid, port) if kind_ == "out" else wf == ("in", nid))
            colour = T.color("survey") if hot else T.color("wire")
            r = PORT_R + 2 if key == self.hot_port else PORT_R
            c = QPointF(x, y) - here
            gender = R.port_gender(self.node, kind_, port)
            if gender == "F":
                qp.setPen(QPen(T.color("background"), 1.5))
                qp.setBrush(T.color("background"))
                qp.drawEllipse(c, r, r)
                qp.setPen(QPen(colour, 2.2))
                qp.setBrush(Qt.BrushStyle.NoBrush)
                qp.drawEllipse(c, r - 1.6, r - 1.6)
            else:
                qp.setPen(QPen(T.color("background"), 2))
                qp.setBrush(colour)
                qp.drawEllipse(c, r, r)
                if gender == "M":                          # the pin
                    qp.setPen(Qt.PenStyle.NoPen)
                    qp.setBrush(T.color("background"))
                    qp.drawEllipse(c, 1.6, 1.6)
        pp = self.power_point()
        if pp is not None:                    # the power input: a diamond, filled once a power wire feeds it
            fed = R.power_feed(self.screen.rig, self.node["id"]) is not None
            c = pp - here
            d = 6 if self.hot_port == "power" else 5
            diamond = QPainterPath(QPointF(c.x(), c.y() - d))
            for q in ((c.x() + d, c.y()), (c.x(), c.y() + d), (c.x() - d, c.y())):
                diamond.lineTo(*q)
            diamond.closeSubpath()
            qp.setPen(QPen(T.color("power") if fed else T.color("alert"), 1.6))
            qp.setBrush(T.color("power") if fed else T.color("background"))
            qp.drawPath(diamond)
        if (self.hover or sel) and self.screen.wiring_from is None:
            qp.setBrush(Qt.BrushStyle.NoBrush)
            qp.setPen(QPen(T.color("survey", 0.85), 1.5))
            for r in self._rings():
                qp.drawEllipse(r - here, PORT_R - 1, PORT_R - 1)
        if sel and not R.is_locked(self.node) and self.screen.sel_count == 1:
            draw_handles(qp, w, h)            # resize from any corner or edge

    def _paint_words(self, qp, shape, w, h, small, main, soft, own, widget):
        p = self.node["part"]
        box = text_box(shape, w, h)
        cat = CATEGORY.get(p["type"])
        if self.screen.colour_on and cat and cat != "radio" and not own and shape in ("rect", "rounded"):
            qp.setPen(Qt.PenStyle.NoPen)
            qp.setBrush(category_colour(cat))
            qp.drawRoundedRect(QRectF(3, 8, 4, h - 16), 2, 2)
        x0 = box.left()
        a_type = R.antenna(self.node).get("type")
        if p["type"] == "antenna" and a_type:              # the antenna's own kind, as a glyph
            dpr = widget.devicePixelRatioF() if widget is not None else 2.0
            qp.drawPixmap(QPointF(x0, h / 2 - 11), kind_pixmap("ant_" + a_type, QColor(small), 22, dpr))
            x0 += 28
        tw = max(20, box.right() - x0)
        detail = self._detail()
        pos = R.text_pos(self.node)                       # relay 008: where the words sit, 3 x 3
        note_on = bool(detail and self.screen.show_notes)
        block = 16 + 18 + (18 if note_on else 0)
        top = {"t": 8, "m": (h - block) / 2, "b": h - block - 6}[pos[0]]
        top = max(4, top)
        halign = {"l": Qt.AlignmentFlag.AlignLeft, "c": Qt.AlignmentFlag.AlignHCenter,
                  "r": Qt.AlignmentFlag.AlignRight}[pos[1]]
        qp.setPen(small)
        qp.setFont(T.ui_font(8.5))
        kind = self.screen.kind_label(self.node)
        if a_type:
            kind = R.ANTENNA_TYPES.get(a_type, ("Antenna",))[0]
        qp.drawText(QRectF(x0, top, tw, 16), halign,
                    qp.fontMetrics().elidedText(kind, Qt.TextElideMode.ElideRight, int(tw)))
        qp.setPen(main)
        qp.setFont(T.ui_font(10, QFont.Weight.DemiBold))
        name = p.get("name") or "(no name)"
        if p["type"] == "attenuator" and p.get("db") not in (None, "", 0):
            name = f"{p['db']} dB"
        qp.drawText(QRectF(x0, top + 18, tw, 20), halign,
                    qp.fontMetrics().elidedText(name, Qt.TextElideMode.ElideRight, int(tw)))
        if note_on:
            qp.setPen(soft)
            qp.setFont(T.ui_font(8))
            if pos[0] == "t" and h > G.MIN_H + 10:          # a taller part shows the whole note under the name
                qp.drawText(QRectF(x0, top + 36, tw, max(16, h - top - 40)), int(halign | Qt.AlignmentFlag.AlignTop)
                            | int(Qt.TextFlag.TextWordWrap), str(detail))
            else:
                qp.drawText(QRectF(x0, top + 36, tw, 16), halign,
                            qp.fontMetrics().elidedText(str(detail), Qt.TextElideMode.ElideRight, int(tw)))

    def _detail(self) -> str:
        p = self.node["part"]
        a_type = R.antenna(self.node).get("type")
        detail = p.get("length") or p.get("detail") or ""
        if p["type"] == "sdr":
            sn = self.screen.serial_of(p)
            detail = f"SN {sn}" if sn else "serial not known"
        if p["type"] in R.SMA_TYPES and R.sma(self.node).get("gender"):
            g = R.GENDERS[R.sma(self.node)["gender"]]
            detail = f"{g} · {detail}" if detail else g
        if a_type and R.antenna(self.node).get("az") not in (None, ""):
            a = R.antenna(self.node)
            point = f"az {a['az']:g}°" + (f" el {a['el']:g}°" if a.get("el") not in (None, "") else "")
            detail = f"{point} · {detail}" if detail else point
        return detail

    def _paint_pipe(self, qp, fill, edge, main, small, hot):
        """An adapter: a pipe through its connection points, its name on the pipe, its kind above."""
        w, h = self.wh()
        pipe = self.pipe_path()
        for width, colour in ((PIPE_W + 10, T.color("survey", 0.12)),) if hot else ():
            qp.setPen(QPen(colour, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            qp.drawPath(pipe)
        qp.setBrush(Qt.BrushStyle.NoBrush)
        qp.setPen(QPen(T.color("shadow", 0.25), PIPE_W + 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap,
                       Qt.PenJoinStyle.RoundJoin))
        qp.drawPath(pipe.translated(0, 2))
        qp.setPen(QPen(edge, PIPE_W + 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap, Qt.PenJoinStyle.RoundJoin))
        qp.drawPath(pipe)
        qp.setPen(QPen(fill, PIPE_W - 1, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap, Qt.PenJoinStyle.RoundJoin))
        qp.drawPath(pipe)
        p = self.node["part"]
        qp.setFont(T.ui_font(8, QFont.Weight.DemiBold))
        name = p.get("name") or "adapter"
        r = QRectF(4, h / 2 - PIPE_W / 2, w - 8, PIPE_W)
        qp.setPen(main)
        qp.drawText(r, Qt.AlignmentFlag.AlignCenter, qp.fontMetrics().elidedText(name, Qt.TextElideMode.ElideRight,
                                                                                  int(w * 0.62)))
        kind = R.adapter_kind_text(self.node) or (R.GENDERS.get(R.sma(self.node).get("gender"), ""))
        if kind and self.screen.show_notes:
            qp.setPen(small)
            qp.setFont(T.ui_font(7.5))
            qp.drawText(QRectF(4, 0, w - 8, h / 2 - PIPE_W / 2 - 1), int(Qt.AlignmentFlag.AlignBottom
                                                                         | Qt.AlignmentFlag.AlignLeft), kind)

    # behaviour
    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionChange and not self.screen.animating:
            v = self.screen.snapped(self, value)
            return self.screen.guide(self, v)
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            self.node["x"], self.node["y"] = int(round(self.pos().x())), int(round(self.pos().y()))
            self.moved.emit()
            self.screen.drag_moved(self)
        if change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            self.screen.selection_changed()
        return super().itemChange(change, value)

    def hoverEnterEvent(self, ev):
        self.hover = True
        self.update()

    def hoverLeaveEvent(self, ev):
        self.hover = False
        self.hot_port = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.update()

    def _hot(self, pt: QPointF):
        """The connection point (or power input) under the pointer, if any: the same reach as a press."""
        if self.on_power_input(pt):
            return "power"
        k = self.port_at(pt)
        if k is None:
            return None
        key = (self.node["id"], k[0], k[1])
        return key if key in self.screen.anchor_map else None

    def hoverMoveEvent(self, ev):
        hot = self._hot(ev.scenePos())
        if hot != self.hot_port:
            self.hot_port = hot
            self.update()
            self.setToolTip(self._port_tip(hot))
        grip = self.on_grip(ev.scenePos())
        if grip:
            self.setCursor(HANDLE_CURSOR[grip])
        elif hot is not None or self.port_at(ev.scenePos()) is not None:
            self.setCursor(Qt.CursorShape.CrossCursor)
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor if R.is_locked(self.node)
                           else Qt.CursorShape.OpenHandCursor)

    def _port_tip(self, hot) -> str:
        if hot == "power":
            return "Power input: drag from here to a power source, or from a power source's dot to here"
        if hot is None:
            return ("Locked: it stays where it is. Click to edit it; right-click to format it."
                    if R.is_locked(self.node) else "Click to edit · drag to move · right-click to format and arrange")
        _nid, kind, port = hot
        g = R.port_gender(self.node, kind, port)
        what = ("Output" if kind == "out" else "Input") + (f", SMA {R.GENDER_WORD[g]}" if g else "")
        return f"{what}: drag from here to another part to wire it"

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.RightButton:
            # A right press only selects (the menu opens on release, from the grid): it must never
            # start a drag, a group click or a rubber band, or a second menu could follow it.
            if not self.isSelected():
                self.scene().clearSelection()
                self.screen.select_with_group(self)
            ev.accept()
            return
        grip = self.on_grip(ev.scenePos()) if ev.button() == Qt.MouseButton.LeftButton else None
        if grip and self.port_at(ev.scenePos()) is None:   # a handle (Shift there keeps the proportions)
            n = self.node
            self._resize = (grip, ev.scenePos(), (n["x"], n["y"], *self.wh()))
            ev.accept()
            return
        if ev.button() == Qt.MouseButton.LeftButton and ev.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self.screen.shift_toggle(self)
            self._shifted = True     # Shift+click adds or removes; its release must not narrow the selection
            ev.accept()
            return
        if ev.button() == Qt.MouseButton.LeftButton:
            # A connection point always wins over the part's body (relay 006): an output starts a
            # wire to another part's input, an input a wire from another part's free output.
            port = self.port_at(ev.scenePos())
            if port is not None:
                if port[0] == "out":
                    self.screen.start_wire(self, port[1], ev.scenePos())
                else:
                    self.screen.start_wire_in(self, ev.scenePos())
                ev.accept()
                return
            if self.on_power_input(ev.scenePos()):
                self.screen.start_power_from_part(self, ev.scenePos())
                ev.accept()
                return
            grip = self.on_grip(ev.scenePos())
            if grip:
                n = self.node
                self._resize = (grip, ev.scenePos(), (n["x"], n["y"], *self.wh()))
                ev.accept()
                return
        if ev.button() != Qt.MouseButton.LeftButton:
            ev.ignore()              # the middle button pans: the grid takes it
            return
        self.setCursor(Qt.CursorShape.ClosedHandCursor)
        if ev.modifiers() & Qt.KeyboardModifier.AltModifier:
            self._pulled = self.screen.pull_off(self.node["id"])     # Alt + drag: off what it is mated to
        elif len(R.assembly(self.screen.rig, self.node["id"])) > 1:
            self.screen.status.emit("mated parts move together: hold Alt as you start dragging to pull one off")
        self.screen.group_press(self, ev.modifiers())
        self._press = self.pos()
        self._press_at = ev.screenPos()
        super().mousePressEvent(ev)
        self.screen.begin_drag(self)

    def mouseMoveEvent(self, ev):
        if self._resize is None and not_yet_a_drag(self, ev):
            return
        if self._resize is not None:
            grip, start, rect0 = self._resize
            d = ev.scenePos() - start
            x, y, w, h = G.resize(rect0, grip, d.x(), d.y(),
                                  keep_ratio=bool(ev.modifiers() & Qt.KeyboardModifier.ShiftModifier),
                                  snap=R.GRID if self.screen.snap_on else None)
            if (x, y, w, h) != (self.node["x"], self.node["y"], *self.wh()):
                self.prepareGeometryChange()
                self.node["w"], self.node["h"] = w, h
                self.screen.animating = True          # a move made by a resize is not snapped or guided
                self.setPos(x, y)
                self.screen.animating = False
                self.node["x"], self.node["y"] = x, y
                self.screen._refresh_wires()
            return
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        if ev.button() != Qt.MouseButton.LeftButton:
            ev.accept()
            return
        if getattr(self, "_shifted", False):
            self._shifted = False
            ev.accept()
            return
        if self._resize is not None:
            rect0 = self._resize[2]
            self._resize = None
            if (self.node["x"], self.node["y"], *self.wh()) != rect0:
                self.screen.changed()
            return
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        super().mouseReleaseEvent(ev)
        moved = self._press is not None and self.pos() != self._press
        self.screen.end_drag(self)
        self.screen.group_release(self, moved)
        self._press = None
        pulled, self._pulled = getattr(self, "_pulled", False), False
        clicked = is_click(self, ev) and not moved
        if moved:
            self.screen.try_mate(self.node["id"])        # an SMA part let go on a dot mates with it
        if moved or pulled:
            self.screen.changed()
        if clicked and not (ev.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)):
            self.screen.box_clicked(self.screen.box_item(self.node["id"]) or self)


class EnclosureItem(QGraphicsObject):
    """A box drawn behind parts (the radio's cage). Parts whose centre sits inside it
    move with it; drag a part out to take it out. Any corner resizes it. Drawing only:
    it is stored in chain_layout.json and writes nothing into the recorded text."""

    def __init__(self, screen: "ChainScreen", enc: dict):
        super().__init__()
        self.screen, self.enc = screen, enc
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsMovable
                      | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
                      | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges)
        self.setAcceptHoverEvents(True)
        self.hover = False
        self._resize = None          # (corner, scene point at press, (x, y, w, h) at press)
        self.setPos(enc["x"], enc["y"])
        self.setZValue(-1)
        if R.is_locked(enc):
            self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)

    def wh(self) -> tuple[int, int]:
        return int(self.enc["w"]), int(self.enc["h"])

    def boundingRect(self) -> QRectF:
        w, h = self.wh()
        return QRectF(-6, -6, w + 12, h + 12)

    def corner_at(self, scene_pt: QPointF) -> str | None:
        """The resize handle under a point: a corner or, since relay 007, any edge."""
        w, h = self.wh()
        return handle_at(self.mapFromScene(scene_pt), w, h, GRIP / 2 + 2)

    def paint(self, qp: QPainter, opt, widget=None):
        w, h = self.wh()
        sel = self.isSelected()
        own = self.enc.get("colour") if T.is_hex(self.enc.get("colour")) else None
        if own:                            # the owner's colour, lightened where needed so the name reads
            colour = QColor(T.readable_on(own, T.C["plot"], 3.0))
        else:
            colour = category_colour("enclosure") if self.screen.colour_on else T.color("line_strong")
        qp.setRenderHint(QPainter.RenderHint.Antialiasing)
        fill = QColor(own or colour.name())
        fill.setAlphaF(0.14 if own else 0.08)
        qp.setBrush(fill)
        pen = QPen(T.color("survey") if sel else colour, 2 if sel else 1.4)
        if not sel:
            pen.setStyle(Qt.PenStyle.DashLine)
        qp.setPen(pen)
        qp.drawRoundedRect(QRectF(0, 0, w, h), T.RADIUS, T.RADIUS)
        x0 = 12
        if R.is_locked(self.enc):
            dpr = widget.devicePixelRatioF() if widget is not None else 2.0
            qp.drawPixmap(QPointF(10, 8), kind_pixmap("lock", T.color("lock"), 14, dpr))
            x0 = 30
        qp.setPen(QColor(T.readable_on(colour.name(), T.C["plot"])))
        qp.setFont(T.ui_font(9.5, QFont.Weight.DemiBold))
        name = self.enc.get("name") or "Enclosure"
        qp.drawText(QRectF(x0, 7, w - x0 - 12, 18), Qt.AlignmentFlag.AlignLeft,
                    qp.fontMetrics().elidedText(name, Qt.TextElideMode.ElideRight, w - x0 - 12))
        note = self.enc.get("note") or ""
        if note and self.screen.show_notes:
            qp.setPen(T.color("muted"))
            qp.setFont(T.ui_font(8))
            qp.drawText(QRectF(x0 + qp.fontMetrics().horizontalAdvance(name) * 1.25 + 10, 9, w, 16),
                        Qt.AlignmentFlag.AlignLeft,
                        qp.fontMetrics().elidedText(note, Qt.TextElideMode.ElideRight, max(20, w // 2)))
        if (self.hover or sel) and not R.is_locked(self.enc):
            qp.setPen(Qt.PenStyle.NoPen)
            qp.setBrush(T.color("text2"))
            for pt in handle_points(w, h).values():
                qp.drawRect(QRectF(pt.x() - 4, pt.y() - 4, 8, 8))

    def itemChange(self, change, value):
        if (change == QGraphicsItem.GraphicsItemChange.ItemPositionChange
                and not self.screen.animating and self._resize is None):
            v = self.screen.snapped(self, value)
            return self.screen.guide(self, v)
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            self.enc["x"], self.enc["y"] = int(round(self.pos().x())), int(round(self.pos().y()))
            self.screen.drag_moved(self)
        if change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            self.screen.selection_changed()
        return super().itemChange(change, value)

    def hoverEnterEvent(self, ev):
        self.hover = True
        self.update()

    def hoverLeaveEvent(self, ev):
        self.hover = False
        self.unsetCursor()
        self.update()

    def hoverMoveEvent(self, ev):
        c = None if R.is_locked(self.enc) else self.corner_at(ev.scenePos())
        self.setToolTip("Locked: a click selects it, a drag on it selects a rectangle. It can still be edited."
                        if R.is_locked(self.enc) else
                        ("Drag the corner or edge to resize" if c else "Enclosure: click to edit · drag to move it with "
                                                                       "what is inside · right-click to format"))
        if R.is_locked(self.enc):
            self.setCursor(Qt.CursorShape.CrossCursor)        # a press here starts a rectangle select
        elif c:
            self.setCursor(HANDLE_CURSOR[c])
        else:
            self.setCursor(Qt.CursorShape.OpenHandCursor)

    def press_at(self, scene_pt: QPointF) -> bool:
        """Start a corner resize if the point is on a corner (also used by the check script)."""
        c = self.corner_at(scene_pt)
        if not c:
            return False
        e = self.enc
        self._resize = (c, scene_pt, (e["x"], e["y"], e["w"], e["h"]))
        self.setSelected(True)
        return True

    def drag_to(self, scene_pt: QPointF, keep: bool = False):
        c, start, rect0 = self._resize
        d = scene_pt - start
        x1, y1, w, h = G.resize(rect0, c, d.x(), d.y(), keep_ratio=keep, snap=R.GRID if self.screen.snap_on else None,
                                min_w=R.ENC_MIN_W, min_h=R.ENC_MIN_H, max_w=R.ENC_MAX, max_h=R.ENC_MAX)
        self.prepareGeometryChange()
        self.enc["w"], self.enc["h"] = int(w), int(h)
        self.setPos(x1, y1)
        self.update()

    def release_resize(self):
        before = self._resize[2]
        self._resize = None
        e = self.enc
        if (e["x"], e["y"], e["w"], e["h"]) != before:
            self.screen.changed()

    def start_carry(self):
        """The parts in the box (and their groups) come along when it moves
        (ChainScreen.begin_drag); also used by the check script."""
        self.screen.begin_drag(self)

    def end_carry(self):
        if self.screen.end_drag(self):
            self.screen.changed()

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.RightButton:
            if not self.isSelected():
                self.scene().clearSelection()
                self.screen.select_with_group(self)
            ev.accept()
            return
        if ev.button() == Qt.MouseButton.LeftButton and R.is_locked(self.enc):
            # A locked box lets the press through: a drag on it selects a rectangle; a plain click
            # still selects the box itself (ChainView.mouseReleaseEvent), to edit or rename it.
            self.screen.view.locked_click = (self, ev.scenePos())
            ev.ignore()
            return
        if ev.button() != Qt.MouseButton.LeftButton:
            ev.ignore()
            return
        if ev.button() == Qt.MouseButton.LeftButton and ev.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self.screen.shift_toggle(self)
            self._shifted = True     # its release must not let Qt narrow the selection to this box
            ev.accept()
            return
        if ev.button() == Qt.MouseButton.LeftButton and self.press_at(ev.scenePos()):
            ev.accept()
            return
        if ev.button() == Qt.MouseButton.LeftButton:
            self.screen.group_press(self, ev.modifiers())
        self._press_at = ev.screenPos()
        super().mousePressEvent(ev)
        self.screen.begin_drag(self)

    def mouseMoveEvent(self, ev):
        if self._resize is not None:
            self.drag_to(ev.scenePos(), bool(ev.modifiers() & Qt.KeyboardModifier.ShiftModifier))
            return
        if not_yet_a_drag(self, ev):
            return
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        if ev.button() != Qt.MouseButton.LeftButton:
            ev.accept()
            return
        if getattr(self, "_shifted", False):
            self._shifted = False
            ev.accept()
            return
        if self._resize is not None:
            self.release_resize()
            return
        super().mouseReleaseEvent(ev)
        clicked = is_click(self, ev)
        moved = self.screen.end_drag(self)
        self.screen.group_release(self, moved)
        if moved:
            self.screen.changed()
        if clicked and not moved and not (ev.modifiers() & (Qt.KeyboardModifier.ControlModifier
                                                           | Qt.KeyboardModifier.ShiftModifier)):
            self.screen.box_clicked(self.screen.box_item(self.enc["id"]) or self)


class WireItem(QGraphicsPathItem):
    """A wire between two connection points, drawn in the chosen style. The cables it
    carries (coax, jumper, USB) are named in a label at its middle. Past the radio a
    wire is the USB side and is drawn dashed when that option is on."""

    def __init__(self, screen: "ChainScreen", wire: dict):
        super().__init__()
        self.screen, self.wire = screen, wire
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        self.setAcceptHoverEvents(True)
        self.setZValue(1)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("A wire: click it to give it cables, drop a cable from the Cables list onto it, "
                        "or right-click for more")
        self.color = T.color("wire")
        self.digital = False
        self.hover = False
        self.label_rect = QRectF()
        self.label_text = ""
        self.refresh()

    def cables(self) -> list:
        return self.wire.get("cables") or []

    def ends(self):
        am = self.screen.anchor_map
        return am.get((self.wire["from"], "out", self.wire["port"])), am.get((self.wire["to"], "in", 0))

    def is_mated(self) -> bool:
        return R.mated(self.wire)

    def clash(self) -> bool:
        """Two males or two females mated: they cannot screw together (a Checks warning too)."""
        nodes = {n["id"]: n for n in self.screen.rig["nodes"]}
        a, b = nodes.get(self.wire["from"]), nodes.get(self.wire["to"])
        if a is None or b is None:
            return False
        ga, gb = R.port_gender(a, "out", self.wire["port"]), R.port_gender(b, "in")
        return bool(ga and ga == gb)

    def refresh(self):
        a, b = self.ends()
        if a is None or b is None:
            return
        self.prepareGeometryChange()
        if self.is_mated():                # a direct mating: a short connector stub across a small gap
            stub = QPainterPath(QPointF(a[0], a[1]))
            stub.lineTo(b[0], b[1])
            self.setPath(stub)
            self.label_text, self.label_rect = "", QRectF()
            self.setToolTip("Mated directly, port to port (SMA parts screwed together). In the recorded text "
                            "it is the same as a wire with no cable. Click it, then Delete, to separate them."
                            + ("\nM to M or F to F: these won't screw together." if self.clash() else ""))
            return
        self.setPath(wire_path(a, b, self.screen.wire_style))
        hot = self.isSelected() or self.hover
        cab = self.cables()
        width = T.WIRE_HOT_W if hot else T.WIRE_W
        pen = QPen(T.color("survey") if self.isSelected() else self.color, width,
                   Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        if self.digital and self.screen.dash_usb:
            pen.setDashPattern([4, 3])
        self.setPen(pen)
        self.label_text = "  +  ".join(cable_summary(c) for c in cab) if self.screen.show_labels else ""
        if self.label_text:
            fm = QFontMetricsF(T.ui_font(8.5))
            w = min(fm.horizontalAdvance(self.label_text) + 18, 260)
            h = fm.height() + 6
            mid = self.path().pointAtPercent(0.5)
            self.label_rect = QRectF(mid.x() - w / 2, mid.y() - h / 2, w, h)
        else:
            self.label_rect = QRectF()

    def label_point(self) -> QPointF:
        return self.path().pointAtPercent(0.5)

    def joint_rect(self) -> QRectF:
        """The collar half way along the stub between two mated points."""
        a, b = self.ends()
        if a is None:
            return QRectF()
        mx, my = (a[0] + b[0]) / 2 if b else a[0], (a[1] + b[1]) / 2 if b else a[1]
        across = a[2] in ("left", "right")
        w, h = (8, 20) if across else (20, 8)
        return QRectF(mx - w / 2, my - h / 2, w, h)

    def boundingRect(self):
        if self.is_mated():
            return self.joint_rect().adjusted(-6, -6, 6, 6)
        r = super().boundingRect()
        return r.united(self.label_rect.adjusted(-2, -2, 2, 2)) if not self.label_rect.isNull() else r

    def shape(self):
        if self.is_mated():
            p = QPainterPath()
            p.addRoundedRect(self.joint_rect().adjusted(-4, -4, 4, 4), 6, 6)
            return p
        st = QPainterPathStroker()
        st.setWidth(12)
        out = st.createStroke(self.path())
        if not self.label_rect.isNull():
            out.addRoundedRect(self.label_rect, 8, 8)
        return out

    def paint(self, qp: QPainter, opt, widget=None):
        qp.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.is_mated():                 # the joint: a connector stub with a collar in the middle
            r = self.joint_rect()
            colour = T.color("alert") if self.clash() else (
                T.color("survey") if self.isSelected() else (T.color("text2") if self.hover else self.color))
            qp.setPen(QPen(T.color("background"), 9, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            qp.drawPath(self.path())
            qp.setPen(QPen(colour, 5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            qp.drawPath(self.path())
            qp.setPen(QPen(T.color("background"), 1.5))
            qp.setBrush(colour)
            qp.drawRoundedRect(r, 3, 3)
            return
        qp.setPen(self.pen())
        qp.setBrush(Qt.BrushStyle.NoBrush)
        qp.drawPath(self.path())
        if self.label_text:
            first = self.cables()[0].get("type")
            edge = T.color("survey") if self.isSelected() else cable_colour(first)
            qp.setPen(QPen(edge, 1.2))
            qp.setBrush(T.color("panel"))
            qp.drawRoundedRect(self.label_rect, self.label_rect.height() / 2, self.label_rect.height() / 2)
            qp.setPen(T.color("text2"))
            qp.setFont(T.ui_font(8.5))
            qp.drawText(self.label_rect.adjusted(9, 0, -9, 0), Qt.AlignmentFlag.AlignCenter,
                        qp.fontMetrics().elidedText(self.label_text, Qt.TextElideMode.ElideRight,
                                                    int(self.label_rect.width() - 18)))

    def hoverEnterEvent(self, ev):
        self.hover = True
        self.refresh()

    def hoverLeaveEvent(self, ev):
        self.hover = False
        self.refresh()

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            self.refresh()
            self.screen.selection_changed()
        return super().itemChange(change, value)


class PowerItem(QGraphicsObject):
    """A power source (USB-C lead, injector, battery, the radio's bias-tee). Drag from its
    dot to a part that needs power. Drawing only: it and its power wires live in their own
    lists in chain_layout.json and no path ever walks them."""

    def __init__(self, screen: "ChainScreen", box: dict):
        super().__init__()
        self.screen, self.box = screen, box
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsMovable
                      | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
                      | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges)
        if R.is_locked(box):
            self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
        self.setAcceptHoverEvents(True)
        self.setPos(box["x"], box["y"])
        self.hover = False
        self.hot = False
        self._press = None
        self._resize = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setToolTip("Power source: click to edit it; drag from its dot to the part it powers")

    def wh(self) -> tuple[int, int]:
        return G.power_size(self.box)

    def shape_name(self) -> str:
        s = self.box.get("shape")
        return s if s in R.SHAPES and s != "pipe" else R.DEFAULT_SHAPE

    def boundingRect(self) -> QRectF:
        m = PORT_R + 10
        w, h = self.wh()
        return QRectF(-m, -m, w + 2 * m, h + 2 * m)

    def shape(self) -> QPainterPath:
        w, h = self.wh()
        p = QPainterPath()
        p.setFillRule(Qt.FillRule.WindingFill)
        p.addPath(outline_path(self.shape_name(), w, h))
        p.addEllipse(self.out_point() - self.pos(), PORT_HIT, PORT_HIT)
        if self.isSelected():
            for c in handle_points(w, h).values():
                p.addRect(QRectF(c.x() - 7, c.y() - 7, 14, 14))
        return p

    def on_grip(self, scene_pt: QPointF) -> str | None:
        if not self.isSelected() or R.is_locked(self.box):
            return None
        w, h = self.wh()
        return handle_at(self.mapFromScene(scene_pt), w, h)

    def out_point(self) -> QPointF:
        a = self.screen.power_out.get(self.box["id"])
        w, h = self.wh()
        return QPointF(a[0], a[1]) if a else self.mapToScene(QPointF(w, h / 2))

    def on_dot(self, scene_pt: QPointF) -> bool:
        return (self.out_point() - scene_pt).manhattanLength() <= PORT_R * 2.4

    def paint(self, qp: QPainter, opt, widget=None):
        w, h = self.wh()
        sel = self.isSelected()
        body = outline_path(self.shape_name(), w, h)
        qp.setRenderHint(QPainter.RenderHint.Antialiasing)
        qp.setPen(Qt.PenStyle.NoPen)
        for dy, _spread, alpha in T.SHADOW_LAYERS[:2]:
            qp.setBrush(T.color("shadow", alpha))
            qp.drawPath(body.translated(0, dy))
        if sel:
            qp.setBrush(Qt.BrushStyle.NoBrush)
            for width, alpha in ((12, 0.07), (7, 0.13)):
                qp.setPen(QPen(T.color("survey", alpha), width))
                qp.drawPath(body)
        own = self.box.get("colour") if T.is_hex(self.box.get("colour")) else None
        fill = QColor(own) if own else T.color("power_bg")
        main, soft = (QColor(c) for c in T.text_on(own)) if own else (T.color("text"), T.color("power"))
        if self.hover and not sel and not own:
            fill = QColor(fill).lighter(115)
        qp.setBrush(fill)
        qp.setPen(QPen(T.color("survey") if sel else (QColor(T.mix(own, main.name(), 0.35)) if own else T.color("power")),
                       T.SELECTED_BORDER if sel else T.BORDER, Qt.PenStyle.SolidLine))
        qp.drawPath(body)
        box = text_box(self.shape_name(), w, h)
        dpr = widget.devicePixelRatioF() if widget is not None else 2.0
        qp.drawPixmap(QPointF(box.left(), h / 2 - 8), kind_pixmap(R.POWER, soft, 16, dpr))
        x0, tw = box.left() + 22, max(20, box.right() - box.left() - 22)
        pos = R.text_pos(self.box)
        top = max(3, {"t": 5, "m": h / 2 - 17, "b": h - 37}[pos[0]])
        halign = {"l": Qt.AlignmentFlag.AlignLeft, "c": Qt.AlignmentFlag.AlignHCenter,
                  "r": Qt.AlignmentFlag.AlignRight}[pos[1]]
        qp.setPen(soft)
        qp.setFont(T.ui_font(8))
        qp.drawText(QRectF(x0, top, tw, 14), halign, "Power")
        qp.setPen(main)
        qp.setFont(T.ui_font(9.5, QFont.Weight.DemiBold))
        qp.drawText(QRectF(x0, top + 15, tw, 18), halign,
                    qp.fontMetrics().elidedText(self.box.get("name") or "power source", Qt.TextElideMode.ElideRight,
                                                int(tw)))
        if R.is_locked(self.box):
            qp.drawPixmap(QPointF(w - 18, 4), kind_pixmap("lock", T.color("lock"), 12, dpr))
        c = self.out_point() - self.pos()
        r = PORT_R + 2 if self.hot else PORT_R
        qp.setPen(QPen(T.color("background"), 2))
        qp.setBrush(T.color("power"))
        qp.drawEllipse(c, r, r)
        if sel and not R.is_locked(self.box) and self.screen.sel_count == 1:
            draw_handles(qp, w, h)

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionChange and not self.screen.animating:
            v = self.screen.snapped(self, value)
            return self.screen.guide(self, v)
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            self.box["x"], self.box["y"] = int(round(self.pos().x())), int(round(self.pos().y()))
            self.screen.drag_moved(self)
            self.screen._refresh_wires()
        if change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            self.screen.selection_changed()
        return super().itemChange(change, value)

    def hoverEnterEvent(self, ev):
        self.hover = True
        self.update()

    def hoverLeaveEvent(self, ev):
        self.hover = self.hot = False
        self.update()

    def hoverMoveEvent(self, ev):
        grip = self.on_grip(ev.scenePos())
        if grip and not self.on_dot(ev.scenePos()):
            self.setCursor(HANDLE_CURSOR[grip])
            return
        hot = self.on_dot(ev.scenePos())
        if hot != self.hot:
            self.hot = hot
            self.update()
            self.setToolTip("Drag from this dot to the part it powers" if hot else
                            ("Locked: it stays where it is" if R.is_locked(self.box) else
                             "Power source: click to edit · drag to move · right-click to format and arrange"))
        self.setCursor(Qt.CursorShape.CrossCursor if hot else
                       (Qt.CursorShape.ArrowCursor if R.is_locked(self.box) else Qt.CursorShape.OpenHandCursor))

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.RightButton:
            if not self.isSelected():
                self.scene().clearSelection()
                self.screen.select_with_group(self)
            ev.accept()
            return
        if ev.button() != Qt.MouseButton.LeftButton:
            ev.ignore()
            return
        grip = self.on_grip(ev.scenePos())
        if grip and not self.on_dot(ev.scenePos()):        # a handle (Shift there keeps the proportions)
            b = self.box
            self._resize = (grip, ev.scenePos(), (b["x"], b["y"], *self.wh()))
            ev.accept()
            return
        if ev.button() == Qt.MouseButton.LeftButton and ev.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self.screen.shift_toggle(self)
            self._shifted = True     # its release must not let Qt narrow the selection to this box
            ev.accept()
            return
        if ev.button() == Qt.MouseButton.LeftButton and self.on_dot(ev.scenePos()):
            self.screen.start_power_wire(self, ev.scenePos())
            ev.accept()
            return
        grip = self.on_grip(ev.scenePos())
        if grip:
            b = self.box
            self._resize = (grip, ev.scenePos(), (b["x"], b["y"], *self.wh()))
            ev.accept()
            return
        if ev.button() == Qt.MouseButton.LeftButton:
            self.screen.group_press(self, ev.modifiers())
        self._press = self.pos()
        self._press_at = ev.screenPos()
        super().mousePressEvent(ev)
        self.screen.begin_drag(self)

    def mouseMoveEvent(self, ev):
        if self._resize is None and not_yet_a_drag(self, ev):
            return
        if self._resize is not None:
            grip, start, rect0 = self._resize
            d = ev.scenePos() - start
            x, y, w, h = G.resize(rect0, grip, d.x(), d.y(),
                                  keep_ratio=bool(ev.modifiers() & Qt.KeyboardModifier.ShiftModifier),
                                  snap=R.GRID if self.screen.snap_on else None,
                                  min_w=G.POWER_MIN_W, min_h=G.POWER_MIN_H)
            self.prepareGeometryChange()
            self.box["w"], self.box["h"] = w, h
            self.screen.animating = True
            self.setPos(x, y)
            self.screen.animating = False
            self.box["x"], self.box["y"] = x, y
            self.screen._refresh_wires()
            return
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        if ev.button() != Qt.MouseButton.LeftButton:
            ev.accept()
            return
        if getattr(self, "_shifted", False):
            self._shifted = False
            ev.accept()
            return
        if self._resize is not None:
            rect0 = self._resize[2]
            self._resize = None
            if (self.box["x"], self.box["y"], *self.wh()) != rect0:
                self.screen.changed()
            return
        super().mouseReleaseEvent(ev)
        moved = self._press is not None and self.pos() != self._press
        clicked = is_click(self, ev) and not moved
        self.screen.end_drag(self)
        self.screen.group_release(self, moved)
        self._press = None
        if moved:
            self.screen.changed()
        if clicked and not (ev.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)):
            self.screen.box_clicked(self.screen.box_item(self.box["id"]) or self)


class PowerWireItem(QGraphicsPathItem):
    """A power wire: dashed, in the power colour, so it never looks like an RF cable."""

    def __init__(self, screen: "ChainScreen", wire: dict):
        super().__init__()
        self.screen, self.wire = screen, wire
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("A power wire (drawing only): click it, then Delete, to take it out")
        self.hover = False
        self.refresh()

    def refresh(self):
        a = self.screen.power_out.get(self.wire["from"])
        b = self.screen.power_map.get(self.wire["to"])
        if a is None or b is None:
            return
        self.prepareGeometryChange()
        self.setPath(wire_path(a, b, self.screen.wire_style, b[2]))
        hot = self.isSelected() or self.hover
        pen = QPen(T.color("survey") if self.isSelected() else T.color("power"),
                   T.WIRE_HOT_W if hot else T.WIRE_W, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap,
                   Qt.PenJoinStyle.RoundJoin)
        pen.setDashPattern([3, 2.5])
        self.setPen(pen)

    def shape(self):
        st = QPainterPathStroker()
        st.setWidth(12)
        return st.createStroke(self.path())

    def hoverEnterEvent(self, ev):
        self.hover = True
        self.refresh()

    def hoverLeaveEvent(self, ev):
        self.hover = False
        self.refresh()

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            self.refresh()
            self.screen.selection_changed()
        return super().itemChange(change, value)


class ChainScene(QGraphicsScene):
    def __init__(self, screen: "ChainScreen"):
        super().__init__(screen)
        self.screen = screen

    def drawBackground(self, qp: QPainter, rect: QRectF):
        qp.fillRect(rect, T.color("plot"))
        o = self.screen.grid
        if o["style"] == "none":
            return
        g = R.GRID
        big = g * 5 if o["major"] else 0
        x0, y0 = math.floor(rect.left() / g) * g, math.floor(rect.top() / g) * g
        xs = list(range(int(x0), int(rect.right()) + g, g))
        ys = list(range(int(y0), int(rect.bottom()) + g, g))
        if len(xs) * len(ys) > 200000:          # zoomed far out: the grid would be a smear
            return

        def is_big(v):
            return big and v % big == 0

        if o["style"] == "lines":
            minor = [QLineF(x, rect.top(), x, rect.bottom()) for x in xs if not is_big(x)]
            minor += [QLineF(rect.left(), y, rect.right(), y) for y in ys if not is_big(y)]
            pen = QPen(T.color("lines", 0.6), 0)
            if o["dotted"]:
                pen.setStyle(Qt.PenStyle.DotLine)
            qp.setPen(pen)
            qp.drawLines(minor)
            if big:
                major = [QLineF(x, rect.top(), x, rect.bottom()) for x in xs if is_big(x)]
                major += [QLineF(rect.left(), y, rect.right(), y) for y in ys if is_big(y)]
                pen = QPen(T.color("line_strong"), 0)
                if o["dotted"]:
                    pen.setStyle(Qt.PenStyle.DashLine)
                qp.setPen(pen)
                qp.drawLines(major)
            return
        qp.setPen(QPen(T.color("lines"), 2))
        qp.drawPoints([QPointF(x, y) for x in xs for y in ys if not (is_big(x) and is_big(y))])
        if big:
            qp.setPen(QPen(T.color("line_strong"), 3.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            qp.drawPoints([QPointF(x, y) for x in xs if is_big(x) for y in ys if is_big(y)])


class ChainView(QGraphicsView):
    def __init__(self, screen: "ChainScreen", scene: QGraphicsScene):
        super().__init__(scene)
        self.screen = screen
        self.setAcceptDrops(True)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._pan = None
        self._space = False
        self.locked_click = None       # (locked enclosure, scene point) between a press on it and the release
        self.menu_open = False         # one right-click, one menu (relay 006)
        self._menu_at = (0.0, None)    # when and where the last menu was opened

    def dragEnterEvent(self, ev):
        if ev.mimeData().hasFormat(MIME):
            ev.acceptProposedAction()

    def dragMoveEvent(self, ev):
        if ev.mimeData().hasFormat(MIME):
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        t = bytes(ev.mimeData().data(MIME)).decode()
        pt = self.mapToScene(ev.position().toPoint())
        if t.startswith(CABLE_MIME_PREFIX):
            self.screen.add_cable_at(t[len(CABLE_MIME_PREFIX):], pt)
        else:
            before = {n["id"] for n in self.screen.rig["nodes"]}
            self.screen.add_part(t, pt.x() - R.PART_W / 2, pt.y() - R.PART_H / 2)
            new = [n for n in self.screen.rig["nodes"] if n["id"] not in before]
            if new and new[0]["part"]["type"] == "antenna" and not R.antenna(new[0]).get("type"):
                nid = new[0]["id"]
                QTimer.singleShot(0, lambda: self.screen.choose_antenna(nid, QCursor.pos()))
        ev.acceptProposedAction()

    def _start_pan(self, ev):
        self._pan = ev.position().toPoint()
        self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.MiddleButton or (ev.button() == Qt.MouseButton.LeftButton and self._space):
            self._start_pan(ev)                                # middle button, or Space + drag, pans
            return
        self.locked_click = None
        top = next((i for i in self.scene().items(self.mapToScene(ev.position().toPoint()))
                    if isinstance(i, (PartItem, EnclosureItem, PowerItem, WireItem, PowerWireItem))), None)
        if top is None or isinstance(top, (WireItem, PowerWireItem)) or self.screen.box_id(top) != self.screen.card_wanted:
            self.screen.card_wanted = None       # a press anywhere else forgets which card was asked for
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if self._pan is not None:
            p = ev.position().toPoint()
            d = p - self._pan
            self._pan = p
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - d.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - d.y())
            return
        if self.screen.wiring_from is not None:
            self.screen.drag_wire(self.mapToScene(ev.position().toPoint()))
            return
        if self.screen.power_from is not None:
            self.screen.drag_power_wire(self.mapToScene(ev.position().toPoint()))
            return
        self.screen.grid_status.refresh(self.mapToScene(ev.position().toPoint()))
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        if self._pan is not None and ev.button() in (Qt.MouseButton.MiddleButton, Qt.MouseButton.LeftButton):
            self._pan = None
            if self._space:
                self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
            else:
                self.viewport().unsetCursor()
            return
        if self.screen.wiring_from is not None:
            self.screen.end_wire(self.mapToScene(ev.position().toPoint()))
            return
        if self.screen.power_from is not None:
            self.screen.end_power_wire(self.mapToScene(ev.position().toPoint()))
            return
        lc, self.locked_click = self.locked_click, None
        super().mouseReleaseEvent(ev)
        if lc is not None and ev.button() == Qt.MouseButton.LeftButton:
            enc, at = lc
            if QLineF(at, self.mapToScene(ev.position().toPoint())).length() < 4:
                self.scene().clearSelection()                  # a plain click on a locked box selects it
                self.screen.select_with_group(enc)
                self.screen.box_clicked(enc)                   # and opens its card (relay 008)

    def wheelEvent(self, ev):
        """The wheel zooms round the pointer; Shift + wheel scrolls up and down (the owner's
        choice, relay 006). A sideways swipe on a touchpad scrolls sideways."""
        dy = ev.angleDelta().y()
        if ev.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            d = dy or ev.angleDelta().x()          # some systems send Shift + wheel as a sideways turn
            bar = self.verticalScrollBar()
            bar.setValue(bar.value() - round(d / 120 * 60))
            ev.accept()
            return
        if not dy:
            return super().wheelEvent(ev)
        # Zoom, then scroll so the point under the pointer stays under it. (Qt's own anchor
        # uses the last mouse-move position, which can lag the wheel's.)
        p = ev.position().toPoint()
        old = self.mapToScene(p)
        anchor = self.transformationAnchor()
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.screen.zoom_by(1.15 if dy > 0 else 1 / 1.15)
        self.setTransformationAnchor(anchor)
        d = self.mapFromScene(old) - p
        self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() + d.x())
        self.verticalScrollBar().setValue(self.verticalScrollBar().value() + d.y())

    def contextMenuEvent(self, ev):
        """One right-click gives exactly one menu (relay 006): the part's on a part, the wire's on a
        wire, the grid's on empty grid. A second context event for the same click (Windows can send
        one on the release and another for the same click) is swallowed: none while a menu is
        open, and none within half a second at the same place."""
        import time
        ev.accept()
        when, where = self._menu_at
        if self.menu_open or (time.monotonic() - when < 0.5 and where is not None
                              and (where - ev.globalPos()).manhattanLength() < 6):
            return
        pt = self.mapToScene(ev.pos())
        items = self.scene().items(pt)
        top = next((i for i in items if isinstance(i, (PartItem, EnclosureItem, PowerItem, WireItem, PowerWireItem))),
                   None)
        m = Menu(self)
        m.setToolTipsVisible(True)
        if isinstance(top, (PartItem, EnclosureItem, PowerItem)):
            if not top.isSelected():
                self.scene().clearSelection()
                self.screen.select_with_group(top)
            self.screen.fill_box_menu(m, top)
        elif isinstance(top, (WireItem, PowerWireItem)):
            self.scene().clearSelection()
            top.setSelected(True)
            self.screen.fill_wire_menu(m, top)
        else:
            self.screen.fill_grid_menu(m, pt)
        self.menu_open = True
        self._menu_at = (time.monotonic(), ev.globalPos())
        try:
            m.exec(ev.globalPos())
        finally:
            self.menu_open = False
            self._menu_at = (time.monotonic(), ev.globalPos())

    def keyPressEvent(self, ev):
        if self.screen.key(ev):
            ev.accept()
            return
        if ev.key() == Qt.Key.Key_Space and not ev.isAutoRepeat():
            self._space = True
            self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
            ev.accept()
            return
        super().keyPressEvent(ev)

    def keyReleaseEvent(self, ev):
        if ev.key() == Qt.Key.Key_Space and not ev.isAutoRepeat():
            self._space = False
            if self._pan is None:
                self.viewport().unsetCursor()
            ev.accept()
            return
        super().keyReleaseEvent(ev)

    def focusOutEvent(self, ev):
        self._space = False
        super().focusOutEvent(ev)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self.screen._place_empty_hint()
        if hasattr(self.screen, "grid_status"):
            self.screen.grid_status.refresh()
        if self.screen.card.isVisible():
            self.screen.card._fit()
            self.screen._place_card()


LIB_MIME_PREFIX = "lib:"
POWER_MIME_PREFIX = "power:"


class PartsList(QListWidget):
    """The palette. One list for physical parts (with the owner's own parts, housings and
    power sources), one for cables (which go on wires). Tiles are drawn by TileDelegate."""

    def __init__(self, labels: dict, cables: bool = False):
        super().__init__()
        self.cables = cables
        self.setObjectName("Palette")
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.setMouseTracking(True)
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setItemDelegate(TileDelegate(self))
        self.colour_on = True
        self.library: list = []
        self.pressed_row = None
        self.on_library_menu = None        # set by the screen: (entry or None, global point) -> None
        self.set_labels(labels)

    def set_library(self, entries: list):
        self.library = [e for e in entries if not e.get("removed")]
        self.set_labels(self.labels)

    def _entry(self, text: str, key: str, icon: str, cat: str | None, tip: str):
        it = QListWidgetItem(text)
        it.setData(Qt.ItemDataRole.UserRole, key)
        it.setData(ROLE_KIND, "item")
        it.setData(ROLE_ICON, icon)
        it.setData(ROLE_CAT, cat or "")
        it.setToolTip(tip)
        self.addItem(it)

    def resizeEvent(self, ev):
        """Rows are measured against the list's width (a hint wraps), so measure them again when it
        changes: measured once while the list was still narrow, the empty My parts hint stood 138 px
        tall and pushed the housings below the fold (found in relay 008)."""
        super().resizeEvent(ev)
        if ev.oldSize().width() != ev.size().width():
            self.doItemsLayout()

    def filter(self, text: str):
        """Show only entries whose name contains the text (any case); a heading shows while any entry
        under it does; hints hide while searching (relay 007: search in the Parts list)."""
        q = text.strip().lower()
        head = None
        shown_under = {}
        for i in range(self.count()):
            it = self.item(i)
            kind = it.data(ROLE_KIND)
            if kind == "heading":
                head = i
                shown_under[head] = False
                continue
            if kind == "hint":
                it.setHidden(bool(q))
                continue
            hide = bool(q) and q not in (it.text() or "").lower()
            it.setHidden(hide)
            if not hide and head is not None:
                shown_under[head] = True
        for i, any_shown in shown_under.items():
            self.item(i).setHidden(bool(q) and not any_shown)
        if self.cables and self.count():
            self.setFixedHeight(sum(self.sizeHintForRow(i) for i in range(self.count())
                                    if not self.item(i).isHidden()) + 6)

    def set_colour_on(self, on: bool):
        """Colour coding on or off: nothing is rebuilt; the tiles are simply painted again."""
        self.colour_on = on
        self.viewport().update()

    def set_labels(self, labels: dict):
        self.labels = labels
        self.clear()
        if not self.cables:
            self._heading("MY PARTS")
            if not self.library:
                self._hint("Nothing here yet. Right-click a part on the grid and choose \"Save to my parts\" "
                           "to keep it, with its name and note, for next time.")
            for e in self.library:
                t = e["part"]["type"]
                name = e["part"].get("name") or labels.get(t, {}).get("label", t)
                if e["part"].get("detail"):
                    name += f" ({e['part']['detail']})"
                self._entry(name, LIB_MIME_PREFIX + e["id"], t, CATEGORY.get(t),
                            "Your own part: drops with this name and note every time. Right-click to rename, "
                            "edit or remove it from this list (rigs that used it keep their parts).")
            self._heading("HOUSINGS · drawing only", "enclosure")
            for key, name in (("aluminium", "Enclosure (aluminium)"), ("plastic", "Enclosure (plastic)")):
                self._entry(name, f"{R.ENCLOSURE}:{key}", R.ENCLOSURE, "enclosure",
                            f"Drag onto the grid: a box called '{R.ENC_PRESETS[key]}' drawn behind parts, such as "
                            "the radio's cage. Drawing only: it writes nothing into the recorded text.")
            self._heading("POWER · drawing only", R.POWER)
            for key, name in R.POWER_KINDS.items():
                self._entry(name, POWER_MIME_PREFIX + key, R.POWER, R.POWER,
                            "Drag onto the grid, then drag from its dot to the part it powers. Power wires are "
                            "drawing only: they never enter the recorded text.")
            self._heading("SMA · drop onto a dot to mate", "sma")
            for t in R.SMA_TYPES:
                if t in labels:
                    self._entry(labels[t]["label"], t, t, "sma",
                                f"Drag onto the grid to add a {labels[t]['label']}. Drop it with a dot on another "
                                "part's dot and the two mate directly, port to port, with no wire.")
            self._heading("PARTS")
        order = PALETTE_ORDER + [t for t in labels if t not in PALETTE_ORDER]
        for t in order:
            if t not in labels or ((t in R.CABLE_TYPES) != self.cables) or (not self.cables and t in R.SMA_TYPES):
                continue
            if self.cables:
                cat = t
                tip = f"Drop onto a wire: the wire then carries a {labels[t]['label']}."
            else:
                cat = CATEGORY.get(t)
                tip = labels[t].get("note") or f"Drag onto the grid to add a {labels[t]['label']}."
                tip = tip[0].upper() + tip[1:] + ("" if tip.endswith(".") else ".")
            self._entry(labels[t]["label"], t, t, cat, tip)
        if self.cables and self.count():
            self.setFixedHeight(sum(self.sizeHintForRow(i) for i in range(self.count())) + 6)

    def _heading(self, text: str, cat: str | None = None):
        it = QListWidgetItem(text)
        it.setFlags(Qt.ItemFlag.NoItemFlags)
        it.setData(ROLE_KIND, "heading")
        it.setData(ROLE_CAT, cat or "")
        self.addItem(it)

    def _hint(self, text: str):
        it = QListWidgetItem(text)
        it.setData(ROLE_KIND, "hint")
        it.setFlags(Qt.ItemFlag.NoItemFlags)
        self.addItem(it)

    def mousePressEvent(self, ev):
        it = self.itemAt(ev.position().toPoint())
        if ev.button() == Qt.MouseButton.LeftButton and it is not None and it.data(ROLE_KIND) == "item":
            self.pressed_row = self.row(it)
            self.viewport().update()
        super().mousePressEvent(ev)

    def mouseReleaseEvent(self, ev):
        self.pressed_row = None
        self.viewport().update()
        super().mouseReleaseEvent(ev)

    def leaveEvent(self, ev):
        self.pressed_row = None
        self.viewport().update()
        super().leaveEvent(ev)

    def contextMenuEvent(self, ev):
        if self.cables or self.on_library_menu is None:
            return
        it = self.itemAt(ev.pos())
        key = it.data(Qt.ItemDataRole.UserRole) if it is not None else None
        if key and str(key).startswith(LIB_MIME_PREFIX):
            entry = next((e for e in self.library if e["id"] == key[len(LIB_MIME_PREFIX):]), None)
            self.on_library_menu(entry, ev.globalPos())
        elif it is not None and it.text() == "MY PARTS":
            self.on_library_menu(None, ev.globalPos())

    def startDrag(self, actions):
        self.pressed_row = None
        it = self.currentItem()
        if not it or not it.data(Qt.ItemDataRole.UserRole):
            return
        md = QMimeData()
        key = it.data(Qt.ItemDataRole.UserRole)
        md.setData(MIME, ((CABLE_MIME_PREFIX if self.cables else "") + key).encode())
        d = QDrag(self)
        d.setMimeData(md)
        colour = palette_colour(it.data(ROLE_CAT), self.colour_on)
        d.setPixmap(kind_pixmap(it.data(ROLE_ICON) or "custom", colour, 24, self.devicePixelRatioF()))
        d.exec(Qt.DropAction.CopyAction)


# ---------------------------------------------------------------- Output and the grid's status (relay 007)
class OutputPanel(QFrame):
    """Technical data for what is selected, or for the whole rig: labels muted, values in the mono
    font, a Copy button for the panel's text. Read-only: it never changes the drawing."""

    def __init__(self, screen):
        super().__init__()
        self.screen = screen
        self.setObjectName("TextPanel")
        self.setMinimumWidth(240)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(T.SPACE * 2, T.SPACE + 4, T.SPACE * 2, T.SPACE + 4)
        lay.setSpacing(T.SPACE // 2)
        head = QHBoxLayout()
        t = QLabel("Output")
        t.setFont(T.ui_font(10, QFont.Weight.DemiBold))
        self.what = ElidedLabel(tip="What Output is showing")
        self.what.setProperty("muted", True)
        self.what.setFont(T.ui_font(8.5))
        self.copy_btn = QPushButton("Copy")
        self.copy_btn.setToolTip("Copy everything in this panel as plain text")
        self.copy_btn.clicked.connect(self.copy)
        head.addWidget(t)
        head.addWidget(self.what, 1)
        head.addWidget(self.copy_btn)
        lay.addLayout(head)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
        self.body = QWidget()
        self.grid = QGridLayout(self.body)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(T.SPACE * 1.5)
        self.grid.setVerticalSpacing(3)
        self.scroll.setWidget(self.body)
        lay.addWidget(self.scroll, 1)
        self.sections: list = []

    def show_sections(self, what: str, sections: list):
        """sections: [(title, [(label, value), ...]), ...]"""
        self.what.setText(what)
        self.sections = sections
        while self.grid.count():
            w = self.grid.takeAt(0).widget()
            if w:
                w.hide()
                w.deleteLater()
        row = 0
        for title, rows in sections:
            h = QLabel(title.upper())
            h.setProperty("muted", True)
            h.setFont(T.ui_font(7.5, QFont.Weight.DemiBold))
            h.setContentsMargins(0, T.SPACE if row else 0, 0, 2)
            self.grid.addWidget(h, row, 0, 1, 2)
            row += 1
            for label, value in rows:
                lab = QLabel(label)
                lab.setProperty("muted", True)
                lab.setFont(T.ui_font(8.5))
                lab.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
                val = QLabel(str(value))
                val.setFont(T.mono_font(9))
                val.setWordWrap(True)
                val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                self.grid.addWidget(lab, row, 0)
                self.grid.addWidget(val, row, 1)
                row += 1
        self.grid.setColumnStretch(1, 1)
        self.grid.setRowStretch(row, 1)

    def text(self) -> str:
        out = [f"Output: {self.what.text()}"]
        for title, rows in self.sections:
            out.append("")
            out.append(title)
            out += [f"  {label}: {value}" for label, value in rows]
        return "\n".join(out)

    def copy(self):
        QApplication.clipboard().setText(self.text())
        self.screen.status.emit("Output copied")


class GridStatus(QWidget):
    """A small, quiet overlay in the grid's bottom-right corner: where the pointer is in grid squares,
    the zoom (click it for actual size), a scale bar of one grid square, and how much is selected. It
    moves to the other corner, or fades, rather than cover a part."""

    def __init__(self, screen, view):
        super().__init__(view)
        self.screen, self.view = screen, view
        self.xy = (0, 0)
        self.setToolTip("Pointer position in grid squares, zoom (click for actual size), one grid square, "
                        "and how many boxes are selected")
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.zoom_rect = QRectF()
        self.faded = False

    def text_parts(self):
        z = self.view.transform().m11()
        n = self.screen.sel_count
        return (f"X {self.xy[0]}  Y {self.xy[1]}", f"{round(z * 100)} %",
                f"{n} selected" if n else "nothing selected")

    def sizeHint(self):
        fm = QFontMetricsF(T.ui_font(8))
        a, b, c = self.text_parts()
        sq = R.GRID * self.view.transform().m11()
        return QSize(int(fm.horizontalAdvance(a) + fm.horizontalAdvance(b) + fm.horizontalAdvance(c)
                         + max(sq, 8) + fm.horizontalAdvance("1 square") + 70), 24)

    def paintEvent(self, ev):
        qp = QPainter(self)
        qp.setRenderHint(QPainter.RenderHint.Antialiasing)
        qp.setOpacity(0.3 if self.faded else 1.0)
        qp.setPen(QPen(T.color("lines"), 1))
        qp.setBrush(T.color("panel", 0.85))
        qp.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 6, 6)
        qp.setFont(T.ui_font(8))
        fm = qp.fontMetrics()
        a, b, c = self.text_parts()
        x = 10.0
        h = self.height()
        for i, part in enumerate((a, b)):
            qp.setPen(T.color("muted") if i == 0 else T.color("survey_light"))
            qp.drawText(QPointF(x, h / 2 + fm.ascent() / 2 - 1), part)
            if i == 1:
                self.zoom_rect = QRectF(x - 4, 0, fm.horizontalAdvance(part) + 8, h)
            x += fm.horizontalAdvance(part) + 14
        sq = max(4.0, R.GRID * self.view.transform().m11())        # one grid square, at this zoom
        qp.setPen(QPen(T.color("text2"), 1.5))
        qp.drawLine(QPointF(x, h / 2 + 3), QPointF(x + sq, h / 2 + 3))
        qp.drawLine(QPointF(x, h / 2), QPointF(x, h / 2 + 5))
        qp.drawLine(QPointF(x + sq, h / 2), QPointF(x + sq, h / 2 + 5))
        x += sq + 6
        qp.setPen(T.color("muted"))
        qp.drawText(QPointF(x, h / 2 + fm.ascent() / 2 - 1), "1 square")
        x += fm.horizontalAdvance("1 square") + 14
        qp.drawText(QPointF(x, h / 2 + fm.ascent() / 2 - 1), c)
        qp.end()

    def mousePressEvent(self, ev):
        if self.zoom_rect.contains(ev.position()):
            self.screen.zoom_reset()
            self.refresh()
            ev.accept()
            return
        ev.ignore()

    def refresh(self, scene_pt: QPointF | None = None):
        if scene_pt is not None:
            self.xy = (math.floor(scene_pt.x() / R.GRID), math.floor(scene_pt.y() / R.GRID))
        self.resize(self.sizeHint())
        self.place()
        self.update()

    def place(self):
        """Bottom-right; bottom-left if a part is under that; faded if both corners are taken."""
        vp = self.view.viewport().geometry()
        w, h = self.width(), self.height()
        spots = [QPoint(vp.right() - w - 10, vp.bottom() - h - 10), QPoint(vp.left() + 10, vp.bottom() - h - 10)]
        try:
            boxes = [it.sceneBoundingRect() for it in self.screen.all_box_items()]
        except RuntimeError:                        # mid-redraw: the old items are being replaced
            return
        for spot in spots:
            tl = self.view.mapToScene(spot - vp.topLeft())
            br = self.view.mapToScene(spot - vp.topLeft() + QPoint(w, h))
            if not any(QRectF(tl, br).intersects(b) for b in boxes):
                self.move(spot)
                self.faded = False
                return
        self.move(spots[0])
        self.faded = True


# ---------------------------------------------------------------- the edit card
class EditCard(QFrame):
    def __init__(self, screen: "ChainScreen", parent):
        super().__init__(parent)
        self.screen = screen
        self.setObjectName("Card")
        self.setStyleSheet("QLabel { background: transparent; border: none; }")
        self.setFixedWidth(300)
        # The card has no menu of its own and must not pass a right-click on to the grid behind
        # it (one right-click, one menu: relay 006). Its text boxes keep their own edit menus.
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.PreventContextMenu)
        # The card scrolls when it is taller than the grid (an adapter's card is long; at 150 %
        # display scaling it ran off the bottom: relay 006).
        outer = QVBoxLayout(self)
        outer.setContentsMargins(1, 1, 1, 1)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
        self.inner = QWidget()
        self.inner.setObjectName("CardInner")
        self.inner.setStyleSheet("QWidget#CardInner { background: transparent; }")
        self.scroll.setWidget(self.inner)
        outer.addWidget(self.scroll)
        self.lay = QVBoxLayout(self.inner)
        self.lay.setContentsMargins(13, 11, 13, 11)
        self.lay.setSpacing(6)
        self.node = None
        self.wire = None
        self.enc = None
        self.power = None
        self.hide()

    def _clear(self):
        """Empty the card, rows of buttons included. (Before relay 006 the widgets inside a row's
        own layout were left behind, hidden but alive, so every card piled up old buttons.)"""
        def empty(lay):
            while lay.count():
                item = lay.takeAt(0)
                if item.widget() is not None:
                    item.widget().hide()
                    item.widget().deleteLater()
                elif item.layout() is not None:
                    empty(item.layout())
                    item.layout().deleteLater()
        empty(self.lay)

    def _field(self, label: str, widget: QWidget):
        lab = QLabel(label)
        lab.setProperty("muted", True)
        lab.setFont(T.ui_font(8.5))
        self.lay.addWidget(lab)
        self.lay.addWidget(widget)

    @staticmethod
    def number_edit(value, on_value, hint: str) -> QLineEdit:
        """A box for a nominal dB figure: +20, -3.5 (a typed − or a decimal comma are fine).
        Blank means "not known". A box that does not hold a number is outlined and stores nothing."""
        e = QLineEdit("" if value in (None, "") else f"{float(value):g}")
        e.setPlaceholderText(hint)
        e.setToolTip(hint)

        def edited(text):
            t = text.strip().replace("−", "-").replace(",", ".").replace(" ", "").removesuffix("dB").removesuffix("db")
            if not t:
                e.setStyleSheet("")
                on_value(None)
                return
            try:
                v = float(t)
            except ValueError:
                e.setStyleSheet(f"border-color: {T.C['alert']};")
                return
            e.setStyleSheet("")
            on_value(v)

        e.textEdited.connect(edited)
        return e

    def _row(self, *buttons):
        row = QHBoxLayout()
        row.setSpacing(T.SPACE)
        for b in buttons:
            row.addWidget(b)
        self.lay.addLayout(row)

    def _lock_button(self, box: dict, bid: str) -> QPushButton:
        b = QPushButton("Unlock" if R.is_locked(box) else "Lock")
        b.setToolTip("A locked box cannot be moved by accident: not by dragging, the arrow keys, Align or "
                     "Tidy (Ctrl+Shift+L)")
        b.clicked.connect(lambda: self.screen.set_lock([bid], not R.is_locked(box)))
        return b

    def _fit(self):
        """As tall as what it holds, but never taller than the grid it sits on: then it scrolls."""
        self.inner.adjustSize()
        room = self.parent().viewport().height() - 16 if self.parent() is not None else 2000
        self.setFixedHeight(max(120, min(self.inner.sizeHint().height() + 2, room)))

    def _note(self, text: str):
        lab = QLabel(text)
        lab.setWordWrap(True)
        lab.setProperty("muted", True)
        lab.setFont(T.ui_font(8.5))
        self.lay.addWidget(lab)
        return lab

    def _colour_row(self, box: dict, bid: str):
        """A row of palette swatches, a custom colour and "Use kind colour" (relay 006). The
        colour is the drawing's only: it never reaches the recorded text."""
        lab = QLabel("Colour")
        lab.setProperty("muted", True)
        lab.setFont(T.ui_font(8.5))
        self.lay.addWidget(lab)
        grid = QGridLayout()
        grid.setHorizontalSpacing(4)
        grid.setVerticalSpacing(4)
        cur = (box.get("colour") or "").lower()
        for k, (name, hexv) in enumerate(T.SWATCHES):
            b = QPushButton()
            b.setObjectName("Swatch")
            b.setFixedSize(22, 22)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            on = hexv.lower() == cur
            b.setToolTip(name + (" (chosen)" if on else ""))
            b.setStyleSheet(f"QPushButton#Swatch {{ background: {hexv}; border: {2 if on else 1}px "
                            f"solid {T.C['survey'] if on else T.C['line_strong']}; border-radius: 11px; "
                            f"padding: 0; min-height: 0; }}"
                            f"QPushButton#Swatch:hover {{ border: 2px solid {T.C['text']}; }}")
            b.clicked.connect(lambda _=False, h=hexv: self.screen.set_colour([bid], h))
            grid.addWidget(b, k // 6, k % 6)
        grid.setColumnStretch(6, 1)
        self.lay.addLayout(grid)
        custom = QPushButton("Custom…")
        custom.setToolTip("Pick any colour. Text on the box turns light or dark to stay readable.")
        custom.clicked.connect(lambda: self.screen.pick_colour([bid]))
        back = QPushButton("Use kind colour")
        back.setEnabled(bool(box.get("colour")))
        back.setToolTip("Go back to the colour of its kind" if box.get("colour") else
                        "It already uses the colour of its kind")
        back.clicked.connect(lambda: self.screen.set_colour([bid], None))
        self._row(custom, back)

    def _textpos_row(self, box: dict, bid: str):
        """Where the box's words sit: a 3 x 3 picker (relay 008). Drawing only."""
        pick = PosPicker(R.text_pos(box))
        pick.chosen.connect(lambda code: self.screen.set_text_pos([bid], code))
        self._field("Text position", pick)

    def _shape_row(self, box: dict, bid: str, default: str | None = None):
        """The node's shape (relay 007): a rectangle (relay 008), or one the owner picks. Drawing only."""
        c = QComboBox()
        c.addItem("Default (rectangle)", "")
        adapter = (box.get("part") or {}).get("type") == "adapter"
        for k, label in R.SHAPES.items():
            if k == "pipe" and not adapter:
                continue
            c.addItem(label, k)
        c.setCurrentIndex(max(0, c.findData(box.get("shape", ""))))
        c.setToolTip("How this box is drawn. Drawing only: the shape never enters the recorded text.")
        c.currentIndexChanged.connect(lambda i: self.screen.set_shape([bid], c.itemData(i) or None))
        self._field("Shape", c)

    def _antenna_rows(self, node: dict):
        """An antenna's type and its settings (relay 007). Drawing and Output only: they never enter
        the recorded text; the name and note are written exactly as before."""
        a = R.antenna(node)
        kind = QComboBox()
        kind.addItem("Not set", "")
        for k, (label, _f) in R.ANTENNA_TYPES.items():
            kind.addItem(QIcon(kind_pixmap("ant_" + k, T.color("text2"), 16, 2.0)), label, k)
        kind.setCurrentIndex(max(0, kind.findData(a.get("type", ""))))
        kind.setToolTip("The antenna's type: its shape on the grid follows, and it gets the settings that type has")
        kind.currentIndexChanged.connect(lambda i: self.screen.set_antenna(node["id"], type=kind.itemData(i) or None))
        self._field("Antenna type", kind)
        if not a.get("type"):
            return
        _label, fields = R.ANTENNA_TYPES.get(a["type"], R.ANTENNA_TYPES["custom"])
        for key, label in fields + R.ANTENNA_COMMON:
            if key in R.ANTENNA_NUMBERS:
                e = self.number_edit(a.get(key), lambda v, k=key: self.screen.set_antenna(node["id"], redraw=False,
                                                                                          **{k: v}), label)
            else:
                e = QLineEdit(str(a.get(key) or ""))
                e.setPlaceholderText(label)
                e.setToolTip(label)
                e.textEdited.connect(lambda v, k=key: self.screen.set_antenna(node["id"], redraw=False, **{k: v or None}))
            self._field(label, e)
        self._note("Antenna settings are drawing and Output only: they do not enter the recorded text. "
                   "Nominal gain is shown beside the estimate, not added into it.")

    def _sma_rows(self, node: dict):
        """An SMA part's kind: an adapter's shape, gender and turn (written into the recorded text
        as its detail, the owner's decision A2); a DC block's, pad's or bulkhead's gender (drawing only)."""
        t = node["part"]["type"]
        s = R.sma(node)
        if t == "adapter":
            shape = QComboBox()
            shape.addItem("Not chosen", "")
            for k in R.ADAPTER_SHAPES:
                shape.addItem({"straight": "Straight (barrel)", "90": "90° right angle", "T": "T (three ports)",
                               "180": "180° (U)"}[k], k)
            shape.setCurrentIndex(max(0, shape.findData(s.get("shape", ""))))
            shape.setToolTip("The adapter's shape. Its connection points move to suit: a 90° adapter has them on "
                             "two sides next to each other, a T has three.")
            shape.currentIndexChanged.connect(lambda i: self.screen.set_sma(node["id"], shape=shape.itemData(i)))
            self._field("Adapter shape", shape)
        gender = QComboBox()
        gender.addItem("Not chosen", "")
        for k, v in R.GENDERS.items():
            gender.addItem(f"{v}  ({'male' if k[0] == 'M' else 'female'} toward the source)", k)
        gender.setCurrentIndex(max(0, gender.findData(s.get("gender", ""))))
        gender.setToolTip("Which end is male (a pin) and which female (a socket), read from the source side. "
                          "On the grid a male port is a filled dot with a pin, a female port a ring.")
        gender.currentIndexChanged.connect(lambda i: self.screen.set_sma(node["id"], gender=gender.itemData(i)))
        self._field("Gender", gender)
        if t == "adapter":
            turn = QPushButton("Turn 90°")
            turn.setEnabled(bool(s.get("shape")))
            turn.setToolTip("Turn the adapter a quarter turn clockwise: its connection points move round."
                            if s.get("shape") else "Choose a shape first: a plain adapter has no fixed sides to turn")
            turn.clicked.connect(lambda: self.screen.turn_adapter(node["id"]))
            self._row(turn)
            if R.adapter_kind_text(node):
                one = R.path_parts({"nodes": [node], "wires": []}, [(node["id"], None)], self.screen.labels)[0]
                self._note(f"Recorded as “{engine_chain.part_text(one)}”.")
            else:
                self._note("No kind chosen: recorded exactly as before.")
        else:
            self._note("Gender is drawing only here: it shows on the dots and warns about M to M or F to F. "
                       "It never enters the recorded text.")

    def show_for(self, item: PartItem | None):
        # Hidden while rebuilt: widgets added to a panel already on screen stay
        # hidden until shown, so the card is rebuilt off screen and shown whole.
        self.hide()
        self._clear()
        self.wire, self.enc, self.power = None, None, None
        self.node = item.node if item else None
        if item is None:
            self.hide()
            return
        node, p = item.node, item.node["part"]
        head = ElidedLabel("Edit part · " + self.screen.labels.get(p["type"], {}).get("label", p["type"]))
        head.setFont(T.ui_font(10, QFont.Weight.DemiBold))
        self.lay.addWidget(head)

        def text_edit(key, label):
            e = QLineEdit(str(p.get(key) or ""))
            e.textEdited.connect(lambda v, k=key: self._set(k, v))
            self._field(label, e)

        if p["type"] != "attenuator":
            text_edit("name", "Name")
        text_edit("detail", "Note" if p["type"] != R.SPLITTER else "Note (used when a port has no label)")
        if p["type"] in LENGTH_TYPES:
            text_edit("length", "Length")
        if p["type"] == "attenuator":
            s = QSpinBox()
            s.setRange(0, 120)
            s.setSuffix(" dB")
            try:
                s.setValue(int(float(p.get("db") or 0)))
            except ValueError:
                s.setValue(0)
            s.valueChanged.connect(lambda v: self._set("db", v or ""))
            self._field("Attenuation", s)
        if p["type"] == "sdr":
            c = QComboBox()
            rxs = list(self.screen.receivers)
            if p.get("rx") and p["rx"] not in rxs:
                rxs.append(p["rx"])
            for rx in rxs:
                sn = self.screen.serials.get(rx)
                c.addItem(f"{rx}" + (f"   SN {sn}" if sn else ""), rx)
            if p.get("rx") in rxs:
                c.setCurrentIndex(rxs.index(p["rx"]))
            else:
                c.setCurrentIndex(-1)
            c.currentIndexChanged.connect(lambda i, c=c: self._set("rx", c.itemData(i)))
            self._field("Receiver (matched by serial)", c)
        if p["type"] == R.SPLITTER:
            s = QSpinBox()
            s.setRange(2, 8)
            s.setValue(R.out_ports(node))
            s.valueChanged.connect(self._ports)
            self._field("Outputs", s)
            for i, lab in enumerate(node.get("ports") or []):
                e = QLineEdit(lab)
                e.textEdited.connect(lambda v, i=i: self._port_label(i, v))
                self._field(f"Output {i + 1} label (goes into the recorded text)", e)
        if p["type"] not in ("sdr", "pc"):
            self._field("Nominal gain or loss, dB (for the estimate)",
                        self.number_edit(node.get("gain_db"), self._gain,
                                         "e.g. +20 for an LNA, -3.5 for a splitter; blank if not known"))
        if p["type"] not in ("sdr", "pc", "usb", "bulkhead_usb", "isolator"):
            pw = QCheckBox("Needs power")
            pw.setChecked(R.needs_power(node))
            pw.setToolTip("A part that needs power gets a power input (the diamond) and a warning until a "
                          "power wire feeds it. Drawing only.")
            pw.toggled.connect(self._needs_power)
            self.lay.addWidget(pw)
        if p["type"] in R.SMA_TYPES:
            self._sma_rows(node)
        if p["type"] == "antenna":
            self._antenna_rows(node)
        self._shape_row(node, node["id"])
        self._textpos_row(node, node["id"])
        self._colour_row(node, node["id"])
        save = QPushButton("Save to my parts")
        save.setToolTip("Keep this part (kind, name, note, fields, size, figures) in My parts, to drop again "
                        "with the same name and note")
        save.clicked.connect(lambda: self.screen.save_to_library(node))
        self._row(self._lock_button(node, node["id"]), save)
        out = QPushButton("Take out of rig")
        out.setToolTip("Removes this part from the drawing. Nothing is saved until Save.")
        out.clicked.connect(self.screen.take_out_selected)
        self.lay.addWidget(out)
        self._fit()
        self.show()
        self.raise_()

    def _gain(self, v):
        if self.node is None:
            return
        if v is None:
            self.node.pop("gain_db", None)
        else:
            self.node["gain_db"] = v
        self.screen.changed(redraw=False, merge=("gain", self.node["id"]))

    def _needs_power(self, on: bool):
        if self.node is None:
            return
        nid = self.node["id"]
        if on == (self.node["part"]["type"] in R.NEEDS_POWER_BY_TYPE):
            self.node.pop("needs_power", None)
        else:
            self.node["needs_power"] = on
        if not on:
            R.disconnect_power(self.screen.rig, nid)
        self.screen.changed()
        self.screen.select(nid)

    def show_for_power(self, item: "PowerItem"):
        self.hide()
        self._clear()
        self.node, self.wire, self.enc = None, None, None
        self.power = item.box
        head = QLabel("Edit power source")
        head.setFont(T.ui_font(10, QFont.Weight.DemiBold))
        self.lay.addWidget(head)
        kind = QComboBox()
        for k, label in R.POWER_KINDS.items():
            kind.addItem(label, k)
        kind.setCurrentIndex(list(R.POWER_KINDS).index(item.box["kind"]))
        kind.currentIndexChanged.connect(lambda i: self._power_kind(kind.itemData(i)))
        self._field("Kind", kind)
        for key, label in (("name", "Name"), ("note", "Note")):
            e = QLineEdit(str(item.box.get(key) or ""))
            e.textEdited.connect(lambda v, k=key: self._power_set(k, v))
            self._field(label, e)
        names = {n["id"]: n["part"].get("name") or "a part" for n in self.screen.rig["nodes"]}
        feeds = [names.get(w["to"], "?") for w in R.power_wires(self.screen.rig) if w["from"] == item.box["id"]]
        hint = QLabel(("Feeds: " + ", ".join(feeds)) if feeds else
                      "Feeds nothing yet: drag from its dot to a part that needs power.")
        hint.setWordWrap(True)
        hint.setProperty("muted", True)
        hint.setFont(T.ui_font(8.5))
        self.lay.addWidget(hint)
        self._note("Drawing only: power sources and power wires never enter the recorded text.")
        self._shape_row(item.box, item.box["id"])
        self._textpos_row(item.box, item.box["id"])
        self._colour_row(item.box, item.box["id"])
        out = QPushButton("Take out of rig")
        out.setToolTip("Removes this power source and its power wires from the drawing. Nothing is saved until Save.")
        out.clicked.connect(self.screen.take_out_selected)
        self._row(self._lock_button(item.box, item.box["id"]), out)
        self._fit()
        self.show()
        self.raise_()

    def _power_set(self, key, value):
        if getattr(self, "power", None) is None:
            return
        self.power[key] = value
        self.screen.changed(redraw=False, merge=("power", self.power["id"], key))
        for it in self.screen.power_items.values():
            it.update()

    def _power_kind(self, kind):
        b = self.power
        if b.get("name") in (None, "", R.POWER_KINDS.get(b["kind"])):
            b["name"] = R.POWER_KINDS[kind]
        b["kind"] = kind
        pid = b["id"]
        self.screen.changed()
        self.screen.select(pid)

    def show_for_enclosure(self, item: "EnclosureItem"):
        self.hide()
        self._clear()
        self.wire, self.node, self.power = None, None, None
        self.enc = item.enc
        head = QLabel("Edit enclosure")
        head.setFont(T.ui_font(10, QFont.Weight.DemiBold))
        self.lay.addWidget(head)
        for key, label in (("name", "Name"), ("note", "Note")):
            e = QLineEdit(str(item.enc.get(key) or ""))
            e.textEdited.connect(lambda v, k=key: self._enc_set(k, v))
            self._field(label, e)
        hint = QLabel("Drawing only: an enclosure writes nothing into the recorded text. "
                      "Parts inside it move with it, and so do their groups; drag one out to take it "
                      "out. Right-click it to bring it in front of parts or send it back.")
        hint.setWordWrap(True)
        hint.setProperty("muted", True)
        hint.setFont(T.ui_font(8.5))
        self.lay.addWidget(hint)
        self._colour_row(item.enc, item.enc["id"])
        out = QPushButton("Take out of rig")
        out.setToolTip("Removes the box only; the parts in it stay. Nothing is saved until Save.")
        out.clicked.connect(self.screen.take_out_selected)
        self._row(self._lock_button(item.enc, item.enc["id"]), out)
        self._fit()
        self.show()
        self.raise_()

    def _enc_set(self, key, value):
        if self.enc is None:
            return
        self.enc[key] = value
        self.screen.changed(redraw=False, merge=("enc", self.enc["id"], key))
        for it in self.screen.enc_items:
            it.update()

    def show_for_wire(self, item: "WireItem"):
        """The card for a wire: the cables it carries, in order from its start."""
        self.hide()
        self._clear()
        self.node, self.wire, self.enc, self.power = None, item.wire, None, None
        nodes = {n["id"]: n for n in self.screen.rig["nodes"]}
        a = nodes[self.wire["from"]]["part"].get("name") or "?"
        b = nodes[self.wire["to"]]["part"].get("name") or "?"
        head = QLabel(f"{'Mated' if R.mated(self.wire) else 'Connection'} · {a} → {b}")
        head.setFont(T.ui_font(10, QFont.Weight.DemiBold))
        head.setWordWrap(True)
        self.lay.addWidget(head)
        if R.mated(self.wire):
            self._note("Screwed together directly, port to port, with no wire. In the recorded text this is the "
                       "same as a wire with no cable. Mated parts move as one piece.")
            if item.clash():
                warn = self._note("M to M or F to F: these won't screw together.")
                warn.setStyleSheet(f"color: {T.C['alert']};")
            sep = QPushButton("Separate")
            sep.setToolTip("Unscrew them: the mating is taken out and the second part steps away. Undo puts it back.")
            sep.clicked.connect(self.screen.take_out_selected)
            self._row(sep)
            self._fit()
            self.show()
            self.raise_()
            return
        cables = self.wire.get("cables") or []
        if not cables:
            note = QLabel("No cable: the two parts join directly (screwed together, or through a bulkhead).")
            note.setProperty("muted", True)
            note.setWordWrap(True)
            self.lay.addWidget(note)
        types = [t for t in R.CABLE_TYPES if t in self.screen.labels]
        for i, c in enumerate(cables):
            box = QFrame()
            box.setObjectName("CardBox")
            box.setStyleSheet(f"QFrame#CardBox {{ border: 1px solid {cable_colour(c.get('type')).name()};"
                              f" border-radius: 8px; }}")
            bl = QGridLayout(box)
            bl.setContentsMargins(10, 8, 10, 8)
            bl.setHorizontalSpacing(6)
            bl.setVerticalSpacing(4)
            kind = QComboBox()
            for t in types:
                kind.addItem(self.screen.labels[t]["label"], t)
            kind.setCurrentIndex(max(0, types.index(c["type"]) if c.get("type") in types else 0))
            kind.currentIndexChanged.connect(lambda k, i=i, kind=kind: self._cable_type(i, kind.itemData(k)))
            rm = QPushButton("Remove")
            rm.setToolTip("Take this cable off the wire")
            rm.clicked.connect(lambda _=False, i=i: self._cable_remove(i))
            bl.addWidget(kind, 0, 0)
            bl.addWidget(rm, 0, 1)

            def edit(key, hint, i=i, c=c):
                e = QLineEdit(str(c.get(key) or ""))
                e.setPlaceholderText(hint)
                e.setToolTip(hint)
                e.textEdited.connect(lambda v, i=i, k=key: self._cable_set(i, k, v))
                return e

            bl.addWidget(edit("name", "Name (goes into the recorded text)"), 1, 0)
            bl.addWidget(edit("length", "Length"), 1, 1)
            bl.addWidget(edit("detail", "Note (goes into the text in brackets)"), 2, 0, 1, 2)
            bl.addWidget(self.number_edit(c.get("gain_db"), lambda v, i=i: self._cable_gain(i, v),
                                          "Nominal loss, dB, e.g. -1.2 (drawing only: for the estimate)"), 3, 0, 1, 2)
            bl.setColumnStretch(0, 3)
            bl.setColumnStretch(1, 2)
            self.lay.addWidget(box)
        add = QHBoxLayout()
        for t in types:
            btn = QPushButton("+ " + {"coax": "Coax", "jumper": "Jumper", "usb": "USB"}.get(t, t))
            btn.setToolTip(f"Add a {self.screen.labels[t]['label']} to this connection, after any already on it")
            btn.clicked.connect(lambda _=False, t=t: self._cable_add(t))
            add.addWidget(btn)
        self.lay.addLayout(add)
        out = QPushButton("Take wire out")
        out.setToolTip("Removes this wire (and its cables) from the drawing. Nothing is saved until Save.")
        out.clicked.connect(self.screen.take_out_selected)
        self.lay.addWidget(out)
        self._fit()
        self.show()
        self.raise_()

    def _cable_set(self, i, key, value):
        c = self.wire["cables"][i]
        if value in (None, "") and key == "length":
            c.pop("length", None)
        else:
            c[key] = value
        self.screen.changed(redraw=False, merge=("cable", self.wire["from"], self.wire["port"], i, key))
        self.screen._refresh_wires()

    def _cable_gain(self, i, v):
        c = self.wire["cables"][i]
        if v is None:
            c.pop("gain_db", None)
        else:
            c["gain_db"] = v
        self.screen.changed(redraw=False, merge=("cable", self.wire["from"], self.wire["port"], i, "gain_db"))

    def _cable_type(self, i, type_):
        c = self.wire["cables"][i]
        old_label = self.screen.labels.get(c.get("type"), {}).get("label")
        if not c.get("name") or c.get("name") == old_label:
            c["name"] = self.screen.labels.get(type_, {}).get("label", type_)
        c["type"] = type_
        self._wire_changed()

    def _cable_remove(self, i):
        del self.wire["cables"][i]
        if not self.wire["cables"]:
            self.wire.pop("cables")
        self._wire_changed()

    def _cable_add(self, type_):
        self.wire.setdefault("cables", []).append(R.new_cable(type_, self.screen.labels))
        self._wire_changed()

    def _wire_changed(self):
        key = (self.wire["from"], self.wire["port"])
        self.screen.changed()
        self.screen.select_wire(*key)

    def _set(self, key, value):
        if self.node is None:
            return
        if value in (None, ""):
            self.node["part"].pop(key, None) if key in R.FIELDS else self.node["part"].__setitem__(key, "")
        else:
            self.node["part"][key] = value
        self.screen.changed(redraw=False, merge=("field", self.node["id"], key))

    def _port_label(self, i, v):
        self.node["ports"][i] = v
        self.screen.changed(redraw=False, merge=("port", self.node["id"], i))

    def _ports(self, n):
        ports = list(self.node.get("ports") or [])
        while len(ports) < n:
            ports.append(f"out {len(ports) + 1}")
        self.node["ports"] = ports[:n]
        self.screen.rig["wires"] = [w for w in self.screen.rig["wires"]
                                    if not (w["from"] == self.node["id"] and w["port"] >= n)]
        nid = self.node["id"]
        self.screen.changed()
        self.screen.select(nid)


# ---------------------------------------------------------------- the screen
class ChainScreen(QWidget):
    status = Signal(str)

    def __init__(self, service: Service):
        super().__init__()
        self.svc = service
        self.summary, self.layout_doc = {"chains": {}, "current": None}, R.empty_layout()
        self.labels = R.labels_from(None)
        self.receivers, self.serials, self.roles = [], {}, {}
        self.display: dict = {}
        self.rig_name: str | None = None
        self.rig = R.new_rig()
        self.static_ = False
        self.read_only = False         # an archived rig is shown read-only until it is restored
        self.version_view = None       # relay 012: {"run", "version", ...} while a run's rig is shown as it was
        self.renamed_from = None
        self._soft = False
        self.dirty = False
        self.snap_on = True
        self.colour_on = True
        self.show_notes = True
        self.wire_style = "curved"
        self.show_labels = True
        self.dash_usb = True
        self.grid = dict(GRID_DEFAULTS)
        self.animating = False
        self.drop_target = None
        self.anchor_map: dict = {}
        self.undo_stack: list = []
        self.redo_stack: list = []
        self._last = None
        self._saved = None
        self._merge = None
        self._load_prefs()
        self.wiring_from = None
        self.power_from = None         # a power source whose power wire is being dragged, or ("to", part id)
        self.temp_wire = None
        self.items_by_id: dict[str, PartItem] = {}
        self.wire_items: list[WireItem] = []
        self.enc_items: list = []
        self.enc_by_id: dict = {}
        self.power_items: dict = {}
        self.power_wire_items: list = []
        self.power_map: dict = {}      # part id -> its power input (x, y, side)
        self.mated_keys: set = set()   # connection points joined port to port (drawn as a joint, not a dot)
        self.sel_count = 0             # how many boxes are selected (kept here: painting asks for it often)
        self.card_wanted = None        # the box whose card was asked for by a click (relay 008); a press or drag asks nothing
        self.power_out: dict = {}      # power source id -> its output dot (x, y, side)
        self.group_marks: list = []
        self.guide_items: list = []
        self._drag = None              # (lead box, its place at press, {carried item: its place}, other boxes, held flags)
        self._group_click = None       # ("group" | "enter", item) between a press and its release
        self.clip: dict | None = None  # what Copy took (also on the system clipboard)
        self._paste_n = 0
        self.library: list = []
        self._build()

    # -- the window's own preferences
    def _load_prefs(self):
        q = prefs()
        style = str(q.value("grid/style", GRID_DEFAULTS["style"]))
        self.grid = {"style": style if style in ("dots", "lines", "none") else "dots",
                     "major": q.value("grid/major", GRID_DEFAULTS["major"], type=bool),
                     "dotted": q.value("grid/dotted", GRID_DEFAULTS["dotted"], type=bool)}
        self.snap_on = q.value("view/snap", True, type=bool)
        self.colour_on = q.value("view/colour", True, type=bool)
        self.show_notes = q.value("parts/notes", True, type=bool)
        style = str(q.value("wires/style", "curved"))
        self.wire_style = style if style in G.WIRE_STYLES else "curved"
        self.show_labels = q.value("wires/labels", True, type=bool)
        self.dash_usb = q.value("wires/dash_usb", True, type=bool)

    def _save_prefs(self):
        q = prefs()
        q.setValue("grid/style", self.grid["style"])
        q.setValue("grid/major", self.grid["major"])
        q.setValue("grid/dotted", self.grid["dotted"])
        q.setValue("view/snap", self.snap_on)
        q.setValue("view/colour", self.colour_on)
        q.setValue("parts/notes", self.show_notes)
        q.setValue("wires/style", self.wire_style)
        q.setValue("wires/labels", self.show_labels)
        q.setValue("wires/dash_usb", self.dash_usb)
        q.setValue("panels/across", self.split_across.saveState())
        q.setValue("panels/down", self.split_down.saveState())

    # -- layout of the screen
    def _tool(self, text: str, tip: str, slot, accent: bool = False, icon: str | None = None,
              kind: str = "Tool", label: bool = True) -> QToolButton:
        """A toolbar button (relay 007): a line icon, its label where there is room, one height for all.
        kind: "Tool" (plain), "Primary" (Save, Use for runs) or "Danger" (Clear grid)."""
        btn = QToolButton()
        btn.setObjectName(kind)
        btn.setText(text)
        if icon:
            btn.setIcon(tool_icon(icon, "background" if kind == "Primary" else ("alert_text" if kind == "Danger"
                                                                                else "text2")))
            btn.setIconSize(QSize(16, 16))
        btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon if label and text
                               else Qt.ToolButtonStyle.ToolButtonIconOnly)
        btn.setToolTip(tip)
        btn.clicked.connect(slot)
        btn.setFocusPolicy(Qt.FocusPolicy.TabFocus)       # the arrow keys stay with the grid
        btn.setFixedHeight(T.TOOL_H)
        return btn

    @staticmethod
    def _segment(*widgets, name: str = "") -> QFrame:
        """A group of toolbar controls on one rounded surface (rig, edit, arrange, view, run)."""
        seg = QFrame()
        seg.setObjectName("Segment")
        seg.setAccessibleName(name)
        lay = QHBoxLayout(seg)
        lay.setContentsMargins(T.SPACE // 2, T.SPACE // 2, T.SPACE // 2, T.SPACE // 2)
        lay.setSpacing(T.SPACE // 2)
        for w in widgets:
            lay.addWidget(w)
        return seg

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # The toolbar (relay 007, spread out in relay 011): the rig section and its actions with Save and
        # Use for runs on the first row; edit, arrange, picture and help, then the run switches and the
        # set-apart Clear grid on the second. The view menus (Grid, Parts, Wires, Zoom) sit on a small bar
        # at the grid's top edge, with the view they change. One height for every toolbar button.
        bar = QFrame()
        bar.setObjectName("Toolbar")
        rows = QVBoxLayout(bar)
        rows.setContentsMargins(T.SPACE * 2, T.SPACE + T.SPACE // 2, T.SPACE * 2, T.SPACE + T.SPACE // 2)
        rows.setSpacing(T.SPACE)
        b = QHBoxLayout()
        b.setSpacing(T.SPACE * 2)
        rows.addLayout(b)
        info = QHBoxLayout()
        info.setSpacing(T.SPACE * 2)
        rows.addLayout(info)
        top = Qt.AlignmentFlag.AlignTop
        lab = QLabel()
        lab.setPixmap(tool_icon("rig", "muted").pixmap(16, 16))
        lab.setToolTip("Rig: the drawing on the grid")
        self.pick = QComboBox()
        self.pick.setObjectName("RigPick")
        self.pick.setMinimumWidth(220)
        self.pick.setFixedHeight(T.TOOL_H - 6)
        self.pick.setToolTip("The rig shown on the grid. Each saved rig, and each chain the web page made, is "
                             "listed; archived rigs are listed apart, under Archived.")
        self.pick.activated.connect(self._picked)
        # The rig's state has a line of its own under the picker (relay 011): it wraps, never clips.
        self.state = QLabel()
        self.state.setObjectName("RigState")
        self.state.setProperty("muted", True)
        self.state.setFont(T.ui_font(8.5))
        self.state.setWordWrap(True)
        self.state.setTextFormat(Qt.TextFormat.PlainText)
        self.state.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Minimum)
        self.state.setToolTip("This rig's state: saved or not, when it last ran, static or not")
        self.pill = QLabel()
        self.pill.setObjectName("Pill")
        rig_seg = QFrame()
        rig_seg.setObjectName("Segment")
        rig_seg.setAccessibleName("rig")
        rv = QVBoxLayout(rig_seg)
        rv.setContentsMargins(T.SPACE // 2, T.SPACE // 2, T.SPACE, T.SPACE // 2 + 2)
        rv.setSpacing(T.SPACE // 2)
        rh = QHBoxLayout()
        rh.setSpacing(T.SPACE // 2)
        rh.addWidget(lab)
        rh.addWidget(self.pick, 1)
        rh.addWidget(self.pill)
        rv.addLayout(rh)
        rv.addWidget(self.state)
        rig_seg.setMinimumWidth(T.SPACE * 45)
        self.btn_new = self._tool("New rig", "Start a new rig: blank, or from a template", self.new_rig, icon="new")
        self.btn_rename = self._tool("Rename", "Give this rig a new name", self.rename_rig, icon="rename")
        self.btn_rig = self._tool("Rig", "History of this rig's saves; archive or restore it", lambda: None,
                                  icon="more")
        self.btn_rig.setText("Rig ▾")
        self.btn_rig.setMenu(self._rig_menu())
        self.btn_rig.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        b.addWidget(rig_seg, 0, top)
        b.addWidget(self._segment(self.btn_new, self.btn_rename, self.btn_rig, name="rig actions"), 0, top)
        b.addStretch(1)
        self.chk_static = QCheckBox("Static")
        self.chk_static.setToolTip("A static rig is never questioned before a run: it does not change.")
        self.chk_static.clicked.connect(self._static_clicked)
        self.btn_text = self._tool("Recorded text", "Show or hide the text each run records for this rig, one line "
                                   "per radio, with the nominal gain estimate", lambda: None, icon="text")
        self.btn_text.setCheckable(True)
        self.btn_text.toggled.connect(self._toggle_text)
        self.btn_out = self._tool("Output", "Show or hide the Output panel: technical data for what is selected, "
                                  "or for the whole rig", lambda: None, icon="output")
        self.btn_out.setCheckable(True)
        self.btn_save = self._tool("Save", "Write this rig to chain.json through the service", self.save,
                                   icon="save", kind="Primary")
        self.btn_use = self._tool("Use for runs", "", self.use_for_runs, icon="use", kind="Primary")
        b.addWidget(self.btn_save, 0, top)
        b.addWidget(self.btn_use, 0, top)
        root.addWidget(bar)

        self.btn_undo = self._tool("Undo", "Undo (Ctrl+Z)", self.undo, icon="undo", label=False)
        self.btn_redo = self._tool("Redo", "Redo (Ctrl+Y)", self.redo, icon="redo", label=False)
        info.addWidget(self._segment(self.btn_undo, self.btn_redo, name="edit"))
        self.btn_tidy = self._tool("Tidy", "Arrange the rig: source on the left, branches one under another. "
                                   "Locked boxes stay; parts make way for a group. Undo puts it back.", self.tidy,
                                   icon="tidy")
        self.menus = {}
        arrange = []
        view = []
        for key, text, tip, build, icon in (
                ("arrange", "Arrange", "Stacking, alignment, lock and groups of the selected boxes",
                 self._arrange_menu, "arrange"),
                ("grid", "Grid", "How the grid looks, and snapping", self._grid_menu, "grid"),
                ("parts", "Parts", "How parts look", self._parts_menu, "parts"),
                ("wires", "Wires", "How wires and their cables look", self._wires_menu, "wires"),
                ("zoom", "Zoom", "Zoom and fit", self._zoom_menu, "zoom")):
            btn = self._tool(text + " ▾", tip, lambda: None, icon=icon)
            btn.setMenu(build())
            btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
            (arrange if key == "arrange" else view).append(btn)
            self.menus[key] = btn
        info.addWidget(self._segment(self.btn_tidy, *arrange, name="arrange"))
        self.btn_picture = self._tool("Picture…", "Save the drawing as a PNG picture, to a place you pick",
                                      lambda: self.export_picture(), icon="picture")
        self.btn_help = self._tool("Help", "Keyboard shortcuts (F1)", self.show_help, icon="help", label=False)
        info.addWidget(self._segment(self.btn_picture, self.btn_help, name="picture and help"))
        info.addStretch(1)
        info.addWidget(self._segment(self.chk_static, self.btn_text, self.btn_out, name="run"))
        self.btn_clear = self._tool("Clear grid", "Take every part and wire off the grid. Undo brings them back; "
                                    "nothing is saved until Save.", self.clear_grid, icon="clear", kind="Danger")
        info.addWidget(self.btn_clear)
        # The view bar (relay 011): built here, placed on the grid's top edge below.
        self.view_bar = self._segment(*view, name="view")

        # The Checks strip: problems as the rig is built. Each one selects what it is about.
        self.checks_box = QFrame()
        self.checks_box.setObjectName("Checks")
        cb = QHBoxLayout(self.checks_box)
        cb.setContentsMargins(T.SPACE + T.SPACE // 2, T.SPACE // 2, T.SPACE, T.SPACE // 2)
        cb.setSpacing(T.SPACE // 2)
        self.checks_title = QLabel("Checks")
        self.checks_title.setFont(T.ui_font(8.5, QFont.Weight.DemiBold))
        cb.addWidget(self.checks_title)
        self.checks_row = QHBoxLayout()
        self.checks_row.setSpacing(T.SPACE // 2)
        cb.addLayout(self.checks_row, 1)
        cwrap = QHBoxLayout()
        cwrap.setContentsMargins(T.SPACE * 2, T.SPACE, T.SPACE * 2, 0)
        cwrap.addWidget(self.checks_box)
        root.addLayout(cwrap)
        self.check_list: list = []

        self.notice = QLabel()
        self.notice.setObjectName("Notice")
        self.notice.setWordWrap(True)
        self.notice.hide()
        nwrap = QHBoxLayout()
        nwrap.setContentsMargins(16, 10, 16, 0)
        nwrap.addWidget(self.notice)
        root.addLayout(nwrap)

        # Panels the owner can resize: parts | (grid over recorded text).
        self.split_across = QSplitter(Qt.Orientation.Horizontal)
        self.split_across.setHandleWidth(6)
        self.split_across.setChildrenCollapsible(False)
        side = QFrame()
        side.setObjectName("Side")
        side.setMinimumWidth(170)
        s = QVBoxLayout(side)
        s.setContentsMargins(T.SPACE * 2, T.SPACE * 2, T.SPACE * 1 + 4, T.SPACE * 2)
        s.setSpacing(T.SPACE)
        self.search = QLineEdit()
        self.search.setObjectName("PartSearch")
        self.search.setPlaceholderText("Search parts, cables and My parts")
        self.search.setClearButtonEnabled(True)
        self.search.setToolTip("Type to show only the parts, cables and My parts whose names contain it")
        self.search.textChanged.connect(self._filter_parts)
        s.addWidget(self.search)
        hint = QLabel("Drag onto the grid")
        hint.setProperty("muted", True)
        hint.setFont(T.ui_font(8.5))
        s.addWidget(hint)
        self.parts = PartsList(self.labels)
        self.parts.colour_on = self.colour_on
        self.parts.on_library_menu = self.library_menu
        self.parts.set_labels(self.labels)
        self.parts.setMinimumHeight(110)            # never squeezed away by the cables and the key
        s.addWidget(self.parts, 1)
        hint2 = QLabel("CABLES · drop onto a wire")
        hint2.setProperty("muted", True)
        hint2.setFont(T.ui_font(7.5, QFont.Weight.DemiBold))
        s.addWidget(hint2)
        self.cables = PartsList(self.labels, cables=True)
        self.cables.colour_on = self.colour_on
        self.cables.set_labels(self.labels)
        s.addWidget(self.cables)
        self.legend = QWidget()
        lg = QGridLayout(self.legend)
        lg.setContentsMargins(2, 8, 0, 0)
        lg.setHorizontalSpacing(6)
        lg.setVerticalSpacing(2)
        rows = ([(category_colour(cat), name) for cat, name in CATEGORY_NAMES]
                + [(cable_colour("coax"), "Coax and jumpers (on wires)"),
                   (T.color("power"), "Power (drawing only)"),
                   ("dash", "USB side of a radio"),
                   ("M", "SMA male port (a pin)"), ("F", "SMA female port (a socket)")])
        for i, (c, name) in enumerate(rows):
            mark = QLabel()
            if c == "dash":
                mark.setPixmap(self._dash_pixmap())
            elif c in ("M", "F"):
                mark.setPixmap(self._gender_pixmap(c))
            else:
                mark.setPixmap(dot_icon(c).pixmap(10, 10))
            text = QLabel(name)
            text.setProperty("muted", True)
            text.setFont(T.ui_font(8.5))
            text.setWordWrap(True)
            row, col = i // 2, (i % 2) * 2
            lg.addWidget(mark, row, col, Qt.AlignmentFlag.AlignTop)
            lg.addWidget(text, row, col + 1, Qt.AlignmentFlag.AlignTop)
        lg.setColumnStretch(1, 1)
        lg.setColumnStretch(3, 1)
        self.btn_key = QPushButton("Key ▾")
        self.btn_key.setObjectName("KeyToggle")
        self.btn_key.setCheckable(True)
        self.btn_key.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_key.setToolTip("Show or hide the key to the colours and marks")
        self.btn_key.setStyleSheet(f"QPushButton#KeyToggle {{ background: transparent; border: none; text-align: left;"
                                   f" padding: 2px 2px; min-height: 0; color: {T.C['muted']}; font-weight: 600; }}"
                                   f"QPushButton#KeyToggle:hover {{ color: {T.C['text']}; }}")
        self.btn_key.toggled.connect(self._show_key)
        s.addWidget(self.btn_key)
        # The key scrolls on a short screen rather than push the Parts list away (150 % scaling).
        self.legend_box = QScrollArea()
        self.legend_box.setWidgetResizable(True)
        self.legend_box.setFrameShape(QFrame.Shape.NoFrame)
        self.legend_box.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.legend_box.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
        self.legend_box.setWidget(self.legend)
        self.legend_box.setMinimumHeight(60)
        self.legend_box.setMaximumHeight(self.legend.sizeHint().height() + 4)
        s.addWidget(self.legend_box)
        self.split_across.addWidget(side)

        self.split_down = QSplitter(Qt.Orientation.Vertical)
        self.split_down.setHandleWidth(6)
        self.split_down.setChildrenCollapsible(False)
        holder = QWidget()
        hv = QVBoxLayout(holder)
        hv.setContentsMargins(12, T.SPACE * 2, 16, 6)
        hv.setSpacing(T.SPACE // 2)
        vb = QHBoxLayout()
        vb.setContentsMargins(0, 0, 0, 0)
        vb.addWidget(self.view_bar)
        vb.addStretch(1)
        hv.addLayout(vb)
        self.scene = ChainScene(self)
        self.scene.setSceneRect(-200, -400, 2600, 1100)
        self.view = ChainView(self, self.scene)
        self.view.setStyleSheet(f"border: 1px solid {T.C['lines']}; border-radius: {T.RADIUS}px;")
        hv.addWidget(self.view)
        self.split_down.addWidget(holder)
        self.card = EditCard(self, self.view)
        self.view.horizontalScrollBar().valueChanged.connect(self._place_card)
        self.view.verticalScrollBar().valueChanged.connect(self._place_card)
        # What to do next on an empty grid (relay 006: empty states say what to do).
        self.empty_hint = QLabel(self.view)
        self.empty_hint.setObjectName("EmptyHint")
        self.empty_hint.setWordWrap(True)
        self.empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_hint.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.empty_hint.setStyleSheet(f"QLabel#EmptyHint {{ color: {T.C['muted']}; background: {T.C['panel']};"
                                      f" border: 1px dashed {T.C['line_strong']}; border-radius: {T.RADIUS}px;"
                                      f" padding: {T.SPACE * 2}px {T.SPACE * 3}px; }}")
        self.empty_hint.setFixedWidth(420)
        self.empty_hint.hide()

        self.text_panel = QFrame()
        self.text_panel.setObjectName("TextPanel")
        self.text_panel.setMinimumHeight(70)
        tp = QVBoxLayout(self.text_panel)
        tp.setContentsMargins(16, 12, 16, 12)
        head = QHBoxLayout()
        t1 = QLabel("Recorded text")
        t1.setFont(T.ui_font(10, QFont.Weight.DemiBold))
        t2 = ElidedLabel("what each run writes into its record, one line per radio")
        t2.setProperty("muted", True)
        head.addWidget(t1)
        head.addWidget(t2, 1)
        tp.addLayout(head)
        self.text_grid = QGridLayout()
        self.text_grid.setHorizontalSpacing(14)
        self.text_grid.setVerticalSpacing(6)
        tp.addLayout(self.text_grid)
        tp.addStretch(1)
        self.text_panel.hide()
        self.split_down.addWidget(self.text_panel)
        self.split_down.setStretchFactor(0, 1)
        self.split_across.addWidget(self.split_down)
        self.output = OutputPanel(self)
        self.split_across.addWidget(self.output)
        self.split_across.setStretchFactor(1, 1)
        self.split_across.setSizes([220, 1000, 300])
        self.grid_status = GridStatus(self, self.view)
        q = prefs()
        for key, sp in (("panels/across", self.split_across), ("panels/down", self.split_down)):
            state = q.value(key)
            if state is not None:
                sp.restoreState(state)
        self.split_across.splitterMoved.connect(lambda *_: self._save_prefs())
        self.split_down.splitterMoved.connect(lambda *_: self._save_prefs())
        out_on = q.value("panels/output", True, type=bool)
        self.btn_out.setChecked(out_on)
        self.output.setVisible(out_on)
        self.btn_out.toggled.connect(self._toggle_output)
        root.addWidget(self.split_across, 1)
        self.scene.selectionChanged.connect(self.selection_changed)

        for keys, slot in ((QKeySequence.StandardKey.Undo, self.undo), (QKeySequence.StandardKey.Redo, self.redo),
                           (QKeySequence("Ctrl+Shift+Z"), self.redo),
                           # Shift+] arrives as } on many keyboards, so both spellings are listed
                           (QKeySequence("Ctrl+Shift+]"), lambda: self.arrange("front")),
                           (QKeySequence("Ctrl+}"), lambda: self.arrange("front")),
                           (QKeySequence("Ctrl+]"), lambda: self.arrange("forward")),
                           (QKeySequence("Ctrl+["), lambda: self.arrange("backward")),
                           (QKeySequence("Ctrl+Shift+["), lambda: self.arrange("back")),
                           (QKeySequence("Ctrl+{"), lambda: self.arrange("back")),
                           (QKeySequence("Ctrl+G"), self.group_selected),
                           (QKeySequence("Ctrl+Shift+G"), self.ungroup_selected),
                           (QKeySequence("Ctrl+Shift+L"), self.toggle_lock),
                           (QKeySequence("F1"), self.show_help)):
            sc = QShortcut(QKeySequence(keys), self)
            sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            sc.activated.connect(slot)
        self.key_on = prefs().value("parts/key", False, type=bool)   # folded at first: the Parts list keeps its room
        self.btn_key.setChecked(self.key_on)
        self._show_key(self.key_on)

    def _check(self, m: QMenu, text: str, on: bool, slot) -> QAction:
        act = QAction(text, m, checkable=True)
        act.setChecked(on)
        act.toggled.connect(slot)
        m.addAction(act)
        return act

    def _rig_menu(self) -> QMenu:
        m = Menu(self)
        m.aboutToShow.connect(lambda: self.fill_rig_menu(m))     # filled when opened: it follows the rig
        return m

    def fill_rig_menu(self, m: QMenu):
        m.clear()
        d = self.display.get(self.rig_name or "", {})
        if self.version_view:                     # a run's rig as it was: only the way back is offered
            a = m.addAction(tool_icon("history"), "Back to the rig as it is now")
            a.setToolTip("Leave this read-only view and open the rig's current drawing")
            a.triggered.connect(lambda: self.open_rig(self.rig_name) if self.rig_name in self.display else self.reload())
            return
        a = m.addAction(tool_icon("history"), "History…")
        a.setEnabled(bool(d.get("history")))
        a.setToolTip("Every Save keeps the version it replaces: look at them and restore one" if a.isEnabled()
                     else "No earlier versions yet: each Save keeps the one before it")
        a.triggered.connect(self.history_dialog)
        m.addSeparator()
        if self.read_only:
            a = m.addAction(tool_icon("archive"), "Restore rig")
            a.setToolTip("Bring it back to the rig list, editable and usable for runs")
            a.triggered.connect(self.restore_rig)
        else:
            a = m.addAction(tool_icon("archive"), "Archive rig…")
            why = self._why_not_archive()
            a.setEnabled(why is None)
            a.setToolTip(why or "Move it to the Archived section of the rig list. Nothing is removed.")
            a.triggered.connect(self.archive_rig)
        gone = sorted(n for n, x in self.display.items() if x.get("archived"))
        sub = Menu(m)
        sub.setTitle("Open an archived rig")
        for n in gone:
            a = sub.addAction(n)
            a.triggered.connect(lambda _=False, n=n: self._open_named(n))
        m.addMenu(sub).setEnabled(bool(gone))

    def _open_named(self, name: str):
        if self._ask_unsaved() and name in self.display:
            self.open_rig(name)

    def _why_not_archive(self) -> str | None:
        d = self.display.get(self.rig_name or "")
        if not d:
            return "Open a rig first"
        if not d.get("chains"):
            return "This rig has never been saved: there is nothing to archive"
        if self.dirty:
            return "Save it or undo the changes first"
        if self.rig_is_used(d):
            return "This rig is the one runs record now: make another rig current (Use for runs) first"
        return None

    def archive_rig(self):
        """Archive, never delete (the owner's decision, 2026-11-06): the rig moves to the Archived
        section; its chains stay in chain.json exactly as they are; Restore brings it back."""
        why = self._why_not_archive()
        if why:
            self.show_notice(f"Not archived: {why}.")
            return
        name = self.rig_name
        b = QMessageBox.question(self, "Archive rig",
                                 f"Archive “{name}”?\n\nIt moves to the Archived section of the rig list and opens "
                                 "read-only. Nothing is removed: its chains stay in chain.json exactly as they are, "
                                 "and Restore (Rig menu) brings it back.")
        if b != QMessageBox.StandardButton.Yes:
            return
        if self._set_archived(name, True):
            live = sorted((n for n, x in self.display.items() if not x.get("archived")), key=str.lower)
            if live:
                self.open_rig(live[0])
            self.status.emit(f"“{name}” archived")

    def restore_rig(self):
        name = self.rig_name
        if name and self._set_archived(name, False):
            self.open_rig(name)
            self.status.emit(f"“{name}” restored")

    def _set_archived(self, name: str, on: bool) -> bool:
        try:
            got = self.svc.chains()
            summ = got.get("chain") or {}
            chains = {n: {k: v for k, v in c.items() if k not in DERIVED} for n, c in (summ.get("chains") or {}).items()}
            doc = {"schema": 1, "current": summ.get("current"), "chains": chains}
            lay = R.check_layout(got.get("layout") or {})
            lay = R.set_archived(doc, lay, name, self.display[name]["rig"], on, self.labels)
            self.svc.save(doc, lay)
        except (ValueError, ServiceError) as exc:
            self.show_notice(f"Not {'archived' if on else 'restored'}: {exc}")
            return False
        self.reload(keep=name)
        return True

    def history_dialog(self):
        """Every Save kept the drawing it replaced. Look at one; restore it (a restore is a new Save,
        so the version it replaces is kept as well, and nothing is ever lost)."""
        d = self.display.get(self.rig_name or "", {})
        versions = list(reversed(d.get("history") or []))
        if not versions:
            return
        dlg = QDialog(self)
        dlg.setWindowTitle(f"History of “{self.rig_name}”")
        dlg.resize(720, 460)
        lay = QHBoxLayout(dlg)
        lst = QListWidget()
        lst.setObjectName("Versions")
        for k, v in enumerate(versions):
            n = len(v["rig"].get("nodes") or [])
            it = QListWidgetItem(f"{_when(v.get('saved'))}   ·   {n} part{'s' if n != 1 else ''}")
            it.setData(Qt.ItemDataRole.UserRole, k)
            lst.addItem(it)
        lst.setFixedWidth(260)
        lay.addWidget(lst)
        right = QVBoxLayout()
        info = QLabel()
        info.setWordWrap(True)
        info.setFont(T.mono_font(9))
        info.setAlignment(Qt.AlignmentFlag.AlignTop)
        right.addWidget(info, 1)
        note = QLabel("Restoring puts that version on the grid and saves it. The version it replaces is kept in this "
                      "history too, so nothing is lost. Recorded text comes only from the current version.")
        note.setWordWrap(True)
        note.setProperty("muted", True)
        right.addWidget(note)
        row = QHBoxLayout()
        close = QPushButton("Close")
        restore = QPushButton("Restore this version")
        restore.setToolTip("Put this version on the grid and save it as the current one")
        close.clicked.connect(dlg.reject)
        restore.clicked.connect(dlg.accept)
        row.addStretch(1)
        row.addWidget(close)
        row.addWidget(restore)
        right.addLayout(row)
        lay.addLayout(right, 1)

        def show(i):
            if i < 0:
                return
            v = versions[i]
            texts = R.rig_texts(self.rig_name, v["rig"], self.labels)
            info.setText(f"Saved {_when(v.get('saved'))}\n\n" + "\n".join(f"{n}:\n  {t}" for n, t in texts.items()))

        lst.currentRowChanged.connect(show)
        lst.setCurrentRow(0)
        self._history_dlg = dlg
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self.restore_version(versions[lst.currentRow()])

    def open_version(self, info: dict) -> bool:
        """Relay 012, "Open rig as it was": the drawing a run recorded (its rig reference), found by version in
        the rig's current drawing or its history, shown read-only and labelled with the run and the version.
        Nothing is saved; the Rig menu's "Back to the rig as it is now" leaves."""
        ref = (info or {}).get("ref") or {}
        run = (info or {}).get("run") or "this run"
        if self.dirty and not self._ask_unsaved():
            return False
        if not self.reload(keep=self.rig_name):
            return False
        found = engine_chain.find_drawing(self.layout_doc, str(ref.get("version") or ""), ref.get("rig"))
        if not found:
            self.show_notice(f"The drawing run {run} recorded (“{ref.get('rig')}”, version {ref.get('version')}) "
                             "is not kept in chain_layout.json any more. The run's chain text, in its record, still "
                             "says what was built.")
            return False
        name = found["rig"]
        self.rig_name = name
        self.rig = copy.deepcopy(found["drawing"])
        self.rig.setdefault("nodes", [])
        self.rig.setdefault("wires", [])
        self.read_only = True
        self.renamed_from = None
        self.dirty = False
        self.version_view = {"run": run, "version": ref.get("version"), "where": found["where"],
                             "saved": found.get("saved")}
        self._reset_history(saved=True)
        if name in self.display:
            self._show_in_pick(name)
        self.scene.clearSelection()
        self.redraw()
        when = ("the rig's current drawing" if found["where"] == "current"
                else f"a kept version, replaced {_when(found.get('saved'))}")
        renamed = f" (recorded as “{ref.get('rig')}”)" if ref.get("rig") and ref.get("rig") != name else ""
        self.show_notice(f"“{name}”{renamed} as it was for run {run}: version {ref.get('version')}, {when}. "
                         "Read-only: look, select and copy; nothing here changes the rig. Rig menu → Back to the rig "
                         "as it is now.")
        self.status.emit(f"opened “{name}” as it was for {run}")
        QTimer.singleShot(0, self._center)
        return True

    def restore_version(self, version: dict) -> bool:
        """Put a kept version back on the grid and save it (a new Save: what it replaces is kept too)."""
        self.rig = copy.deepcopy(version["rig"])
        self.rig.setdefault("nodes", [])
        self.rig.setdefault("wires", [])
        self.changed()
        ok = self.save()
        if ok:
            self.status.emit(f"version of {_when(version.get('saved'))} restored and saved")
        return ok

    def _grid_menu(self) -> QMenu:
        m = Menu(self)
        m.add_heading("Grid")
        grp = QActionGroup(m)
        self.grid_actions = {}
        for key, text in (("dots", "Dots"), ("lines", "Lines"), ("none", "No grid")):
            a = QAction(text, m, checkable=True)
            a.setChecked(self.grid["style"] == key)
            a.triggered.connect(lambda _=False, k=key: self._set_grid("style", k))
            grp.addAction(a)
            m.addAction(a)
            self.grid_actions[key] = a
        m.add_heading("Lines")
        self.act_major = self._check(m, "Major lines every 5 squares", self.grid["major"],
                                     lambda on: self._set_grid("major", on))
        self.act_dotted = self._check(m, "Dotted lines", self.grid["dotted"], lambda on: self._set_grid("dotted", on))
        m.add_heading("Placing")
        self.act_snap = self._check(m, "Snap to grid", self.snap_on, self._set_snap)
        return m

    def _parts_menu(self) -> QMenu:
        m = Menu(self)
        m.add_heading("Look")
        self.act_colour = self._check(m, "Colour parts by kind", self.colour_on, self._set_colour)
        self.act_notes = self._check(m, "Show notes on parts", self.show_notes, self._set_notes)
        m.add_heading("Size")
        reset = QAction("Standard size for the selected parts", m)
        reset.setToolTip("Puts resized parts back to the standard size")
        reset.triggered.connect(self.reset_sizes)
        m.addAction(reset)
        return m

    def _wires_menu(self) -> QMenu:
        m = Menu(self)
        m.add_heading("Shape")
        grp = QActionGroup(m)
        self.wire_actions = {}
        for key, text in (("curved", "Curved"), ("angled", "Right angles"), ("straight", "Straight")):
            a = QAction(text, m, checkable=True)
            a.setChecked(self.wire_style == key)
            a.triggered.connect(lambda _=False, k=key: self._set_wire_style(k))
            grp.addAction(a)
            m.addAction(a)
            self.wire_actions[key] = a
        m.add_heading("Cables")
        self.act_labels = self._check(m, "Name the cables on each wire", self.show_labels, self._set_labels_shown)
        self.act_dash = self._check(m, "Dash the USB side of a radio", self.dash_usb, self._set_dash)
        return m

    def _arrange_menu(self) -> QMenu:
        m = Menu(self)
        m.aboutToShow.connect(lambda: self.fill_arrange_menu(m))   # filled when opened: it follows the selection
        return m

    ARRANGE_ITEMS = (("front", "Bring to front", "Ctrl+Shift+]"), ("forward", "Bring forward", "Ctrl+]"),
                     ("backward", "Send backward", "Ctrl+["), ("back", "Send to back", "Ctrl+Shift+["))
    ALIGN_ITEMS = (("left", "Align left edges"), ("centre", "Align centres"), ("right", "Align right edges"),
                   ("top", "Align tops"), ("middle", "Align middles"), ("bottom", "Align bottoms"))

    def fill_arrange_menu(self, m: QMenu):
        """The Arrange actions, for the toolbar menu and the right-click menu alike. The key
        is shown beside each; the keys themselves are window shortcuts (see _build)."""
        m.clear()
        self._add_arrange(m)

    def _add_arrange(self, m: QMenu, align_sub: bool = False):
        ids = self.selected_boxes()
        heading = m.add_heading if isinstance(m, Menu) else m.addSection    # any menu will do
        heading("Stacking")
        for how, text, keys in self.ARRANGE_ITEMS:
            a = QAction(f"{text}\t{keys}", m)
            a.setEnabled(bool(ids))
            a.setToolTip("" if ids else "Select a box first")
            a.triggered.connect(lambda _=False, h=how: self.arrange(h))
            m.addAction(a)
        if align_sub:
            target = Menu(m)
            target.setTitle("Align and distribute")
            m.addMenu(target).setEnabled(len(ids) >= 2)
        else:
            heading("Align")
            target = m
        for how, text in self.ALIGN_ITEMS:
            a = QAction(text, target)
            a.setEnabled(len(ids) >= 2)
            a.setToolTip("" if len(ids) >= 2 else "Select two boxes or more to line them up")
            a.triggered.connect(lambda _=False, h=how: self.align_selected(h))
            target.addAction(a)
        if align_sub:
            target.addSeparator()
        for axis, text in (("across", "Distribute evenly across"), ("down", "Distribute evenly down")):
            a = QAction(text, target)
            a.setEnabled(len(ids) >= 3)
            a.setToolTip("" if len(ids) >= 3 else "Select three boxes or more to spread them evenly")
            a.triggered.connect(lambda _=False, ax=axis: self.distribute_selected(ax))
            target.addAction(a)
        heading("Lock and groups")
        locked = bool(ids) and all(R.is_locked(R.box(self.rig, i)) for i in ids)
        a = QAction(("Unlock" if locked else "Lock") + "\tCtrl+Shift+L", m)
        a.setEnabled(bool(ids))
        a.setToolTip("Locked boxes cannot be moved by anything; they can still be edited, renamed or taken out"
                     if ids else "Select a box first")
        a.triggered.connect(self.toggle_lock)
        m.addAction(a)
        a = QAction("Group\tCtrl+G", m)
        a.setEnabled(self.can_group(ids))
        a.setToolTip("" if self.can_group(ids) else "Select two boxes or more that are not already one group")
        a.triggered.connect(self.group_selected)
        m.addAction(a)
        a = QAction("Ungroup\tCtrl+Shift+G", m)
        a.setEnabled(any(R.group_of(self.rig, i) for i in ids))
        a.setToolTip("" if a.isEnabled() else "Nothing selected is in a group")
        a.triggered.connect(self.ungroup_selected)
        m.addAction(a)

    def fill_box_menu(self, m: QMenu, item):
        """The right-click menu on a part, an enclosure or a power source: format and arrange only
        (relay 008) - text position, shape, colour, stacking, align, lock, groups, the clipboard. What
        the box is (name, note, fields, kind) is edited on its card, which a left click opens."""
        ids = self.selected_boxes()
        boxes = [b for b in (R.box(self.rig, i) for i in ids) if b is not None]
        if not isinstance(item, EnclosureItem):
            heading = m.add_heading
            heading("Format")
            cur = R.text_pos(boxes[0]) if len(boxes) == 1 else None
            pick = PosPicker(cur or "", m)
            pick.setContentsMargins(T.SPACE * 4 + 2, 2, T.SPACE * 2, 4)
            wa = QWidgetAction(m)
            holder = QWidget()
            holder.setObjectName("MenuRow")
            holder.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            holder.setStyleSheet("QWidget#MenuRow, QWidget#MenuRow QLabel { background: transparent; border: none; }")
            hl = QHBoxLayout(holder)
            hl.setContentsMargins(T.SPACE * 4 + 2, 2, T.SPACE * 2, 4)
            lab = QLabel("Text position")
            lab.setFont(T.ui_font(9))
            hl.addWidget(lab)
            hl.addStretch(1)
            hl.addWidget(pick)
            wa.setDefaultWidget(holder)
            pick.chosen.connect(lambda code: (m.close(), self.set_text_pos(ids, code)))
            m.addAction(wa)
            sub = Menu(m)
            sub.setTitle("Shape")
            shape_cur = boxes[0].get("shape", "") if len(boxes) == 1 else None
            adapters = all(R.kind_of(self.rig, i) == "part" and R.box(self.rig, i)["part"]["type"] == "adapter"
                           for i in ids)
            for key, label in [("", "Default (rectangle)")] + list(R.SHAPES.items()):
                if key == "pipe" and not adapters:
                    continue
                a = sub.addAction(label)
                a.setCheckable(True)
                a.setChecked(shape_cur == key)
                a.triggered.connect(lambda _=False, k=key: self.set_shape(ids, k or None))
            m.addMenu(sub)
        else:
            m.add_heading("Format")
        box_colour_menu(m, self, ids)
        self._add_arrange(m, align_sub=True)
        m.add_heading("Clipboard")
        for text, slot, ok, why in (("Cut\tCtrl+X", self.cut, True, ""), ("Copy\tCtrl+C", self.copy, True, ""),
                                    ("Paste\tCtrl+V", lambda: self.paste(), self._clip_fragment() is not None,
                                     "Nothing copied yet"),
                                    ("Duplicate\tCtrl+D", self.duplicate, True, "")):
            a = m.addAction(text)
            a.setEnabled(ok)
            if not ok:
                a.setToolTip(why)
            a.triggered.connect(slot)
        if isinstance(item, PartItem) and len(ids) == 1:
            node = item.node
            mates = [w["to"] if w["from"] == node["id"] else w["from"] for w in self.rig["wires"]
                     if R.mated(w) and node["id"] in (w["from"], w["to"])]
            for other in mates:
                a = m.addAction(f"Separate from {R._pname(R.box(self.rig, other))}")
                a.triggered.connect(lambda _=False, o=other: self.separate_from(node["id"], o))
        m.addSeparator()
        a = m.addAction("Take out of rig\tDelete")
        a.triggered.connect(self.take_out_selected)

    def _sma_menu(self, m: QMenu, node: dict):
        """Adapter shape, gender and turn, the same choices as the part's card."""
        nid = node["id"]
        s = R.sma(node)
        if node["part"]["type"] == "adapter":
            sub = Menu(m)
            sub.setTitle("Adapter shape")
            for key, label in [("", "Not chosen")] + [(k, {"straight": "Straight (barrel)", "90": "90\u00b0 right angle",
                                                               "T": "T (three ports)", "180": "180\u00b0 (U)"}[k])
                                                        for k in R.ADAPTER_SHAPES]:
                a = sub.addAction(label)
                a.setCheckable(True)
                a.setChecked(s.get("shape", "") == key)
                a.triggered.connect(lambda _=False, k=key: self.set_sma(nid, shape=k))
            m.addMenu(sub)
        sub = Menu(m)
        sub.setTitle("Gender")
        for key, label in [("", "Not chosen")] + list(R.GENDERS.items()):
            a = sub.addAction(label)
            a.setCheckable(True)
            a.setChecked(s.get("gender", "") == key)
            a.triggered.connect(lambda _=False, k=key: self.set_sma(nid, gender=k))
        m.addMenu(sub)
        if node["part"]["type"] == "adapter":
            a = m.addAction("Turn 90\u00b0")
            a.setEnabled(bool(s.get("shape")))
            a.setToolTip("" if s.get("shape") else "Choose a shape first")
            a.triggered.connect(lambda: self.turn_adapter(nid))

    def fill_wire_menu(self, m: QMenu, item):
        """The right-click menu on a wire, a mating or a power wire."""
        if isinstance(item, PowerWireItem):
            a = m.addAction("Take power wire out\tDelete")
        elif R.mated(item.wire):
            a = m.addAction("Separate\tDelete")
            a.setToolTip("Unscrew the two parts: the second one steps away")
        else:
            a = m.addAction("Take wire out\tDelete")
        a.triggered.connect(self.take_out_selected)
        if isinstance(item, WireItem) and not R.mated(item.wire):
            sub = Menu(m)
            sub.setTitle("Add a cable")
            for t in [t for t in R.CABLE_TYPES if t in self.labels]:
                a = sub.addAction(dot_icon(cable_colour(t)), self.labels[t]["label"])
                a.triggered.connect(lambda _=False, t=t, w=item.wire: self._add_cable_to(w, t))
            m.addMenu(sub)

    def _add_cable_to(self, wire: dict, type_: str):
        key = (wire["from"], wire["port"])
        wire.setdefault("cables", []).append(R.new_cable(type_, self.labels))
        self.changed()
        self.select_wire(*key)

    def fill_grid_menu(self, m: QMenu, pt: QPointF):
        """The right-click menu on empty grid."""
        a = m.addAction("Paste here\tCtrl+V")
        a.setEnabled(self._clip_fragment() is not None)
        a.setToolTip("" if a.isEnabled() else "Nothing copied yet")
        a.triggered.connect(lambda: self.paste(at=pt))
        a = m.addAction("Select all\tCtrl+A")
        a.triggered.connect(self.select_all)
        m.addSeparator()
        a = m.addAction("Zoom to fit\tCtrl+0")
        a.triggered.connect(self.zoom_fit)
        a = m.addAction("Keyboard shortcuts\tF1")
        a.triggered.connect(self.show_help)

    def _zoom_menu(self) -> QMenu:
        m = Menu(self)
        for text, slot, keys in (("Zoom in", lambda: self.zoom_by(1.25), "Ctrl++"),
                                 ("Zoom out", lambda: self.zoom_by(0.8), "Ctrl+-"),
                                 ("Zoom to fit", self.zoom_fit, "Ctrl+0"),
                                 ("Zoom to the selection", self.zoom_selection, "Ctrl+2"),
                                 ("Actual size", self.zoom_reset, "Ctrl+1")):
            a = QAction(text, m)
            a.setShortcut(QKeySequence(keys))
            a.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            a.triggered.connect(slot)
            m.addAction(a)
            self.addAction(a)
        return m

    def _set_notes(self, on: bool):
        self.show_notes = on
        self._save_prefs()
        for it in self.items_by_id.values():
            it.update()

    def _set_wire_style(self, style: str):
        self.wire_style = style
        self._save_prefs()
        self._refresh_wires()

    def _set_labels_shown(self, on: bool):
        self.show_labels = on
        self._save_prefs()
        self._refresh_wires()

    def _set_dash(self, on: bool):
        self.dash_usb = on
        self._save_prefs()
        self._refresh_wires()

    def reset_sizes(self):
        sel = [i for i in self.scene.selectedItems() if isinstance(i, PartItem)
               and ("w" in i.node or "h" in i.node)]
        if not sel:
            self.status.emit("select resized parts first")
            return
        for it in sel:
            it.node.pop("w", None)
            it.node.pop("h", None)
        self.changed()

    ZOOM_MIN, ZOOM_MAX = 0.2, 3.0

    def zoom_by(self, f: float):
        z = self.view.transform().m11() * f
        if self.ZOOM_MIN <= z <= self.ZOOM_MAX:
            self.view.scale(f, f)
        if hasattr(self, "grid_status"):
            self.grid_status.refresh()

    def zoom_selection(self):
        items = [i for i in self.scene.selectedItems() if isinstance(i, (PartItem, EnclosureItem, PowerItem))]
        if not items:
            self.status.emit("select something to zoom to")
            return
        r = QRectF()
        for i in items:
            w, h = i.wh()
            r = r.united(i.mapRectToScene(QRectF(0, 0, w, h)))
        self.view.fitInView(r.adjusted(-60, -60, 60, 60), Qt.AspectRatioMode.KeepAspectRatio)
        z = self.view.transform().m11()
        if z > 2:
            self.view.scale(2 / z, 2 / z)

    def _set_grid(self, key, value):
        self.grid[key] = value
        self._save_prefs()
        self.scene.invalidate(self.scene.sceneRect(), QGraphicsScene.SceneLayer.BackgroundLayer)

    def _set_snap(self, on: bool):
        self.snap_on = on
        self._save_prefs()

    def _set_colour(self, on: bool):
        """Colour coding on or off. Every colour is worked out when it is painted (node_look,
        palette_colour), so this only repaints: nothing can be left in an old colour (relay 006)."""
        self.colour_on = on
        for lst in (self.parts, self.cables):
            lst.set_colour_on(on)
        self._save_prefs()
        self._fill_legend()
        self.redraw()

    def _show_key(self, on: bool):
        self.key_on = on
        self.btn_key.setText("Key ▾" if on else "Key ▸")
        prefs().setValue("parts/key", on)
        self._fill_legend()

    def _fill_legend(self):
        on = getattr(self, "key_on", True)
        self.btn_key.setVisible(self.colour_on)
        self.legend_box.setVisible(self.colour_on and on)

    def _gender_pixmap(self, g: str) -> QPixmap:
        """The legend's mark for a male or a female SMA port, drawn as on the grid."""
        dpr = self.devicePixelRatioF() or 1.0
        pm = QPixmap(int(14 * dpr), int(14 * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.GlobalColor.transparent)
        qp = QPainter(pm)
        qp.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = QPointF(7, 7)
        if g == "F":
            qp.setPen(QPen(T.color("wire"), 2.0))
            qp.setBrush(Qt.BrushStyle.NoBrush)
            qp.drawEllipse(c, 4.2, 4.2)
        else:
            qp.setPen(Qt.PenStyle.NoPen)
            qp.setBrush(T.color("wire"))
            qp.drawEllipse(c, 5.5, 5.5)
            qp.setBrush(T.color("background"))
            qp.drawEllipse(c, 1.6, 1.6)
        qp.end()
        return pm

    @staticmethod
    def _dash_pixmap() -> QPixmap:
        pm = QPixmap(14, 10)
        pm.fill(Qt.GlobalColor.transparent)
        qp = QPainter(pm)
        pen = QPen(T.color("wire"), 2)
        pen.setDashPattern([2, 1.5])
        qp.setPen(pen)
        qp.drawLine(0, 5, 14, 5)
        qp.end()
        return pm

    def zoom_fit(self):
        if not self.items_by_id and not self.enc_items and not self.power_items:
            return
        self.view.fitInView(self.scene.itemsBoundingRect().adjusted(-40, -40, 40, 40),
                            Qt.AspectRatioMode.KeepAspectRatio)
        if self.view.transform().m11() > 1.5:
            self.zoom_reset()

    def zoom_reset(self):
        self.view.resetTransform()
        self._center()
        if hasattr(self, "grid_status"):
            self.grid_status.refresh()

    def hideEvent(self, ev):
        self._save_prefs()
        super().hideEvent(ev)

    # -- data from the service
    def reload(self, keep: str | None = None):
        try:
            got = self.svc.chains()
        except ServiceError as exc:
            self.show_notice(f"Cannot read the rigs: {exc}")
            return False
        summ = got.get("chain") or {}
        self.summary = summ
        self.labels = R.labels_from(summ.get("parts"))
        self.receivers = list(summ.get("receivers") or [])
        self.serials = got.get("serials") or {}
        self.roles = got.get("roles") or {}
        try:
            self.layout_doc = R.check_layout(got.get("layout") or {})
        except ValueError as exc:
            self.layout_doc = R.empty_layout()
            self.show_notice(f"The saved drawings could not be read ({exc}); every chain is shown as a straight row.")
        self.display = R.rigs_for_display(self.chain_doc(), self.layout_doc, self.labels)
        self.library = copy.deepcopy(R.library(self.layout_doc))
        self.parts.library = [e for e in self.library if not e.get("removed")]
        self.parts.set_labels(self.labels)
        self.cables.set_labels(self.labels)
        self._fill_pick()
        names = sorted((n for n in self.display if not self.display[n].get("archived")), key=str.lower)
        want = keep if keep in self.display else self.rig_of_chain(summ.get("current"))
        if want is None and names:
            want = names[0]
        if want is not None:
            self.open_rig(want)
        else:
            self.rig_name, self.rig = None, R.new_rig()
            self.redraw()
        self.status.emit(f"service {self.svc.base}: {len(summ.get('chains') or {})} chains read")
        return True

    def chain_doc(self) -> dict:
        """chain.json as the service holds it, without the summary's derived keys."""
        chains = {}
        for n, c in (self.summary.get("chains") or {}).items():
            chains[n] = {k: v for k, v in c.items() if k not in DERIVED}
        return {"schema": 1, "current": self.summary.get("current"), "chains": chains}

    def rig_of_chain(self, chain_name: str | None) -> str | None:
        for name, d in self.display.items():
            if chain_name in d["chains"]:
                return name
        return None

    def _fill_pick(self):
        """The rig picker: every rig, then the archived ones apart, under an Archived heading."""
        live = sorted((n for n in self.display if not self.display[n].get("archived")), key=str.lower)
        gone = sorted((n for n in self.display if self.display[n].get("archived")), key=str.lower)
        self.pick.blockSignals(True)
        self.pick.clear()
        for n in live:
            self.pick.addItem(n, n)
        if gone:
            self.pick.insertSeparator(self.pick.count())
            self.pick.addItem("ARCHIVED", None)
            head = self.pick.model().item(self.pick.count() - 1)
            head.setEnabled(False)
            for n in gone:
                self.pick.addItem(tool_icon("archive", "muted"), f"{n}  (archived)", n)
        self.pick.blockSignals(False)

    def _show_in_pick(self, name: str | None):
        i = self.pick.findData(name) if name is not None else -1
        if i < 0 and name is not None:
            self.pick.addItem(name, name)
            i = self.pick.count() - 1
        self.pick.setCurrentIndex(i)

    def open_rig(self, name: str):
        d = self.display[name]
        self.version_view = None
        self.rig_name = name
        self.rig = copy.deepcopy(d["rig"])
        self.static_ = R.rig_state(d["chains"])["static"]
        self.read_only = bool(d.get("archived"))
        self.renamed_from = None
        self.dirty = False
        self._reset_history(saved=True)
        self._show_in_pick(name)
        self.scene.clearSelection()          # part ids repeat between rigs
        self.redraw()
        if d["stale_drawing"]:
            self.show_notice("This chain was changed in the old web page after it was drawn here, so it is shown "
                             "as a straight row, exactly as chain.json holds it now. Saving here draws it anew.")
        elif self.read_only:
            self.show_notice("This rig is archived, so it is read-only: look, select and copy, but nothing changes. "
                             "Restore it from the Rig menu to edit it or use it for runs.")
        else:
            self.show_notice(None)
        QTimer.singleShot(0, self._center)

    def _center(self):
        """Show the whole rig if it fits; else start from its left end."""
        if not self.items_by_id:
            return
        box = self.scene.itemsBoundingRect().adjusted(-40, -40, 40, 40)
        vw = self.view.viewport().width() / max(self.view.transform().m11(), 1e-6)
        if box.width() <= vw:
            self.view.centerOn(box.center())
        else:
            self.view.centerOn(box.left() + vw / 2, box.center().y())

    # -- helpers the items call
    def role_of(self, part: dict) -> str | None:
        rx = part.get("rx")
        for role, who in self.roles.items():
            if who == rx:
                return "recorder" if role.startswith("record") else "survey"
        return None

    def serial_of(self, part: dict) -> str | None:
        return self.serials.get(part.get("rx") or "")

    def kind_label(self, node: dict) -> str:
        p = node["part"]
        label = self.labels.get(p["type"], {}).get("label", p["type"])
        if p["type"] == "sdr":
            role = self.role_of(p)
            return "Radio" + (f" · {role}" if role else "") + (f" · {p['rx']}" if p.get("rx") else "")
        if p["type"] == R.SPLITTER:
            return f"Splitter · {R.out_ports(node)} outputs"
        return label

    # -- drawing
    def redraw(self):
        sel = [i.node["id"] for i in self.scene.selectedItems() if isinstance(i, PartItem)]
        sel_w = [(i.wire["from"], i.wire["port"]) for i in self.scene.selectedItems() if isinstance(i, WireItem)]
        sel_e = {i.enc["id"] for i in self.scene.selectedItems() if isinstance(i, EnclosureItem)}
        sel_p = {i.box["id"] for i in self.scene.selectedItems() if isinstance(i, PowerItem)}
        self.card.show_for(None)
        self.group_marks, self.guide_items = [], []
        self._drag, self._group_click = None, None
        self.scene.clear()
        self.temp_wire = None
        self.items_by_id, self.wire_items = {}, []
        self.power_items, self.power_wire_items = {}, []
        self.drop_target = None
        self.anchor_map = G.anchors(self.rig)
        self.power_map, self.power_out = G.power_points(self.rig)
        self.mated_keys = self._mated_keys()
        self.enc_items, self.enc_by_id = [], {}
        zi = {b: 2 * k for k, b in enumerate(R.stack(self.rig))}      # boxes on even z, wires between
        held = {}                                                       # part id -> enclosures holding it
        for e in R.enclosures(self.rig):
            ei = EnclosureItem(self, e)
            ei.setZValue(zi[e["id"]])
            self.scene.addItem(ei)
            self.enc_items.append(ei)
            self.enc_by_id[e["id"]] = ei
            for nid in G.inside(self.rig, e):
                held.setdefault(nid, []).append(e["id"])
        for e in R.enclosures(self.rig):
            if e["id"] in sel_e:
                self.enc_by_id[e["id"]].setSelected(True)
        for n in self.rig["nodes"]:
            it = PartItem(self, n)
            it.setZValue(zi[n["id"]])
            it.moved.connect(self._refresh_wires)
            self.scene.addItem(it)
            self.items_by_id[n["id"]] = it
        colours = self._wire_colours() if self.colour_on else {}
        usb_side = self._usb_side()
        for w in self.rig["wires"]:
            wi = WireItem(self, w)
            wi.color = colours.get((w["from"], w["port"], w["to"]), T.color("wire"))
            wi.digital = w["from"] in usb_side
            # just under the lower of its two parts, but always above an enclosure it runs into or out of
            ends = (w["from"], w["to"])
            wi.setZValue(max([min(zi[w["from"]], zi[w["to"]]) - 1]
                             + [zi[e] + 1 for nid in ends for e in held.get(nid, [])]))
            if R.mated(w):                  # a joint sits over both parts, so it can be clicked
                wi.setZValue(max(zi[w["from"]], zi[w["to"]]) + 1)
            wi.refresh()
            self.scene.addItem(wi)
            self.wire_items.append(wi)
        for b in R.power_boxes(self.rig):
            pi = PowerItem(self, b)
            pi.setZValue(zi[b["id"]])
            self.scene.addItem(pi)
            self.power_items[b["id"]] = pi
        for w in R.power_wires(self.rig):
            pw = PowerWireItem(self, w)
            pw.setZValue(max([min(zi[w["from"]], zi[w["to"]]) - 1] + [zi[e] + 1 for e in held.get(w["to"], [])]))
            self.scene.addItem(pw)
            self.power_wire_items.append(pw)
        if self.read_only:
            for it in self.all_box_items():
                it.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
        for nid in sel:
            if nid in self.items_by_id:
                self.items_by_id[nid].setSelected(True)
        for key in sel_w:
            wi = self.wire_item(*key)
            if wi is not None:
                wi.setSelected(True)
        for pid in sel_p:
            if pid in self.power_items:
                self.power_items[pid].setSelected(True)
        self._grow_scene()
        self.refresh_side()
        if getattr(self, "_pending_settle", None):
            self._settle()

    def _usb_side(self) -> set:
        """Parts from a radio onward: the wires leaving them carry USB, not RF."""
        out = {n["id"] for n in self.rig["nodes"] if n["part"]["type"] == "sdr"}
        grew = True
        while grew:
            grew = False
            for w in self.rig["wires"]:
                if w["from"] in out and w["to"] not in out:
                    out.add(w["to"])
                    grew = True
        return out

    def _grow_scene(self):
        base = QRectF(-200, -400, 2600, 1100)
        if self.items_by_id or self.enc_items or self.power_items:
            base = base.united(self.scene.itemsBoundingRect().adjusted(-600, -400, 600, 400))
        self.scene.setSceneRect(base)

    def _wire_colours(self) -> dict:
        """Past a splitter, a branch takes its radio's colour, as on the design."""
        out = {}
        for path in R.paths(self.rig):
            ids = [nid for nid, _ in path]
            radio = next((self.items_by_id[i].node["part"] for i in ids
                          if self.items_by_id[i].node["part"]["type"] == "sdr"), None)
            split = max((k for k, i in enumerate(ids) if self.items_by_id[i].node["part"]["type"] == R.SPLITTER),
                        default=None)
            if radio is None or split is None:
                continue
            c = T.color("recorder") if self.role_of(radio) == "recorder" else T.color("survey")
            for k in range(split, len(path) - 1):
                if self.items_by_id[ids[k + 1]].node["part"]["type"] == "sdr":
                    out[(path[k][0], path[k][1], ids[k + 1])] = c
                    break
                out[(path[k][0], path[k][1], ids[k + 1])] = c
        return out

    def _mated_keys(self) -> set:
        out = set()
        for w in self.rig["wires"]:
            if R.mated(w):
                out |= {(w["from"], "out", w["port"]), (w["to"], "in", 0)}
        return out

    def _refresh_wires(self):
        self.anchor_map = G.anchors(self.rig)
        self.power_map, self.power_out = G.power_points(self.rig)
        self.mated_keys = self._mated_keys()
        for w in self.wire_items:
            w.refresh()
        for w in self.power_wire_items:
            w.refresh()
        for it in self.items_by_id.values():
            it.update()
        for it in self.power_items.values():
            it.update()
        self._place_card()
        self._mark_groups()

    def selection_changed(self):
        try:
            items = self.scene.selectedItems()
        except RuntimeError:
            return
        parts = [i for i in items if isinstance(i, PartItem)]
        wires = [i for i in items if isinstance(i, WireItem)]
        boxes = [i for i in items if isinstance(i, EnclosureItem)]
        powers = [i for i in items if isinstance(i, PowerItem)]
        was, self.sel_count = self.sel_count, len(parts) + len(boxes) + len(powers)
        if was != self.sel_count:                  # the resize handles show for a single box only
            for it in parts + powers:
                it.update()
        # Relay 008: a box's card opens only when it was asked for - a left click on the box (or an edit
        # made on its card) - never because a press, a drag, a rectangle or a right-click selected it.
        one = (powers + boxes + parts)[0] if len(powers + boxes + parts) == 1 and not wires else None
        wanted = one is not None and self.box_id(one) == self.card_wanted
        if one is None or not wanted:
            if self.card.wire is None or not wires:
                self.card.show_for(None)
        if self.read_only and not wires:
            self.card.show_for(None)                   # an archived rig: look in Output, edit nothing
        elif not wanted:
            pass
        elif isinstance(one, PowerItem):
            if self.card.power is not one.box or not self.card.isVisible():
                self.card.show_for_power(one)
        elif isinstance(one, EnclosureItem):
            if self.card.enc is not one.enc or not self.card.isVisible():
                self.card.show_for_enclosure(one)
        elif isinstance(one, PartItem):
            if self.card.node is not one.node or not self.card.isVisible():
                self.card.show_for(one)
        if len(wires) == 1 and not parts and not boxes and not powers:
            if self.card.wire is not wires[0].wire or not self.card.isVisible():
                self.card.show_for_wire(wires[0])
        elif wires:
            self.card.show_for(None)
        self._place_card()
        self._mark_groups()
        self.fill_output()
        if hasattr(self, "grid_status"):
            self.grid_status.refresh()

    def wire_item(self, src: str, port: int) -> "WireItem | None":
        return next((w for w in self.wire_items if w.wire["from"] == src and w.wire["port"] == port), None)

    def select_wire(self, src: str, port: int):
        self.scene.clearSelection()
        wi = self.wire_item(src, port)
        if wi is not None:
            wi.setSelected(True)

    def select(self, nid: str):
        """Select one box and open its card (used after an edit made on the card, so it stays open)."""
        self.card_wanted = nid
        self.scene.clearSelection()
        it = self.box_item(nid)
        if it is not None:
            it.setSelected(True)
        self.selection_changed()

    def _place_empty_hint(self):
        h = self.empty_hint
        if not h.isVisible():
            return
        h.adjustSize()
        vp = self.view.viewport().geometry()
        h.move(int(vp.x() + (vp.width() - h.width()) / 2), int(vp.y() + (vp.height() - h.height()) / 2))

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._place_empty_hint()

    def _place_card(self):
        if not self.card.isVisible():
            return
        if self.card.wire is not None:
            wi = self.wire_item(self.card.wire["from"], self.card.wire["port"])
            if wi is None:
                return
            p = self.view.mapFromScene(wi.label_point() + QPointF(-60, 26))
            vp = self.view.viewport().rect()
            x = min(max(8, p.x()), vp.width() - self.card.width() - 8)
            y = p.y()
            if y + self.card.height() > vp.height() - 8:
                y = self.view.mapFromScene(wi.label_point()).y() - 26 - self.card.height()
            self.card.move(int(x), int(max(8, y)))
            return
        bid = (self.card.node or self.card.enc or self.card.power or {}).get("id")
        it = self.box_item(bid) if bid else None
        if it is None:
            return
        w, h = it.wh()
        tl = self.view.mapFromScene(it.mapToScene(QPointF(0, 0)))
        br = self.view.mapFromScene(it.mapToScene(QPointF(w, h)))
        x, y = card_place((tl.x(), tl.y(), br.x() - tl.x(), br.y() - tl.y()),
                          (self.card.width(), self.card.height()), self.view.viewport().rect().getRect()[2:])
        self.card.move(int(x), int(y))

    # -- editing
    def add_part(self, type_: str, x: float, y: float):
        if self.rig_name is None:
            self.show_notice("Make a rig first (New rig), then drag parts onto it.")
            return
        if type_ == R.ENCLOSURE or type_.startswith(R.ENCLOSURE + ":"):
            preset = type_.partition(":")[2] or "aluminium"
            cx, cy = x + R.PART_W / 2, y + R.PART_H / 2
            e = R.new_enclosure(self.rig, cx - R.ENC_W / 2, cy - R.ENC_H / 2,
                                name=R.ENC_PRESETS.get(preset, "Enclosure"))
            self.changed()
            self.scene.clearSelection()
            for ei in self.enc_items:
                if ei.enc["id"] == e["id"]:
                    ei.setSelected(True)
            return
        if type_.startswith(POWER_MIME_PREFIX):
            kind = type_[len(POWER_MIME_PREFIX):]
            b = R.new_power(self.rig, kind, x + (R.PART_W - R.POWER_W) / 2, y + (R.PART_H - R.POWER_H) / 2)
            self.changed()
            self.select(b["id"])
            self.show_notice("Drag from the power source's dot to the part it powers.", soft=True)
            return
        if type_.startswith(LIB_MIME_PREFIX):
            entry = next((e for e in self.library if e["id"] == type_[len(LIB_MIME_PREFIX):]), None)
            if entry is None:
                return
            node = R.node_from_lib(self.rig, entry, x, y)
            type_ = node["part"]["type"]
        else:
            node = R.new_node(self.rig, type_, x, y, self.labels)
        if not self.snap_on:
            node["x"], node["y"] = int(x), int(y)
        if type_ == "sdr" and self.receivers and not node["part"].get("rx"):
            used = {n["part"].get("rx") for n in self.rig["nodes"] if n is not node}
            node["part"]["rx"] = next((r for r in self.receivers if r not in used), self.receivers[0])
            if not node["part"].get("name") or node["part"]["name"] == self.labels.get("sdr", {}).get("label"):
                node["part"]["name"] = node["part"]["rx"]
        self.try_mate(node["id"])
        self.changed()
        self.select(node["id"])

    def add_cable_at(self, type_: str, pt: QPointF):
        """A cable dropped from the Cables list: onto the wire under (or nearest to)
        the drop point. A cable is never a box on its own."""
        near = [i for i in self.scene.items(QRectF(pt.x() - 14, pt.y() - 14, 28, 28)) if isinstance(i, WireItem)]
        if not near:
            self.show_notice("Drop a cable onto a wire: cables are the connections between parts. "
                             "Wire the two parts first, then drop the cable on the line between them.")
            return
        wi = near[0]
        if R.mated(wi.wire):
            self.show_notice("Those two parts are mated directly, port to port, so there is no wire to put a cable "
                             "on. Separate them first (click the joint, then Delete), then wire them.")
            return
        key = (wi.wire["from"], wi.wire["port"])
        wi.wire.setdefault("cables", []).append(R.new_cable(type_, self.labels))
        self.show_notice(None)
        self.changed()
        self.select_wire(*key)

    def start_wire(self, item: PartItem, port: int, pt: QPointF):
        self.wiring_from = (item.node["id"], port)
        self._temp_wire()
        item.update()
        self.drag_wire(pt)

    def start_wire_in(self, item: PartItem, pt: QPointF):
        """A wire dragged from an input dot (relay 006: every dot starts a wire). Let go over
        another part and that part's first free output feeds this input."""
        self.wiring_from = ("in", item.node["id"])
        self._temp_wire()
        item.update()
        self.drag_wire(pt)

    def _temp_wire(self):
        self.temp_wire = QGraphicsPathItem()
        self.temp_wire.setPen(QPen(T.color("survey"), 2, Qt.PenStyle.DashLine, Qt.PenCapStyle.RoundCap))
        self.temp_wire.setZValue(1e6)
        self.scene.addItem(self.temp_wire)

    def drag_wire(self, pt: QPointF):
        if self.temp_wire is None:
            return
        nid = self.wiring_from[1] if self.wiring_from[0] == "in" else self.wiring_from[0]
        node = self.items_by_id[nid].node
        side = G.side_to_point(node, pt.x(), pt.y())
        ax, ay = G.mid_side(node, side)
        target = next((i for i in self.scene.items(pt) if isinstance(i, PartItem)
                       and i.node["id"] != node["id"]), None)
        end = (pt.x(), pt.y())
        end_side = None
        if target is not None:              # the curve settles onto the part it will join
            end_side = G.OPPOSITE[G.side_toward(node, target.node)]
            end = G.mid_side(target.node, end_side)
        self.temp_wire.setPath(wire_path((ax, ay, side), end, self.wire_style, end_side))
        new = target.node["id"] if target is not None else None
        if new != self.drop_target:
            old, self.drop_target = self.drop_target, new
            for i in (old, new):
                if i in self.items_by_id:
                    self.items_by_id[i].update()

    def end_wire(self, pt: QPointF):
        wf = self.wiring_from
        self.wiring_from = None
        self.drop_target = None
        if self.temp_wire is not None:
            self.scene.removeItem(self.temp_wire)
            self.temp_wire = None
        if wf[0] == "in":
            return self._end_wire_in(wf[1], pt)
        src, port = wf
        target = next((i for i in self.scene.items(pt) if isinstance(i, PartItem)), None)
        if target is None or target.node["id"] == src:
            self.items_by_id[src].update()
            return
        old = [w for w in self.rig["wires"] if w["from"] == src and w["port"] == port]
        if old:
            R.disconnect(self.rig, src, port)          # dragging from a wired output re-wires it
        why = R.connect(self.rig, src, port, target.node["id"])
        if why:
            self.rig["wires"] += old
            self.show_notice(f"Not wired: {why}.")
            self.items_by_id[src].update()
            return
        if old and old[0].get("cables"):                 # a re-wired output keeps its cables
            R.wire_of(self.rig, src, port)["cables"] = old[0]["cables"]
        self.show_notice(None)
        self.changed()

    def _end_wire_in(self, dst: str, pt: QPointF):
        target = next((i for i in self.scene.items(pt) if isinstance(i, PartItem)), None)
        if target is None or target.node["id"] == dst:
            self.items_by_id[dst].update()
            return
        free = target.free_outputs()
        if not free:
            self.show_notice(f"Not wired: {target.node['part'].get('name') or 'that part'} has no free output. "
                             "Take one of its wires out first, or drag from its output dot instead.")
            self.items_by_id[dst].update()
            return
        old = [w for w in self.rig["wires"] if w["to"] == dst]
        self.rig["wires"] = [w for w in self.rig["wires"] if w["to"] != dst]   # dragging from a wired input re-wires it
        why = R.connect(self.rig, target.node["id"], free[0], dst)
        if why:
            self.rig["wires"] += old
            self.show_notice(f"Not wired: {why}.")
            self.items_by_id[dst].update()
            return
        if old and old[0].get("cables"):
            R.wire_of(self.rig, target.node["id"], free[0])["cables"] = old[0]["cables"]
        self.show_notice(None)
        self.changed()

    # -- power wires
    def start_power_wire(self, item: "PowerItem", pt: QPointF):
        self.power_from = item.box["id"]
        self.temp_wire = QGraphicsPathItem()
        pen = QPen(T.color("power"), T.WIRE_W, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap)
        pen.setDashPattern([3, 2.5])
        self.temp_wire.setPen(pen)
        self.temp_wire.setZValue(1e6)
        self.scene.addItem(self.temp_wire)
        self.drag_power_wire(pt)

    def start_power_from_part(self, item: PartItem, pt: QPointF):
        """A power wire dragged from a part's power input (the diamond) to a power source."""
        self.power_from = ("to", item.node["id"])
        self.temp_wire = QGraphicsPathItem()
        pen = QPen(T.color("power"), T.WIRE_W, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap)
        pen.setDashPattern([3, 2.5])
        self.temp_wire.setPen(pen)
        self.temp_wire.setZValue(1e6)
        self.scene.addItem(self.temp_wire)
        self.drag_power_wire(pt)

    def drag_power_wire(self, pt: QPointF):
        if self.temp_wire is None:
            return
        if isinstance(self.power_from, tuple):           # from a part's power input to a source
            a = self.power_map.get(self.power_from[1])
            if a is None:
                return
            src = next((i for i in self.scene.items(pt) if isinstance(i, PowerItem)), None)
            end = (pt.x(), pt.y()) if src is None else (src.out_point().x(), src.out_point().y())
            self.temp_wire.setPath(wire_path(a, end, self.wire_style))
            return
        a = self.power_out.get(self.power_from)
        b = self.power_items[self.power_from]
        if a is None:
            a = (b.pos().x() + b.wh()[0], b.pos().y() + b.wh()[1] / 2, "right")
        target = next((i for i in self.scene.items(pt) if isinstance(i, PartItem)), None)
        end, side = (pt.x(), pt.y()), None
        if target is not None:
            w, h = target.wh()
            side = "top" if a[1] < target.pos().y() + h / 2 else "bottom"
            end = (target.pos().x() + w * 0.8, target.pos().y() + (0 if side == "top" else h))
        self.temp_wire.setPath(wire_path(a, end, self.wire_style, side))
        new = target.node["id"] if target is not None else None
        if new != self.drop_target:
            old, self.drop_target = self.drop_target, new
            for nid in (old, new):
                if nid in self.items_by_id:
                    self.items_by_id[nid].update()

    def end_power_wire(self, pt: QPointF):
        src, self.power_from = self.power_from, None
        self.drop_target = None
        if self.temp_wire is not None:
            self.scene.removeItem(self.temp_wire)
            self.temp_wire = None
        if isinstance(src, tuple):                       # from a part's power input
            box = next((i for i in self.scene.items(pt) if isinstance(i, PowerItem)), None)
            if box is None:
                self.items_by_id[src[1]].update()
                return
            old = R.power_feed(self.rig, src[1])
            R.disconnect_power(self.rig, src[1])
            why = R.connect_power(self.rig, box.box["id"], src[1])
            if why:
                if old is not None:
                    self.rig.setdefault("power_wires", []).append(old)
                self.show_notice(f"No power wire: {why}.")
                return
            self.show_notice(None)
            self.changed()
            return
        target = next((i for i in self.scene.items(pt) if isinstance(i, PartItem)), None)
        if target is None:
            for it in self.items_by_id.values():
                it.update()
            return
        old = R.power_feed(self.rig, target.node["id"])
        if old is not None and old["from"] != src:
            R.disconnect_power(self.rig, target.node["id"])      # dragging a new feed onto a part replaces it
        why = R.connect_power(self.rig, src, target.node["id"])
        if why:
            if old is not None and R.power_feed(self.rig, target.node["id"]) is None:
                self.rig.setdefault("power_wires", []).append(old)
            self.show_notice(f"No power wire: {why}.")
            for it in self.items_by_id.values():
                it.update()
            return
        self.show_notice(None)
        self.changed()

    # -- direct mating, colours and SMA kinds (relay 006)
    def _mate_candidate(self, piece: set, offset=(0.0, 0.0), tol: float = MATE_TOL, moving: set | None = None):
        """The nearest pair (distance, a, kind, port, b, kind, port): a free point of `piece` (shifted
        by `offset`) within `tol` of where it would sit to mate with a free point of another part, of
        the other kind (an output meets an input), at least one of the two being an SMA part."""
        moving = moving or piece
        nodes = {n["id"]: n for n in self.rig["nodes"]}
        am = self.anchor_map or G.anchors(self.rig)
        outs = {(w["from"], w["port"]) for w in self.rig["wires"]}
        ins = {w["to"] for w in self.rig["wires"]}

        def free(n, kind, port):
            return (n, port) not in outs if kind == "out" else n not in ins

        sma = set(R.SMA_TYPES)
        best = None
        for (a, ka, pa), (xa, ya, _) in am.items():
            if a not in piece or a not in nodes or not free(a, ka, pa):
                continue
            for (b, kb, pb), pt in am.items():
                if b in moving or b not in nodes or kb == ka or not free(b, kb, pb):
                    continue
                if not ({nodes[a]["part"]["type"], nodes[b]["part"]["type"]} & sma):
                    continue
                tx, ty = G.mate_target(pt)
                d = math.hypot(tx - xa - offset[0], ty - ya - offset[1])
                if d <= tol and (best is None or d < best[0]):
                    best = (d, a, ka, pa, b, kb, pb)
        return best

    def try_mate(self, nid: str) -> bool:
        """An SMA part (or a part dropped on an SMA part's dot) whose free connection point lands near
        another part's free point of the other kind mates with it: a wire marked "mated", the two
        points a small gap apart with a connector stub between them (relay 007). The piece that
        was dropped settles into place and the joint flashes once. Returns whether it mated."""
        piece = R.assembly(self.rig, nid)
        if any(R.is_locked(R.box(self.rig, m)) for m in piece):
            return False
        self.anchor_map = G.anchors(self.rig)
        best = self._mate_candidate(piece)
        if best is None:
            return False
        nodes = {n["id"]: n for n in self.rig["nodes"]}
        old = {m: (nodes[m]["x"], nodes[m]["y"]) for m in piece}
        _, a, ka, pa, b, kb, pb = best
        src, port, dst = (a, pa, b) if ka == "out" else (b, pb, a)
        why = R.mate(self.rig, src, port, dst)
        if why:
            self.show_notice(f"Not mated: {why}.", soft=True)
            return False
        for _ in range(3):                # the dropped piece moves so its point sits the gap away
            am = G.anchors(self.rig)
            tx, ty = G.mate_target(am[(b, kb, pb)])
            dx, dy = tx - am[(a, ka, pa)][0], ty - am[(a, ka, pa)][1]
            if abs(dx) < 0.5 and abs(dy) < 0.5:
                break
            for m in piece:
                nodes[m]["x"], nodes[m]["y"] = int(round(nodes[m]["x"] + dx)), int(round(nodes[m]["y"] + dy))
        am = G.anchors(self.rig)
        pa_, pb_ = am[(a, ka, pa)], am[(b, kb, pb)]
        self._pending_settle = (old, ((pa_[0] + pb_[0]) / 2, (pa_[1] + pb_[1]) / 2))
        self.status.emit(f"mated {R._pname(nodes[src])} with {R._pname(nodes[dst])}")
        return True

    def _settle(self):
        """After a redraw that followed a mating: the dropped piece glides the last few pixels into
        place, and the joint flashes once, quietly."""
        pend, self._pending_settle = getattr(self, "_pending_settle", None), None
        if not pend:
            return
        old, joint = pend
        group = QParallelAnimationGroup(self)
        for nid, (x, y) in old.items():
            it = self.items_by_id.get(nid)
            if it is None or (it.pos().x(), it.pos().y()) == (x, y):
                continue
            a = QPropertyAnimation(it, b"pos", group)
            a.setDuration(160)
            a.setStartValue(QPointF(x, y))
            a.setEndValue(it.pos())
            a.setEasingCurve(QEasingCurve.Type.OutCubic)
            group.addAnimation(a)
        if group.animationCount():
            self.animating = True

            def done():
                self.animating = False
                for nid in old:
                    b = R.box(self.rig, nid)
                    it = self.items_by_id.get(nid)
                    if b is not None and it is not None:
                        b["x"], b["y"] = int(round(it.pos().x())), int(round(it.pos().y()))
                self._refresh_wires()

            group.finished.connect(done)
            self._settle_anim = group
            group.start()
        ring = QGraphicsEllipseItem()
        ring.setZValue(1e6 + 2)
        ring.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.scene.addItem(ring)
        flash = QVariantAnimation(self)
        flash.setDuration(450)
        flash.setStartValue(0.0)
        flash.setEndValue(1.0)

        def step(t):
            try:
                r = 6 + 16 * t
                ring.setRect(QRectF(joint[0] - r, joint[1] - r, 2 * r, 2 * r))
                ring.setPen(QPen(T.color("survey", 0.9 * (1 - t)), 2))
            except RuntimeError:
                pass

        def gone():
            try:
                self.scene.removeItem(ring)
            except RuntimeError:
                pass

        flash.valueChanged.connect(step)
        flash.finished.connect(gone)
        self._flash = flash
        step(0.0)
        flash.start()

    def magnet(self, lead, v: QPointF) -> QPointF:
        """While a part is dragged, a point that comes near a matching point is pulled the last few
        pixels onto its mating place (a gentle magnetic pull), with a soft ring where it will sit."""
        d = self._drag
        if not isinstance(lead, PartItem) or not d:
            self._show_magnet(None)
            return v
        moving = {self.box_id(i) for i in d[2] if isinstance(i, PartItem)} | {lead.node["id"]}
        off = (v.x() - lead.pos().x(), v.y() - lead.pos().y())
        best = self._mate_candidate(moving, off, MAGNET, moving)
        if best is None:
            self._show_magnet(None)
            return v
        _, a, ka, pa, b, kb, pb = best
        tx, ty = G.mate_target(self.anchor_map[(b, kb, pb)])
        ax, ay = self.anchor_map[(a, ka, pa)][:2]
        self._show_magnet((tx, ty))
        return QPointF(v.x() + (tx - ax - off[0]), v.y() + (ty - ay - off[1]))

    def _show_magnet(self, at):
        ring = getattr(self, "_magnet_ring", None)
        try:
            if at is None:
                if ring is not None:
                    ring.hide()
                return
            if ring is None or ring.scene() is not self.scene:
                ring = QGraphicsEllipseItem()
                ring.setZValue(1e6 + 2)
                ring.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
                ring.setPen(QPen(T.color("survey", 0.7), 1.5, Qt.PenStyle.DashLine))
                self.scene.addItem(ring)
                self._magnet_ring = ring
            ring.setRect(QRectF(at[0] - 10, at[1] - 10, 20, 20))
            ring.show()
        except RuntimeError:
            self._magnet_ring = None

    def pull_off(self, nid: str) -> bool:
        """Alt + drag: the part comes off what it is mated to and moves alone. Undo puts it back."""
        gone = [w for w in self.rig["wires"] if R.mated(w) and nid in (w["from"], w["to"])]
        if not gone:
            return False
        self.rig["wires"] = [w for w in self.rig["wires"] if w not in gone]
        for wi in self.wire_items:
            if wi.wire in gone:
                wi.hide()
        self._refresh_wires()
        self.status.emit("pulled off: let go anywhere (near a matching point it mates again)")
        return True

    def _realign(self, root: str):
        """After an adapter turns or changes shape, the parts mated to it move to meet its
        connection points again. The root stays where it is."""
        nodes = {n["id"]: n for n in self.rig["nodes"]}
        placed, todo = {root}, [root]
        while todo:
            n = todo.pop()
            for w in [w for w in self.rig["wires"] if R.mated(w) and n in (w["from"], w["to"])]:
                other = w["to"] if w["from"] == n else w["from"]
                if other in placed:
                    continue
                am = G.anchors(self.rig)
                here = (w["from"], "out", w["port"]) if w["from"] == n else (w["to"], "in", 0)
                there = (w["to"], "in", 0) if w["from"] == n else (w["from"], "out", w["port"])
                if here not in am or there not in am:
                    continue
                tx, ty = G.mate_target(am[here])
                dx, dy = tx - am[there][0], ty - am[there][1]
                rest = {"nodes": self.rig["nodes"], "wires": [x for x in self.rig["wires"] if x is not w]}
                for m in R.assembly(rest, other) - placed:
                    nodes[m]["x"], nodes[m]["y"] = int(round(nodes[m]["x"] + dx)), int(round(nodes[m]["y"] + dy))
                placed.add(other)
                todo.append(other)

    def _separate(self, w: dict):
        """Unscrew a mating: the mating goes, and the second part (with what is mated to it)
        steps two grid squares away, so the two dots show again."""
        side = (self.anchor_map.get((w["from"], "out", w["port"])) or (0, 0, "right"))[2]
        nx, ny = G.NORMAL[side]
        R.disconnect(self.rig, w["from"], w["port"])
        for m in R.assembly(self.rig, w["to"]):
            b = R.box(self.rig, m)
            if not R.is_locked(b):
                b["x"], b["y"] = b["x"] + nx * 2 * R.GRID, b["y"] + ny * 2 * R.GRID

    def separate_from(self, nid: str, other: str):
        w = next((w for w in self.rig["wires"] if R.mated(w) and {w["from"], w["to"]} == {nid, other}), None)
        if w is not None:
            self._separate(w)
            self.changed()

    def set_text_pos(self, ids, code: str):
        """Put boxes' words in one of nine places (relay 008). Drawing only."""
        hit = False
        for bid in ids:
            b = R.box(self.rig, bid)
            if b is None or R.kind_of(self.rig, bid) == "enclosure" or code not in R.TEXT_POS:
                continue
            if code == R.DEFAULT_TEXT_POS:
                hit |= "text_pos" in b
                b.pop("text_pos", None)
            else:
                hit |= b.get("text_pos") != code
                b["text_pos"] = code
        if hit:
            self.changed()
            self.select_ids(list(ids))
            if len(ids) == 1:
                self.card_wanted = ids[0]
                self.selection_changed()

    def box_clicked(self, item):
        """A left click (press and release without dragging) on a box opens its card (relay 008)."""
        self.card_wanted = self.box_id(item)
        self.selection_changed()

    def set_shape(self, ids, shape: str | None):
        """Draw boxes in another shape (None: back to their kind's). Drawing only."""
        hit = False
        for bid in ids:
            b = R.box(self.rig, bid)
            if b is None or R.kind_of(self.rig, bid) == "enclosure":
                continue
            if shape in R.SHAPES:
                hit |= b.get("shape") != shape
                b["shape"] = shape
            elif "shape" in b:
                b.pop("shape")
                hit = True
        if hit:
            self.changed()
            self.select_ids(list(ids))

    def set_antenna(self, nid: str, redraw: bool = True, **kw):
        """Set an antenna's type or one of its settings (None clears it). Drawing and Output only."""
        node = R.box(self.rig, nid)
        if node is None:
            return
        a = dict(R.antenna(node))
        for k, v in kw.items():
            if v in (None, ""):
                a.pop(k, None)
            else:
                a[k] = v
        if a:
            node["antenna"] = a
        else:
            node.pop("antenna", None)
        if redraw:
            self.changed()
            self.select(nid)
        else:
            self.changed(redraw=False, merge=("antenna", nid, tuple(kw)))
            it = self.items_by_id.get(nid)
            if it is not None:
                it.update()
            self.fill_output()

    def choose_antenna(self, nid: str, where: QPoint):
        """On dropping an antenna: a small menu asks its type (Esc leaves it unset)."""
        m = Menu(self)
        m.add_heading("What kind of antenna?")
        for k, (label, _f) in R.ANTENNA_TYPES.items():
            a = m.addAction(QIcon(kind_pixmap("ant_" + k, T.color("text2"), 16, 2.0)), label)
            a.triggered.connect(lambda _=False, k=k: self.set_antenna(nid, type=k))
        m.exec(where)

    def set_colour(self, ids, colour: str | None):
        """Give boxes a colour of their own (None: back to their kind's colour). Drawing only."""
        hit = False
        for bid in ids:
            b = R.box(self.rig, bid)
            if b is None:
                continue
            if colour and T.is_hex(colour):
                hit |= b.get("colour") != colour
                b["colour"] = colour
            elif "colour" in b:
                b.pop("colour")
                hit = True
        if hit:
            self.changed()
            self.select_ids(list(ids))

    def pick_colour(self, ids):
        b = R.box(self.rig, ids[0]) if ids else None
        start = QColor((b or {}).get("colour") or T.C["survey"])
        c = QColorDialog.getColor(start, self, "Colour")
        if c.isValid():
            self.set_colour(ids, c.name())

    def set_sma(self, nid: str, **kw):
        """Choose an SMA part's shape or gender. A T that becomes another shape loses its second
        output's wire; parts mated to it move to meet its points again."""
        node = R.box(self.rig, nid)
        if node is None:
            return
        R.set_sma(node, **{k: (v or None) for k, v in kw.items()})
        n = R.out_ports(node)
        self.rig["wires"] = [w for w in self.rig["wires"] if not (w["from"] == nid and w["port"] >= n)]
        if "shape" in kw and not R.sma(node).get("shape"):
            R.set_sma(node, turn=None)
        self._realign(nid)
        self.changed()
        self.select(nid)

    def turn_adapter(self, nid: str):
        node = R.box(self.rig, nid)
        if node is None or not R.sma(node).get("shape"):
            return
        R.set_sma(node, turn=(int(R.sma(node).get("turn") or 0) + 1) % 4)
        self._realign(nid)
        self.changed()
        self.select(nid)

    # -- stacking, groups and dragging
    def box_id(self, item) -> str:
        if isinstance(item, PartItem):
            return item.node["id"]
        if isinstance(item, PowerItem):
            return item.box["id"]
        return item.enc["id"]

    def box_item(self, bid: str):
        return self.items_by_id.get(bid) or self.enc_by_id.get(bid) or self.power_items.get(bid)

    def all_box_items(self) -> list:
        return list(self.enc_items) + list(self.items_by_id.values()) + list(self.power_items.values())

    def selected_boxes(self) -> list[str]:
        try:
            return [self.box_id(i) for i in self.scene.selectedItems()
                    if isinstance(i, (PartItem, EnclosureItem, PowerItem))]
        except RuntimeError:
            return []

    def _members(self, g: dict) -> list:
        return [i for i in (self.box_item(m) for m in g["members"]) if i is not None]

    def select_with_group(self, item):
        g = R.group_of(self.rig, self.box_id(item))
        for it in (self._members(g) if g else [item]):
            it.setSelected(True)

    def can_group(self, ids: list[str]) -> bool:
        if len(ids) < 2:
            return False
        gs = {id(R.group_of(self.rig, i)) for i in ids}
        g = R.group_of(self.rig, ids[0])
        return not (len(gs) == 1 and g is not None and set(g["members"]) == set(ids))

    def arrange(self, how: str):
        ids = self.selected_boxes()
        if self.animating or not ids:
            return
        if R.restack(self.rig, ids, how):
            self.changed()
        else:
            self.status.emit({"front": "already at the front", "forward": "already at the front",
                              "backward": "already at the back", "back": "already at the back"}[how])

    def group_selected(self):
        ids = self.selected_boxes()
        if self.animating or not self.can_group(ids):
            if len(ids) < 2:
                self.show_notice("Select two boxes or more to group them (Ctrl+click, or drag a "
                                 "rectangle round them), then Group.", soft=True)
            return
        R.make_group(self.rig, ids)
        self.changed()

    def ungroup_selected(self):
        ids = self.selected_boxes()
        if self.animating or not ids:
            return
        if R.ungroup(self.rig, ids):
            self.changed()

    def group_press(self, item, mods):
        """A press on a grouped box. The first click takes the whole group; a click on a
        group already selected as a whole goes to the one box (decided on release, so a
        drag still moves the group). Ctrl+click is left to Qt: it toggles the one box."""
        self._group_click = None
        if mods & Qt.KeyboardModifier.ControlModifier:
            return
        g = R.group_of(self.rig, self.box_id(item))
        if not g:
            return
        members = self._members(g)
        if all(m.isSelected() for m in members):
            self._group_click = ("enter", item)
            return
        self.scene.clearSelection()
        for m in members:
            m.setSelected(True)
        self._group_click = ("group", item)

    def group_release(self, item, moved: bool):
        click, self._group_click = self._group_click, None
        if moved or not click or click[1] is not item:
            return
        if click[0] == "group":            # Qt narrowed the selection to the one box: widen it again
            g = R.group_of(self.rig, self.box_id(item))
            for m in (self._members(g) if g else []):
                m.setSelected(True)
        # "enter": Qt has already left just this box selected, which is the second click

    def begin_drag(self, lead):
        """A box is about to be dragged. Only the box under the pointer is moved by Qt; every
        other selected box, what an enclosure holds and the whole group of anything it holds
        follow it by the same step (drag_moved). So a smart-guide snap on the lead moves the
        whole selection rigidly. Locked boxes stay where they are."""
        try:
            sel = [i for i in self.scene.selectedItems() if isinstance(i, (PartItem, EnclosureItem, PowerItem))]
        except RuntimeError:
            return
        if lead not in sel:
            sel.append(lead)
        if R.is_locked(R.box(self.rig, self.box_id(lead))):
            self._drag = None
            return
        moving = G.moving_with(self.rig, [self.box_id(i) for i in sel])
        carried, held = {}, []
        for bid in moving:
            it = self.box_item(bid)
            if it is None or it is lead:
                continue
            carried[it] = it.pos()
            if it.flags() & QGraphicsItem.GraphicsItemFlag.ItemIsMovable:
                it.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)    # Qt must not move it too
                held.append(it)
        others = []
        for it in self.all_box_items():
            if it is lead or it in carried:
                continue
            w, h = it.wh()
            others.append((it.pos().x(), it.pos().y(), w, h))
        self._drag = (lead, lead.pos(), carried, others, held)

    def snapped(self, item, v: QPointF) -> QPointF:
        """Where a box goes when Qt moves it: on the grid while the owner drags it (snap on), else
        exactly where it was put. Only the box under the pointer snaps; what it carries follows by
        the same step, so a mated pair that sits off the grid stays lined up (relay 006)."""
        d = self._drag
        if self.snap_on and d and d[0] is item:
            return QPointF(R.snap(v.x()), R.snap(v.y()))
        return v

    def guide(self, item, v: QPointF) -> QPointF:
        """A box's next place while it is dragged: snapped to the smart guides of the boxes
        that are not moving, with the guide lines drawn. Any other move passes through."""
        d = self._drag
        if not d or d[0] is not item or self.animating:
            return v
        w, h = item.wh()
        dx, dy, lines = G.guide_snap((v.x(), v.y(), w, h), d[3])
        pulled = self.magnet(item, QPointF(v.x() + dx, v.y() + dy))
        if pulled != QPointF(v.x() + dx, v.y() + dy):
            lines = []                       # the magnet wins over the guides
        self._show_guides(lines)
        return pulled

    def _show_guides(self, lines):
        while len(self.guide_items) < len(lines):
            li = QGraphicsLineItem()
            pen = QPen(T.color("guide"), 1, Qt.PenStyle.DashLine)
            pen.setCosmetic(True)
            li.setPen(pen)
            li.setZValue(1e6 + 1)
            li.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            self.scene.addItem(li)
            self.guide_items.append(li)
        for k, li in enumerate(self.guide_items):
            if k < len(lines):
                kind, at, a, b = lines[k]
                li.setLine(QLineF(at, a, at, b) if kind == "v" else QLineF(a, at, b, at))
                li.show()
            else:
                li.hide()

    def drag_moved(self, item):
        d = self._drag
        if d and d[0] is item and d[2]:
            step = item.pos() - d[1]
            for it, start in d[2].items():
                it.setPos(start + step)
        if self.group_marks:
            self._mark_groups()

    def end_drag(self, lead) -> bool:
        d, self._drag = self._drag, None
        self._show_guides([])
        self._show_magnet(None)
        if d:
            for it in d[4]:
                try:
                    it.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
                except RuntimeError:
                    pass
        return bool(d and d[0] is lead and lead.pos() != d[1])

    # -- selecting, nudging, the clipboard, lock, align (relay 005)
    def shift_toggle(self, item):
        """Shift+click: add the box (with its group) to the selection, or take it out."""
        g = R.group_of(self.rig, self.box_id(item))
        members = self._members(g) if g else [item]
        on = not item.isSelected()
        for m in members:
            m.setSelected(on)

    def select_all(self):
        for it in self.all_box_items():
            it.setSelected(True)

    def key(self, ev) -> bool:
        """The grid's own keys (see SHORTCUTS). True when the key was used."""
        k, mods = ev.key(), ev.modifiers()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        steps = {Qt.Key.Key_Left: (-1, 0), Qt.Key.Key_Right: (1, 0), Qt.Key.Key_Up: (0, -1), Qt.Key.Key_Down: (0, 1)}
        if k in steps and not ctrl:
            dx, dy = steps[k]
            n = R.GRID * (5 if shift else 1)
            self.nudge(dx * n, dy * n)
            return True
        if k in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.take_out_selected()
            return True
        if k == Qt.Key.Key_Escape:
            if self.wiring_from is not None or self.power_from is not None:
                self.cancel_wire()
            else:
                self.scene.clearSelection()
            return True
        if ctrl and not shift:
            act = {Qt.Key.Key_A: self.select_all, Qt.Key.Key_C: self.copy, Qt.Key.Key_X: self.cut,
                   Qt.Key.Key_V: self.paste, Qt.Key.Key_D: self.duplicate}.get(k)
            if act is not None:
                act()
                return True
        return False

    def cancel_wire(self):
        self.wiring_from = self.power_from = None
        self.drop_target = None
        if self.temp_wire is not None:
            self.scene.removeItem(self.temp_wire)
            self.temp_wire = None
        for it in self.items_by_id.values():
            it.update()

    def nudge(self, dx: int, dy: int):
        ids = self.selected_boxes()
        if self.animating or not ids:
            return
        if G.shift(self.rig, ids, dx, dy):
            self.changed(merge=("nudge", tuple(sorted(ids))))
        else:
            self.status.emit("locked: unlock it first (Ctrl+Shift+L)")

    def _fragment_of_selection(self) -> dict | None:
        ids = self.selected_boxes()
        return R.fragment(self.rig, ids) if ids else None

    def copy(self) -> bool:
        frag = self._fragment_of_selection()
        if not frag:
            return False
        self.clip = frag
        self._paste_n = 0
        md = QMimeData()
        md.setData(FRAGMENT_MIME, json.dumps(frag).encode())
        md.setText(" > ".join(n["part"].get("name") or n["part"]["type"] for n in frag.get("nodes", [])))
        QApplication.clipboard().setMimeData(md)
        n = sum(len(frag.get(k, [])) for k in ("nodes", "enclosures", "power"))
        self.status.emit(f"copied {n} box{'es' if n != 1 else ''}")
        return True

    def _clip_fragment(self) -> dict | None:
        md = QApplication.clipboard().mimeData()
        if md is not None and md.hasFormat(FRAGMENT_MIME):
            try:
                return json.loads(bytes(md.data(FRAGMENT_MIME)).decode())
            except ValueError:
                pass
        return self.clip

    def cut(self):
        if self.copy():
            self.take_out_selected()

    def paste(self, at: QPointF | None = None):
        """Paste at the pointer when it is over the grid (or at `at`), else beside the originals."""
        frag = self._clip_fragment()
        if not frag or self.rig_name is None or self.animating:
            return
        boxes = list(frag.get("nodes", [])) + list(frag.get("enclosures", [])) + list(frag.get("power", []))
        x0 = min(b["x"] for b in boxes)
        y0 = min(b["y"] for b in boxes)
        if at is None:
            p = self.view.mapFromGlobal(QCursor.pos())
            if self.view.viewport().rect().contains(p) and self.view.underMouse():
                at = self.view.mapToScene(p)
        if at is not None:
            dx, dy = R.snap(at.x() - x0), R.snap(at.y() - y0)
        else:
            self._paste_n += 1
            dx = dy = 2 * R.GRID * self._paste_n
        self._paste_fragment(frag, dx, dy)

    def _paste_fragment(self, frag: dict, dx: float, dy: float):
        made = R.paste(self.rig, frag, dx, dy)
        self.changed()
        self.scene.clearSelection()
        for bid in made:
            it = self.box_item(bid)
            if it is not None:
                it.setSelected(True)
        self.status.emit(f"pasted {len(made)} box{'es' if len(made) != 1 else ''}")

    def duplicate(self):
        frag = self._fragment_of_selection()
        if frag and self.rig_name is not None and not self.animating:
            self._paste_fragment(frag, 2 * R.GRID, 2 * R.GRID)

    def set_lock(self, ids, on: bool):
        if R.set_locked(self.rig, ids, on):
            self.changed()
            if len(ids) == 1:
                self.select(ids[0])
            self.status.emit("locked: it stays where it is" if on else "unlocked")

    def toggle_lock(self):
        ids = self.selected_boxes()
        if not ids:
            return
        self.set_lock(ids, not all(R.is_locked(R.box(self.rig, i)) for i in ids))

    def align_selected(self, how: str):
        ids = self.selected_boxes()
        if G.place(self.rig, G.align(self.rig, ids, how)):
            self.changed()
        else:
            self.status.emit("already lined up")

    def distribute_selected(self, axis: str):
        ids = self.selected_boxes()
        if G.place(self.rig, G.distribute(self.rig, ids, axis)):
            self.changed()
        else:
            self.status.emit("already evenly spread")

    def _mark_groups(self):
        """A dashed frame round each group selected as a whole."""
        for m in self.group_marks:
            try:
                self.scene.removeItem(m)
            except RuntimeError:
                pass
        self.group_marks = []
        try:
            for g in R.groups(self.rig):
                members = self._members(g)
                if len(members) < 2 or not all(m.isSelected() for m in members):
                    continue
                r = QRectF()
                for m in members:
                    w, h = m.wh()
                    r = r.united(m.mapRectToScene(QRectF(0, 0, w, h)))
                mark = QGraphicsRectItem(r.adjusted(-12, -12, 12, 12))
                pen = QPen(T.color("survey", 0.8), 1.4, Qt.PenStyle.DashLine)
                mark.setPen(pen)
                mark.setBrush(Qt.BrushStyle.NoBrush)
                mark.setZValue(1e6 - 1)
                mark.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
                self.scene.addItem(mark)
                self.group_marks.append(mark)
        except RuntimeError:                # mid-redraw: the items are being replaced
            self.group_marks = []

    def take_out_selected(self):
        sel = self.scene.selectedItems()
        if not sel:
            return
        for it in sel:
            if isinstance(it, PartItem):
                R.remove_node(self.rig, it.node["id"])
            elif isinstance(it, WireItem) and R.mated(it.wire):
                self._separate(it.wire)
            elif isinstance(it, WireItem):
                R.disconnect(self.rig, it.wire["from"], it.wire["port"])
            elif isinstance(it, EnclosureItem):
                R.remove_enclosure(self.rig, it.enc["id"])
            elif isinstance(it, PowerItem):
                R.remove_power(self.rig, it.box["id"])
            elif isinstance(it, PowerWireItem):
                R.disconnect_power(self.rig, it.wire["to"])
        self.changed()

    def changed(self, redraw: bool = True, merge=None):
        """Every edit ends here. The state before it goes on the undo list; edits that
        keep typing into one field (merge key unchanged) count as one step. An archived rig is
        read-only: any edit is put back at once, with a word why (relay 007)."""
        if self.read_only and self._last is not None:
            self._restore(self._last)
            self.show_notice("This rig is archived, so it is read-only. Restore it from the Rig menu to change it.")
            return
        if self._last is not None and not (merge is not None and merge == self._merge and self.undo_stack):
            self.undo_stack.append(self._last)
            del self.undo_stack[:-UNDO_MAX]
        self._merge = merge
        self.redo_stack.clear()
        self._last = self._snapshot()
        self.dirty = self._saved is None or self._last != self._saved
        if redraw:
            self.redraw()
        else:
            for it in self.items_by_id.values():
                it.update()
            self.refresh_side()

    # -- undo and redo
    def _snapshot(self) -> dict:
        return {"rig": copy.deepcopy(self.rig), "static": self.static_}

    def _reset_history(self, saved: bool):
        self.undo_stack, self.redo_stack, self._merge = [], [], None
        self._last = self._snapshot()
        self._saved = copy.deepcopy(self._last) if saved else None

    def _restore(self, snap: dict):
        self._last, self._merge = snap, None
        self.rig = copy.deepcopy(snap["rig"])
        self.static_ = snap["static"]
        self.dirty = self._saved is None or snap != self._saved
        self.redraw()

    def undo(self):
        if self.animating or not self.undo_stack:
            return
        self.redo_stack.append(self._last)
        self.show_notice(None)
        self._restore(self.undo_stack.pop())

    def redo(self):
        if self.animating or not self.redo_stack:
            return
        self.undo_stack.append(self._last)
        self.show_notice(None)
        self._restore(self.redo_stack.pop())

    # -- arranging
    def tidy(self):
        """Glide every part to its tidy place (geometry.tidy); one undo step."""
        target, rects = G.tidy_all(self.rig)
        moves = {nid: xy for nid, xy in target.items()
                 if self.box_item(nid) is not None
                 and (R.box(self.rig, nid)["x"], R.box(self.rig, nid)["y"]) != xy}
        boxes = {ei: rect for ei in self.enc_items
                 for eid, rect in rects.items()
                 if ei.enc["id"] == eid and (ei.enc["x"], ei.enc["y"], ei.enc["w"], ei.enc["h"]) != rect}
        if not moves and not boxes:
            self.status.emit("already tidy")
            return
        self.animating = True
        self.view.setInteractive(False)
        group = QParallelAnimationGroup(self)
        for nid, (x, y) in moves.items():
            it = self.box_item(nid)
            a = QPropertyAnimation(it, b"pos", group)
            a.setDuration(420)
            a.setStartValue(it.pos())
            a.setEndValue(QPointF(x, y))
            a.setEasingCurve(QEasingCurve.Type.OutCubic)
            group.addAnimation(a)
        for ei, (x, y, _w, _h) in boxes.items():
            a = QPropertyAnimation(ei, b"pos", group)
            a.setDuration(420)
            a.setStartValue(ei.pos())
            a.setEndValue(QPointF(x, y))
            a.setEasingCurve(QEasingCurve.Type.OutCubic)
            group.addAnimation(a)

        def done():
            for nid, (x, y) in moves.items():      # exact grid places, whatever the last frame was
                b = R.box(self.rig, nid)
                b["x"], b["y"] = x, y
            for ei, (x, y, w, h) in boxes.items():
                ei.enc.update(x=x, y=y, w=w, h=h)
            self.animating = False
            self.view.setInteractive(True)
            self.changed()
            QTimer.singleShot(0, self._center)

        group.finished.connect(done)
        self._anim = group
        group.start()

    def clear_grid(self):
        if self.read_only or (not self.rig["nodes"] and not R.enclosures(self.rig) and not R.power_boxes(self.rig)) or self.animating:
            return
        self.rig = R.new_rig()
        self.changed()
        self.show_notice("Grid cleared. Undo (Ctrl+Z) brings it all back; nothing is saved until Save.")

    # -- toolbar and side panels
    def refresh_side(self):
        d = self.display.get(self.rig_name or "", {"chains": {}})
        st = R.rig_state(d["chains"])
        used = bool(d["chains"]) and self.rig_is_used(d)
        self.pill.setVisible(used)
        if used:
            n = len(d["chains"])
            self.pill.setText("Used for runs" + (f" · {n} radios, each its own path" if n > 1 else ""))
            self.pill.setStyleSheet(f"background: {T.rgba('survey', 0.16)}; color: {T.C['survey_light']};")
        bits = []
        if self.dirty:
            bits.append("unsaved changes")
        if st["last_used"]:
            bits.append(f"last ran {_when(st['last_used'])}")
        elif d["chains"]:
            bits.append("not run since it was last changed")
        if self.static_:
            bits.append("static: never asked")
        elif st["needs_confirm"]:
            bits.append("the next run asks \"still built as drawn?\"")
        self.state.setText(" · ".join(bits))
        self.chk_static.setChecked(self.static_)
        has_rig = self.rig_name is not None
        anything = bool(self.rig["nodes"] or R.enclosures(self.rig) or R.power_boxes(self.rig))

        def able(btn, ok, yes, no):              # a disabled control says why (relay 006)
            btn.setEnabled(ok)
            btn.setToolTip(yes if ok else no)

        able(self.btn_undo, bool(self.undo_stack), "Undo (Ctrl+Z)", "Nothing to undo yet")
        able(self.btn_redo, bool(self.redo_stack), "Redo (Ctrl+Y)", "Nothing to redo: undo something first")
        able(self.btn_tidy, bool(self.rig["nodes"]), "Arrange the rig: source on the left, branches one under "
             "another. Locked boxes stay; parts make way for a group. Undo puts it back.", "Nothing on the grid to tidy")
        able(self.btn_clear, anything and not self.read_only, "Take every part and wire off the grid. Undo brings "
             "them back; nothing is saved until Save.",
             "Read-only: nothing here can be cleared" if self.read_only else "The grid is already empty")
        ro = "Archived rigs are read-only: restore it first (Rig menu)"
        able(self.btn_save, has_rig and not self.read_only, "Write this rig to chain.json through the service",
             ro if self.read_only else "Open a rig, or make one with New rig, first")
        able(self.btn_rename, has_rig and not self.read_only, "Give this rig a new name",
             ro if self.read_only else "Open a rig, or make one with New rig, first")
        able(self.btn_picture, anything, "Save the drawing as a PNG picture, to a place you pick",
             "Nothing on the grid to draw yet")
        self.chk_static.setEnabled(has_rig)
        if not has_rig:
            no_use = "Open a rig, or make one with New rig, first"
        elif not d["chains"]:
            no_use = "Save this rig first: a rig that was never saved has no chain to run"
        else:
            no_use = "Save first: runs record the saved rig, and this one has unsaved changes"
        if self.read_only:
            no_use = "An archived rig cannot be used for runs: restore it first (Rig menu)"
        able(self.btn_use, bool(d["chains"]) and not self.dirty and not self.read_only,
             "Make this rig's chain the one every run records", no_use)
        if not has_rig:
            self.empty_hint.setText("No rig is open.\nClick New rig to start one, or pick a rig from the list above.")
        elif not anything:
            self.empty_hint.setText("This rig is empty.\nDrag a part from the list on the left onto the grid. "
                                    "To wire two parts, drag from a part's dot to the other part.")
        self.empty_hint.setVisible(not has_rig or not anything)
        self._place_empty_hint()
        if getattr(self, "_soft", False) and "power source's dot" not in self.notice.text():
            self.show_notice(None)
        self._fill_checks()
        self._fill_text()
        self.fill_output()
        if hasattr(self, "grid_status"):
            self.grid_status.refresh()

    CHECK_COLOUR = {"wiring": "anomaly", "radio": "survey", "power": "power", "loose": "muted"}

    def _fill_checks(self):
        """The Checks strip: every problem as a chip; a click selects what it is about."""
        while self.checks_row.count():
            w = self.checks_row.takeAt(0).widget()
            if w:
                w.hide()
                w.deleteLater()
        order = {"power": 0, "radio": 1, "wiring": 2, "loose": 3}
        self.check_list = sorted(R.checks(self.rig) if self.rig_name is not None else [],
                                 key=lambda c: order.get(c["kind"], 9))
        if not self.check_list:
            ok = QLabel("No problems found" if self.rig["nodes"] else "Nothing on the grid yet")
            ok.setProperty("muted", True)
            ok.setFont(T.ui_font(8.5))
            self.checks_row.addWidget(ok)
            self.checks_row.addStretch(1)
            self.checks_title.setText("Checks")
            return
        self.checks_title.setText(f"Checks · {len(self.check_list)}")
        shown = self.check_list[:4]
        for c in shown:
            b = QPushButton(c["text"])
            b.setObjectName("CheckChip")
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.setIcon(dot_icon(T.color(self.CHECK_COLOUR.get(c["kind"], "anomaly"))))
            b.setToolTip("Click to select " + ("it" if len(c["ids"]) == 1 else "them")
                         + ". A check warns; it never stops a save.")
            b.clicked.connect(lambda _=False, ids=c["ids"]: self.select_ids(ids))
            self.checks_row.addWidget(b)
        if len(self.check_list) > len(shown):
            more = QPushButton(f"+{len(self.check_list) - len(shown)} more")
            more.setObjectName("CheckChip")
            more.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            m = Menu(more)
            for c in self.check_list[len(shown):]:
                a = m.addAction(c["text"])
                a.triggered.connect(lambda _=False, ids=c["ids"]: self.select_ids(ids))
            more.setMenu(m)
            self.checks_row.addWidget(more)
        self.checks_row.addStretch(1)

    def select_ids(self, ids):
        self.scene.clearSelection()
        items = [self.box_item(i) for i in ids if self.box_item(i) is not None]
        for it in items:
            it.setSelected(True)
        if items:
            self.view.ensureVisible(items[0], 80, 80)

    def _fill_text(self):
        while self.text_grid.count():
            w = self.text_grid.takeAt(0).widget()
            if w:
                w.hide()
                w.deleteLater()
        if self.rig_name is None:
            return
        texts = R.rig_texts(self.rig_name, self.rig, self.labels)
        chains = R.rig_chains(self.rig_name, self.rig, self.labels)
        estimates = [R.estimate(self.rig, p) for p in R.paths(self.rig)]
        for row, (name, text) in enumerate(texts.items()):
            est = R.estimate_text(estimates[row] if row < len(estimates) else None)
            ant = next((n for n, _p in R.paths(self.rig)[row] if R.box(self.rig, n)["part"]["type"] == "antenna"),
                       None) if row < len(R.paths(self.rig)) else None
            gain = R.antenna(R.box(self.rig, ant)).get("gain_dbi") if ant else None
            if est:
                e = QLabel(f"{est}   ·   estimate, nominal"
                           + (f"   ·   antenna {gain:+g} dBi, not in the estimate" if gain not in (None, "") else ""))
                e.setFont(T.ui_font(8.5))
                e.setProperty("muted", True)
                e.setToolTip("An estimate, nominal: the dB figures typed on the parts and cables between the "
                             "source and the radio, added up. Cable loss depends on frequency; the figures are at "
                             "whatever frequency you typed them for. Never a measured level, and never drawn on "
                             "a spectrum or a report.")
                self.text_grid.addWidget(e, row, 2, Qt.AlignmentFlag.AlignTop)
            radio = R.radio_of(chains[name])
            role = self.role_of(radio) if radio else None
            sn = self.serial_of(radio) if radio else None
            who = (radio or {}).get("name") or "no radio"
            lab = QLabel(who + (f"\nSN {sn}" if sn else ""))
            lab.setFont(T.mono_font())
            lab.setStyleSheet(f"color: {T.C['recorder_light'] if role == 'recorder' else T.C['survey_light']};")
            val = QLabel(text + ("" if not self.dirty else "    (not saved yet)"))
            val.setFont(T.mono_font())
            val.setWordWrap(True)
            val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            val.setStyleSheet(f"color: {T.C['text2']};")
            self.text_grid.addWidget(lab, row, 0, Qt.AlignmentFlag.AlignTop)
            self.text_grid.addWidget(val, row, 1)
        self.text_grid.setColumnStretch(1, 1)
        if any(estimates):
            foot = QLabel("Estimates are nominal: the figures you typed on parts and cables, added up from the "
                          "source to the radio. Cable loss depends on frequency. Not a measured level, and not "
                          "part of the recorded text.")
            foot.setWordWrap(True)
            foot.setProperty("muted", True)
            foot.setFont(T.ui_font(8))
            self.text_grid.addWidget(foot, len(texts), 0, 1, 3)

    def show_notice(self, text: str | None, soft: bool = False):
        self._soft = soft and bool(text)
        self.notice.setText(text or "")
        self.notice.setVisible(bool(text))

    def _toggle_output(self, on: bool):
        self.output.setVisible(on)
        prefs().setValue("panels/output", on)
        if on:
            self.fill_output()

    def _filter_parts(self, text: str):
        for lst in (self.parts, self.cables):
            lst.filter(text)

    # -- the Output panel (relay 007)
    def fill_output(self):
        if not hasattr(self, "output") or not self.output.isVisible():
            return
        try:
            items = self.scene.selectedItems()
        except RuntimeError:
            return
        try:
            parts = [i for i in items if isinstance(i, PartItem)]
            wires = [i for i in items if isinstance(i, WireItem)]
            others = [i for i in items if isinstance(i, (EnclosureItem, PowerItem))]
            [i.isSelected() for i in items]
        except RuntimeError:
            return
        paths = R.paths(self.rig)
        if len(parts) == 1 and not wires and not others:
            node = parts[0].node
            secs = [("Part", self._part_rows(node))]
            if node["part"]["type"] == "antenna":
                lines = R.antenna_lines(node)
                secs.append(("Antenna", lines or [("Type", "not set: choose it on the part's card")]))
            on = [p_ for p_ in paths if any(nid == node["id"] for nid, _ in p_)]
            for k, p_ in enumerate(on[:3]):
                secs.append((f"Path {k + 1} of {len(on)}" if len(on) > 1 else "Path", self._path_rows(p_)))
            self.output.show_sections(R._pname(node), secs)
        elif len(wires) == 1 and not parts and not others:
            w = wires[0].wire
            on = [p_ for p_ in paths if any(nid == w["from"] and port == w["port"] for nid, port in p_)]
            secs = [("Path" if len(on) == 1 else f"Path {k + 1} of {len(on)}", self._path_rows(p_))
                    for k, p_ in enumerate(on[:3])]
            self.output.show_sections("a mating" if R.mated(w) else "a wire", secs or [("Path", [("Paths", "none")])])
        elif len(others) == 1 and not parts and not wires:
            it = others[0]
            if isinstance(it, PowerItem):
                names = {n["id"]: R._pname(n) for n in self.rig["nodes"]}
                feeds = [names.get(w["to"], "?") for w in R.power_wires(self.rig) if w["from"] == it.box["id"]]
                rows = [("Kind", R.POWER_KINDS.get(it.box["kind"], it.box["kind"])), ("Name", it.box.get("name") or ""),
                        ("Feeds", ", ".join(feeds) or "nothing yet"), ("Shape", R.SHAPES.get(it.shape_name(), "")),
                        ("Recorded text", "never: drawing only")]
                self.output.show_sections(it.box.get("name") or "power source", [("Power source", rows)])
            else:
                inside = [R._pname(R.box(self.rig, b)) for b in G.inside_any(self.rig, it.enc)
                          if R.kind_of(self.rig, b) == "part"]
                rows = [("Name", it.enc.get("name") or ""), ("Holds", ", ".join(inside) or "nothing"),
                        ("Size", f"{it.enc['w']} × {it.enc['h']} px"), ("Recorded text", "never: drawing only")]
                self.output.show_sections(it.enc.get("name") or "enclosure", [("Enclosure", rows)])
        else:
            what = f"{len(items)} selected" if items else (self.rig_name or "no rig open")
            self.output.show_sections(what, [("Rig", self._rig_rows(paths))])

    def _part_rows(self, node: dict) -> list:
        p = node["part"]
        rows = [("Kind", self.labels.get(p["type"], {}).get("label", p["type"])), ("Name", p.get("name") or ""),
                ("Note", p.get("detail") or "—")]
        if p.get("length"):
            rows.append(("Length", p["length"]))
        if p["type"] == "attenuator" and p.get("db") not in (None, ""):
            rows.append(("Attenuation", f"{p['db']} dB"))
        if p["type"] == "sdr":
            rows.append(("Receiver", p.get("rx") or "not chosen"))
            rows.append(("Serial", self.serial_of(p) or "not known"))
        kind = R.adapter_kind_text(node)
        if kind:
            rows.append(("Adapter kind", f"{kind} (in the recorded text)"))
        ports = []
        for (nid, k, port), (x, y, side) in sorted(self.anchor_map.items()):
            if nid != node["id"]:
                continue
            g = R.port_gender(node, k, port)
            ports.append(f"{'in' if k == 'in' else 'out ' + str(port + 1) if R.out_ports(node) > 1 else 'out'}"
                         f" {side}" + (f", {R.GENDER_WORD[g]}" if g else ""))
        rows.append(("Ports", "; ".join(ports) or "none"))
        if R.needs_power(node):
            feed = R.power_feed(self.rig, node["id"])
            src = R.box(self.rig, feed["from"]) if feed else None
            rows.append(("Power", f"fed by {src.get('name') or 'a power source'}" if src else "needs power: not fed"))
        else:
            rows.append(("Power", "does not need power"))
        g = R.figure(node, p)
        rows.append(("Nominal dB", "not given" if g is None else f"{g:+g} dB (estimate only)"))
        label = {"pipe": "Pipe"}.get(R.shape_of(node), R.SHAPES.get(R.shape_of(node), R.shape_of(node)))
        rows.append(("Shape", label + ("" if node.get("shape") else ", its kind's")))
        return rows

    def _path_rows(self, path) -> list:
        rep_ = R.path_report(self.rig, path, self.labels)
        e = rep_["estimate"]
        est = "no radio on this path" if e is None else R.estimate_text(e).replace("est.", "est., nominal:", 1)
        rows = [("Parts", " › ".join(rep_["parts"])), ("Recorded text", rep_["text"]),
                ("Cables", f"{rep_['cables']}, {rep_['cable_m']:g} m in all"
                           + (f" (length not read on: {', '.join(rep_['unread_lengths'])})" if rep_["unread_lengths"] else "")),
                ("Connections", f"{rep_['joins']} joins, {rep_['connectors']} connectors"
                                + (f" ({rep_['mated']} mated directly)" if rep_["mated"] else "")),
                ("Genders", "; ".join(rep_["genders"]) or "no mated pair has both genders set"),
                ("Power", "; ".join(f"{n}: {'fed' if ok else 'NOT fed'}" for n, ok in rep_["power"]) or "nothing needs power"),
                ("Estimate", est)]
        if rep_["antenna_gain"] not in (None, ""):
            rows.append(("Antenna gain", f"{rep_['antenna_gain']:g} dBi (not in the estimate)"))
        return rows

    def _rig_rows(self, paths) -> list:
        d = self.display.get(self.rig_name or "", {"chains": {}})
        st = R.rig_state(d.get("chains") or {})
        radios = [f"{R._pname(n)}" + (f" (SN {self.serial_of(n['part'])})" if self.serial_of(n["part"]) else "")
                  for n in self.rig["nodes"] if n["part"]["type"] == "sdr"]
        checks = R.checks(self.rig)
        rows = [("Rig", self.rig_name or "none open"), ("Radios", ", ".join(radios) or "none"),
                ("Paths", str(len(paths))), ("Open checks", str(len(checks)) + (": " + "; ".join(c["text"] for c in checks[:5])
                                                                                if checks else ""))]
        for name, text in R.rig_texts(self.rig_name or "rig", self.rig, self.labels).items():
            rows.append((name, text))
        rows += [("Last ran", _when(st["last_used"]) or "not since last changed"),
                 ("Static", "yes: never asked before a run" if self.static_ else "no"),
                 ("Used for runs", "yes" if d.get("chains") and self.rig_is_used(d) else "no"),
                 ("Archived", "yes (read-only)" if self.read_only else "no"),
                 ("Saved versions", str(len(d.get("history") or [])))]
        return rows

    def _toggle_text(self, on: bool):
        self.text_panel.setVisible(on)

    def _static_clicked(self, on: bool):
        self.static_ = on
        self.changed(redraw=False)

    # -- rigs
    def _ask_unsaved(self) -> bool:
        """True when it is fine to leave the current drawing."""
        if not self.dirty:
            return True
        b = QMessageBox.question(self, "Unsaved changes",
                                 f"Save the changes to “{self.rig_name}” first?",
                                 QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
                                 | QMessageBox.StandardButton.Cancel)
        if b == QMessageBox.StandardButton.Save:
            return self.save()
        return b == QMessageBox.StandardButton.Discard

    def _picked(self, i: int):
        name = self.pick.itemData(i) or self.pick.itemText(i)
        if name == self.rig_name:
            return
        if not self._ask_unsaved():
            self._show_in_pick(self.rig_name)
            return
        if name in self.display:
            self.open_rig(name)

    def _taken(self, name: str) -> bool:
        return name in self.display or name in (self.summary.get("chains") or {})

    def new_rig(self, name: str | None = None, template: str | None = None):
        """New rig: a name and a starting drawing - blank, or one of the templates (relay 007).
        Nothing is saved until Save."""
        if not self._ask_unsaved():
            return
        if name is None:
            dlg = QDialog(self)
            dlg.setWindowTitle("New rig")
            lay = QVBoxLayout(dlg)
            lay.setContentsMargins(T.SPACE * 2, T.SPACE * 2, T.SPACE * 2, T.SPACE * 2)
            lay.setSpacing(T.SPACE)
            lab = QLabel("Name of the new rig")
            lab.setProperty("muted", True)
            ed = QLineEdit()
            lay.addWidget(lab)
            lay.addWidget(ed)
            lab2 = QLabel("Start from")
            lab2.setProperty("muted", True)
            lay.addWidget(lab2)
            pick = QListWidget()
            pick.setObjectName("Templates")
            for key, label in R.TEMPLATES.items():
                it = QListWidgetItem(label)
                it.setData(Qt.ItemDataRole.UserRole, key)
                pick.addItem(it)
            pick.setCurrentRow(0)
            pick.setFixedHeight(4 * 36)
            lay.addWidget(pick)
            row = QHBoxLayout()
            ok, cancel = QPushButton("Make rig"), QPushButton("Cancel")
            ok.setDefault(True)
            ok.clicked.connect(dlg.accept)
            cancel.clicked.connect(dlg.reject)
            row.addStretch(1)
            row.addWidget(cancel)
            row.addWidget(ok)
            lay.addLayout(row)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            name = ed.text()
            template = pick.currentItem().data(Qt.ItemDataRole.UserRole) if pick.currentItem() else "blank"
        name = (name or "").strip()
        if not name:
            return
        if self._taken(name):
            self.show_notice(f"There is already a rig or chain called “{name}”.")
            return
        rig = R.template(template or "blank", self.labels, self.receivers)
        self.rig_name, self.rig, self.static_, self.renamed_from, self.read_only = name, rig, False, None, False
        self.display[name] = {"rig": R.new_rig(), "chains": {}, "stale_drawing": False, "archived": False, "history": []}
        self._show_in_pick(name)
        self.dirty = True
        self._reset_history(saved=False)
        self.redraw()
        QTimer.singleShot(0, self._center)
        self.show_notice("A new rig" + ("" if (template or "blank") == "blank" else
                                         f", from “{R.TEMPLATES[template]}”") +
                         ". Drag parts onto the grid, wire them, then Save.")

    def rename_rig(self):
        if self.rig_name is None:
            return
        name, ok = QInputDialog.getText(self, "Rename rig", "New name:", text=self.rig_name)
        name = (name or "").strip()
        if not ok or not name or name == self.rig_name:
            return
        if self._taken(name):
            self.show_notice(f"There is already a rig or chain called “{name}”.")
            return
        if not self.display.get(self.rig_name, {}).get("chains"):     # never saved: just a new name
            self.display[name] = self.display.pop(self.rig_name)
            self.rig_name = name
            i = self.pick.currentIndex()
            self.pick.setItemText(i, name)
            return
        self.renamed_from = self.renamed_from or self.rig_name
        old = self.rig_name
        self.rig_name = name
        if not self.save(renamed_from=self.renamed_from):
            self.rig_name = old

    def save(self, renamed_from: str | None = None) -> bool:
        if self.rig_name is None:
            return False
        if not self.rig["nodes"]:
            self.show_notice("An empty rig cannot be saved: drag at least one part onto the grid.")
            return False
        try:
            doc, lay = R.apply_rig(self.chain_doc(), self.layout_doc, self.rig_name, self.rig,
                                   static=self.static_, renamed_from=renamed_from or self.renamed_from,
                                   labels=self.labels)
            self.svc.save(doc, lay)
        except (ValueError, ServiceError) as exc:
            self.show_notice(f"Not saved: {exc}")
            return False
        self.dirty = False
        self.reload(keep=self.rig_name)
        self.status.emit(f"saved “{self.rig_name}”")
        return True

    def use_for_runs(self):
        """The whole rig becomes current. A split rig's runs then each record the path
        that ends at the radio they use (matched by serial in the engine)."""
        d = self.display.get(self.rig_name or "")
        if not d or not d["chains"] or self.dirty:
            return
        names = list(d["chains"])
        st = R.rig_state(d["chains"])
        warn = R.checks(self.rig)
        if warn:
            listed = "\n".join(f"  •  {c['text']}" for c in warn[:8]) + (
                f"\n  …and {len(warn) - 8} more" if len(warn) > 8 else "")
            b = QMessageBox.question(self, "Open checks",
                                     f"“{self.rig_name}” has {len(warn)} open check{'s' if len(warn) != 1 else ''}:"
                                     f"\n\n{listed}\n\nUse it for runs anyway?")
            if b != QMessageBox.StandardButton.Yes:
                return
        if st["needs_confirm"]:
            b = QMessageBox.question(self, "Still built as drawn?",
                                     f"“{self.rig_name}” last ran {_when(st['last_used'])}. "
                                     "Is the rig still built exactly as drawn?")
            if b != QMessageBox.StandardButton.Yes:
                self.show_notice("Then change the drawing to match the rig and Save it before using it for runs.")
                return
        try:
            if len(names) == 1:
                self.svc.use(names[0])
            else:
                self.svc.use_rig(names)
        except ServiceError as exc:
            self.show_notice(f"Not set for runs: {exc}")
            return
        self.reload(keep=self.rig_name)
        self.status.emit(f"runs now record “{self.rig_name}”"
                         + (", each radio its own path" if len(names) > 1 else ""))

    def rig_is_used(self, d: dict) -> bool:
        """Is this rig what runs record? A split rig: its path texts are exactly the
        engine's per-radio texts. One path: it is chain.json's current, as before."""
        by = self.summary.get("runs_record")
        if by:
            texts = {engine_chain.chain_text(c["parts"]) for c in d["chains"].values()}
            return len(d["chains"]) > 1 and texts == set(by.values())
        return self.summary.get("current") in d["chains"]

    # -- the owner's parts library (relay 005)
    def _store_library(self, entries: list) -> bool:
        """Write My parts into chain_layout.json through the service. The drawings and chain.json
        are read fresh first and written back unchanged, so an unsaved rig is not saved and a
        change the old web page made meanwhile is not overwritten."""
        try:
            got = self.svc.chains()
            summ = got.get("chain") or {}
            chains = {n: {k: v for k, v in c.items() if k not in DERIVED} for n, c in (summ.get("chains") or {}).items()}
            doc = {"schema": 1, "current": summ.get("current"), "chains": chains}
            lay = R.check_layout(got.get("layout") or {})
            lay["library"] = R.check_library(copy.deepcopy(entries))
            if not lay["library"]:
                lay.pop("library")
            self.svc.save(doc, lay)
        except (ValueError, ServiceError) as exc:
            self.show_notice(f"My parts not saved: {exc}")
            return False
        self.library = copy.deepcopy(entries)
        if entries:
            self.layout_doc["library"] = copy.deepcopy(entries)
        else:
            self.layout_doc.pop("library", None)
        self.parts.set_library(self.library)
        return True

    def save_to_library(self, node: dict):
        name, ok = QInputDialog.getText(self, "Save to my parts", "Name in My parts (and on the part when dropped):",
                                        text=node["part"].get("name") or "")
        if not ok:
            return
        entry = R.lib_entry(node, [e["id"] for e in self.library])
        if name.strip():
            entry["part"]["name"] = name.strip()
        if self._store_library(self.library + [entry]):
            self.status.emit(f"“{entry['part'].get('name')}” is in My parts")

    def library_menu(self, entry: dict | None, where: QPoint):
        m = Menu(self)
        if entry is not None:
            a = m.addAction("Rename…")
            a.triggered.connect(lambda: self._lib_rename(entry["id"]))
            a = m.addAction("Edit…")
            a.triggered.connect(lambda: self._lib_edit(entry["id"]))
            m.addSeparator()
            a = m.addAction("Remove from list")
            a.triggered.connect(lambda: self._lib_remove(entry["id"]))
        removed = [e for e in self.library if e.get("removed")]
        if removed:
            sub = Menu(m)
            sub.setTitle("Put back a removed part")
            for e in removed:
                a = sub.addAction(e["part"].get("name") or e["part"]["type"])
                a.triggered.connect(lambda _=False, i=e["id"]: self._lib_put_back(i))
            m.addMenu(sub)
        if m.actions():
            m.exec(where)

    def _lib_change(self, lid: str, fn) -> bool:
        entries = copy.deepcopy(self.library)
        e = next((x for x in entries if x["id"] == lid), None)
        if e is None:
            return False
        fn(e)
        return self._store_library(entries)

    def _lib_rename(self, lid: str):
        e = next((x for x in self.library if x["id"] == lid), None)
        if e is None:
            return
        name, ok = QInputDialog.getText(self, "Rename", "Name:", text=e["part"].get("name") or "")
        if ok and name.strip():
            self._lib_change(lid, lambda x: x["part"].__setitem__("name", name.strip()))

    def _lib_edit(self, lid: str):
        e = next((x for x in self.library if x["id"] == lid), None)
        if e is None:
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Edit my part")
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(T.SPACE * 2, T.SPACE * 2, T.SPACE * 2, T.SPACE * 2)
        lay.setSpacing(T.SPACE)
        fields = {}
        for key, label in (("name", "Name"), ("detail", "Note"), ("length", "Length")):
            if key == "length" and e["part"]["type"] not in LENGTH_TYPES:
                continue
            lab = QLabel(label)
            lab.setProperty("muted", True)
            ed = QLineEdit(str(e["part"].get(key) or ""))
            lay.addWidget(lab)
            lay.addWidget(ed)
            fields[key] = ed
        gain = {"v": e.get("gain_db")}
        lab = QLabel("Nominal gain or loss, dB")
        lab.setProperty("muted", True)
        lay.addWidget(lab)
        lay.addWidget(EditCard.number_edit(e.get("gain_db"), lambda v: gain.__setitem__("v", v), "blank if not known"))
        pw = QCheckBox("Needs power")
        pw.setChecked(R.needs_power({"part": e["part"], **({"needs_power": e["needs_power"]}
                                                           if "needs_power" in e else {})}))
        lay.addWidget(pw)
        note = QLabel("Parts already dropped into rigs keep their own copy: only new drops change.")
        note.setWordWrap(True)
        note.setProperty("muted", True)
        lay.addWidget(note)
        row = QHBoxLayout()
        ok, cancel = QPushButton("Save"), QPushButton("Cancel")
        ok.clicked.connect(dlg.accept)
        cancel.clicked.connect(dlg.reject)
        row.addStretch(1)
        row.addWidget(cancel)
        row.addWidget(ok)
        lay.addLayout(row)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        def apply(x):
            for key, ed in fields.items():
                v = ed.text().strip()
                if key == "length" and not v:
                    x["part"].pop("length", None)
                else:
                    x["part"][key] = v
            if gain["v"] is None:
                x.pop("gain_db", None)
            else:
                x["gain_db"] = gain["v"]
            if pw.isChecked() == (x["part"]["type"] in R.NEEDS_POWER_BY_TYPE):
                x.pop("needs_power", None)
            else:
                x["needs_power"] = pw.isChecked()

        self._lib_change(lid, apply)

    def _lib_remove(self, lid: str):
        """Takes the entry off the list. It is kept, marked removed, so "Put back" can restore
        it (rule 4: nothing saved is deleted). Rigs that used it keep their parts."""
        e = next((x for x in self.library if x["id"] == lid), None)
        if e is None:
            return
        b = QMessageBox.question(self, "Remove from My parts",
                                 f"Take “{e['part'].get('name') or e['part']['type']}” off the My parts list?\n\n"
                                 "Rigs that used it keep their parts. Right-click the MY PARTS heading to put it back.")
        if b == QMessageBox.StandardButton.Yes:
            self._lib_change(lid, lambda x: x.__setitem__("removed", True))

    def _lib_put_back(self, lid: str):
        self._lib_change(lid, lambda x: x.pop("removed", None))

    # -- picture and help (relay 005)
    def export_picture(self, path: str | None = None) -> str | None:
        """Save the drawing as a PNG, at twice screen size, only when asked and only where the
        owner picks (path given: the check script). Returns the path written."""
        if not (self.rig["nodes"] or R.enclosures(self.rig) or R.power_boxes(self.rig)):
            self.status.emit("nothing to draw yet")
            return None
        if path is None:
            start = str(Path.home() / f"{(self.rig_name or 'rig').replace('/', '-')}.png")
            path, _ = QFileDialog.getSaveFileName(self, "Save the drawing as a picture", start, "PNG picture (*.png)")
            if not path:
                return None
            if not path.lower().endswith(".png"):
                path += ".png"
        sel = list(self.scene.selectedItems())
        self.scene.clearSelection()
        self._show_guides([])
        src = QRectF()
        for it in self.all_box_items() + self.wire_items + self.power_wire_items:
            src = src.united(it.sceneBoundingRect())
        src = src.adjusted(-40, -40, 40, 40)
        scale = 2
        img = QImage(int(src.width() * scale), int(src.height() * scale), QImage.Format.Format_ARGB32)
        img.fill(T.color("plot"))
        qp = QPainter(img)
        qp.setRenderHint(QPainter.RenderHint.Antialiasing)
        qp.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        self.scene.render(qp, QRectF(0, 0, img.width(), img.height()), src)
        qp.end()
        for it in sel:
            it.setSelected(True)
        if not img.save(path, "PNG"):
            self.show_notice(f"The picture could not be saved to {path}.")
            return None
        self.status.emit(f"picture saved: {path}")
        return path

    def show_help(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Keyboard shortcuts")
        dlg.resize(700, 680)
        outer = QVBoxLayout(dlg)
        outer.setContentsMargins(T.SPACE * 3, T.SPACE * 3, T.SPACE * 3, T.SPACE * 2)
        head = QLabel("Keyboard shortcuts")
        head.setFont(T.ui_font(12, QFont.Weight.DemiBold))
        outer.addWidget(head)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        grid = QGridLayout(body)
        grid.setHorizontalSpacing(T.SPACE * 3)
        grid.setVerticalSpacing(T.SPACE // 2 + 2)
        top = int(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        row, last = 0, None
        for group, keys, what, _how in SHORTCUTS:
            if group != last:
                g = QLabel(group.upper())
                g.setProperty("muted", True)
                g.setFont(T.ui_font(7.5, QFont.Weight.DemiBold))
                g.setContentsMargins(0, T.SPACE * 2 if last else 0, 0, 2)
                grid.addWidget(g, row, 0, 1, 2)
                row += 1
                last = group
            k = QLabel(keys.replace("   ·   ", "\n"))
            k.setFont(T.mono_font(9))
            k.setStyleSheet(f"color: {T.C['survey_light']};")
            w = QLabel(what)
            w.setWordWrap(True)
            w.setAlignment(Qt.AlignmentFlag(top))          # on the label, so it still takes the column's width
            grid.addWidget(k, row, 0, Qt.AlignmentFlag(top))
            grid.addWidget(w, row, 1)
            row += 1
        grid.setColumnMinimumWidth(0, 210)
        grid.setColumnStretch(1, 1)
        grid.setRowStretch(row, 1)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        close = QPushButton("Close")
        close.clicked.connect(dlg.accept)
        r = QHBoxLayout()
        r.addStretch(1)
        r.addWidget(close)
        outer.addLayout(r)
        self._help = dlg
        dlg.exec()

    def can_close(self) -> bool:
        return self._ask_unsaved()

    def debug_state(self) -> str:
        return json.dumps({"rig": self.rig_name, "dirty": self.dirty, "rig_doc": self.rig})
