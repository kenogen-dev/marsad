"""The Chain screen: a rig drawn on a grid.

Drag a part from the list onto the grid. To wire, drag from a part's dot, or from
one of the rings that appear round a part when the pointer is over it, onto another
part; a wire leaves from whichever side faces the part it goes to. Cables (Coax and
USB, each with a length; relay 017) live on the wires: drop one from the Cables list onto a wire,
or click a wire to give it cables; chokes drop onto cables and slide along them. Drag a part's
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
                           QLinearGradient,
                           QImage, QKeySequence, QTextOption, QTransform,
                           QPainter, QPainterPath, QPainterPathStroker, QPen, QPixmap, QShortcut)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QColorDialog, QComboBox, QDialog,
                               QSizePolicy, QToolButton, QWidgetAction,
                               QFileDialog, QFrame,
                               QGraphicsEllipseItem, QGraphicsItem, QGraphicsLineItem,
                               QGraphicsObject, QGraphicsPathItem, QGraphicsRectItem, QGraphicsScene, QGraphicsView,
                               QGridLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMenu, QMessageBox, QPlainTextEdit, QPushButton,
                               QScrollArea,
                               QSpinBox, QSplitter, QStyle, QStyledItemDelegate, QVBoxLayout, QWidget)

from marsad import chain as engine_chain

from . import explain as E
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
                 "coax", "filter", "lna", R.SPLITTER, "sdr", "usb", "bulkhead_usb",
                 "isolator", R.HUB, "pc", "custom"]
LENGTH_TYPES = {"coax", "jumper", "usb"}       # "jumper": only on parts saved before relay 017, never offered
# Colour coding: what each kind of part does. Custom parts stay neutral. Relay 006: each section
# of the Parts list has a hue of its own (SMA parts were sharing the cables' blue before).
CATEGORY = {"antenna": "source", "probe": "source", "load": "source",
            "adapter": "sma", "dcblock": "sma", "attenuator": "sma", "bulkhead_sma": "sma",
            "coax": "cable", "jumper": "cable",
            "filter": "condition", "lna": "condition",
            R.SPLITTER: "split", "sdr": "radio", R.ENCLOSURE: "enclosure",
            "usb": "digital", "bulkhead_usb": "digital", "isolator": "digital", "pc": "digital",
            R.HUB: "digital"}
CATEGORY_NAMES = [("source", "Sources"), ("sma", "SMA adapters, blocks, pads, bulkheads"),
                  ("condition", "Filters and amplifiers"), ("split", "Splitters"),
                  ("radio", "Radios"), ("digital", "USB parts and USB cables"),
                  ("enclosure", "Enclosures (drawing only)")]
PORT_HIT = 11                  # px (scene), how near a connection point a press must be to start a wire
BEND_HIT = 8                   # px (scene), how near a bend point or a wire's end handle a press must be (relay 015)
BEAD_HIT = 9                   # px (scene), how near a choke's bead a press must be (relay 017)
WIRE_END_AT = 22               # px along a selected wire from each end, where its end handle sits (relay 015)
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
    ("Rigs", "Ctrl+N", "New rig", "window"),
    ("Rigs", "Ctrl+O", "Open rig… (the rig used for runs is marked)", "window"),
    ("Rigs", "Ctrl+S", "Save", "window"),
    ("Rigs", "Ctrl+F", "Find a part by name or kind; Enter for the next, Esc to close", "window"),
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
    ("Arranging", "Direction button", "Build left → right or top → bottom; Tidy then lays the rig out that way",
     "menu"),
    ("Parts", "Click a part", "Open its card: name, note, fields, colour, shape, text position", "mouse"),
    ("Parts", "Drag a part", "Move it (a drag opens nothing)", "mouse"),
    ("Resizing", "Drag a handle", "Resize a selected part, enclosure or power source", "mouse"),
    ("Resizing", "Shift + drag a handle", "Keep the proportions", "mouse"),
    ("Resizing", "Alt + drag a handle", "Resize from the centre", "mouse"),
    ("Resizing", "Shift+Alt + drag a handle", "Both: proportions kept, from the centre", "mouse"),
    ("Resizing", "Ctrl + drag a handle", "Ignore the grid while held, for fine sizing", "mouse"),
    ("Parts", "Right-click a part", "The format and arrange menu: text position, shape, colour, stacking, align, "
                                    "lock, groups, copy and paste", "mouse"),
    ("Wiring", "Drag from a dot", "Wire it to the part you let go over (from an input dot too)", "mouse"),
    ("Wiring", "Drop an SMA part on a dot", "Mate it directly, port to port, with no wire", "mouse"),
    ("Wiring", "Drag a selected wire's end handle", "Reconnect that end to another part", "mouse"),
    ("Wiring", "Click a wire", "Select it (and open its card: its cables)", "mouse"),
    ("Wiring", "Drag a wire", "Move it: the stretch under the pointer moves, re-routed, ends kept on their parts",
     "mouse"),
    ("Wiring", "Ctrl+click a wire", "Add a bend point there (on the grid when snap is on)", "mouse"),
    ("Wiring", "Drag a bend point", "Move it", "mouse"),
    ("Wiring", "Click a bend point, then Delete", "Remove it (the wire's menu: Remove point, Straighten)", "mouse"),
    ("Cables", "Drop Coax or USB on a wire", "The wire carries that cable; give it a length on its card", "mouse"),
    ("Cables", "Drop a Choke on a cable", "A ferrite bead on that cable, where it was dropped", "mouse"),
    ("Cables", "Drag a choke", "Slide it along its cable (right-click it to remove it)", "mouse"),
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
    if cat in ("coax", "jumper", "usb", R.CHOKE):
        return cable_colour(cat)
    return category_colour(cat)


def section_colour(cat: str | None, on: bool = True) -> QColor:
    """A Parts-list section's colour (relay 018); My parts takes the accent."""
    if on and cat == "mine":
        return T.color("survey")
    return palette_colour(cat, on)


def cable_summary(c: dict) -> str:
    """A cable's label on its wire: what the recorded text calls it (a new cable's name holds its length)."""
    if c.get("auto"):
        return R.cable_words(c)[0]
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
    return routed_path(G.route(a, b, style, side_b), a, b)


def routed_path(r: tuple, a: tuple, b: tuple) -> QPainterPath:
    """Any routed wire as a path: a curve, right angles or a straight line, or (relay 015) a smooth curve
    through bend points."""
    if r[0] == "spline":
        path = QPainterPath(QPointF(*r[1][0][0]))
        for _p0, c1, c2, p1 in r[1]:
            path.cubicTo(QPointF(*c1), QPointF(*c2), QPointF(*p1))
        return path
    if r[0] == "cubic":
        path = QPainterPath(QPointF(a[0], a[1]))
        path.cubicTo(QPointF(*r[1]), QPointF(*r[2]), QPointF(b[0], b[1]))
        return path
    return route_path(r)


def _when(stamp: str | None) -> str:
    return stamp.replace("T", " ")[:16] if stamp else ""


def _esc(text) -> str:
    """Plain text for a rich-text label (relay 018's reports)."""
    import html
    return html.escape(str(text))


def _now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.now().isoformat(timespec="seconds")


def prefs() -> QSettings:
    """The window's own preferences (grid look, panel sizes), in one small file in
    the user's home folder, never in the registry and never in the station. MARSAD_DESKTOP_INI points it elsewhere
    (relay 018: the window checks use a file of their own, so they never change the owner's)."""
    import os
    return QSettings(os.environ.get("MARSAD_DESKTOP_INI") or str(Path.home() / ".marsad_desktop.ini"),
                     QSettings.Format.IniFormat)


def category_colour(cat: str | None) -> QColor:
    if cat == "radio":
        return T.color("survey")
    return T.color("cat_" + cat) if cat else T.color("muted")


RESIZE_HINT = "resizing: Shift keeps proportions · Alt from the centre · Shift+Alt both · Ctrl ignores the grid"


def resize_keys(screen, mods) -> dict:
    """Relay 015: what the held keys ask of a resize, as in drawing programs. Shift keeps the proportions,
    Alt resizes about the centre, Ctrl ignores the grid while held."""
    return {"keep_ratio": bool(mods & Qt.KeyboardModifier.ShiftModifier),
            "from_centre": bool(mods & Qt.KeyboardModifier.AltModifier),
            "snap": None if (mods & Qt.KeyboardModifier.ControlModifier) or not screen.snap_on else R.GRID}


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

    def open_at(self, global_pos: QPoint):
        """Relay 016: a right-click menu opens with its top-left corner a few pixels below and right of the
        pointer, never anchored to what was clicked, and flips to the pointer's other side only where it would run
        off the window. Every context menu in the window opens this way."""
        win = self.parentWidget().window() if self.parentWidget() is not None else None
        area = win.frameGeometry() if win is not None else None
        scr = QApplication.screenAt(global_pos) or QApplication.primaryScreen()
        avail = scr.availableGeometry() if scr is not None else None
        if area is None:
            area = avail
        elif avail is not None:
            area = area.intersected(avail) if area.intersects(avail) else avail
        size = self.sizeHint()
        x, y = G.menu_place(global_pos.x(), global_pos.y(), size.width(), size.height(),
                            (area.left(), area.top(), area.right(), area.bottom()) if area is not None else None)
        return self.exec(QPoint(x, y))

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
        body = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        qp.setPen(QPen(T.color("line_strong"), T.BORDER))
        qp.setBrush(T.color("menu", T.MENU_ALPHA))
        qp.drawRoundedRect(body, 6, 6)
        qp.setPen(QPen(T.color("highlight", T.HIGHLIGHT_ALPHA["overlay"]), 1))   # relay 016: the overlay's top edge
        qp.drawLine(QPointF(body.left() + 6, body.top() + 1), QPointF(body.right() - 6, body.top() + 1))
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
                qp.setPen(T.color("menu_muted"))
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
                qp.setPen(T.color("menu_muted") if on else T.color("faint"))
                qp.drawText(r.adjusted(0, 0, -pad, 0),
                            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight), keys)
            if a.isCheckable() and a.isChecked():
                qp.setPen(QPen(T.color("survey"), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                cy = r.center().y()
                x0 = r.left() + T.SPACE + 5
                qp.drawPolyline([QPointF(x0, cy), QPointF(x0 + 4, cy + 4), QPointF(x0 + 11, cy - 4)])
            if a.menu() is not None:
                qp.setPen(T.color("menu_muted"))
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


class NoteEdit(QPlainTextEdit):
    """Relay 016: a card's note as a box that wraps and grows with its text (two to six lines, then it scrolls).
    One paragraph only: a part's note can reach the recorded text, which is one line per run, so Enter adds no line
    break and pasted line breaks become spaces."""
    edited = Signal(str)

    def __init__(self, text: str = "", placeholder: str = ""):
        super().__init__()
        self.setPlainText(" ".join(str(text or "").split("\n")))
        self.setPlaceholderText(placeholder)
        self.setTabChangesFocus(True)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.textChanged.connect(self._changed)
        self._fit()

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            ev.accept()
            return
        super().keyPressEvent(ev)

    def insertFromMimeData(self, source):
        self.insertPlainText(" ".join((source.text() or "").split()))

    def text(self) -> str:
        return self.toPlainText()

    def _changed(self):
        self._fit()
        self.edited.emit(self.toPlainText())

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._fit()

    def _fit(self):
        lines = max(1, int(self.document().size().height()))
        shown = min(max(lines, 2), 6)
        m = self.contentsMargins()
        h = self.fontMetrics().lineSpacing() * shown + m.top() + m.bottom() + 2 * int(self.document().documentMargin()) + 4
        if self.height() != h:
            self.setFixedHeight(h)


_WRAP: dict = {}
WORDS_KIND, WORDS_NAME, WORDS_NOTE = 18, 18, 14     # px: a part's kind line, one line of its name, one of its note


def wrap_lines(font: QFont, text: str, width: float, max_lines: int) -> tuple[list[str], bool]:
    """Relay 016: text broken into lines that fit `width`, at most `max_lines`; a word too long for a line is broken
    within it. When it does not all fit, the last line ends with "…" and the second value is True, so the caller
    shows the whole text on hover. Cached: painting asks for the same words over and over."""
    key = (font.key(), text, round(width), max_lines)
    hit = _WRAP.get(key)
    if hit is not None:
        return hit
    fm = QFontMetricsF(font)
    lines: list[str] = []
    cur = ""
    for word in str(text).split():
        trial = word if not cur else cur + " " + word
        if fm.horizontalAdvance(trial) <= width:
            cur = trial
            continue
        if cur:
            lines.append(cur)
        while fm.horizontalAdvance(word) > width and len(word) > 1:     # a word wider than the line: break it
            k = len(word)
            while k > 1 and fm.horizontalAdvance(word[:k]) > width:
                k -= 1
            lines.append(word[:k])
            word = word[k:]
        cur = word
    if cur:
        lines.append(cur)
    cut = len(lines) > max(max_lines, 1)
    if cut:
        rest = " ".join(lines[max_lines - 1:])
        lines = lines[:max_lines - 1] + [fm.elidedText(rest + " …", Qt.TextElideMode.ElideRight, int(width))]
        if not lines[-1].endswith("…"):
            lines[-1] = fm.elidedText(lines[-1] + "…", Qt.TextElideMode.ElideRight, int(width))
    out = (lines or [""], cut)
    if len(_WRAP) > 8192:
        _WRAP.clear()
    _WRAP[key] = out
    return out


class WrapChip(QPushButton):
    """Relay 016: a Checks chip whose words wrap instead of running off the strip: a button holding its dot and a
    word-wrapped label, as tall as its words need at the width it is given."""

    def __init__(self, text: str, icon: QIcon):
        super().__init__()
        self._text = text
        lay = QHBoxLayout(self)
        lay.setContentsMargins(T.SPACE, 4, T.SPACE, 4)
        lay.setSpacing(T.SPACE - 2)
        dot = QLabel()
        dot.setPixmap(icon.pixmap(12, 12))
        dot.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.lab = QLabel(text)
        self.lab.setWordWrap(True)
        self.lab.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        lay.addWidget(dot, 0, Qt.AlignmentFlag.AlignTop)
        lay.addWidget(self.lab, 1)
        sp = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        sp.setHeightForWidth(True)
        self.setSizePolicy(sp)
        self.setMaximumWidth(T.SPACE * 58)

    def text(self) -> str:
        return self._text

    def sizeHint(self):
        """Its words on one line when they fit the chip's widest; wrapped beyond that."""
        m = self.layout().contentsMargins()
        w = (self.lab.fontMetrics().horizontalAdvance(self._text) + 12 + self.layout().spacing()
             + m.left() + m.right() + 4)
        w = min(w, self.maximumWidth())
        return QSize(w, self.heightForWidth(w))

    def minimumSizeHint(self):
        return QSize(T.SPACE * 16, self.layout().minimumSize().height())

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, w: int) -> int:
        return self.layout().heightForWidth(w)


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


_LABEL: dict = {}


def label_size(text: str) -> tuple[float, float]:
    """A wire's cable label box (w, h) for its text. Remembered: a drag refreshes every wire several times a frame,
    and measuring the same words again each time was a steady cost (relay 016 paid for its depth with this)."""
    hit = _LABEL.get(text)
    if hit is None:
        fm = QFontMetricsF(T.ui_font(8.5))
        hit = _LABEL[text] = (min(fm.horizontalAdvance(text) + 18, 260), fm.height() + 6)
        if len(_LABEL) > 2048:
            _LABEL.clear()
    return hit


def label_line(text: str, width: int) -> str:
    """A wire label shortened to its pill's width, remembered with its size (painting asks every frame)."""
    key = ("line", text, width)
    hit = _LABEL.get(key)
    if hit is None:
        hit = _LABEL[key] = QFontMetricsF(T.ui_font(8.5)).elidedText(text, Qt.TextElideMode.ElideRight, width)
    return hit


_LIT: dict = {}


def lit_brush(fill: QColor, h: float) -> QBrush:
    """Relay 016: a part's fill lit from above, a step lighter at the top and a touch darker at the bottom, so it
    reads as raised. Made once per colour and height (painting asks constantly)."""
    key = (fill.rgba(), round(h))
    br = _LIT.get(key)
    if br is None:
        g = QLinearGradient(0, 0, 0, max(h, 1))
        top = QColor(fill).lighter(109)
        top.setAlpha(fill.alpha())
        bottom = QColor(fill).darker(104)
        bottom.setAlpha(fill.alpha())
        g.setColorAt(0, top)
        g.setColorAt(0.55, fill)
        g.setColorAt(1, bottom)
        br = _LIT[key] = QBrush(g)
        if len(_LIT) > 4096:
            _LIT.clear()
    return br


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
    if shape == "triangle_down":                # relay 016: the words sit in the wide top of the point
        return QRectF(w * 0.2, 0, w * 0.6, h * 0.62)
    if shape == "trapezoid_down":
        return QRectF(w * 0.2, 0, w * 0.6, h)
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
    elif kind == R.CHOKE:                       # relay 017: a ferrite bead on a cable
        line((1, 8), (4.5, 8)); p.addRoundedRect(QRectF(4.5, 4, 7, 8), 2.5, 2.5); line((11.5, 8), (15, 8))
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
    elif kind == R.HUB:                         # relay 015: one upstream, several downstream
        p.addRoundedRect(QRectF(5, 5, 6, 6), 1.5, 1.5); line((11, 8), (15, 8))
        line((5, 6.5), (1, 3)); line((5, 8), (1, 8)); line((5, 9.5), (1, 13))
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
    elif name == "dir_right":            # relay 016: two boxes joined left to right
        p.addRoundedRect(QRectF(1, 5, 5, 6), 1, 1); p.addRoundedRect(QRectF(10, 5, 5, 6), 1, 1)
        line((6, 8), (10, 8)); line((8.5, 6.5), (10, 8), (8.5, 9.5))
    elif name == "dir_down":             # and top to bottom
        p.addRoundedRect(QRectF(5, 1, 6, 5), 1, 1); p.addRoundedRect(QRectF(5, 10, 6, 5), 1, 1)
        line((8, 6), (8, 10)); line((6.5, 8.5), (8, 10), (9.5, 8.5))
    elif name == "textpos":              # relay 018, the format bar: a 3 x 3 with lines in one corner
        p.addRoundedRect(QRectF(1.5, 1.5, 13, 13), 2, 2); line((4, 5), (10, 5)); line((4, 8), (8, 8))
    elif name == "colour":
        p.addEllipse(QRectF(2, 2, 12, 12)); p.addEllipse(QRectF(5, 4.5, 2, 2)); p.addEllipse(QRectF(9, 4.5, 2, 2))
        p.addEllipse(QRectF(4, 8.5, 2, 2))
    elif name == "shape":
        p.addRoundedRect(QRectF(1.5, 5, 7, 7), 1.5, 1.5); p.addEllipse(QRectF(8, 2, 6.5, 6.5))
    elif name == "lock":
        p.addRoundedRect(QRectF(3, 7, 10, 7.5), 1.5, 1.5); p.moveTo(5, 7); p.cubicTo(5, 1.5, 11, 1.5, 11, 7)
    elif name == "find":
        p.addEllipse(QRectF(2, 2, 8.5, 8.5)); line((9.5, 9.5), (14.5, 14.5))
    elif name == "minimap":
        p.addRoundedRect(QRectF(1.5, 3, 13, 10), 1.5, 1.5); p.addRect(QRectF(8, 7, 5, 4.5))
    elif name == "check":
        line((2, 8.5), (6, 12.5), (14, 3.5))
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


TEMPLATE_PREVIEW = (260, 120)
TEMPLATE_ROW = 40                # px, one row of the New rig template list


def template_pixmap(key: str, labels: dict | None, receivers: list | None, dpr: float = 2.0,
                    down: bool = False) -> QPixmap:
    """Relay 015: a small picture of a New rig template's shape, its parts as boxes in their kind colours and
    its wires as lines; Blank shows an empty dashed grid. Relay 016: drawn top to bottom when `down`."""
    w, h = TEMPLATE_PREVIEW
    pm = QPixmap(int(w * dpr), int(h * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(T.color("panel"))
    qp = QPainter(pm)
    qp.setRenderHint(QPainter.RenderHint.Antialiasing)
    rig = R.template(key, labels, receivers, down=down)
    if not rig["nodes"]:
        pen = QPen(T.color("line_strong"), 1.2, Qt.PenStyle.DashLine)
        qp.setPen(pen)
        qp.setBrush(Qt.BrushStyle.NoBrush)
        qp.drawRoundedRect(QRectF(16, 16, w - 32, h - 32), 6, 6)
        qp.setPen(T.color("muted"))
        qp.setFont(T.ui_font(9))
        qp.drawText(QRectF(0, 0, w, h), Qt.AlignmentFlag.AlignCenter, "an empty grid")
        qp.end()
        return pm
    rects = {n["id"]: (n["x"], n["y"], *G.size(n)) for n in rig["nodes"]}
    x0 = min(r[0] for r in rects.values())
    y0 = min(r[1] for r in rects.values())
    x1 = max(r[0] + r[2] for r in rects.values())
    y1 = max(r[1] + r[3] for r in rects.values())
    pad = 10
    k = min((w - 2 * pad) / max(x1 - x0, 1), (h - 2 * pad) / max(y1 - y0, 1))
    ox, oy = (w - (x1 - x0) * k) / 2, (h - (y1 - y0) * k) / 2

    def box(nid):
        x, y, bw, bh = rects[nid]
        return QRectF(ox + (x - x0) * k, oy + (y - y0) * k, bw * k, bh * k)
    qp.setPen(QPen(T.color("wire"), 1.6))
    for wre in rig["wires"]:
        a, b = box(wre["from"]), box(wre["to"])
        if down:
            qp.drawLine(QPointF(a.center().x(), a.bottom()), QPointF(b.center().x(), b.top()))
        else:
            qp.drawLine(QPointF(a.right(), a.center().y()), QPointF(b.left(), b.center().y()))
    qp.setFont(T.ui_font(7))
    for n in rig["nodes"]:
        r = box(n["id"])
        colour = category_colour(CATEGORY.get(n["part"]["type"]))
        fill = QColor(colour)
        fill.setAlphaF(0.22)
        qp.setPen(QPen(colour, 1.3))
        qp.setBrush(fill)
        qp.drawRoundedRect(r, 3, 3)
        qp.setPen(T.color("text"))
        label = (labels or R.PART_TYPES).get(n["part"]["type"], {}).get("label", n["part"]["type"])
        qp.drawText(r.adjusted(2, 0, -2, 0), Qt.AlignmentFlag.AlignCenter,
                    qp.fontMetrics().elidedText(label, Qt.TextElideMode.ElideRight, int(r.width() - 4)))
    qp.end()
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
            # relay 018: each section has a header in its colour: a coloured bar, the title in a readable shade of
            # that colour, and a hairline in it running on to the edge
            x0 = 2
            cat = index.data(ROLE_CAT)
            col = section_colour(cat, self.lst.colour_on)
            if cat and self.lst.colour_on:
                qp.setPen(Qt.PenStyle.NoPen)
                qp.setBrush(col)
                qp.drawRoundedRect(QRectF(r.left() + 2, r.bottom() - 16, 4, 12), 2, 2)
                x0 = 12
            ink = QColor(T.readable_on(col.name(), T.C["side"])) if cat and self.lst.colour_on else T.color("muted")
            qp.setPen(ink)
            qp.setFont(T.ui_font(7.5, QFont.Weight.Bold))
            fm = QFontMetricsF(qp.font())
            qp.drawText(r.adjusted(x0, 0, 0, -4), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom), text)
            if cat and self.lst.colour_on:
                line = QColor(col)
                line.setAlphaF(0.28)
                qp.setPen(QPen(line, 1))
                x1 = r.left() + x0 + fm.horizontalAdvance(text) + 8
                if x1 < r.right() - 4:
                    qp.drawLine(QPointF(x1, r.bottom() - 9.5), QPointF(r.right() - 4, r.bottom() - 9.5))
            qp.restore()
            return
        tile = r.adjusted(0, 2, -2, -2)
        hover = bool(opt.state & QStyle.StateFlag.State_MouseOver)
        pressed = self.lst.pressed_row == index.row()
        colour = palette_colour(index.data(ROLE_CAT), self.lst.colour_on)
        rad = round(T.RADIUS * 0.5)
        body = tile.adjusted(0.5, 0.5, -0.5, -0.5)
        # relay 018: a tile keeps its section's colour - a tint mixed into the raised fill, lit from above as in relay
        # 016, a coloured edge on the left and a coloured icon - so the list reads by colour, not grey
        base = T.C["tile_pressed" if pressed else ("tile_hover" if hover else "tile")]
        tint = 0.0 if not self.lst.colour_on or not index.data(ROLE_CAT) else (0.16 if hover else 0.10)
        top = QColor(T.mix(T.mix(base, colour.name(), tint), "#FFFFFF", 0.035))
        bottom = QColor(T.mix(base, colour.name(), tint * 0.6))
        g = QLinearGradient(body.topLeft(), body.bottomLeft())
        g.setColorAt(0, top)
        g.setColorAt(1, bottom)
        for dy, _s, alpha in T.SHADOWS["raised"][:1]:        # a soft shadow under the tile
            qp.setPen(Qt.PenStyle.NoPen)
            qp.setBrush(T.qc("shadow", alpha))
            qp.drawRoundedRect(body.translated(0, dy), rad, rad)
        qp.setBrush(QBrush(g))
        edge = T.color("survey") if pressed else (QColor(T.mix(T.C["line_strong"], colour.name(), 0.45 if hover else 0.25))
                                                   if tint else (T.color("line_strong") if hover else T.color("tile_edge")))
        qp.setPen(QPen(edge, T.BORDER))
        qp.drawRoundedRect(body, rad, rad)
        if tint:
            qp.save()
            clip = QPainterPath()
            clip.addRoundedRect(body, rad, rad)
            qp.setClipPath(clip)
            qp.setPen(Qt.PenStyle.NoPen)
            qp.setBrush(colour)
            qp.drawRect(QRectF(body.left(), body.top(), 3, body.height()))
            qp.restore()
        qp.setPen(QPen(T.color("highlight", T.HIGHLIGHT_ALPHA["raised"]), 1))     # the lit top edge
        qp.drawLine(QPointF(body.left() + rad, body.top() + 1), QPointF(body.right() - rad, body.top() + 1))
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
        shape = G.oriented(R.shape_of(self.node), self.screen.down)
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
            x, y = G.mid_side(self.node, s, self.screen.down)
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
        shape = G.oriented(R.shape_of(self.node), self.screen.down)
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
            # a soft shadow, the outline offset: a part sits at the raised level, a selected one is lifted to the
            # overlay level (relay 016), its shadow deeper
            for dy, _spread, alpha in T.SHADOWS["overlay" if sel else "raised"]:
                qp.setBrush(T.qc("shadow", alpha))
                qp.drawPath(body.translated(0, dy))
            if sel or target:                              # a faint outer glow
                qp.setBrush(Qt.BrushStyle.NoBrush)
                for width, alpha in ((12, 0.07), (7, 0.13)):
                    qp.setPen(QPen(T.qc("survey", alpha if sel else alpha * 1.6), width))
                    qp.drawPath(body)
            qp.setBrush(lit_brush(fill, h))
            qp.setPen(QPen(edge, T.SELECTED_BORDER if (sel or target) else T.BORDER))
            qp.drawPath(body)
            self._paint_words(qp, shape, w, h, small, main, soft, own, widget)
        if self.node.get("wall"):                          # relay 014: seated across an enclosure's wall
            self._paint_mounted(qp, w, h)
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
            hot = key == self.hot_port or (wf == (nid, port) if kind_ == "out"
                                           else bool(wf) and wf[:2] == ("in", nid) and (wf + (0,))[2] == port)
            colour = T.color("survey") if hot else T.color("wire")
            r = PORT_R + 2 if key == self.hot_port else PORT_R
            c = QPointF(x, y) - here
            gender = R.port_gender(self.node, kind_, port)
            if gender == "F":
                qp.setPen(QPen(T.qc("background"), 1.5))
                qp.setBrush(T.qc("background"))
                qp.drawEllipse(c, r, r)
                qp.setPen(QPen(colour, 2.2))
                qp.setBrush(Qt.BrushStyle.NoBrush)
                qp.drawEllipse(c, r - 1.6, r - 1.6)
            else:
                qp.setPen(QPen(T.qc("background"), 2))
                qp.setBrush(colour)
                qp.drawEllipse(c, r, r)
                if gender == "M":                          # the pin
                    qp.setPen(Qt.PenStyle.NoPen)
                    qp.setBrush(T.qc("background"))
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
            qp.setPen(QPen(T.qc("power") if fed else T.color("alert"), 1.6))
            qp.setBrush(T.qc("power") if fed else T.color("background"))
            qp.drawPath(diamond)
        if (self.hover or sel) and self.screen.wiring_from is None:
            qp.setBrush(Qt.BrushStyle.NoBrush)
            qp.setPen(QPen(T.qc("survey", 0.85), 1.5))
            for r in self._rings():
                qp.drawEllipse(r - here, PORT_R - 1, PORT_R - 1)
        if sel and not R.is_locked(self.node) and self.screen.sel_count == 1:
            draw_handles(qp, w, h)            # resize from any corner or edge

    def _paint_mounted(self, qp, w, h):
        """A bulkhead seated on a wall: the wall line runs on through the part as a faint line, with a short
        flange either side of the box, so it reads as mounted through the enclosure."""
        side = (self.node.get("wall") or {}).get("side")
        col = category_colour("enclosure") if self.screen.colour_on else T.color("line_strong")
        faint, strong = QColor(col), QColor(col)
        faint.setAlphaF(0.4)
        strong.setAlphaF(0.9)
        if side in ("top", "bottom"):
            a, b, ends = QPointF(0, h / 2), QPointF(w, h / 2), ((QPointF(-9, h / 2), QPointF(0, h / 2)),
                                                                (QPointF(w, h / 2), QPointF(w + 9, h / 2)))
        else:
            a, b, ends = QPointF(w / 2, 0), QPointF(w / 2, h), ((QPointF(w / 2, -9), QPointF(w / 2, 0)),
                                                                (QPointF(w / 2, h), QPointF(w / 2, h + 9)))
        qp.setPen(QPen(faint, 1.2, Qt.PenStyle.DashLine))
        qp.drawLine(a, b)
        qp.setPen(QPen(strong, 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        for p1, p2 in ends:
            qp.drawLine(p1, p2)

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
        wd = self.words(shape, w, h, x0)
        tw, block = wd["tw"], wd["block"]
        pos = R.text_pos(self.node)                       # relay 008: where the words sit, 3 x 3
        top = {"t": box.top() + 8, "m": box.top() + (box.height() - block) / 2,
               "b": box.bottom() - block - 6}[pos[0]]
        top = max(box.top() + 4, top)
        halign = {"l": Qt.AlignmentFlag.AlignLeft, "c": Qt.AlignmentFlag.AlignHCenter,
                  "r": Qt.AlignmentFlag.AlignRight}[pos[1]]
        k = wd["k"]                                       # relay 018: the part's own font size, as a scale
        qp.setPen(small)
        qp.setFont(wd["f_kind"])
        qp.drawText(QRectF(x0, top, tw, 16 * k), halign, wd["kind_line"])
        # relay 016: the name and the note wrap inside the part; what does not fit ends in "…" (the whole text is
        # in the part's tooltip), and the part grows to fit when its words are edited and there is room
        y = top + WORDS_KIND * k
        qp.setPen(main)
        qp.setFont(wd["f_name"])
        for line in wd["name"]:
            qp.drawText(QRectF(x0, y, tw, (WORDS_NAME + 2) * k), halign, line)
            y += WORDS_NAME * k
        if wd["note"]:
            qp.setPen(soft)
            qp.setFont(wd["f_note"])
            y += 2
            for line in wd["note"]:
                qp.drawText(QRectF(x0, y, tw, (WORDS_NOTE + 2) * k), halign, line)
                y += WORDS_NOTE * k

    def words(self, shape: str, w: float, h: float, x0: float | None = None) -> dict:
        """Relay 016: how a part's words lay out in its box: the kind line, the name and the note wrapped into the
        room there is (the name first, then the note), whether anything was cut, and the height the part would
        need to show them all. Painting and growing to fit both ask this, so they always agree. Kept until the next
        edit (the screen's words_gen), so a drag repaints from the cache."""
        key = (shape, w, h, x0, self.screen.show_notes, self.screen.words_gen, self.node.get("font_size"),
               self.node.get("bold"))
        hit = getattr(self, "_words", None)
        if hit is not None and hit[0] == key:
            return hit[1]
        out = self._lay_words(shape, w, h, x0)
        self._words = (key, out)
        return out

    def _lay_words(self, shape: str, w: float, h: float, x0: float | None) -> dict:
        p = self.node["part"]
        box = text_box(shape, w, h)
        if x0 is None:
            x0 = box.left() + (28 if p["type"] == "antenna" and R.antenna(self.node).get("type") else 0)
        tw = max(20, box.right() - x0)
        a_type = R.antenna(self.node).get("type")
        kind = R.ANTENNA_TYPES.get(a_type, ("Antenna",))[0] if a_type else self.screen.kind_label(self.node)
        lead = R.antenna(self.node).get("lead") if p["type"] == "antenna" else None
        if lead:                                     # relay 017: the moulded lead is shown on the antenna
            kind += f" · lead {lead['length']}" if lead.get("length") else " · built-in lead"
        name = p.get("name") or "(no name)"
        if p["type"] == "attenuator" and p.get("db") not in (None, "", 0):
            name = f"{p['db']} dB"
        detail = str(self._detail() or "")
        note_on = bool(detail and self.screen.show_notes)
        # relay 018: the format bar's font size (per part, drawing only) scales the three lines together; bold makes
        # the name heavier and the kind line demi-bold
        fs = R.font_size(self.node) or 10.0
        k = fs / 10.0
        bold = bool(self.node.get("bold"))
        f_name = T.ui_font(fs, QFont.Weight.Bold if bold else QFont.Weight.DemiBold)
        f_note = T.ui_font(8 * k)
        f_kind = T.ui_font(8.5 * k, QFont.Weight.DemiBold if bold else QFont.Weight.Normal)
        kh, nh, th = WORDS_KIND * k, WORDS_NAME * k, WORDS_NOTE * k
        name_all = wrap_lines(f_name, name, tw, 99)[0]
        note_all = wrap_lines(f_note, detail, tw, 99)[0] if note_on else []
        room = box.height() - 12 - kh
        n_name = max(1, min(len(name_all), int(room // nh)))
        n_note = min(len(note_all), int(max(0, room - n_name * nh - 2) // th)) if note_on else 0
        if note_on and n_note == 0 and n_name > 1:          # keep one line of the note in sight
            n_name -= 1
            n_note = min(len(note_all), int(max(0, room - n_name * nh - 2) // th))
        name_lines, cut = wrap_lines(f_name, name, tw, n_name)
        note_lines, cut2 = wrap_lines(f_note, detail, tw, n_note) if n_note else ([], note_on)
        block = kh + len(name_lines) * nh + (2 + len(note_lines) * th if note_lines else 0)
        need = 14 + kh + len(name_all) * nh + (2 + len(note_all) * th if note_on else 0)
        need = need * h / max(box.height(), 1)                # a shape whose words sit in part of its box
        kind_line = QFontMetricsF(f_kind).elidedText(kind, Qt.TextElideMode.ElideRight, int(tw))
        return {"kind": kind, "kind_line": kind_line, "name": name_lines, "note": note_lines, "cut": bool(cut or cut2), "tw": tw,
                "block": block, "need": need, "full": name + (f"\n{detail}" if detail else ""),
                "k": k, "f_name": f_name, "f_note": f_note, "f_kind": f_kind}

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
        self.setToolTip(self._port_tip(None))
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
            hint = ("Locked: it stays where it is. Click to edit it; right-click to format it."
                    if R.is_locked(self.node) else "Click to edit · drag to move · right-click to format and arrange")
            shape = G.oriented(R.shape_of(self.node), self.screen.down)
            if shape != "pipe":
                wd = self.words(shape, *self.wh())
                if wd["cut"]:                  # relay 016: words cut short in the part are whole here
                    return wd["full"] + "\n\n" + hint
            return hint
        _nid, kind, port = hot
        if kind == "in" and self.node["part"]["type"] == "pc":       # relay 018: which radio, where it is plugged
            radio = R.upstream_radio(self.screen.rig, self.node["id"], port)
            lines = self.screen.usb_lines(radio)
            return (f"USB input {port + 1}" + (f": {R._pname(radio)}" if radio else ": no radio wired here yet")
                    + ("\n" + "\n".join(lines) if lines else "") + "\n\nDrawing and Output only: never in the "
                    "recorded text.")
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
                    self.screen.start_wire_in(self, ev.scenePos(), port[1])
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
            x, y, w, h = G.resize(rect0, grip, d.x(), d.y(), **resize_keys(self.screen, ev.modifiers()))
            self.screen.status.emit(RESIZE_HINT)
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

    def drag_to(self, scene_pt: QPointF, keep: bool = False, mods=None):
        """Resize to a pointer place. `mods` (the held keys, relay 015) wins over `keep` when given."""
        c, start, rect0 = self._resize
        d = scene_pt - start
        keys = resize_keys(self.screen, mods) if mods is not None else {
            "keep_ratio": keep, "from_centre": False, "snap": R.GRID if self.screen.snap_on else None}
        x1, y1, w, h = G.resize(rect0, c, d.x(), d.y(), **keys,
                                min_w=R.ENC_MIN_W, min_h=R.ENC_MIN_H, max_w=R.ENC_MAX, max_h=R.ENC_MAX)
        self.screen.status.emit(RESIZE_HINT)
        self.prepareGeometryChange()
        self.enc["w"], self.enc["h"] = int(w), int(h)
        self.setPos(x1, y1)
        self.enc["x"], self.enc["y"] = int(round(x1)), int(round(y1))
        self.update()
        for nid in G.reseat(self.screen.rig, self.enc["id"]):        # relay 014: seated bulkheads stay on the wall
            it = self.screen.box_item(nid)
            n = R.box(self.screen.rig, nid)
            if it is not None and n is not None:
                it.setPos(n["x"], n["y"])

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
            self.drag_to(ev.scenePos(), mods=ev.modifiers())
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
    carries (Coax, USB) are named in a label at its middle, and their chokes are beads along it. Past the radio a
    wire is the USB side and is drawn dashed when that option is on.

    Relay 015: a wire can bend; drag a point anywhere (on the grid when snap is on); click one and press Delete to
    remove it. A selected wire shows a handle near each end: drag it to another part to reconnect that end. Bends
    live in chain_layout.json. Relay 016: a click selects; dragging the wire moves it (the stretch under the pointer,
    re-routed, its ends kept); Ctrl+click adds a bend point. A double-click adds nothing."""

    def __init__(self, screen: "ChainScreen", wire: dict):
        super().__init__()
        self.screen, self.wire = screen, wire
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        self.setAcceptHoverEvents(True)
        self.setZValue(1)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("A wire: click it to give it cables, drop a cable from the Cables list onto it, "
                        "drag it to move it, Ctrl+click to add a bend point, or right-click for more")
        self.color = T.color("wire")
        self.digital = False
        self.hover = False
        self.label_rect = QRectF()
        self.label_text = ""
        self.route = None
        self._drag = None            # ("point", i) | ("body", scene pt, screen pt) | ("end", "from"|"to")
        self.refresh()

    def cables(self) -> list:
        return self.wire.get("cables") or []

    def points(self) -> list:
        return self.wire.get("points") or []

    def ends(self):
        am = self.screen.anchor_map
        return am.get((self.wire["from"], "out", self.wire["port"])), am.get((self.wire["to"], "in", self.in_key()))

    def in_key(self) -> int:
        """Which of its far part's inputs this wire uses (relay 015: a PC or hub has several)."""
        return R.in_slot(self.wire) if R.multi_in(R.box(self.screen.rig, self.wire["to"])) else 0

    def key(self) -> tuple:
        return (self.wire["from"], self.wire["port"])

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

    def sel_point(self) -> int | None:
        sp = self.screen.sel_point
        if sp and sp[:2] == self.key() and sp[2] < len(self.points()):
            return sp[2]
        return None

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
            self.beads = []
            self.setToolTip("Mated directly, port to port (SMA parts screwed together). In the recorded text "
                            "it is the same as a wire with no cable. Click it, then Delete, to separate them."
                            + ("\nM to M or F to F: these won't screw together." if self.clash() else ""))
            return
        self.route = G.route_via(a, b, self.points(), self.screen.wire_style, square=bool(self.wire.get("square")))
        self.setPath(routed_path(self.route, a, b))
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
            w, h = label_size(self.label_text)
            mid = self.label_point()
            self.label_rect = QRectF(mid.x() - w / 2, mid.y() - h / 2, w, h)
        else:
            self.label_rect = QRectF()
        self.beads = self.bead_spots()

    # -- chokes (relay 017): beads on the wire, one stretch per cable
    def bead_spots(self) -> list:
        """(cable index, choke index, point, angle in degrees) for every choke on this wire."""
        cab = self.cables()
        path = self.path()
        length = path.length()
        out = []
        if not cab or length <= 0:
            return out
        for ci, c in enumerate(cab):
            for k, ch in enumerate(c.get("chokes") or []):
                pc = path.percentAtLength(R.choke_global(len(cab), ci, ch.get("at", 0.5)) * length)
                out.append((ci, k, path.pointAtPercent(pc), path.angleAtPercent(pc)))
        return out

    def bead_at(self, pt: QPointF):
        for ci, k, q, _a in getattr(self, "beads", []):
            if QLineF(q, pt).length() <= BEAD_HIT:
                return ci, k
        return None

    def along(self, pt: QPointF) -> float:
        """How far along the wire (0..1, by length) the point nearest `pt` lies."""
        path = self.path()
        length = path.length()
        if length <= 0:
            return 0.5
        best, best_d = 0.5, 1e18
        steps = max(40, int(length / 4))
        for s in range(steps + 1):
            f = s / steps
            q = path.pointAtPercent(path.percentAtLength(f * length))
            d = (q.x() - pt.x()) ** 2 + (q.y() - pt.y()) ** 2
            if d < best_d:
                best, best_d = f, d
        return best

    def label_point(self) -> QPointF:
        """The middle of the wire, or with bends the middle of its longest stretch, never on a corner."""
        spot = G.label_spot(self.route) if self.points() and self.route else None
        return QPointF(*spot) if spot else self.path().pointAtPercent(0.5)

    def end_handles(self) -> dict:
        """Where the two end handles sit on a selected wire: a little way along it from each end, clear of
        the parts' own dots (which start new wires)."""
        if self.is_mated() or not self.isSelected():
            return {}
        path = self.path()
        n = path.length()
        if n < 3 * BEND_HIT:                       # a stub too short for two handles
            return {}
        at = min(WIRE_END_AT, n * 0.3)             # a short wire (parts close together) keeps its handles apart
        return {"from": path.pointAtPercent(path.percentAtLength(at)),
                "to": path.pointAtPercent(path.percentAtLength(n - at))}

    def point_at(self, pt: QPointF) -> int | None:
        best, best_d = None, BEND_HIT
        for i, q in enumerate(self.points()):
            d = QLineF(QPointF(q[0], q[1]), pt).length()
            if d <= best_d:
                best, best_d = i, d
        return best

    def end_at(self, pt: QPointF) -> str | None:
        for which, q in self.end_handles().items():
            if QLineF(q, pt).length() <= BEND_HIT:
                return which
        return None

    def joint_rect(self) -> QRectF:
        """The collar half way along the stub between two mated points."""
        a, b = self.ends()
        if a is None:
            return QRectF()
        mx, my = (a[0] + b[0]) / 2 if b else a[0], (a[1] + b[1]) / 2 if b else a[1]
        across = a[2] in ("left", "right")
        w, h = (8, 20) if across else (20, 8)
        return QRectF(mx - w / 2, my - h / 2, w, h)

    def _handle_rects(self) -> list:
        out = [QRectF(q[0] - BEND_HIT, q[1] - BEND_HIT, 2 * BEND_HIT, 2 * BEND_HIT) for q in self.points()]
        out += [QRectF(q.x() - BEND_HIT, q.y() - BEND_HIT, 2 * BEND_HIT, 2 * BEND_HIT)
                for q in self.end_handles().values()]
        return out

    def boundingRect(self):
        if self.is_mated():
            return self.joint_rect().adjusted(-6, -6, 6, 6)
        r = super().boundingRect()
        if not self.label_rect.isNull():
            r = r.united(self.label_rect.adjusted(-2, -2, 2, 2))
        for h in self._handle_rects():
            r = r.united(h)
        for _ci, _k, q, _a in getattr(self, "beads", []):
            r = r.united(QRectF(q.x() - BEAD_HIT, q.y() - BEAD_HIT, 2 * BEAD_HIT, 2 * BEAD_HIT))
        return r

    def shape(self):
        if self.is_mated():
            p = QPainterPath()
            p.addRoundedRect(self.joint_rect().adjusted(-4, -4, 4, 4), 6, 6)
            return p
        st = QPainterPathStroker()
        st.setWidth(12)
        out = st.createStroke(self.path())
        out.setFillRule(Qt.FillRule.WindingFill)
        if not self.label_rect.isNull():
            out.addRoundedRect(self.label_rect, 8, 8)
        for h in self._handle_rects():
            out.addEllipse(h)
        for _ci, _k, q, _a in getattr(self, "beads", []):
            out.addEllipse(q, BEAD_HIT, BEAD_HIT)
        return out

    def paint(self, qp: QPainter, opt, widget=None):
        qp.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.is_mated():                 # the joint: a connector stub with a collar in the middle
            r = self.joint_rect()
            colour = T.color("alert") if self.clash() else (
                T.color("survey") if self.isSelected() else (T.color("text2") if self.hover else self.color))
            qp.setPen(QPen(T.qc("background"), 9, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            qp.drawPath(self.path())
            qp.setPen(QPen(colour, 5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            qp.drawPath(self.path())
            qp.setPen(QPen(T.qc("background"), 1.5))
            qp.setBrush(colour)
            qp.drawRoundedRect(r, 3, 3)
            return
        qp.setPen(self.pen())
        qp.setBrush(Qt.BrushStyle.NoBrush)
        qp.drawPath(self.path())
        cab = self.cables()
        drag = self._drag[1:3] if self._drag and self._drag[0] == "choke" else None
        for ci, k, q, ang in getattr(self, "beads", []):      # chokes: a bead lying along the wire
            qp.save()
            qp.translate(q)
            qp.rotate(-ang)
            hot = (ci, k) == drag
            qp.setPen(QPen(T.qc("survey") if hot else T.qc("background"), 1.6))
            qp.setBrush(cable_colour(cab[ci].get("type")).darker(130 if hot else 115))
            qp.drawRoundedRect(QRectF(-7.5, -5, 15, 10), 4, 4)
            qp.setPen(QPen(T.qc("background"), 1))
            qp.drawLine(QPointF(-2.5, -3), QPointF(-2.5, 3))
            qp.drawLine(QPointF(2.5, -3), QPointF(2.5, 3))
            qp.restore()
        if self.label_text:
            first = self.cables()[0].get("type")
            edge = T.color("survey") if self.isSelected() else cable_colour(first)
            qp.setPen(QPen(edge, 1.2))
            qp.setBrush(T.qc("panel"))
            qp.drawRoundedRect(self.label_rect, self.label_rect.height() / 2, self.label_rect.height() / 2)
            qp.setPen(T.qc("text2"))
            qp.setFont(T.ui_font(8.5))
            qp.drawText(self.label_rect.adjusted(9, 0, -9, 0), Qt.AlignmentFlag.AlignCenter,
                        label_line(self.label_text, int(self.label_rect.width() - 18)))
        # bend points: small squares, shown while the wire is hovered or selected (the picked one filled)
        if self.points() and (self.hover or self.isSelected()):
            sp = self.sel_point()
            for i, q in enumerate(self.points()):
                r = QRectF(q[0] - 4.5, q[1] - 4.5, 9, 9)
                qp.setPen(QPen(T.qc("survey") if self.isSelected() else T.color("text2"), 1.6))
                qp.setBrush(T.qc("survey") if i == sp else T.color("background"))
                qp.drawRect(r)
        # end handles: small diamonds near each end of a selected wire
        for q in self.end_handles().values():
            d = QPainterPath()
            d.moveTo(q.x(), q.y() - 6)
            d.lineTo(q.x() + 6, q.y())
            d.lineTo(q.x(), q.y() + 6)
            d.lineTo(q.x() - 6, q.y())
            d.closeSubpath()
            qp.setPen(QPen(T.qc("background"), 1.5))
            qp.setBrush(T.qc("survey"))
            qp.drawPath(d)

    def hoverEnterEvent(self, ev):
        self.hover = True
        self.refresh()

    def hoverLeaveEvent(self, ev):
        self.hover = False
        self.unsetCursor()
        self.refresh()

    def hoverMoveEvent(self, ev):
        pt = ev.scenePos()
        hit = self.bead_at(pt)
        if hit is not None:
            self.setCursor(Qt.CursorShape.SizeHorCursor)
            note = str((self.cables()[hit[0]]["chokes"][hit[1]]).get("note") or "").strip()
            self.setToolTip(("A choke" + (f": {note}" if note else "") + ". Drag it along the cable to show where it "
                             "sits; right-click to remove it. Drawing only, except that the cable's text says "
                             "\"with choke\"."))
        elif self.end_at(pt):
            self.setCursor(Qt.CursorShape.SizeAllCursor)
            self.setToolTip("Drag this end to another part to reconnect it")
        elif self.point_at(pt) is not None:
            self.setCursor(Qt.CursorShape.SizeAllCursor)
            self.setToolTip("A bend point: drag it, or click it and press Delete to remove it")
        else:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setToolTip("A wire: click it to give it cables, drop a cable from the Cables list onto it, "
                            "drag it to move it, Ctrl+click to add a bend point, or right-click for more")

    def _select_only(self, mods):
        if mods & Qt.KeyboardModifier.ControlModifier:            # Ctrl+click toggles, as on boxes
            self.setSelected(not self.isSelected())
            return
        if not (mods & Qt.KeyboardModifier.ShiftModifier):
            if not self.isSelected() or len(self.scene().selectedItems()) > 1:
                self.scene().clearSelection()
        self.setSelected(True)

    def mousePressEvent(self, ev):
        """Relay 016: a left-click selects the wire (or the bend point under it); a drag then moves what was
        pressed: a point moves the point, the wire's body moves the stretch under the pointer, ends kept on their
        parts. Ctrl+left-click adds a bend point there. The handles near the ends still reconnect."""
        if ev.button() != Qt.MouseButton.LeftButton or self.is_mated() or self.screen.read_only:
            super().mousePressEvent(ev)
            return
        pt = ev.scenePos()
        bead = self.bead_at(pt)
        if bead is not None and not ev.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self._select_only(ev.modifiers())          # relay 017: a choke slides along its own cable
            self.screen.sel_point = None
            self._drag = ("choke", bead[0], bead[1], False)
            self.update()
            ev.accept()
            return
        which = self.end_at(pt)
        if which:
            self._drag = ("end", which)
            self.screen.start_reconnect(self, which, pt)
            ev.accept()
            return
        i = self.point_at(pt)
        if ev.modifiers() & Qt.KeyboardModifier.ControlModifier and i is None:
            self.screen.add_bend(self.wire, pt)            # Ctrl+click: a bend point here (one undo step)
            self._drag = None
            ev.accept()
            return
        self._select_only(ev.modifiers())
        if i is not None:
            self.screen.sel_point = (*self.key(), i)
            self._drag = ("point", i, False)
            self.update()
            ev.accept()
            return
        self.screen.sel_point = None
        a, b = self.ends()
        if a is None or b is None:
            self._drag = None
        else:                       # the wire as it was when grabbed: every move is worked out again from here
            self._drag = ("body", pt, ev.screenPos(), [list(q) for q in self.points()], a, b, False)
        self.update()
        ev.accept()

    def mouseMoveEvent(self, ev):
        d = self._drag
        if d is None:
            return
        pt = ev.scenePos()
        if d[0] == "end":
            self.screen.drag_reconnect(pt)
            return
        if d[0] == "choke":
            _k, ci, k, _moved = d
            cab = self.cables()
            if ci >= len(cab) or k >= len(cab[ci].get("chokes") or []):
                return
            at = R.choke_local(len(cab), ci, self.along(pt))
            if cab[ci]["chokes"][k].get("at") != at:
                cab[ci]["chokes"][k]["at"] = at
                self._drag = ("choke", ci, k, True)
                self.screen.status.emit("sliding the choke along its cable (drawing only; Undo puts it back)")
                self.refresh()
                self.update()
            return
        if d[0] == "body":
            _k, grab, at, orig, a, b, moved = d
            if not moved and (ev.screenPos() - at).manhattanLength() < CLICK_TOL * 2:
                return                   # a click, not a drag yet
            new = G.move_wire(a, b, orig, self.screen.wire_style, bool(self.wire.get("square")),
                              (grab.x(), grab.y()), pt.x() - grab.x(), pt.y() - grab.y(),
                              snap=R.GRID if self.screen.snap_on else None)
            if new:
                self.wire["points"] = new
            else:
                self.wire.pop("points", None)
            self._drag = d[:6] + (True,)
            self.screen.status.emit("moving the wire: its ends stay on their parts (Undo puts it back)")
            self.refresh()
            self.update()
            return
        if d[0] == "point":
            x, y = self.screen.bend_place(pt)
            pts = self.wire["points"]
            if pts[d[1]] != [x, y]:
                pts[d[1]] = [x, y]
                self._drag = ("point", d[1], True)
                self.refresh()
                self.update()

    def mouseReleaseEvent(self, ev):
        d, self._drag = self._drag, None
        if d is None or ev.button() != Qt.MouseButton.LeftButton:
            super().mouseReleaseEvent(ev)
            return
        if d[0] == "end":
            self.screen.end_reconnect(ev.scenePos())
        elif d[0] == "choke":
            if d[3]:
                self.screen.changed()
            self.screen.select_wire(*self.key())
        elif d[0] == "point" and d[2]:
            self.screen.changed()
        elif d[0] == "body" and d[6]:
            self.screen.changed()
            self.screen.select_wire(*self.key())
        ev.accept()

    def mouseDoubleClickEvent(self, ev):
        """Relay 016: a double-click no longer adds a bend point (Ctrl+click does); it is a second click."""
        ev.accept()

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
            x, y, w, h = G.resize(rect0, grip, d.x(), d.y(), **resize_keys(self.screen, ev.modifiers()),
                                  min_w=G.POWER_MIN_W, min_h=G.POWER_MIN_H)
            self.screen.status.emit(RESIZE_HINT)
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
            pen = QPen(T.qc("lines", 0.6), 0)
            if o["dotted"]:
                pen.setStyle(Qt.PenStyle.DotLine)
            qp.setPen(pen)
            qp.drawLines(minor)
            if big:
                major = [QLineF(x, rect.top(), x, rect.bottom()) for x in xs if is_big(x)]
                major += [QLineF(rect.left(), y, rect.right(), y) for y in ys if is_big(y)]
                pen = QPen(T.qc("line_strong"), 0)
                if o["dotted"]:
                    pen.setStyle(Qt.PenStyle.DashLine)
                qp.setPen(pen)
                qp.drawLines(major)
            return
        qp.setPen(QPen(T.qc("lines"), 2))
        qp.drawPoints([QPointF(x, y) for x in xs for y in ys if not (is_big(x) and is_big(y))])
        if big:
            qp.setPen(QPen(T.qc("line_strong"), 3.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
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
            self.screen.menu_point = pt
            self.screen.fill_wire_menu(m, top)
        else:
            self.screen.fill_grid_menu(m, pt)
        self.menu_open = True
        self._menu_at = (time.monotonic(), ev.globalPos())
        try:
            m.open_at(ev.globalPos())
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

    def drawForeground(self, qp: QPainter, rect: QRectF):
        """Relay 016: the grid is slightly recessed, an inner shade along its four edges, so the drawing sits in
        something. Drawn in the viewport's own pixels over the scene: four gradient strips, a few fills a frame.
        (A picture of the drawing is made from the scene, so it never carries the shade.)"""
        super().drawForeground(qp, rect)
        depth, alpha = T.GRID_RECESS
        vr = self.viewport().rect()
        w, h = vr.width(), vr.height()
        qp.save()
        qp.resetTransform()
        qp.setPen(Qt.PenStyle.NoPen)
        dark, clear = T.color("shadow", alpha), T.color("shadow", 0.0)
        for (x0, y0, x1, y1), r in (((0, 0, 0, depth), QRectF(0, 0, w, depth)),
                                     ((0, h, 0, h - depth * 0.7), QRectF(0, h - depth * 0.7, w, depth * 0.7)),
                                     ((0, 0, depth, 0), QRectF(0, 0, depth, h)),
                                     ((w, 0, w - depth, 0), QRectF(w - depth, 0, depth, h))):
            g = QLinearGradient(x0, y0, x1, y1)
            g.setColorAt(0, dark)
            g.setColorAt(1, clear)
            qp.fillRect(r, QBrush(g))
        qp.restore()

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

    def set_colour_on(self, on: bool):
        """Colour coding on or off: nothing is rebuilt; the tiles are simply painted again."""
        self.colour_on = on
        self.viewport().update()

    # Relay 018: the sections, in the owner's order, each with its own colour (a part keeps its kind's colour).
    SECTIONS = (("ANTENNAS", "source", ("antenna", "probe", "load")),
                ("RF PARTS", "condition", ("filter", "lna", R.SPLITTER, "custom")),
                ("RADIOS AND PC", "radio", ("sdr", "bulkhead_usb", "isolator", R.HUB, "pc")))

    def set_labels(self, labels: dict):
        """The palette, one list (relay 018): My parts, Housings, Antennas, RF parts, SMA, Cables, On cables,
        Power, Radios and PC, each section headed in its colour. Cables (and the choke) drop onto wires; their
        keys carry the cable prefix, so a drag from this list says what it is."""
        self.labels = labels
        self.clear()

        def note_of(t):
            tip = labels[t].get("note") or f"Drag onto the grid to add a {labels[t]['label']}."
            return tip[0].upper() + tip[1:] + ("" if tip.endswith(".") else ".")

        self._heading("MY PARTS", "mine")
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
        placed = set()
        sections = list(self.SECTIONS)
        for title, cat, types in sections[:2]:
            self._heading(title, cat)
            for t in types:
                if t in labels:
                    self._entry(labels[t]["label"], t, t, CATEGORY.get(t), note_of(t))
                    placed.add(t)
        self._heading("SMA · drop onto a dot to mate", "sma")
        for t in R.SMA_TYPES:
            if t in labels:
                self._entry(labels[t]["label"], t, t, "sma",
                            f"Drag onto the grid to add a {labels[t]['label']}. Drop it with a dot on another "
                            "part's dot and the two mate directly, port to port, with no wire.")
                placed.add(t)
        self._heading("CABLES · drop onto a wire", "coax")
        for t in R.CABLE_TYPES:
            if t in labels:
                self._entry(labels[t]["label"], CABLE_MIME_PREFIX + t, t, t,
                            f"Drop onto a wire: the wire then carries a {labels[t]['label']}. Give it a length on its "
                            "card; the recorded text says it, e.g. "
                            + ("\"3 ft RG316 coax\"." if t == "coax" else "\"16 ft USB cable\"."))
        self._heading("ON CABLES · drop onto a cable", R.CHOKE)
        self._entry("Choke", CABLE_MIME_PREFIX + R.CHOKE, R.CHOKE, R.CHOKE,
                    "A ferrite bead or loop that stops the cable's outside carrying noise. Drop it onto a cable, "
                    "then slide it along the wire. The cable's text then says \"with choke\".")
        self._heading("POWER · drawing only", R.POWER)
        for key, name in R.POWER_KINDS.items():
            self._entry(name, POWER_MIME_PREFIX + key, R.POWER, R.POWER,
                        "Drag onto the grid, then drag from its dot to the part it powers. Power wires are "
                        "drawing only: they never enter the recorded text.")
        title, cat, types = sections[2]
        self._heading(title, cat)
        rest = [t for t in PALETTE_ORDER + [t for t in labels if t not in PALETTE_ORDER]
                if t in labels and t not in placed and t not in R.CABLE_TYPES and t not in R.OLD_CABLE_TYPES
                and t not in R.SMA_TYPES and t not in ("usb",)]
        for t in list(types) + [t for t in rest if t not in types]:
            if t in labels and t not in placed:
                self._entry(labels[t]["label"], t, t, CATEGORY.get(t), note_of(t))
                placed.add(t)

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
        if self.on_library_menu is None:
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
        key = it.data(Qt.ItemDataRole.UserRole)       # relay 018: a cable's key already carries its prefix
        md.setData(MIME, key.encode())
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
class CardShadow(QWidget):
    """Relay 016: the floating card's soft shadow, at the overlay level. A sibling drawn just under the card from one
    cached picture (layered rounded rectangles, no blur), so dragging on the grid behind it costs a single
    drawPixmap a frame, where a QGraphicsEffect would blur again on every frame."""
    M = 18                                   # px of shadow round the card

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setStyleSheet("background: transparent;")
        self._pm = None
        self.hide()

    def follow(self, card: QWidget):
        dy = T.CARD_SHADOW["dy"]
        self.setGeometry(card.geometry().adjusted(-self.M, -self.M + dy, self.M, self.M + dy))
        self.stackUnder(card)

    def paintEvent(self, ev):
        dpr = self.devicePixelRatioF()
        key = (self.width(), self.height(), dpr)
        if self._pm is None or self._pm[0] != key:
            pm = QPixmap(int(self.width() * dpr), int(self.height() * dpr))
            pm.setDevicePixelRatio(dpr)
            pm.fill(Qt.GlobalColor.transparent)
            p = QPainter(pm)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setPen(Qt.PenStyle.NoPen)
            steps = 9
            for k in range(steps):           # outermost first, each a little darker and smaller: a soft fall-off
                inset = self.M * (1 - (k + 1) / steps)
                p.setBrush(T.color("shadow", T.CARD_SHADOW["alpha"] / steps))
                r = QRectF(self.rect()).adjusted(inset, inset, -inset, -inset)
                p.drawRoundedRect(r, T.RADIUS + inset, T.RADIUS + inset)
            p.end()
            self._pm = (key, pm)
        qp = QPainter(self)
        qp.drawPixmap(0, 0, self._pm[1])
        qp.end()


class EditCard(QFrame):
    def __init__(self, screen: "ChainScreen", parent):
        super().__init__(parent)
        self.screen = screen
        self.shadow = CardShadow(parent)
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
        self._hold = None              # relay 018: the wire whose card is rebuilt in place (no hide, no flicker)
        self._anim = None
        self.hide()

    # the shadow follows the card wherever it is placed, and shows and hides with it (relay 016)
    def moveEvent(self, ev):
        super().moveEvent(ev)
        self.shadow.follow(self)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self.shadow.follow(self)

    def showEvent(self, ev):
        super().showEvent(ev)
        self.shadow.follow(self)
        self.shadow.show()
        self.raise_()

    def hideEvent(self, ev):
        super().hideEvent(ev)
        self.shadow.hide()

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

    WIDTHS = (300, 360, 420, 480)      # relay 018: a card widens before it would ever need a scroll bar

    def _need(self, width: int) -> int:
        """The height the card's contents need at this width (wrapped text counted at that width)."""
        lay = self.inner.layout()
        inner_w = width - 2
        h = lay.totalHeightForWidth(inner_w) if lay.hasHeightForWidth() else lay.totalSizeHint().height()
        return max(h, lay.totalMinimumSize().height()) + 2

    def _target(self) -> int:
        """Relay 018: no scroll bar and no cut-off text. The card is as tall as what it holds; where the grid is too
        short for that, it widens (text wraps into fewer lines) until it fits. Only a card taller than the grid even
        at its widest falls back to scrolling. Returns the height and sets the width."""
        room = self.screen.card_room()
        self.inner.layout().activate()
        least = self.inner.layout().totalMinimumSize().width() + 4      # what its widest row cannot do without
        for wd in self.WIDTHS:
            if wd < least and wd != self.WIDTHS[-1]:
                continue
            need = self._need(wd)
            if need <= room:
                self.setFixedWidth(wd)
                self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
                return max(120, need)
        self.setFixedWidth(self.WIDTHS[-1])
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        return max(120, room)

    def _fit(self):
        if self._anim is not None:
            self._anim.stop()
            self._anim = None
        self.setFixedHeight(self._target())

    def _ease_to(self, old_h: int):
        """Relay 018: after an in-place rebuild, the card eases from its old height to the new one (about 120 ms);
        the scroll bar stays off throughout, so none flashes up mid-way."""
        new_h = self._target()
        bar = self.scroll.verticalScrollBarPolicy()
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        if self._anim is not None:
            self._anim.stop()
        if old_h == new_h or old_h <= 0:
            self.setFixedHeight(new_h)
            self.scroll.setVerticalScrollBarPolicy(bar)
            return
        anim = QVariantAnimation(self)
        anim.setDuration(120)
        anim.setStartValue(int(old_h))
        anim.setEndValue(int(new_h))
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.valueChanged.connect(lambda v: self.setFixedHeight(int(v)))

        def done():
            self.setFixedHeight(new_h)
            self.scroll.setVerticalScrollBarPolicy(bar)
            self._anim = None
            self.screen._place_card()
        anim.finished.connect(done)
        self._anim = anim
        anim.start()

    def _note(self, text: str):
        lab = QLabel(text)
        lab.setWordWrap(True)
        lab.setProperty("muted", True)
        lab.setFont(T.ui_font(8.5))
        self.lay.addWidget(lab)
        return lab

    _look_open = False                  # relay 018: the card's Look section, folded unless the owner opened it

    def _look_section(self, box: dict, bid: str, shape: bool = True, textpos: bool = True):
        """Relay 018: shape, text position, colour and lock are the format bar's now (one place, for every box
        selected). The card keeps them too, folded under one line, so it stays short enough never to scroll."""
        head = QPushButton(("Look ▾" if self._look_open else "Look ▸") + "   shape, text, colour, lock")
        head.setObjectName("LookToggle")
        head.setToolTip("Also on the format bar above the grid, for every box selected")
        head.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)     # never wider than the card
        head.setFlat(True)
        head.setStyleSheet(f"QPushButton#LookToggle {{ background: transparent; border: none; text-align: left;"
                           f" padding: 4px 0; min-height: 0; color: {T.C['muted']}; }}"
                           f"QPushButton#LookToggle:hover {{ color: {T.C['text']}; }}")
        self.lay.addWidget(head)
        box_w = QWidget()
        box_w.setObjectName("LookBox")
        box_w.setStyleSheet("QWidget#LookBox { background: transparent; }")
        inner = QVBoxLayout(box_w)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(6)
        outer, self.lay = self.lay, inner
        try:
            if shape:
                self._shape_row(box, bid)
            if textpos:
                self._textpos_row(box, bid)
            self._colour_row(box, bid)
            self._row(self._lock_button(box, bid))
        finally:
            self.lay = outer
        self.lay.addWidget(box_w)
        box_w.setVisible(self._look_open)

        def flip():
            EditCard._look_open = not EditCard._look_open
            box_w.setVisible(EditCard._look_open)
            head.setText(("Look ▾" if EditCard._look_open else "Look ▸") + head.text()[6:])
            old = self.height()
            self.inner.adjustSize()
            self._ease_to(old)
        head.clicked.connect(flip)

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
        self._lead_rows(node)
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

    def _lead_rows(self, node: dict):
        """Relay 017: the lead moulded into the antenna (what was drawn as a Pigtail): its length, connector and
        note. It belongs to the antenna, so it is drawn on it and shown in Output, and never reaches the text."""
        lead = R.antenna(node).get("lead") or {}
        nid = node["id"]
        box = QFrame()
        box.setObjectName("CardBox")
        g = QGridLayout(box)
        g.setContentsMargins(10, 8, 10, 8)
        g.setVerticalSpacing(4)
        g.addWidget(E.titled("Built-in lead", "antenna_lead", muted=False), 0, 0, 1, 3)
        n, u = R.split_length(lead.get("length"))
        num = QLineEdit(n)
        num.setPlaceholderText("Length, e.g. 2")
        num.setToolTip("How long the moulded lead is: a number, then its unit")
        unit = QComboBox()
        for k in R.LENGTH_UNITS:
            unit.addItem(k, k)
        unit.setCurrentIndex(R.LENGTH_UNITS.index(u) if u in R.LENGTH_UNITS else 0)
        num.textEdited.connect(lambda v: self.screen.set_lead(nid, "length", R.length_text(v.strip(), unit.currentData())))
        unit.currentIndexChanged.connect(lambda i: self.screen.set_lead(
            nid, "length", R.length_text(num.text().strip(), unit.itemData(i))))
        lab = QLabel("Length")
        lab.setProperty("muted", True)
        g.addWidget(lab, 1, 0)
        g.addWidget(num, 1, 1)
        g.addWidget(unit, 1, 2)
        con = QLineEdit(str(lead.get("connector") or ""))
        con.setPlaceholderText("e.g. SMA male")
        con.setToolTip("The connector at the lead's end")
        con.textEdited.connect(lambda v: self.screen.set_lead(nid, "connector", v.strip() or None))
        lab = QLabel("Connector")
        lab.setProperty("muted", True)
        g.addWidget(lab, 2, 0)
        g.addWidget(con, 2, 1, 1, 2)
        note = NoteEdit(str(lead.get("note") or ""), "Anything about the lead")
        note.edited.connect(lambda v: self.screen.set_lead(nid, "note", v.strip() or None))
        lab = QLabel("Note")
        lab.setProperty("muted", True)
        g.addWidget(lab, 3, 0, Qt.AlignmentFlag.AlignTop)
        g.addWidget(note, 3, 1, 1, 2)
        hint = QLabel("Drawing and Output only: the lead never enters the recorded text.")
        hint.setWordWrap(True)
        hint.setProperty("muted", True)
        g.addWidget(hint, 4, 0, 1, 3)
        g.setColumnStretch(1, 1)
        # relay 018: folded while the antenna has no lead, so the card stays short enough never to scroll
        rows = [g.itemAt(i).widget() for i in range(g.count())
                if g.getItemPosition(i)[0] >= 1 and g.itemAt(i).widget() is not None]
        has = bool(lead)
        tog = QToolButton()
        tog.setObjectName("Tool")
        tog.setAutoRaise(True)
        tog.setToolTip("Show or hide the lead's length, connector and note")
        g.addWidget(tog, 0, 2, Qt.AlignmentFlag.AlignRight)

        def show(on):
            for w_ in rows:
                w_.setVisible(on)
            tog.setText("Hide ▾" if on else ("Show ▸" if has else "Add ▸"))
        show(has or EditCard._lead_open)

        def flip():
            EditCard._lead_open = not rows[0].isVisible()
            old = self.height()
            show(EditCard._lead_open)
            self.inner.adjustSize()
            self._ease_to(old)
        tog.clicked.connect(lambda *_: flip())
        self.lay.addWidget(box)

    _lead_open = False

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
        if item is None and self._hold is not None:
            return                       # relay 018: a wire card being rebuilt in place stays on screen
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
        if R.old_word(p):                    # relay 017: a Jumper or Pigtail part, offered a conversion
            it_ = next((x for x in R.old_words(self.screen.rig) if x["where"] == "part" and x["id"] == node["id"]),
                       None)
            if it_ is not None:
                lab = QLabel(f"\"{'Pigtail' if it_['kind'] == 'pigtail' else 'Jumper'}\" is no longer used. "
                             + ("This lead can become the antenna's built-in lead (drawing and Output only)."
                                if it_.get("merge") else f"It can become: {it_['becomes']}"))
                lab.setWordWrap(True)
                self.lay.addWidget(lab)
                btn = QPushButton("Merge into antenna" if it_.get("merge") else "Convert to Coax / USB cable")
                btn.setToolTip("Changes this rig's recorded text, so the next run records it as changed. "
                               "Undo puts it back.")
                btn.clicked.connect(lambda _=False, x=it_: self.screen.convert_old_item(x))
                self._row(btn, E.info("old_words"))

        def text_edit(key, label):
            e = QLineEdit(str(p.get(key) or ""))
            e.textEdited.connect(lambda v, k=key: self._set(k, v))
            self._field(label, e)

        def note_edit(key, label):          # relay 016: a note wraps in a box that grows with it
            e = NoteEdit(str(p.get(key) or ""))
            e.edited.connect(lambda v, k=key: self._set(k, v))
            self._field(label, e)

        if p["type"] != "attenuator":
            text_edit("name", "Name")
        note_edit("detail", "Note" if p["type"] != R.SPLITTER else "Note (used when a port has no label)")
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
        self._look_section(node, node["id"])
        save = QPushButton("Save to my parts")
        save.setToolTip("Keep this part (kind, name, note, fields, size, figures) in My parts, to drop again "
                        "with the same name and note")
        save.clicked.connect(lambda: self.screen.save_to_library(node))
        out = QPushButton("Take out of rig")
        out.setToolTip("Removes this part from the drawing. Nothing is saved until Save.")
        out.clicked.connect(self.screen.take_out_selected)
        self._row(save, out)
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
            e = QLineEdit(str(item.box.get(key) or "")) if key == "name" else NoteEdit(str(item.box.get(key) or ""))
            (e.textEdited if key == "name" else e.edited).connect(lambda v, k=key: self._power_set(k, v))
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
        self._look_section(item.box, item.box["id"])
        out = QPushButton("Take out of rig")
        out.setToolTip("Removes this power source and its power wires from the drawing. Nothing is saved until Save.")
        out.clicked.connect(self.screen.take_out_selected)
        self._row(out)
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
            e = QLineEdit(str(item.enc.get(key) or "")) if key == "name" else NoteEdit(str(item.enc.get(key) or ""))
            (e.textEdited if key == "name" else e.edited).connect(lambda v, k=key: self._enc_set(k, v))
            self._field(label, e)
        hint = QLabel("Drawing only: an enclosure writes nothing into the recorded text. "
                      "Parts inside it move with it, and so do their groups; drag one out to take it "
                      "out. Right-click it to bring it in front of parts or send it back.")
        hint.setWordWrap(True)
        hint.setProperty("muted", True)
        hint.setFont(T.ui_font(8.5))
        self.lay.addWidget(hint)
        self._look_section(item.enc, item.enc["id"], shape=False, textpos=False)
        out = QPushButton("Take out of rig")
        out.setToolTip("Removes the box only; the parts in it stay. Nothing is saved until Save.")
        out.clicked.connect(self.screen.take_out_selected)
        self._row(out)
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

    def show_for_wire(self, item: "WireItem", smooth: bool = False):
        """The card for a wire: the cables it carries, in order from its start. smooth (relay 018): rebuilt in place
        on screen after an edit on it (a cable's type, a cable or choke added or removed), with updates held so
        nothing flickers, then eased to its new height."""
        smooth = smooth and self.isVisible()
        old_h = self.height()
        if smooth:
            self.setUpdatesEnabled(False)
        else:
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
            self._finish(smooth, old_h)
            return
        cables = self.wire.get("cables") or []
        if not cables:
            note = QLabel("No cable: the two parts join directly (screwed together, or through a bulkhead).")
            note.setProperty("muted", True)
            note.setWordWrap(True)
            self.lay.addWidget(note)
        self._rec_labels = {}
        for i, c in enumerate(cables):
            self.lay.addWidget(self._cable_box(i, c))
        add = QHBoxLayout()
        for t in R.CABLE_TYPES:
            btn = QPushButton("+ " + {"coax": "Coax", "usb": "USB"}[t])
            btn.setToolTip(f"Add a {'Coax' if t == 'coax' else 'USB'} cable to this connection, after any already "
                           "on it")
            btn.clicked.connect(lambda _=False, t=t: self._cable_add(t))
            add.addWidget(btn)
        ch = QPushButton("+ Choke")
        ch.setEnabled(bool(cables))
        ch.setToolTip("Put a choke on this wire's last cable, half way along; drag the bead to move it"
                      if cables else "A choke sits on a cable: add a Coax or USB cable first")
        ch.clicked.connect(lambda: self._choke_add(len(self.wire.get("cables") or []) - 1))
        add.addWidget(ch)
        self.lay.addLayout(add)
        out = QPushButton("Take wire out")
        out.setToolTip("Removes this wire (and its cables) from the drawing. Nothing is saved until Save.")
        out.clicked.connect(self.screen.take_out_selected)
        self.lay.addWidget(out)
        self._finish(smooth, old_h)

    def _finish(self, smooth: bool, old_h: int):
        if smooth:
            for w in self.inner.findChildren(QWidget):     # widgets added to a shown card start hidden
                if not w.isHidden() or w.parent() is None:
                    continue
                if not w.testAttribute(Qt.WidgetAttribute.WA_WState_ExplicitShowHide):
                    w.show()
            self.inner.adjustSize()
            self._ease_to(old_h)
            self.setUpdatesEnabled(True)
            self.raise_()
            return
        self._fit()
        self.show()
        self.raise_()

    def _cable_box(self, i: int, c: dict) -> QFrame:
        """One cable on the wire's card (relay 017). A cable drawn since is Coax or USB with a length and, for
        coax, a type; its note reaches the text only when ticked. A cable saved before keeps its own name and
        note as saved; one in the old words (Jumper, Pigtail) offers to convert, and changes nothing until then."""
        box = QFrame()
        box.setObjectName("CardBox")
        box.setStyleSheet(f"QFrame#CardBox {{ border: 1px solid {cable_colour(c.get('type')).name()};"
                          f" border-radius: 8px; }}")
        bl = QGridLayout(box)
        bl.setContentsMargins(10, 8, 10, 8)
        bl.setHorizontalSpacing(6)
        bl.setVerticalSpacing(4)
        rm = QPushButton("Remove")
        rm.setToolTip("Take this cable off the wire (its chokes go with it)")
        rm.clicked.connect(lambda _=False, i=i: self._cable_remove(i))
        row = 0

        def label(text, key=None):
            if key:
                return E.titled(text, key)
            lab = QLabel(text)
            lab.setProperty("muted", True)
            return lab

        if c.get("auto"):
            kind = QComboBox()
            for t in R.CABLE_TYPES:
                kind.addItem("Coax" if t == "coax" else "USB cable", t)
            kind.setCurrentIndex(R.CABLE_TYPES.index(c["type"]) if c.get("type") in R.CABLE_TYPES else 0)
            kind.currentIndexChanged.connect(lambda k, i=i, kind=kind: self._cable_type(i, kind.itemData(k)))
            E.attach(kind, "cable_coax" if c.get("type") == "coax" else "cable_usb")
            bl.addWidget(kind, row, 0, 1, 2)
            bl.addWidget(E.info("cable_coax" if c.get("type") == "coax" else "cable_usb"), row, 2)
            bl.addWidget(rm, row, 3)
            row += 1
            n, u = R.split_length(c.get("length"))
            num = QLineEdit(n)
            num.setPlaceholderText("e.g. 3")
            num.setToolTip("The cable's length: a number, then its unit beside it")
            unit = QComboBox()
            for k in R.LENGTH_UNITS:
                unit.addItem(k, k)
            unit.setCurrentIndex(R.LENGTH_UNITS.index(u) if u in R.LENGTH_UNITS else 0)
            num.textEdited.connect(lambda v, i=i, unit=unit: self._cable_length(i, v, unit.currentData()))
            unit.currentIndexChanged.connect(lambda k, i=i, num=num, unit=unit:
                                             self._cable_length(i, num.text(), unit.itemData(k)))
            bl.addWidget(label("Length"), row, 0)
            bl.addWidget(num, row, 1)
            bl.addWidget(unit, row, 2, 1, 2)
            row += 1
            if c.get("type") == "coax":
                ct = QLineEdit(str(c.get("coax_type") or ""))
                ct.setPlaceholderText("e.g. RG316, RG58")
                ct.setToolTip("The coax type, as printed on the cable. Free text; it goes into the recorded text.")
                ct.textEdited.connect(lambda v, i=i: self._cable_set(i, "coax_type", v))
                bl.addWidget(label("Coax type"), row, 0)
                bl.addWidget(ct, row, 1, 1, 3)
                row += 1
            note = NoteEdit(str(c.get("note") or ""), "Connector genders, S-bend, maker…")
            note.edited.connect(lambda v, i=i: self._cable_set(i, "note", v))
            bl.addWidget(label("Note"), row, 0, Qt.AlignmentFlag.AlignTop)
            bl.addWidget(note, row, 1, 1, 3)
            row += 1
            inc = QCheckBox("Include the note in the recorded text")
            inc.setChecked(bool(c.get("note_in_text")))
            inc.toggled.connect(lambda on, i=i: self._cable_note_in_text(i, on))
            bl.addWidget(E.with_info(inc, "cable_note_in_text"), row, 0, 1, 4)
            row += 1
        else:
            head = QLabel(R.cable_text(c))
            head.setWordWrap(True)
            head.setToolTip("As saved before November 2026: its text is kept exactly until you change it")
            bl.addWidget(head, row, 0, 1, 3)
            bl.addWidget(rm, row, 3)
            row += 1
            old = R.old_word(c)
            if old:
                bl.addWidget(self._old_offer(i, c, old), row, 0, 1, 4)
                row += 1

            def edit(key, hint, i=i, c=c):
                e = QLineEdit(str(c.get(key) or ""))
                e.setPlaceholderText(hint)
                e.setToolTip(hint)
                e.textEdited.connect(lambda v, i=i, k=key: self._cable_set(i, k, v))
                return e

            bl.addWidget(edit("name", "Name (goes into the recorded text)"), row, 0, 1, 3)
            bl.addWidget(edit("length", "Length"), row, 3)
            row += 1
            bl.addWidget(edit("detail", "Note (goes into the text in brackets)"), row, 0, 1, 4)
            row += 1
            if not old:
                use = QPushButton("Write from length and type")
                use.setToolTip("Rewrite this cable the new way: Coax or USB with a length, its text made from those. "
                               "Its saved text changes, so the next run records the rig as changed. Undo puts it back.")
                use.clicked.connect(lambda _=False, i=i: self._cable_modernise(i))
                bl.addWidget(use, row, 0, 1, 4)
                row += 1
        for k, ch in enumerate(c.get("chokes") or []):
            r = QHBoxLayout()
            r.setSpacing(4)
            dot = QLabel()
            dot.setPixmap(kind_pixmap(R.CHOKE, cable_colour(c.get("type")), 14, self.devicePixelRatioF()))
            r.addWidget(dot)
            e = QLineEdit(str(ch.get("note") or ""))
            e.setPlaceholderText("Choke note, e.g. ferrite, 3 turns")
            e.setToolTip("Drawing only: a choke's note never reaches the recorded text")
            e.textEdited.connect(lambda v, i=i, k=k: self._choke_note(i, k, v))
            r.addWidget(e, 1)
            x = QPushButton("Remove")
            x.setToolTip("Take this choke off the cable (drawing only)")
            x.clicked.connect(lambda _=False, i=i, k=k: self._choke_remove(i, k))
            r.addWidget(x)
            bl.addLayout(r, row, 0, 1, 4)
            row += 1
        bl.addWidget(self.number_edit(c.get("gain_db"), lambda v, i=i: self._cable_gain(i, v),
                                      "Nominal loss, dB, e.g. -1.2 (drawing only: for the estimate)"), row, 0, 1, 4)
        row += 1
        rec = QLabel()
        rec.setObjectName("CableText")
        rec.setProperty("muted", True)
        rec.setWordWrap(True)
        rec.setText("Recorded as: " + R.cable_text(c))
        bl.addWidget(rec, row, 0, 1, 4)
        self._rec_labels[i] = rec
        bl.setColumnStretch(1, 3)
        return box

    def _old_offer(self, i: int, c: dict, old: str) -> QFrame:
        """The gentle offer on a cable in the old words: what it would become, and one button. Nothing changes
        until the owner presses it."""
        f = QFrame()
        f.setObjectName("OldWords")
        f.setStyleSheet(f"QFrame#OldWords {{ border: 1px solid {T.C['lines']}; border-radius: 6px; }}")
        v = QVBoxLayout(f)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(4)
        item = next((x for x in R.old_words(self.screen.rig)
                     if x["where"] == "cable" and x["wire"] == (self.wire["from"], self.wire["port"])
                     and x["i"] == i), None)
        word = "Pigtail" if old == "pigtail" else "Jumper"
        merge = bool(item and item.get("merge"))
        text = (f"\"{word}\" is no longer a cable type. "
                + ("This lead is moulded into the antenna, so it can become the antenna's built-in lead "
                   "(drawing and Output only). The recorded text then drops it."
                   if merge else f"It can become: {item['becomes'] if item else ''}"))
        lab = QLabel(text)
        lab.setWordWrap(True)
        v.addWidget(lab)
        btn = QPushButton("Merge into antenna" if merge else "Convert to Coax / USB cable")
        btn.setToolTip("Changes this rig's recorded text, so the next run records it as changed. Undo puts it back.")
        btn.clicked.connect(lambda _=False, it=item: self.screen.convert_old_item(it))
        r = QHBoxLayout()
        r.addWidget(btn)
        r.addWidget(E.info("old_words"))
        r.addStretch(1)
        v.addLayout(r)
        return f

    def _cable_redo_text(self, i):
        c = self.wire["cables"][i]
        R.sync_cable(c)
        lab = self._rec_labels.get(i)
        if lab is not None:
            lab.setText("Recorded as: " + R.cable_text(c))

    def _cable_length(self, i, number, unit):
        c = self.wire["cables"][i]
        n = str(number or "").strip().replace(",", ".")
        ln = R.length_text(n, unit)
        if ln:
            c["length"] = ln
        else:
            c.pop("length", None)
        self._cable_redo_text(i)
        self.screen.changed(redraw=False, merge=("cable", self.wire["from"], self.wire["port"], i, "length"))
        self.screen._refresh_wires()

    def _cable_note_in_text(self, i, on):
        c = self.wire["cables"][i]
        if on:
            c["note_in_text"] = True
        else:
            c.pop("note_in_text", None)
        self._cable_redo_text(i)
        self.screen.changed(redraw=False)

    def _cable_modernise(self, i):
        self.wire["cables"][i] = R.converted_cable(self.wire["cables"][i])
        self._wire_changed()

    def _choke_add(self, i):
        if i < 0:
            return
        c = self.wire["cables"][i]
        k = len(c.get("chokes") or [])            # spread out, clear of the cable's label in the middle
        c.setdefault("chokes", []).append({"at": (0.3, 0.7, 0.15, 0.85, 0.5)[k % 5]})
        R.sync_cable(c)
        self._wire_changed()

    def _choke_note(self, i, k, v):
        ch = self.wire["cables"][i]["chokes"][k]
        if v.strip():
            ch["note"] = v
        else:
            ch.pop("note", None)
        self.screen.changed(redraw=False, merge=("choke", self.wire["from"], self.wire["port"], i, k))

    def _choke_remove(self, i, k):
        R.remove_choke(self.screen.rig, self.wire["from"], self.wire["port"], i, k)
        self._wire_changed()

    def _cable_set(self, i, key, value):
        c = self.wire["cables"][i]
        if value in (None, "") and key in ("length", "coax_type", "note"):
            c.pop(key, None)
        else:
            c[key] = value
        self._cable_redo_text(i)
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
        if c.get("auto"):                        # relay 017: the text follows from the fields
            c["type"] = type_
            R.sync_cable(c)
            self._wire_changed()
            return
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
        """An edit on the wire's card that changes what the card shows: the drawing is redrawn, the wire stays
        selected, and the card is rebuilt in place, never hidden and shown again (relay 018: no flicker)."""
        key = (self.wire["from"], self.wire["port"])
        self._hold = key
        try:
            self.screen.changed()
            self.screen.select_wire(*key)
            wi = self.screen.wire_item(*key)
            if wi is not None:
                self.show_for_wire(wi, smooth=True)
        finally:
            self._hold = None
        if self.screen.wire_item(*key) is None:
            self.show_for(None)
        self.screen._place_card()

    def _set(self, key, value):
        if self.node is None:
            return
        if value in (None, ""):
            self.node["part"].pop(key, None) if key in R.FIELDS else self.node["part"].__setitem__(key, "")
        else:
            self.node["part"][key] = value
        if key in ("name", "detail", "length"):          # relay 016: the part grows to fit its words, room allowing
            self.screen.fit_text(self.node["id"])
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
# ---------------------------------------------------------------- relay 018: the blank start, the minimap, the footer
class StartPanel(QFrame):
    """What the grid shows when no rig is open (the Chain screen opens blank, relay 018): New rig, Open rig… and the
    last three rigs worked on. Sits in the middle of the grid, like the empty-grid hint, but can be clicked."""

    def __init__(self, screen: "ChainScreen", parent):
        super().__init__(parent)
        self.screen = screen
        self.setObjectName("StartPanel")
        self.setFixedWidth(440)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(T.SPACE * 3, T.SPACE * 3, T.SPACE * 3, T.SPACE * 3)
        lay.setSpacing(T.SPACE)
        t = QLabel("No rig is open")
        t.setFont(T.ui_font(13, QFont.Weight.DemiBold))
        lay.addWidget(t)
        s = QLabel("Start a new rig, or open one you have drawn. Nothing on the grid changes a saved rig until Save.")
        s.setProperty("muted", True)
        s.setWordWrap(True)
        lay.addWidget(s)
        row = QHBoxLayout()
        row.setSpacing(T.SPACE)
        self.btn_new = QToolButton()
        self.btn_new.setObjectName("Primary")
        self.btn_new.setText("New rig")
        self.btn_new.setIcon(tool_icon("new", "background"))
        self.btn_new.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.btn_new.setFixedHeight(T.TOOL_H)
        self.btn_new.clicked.connect(lambda *_: screen.new_rig())
        self.btn_open = QToolButton()
        self.btn_open.setObjectName("Tool")
        self.btn_open.setText("Open rig…")
        self.btn_open.setIcon(tool_icon("rig"))
        self.btn_open.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.btn_open.setFixedHeight(T.TOOL_H)
        self.btn_open.setToolTip("Every rig, with the one used for runs marked (Ctrl+O)")
        self.btn_open.clicked.connect(lambda *_: screen.open_rig_dialog())
        row.addWidget(self.btn_new)
        row.addWidget(self.btn_open)
        row.addStretch(1)
        lay.addLayout(row)
        self.recent_title = QLabel("RECENT")
        self.recent_title.setProperty("muted", True)
        self.recent_title.setFont(T.ui_font(7.5, QFont.Weight.DemiBold))
        self.recent_title.setContentsMargins(0, T.SPACE, 0, 0)
        lay.addWidget(self.recent_title)
        self.recent_box = QVBoxLayout()
        self.recent_box.setSpacing(4)
        lay.addLayout(self.recent_box)
        self.recent_buttons: list = []

    def refresh(self):
        while self.recent_box.count():
            w = self.recent_box.takeAt(0).widget()
            if w:
                w.hide()
                w.deleteLater()
        self.recent_buttons = []
        names = self.screen.recent_rigs()[:3]
        used = self.screen.used_rig_name()
        for n in names:
            d = self.screen.display.get(n, {})
            b = QPushButton(n + ("   ·   used for runs" if n == used else "")
                            + ("   ·   archived" if d.get("archived") else ""))
            b.setObjectName("RecentRig")
            b.setToolTip(f"Open “{n}”")
            b.setStyleSheet("text-align: left;")
            b.clicked.connect(lambda _=False, n=n: self.screen.open_named(n))
            self.recent_box.addWidget(b)
            self.recent_buttons.append(b)
        self.recent_title.setVisible(bool(names))
        if not names:
            q = QLabel("The rigs you open will be listed here.")
            q.setProperty("muted", True)
            q.setFont(T.ui_font(8.5))
            self.recent_box.addWidget(q)
        self.adjustSize()


class Minimap(QWidget):
    """Relay 018, View > Minimap: a small overview of the whole rig in the grid's bottom-right corner, with the part
    of it on screen outlined. Click or drag in it to jump there. Off by default; drawn from the scene, cached until
    the drawing changes."""
    W, H = 220, 140

    def __init__(self, screen: "ChainScreen", view: QGraphicsView):
        super().__init__(view)
        self.screen, self.view = screen, view
        self.setObjectName("Minimap")
        self.setFixedSize(self.W, self.H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("The whole rig: click or drag to jump there (View > Minimap turns it off)")
        self._pm = None
        self._src = QRectF()
        self._stale = True
        self.hide()

    def invalidate(self):
        self._stale = True
        if self.isVisible():
            self.update()

    def place(self):
        vp = self.view.viewport().geometry()
        self.move(vp.right() - self.W - 10, vp.bottom() - self.H - 10)
        self.raise_()

    def _render(self):
        src = QRectF()
        for it in self.screen.all_box_items() + self.screen.wire_items:
            src = src.united(it.sceneBoundingRect())
        if src.isNull():
            src = QRectF(0, 0, 600, 300)
        src = src.adjusted(-60, -60, 60, 60)
        dpr = self.devicePixelRatioF()
        pm = QPixmap(int(self.W * dpr), int(self.H * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        k = min((self.W - 12) / src.width(), (self.H - 12) / src.height())
        w, h = src.width() * k, src.height() * k
        target = QRectF((self.W - w) / 2, (self.H - h) / 2, w, h)
        self.screen.scene.render(p, target, src)
        p.end()
        self._pm, self._src, self._target, self._stale = pm, src, target, False

    def _to_scene(self, pt: QPointF) -> QPointF:
        t, s = self._target, self._src
        return QPointF(s.left() + (pt.x() - t.left()) / max(t.width(), 1) * s.width(),
                       s.top() + (pt.y() - t.top()) / max(t.height(), 1) * s.height())

    def paintEvent(self, ev):
        if self._stale or self._pm is None:
            self._render()
        qp = QPainter(self)
        qp.setRenderHint(QPainter.RenderHint.Antialiasing)
        body = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        qp.setPen(QPen(T.color("line_strong"), 1))
        qp.setBrush(T.color("elev_overlay", 0.94))
        qp.drawRoundedRect(body, 8, 8)
        qp.drawPixmap(0, 0, self._pm)
        vis = self.view.mapToScene(self.view.viewport().rect()).boundingRect()
        t, s = self._target, self._src
        k = t.width() / max(s.width(), 1)
        r = QRectF(t.left() + (vis.left() - s.left()) * k, t.top() + (vis.top() - s.top()) * k,
                   vis.width() * k, vis.height() * k).intersected(body.adjusted(2, 2, -2, -2))
        qp.setPen(QPen(T.color("survey"), 1.5))
        qp.setBrush(T.color("survey", 0.10))
        qp.drawRect(r)
        qp.end()

    def mousePressEvent(self, ev):
        self.view.centerOn(self._to_scene(ev.position()))
        self.update()

    def mouseMoveEvent(self, ev):
        if ev.buttons() & Qt.MouseButton.LeftButton:
            self.view.centerOn(self._to_scene(ev.position()))
            self.update()


class ChecksFooter(QFrame):
    """Relay 018: the Checks, as a footer along the bottom of the Chain screen. A count of each kind in its colour
    (problem: alert; warning: anomaly; note: muted), the first message beside them, and the whole list on a click,
    where a click on a message selects the parts it is about. "No problems found" is quiet, in the muted colour."""
    COLOURS = {"problem": "alert", "warning": "anomaly", "note": "muted"}
    WORDS = {"problem": ("problem", "problems"), "warning": ("warning", "warnings"), "note": ("note", "notes")}

    def __init__(self, screen: "ChainScreen"):
        super().__init__()
        self.screen = screen
        self.setObjectName("Footer")
        h = QHBoxLayout(self)
        h.setContentsMargins(T.SPACE * 2, 4, T.SPACE * 2, 4)
        h.setSpacing(T.SPACE)
        self.title = QLabel("Checks")
        self.title.setFont(T.ui_font(8.5, QFont.Weight.DemiBold))
        h.addWidget(self.title)
        self.counts = {}
        for lv in R.CHECK_LEVELS:
            c = QToolButton()
            c.setObjectName("CheckCount")
            c.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            c.setIcon(dot_icon(T.color(self.COLOURS[lv])))
            c.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            c.clicked.connect(lambda _=False, lv=lv: self.show_all(lv))
            self.counts[lv] = c
            h.addWidget(c)
        self.first = ElidedLabel("", "")
        self.first.setObjectName("FirstCheck")
        self.first.setCursor(Qt.CursorShape.PointingHandCursor)
        self.first.mousePressEvent = lambda ev: self._first_clicked()
        h.addWidget(self.first, 1)
        self.all_btn = QToolButton()
        self.all_btn.setObjectName("Tool")
        self.all_btn.setText("All checks ▴")
        self.all_btn.setToolTip("Every check; click one to select what it is about")
        self.all_btn.clicked.connect(lambda *_: self.show_all(None))
        h.addWidget(self.all_btn)
        self.items: list = []
        self.setFixedHeight(32)

    def show_checks(self, items: list, empty_text: str):
        self.items = items
        counts = R.check_counts(items)
        for lv, btn in self.counts.items():
            n = counts[lv]
            btn.setVisible(n > 0)
            btn.setText(f"{n} {self.WORDS[lv][0 if n == 1 else 1]}")
            btn.setStyleSheet(f"QToolButton#CheckCount {{ background: transparent; border: none; padding: 2px 4px;"
                              f" color: {T.C['alert_text'] if lv == 'problem' else T.C[self.COLOURS[lv]]};"
                              f" font-weight: 600; }} QToolButton#CheckCount:hover {{ color: {T.C['text']}; }}")
            btn.setToolTip(f"Show the {self.WORDS[lv][1]}")
        self.all_btn.setVisible(bool(items))
        if not items:
            self.first.setText(empty_text)
            self.first.setStyleSheet(f"color: {T.C['muted']};")
            self.first.setToolTip("")
            self.title.setStyleSheet(f"color: {T.C['muted']};")
            return
        c = items[0]
        self.first.setText(c["text"])
        self.first.setStyleSheet(f"color: {T.C['text'] if c['level'] != 'note' else T.C['muted']};")
        self.first.setToolTip(c["text"] + "\n\nClick to select it. A check warns; it never stops a save.")
        self.title.setStyleSheet(f"color: {T.C['text']};")

    def _first_clicked(self):
        if self.items:
            self.screen.select_ids(self.items[0]["ids"])

    def show_all(self, level: str | None):
        if not self.items:
            return
        m = Menu(self)
        for lv in R.CHECK_LEVELS:
            these = [c for c in self.items if c["level"] == lv and (level is None or level == lv)]
            if not these:
                continue
            m.add_heading(self.WORDS[lv][1])
            for c in these:
                a = m.addAction(dot_icon(T.color(self.COLOURS[lv])), c["text"])
                a.triggered.connect(lambda _=False, ids=c["ids"]: self.screen.select_ids(ids))
        size = m.sizeHint()
        at = self.mapToGlobal(QPoint(self.all_btn.x() + self.all_btn.width() - size.width(), -size.height() - 4))
        self.screen._footer_menu = m
        m.exec(at)


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
        self.menu_point = None         # where the last right-click on the grid was (relay 015, the wire menu)
        self.anchor_map: dict = {}
        self.undo_stack: list = []
        self.redo_stack: list = []
        self._last = None
        self._saved = None
        self._merge = None
        self._load_prefs()
        self.wiring_from = None
        self.sel_point = None          # relay 015: the picked bend point, (wire from, port, index)
        self.reconnecting = None       # relay 015: a wire end being dragged, (wire from, port, "from"|"to")
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
        self.words_gen = 0             # relay 016: bumped by every edit, so each part lays out its words again
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
        # relay 014: clicked(bool) must not reach the slot. It passed checked=False as new_rig's name, so a
        # mouse click on New rig skipped the name prompt and returned at the empty name: nothing happened.
        btn.clicked.connect(lambda *_: slot())
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

        # Relay 018: the header is two calm rows at most. Row 1: the rig picker with its status, the direction
        # switch, Save and Use for runs. Row 2: the format bar, live when something is selected. Undo, Redo, Tidy
        # and the view controls sit on the bar at the grid's top edge; everything else is in the menu bar
        # (File, Edit, View, Arrange, Tools, Help), built by build_menus. The buttons the menus replaced are still
        # made (hidden): their enabled state and their tips are what the menu entries read.
        bar = QFrame()
        bar.setObjectName("Toolbar")
        rows = QVBoxLayout(bar)
        rows.setContentsMargins(T.SPACE * 2, T.SPACE, T.SPACE * 2, T.SPACE)
        rows.setSpacing(T.SPACE - 2)
        b = QHBoxLayout()
        b.setSpacing(T.SPACE * 1.5)
        rows.addLayout(b)
        top = Qt.AlignmentFlag.AlignVCenter
        lab = QLabel()
        lab.setPixmap(tool_icon("rig", "muted").pixmap(16, 16))
        lab.setToolTip("Rig: the drawing on the grid")
        self.pick = QComboBox()
        self.pick.setObjectName("RigPick")
        self.pick.setMinimumWidth(240)
        self.pick.setFixedHeight(T.TOOL_H - 4)
        self.pick.setPlaceholderText("No rig open")
        self.pick.setToolTip("The rig shown on the grid. Each saved rig, and each chain the web page made, is "
                             "listed; archived rigs are listed apart, under Archived. File > Open rig… shows the "
                             "same list with the rig used for runs marked.")
        self.pick.activated.connect(self._picked)
        self.state = QLabel()
        self.state.setObjectName("RigState")
        self.state.setProperty("muted", True)
        self.state.setFont(T.ui_font(8.5))
        self.state.setTextFormat(Qt.TextFormat.PlainText)
        self.state.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.state.setToolTip("This rig's state: saved or not, when it last ran, static or not")
        self.pill = QLabel()
        self.pill.setObjectName("Pill")
        rig_seg = QFrame()
        rig_seg.setObjectName("Segment")
        rig_seg.setAccessibleName("rig")
        rh = QHBoxLayout(rig_seg)
        rh.setContentsMargins(T.SPACE // 2, T.SPACE // 2, T.SPACE, T.SPACE // 2)
        rh.setSpacing(T.SPACE)
        rh.addWidget(lab)
        rh.addWidget(self.pick)
        rh.addWidget(self.pill)
        rh.addWidget(self.state, 1)
        b.addWidget(rig_seg, 1, top)
        # The menu bar's actions, kept as buttons nobody sees (refresh_side keeps their state; older window checks
        # press them). None of them is in a layout.
        self.btn_new = self._tool("New rig", "Start a new rig: blank, or from a template", self.new_rig, icon="new")
        self.btn_rename = self._tool("Rename", "Give this rig a new name", self.rename_rig, icon="rename")
        self.btn_rig = self._tool("Rig", "History of this rig's saves; archive or restore it", lambda: None,
                                  icon="more")
        self.btn_rig.setText("Rig ▾")
        self.btn_rig.setMenu(self._rig_menu())
        self.btn_rig.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.chk_static = QCheckBox("Static")
        self.chk_static.setToolTip("A static rig is never questioned before a run: it does not change.")
        self.chk_static.clicked.connect(self._static_clicked)
        self.btn_picture = self._tool("Picture…", "Save the drawing as a PNG picture, to a place you pick",
                                      lambda: self.export_picture(), icon="picture")
        self.btn_help = self._tool("Help", "Keyboard shortcuts (F1)", self.show_help, icon="help", label=False)
        self.btn_clear = self._tool("Clear grid", "Take every part and wire off the grid. Undo brings them back; "
                                    "nothing is saved until Save.", self.clear_grid, icon="clear", kind="Danger")
        self._kept = QWidget(self)               # their parent, never shown
        self._kept.hide()
        for w in (self.btn_new, self.btn_rename, self.btn_rig, self.chk_static, self.btn_picture, self.btn_help,
                  self.btn_clear):
            w.setParent(self._kept)
        # Relay 016: the rig's layout direction. Changing it moves nothing until Tidy; Undo covers it.
        self.btn_dir = self._tool("Left → right", "The rig's direction: left to right, or top to bottom. New wires "
                                  "leave from the output side and arrive on the input side; Tidy lays the rig out "
                                  "that way. Changing it moves nothing until you press Tidy.", lambda: None,
                                  icon="dir_right")
        dm = Menu(self)
        self.dir_actions = {}
        grp = QActionGroup(dm)
        for key, text in R.DIRECTIONS.items():
            a = QAction(tool_icon("dir_" + key), text, dm, checkable=True)
            a.triggered.connect(lambda _=False, k=key: self.set_direction(k))
            grp.addAction(a)
            dm.addAction(a)
            self.dir_actions[key] = a
        self.btn_dir.setMenu(dm)
        self.btn_dir.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        b.addWidget(self.btn_dir, 0, top)
        self.btn_save = self._tool("Save", "Write this rig to chain.json through the service (Ctrl+S)", self.save,
                                   icon="save", kind="Primary")
        self.btn_use = self._tool("Use for runs", "", self.use_for_runs, icon="use", kind="Primary")
        b.addWidget(self.btn_save, 0, top)
        b.addWidget(self.btn_use, 0, top)

        # Row 2, the format bar: what the selection looks like. Stays in place when nothing is selected (a quiet
        # line says so), so the grid never jumps up and down as the selection changes.
        self.format_bar = QFrame()
        self.format_bar.setObjectName("FormatBar")
        fb = QHBoxLayout(self.format_bar)
        fb.setContentsMargins(0, 0, 0, 0)
        fb.setSpacing(T.SPACE // 2)
        self.fmt_title = QLabel("FORMAT")
        self.fmt_title.setProperty("muted", True)
        self.fmt_title.setFont(T.ui_font(7.5, QFont.Weight.DemiBold))
        fb.addWidget(self.fmt_title)
        fb.addSpacing(T.SPACE // 2)
        self.fmt_size = QComboBox()
        self.fmt_size.setObjectName("FontSize")
        self.fmt_size.addItem("Standard size", None)
        for fs in R.FONT_SIZES:
            self.fmt_size.addItem(f"{fs} pt", fs)
        self.fmt_size.setToolTip("The selected parts' font size (drawing only; kept in My parts). Standard is 10 pt.")
        self.fmt_size.activated.connect(lambda i: self.set_font(self.selected_boxes(), self.fmt_size.itemData(i)))
        self.fmt_size.setFixedHeight(T.TOOL_H - 6)
        fb.addWidget(self.fmt_size)
        self.fmt_bold = self._tool("B", "Bold: the selected parts' names in a heavier weight (drawing only)",
                                   lambda: self.set_bold(self.selected_boxes(), self.fmt_bold.isChecked()), label=True)
        self.fmt_bold.setCheckable(True)
        f = self.fmt_bold.font()
        f.setBold(True)
        self.fmt_bold.setFont(f)
        self.fmt_bold.setFixedWidth(T.TOOL_H)
        self.fmt_pos = self._tool("Text ▾", "Where the words sit in the box: 3 × 3", lambda: None, icon="textpos")
        self.fmt_pos.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.fmt_pos.setMenu(self._pos_menu())
        self.fmt_colour = self._tool("Colour ▾", "The selection's own colour, or back to its kind's", lambda: None,
                                     icon="colour")
        self.fmt_colour.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        cm = Menu(self)
        cm.setToolTipsVisible(True)
        cm.aboutToShow.connect(lambda: (cm.clear(), box_colour_menu(cm, self, self.selected_boxes()),
                                        self._flatten(cm)))
        self.fmt_colour.setMenu(cm)
        self.fmt_shape = self._tool("Shape ▾", "The selected parts' outline", lambda: None, icon="shape")
        self.fmt_shape.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        sm = Menu(self)
        sm.aboutToShow.connect(lambda: self._fill_shape_menu(sm))
        self.fmt_shape.setMenu(sm)
        self.fmt_layer = self._tool("Layer ▾", "Bring to front, forward, backward or to the back", lambda: None,
                                    icon="arrange")
        self.fmt_layer.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        lm = Menu(self)
        for how, text, keys in self.ARRANGE_ITEMS:
            a = lm.addAction(f"{text}\t{keys}")
            a.triggered.connect(lambda _=False, h=how: self.arrange(h))
        self.fmt_layer.setMenu(lm)
        self.fmt_lock = self._tool("Lock", "Lock or unlock the selection: a locked box is never moved by accident "
                                   "(Ctrl+Shift+L)", self.toggle_lock, icon="lock")
        self.fmt_lock.setCheckable(True)
        self.fmt_widgets = [self.fmt_size, self.fmt_bold, self.fmt_pos, self.fmt_colour, self.fmt_shape,
                            self.fmt_layer, self.fmt_lock]
        fb.addWidget(self._segment(self.fmt_bold, self.fmt_pos, self.fmt_colour, self.fmt_shape, self.fmt_layer,
                                   self.fmt_lock, name="format"))
        self.fmt_hint = QLabel("Select a part, a power source or an enclosure to format it")
        self.fmt_hint.setProperty("muted", True)
        self.fmt_hint.setFont(T.ui_font(8.5))
        fb.addSpacing(T.SPACE)
        fb.addWidget(self.fmt_hint)
        fb.addStretch(1)
        self.format_bar.setFixedHeight(T.TOOL_H + T.SPACE)
        rows.addWidget(self.format_bar)
        root.addWidget(bar)

        # The bar at the grid's top edge: Undo, Redo, Tidy and the view menus, one click away; the panel toggles
        # (Recorded text, Output) at its right end, and Find when it is open.
        self.btn_undo = self._tool("Undo", "Undo (Ctrl+Z)", self.undo, icon="undo", label=False)
        self.btn_redo = self._tool("Redo", "Redo (Ctrl+Y)", self.redo, icon="redo", label=False)
        self.btn_tidy = self._tool("Tidy", "Arrange the rig in its direction: left to right, source on the left and "
                                   "branches one under another; or top to bottom, source at the top and branches side "
                                   "by side. Locked boxes stay; parts make way for a group. Undo puts it back.",
                                   self.tidy, icon="tidy")
        self.menus = {}
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
            self.menus[key] = btn
            if key != "arrange":
                view.append(btn)
            else:
                btn.setParent(self._kept)        # Arrange lives in the menu bar now
        self.btn_text = self._tool("Recorded text", "Show or hide the text each run records for this rig, one line "
                                   "per radio, with the nominal gain estimate", lambda: None, icon="text")
        self.btn_text.setCheckable(True)
        self.btn_text.toggled.connect(self._toggle_text)
        self.btn_out = self._tool("Output", "Show or hide the Output panel: technical data for what is selected, "
                                  "or for the whole rig", lambda: None, icon="output")
        self.btn_out.setCheckable(True)
        self.view_bar = QWidget()
        vbl = QHBoxLayout(self.view_bar)
        vbl.setContentsMargins(0, 0, 0, 0)
        vbl.setSpacing(T.SPACE)
        vbl.addWidget(self._segment(self.btn_undo, self.btn_redo, name="edit"))
        vbl.addWidget(self._segment(self.btn_tidy, *view, name="view"))
        vbl.addStretch(1)
        self.find_box = QLineEdit()
        self.find_box.setObjectName("FindBox")
        self.find_box.setPlaceholderText("Find a part by name or kind  (Enter: next, Esc: close)")
        self.find_box.setClearButtonEnabled(True)
        self.find_box.setFixedWidth(300)
        self.find_box.textChanged.connect(self._find_changed)
        self.find_box.returnPressed.connect(self.find_next)
        self.find_box.installEventFilter(self)
        self.find_count = QLabel()
        self.find_count.setProperty("muted", True)
        self.find_count.setFont(T.ui_font(8.5))
        self.find_box.hide()
        self.find_count.hide()
        vbl.addWidget(self.find_box)
        vbl.addWidget(self.find_count)
        vbl.addWidget(self._segment(self.btn_text, self.btn_out, name="panels"))
        self._find_hits: list = []
        self._find_i = -1

        self.notice = QLabel()
        self.notice.setObjectName("Notice")
        self.notice.setWordWrap(True)
        self.notice.linkActivated.connect(self._notice_link)       # relay 017: Review and convert…
        self.notice.hide()
        nwrap = QHBoxLayout()
        nwrap.setContentsMargins(16, 10, 16, 0)
        nwrap.addWidget(self.notice)
        root.addLayout(nwrap)

        # Panels the owner can resize: parts | (grid over recorded text) | output.
        self.split_across = QSplitter(Qt.Orientation.Horizontal)
        self.split_across.setHandleWidth(6)
        self.split_across.setChildrenCollapsible(False)
        side = QFrame()
        side.setObjectName("Side")
        side.setMinimumWidth(190)
        s = QVBoxLayout(side)
        s.setContentsMargins(T.SPACE * 2, T.SPACE * 2, T.SPACE * 1 + 4, T.SPACE * 2)
        s.setSpacing(T.SPACE)
        # relay 018: the Parts panel's title area, with the search box
        title = QLabel("Parts and cables")
        title.setObjectName("SideTitle")
        title.setFont(T.ui_font(10, QFont.Weight.DemiBold))
        sub = QLabel("drag onto the grid")
        sub.setProperty("muted", True)
        sub.setFont(T.ui_font(8.5))
        th = QVBoxLayout()
        th.setSpacing(0)
        th.addWidget(title)
        th.addWidget(sub)
        s.addLayout(th)
        self.search = QLineEdit()
        self.search.setObjectName("PartSearch")
        self.search.setPlaceholderText("Search parts, cables and My parts")
        self.search.setClearButtonEnabled(True)
        self.search.setToolTip("Type to show only the parts, cables and My parts whose names contain it")
        self.search.textChanged.connect(self._filter_parts)
        s.addWidget(self.search)
        self.parts = PartsList(self.labels)
        self.parts.colour_on = self.colour_on
        self.parts.on_library_menu = self.library_menu
        self.parts.set_labels(self.labels)
        self.parts.setMinimumHeight(110)
        s.addWidget(self.parts, 1)
        self.cables = self.parts                       # relay 018: one list; cables have their own sections in it
        self.legend = QWidget()
        lg = QGridLayout(self.legend)
        lg.setContentsMargins(2, 8, 0, 0)
        lg.setHorizontalSpacing(6)
        lg.setVerticalSpacing(2)
        rows_ = ([(category_colour(cat), name) for cat, name in CATEGORY_NAMES]
                 + [(cable_colour("coax"), "Coax (on wires)"),
                    (T.color("power"), "Power (drawing only)"),
                    ("dash", "USB side of a radio"),
                    ("M", "SMA male port (a pin)"), ("F", "SMA female port (a socket)")])
        for i, (c, name) in enumerate(rows_):
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
        hv.setContentsMargins(12, T.SPACE * 1.5, 16, 6)
        hv.setSpacing(T.SPACE // 2 + 2)
        hv.addWidget(self.view_bar)
        self.scene = ChainScene(self)
        self.scene.setSceneRect(-200, -400, 2600, 1100)
        self.view = ChainView(self, self.scene)
        self.view.setStyleSheet(f"border: 1px solid {T.C['lines']}; border-radius: {T.RADIUS}px;")
        hv.addWidget(self.view)
        self.split_down.addWidget(holder)
        # relay 018: the card floats over the whole area below the header (not boxed in the grid), so a long card
        # has the screen's height and never needs a scroll bar
        self.card = EditCard(self, self)
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
        # relay 018: no rig is open at start; this panel says what can be done
        self.start_panel = StartPanel(self, self.view)
        self.start_panel.hide()
        self.minimap = Minimap(self, self.view)
        self.minimap.setVisible(prefs().value("view/minimap", False, type=bool))
        for sb in (self.view.horizontalScrollBar(), self.view.verticalScrollBar()):
            sb.valueChanged.connect(lambda *_: self.minimap.update() if self.minimap.isVisible() else None)

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
        self.split_across.setSizes([230, 1000, 300])
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

        # relay 018: the Checks footer along the bottom: a count of each kind in its colour, the first message,
        # and the whole list on a click (a click on a message selects what it is about)
        self.footer = ChecksFooter(self)
        self.footer.setVisible(q.value("view/checks", True, type=bool))
        root.addWidget(self.footer)
        self.check_list: list = []
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
                           (QKeySequence("F1"), self.show_help),
                           # relay 018: the menu bar's own keys
                           (QKeySequence("Ctrl+F"), self.open_find),
                           (QKeySequence("Ctrl+S"), self.save),
                           (QKeySequence("Ctrl+N"), self.new_rig),
                           (QKeySequence("Ctrl+O"), self.open_rig_dialog)):
            sc = QShortcut(QKeySequence(keys), self)
            sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            sc.activated.connect(slot)
        self.key_on = prefs().value("parts/key", False, type=bool)   # folded at first: the Parts list keeps its room
        self.btn_key.setChecked(self.key_on)
        self._show_key(self.key_on)
        self.refresh_format()

    def _check(self, m: QMenu, text: str, on: bool, slot) -> QAction:
        act = QAction(text, m, checkable=True)
        act.setChecked(on)
        act.toggled.connect(slot)
        m.addAction(act)
        return act

    # ---------------------------------------------------------------- relay 018: starting clean, recent rigs
    @staticmethod
    def reopen_last() -> bool:
        """Settings: reopen the last rig on start. Off by default (the screen opens blank); MARSAD_REOPEN_RIG=1 turns
        it on for one run of the window (the older window checks use it)."""
        import os
        return os.environ.get("MARSAD_REOPEN_RIG") == "1" or prefs().value("start/chain_reopen", False, type=bool)

    def recent_rigs(self) -> list[str]:
        """The rigs worked on, newest first, that still exist (kept in the window's own preferences)."""
        try:
            names = json.loads(prefs().value("chain/recent", "[]") or "[]")
        except (TypeError, ValueError):
            names = []
        return [n for n in names if isinstance(n, str) and n in self.display]

    def _remember_recent(self, name: str | None):
        if not name or self.version_view:
            return
        try:
            names = json.loads(prefs().value("chain/recent", "[]") or "[]")
        except (TypeError, ValueError):
            names = []
        names = [name] + [n for n in names if n != name and isinstance(n, str)]
        prefs().setValue("chain/recent", json.dumps(names[:8]))

    def used_rig_name(self) -> str | None:
        return next((n for n, d in self.display.items() if d.get("chains") and self.rig_is_used(d)), None)

    def open_named(self, name: str):
        """Open a rig from the start panel, Open rig… or a recent entry; unsaved changes are asked about first."""
        if name not in self.display or not self._ask_unsaved():
            return
        self.open_rig(name)

    def open_rig_dialog(self):
        """File > Open rig… (Ctrl+O): every rig in a list, the one used for runs marked, the archived ones apart.
        Double-click or Open."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Open rig")
        dlg.setObjectName("OpenRigDialog")
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(T.SPACE * 2, T.SPACE * 2, T.SPACE * 2, T.SPACE * 2)
        lay.setSpacing(T.SPACE)
        lst = QListWidget()
        lst.setObjectName("RigList")
        used = self.used_rig_name()
        live = sorted((n for n in self.display if not self.display[n].get("archived")), key=str.lower)
        gone = sorted((n for n in self.display if self.display[n].get("archived")), key=str.lower)
        for n in live + gone:
            d = self.display[n]
            st = R.rig_state(d["chains"])
            bits = []
            if n == used:
                bits.append("used for runs")
            if d.get("archived"):
                bits.append("archived")
            if d.get("draft"):
                bits.append("not saved yet")
            elif st["last_used"]:
                bits.append(f"last ran {_when(st['last_used'])}")
            it = QListWidgetItem(n + ("   ·   " + " · ".join(bits) if bits else ""))
            it.setData(Qt.ItemDataRole.UserRole, n)
            if n == used:
                f = it.font()
                f.setBold(True)
                it.setFont(f)
                it.setForeground(T.color("survey_light"))
            if d.get("archived"):
                it.setForeground(T.color("muted"))
            lst.addItem(it)
            if n == self.rig_name:
                lst.setCurrentItem(it)
        lay.addWidget(lst, 1)
        row = QHBoxLayout()
        cancel, ok = QPushButton("Cancel"), QPushButton("Open")
        ok.setObjectName("Primary")
        ok.setDefault(True)
        cancel.clicked.connect(dlg.reject)
        ok.clicked.connect(dlg.accept)
        lst.itemDoubleClicked.connect(lambda *_: dlg.accept())
        row.addStretch(1)
        row.addWidget(cancel)
        row.addWidget(ok)
        lay.addLayout(row)
        dlg.resize(520, min(560, 140 + 30 * max(3, lst.count())))
        self._open_dlg = dlg
        if dlg.exec() != QDialog.DialogCode.Accepted or lst.currentItem() is None:
            return
        self.open_named(lst.currentItem().data(Qt.ItemDataRole.UserRole))

    # ---------------------------------------------------------------- relay 018: the format bar
    def _pos_menu(self) -> QMenu:
        m = Menu(self)

        def fill():
            m.clear()
            ids = self.selected_boxes()
            boxes = [b for b in (R.box(self.rig, i) for i in ids) if b is not None]
            pick = PosPicker(R.text_pos(boxes[0]) if len(boxes) == 1 else "", m)
            wa = QWidgetAction(m)
            holder = QWidget()
            holder.setObjectName("MenuRow")
            holder.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            holder.setStyleSheet("QWidget#MenuRow, QWidget#MenuRow QLabel { background: transparent; border: none; }")
            hl = QHBoxLayout(holder)
            hl.setContentsMargins(T.SPACE * 2, T.SPACE, T.SPACE * 2, T.SPACE)
            lab = QLabel("Text position")
            lab.setFont(T.ui_font(9))
            hl.addWidget(lab)
            hl.addSpacing(T.SPACE * 2)
            hl.addWidget(pick)
            wa.setDefaultWidget(holder)
            pick.chosen.connect(lambda code: (m.close(), self.set_text_pos(self.selected_boxes(), code)))
            m.addAction(wa)
        m.aboutToShow.connect(fill)
        return m

    def _flatten(self, m: QMenu):
        """box_colour_menu adds its swatches as a Colour submenu; on the format bar's own Colour button they sit in
        the menu itself."""
        subs = [a for a in m.actions() if a.menu() is not None]
        for a in subs:
            sub = a.menu()
            m.removeAction(a)
            for x in list(sub.actions()):
                if x.isSeparator():
                    m.addSeparator()
                else:
                    sub.removeAction(x)
                    x.setParent(m)
                    m.addAction(x)
            sub.deleteLater()

    def _fill_shape_menu(self, m: QMenu):
        m.clear()
        ids = [i for i in self.selected_boxes() if R.kind_of(self.rig, i) in ("part", "power")]
        boxes = [R.box(self.rig, i) for i in ids]
        cur = boxes[0].get("shape", "") if len(boxes) == 1 else None
        adapters = bool(ids) and all(R.kind_of(self.rig, i) == "part" and R.box(self.rig, i)["part"]["type"] == "adapter"
                                     for i in ids)
        for key, label in [("", "Default (rectangle)")] + list(R.SHAPES.items()):
            if key == "pipe" and not adapters:
                continue
            a = m.addAction(label)
            a.setCheckable(True)
            a.setChecked(cur == key)
            a.setEnabled(bool(ids))
            a.triggered.connect(lambda _=False, k=key: self.set_shape(ids, k or None))

    def set_font(self, ids, size):
        """The format bar's font size, per part (drawing only; kept in My parts). None: the standard size."""
        if self.read_only:
            return
        changed = False
        for i in ids:
            b = R.box(self.rig, i)
            if b is None or R.kind_of(self.rig, i) != "part":
                continue
            if size is None:
                changed |= b.pop("font_size", None) is not None
            elif b.get("font_size") != size:
                b["font_size"] = size
                changed = True
        if changed:
            self.changed()
            self.select_ids(ids)

    def set_bold(self, ids, on: bool):
        if self.read_only:
            return
        changed = False
        for i in ids:
            b = R.box(self.rig, i)
            if b is None or R.kind_of(self.rig, i) != "part":
                continue
            if on and not b.get("bold"):
                b["bold"] = True
                changed = True
            elif not on and b.pop("bold", None) is not None:
                changed = True
        if changed:
            self.changed()
            self.select_ids(ids)

    def refresh_format(self):
        """The format bar follows the selection: live with something selected (each control saying what it does,
        or why not), quiet otherwise."""
        if not hasattr(self, "format_bar"):
            return
        ids = self.selected_boxes() if self.rig_name is not None else []
        boxes = [b for b in (R.box(self.rig, i) for i in ids) if b is not None]
        parts = [b for i, b in zip(ids, boxes) if R.kind_of(self.rig, i) == "part"]
        on = bool(boxes) and not self.read_only
        for w in self.fmt_widgets:
            w.setEnabled(on)
        self.fmt_hint.setVisible(not boxes)
        self.fmt_title.setEnabled(on)
        if self.read_only and boxes:
            self.fmt_hint.setText("Read-only: nothing here can be formatted")
            self.fmt_hint.show()
        else:
            self.fmt_hint.setText("Select a part, a power source or an enclosure to format it")
        self.fmt_size.setEnabled(on and bool(parts))
        self.fmt_bold.setEnabled(on and bool(parts))
        if not parts and on:
            self.fmt_size.setToolTip("Font size is for parts: select a part")
        sizes = {R.font_size(b) for b in parts}
        self.fmt_size.blockSignals(True)
        self.fmt_size.setCurrentIndex(self.fmt_size.findData(sizes.pop()) if len(sizes) == 1 else 0)
        self.fmt_size.blockSignals(False)
        self.fmt_bold.setChecked(bool(parts) and all(b.get("bold") for b in parts))
        locked = bool(boxes) and all(R.is_locked(b) for b in boxes)
        self.fmt_lock.setChecked(locked)
        self.fmt_lock.setText("Locked" if locked else "Lock")
        self.fmt_shape.setEnabled(on and any(R.kind_of(self.rig, i) in ("part", "power") for i in ids))

    # ---------------------------------------------------------------- relay 018: Find (Ctrl+F)
    def open_find(self):
        if self.rig_name is None:
            self.status.emit("open a rig first: Find looks in the rig on the grid")
            return
        self.find_box.show()
        self.find_count.show()
        self.find_box.setFocus()
        self.find_box.selectAll()
        self._find_changed(self.find_box.text())

    def close_find(self):
        self.find_box.hide()
        self.find_count.hide()
        self.view.setFocus()

    def _find_changed(self, text: str):
        self._find_hits = R.find_boxes(self.rig, text, self.labels)
        self._find_i = -1
        if not text.strip():
            self.find_count.setText("")
            return
        self.find_count.setText(f"{len(self._find_hits)} found" if self._find_hits else "nothing found")
        if self._find_hits:
            self.find_next()

    def find_next(self):
        if not self._find_hits:
            return
        self._find_i = (self._find_i + 1) % len(self._find_hits)
        bid = self._find_hits[self._find_i]
        self.select_ids([bid])
        it = self.box_item(bid)
        if it is not None:
            self.view.centerOn(it)
        self.find_count.setText(f"{self._find_i + 1} of {len(self._find_hits)}")

    def eventFilter(self, obj, ev):
        if obj is getattr(self, "find_box", None) and ev.type() == ev.Type.KeyPress \
                and ev.key() == Qt.Key.Key_Escape:
            self.close_find()
            return True
        return super().eventFilter(obj, ev)

    # ---------------------------------------------------------------- relay 018: the Tools
    def _last_runs(self) -> dict | None:
        try:
            return (self.svc.last_runs() or {}).get("radios") or {}
        except ServiceError as exc:
            self.show_notice(f"Could not read the last runs: {exc}")
            return None

    def check_against_last_run(self):
        """Tools > Check rig against the last run: for each radio in this rig, the text its path records now against
        the chain text that radio's most recent run recorded, part by part. Read-only."""
        if self.rig_name is None:
            self.show_notice("Open a rig first: the check compares the rig on the grid with each radio's last run.")
            return
        runs = self._last_runs()
        if runs is None:
            return
        got = R.check_against_runs(self.rig_name, self.rig, runs, self.labels)
        sections = []
        for g in got:
            word = g["radio"] + (f" ({g['rx']})" if g["rx"] != g["radio"] else "")
            if g["run"] is None:
                sections.append((word, ["No run on this radio yet: nothing to check against."], "muted"))
                continue
            head = f"last run {g['run']}, started {_when(g['started'])}"
            if g["same"]:
                lines, tone = [f"The same, word for word ({head})."], "open"
            else:
                lines = [head + ("" if not g["wording_only"] else ": differences in wording only")]
                lines += R.compare_words(g["diff"])
                tone = "muted" if g["wording_only"] else "anomaly"
            sections.append((word, lines, tone))
        if not got:
            sections.append(("This rig", ["No radio in this rig has a receiver chosen, so there is nothing to match."],
                             "muted"))
        self._report_dialog("Check rig against the last run",
                            f"“{self.rig_name}”, each radio's path as drawn now, against what that radio's most recent "
                            "run recorded. Wording only: the same part said differently (jumper and coax, 6\" and 6 in, "
                            "capitals, punctuation). Nothing here changes the rig or any run.", sections)

    def compare_versions(self):
        """Tools > Compare rig versions: two saved versions of this rig (the current drawing and every version a Save
        kept), and what changed between them in words."""
        d = self.display.get(self.rig_name or "", {})
        versions = [("the current drawing", d.get("rig") or self.rig)] + [
            (f"saved {_when(v.get('saved'))} (replaced)", v["rig"]) for v in reversed(d.get("history") or [])]
        if self.dirty:
            versions.insert(0, ("as on the grid now (not saved)", self.rig))
        if len(versions) < 2:
            self.show_notice("This rig has only one version: each Save keeps the one before it, so there is nothing "
                             "to compare yet.")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Compare versions of “{self.rig_name}”")
        dlg.setObjectName("CompareDialog")
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(T.SPACE * 2, T.SPACE * 2, T.SPACE * 2, T.SPACE * 2)
        lay.setSpacing(T.SPACE)
        row = QHBoxLayout()
        a, b = QComboBox(), QComboBox()
        for c in (a, b):
            for label, _r in versions:
                c.addItem(label)
        a.setCurrentIndex(1)
        b.setCurrentIndex(0)
        row.addWidget(QLabel("From"))
        row.addWidget(a, 1)
        row.addWidget(QLabel("to"))
        row.addWidget(b, 1)
        lay.addLayout(row)
        out = QLabel()
        out.setObjectName("CompareText")
        out.setWordWrap(True)
        out.setTextFormat(Qt.TextFormat.RichText)
        out.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        out.setAlignment(Qt.AlignmentFlag.AlignTop)
        lay.addWidget(out, 1)

        def show(*_):
            d_ = R.rig_diff(versions[a.currentIndex()][1], versions[b.currentIndex()][1], self.labels, self.rig_name)
            html = []
            for key, title in (("added", "Parts added"), ("removed", "Parts removed"), ("renamed", "Renamed"),
                               ("moved", "Moved between paths"), ("cables", "Cables"), ("texts", "Recorded text")):
                if d_[key]:
                    html.append(f"<p style='margin:8px 0 2px 0; color:{T.C['muted']}'><b>{title.upper()}</b></p>")
                    html += [f"<div style='margin-left:6px'>{_esc(x)}</div>" for x in d_[key]]
            out.setText("".join(html) or f"<span style='color:{T.C['muted']}'>No difference: the two versions are the "
                                         "same drawing.</span>")
            self._compare_text = out.text()
        a.currentIndexChanged.connect(show)
        b.currentIndexChanged.connect(show)
        show()
        close = QPushButton("Close")
        close.clicked.connect(dlg.accept)
        r = QHBoxLayout()
        r.addStretch(1)
        r.addWidget(close)
        lay.addLayout(r)
        dlg.resize(680, 480)
        self._compare_dlg = dlg
        dlg.exec()

    def _report_dialog(self, title: str, intro: str, sections: list):
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.setObjectName("ReportDialog")
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(T.SPACE * 2, T.SPACE * 2, T.SPACE * 2, T.SPACE * 2)
        lay.setSpacing(T.SPACE)
        lab = QLabel(intro)
        lab.setWordWrap(True)
        lab.setProperty("muted", True)
        lay.addWidget(lab)
        body = QLabel()
        body.setObjectName("ReportText")
        body.setWordWrap(True)
        body.setTextFormat(Qt.TextFormat.RichText)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        html = []
        for head, lines, tone in sections:
            html.append(f"<p style='margin:10px 0 2px 0'><b>{_esc(head)}</b></p>")
            for k, line in enumerate(lines):
                colour = T.C[tone] if k == 0 else T.C["text"]
                html.append(f"<div style='margin-left:6px; color:{colour}'>{_esc(line)}</div>")
        body.setText("".join(html))
        lay.addWidget(body, 1)
        close = QPushButton("Close")
        close.clicked.connect(dlg.accept)
        r = QHBoxLayout()
        r.addStretch(1)
        r.addWidget(close)
        lay.addLayout(r)
        dlg.resize(720, max(260, min(620, dlg.sizeHint().height())))
        self._report_dlg = dlg
        self._report_text = body.text()
        dlg.exec()

    # ---------------------------------------------------------------- relay 018: files
    def export_rig_file(self, path: str | None = None) -> str | None:
        """File > Export rig file: this drawing as a file of its own (JSON), to keep or to bring into another
        station's window. Drawing only: it changes nothing here."""
        if self.rig_name is None:
            return None
        if path is None:
            start = str(Path.home() / f"{self.rig_name.replace('/', '-')}.marsad-rig.json")
            path, _ = QFileDialog.getSaveFileName(self, "Export rig file", start, "Marsad rig (*.marsad-rig.json *.json)")
            if not path:
                return None
        doc = {"marsad_rig": 1, "name": self.rig_name, "exported": _now_iso(), "rig": copy.deepcopy(self.rig),
               "note": "A Chain drawing exported from the Marsad window. File > Import rig file brings it in as a new "
                       "rig; nothing reaches chain.json until that rig is saved."}
        try:
            Path(path).write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
        except OSError as exc:
            self.show_notice(f"The rig file could not be written: {exc}")
            return None
        self.status.emit(f"rig file written: {path}")
        return path

    def import_rig_file(self, path: str | None = None, name: str | None = None) -> bool:
        """File > Import rig file: a drawing from a rig file, as a NEW rig under a name not already taken. Kept as a
        drawing at once (like New rig); its chains reach chain.json only when it is saved. No rig is replaced."""
        if not self._ask_unsaved():
            return False
        if path is None:
            path, _ = QFileDialog.getOpenFileName(self, "Import rig file", str(Path.home()),
                                                  "Marsad rig (*.marsad-rig.json *.json)")
            if not path:
                return False
        try:
            doc = json.loads(Path(path).read_text(encoding="utf-8"))
            rig = doc.get("rig") if isinstance(doc, dict) else None
            if not isinstance(rig, dict):
                raise ValueError("this is not a Marsad rig file (no drawing in it)")
            R.check_layout({"schema": 1, "rigs": {"imported": {**rig, "chains": {}}}})
        except (OSError, ValueError) as exc:
            self.show_notice(f"Not imported: {exc}")
            return False
        base = str(doc.get("name") or Path(path).stem.replace(".marsad-rig", "")).strip() or "Imported rig"
        if name is None:
            suggest, k = base, 2
            while self._taken(suggest):
                suggest, k = f"{base} ({k})", k + 1
            name, ok = QInputDialog.getText(self, "Import rig file", "Name for the imported rig:", text=suggest)
            if not ok:
                return False
        name = (name or "").strip()
        if not name or self._taken(name):
            self.show_notice(f"Not imported: {'type a name' if not name else f'there is already a rig called “{name}”'}.")
            return False
        rig = {k: v for k, v in copy.deepcopy(rig).items() if k not in ("chains", "history", "archived")}
        try:
            lay = R.add_draft(self.layout_doc, name, rig, self.chain_doc())
            self.svc.save(self.chain_doc(), lay)
        except (ValueError, ServiceError) as exc:
            self.show_notice(f"Not imported: {exc}")
            return False
        if not self.reload(keep=name):
            return False
        self.show_notice(f"Imported as “{name}”: a drawing only so far. Save it to make its chains; runs can use it "
                         "once it has a path to a radio.", soft=True)
        return True

    def save_as_copy(self, name: str | None = None) -> bool:
        """File > Save as copy: this drawing (as on the grid) kept as a new rig under a new name; this rig is left as
        it was, unsaved changes and all. The copy is a drawing until it is saved."""
        if self.rig_name is None:
            return False
        if name is None:
            suggest, k = f"{self.rig_name} copy", 2
            while self._taken(suggest):
                suggest, k = f"{self.rig_name} copy {k}", k + 1
            name, ok = QInputDialog.getText(self, "Save as copy", "Name for the copy:", text=suggest)
            if not ok:
                return False
        name = (name or "").strip()
        if not name or self._taken(name):
            self.show_notice(f"Not copied: {'type a name' if not name else f'there is already a rig called “{name}”'}.")
            return False
        try:
            lay = R.add_draft(copy.deepcopy(self.layout_doc), name, copy.deepcopy(self.rig), self.chain_doc())
            self.svc.save(self.chain_doc(), lay)
        except (ValueError, ServiceError) as exc:
            self.show_notice(f"Not copied: {exc}")
            return False
        keep_rig, keep_dirty, keep_name = copy.deepcopy(self.rig), self.dirty, self.rig_name
        self.reload(keep=keep_name)
        if keep_dirty:                       # this rig's unsaved changes stay on the grid
            self.rig = keep_rig
            self.changed()
        self.status.emit(f"copy kept as “{name}”: open it from the rig list")
        return True

    def export_station_json(self, path: str | None = None) -> str | None:
        """File > Export a copy of station.json: the station's settings file exactly as it is on disk, for the
        owner's records, read through the service. station.local.json (the location) is never read or included
        (rule 10). Read-only: nothing in the station changes, and there is no import."""
        try:
            got = self.svc.station_json()
        except ServiceError as exc:
            self.show_notice(f"station.json could not be read: {exc}")
            return None
        if path is None:
            start = str(Path.home() / f"station-{_now_iso()[:10]}.json")
            path, _ = QFileDialog.getSaveFileName(self, "Export a copy of station.json", start, "JSON (*.json)")
            if not path:
                return None
        try:
            Path(path).write_text(got.get("text") or "", encoding="utf-8")
        except OSError as exc:
            self.show_notice(f"The copy could not be written: {exc}")
            return None
        self.status.emit(f"copy of station.json written: {path} (station.local.json is never included)")
        return path

    # ---------------------------------------------------------------- relay 018: the PC's USB inputs
    def showEvent(self, ev):
        super().showEvent(ev)
        if not hasattr(self, "_usb_timer"):
            self._usb_timer = QTimer(self)
            self._usb_timer.timeout.connect(self.refresh_radios)
            self._usb_before = self._usb_memory()          # what the window remembered from before it opened
        self._usb_timer.start(15000)
        self.refresh_radios()

    def _usb_memory(self) -> dict:
        try:
            d = json.loads(prefs().value("usb/places", "{}") or "{}")
            return d if isinstance(d, dict) else {}
        except (TypeError, ValueError):
            return {}

    def refresh_radios(self):
        """Where each radio is plugged, from the service's radio list (Windows' device list, nothing opened), and each
        radio's last run (its device index). Asked off the window's thread; the window remembers each radio's USB
        place, so Output can say when one has moved."""
        if not self.isVisible():
            if hasattr(self, "_usb_timer"):
                self._usb_timer.stop()
            return
        from .scan_start import call

        def got(d, err):
            if err or not isinstance(d, dict):
                return
            self.radios_info = {r["name"]: r for r in d.get("radios") or []}
            mem = self._usb_memory()
            for r in self.radios_info.values():
                u, sn = r.get("usb"), r.get("serial")
                if not sn or not u or not u.get("location"):
                    continue
                rec = mem.get(sn) or {}
                rec["seen"] = {k: u.get(k) for k in ("location", "port", "hub")}
                rec["seen_at"] = _now_iso()
                if (r.get("doing") or {}).get("state") in ("surveying", "recording"):
                    rec["run"] = dict(rec["seen"], what=(r.get("doing") or {}).get("text") or "a run")
                mem[sn] = rec
            prefs().setValue("usb/places", json.dumps(mem))
            self.fill_output()

        def got_runs(d, err):
            if not err and isinstance(d, dict):
                self.last_runs_info = d.get("radios") or {}
                self.fill_output()
        call(self.svc.radios, got)
        call(self.svc.last_runs, got_runs)

    def usb_lines(self, radio_node: dict | None) -> list[str]:
        """Relay 018: a radio's USB facts for its PC input's tooltip and Output: serial, which port and hub, speed,
        how Marsad picks its device index, and whether it has moved since its last run."""
        if not radio_node:
            return []
        rx = radio_node["part"].get("rx")
        info = (getattr(self, "radios_info", {}) or {}).get(rx or "")
        sn = self.serial_of(radio_node["part"])
        out = []
        if not rx:
            return ["no receiver chosen on this radio, so its USB place cannot be looked up"]
        if sn:
            out.append(f"SN {sn}")
        if info is None:
            out.append("USB place: not read yet (the service's radio list)")
            return out
        u = info.get("usb")
        if info.get("present") is False:
            out.append("not plugged in now (Windows' device list)")
        elif u and u.get("location"):
            where = (f"port {u['port']} on hub {u['hub']}" if u.get("port") and u.get("hub") is not None
                     else str(u["location"]))
            out.append(f"USB place: {where}" + (f" ({u['location']})" if u.get("port") else ""))
            out.append(f"speed: {u['speed']}" if u.get("speed") else "speed: not reported")
        else:
            out.append("USB place: Windows would not say")
        last = (getattr(self, "last_runs_info", {}) or {}).get(rx)
        dev = (last or {}).get("device")
        out.append("device index: found by serial at each start (USB numbering moves)"
                   + (f"; its last run used index {dev}" if dev is not None else ""))
        mem = (self._usb_memory() if hasattr(self, "_usb_before") else {}).get(sn or "") or {}
        before = (getattr(self, "_usb_before", {}) or {}).get(sn or "") or {}
        moved = None
        if u and u.get("location"):
            if mem.get("run"):
                moved = R.usb_moved_text(rx, mem["run"], u, f"since {mem['run'].get('what') or 'its last run'}")
            if moved is None and before.get("seen"):
                moved = R.usb_moved_text(rx, before["seen"], u, f"since {_when(before.get('seen_at'))}")
        if moved:
            out.append(moved)
        return out

    # ---------------------------------------------------------------- relay 018: the menu bar
    def set_checks_shown(self, on: bool):
        self.footer.setVisible(on)
        prefs().setValue("view/checks", on)

    def set_minimap_shown(self, on: bool):
        self.minimap.setVisible(on)
        prefs().setValue("view/minimap", on)
        if on:
            self.minimap.place()
            self.minimap.invalidate()

    def build_menus(self, settings, about) -> list:
        """The menu bar's menus for this screen (relay 018): File, Edit, View, Arrange, Tools and Help. settings and
        about are the window's own dialogs. Keys are shown beside each entry; the keys themselves stay with the grid
        and the screen's shortcuts, so no key is claimed twice."""
        mb = []

        def act(m, text, slot, enabled=None, tip=""):
            a = m.addAction(text)
            a.triggered.connect(lambda *_: slot())
            if tip:
                a.setToolTip(tip)
            if enabled is not None:
                a.setData(("when", enabled))
            return a

        def refresh(m):                      # each entry's enabled state, asked when the menu opens
            for a in m.actions():
                d = a.data()
                if isinstance(d, tuple) and d and d[0] == "when":
                    ok, why = d[1]()
                    a.setEnabled(ok)
                    if why:
                        a.setToolTip(why)

        def menu(title):
            m = Menu(self)
            m.setTitle(title)
            m.aboutToShow.connect(lambda m=m: refresh(m))
            mb.append(m)
            return m

        has = lambda: (self.rig_name is not None, "" if self.rig_name else "Open a rig first")  # noqa: E731
        editable = lambda: (self.rig_name is not None and not self.read_only,                  # noqa: E731
                            "" if self.rig_name is None or not self.read_only else
                            "Read-only: restore it first (File > Restore rig)")
        del editable
        f = menu("&File")
        act(f, "New rig…\tCtrl+N", self.new_rig)
        act(f, "Open rig…\tCtrl+O", self.open_rig_dialog)
        recent = Menu(f)
        recent.setTitle("Open recent")
        recent.aboutToShow.connect(lambda: self._fill_recent(recent))
        f.addMenu(recent)
        f.addSeparator()
        act(f, "Save\tCtrl+S", self.save, lambda: (self.btn_save.isEnabled(), self.btn_save.toolTip()))
        act(f, "Save as copy…", self.save_as_copy, has)
        act(f, "Rename…", self.rename_rig, lambda: (self.btn_rename.isEnabled(), self.btn_rename.toolTip()))
        act(f, "Use for runs", self.use_for_runs, lambda: (self.btn_use.isEnabled(), self.btn_use.toolTip()))
        st = f.addAction("Static rig (never asked before a run)")
        st.setCheckable(True)
        f.aboutToShow.connect(lambda: (st.setChecked(self.static_), st.setEnabled(self.rig_name is not None)))
        st.triggered.connect(lambda on: (self.chk_static.setChecked(on), self._static_clicked(on)))
        act(f, "History…", self.history_dialog,
            lambda: (bool(self.display.get(self.rig_name or "", {}).get("history")),
                     "No earlier versions yet: each Save keeps the one before it"))
        act(f, "Archive rig…", self.archive_rig,
            lambda: (not self.read_only and self._why_not_archive(asks_first=True) is None,
                     self._why_not_archive(asks_first=True) or ""))
        act(f, "Restore rig", self.restore_rig, lambda: (self.read_only and not self.version_view,
                                                          "Only an archived rig can be restored"))
        f.addSeparator()
        act(f, "Export picture…", lambda: self.export_picture(),
            lambda: (self.btn_picture.isEnabled(), self.btn_picture.toolTip()))
        act(f, "Export rig file…", self.export_rig_file, has)
        act(f, "Import rig file…", self.import_rig_file)
        f.addSeparator()
        act(f, "Export a copy of station.json…", self.export_station_json,
            tip="The settings file as it is on disk, for your records. Your location (station.local.json) is never "
                "included. There is no import: changing presets, bands or caps needs your word, one item at a time.")
        f.addSeparator()
        act(f, "Exit", lambda: self.window().close())

        e = menu("&Edit")
        act(e, "Undo\tCtrl+Z", self.undo, lambda: (bool(self.undo_stack), "Nothing to undo yet"))
        act(e, "Redo\tCtrl+Y", self.redo, lambda: (bool(self.redo_stack), "Nothing to redo: undo something first"))
        e.addSeparator()
        sel = lambda: (bool(self.selected_boxes() or self.scene.selectedItems()), "Select something first")  # noqa
        act(e, "Cut\tCtrl+X", self.cut, sel)
        act(e, "Copy\tCtrl+C", self.copy, sel)
        act(e, "Paste\tCtrl+V", lambda: self.paste(), lambda: (self._clip_fragment() is not None, "Nothing copied yet"))
        act(e, "Duplicate\tCtrl+D", self.duplicate, sel)
        act(e, "Select all\tCtrl+A", self.select_all, has)
        act(e, "Take out of rig\tDelete", self.take_out_selected, sel)
        e.addSeparator()
        act(e, "Find…\tCtrl+F", self.open_find, has)
        e.addSeparator()
        act(e, "Clear grid…", self.clear_grid, lambda: (self.btn_clear.isEnabled(), self.btn_clear.toolTip()))

        v = menu("&View")
        for title, build in (("Grid", self._grid_menu), ("Parts", self._parts_menu), ("Wires", self._wires_menu),
                             ("Zoom", self._zoom_menu)):
            sub = self.menus[title.lower()].menu()
            v.addAction(sub.menuAction())
            sub.setTitle(title)
        v.addSeparator()
        for text, btn in (("Output", self.btn_out), ("Recorded text", self.btn_text)):
            a = v.addAction(text)
            a.setCheckable(True)
            v.aboutToShow.connect(lambda a=a, btn=btn: a.setChecked(btn.isChecked()))
            a.triggered.connect(lambda on, btn=btn: btn.setChecked(on))
        a = v.addAction("Checks (footer)")
        a.setCheckable(True)
        v.aboutToShow.connect(lambda a=a: a.setChecked(self.footer.isVisible()))
        a.triggered.connect(self.set_checks_shown)
        from . import explain as X
        a = v.addAction("Explanations (the ⓘ marks)")
        a.setCheckable(True)
        v.aboutToShow.connect(lambda a=a: a.setChecked(X.shown()))
        a.triggered.connect(X.set_shown)
        a = v.addAction("Minimap")
        a.setCheckable(True)
        v.aboutToShow.connect(lambda a=a: a.setChecked(self.minimap.isVisible()))
        a.triggered.connect(self.set_minimap_shown)
        a = v.addAction("Key to colours and marks")
        a.setCheckable(True)
        v.aboutToShow.connect(lambda a=a: a.setChecked(self.btn_key.isChecked()))
        a.triggered.connect(self.btn_key.setChecked)

        ar = menu("&Arrange")
        ar.aboutToShow.connect(lambda m=ar: self._fill_arrange_bar(m))

        tl = menu("&Tools")
        act(tl, "Check rig against the last run…", self.check_against_last_run, has,
            tip="Each radio's path as drawn, against the chain its most recent run recorded, part by part")
        act(tl, "Compare rig versions…", self.compare_versions,
            lambda: (bool(self.display.get(self.rig_name or "", {}).get("history")) or self.dirty,
                     "Only one version so far: each Save keeps the one before it"))
        tl.addSeparator()
        act(tl, "Settings…", settings)

        hp = menu("&Help")
        act(hp, "Keyboard shortcuts\tF1", self.show_help)
        act(hp, "About Marsad", about)
        return mb

    def _fill_recent(self, m: QMenu):
        m.clear()
        names = self.recent_rigs()
        used = self.used_rig_name()
        for n in names:
            a = m.addAction(n + ("   ·   used for runs" if n == used else ""))
            a.triggered.connect(lambda _=False, n=n: self.open_named(n))
        if not names:
            a = m.addAction("No rigs opened yet")
            a.setEnabled(False)

    def _fill_arrange_bar(self, m: QMenu):
        """Arrange in the menu bar: stacking, align and distribute, lock and groups (as before), then the rig's
        direction and Tidy."""
        m.clear()
        self._add_arrange(m)
        m.add_heading("Direction")
        for key, text in R.DIRECTIONS.items():
            a = m.addAction(tool_icon("dir_" + key), text)
            a.setCheckable(True)
            a.setChecked(G.direction(self.rig) == key)
            a.setEnabled(not self.read_only and self.rig_name is not None)
            a.triggered.connect(lambda _=False, k=key: self.set_direction(k))
        m.addSeparator()
        a = m.addAction("Tidy")
        a.setEnabled(bool(self.rig["nodes"]) and not self.read_only)
        a.triggered.connect(self.tidy)

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
            why = self._why_not_archive(asks_first=True)
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

    def _why_not_archive(self, asks_first: bool = False) -> str | None:
        """Why Archive is refused now; with asks_first, unsaved changes are not a reason (the menu offers it, and
        archive_rig asks Save / Discard / Cancel first)."""
        d = self.display.get(self.rig_name or "")
        if not d:
            return "Open a rig first"
        if not d.get("chains") and not d.get("draft"):
            return "This rig has never been saved: there is nothing to archive"
        if self.dirty and not asks_first:
            return "Save it or undo the changes first"
        if self.rig_is_used(d):
            return "This rig is the one runs record now: make another rig current (Use for runs) first"
        return None

    def archive_rig(self):
        """Archive, never delete (the owner's decision, 2026-11-06): the rig moves to the Archived
        section; its chains stay in chain.json exactly as they are; Restore brings it back."""
        if self.dirty and not self._ask_unsaved():       # relay 014: the same question as everywhere else
            return
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
            for t in R.CABLE_TYPES:
                a = sub.addAction(dot_icon(cable_colour(t)), "Coax" if t == "coax" else "USB cable")
                a.triggered.connect(lambda _=False, t=t, w=item.wire: self._add_cable_to(w, t))
            m.addMenu(sub)
            pt0 = self.menu_point or item.label_point()          # relay 017: chokes
            bead = item.bead_at(pt0)
            a = m.addAction("Put a choke here")
            a.setEnabled(bool(item.cables()))
            a.setToolTip("A ferrite bead on the cable at this point" if item.cables()
                         else "A choke sits on a cable: add a Coax or USB cable first")
            a.triggered.connect(lambda _=False, w=item.wire, p=QPointF(pt0), it=item: self._choke_here(w, it, p))
            if bead is not None:
                a = m.addAction("Remove this choke")
                a.setToolTip("Takes it off the cable: drawing only, and the cable's \"with choke\" if it was the last")
                a.triggered.connect(lambda _=False, w=item.wire, b=bead: self._choke_off(w, *b))
            # relay 015: bend points
            m.addSeparator()
            pt = self.menu_point or item.label_point()
            near = item.point_at(pt)
            a = m.addAction("Add bend point here	Ctrl+click")
            a.setToolTip("A point the wire passes through; drag it anywhere on the grid")
            a.triggered.connect(lambda _=False, w=item.wire, p=QPointF(pt): self.add_bend(w, p))
            a = m.addAction("Remove point\tDelete")
            a.setEnabled(near is not None or item.sel_point() is not None)
            a.triggered.connect(lambda _=False, w=item.wire, i=near: self.remove_bend(w, i))
            a = m.addAction("Straighten (remove all points)")
            a.setEnabled(bool(item.points()))
            a.triggered.connect(lambda _=False, w=item.wire: self.straighten(w))
            a = m.addAction("Right angles")
            a.setCheckable(True)
            a.setChecked(bool(item.wire.get("square")))
            a.setToolTip("Square corners between this wire's points, whatever the wire style")
            a.triggered.connect(lambda on, w=item.wire: self.set_square(w, on))

    def _choke_here(self, wire: dict, item, pt: QPointF):
        why = R.add_choke(self.rig, wire["from"], wire["port"], item.along(pt))
        if why:
            self.show_notice(why)
            return
        self.changed()
        self.select_wire(wire["from"], wire["port"])

    def _choke_off(self, wire: dict, ci: int, k: int):
        R.remove_choke(self.rig, wire["from"], wire["port"], ci, k)
        self.changed()
        self.select_wire(wire["from"], wire["port"])

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
    def reload(self, keep: str | None = None, first: bool = False):
        """Read the rigs from the service and show one. first (relay 018): the window's own start, when no rig is
        opened unless Settings say "reopen the last rig" (then the last one worked on, else the one runs use)."""
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
        if first and keep is None:
            if self.reopen_last():
                recent = self.recent_rigs()
                want = recent[0] if recent else want
            else:
                want = None
        if want is not None:
            self.open_rig(want)
        else:
            self.version_view = None
            self.read_only = False
            self.dirty = False
            self.rig_name, self.rig = None, R.new_rig()
            self._reset_history(saved=True)
            self._show_in_pick(None)
            self.show_notice(None)
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
        if name is None:                     # relay 018: no rig open; the picker says so
            self.pick.setCurrentIndex(-1)
            return
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
        self._remember_recent(name)          # relay 018: the start panel's recent rigs
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
            self.offer_old_words()
        QTimer.singleShot(0, self._center)

    # -- relay 017: Jumper and Pigtail are old words. Offered, never converted silently.
    def old_words_here(self) -> tuple[list, list]:
        lib = R.library_old_words({"library": self.library}) if self.library else []
        return (R.old_words(self.rig) if self.rig else []), lib

    def offer_old_words(self):
        """A gentle notice when the open drawing, or My parts, still says Jumper or Pigtail."""
        items, lib = self.old_words_here()
        if not items and not lib:
            return
        words = sorted({("Pigtail" if x["kind"] == "pigtail" else "Jumper") for x in items + lib})
        where = ("this drawing and My parts" if items and lib else ("My parts" if lib else "this drawing"))
        self.show_notice(f"{' and '.join(words)} {'is' if len(words) == 1 else 'are'} no longer used: cables are "
                         f"Coax or USB with a length. {where[0].upper() + where[1:]} still "
                         f"{'uses' if len(words) == 1 else 'use'} {'it' if len(words) == 1 else 'them'}, unchanged. "
                         f"<a href=\"old-words\">Review and convert…</a>")

    def _notice_link(self, href: str):
        if href == "old-words":
            self.review_old_words()

    def review_old_words(self):
        """Every old word in this drawing and in My parts, what each would become, one button each."""
        items, lib = self.old_words_here()
        dlg = QDialog(self)
        dlg.setWindowTitle("Jumper and Pigtail")
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(T.SPACE * 2, T.SPACE * 2, T.SPACE * 2, T.SPACE * 2)
        lay.setSpacing(T.SPACE)
        head = QHBoxLayout()
        intro = QLabel("Cables are now Coax or USB with a length. Each of these is unchanged until you convert it; "
                       "converting changes the rig's recorded text, so its next run records it as changed. "
                       "Undo puts it back.")
        intro.setWordWrap(True)
        head.addWidget(intro, 1)
        head.addWidget(E.info("old_words"), 0, Qt.AlignmentFlag.AlignTop)
        lay.addLayout(head)
        if not items and not lib:
            lay.addWidget(QLabel("Nothing left in the old words."))
        for it in items:
            lay.addWidget(self._old_row(f"{'Pigtail' if it['kind'] == 'pigtail' else 'Jumper'} in this drawing",
                                        it["was"], it["becomes"],
                                        "Merge into antenna" if it.get("merge") else "Convert to Coax / USB cable",
                                        lambda _=False, it=it: (dlg.accept(), self.convert_old_item(it, reopen=True))))
        for e in lib:
            lay.addWidget(self._old_row("In My parts", e["was"], e["becomes"], "Convert in My parts",
                                        lambda _=False, e=e: (dlg.accept(), self.convert_lib_old(e["id"]))))
        foot = QLabel("Past runs' records are never changed: in records made before November 2026, \"jumper\" "
                      "means a short cable.")
        foot.setWordWrap(True)
        foot.setProperty("muted", True)
        lay.addWidget(foot)
        close = QPushButton("Close")
        close.clicked.connect(dlg.reject)
        r = QHBoxLayout()
        r.addStretch(1)
        r.addWidget(close)
        lay.addLayout(r)
        dlg.resize(560, dlg.sizeHint().height())
        self._old_dialog = dlg
        dlg.exec()

    def _old_row(self, where: str, was: str, becomes: str, button: str, slot) -> QFrame:
        f = QFrame()
        f.setObjectName("CardBox")
        g = QGridLayout(f)
        g.setContentsMargins(10, 8, 10, 8)
        g.setVerticalSpacing(2)
        w = QLabel(where)
        w.setProperty("muted", True)
        g.addWidget(w, 0, 0, 1, 2)
        a = QLabel(f"Now: {was}")
        a.setWordWrap(True)
        b = QLabel(f"Becomes: {becomes}")
        b.setWordWrap(True)
        g.addWidget(a, 1, 0)
        g.addWidget(b, 2, 0)
        btn = QPushButton(button)
        btn.clicked.connect(slot)
        g.addWidget(btn, 1, 1, 2, 1, Qt.AlignmentFlag.AlignVCenter)
        g.setColumnStretch(0, 1)
        return f

    def convert_old_item(self, item: dict | None, reopen: bool = False):
        """Apply one offer: one undoable step, then the notice again for whatever is left."""
        if item is None or self.read_only:
            return
        R.convert_old(self.rig, item)
        self.changed()
        self.scene.clearSelection()
        self.show_notice(None)
        self.offer_old_words()
        if reopen and any(self.old_words_here()):
            QTimer.singleShot(0, self.review_old_words)

    def convert_lib_old(self, lid: str):
        if self._lib_change(lid, R.convert_lib_entry):
            self.show_notice(None)
            self.offer_old_words()
            if any(self.old_words_here()):
                QTimer.singleShot(0, self.review_old_words)

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
        if R.multi_in(node):
            n_in = len(R.used_inputs(self.rig, node["id"]))
            return f"{label} · {n_in} inputs" if n_in > 1 else label
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
        self._show_direction()
        self.items_by_id, self.wire_items = {}, []      # relay 018: emptied before the scene, which signals as it clears
        self.scene.clear()
        self.temp_wire = None
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
                out |= {(w["from"], "out", w["port"]),
                        (w["to"], "in", R.in_slot(w) if R.multi_in(R.box(self.rig, w["to"])) else 0)}
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
        self.refresh_format()
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
        vp = self.view.viewport().geometry()
        for h in (self.empty_hint, getattr(self, "start_panel", None)):
            if h is None or not h.isVisible():
                continue
            h.adjustSize()
            h.move(int(vp.x() + (vp.width() - h.width()) / 2), int(vp.y() + max(16, (vp.height() - h.height()) / 2)))
            h.raise_()
        if getattr(self, "minimap", None) is not None and self.minimap.isVisible():
            self.minimap.place()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._place_empty_hint()
        if hasattr(self, "card") and self.card.isVisible():
            self.card._fit()
            self._place_card()

    def card_room(self) -> int:
        """Relay 018: how tall the card may be: the area below the header (the panels and the grid), less a margin."""
        return max(200, self.split_across.height() - 16) if hasattr(self, "split_across") else 2000

    def _to_screen(self, scene_pt: QPointF) -> QPoint:
        """A scene point in this screen's own coordinates (where the card lives)."""
        return self.view.viewport().mapTo(self, self.view.mapFromScene(scene_pt))

    def _place_card(self):
        """The card beside what it edits, never over it: right of it if there is room, else left, below or above,
        inside the area below the header. Relay 018: that area is the whole screen's, not the grid's."""
        if not self.card.isVisible():
            return
        area = self.split_across.geometry()
        ox, oy = area.x(), area.y()
        if self.card.wire is not None:
            wi = self.wire_item(self.card.wire["from"], self.card.wire["port"])
            if wi is None:
                return
            try:                       # relay 018: a card kept on screen through a redraw may ask mid-redraw
                lp = wi.label_point()
            except RuntimeError:
                return
            p = self._to_screen(lp + QPointF(-60, 26)) - QPoint(ox, oy)
            x = min(max(8, p.x()), area.width() - self.card.width() - 8)
            y = p.y()
            if y + self.card.height() > area.height() - 8:
                y = (self._to_screen(lp) - QPoint(ox, oy)).y() - 26 - self.card.height()
            y = min(max(8, y), max(8, area.height() - self.card.height() - 8))
            self.card.move(int(x + ox), int(y + oy))
            return
        bid = (self.card.node or self.card.enc or self.card.power or {}).get("id")
        it = self.box_item(bid) if bid else None
        if it is None:
            return
        w, h = it.wh()
        tl = self._to_screen(it.mapToScene(QPointF(0, 0))) - QPoint(ox, oy)
        br = self._to_screen(it.mapToScene(QPointF(w, h))) - QPoint(ox, oy)
        x, y = card_place((tl.x(), tl.y(), br.x() - tl.x(), br.y() - tl.y()),
                          (self.card.width(), self.card.height()), (area.width(), area.height()))
        self.card.move(int(x + ox), int(y + oy))

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
        node["x"], node["y"] = G.seat_near(self.rig, node, node["x"], node["y"])   # relay 014: onto a wall
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
        if type_ == R.CHOKE:                          # relay 017: a choke goes onto the cable under the drop
            why = R.add_choke(self.rig, *key, wi.along(pt))
            if why:
                self.show_notice(why)
                return
        else:
            wi.wire.setdefault("cables", []).append(R.new_cable(type_, self.labels))
        self.show_notice(None)
        self.changed()
        self.select_wire(*key)

    def start_wire(self, item: PartItem, port: int, pt: QPointF):
        self.wiring_from = (item.node["id"], port)
        self._temp_wire()
        item.update()
        self.drag_wire(pt)

    def start_wire_in(self, item: PartItem, pt: QPointF, slot: int = 0):
        """A wire dragged from an input dot (relay 006: every dot starts a wire). Let go over
        another part and that part's first free output feeds this input. On a PC or hub (relay 015)
        `slot` says which of its inputs: only the wire on that one is replaced."""
        self.wiring_from = ("in", item.node["id"], slot)
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
        down = self.down
        side = G.side_to_point(node, pt.x(), pt.y(), down)
        ax, ay = G.mid_side(node, side, down)
        target = next((i for i in self.scene.items(pt) if isinstance(i, PartItem)
                       and i.node["id"] != node["id"]), None)
        end = (pt.x(), pt.y())
        end_side = None
        if target is not None:              # the curve settles onto the part it will join
            end_side = G.OPPOSITE[G.side_toward(node, target.node, down)]
            end = G.mid_side(target.node, end_side, down)
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
            return self._end_wire_in(wf[1], pt, wf[2] if len(wf) > 2 else 0)
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

    def _end_wire_in(self, dst: str, pt: QPointF, slot: int = 0):
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
        many = R.multi_in(self.items_by_id[dst].node)
        mine = (lambda w: w["to"] == dst and R.in_slot(w) == slot) if many else (lambda w: w["to"] == dst)
        old = [w for w in self.rig["wires"] if mine(w)]
        self.rig["wires"] = [w for w in self.rig["wires"] if not mine(w)]   # dragging from a wired input re-wires it
        why = R.connect(self.rig, target.node["id"], free[0], dst)
        if why:
            self.rig["wires"] += old
            self.show_notice(f"Not wired: {why}.")
            self.items_by_id[dst].update()
            return
        new = R.wire_of(self.rig, target.node["id"], free[0])
        if many:
            new["in"] = slot
        if old and old[0].get("cables"):
            new["cables"] = old[0]["cables"]
        self.show_notice(None)
        self.changed()

    # -- bend points and moving a wire's end (relay 015)
    def bend_place(self, pt: QPointF) -> tuple[int, int]:
        """Where a bend point goes for a pointer place: on the grid when snap is on."""
        if self.snap_on:
            return R.snap(pt.x()), R.snap(pt.y())
        return int(round(pt.x())), int(round(pt.y()))

    def add_bend(self, wire: dict, pt: QPointF, commit: bool = True) -> int | None:
        """Make a bend point on a wire at pt, in the stretch of the wire it is nearest. Returns its index.
        commit=False leaves the undo step to the drag that follows (WireItem)."""
        if self.read_only or R.mated(wire):
            return None
        it = self.wire_item(wire["from"], wire["port"])
        a, b = it.ends() if it else (None, None)
        if a is None or b is None:
            return None
        x, y = self.bend_place(pt)
        pts = wire.setdefault("points", [])
        i = G.insert_index(a, b, pts, x, y)
        pts.insert(i, [x, y])
        self.sel_point = (wire["from"], wire["port"], i)
        if commit:
            self.changed()
            self.select_wire(wire["from"], wire["port"])
        else:
            it.refresh()
            it.update()
        self.status.emit("bend point made: drag it anywhere, or click it and press Delete to remove it")
        return i

    def remove_bend(self, wire: dict, i: int | None = None) -> bool:
        """Take a bend point off a wire (the picked one when i is None)."""
        pts = wire.get("points") or []
        if i is None:
            sp = self.sel_point
            i = sp[2] if sp and sp[:2] == (wire["from"], wire["port"]) else None
        if i is None or not 0 <= i < len(pts):
            return False
        del pts[i]
        if not pts:
            wire.pop("points", None)
        self.sel_point = None
        self.changed()
        self.select_wire(wire["from"], wire["port"])
        return True

    def straighten(self, wire: dict) -> bool:
        """Take every bend point off a wire."""
        if not wire.get("points"):
            return False
        wire.pop("points", None)
        self.sel_point = None
        self.changed()
        self.select_wire(wire["from"], wire["port"])
        return True

    def set_square(self, wire: dict, on: bool):
        """Right angles between this wire's points, whatever the drawing's wire style."""
        if on:
            wire["square"] = True
        else:
            wire.pop("square", None)
        self.changed()
        self.select_wire(wire["from"], wire["port"])

    def start_reconnect(self, item: "WireItem", which: str, pt: QPointF):
        self.reconnecting = (item.wire["from"], item.wire["port"], which)
        self._temp_wire()
        self.drag_reconnect(pt)

    def _reconnect_target(self, pt: QPointF):
        return next((i for i in self.scene.items(pt) if isinstance(i, PartItem)), None)

    def _reconnect_why(self, target) -> str | None:
        """Would this end go onto `target`? Tried on a copy of the rig, so nothing changes."""
        src, port, which = self.reconnecting
        trial = copy.deepcopy(self.rig)
        return R.reconnect(trial, src, port, which, target.node["id"])

    def drag_reconnect(self, pt: QPointF):
        if self.reconnecting is None or self.temp_wire is None:
            return
        src, port, which = self.reconnecting
        it = self.wire_item(src, port)
        a, b = it.ends() if it else (None, None)
        fixed = b if which == "from" else a
        if fixed is None:
            return
        target = self._reconnect_target(pt)
        why = self._reconnect_why(target) if target is not None else None
        end = (pt.x(), pt.y())
        path = QPainterPath(QPointF(fixed[0], fixed[1]))
        path.lineTo(QPointF(*end))
        self.temp_wire.setPath(path)
        new = target.node["id"] if target is not None and why is None else None
        if target is not None and why:
            self.show_notice(f"Can't connect here: {why}.", soft=True)
        elif target is not None:
            self.show_notice(None)
        if new != self.drop_target:
            old, self.drop_target = self.drop_target, new
            for i in (old, new):
                if i in self.items_by_id:
                    self.items_by_id[i].update()

    def end_reconnect(self, pt: QPointF) -> bool:
        rc, self.reconnecting = self.reconnecting, None
        old_target, self.drop_target = self.drop_target, None
        if self.temp_wire is not None:
            self.scene.removeItem(self.temp_wire)
            self.temp_wire = None
        if old_target in self.items_by_id:
            self.items_by_id[old_target].update()
        if rc is None:
            return False
        target = self._reconnect_target(pt)
        if target is None:
            self.status.emit("let go over a part to reconnect the wire's end there")
            return False
        src, port, which = rc
        why = R.reconnect(self.rig, src, port, which, target.node["id"])
        if why:
            self.show_notice(f"Not reconnected: {why}.")
            return False
        self.show_notice(None)
        self.sel_point = None
        self.changed()
        w = next((w for w in self.rig["wires"] if (w["from"], w["port"]) == (src, port)
                  or (which == "from" and w["from"] == target.node["id"])), None)
        if w is not None:
            self.select_wire(w["from"], w["port"])
        return True

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

    def set_lead(self, nid: str, key: str, value: str | None):
        """Relay 017: one field of an antenna's built-in lead (length, connector, note). Drawing and Output only."""
        node = R.box(self.rig, nid)
        if node is None:
            return
        lead = dict(R.antenna(node).get("lead") or {})
        if value in (None, ""):
            lead.pop(key, None)
        else:
            lead[key] = value
        self.set_antenna(nid, redraw=False, lead=lead or None)
        it = self.items_by_id.get(nid)
        if it is not None:
            it.prepareGeometryChange()
            it.update()
            self.fill_output()

    def choose_antenna(self, nid: str, where: QPoint):
        """On dropping an antenna: a small menu asks its type (Esc leaves it unset)."""
        m = Menu(self)
        m.add_heading("What kind of antenna?")
        for k, (label, _f) in R.ANTENNA_TYPES.items():
            a = m.addAction(QIcon(kind_pixmap("ant_" + k, T.color("text2"), 16, 2.0)), label)
            a.triggered.connect(lambda _=False, k=k: self.set_antenna(nid, type=k))
        m.open_at(where)

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
        if isinstance(item, PartItem) and G.is_bulkhead(item.node) and not d[2]:
            # relay 014: a bulkhead dragged on its own seats across an enclosure wall within reach, slides
            # along it (round the corners), and comes off with a firm drag away
            x, y = G.seat_near(self.rig, item.node, v.x(), v.y(), keep=True)
            if item.node.get("wall"):
                self._show_guides([])
                self._show_magnet(None)
                return QPointF(x, y)
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
        moved = bool(d and d[0] is lead and lead.pos() != d[1])
        if moved:                     # relay 015: wires whose two ends moved together keep their bends
            step = lead.pos() - d[1]
            ids = [self.box_id(lead)] + [self.box_id(it) for it in d[2]]
            G.carry_points(self.rig, ids, step.x(), step.y())
        return moved

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
            sel = self.scene.selectedItems()
            if self.sel_point and len(sel) == 1 and isinstance(sel[0], WireItem) and sel[0].sel_point() is not None:
                self.remove_bend(sel[0].wire)               # relay 015: Delete on a picked bend point removes it
                return True
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
            self.show_notice("This view is read-only: nothing here changes the rig."
                             if self.version_view else
                             "This rig is archived, so it is read-only. Restore it from the Rig menu to change it.")
            return
        if self._last is not None and not (merge is not None and merge == self._merge and self.undo_stack):
            self.undo_stack.append(self._last)
            del self.undo_stack[:-UNDO_MAX]
        self._merge = merge
        self.redo_stack.clear()
        self.words_gen += 1                                  # relay 016: words may have changed: lay them out again
        if G.settle_walls(self.rig) and not redraw:          # relay 014: a seated bulkhead stays on its wall
            redraw = True
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

    # -- words that fit (relay 016)
    def fit_text(self, nid: str) -> bool:
        """A part whose name or note no longer fits grows taller to show them, when there is room: up to the tallest
        a part may be, and only where it would not run into another box. Otherwise it keeps its size and its words
        end in "…" (whole in its tooltip). True when it grew."""
        it = self.items_by_id.get(nid)
        if it is None or self.read_only or R.is_locked(it.node):
            return False
        shape = G.oriented(R.shape_of(it.node), self.down)
        if shape == "pipe":
            return False
        self.words_gen += 1                     # the words just changed: never read them from the cache here
        w, h = it.wh()
        need = it.words(shape, w, h)["need"]
        if need <= h:
            return False
        new_h = min(G.MAX_H, int(math.ceil(need / R.GRID) * R.GRID))
        if new_h <= h:
            return False
        x, y = it.node["x"], it.node["y"]
        grown = (x, y + h, w, new_h - h)                # only the new strip has to be free
        for bid in [b["id"] for b in R._boxes(self.rig)]:
            if bid == nid or R.kind_of(self.rig, bid) == "enclosure":
                continue
            r = G.rect(self.rig, bid)
            if r is not None and G._overlap(grown, r):
                return False
        it.prepareGeometryChange()
        it.node["h"] = new_h
        self._refresh_wires()
        it.update()
        return True

    # -- direction (relay 016)
    @property
    def down(self) -> bool:
        """True when this rig is built top to bottom."""
        return G.direction(self.rig) == "down"

    def set_direction(self, d: str):
        """Left to right or top to bottom. Nothing moves until Tidy; new wires and free dots follow at once.
        One undo step."""
        if d not in R.DIRECTIONS or d == G.direction(self.rig):
            self._show_direction()
            return
        if d == "down":
            self.rig["dir"] = "down"
        else:
            self.rig.pop("dir", None)
        self.changed()
        if not self.read_only:
            self.status.emit(f"{R.DIRECTIONS[d]}: new wires follow it now; press Tidy to lay the rig out that way "
                             "(Undo puts it back)")

    def _show_direction(self):
        if not hasattr(self, "btn_dir"):
            return
        d = G.direction(self.rig)
        self.btn_dir.setText(R.DIRECTIONS[d] + " ▾")
        self.btn_dir.setIcon(tool_icon("dir_" + d))
        for k, a in self.dir_actions.items():
            a.setChecked(k == d)
        self.btn_dir.setEnabled(not self.read_only)

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

        before = {n["id"]: (n["x"], n["y"]) for n in self.rig["nodes"]}

        def done():
            for nid, (x, y) in moves.items():      # exact grid places, whatever the last frame was
                b = R.box(self.rig, nid)
                b["x"], b["y"] = x, y
            for ei, (x, y, w, h) in boxes.items():
                ei.enc.update(x=x, y=y, w=w, h=h)
            G.bends_after_move(self.rig, before)    # relay 015: bends that no longer fit come off (Undo keeps them)
            G.reseat(self.rig)
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
        if not anything:
            self.empty_hint.setText("This rig is empty.\nDrag a part from the list on the left onto the grid. "
                                    "To wire two parts, drag from a part's dot to the other part.")
        # relay 018: no rig open is the start panel (New rig, Open rig…, the recent ones); an open empty rig is the hint
        self.empty_hint.setVisible(has_rig and not anything)
        if not has_rig:
            self.start_panel.refresh()
        self.start_panel.setVisible(not has_rig)
        self.btn_dir.setEnabled(has_rig and not self.read_only)
        if not has_rig:
            self.state.setText("Start a new rig, or open one: File > Open rig… (Ctrl+O)")
        self._place_empty_hint()
        if getattr(self, "_soft", False) and "power source's dot" not in self.notice.text():
            self.show_notice(None)
        self._fill_checks()
        self._fill_text()
        self.fill_output()
        self.refresh_format()
        if self.minimap.isVisible():
            self.minimap.invalidate()
        if hasattr(self, "grid_status"):
            self.grid_status.refresh()

    CHECK_COLOUR = {"wiring": "anomaly", "radio": "survey", "power": "power", "loose": "muted"}

    def _fill_checks(self):
        """Relay 018: the Checks footer. Problems first, then warnings, then notes, each counted in its colour; the
        first message beside the counts; a click on it, or on any in the full list, selects what it is about."""
        self.check_list = R.checks_by_level(self.rig) if self.rig_name is not None else []
        empty = ("No problems found" if self.rig["nodes"] else
                 "Nothing on the grid yet" if self.rig_name is not None else "No rig open")
        self.footer.show_checks(self.check_list, empty)

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
                e.setWordWrap(True)
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
                lines = R.antenna_lines(node) or [("Type", "not set: choose it on the part's card")]
                if R.lead_text(node):
                    lines = lines + [("Built-in lead", R.lead_text(node))]
                secs.append(("Antenna", lines))
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
        if p["type"] == "pc":                         # relay 018: each USB input, read-only from Windows' list
            n_in = max([0] + [int(w.get("in", 0)) + 1 for w in self.rig["wires"] if w["to"] == node["id"]])
            for k in range(max(1, n_in)):
                radio = R.upstream_radio(self.rig, node["id"], k)
                text = (R._pname(radio) + "\n" + "\n".join(self.usb_lines(radio))) if radio else "no radio wired here"
                rows.append((f"USB input {k + 1}", text))
        if p["type"] == "sdr":
            for line in self.usb_lines(node):
                rows.append(("USB", line))
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
        if rep_.get("antenna_lead"):
            rows.append(("Antenna lead", f"{rep_['antenna_lead']} (built in; not in the recorded text)"))
        if rep_.get("chokes"):
            rows.append(("Chokes", f"{rep_['chokes']} on this path's cables"))
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
        if b == QMessageBox.StandardButton.Discard:
            self.discard_changes()
            return True
        return False

    def discard_changes(self):
        """Discard (relay 014): drop the unsaved edits. A rig that has been saved goes back to its saved
        drawing and stays in the list; the action that asked then carries on."""
        name = self.rig_name
        self.dirty = False
        if name in self.display:
            self.open_rig(name)
        else:
            self.rig_name, self.rig = None, R.new_rig()
            self.redraw()

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

    def new_rig(self, name: str | None = None, template: str | None = None, direction: str | None = None):
        """New rig (relay 014, the owner's flow): unsaved changes first (Save / Discard / Cancel), then a name, which
        way it is built (relay 016: left to right or top to bottom) and a starting drawing - blank, or one of relay
        007's templates, in that direction. A taken name is refused in the prompt. The
        rig is saved at once as a drawing (chain_layout.json), so it is in the picker from the start; its chains
        reach chain.json on the first Save that has a path."""
        if not self._ask_unsaved():
            return False
        if name is None:
            dlg = QDialog(self)
            dlg.setObjectName("NewRigDialog")
            dlg.setWindowTitle("New rig")
            lay = QVBoxLayout(dlg)
            lay.setContentsMargins(T.SPACE * 2, T.SPACE * 2, T.SPACE * 2, T.SPACE * 2)
            lay.setSpacing(T.SPACE)
            lab = QLabel("Name the new rig")
            lab.setProperty("muted", True)
            ed = QLineEdit()
            ed.setObjectName("NewRigName")
            lay.addWidget(lab)
            lay.addWidget(ed)
            err = QLabel("")
            err.setObjectName("NewRigError")
            err.setStyleSheet(f"color: {T.C['alert']};")
            err.setWordWrap(True)
            err.hide()
            lay.addWidget(err)
            # relay 016: which way the rig is built; changeable later from the toolbar
            lab_d = QLabel("Build it")
            lab_d.setProperty("muted", True)
            lay.addWidget(lab_d)
            drow = QHBoxLayout()
            drow.setSpacing(T.SPACE)
            dbtn = {}
            for key, text in R.DIRECTIONS.items():
                b = QPushButton(tool_icon("dir_" + key), text)
                b.setObjectName("Dir_" + key)
                b.setCheckable(True)
                b.setAutoExclusive(True)
                b.setProperty("accent", True)
                dbtn[key] = b
                drow.addWidget(b)
            dbtn["right"].setChecked(True)
            drow.addStretch(1)
            lay.addLayout(drow)
            lab2 = QLabel("Start from (optional)")
            lab2.setProperty("muted", True)
            lay.addWidget(lab2)
            pick = QListWidget()
            pick.setObjectName("Templates")
            for key, label in R.TEMPLATES.items():
                it = QListWidgetItem(label)
                it.setData(Qt.ItemDataRole.UserRole, key)
                it.setSizeHint(QSize(0, TEMPLATE_ROW))     # room for the theme's padded row box
                pick.addItem(it)
            pick.setFixedHeight(len(R.TEMPLATES) * TEMPLATE_ROW + 8)
            pick.setFixedWidth(250)
            # relay 015: the choice shows plainly - the picked start highlighted, a small preview of its shape
            # and one line saying what it gives. Blank is picked to begin with.
            prev = QLabel()
            prev.setObjectName("TemplatePreview")
            prev.setFixedSize(*TEMPLATE_PREVIEW)
            prev.setAlignment(Qt.AlignmentFlag.AlignCenter)
            note = QLabel("")
            note.setObjectName("TemplateNote")
            note.setProperty("muted", True)
            note.setWordWrap(True)
            side = QVBoxLayout()
            side.addWidget(prev)
            side.addWidget(note)
            side.addStretch(1)
            both = QHBoxLayout()
            both.addWidget(pick)
            both.addLayout(side, 1)
            lay.addLayout(both)

            def show_pick(*_):
                it = pick.currentItem()
                key = it.data(Qt.ItemDataRole.UserRole) if it else "blank"
                prev.setPixmap(template_pixmap(key, self.labels, self.receivers,
                                               dpr=dlg.devicePixelRatioF() if dlg.isVisible() else 2.0,
                                               down=dbtn["down"].isChecked()))
                note.setText(R.TEMPLATE_NOTES.get(key, ""))
            pick.currentRowChanged.connect(show_pick)
            for b in dbtn.values():
                b.toggled.connect(lambda on, *_: on and show_pick())
            pick.setCurrentRow(0)
            pick.item(0).setSelected(True)
            show_pick()
            row = QHBoxLayout()
            ok, cancel = QPushButton("Make rig"), QPushButton("Cancel")
            ok.setObjectName("MakeRig")
            ok.setDefault(True)

            def try_ok():
                n = ed.text().strip()
                why = ("Type a name for the rig." if not n else
                       f"There is already a rig or chain called “{n}”: pick another name." if self._taken(n) else "")
                err.setText(why)
                err.setVisible(bool(why))
                if not why:
                    dlg.accept()
            ok.clicked.connect(lambda *_: try_ok())
            cancel.clicked.connect(lambda *_: dlg.reject())
            row.addStretch(1)
            row.addWidget(cancel)
            row.addWidget(ok)
            lay.addLayout(row)
            self._new_rig_dlg = dlg
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return False
            name = ed.text()
            template = pick.currentItem().data(Qt.ItemDataRole.UserRole) if pick.currentItem() else "blank"
            direction = "down" if dbtn["down"].isChecked() else "right"
        name = (name or "").strip()
        if not name:
            return False
        if self._taken(name):
            self.show_notice(f"Not made: there is already a rig or chain called “{name}”.")
            return False
        rig = R.template(template or "blank", self.labels, self.receivers, down=direction == "down")
        try:
            lay = R.add_draft(self.layout_doc, name, rig, self.chain_doc())
            self.svc.save(self.chain_doc(), lay)
        except (ValueError, ServiceError) as exc:
            self.show_notice(f"Not made: {exc}")
            return False
        if not self.reload(keep=name) or self.rig_name != name:
            return False
        self.show_notice(("A new rig" + ("" if (template or "blank") == "blank" else
                                         f", from “{R.TEMPLATES[template]}”") +
                          ". Draw the rig, then Save: runs can use it once it has a path to a radio."), soft=True)
        self.status.emit(f"made “{name}”")
        return True

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
        if self.display.get(self.rig_name, {}).get("draft") and self.rig_name in (self.layout_doc.get("rigs") or {}):
            if self.dirty and not self._ask_unsaved():
                return
            lay = copy.deepcopy(self.layout_doc)                       # a draft: rename its drawing (relay 014)
            lay["rigs"][name] = lay["rigs"].pop(self.rig_name)
            try:
                self.svc.save(self.chain_doc(), lay)
            except ServiceError as exc:
                self.show_notice(f"Not renamed: {exc}")
                return
            self.reload(keep=name)
            self.status.emit(f"renamed to “{name}”")
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
            if R.old_word(entry["part"]):          # relay 017: a Jumper or Pigtail saved in My parts
                a = m.addAction("Convert to Coax / USB cable")
                a.setToolTip("Becomes: " + engine_chain.part_text(R.converted_part(entry["part"]))
                             + ". Parts already dropped into rigs keep their own copy.")
                a.triggered.connect(lambda: self.convert_lib_old(entry["id"]))
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
            m.open_at(where)

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
