"""Starting scans and recorder sessions from the window (relay 010).

Three panels, all asking the background service, which composes, checks and launches:

  StartScanDialog   saved scans (the main path): pick one, change only the free settings
                    (tag, duration, radio, analysis, conditions), check, Start. A full
                    new-scan setup (range strip, engine, locked and free settings, live
                    fingerprint, what the program will really do, warnings, trade-off table)
                    is one step away, on the same dialog.
  StartWatchDialog  `marsad watch`, survey mode (one radio surveys, the other records) or
                    fixed mode (one radio parked on a frequency).

Nothing here computes a figure of its own: geometry, fingerprint, warnings and the command all
come from the service (marsad/live.py scan_plan), which takes them from the engine. The
command is hidden until "Show command" opens it in a side panel.
"""
from __future__ import annotations

import copy

from PySide6.QtCore import QObject, QPointF, QRectF, QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QDialog, QDoubleSpinBox,
                               QFrame, QGridLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton,
                               QScrollArea, QSizePolicy, QSpinBox, QSplitter, QStackedWidget, QTableWidget,
                               QTableWidgetItem, QToolButton, QVBoxLayout, QWidget)

from marsad_desktop import explain as X
from marsad_desktop import rig as R
from marsad_desktop import theme as T
from marsad_desktop.service import Service, ServiceError

SKY = ["", "clear", "sunny", "overcast", "raining", "night, clear", "night, overcast"]
FFT_BINS = [64, 128, 256, 512, 1024, 2048, 4096, 8192]
LEVEL_COLOUR = {"bad": "alert", "warn": "anomaly", "ok": "open"}
NO_CONDITIONS = ["bad", "Conditions", "Nothing typed, so the run would record no conditions. Fill in at least one "
                                      "(the weather is measured on its own; this is what the room was doing)."]
LEVEL_WORD = {"bad": "Problem", "warn": "Warning", "ok": "Note"}
FREE_KEYS = ("tag", "durVal", "unit", "rx", "analyze", "useskip", "skipmin", "thresh", "floorwin")
COND_KEYS = ("temp", "humid", "where", "sky", "notes")
NEW_SET_TEXT = ("This starts a new comparison set: runs with the old settings can't be compared with it.")


# ---------------------------------------------------------------- calls off the window's thread

class _Done(QObject):
    done = Signal(object, object)


class _Job(QRunnable):
    def __init__(self, fn, sig):
        super().__init__()
        self.fn, self.sig = fn, sig

    def run(self):
        try:
            out, err = self.fn(), None
        except ServiceError as exc:
            out, err = None, str(exc)
        except Exception as exc:  # noqa: BLE001 - shown to the owner, never a crash
            out, err = None, f"{type(exc).__name__}: {exc}"
        self.sig.done.emit(out, err)


_LIVE: set = set()


def call(fn, then):
    """Run a service call on the pool; `then(result, error)` on the window's thread."""
    sig = _Done()
    _LIVE.add(sig)

    def fin(out, err):
        _LIVE.discard(sig)
        then(out, err)
    sig.done.connect(fin, Qt.ConnectionType.QueuedConnection)
    QThreadPool.globalInstance().start(_Job(fn, sig))


# ---------------------------------------------------------------- small pieces

def mark_radio_item(cb, i: int, r: dict, text: str):
    """A radio in a picker: its words, and, when switched off on Live Scan, "disabled" and not
    selectable (relay 011: a disabled radio is never offered). A picker already set to it keeps
    showing it, so the checks can say why it cannot start."""
    off = r.get("enabled") is False
    cb.setItemText(i, text + ("  · disabled" if off else ""))
    item = cb.model().item(i) if hasattr(cb.model(), "item") else None
    if item is not None:
        item.setEnabled(not off)


def label(text, muted=False, size=None, bold=False, wrap=False):
    w = QLabel(text)
    if muted:
        w.setProperty("muted", True)
    if size or bold:
        w.setFont(T.ui_font(size or 10, QFont.Weight.DemiBold if bold else None))
    w.setWordWrap(wrap)
    return w


def badge(text, colour):
    w = QLabel(text)
    w.setObjectName("Pill")
    w.setStyleSheet(f"color: {T.C[colour]}; border: 1px solid {T.rgba(colour, 0.55)};")
    return w


def tool(text, tip="", kind="Tool"):
    b = QToolButton()
    b.setObjectName(kind)
    b.setText(text)
    b.setToolTip(tip)
    return b


def section(title, badge_text=None, badge_colour="muted", key=None):
    box = QFrame()
    box.setObjectName("TextPanel")
    v = QVBoxLayout(box)
    v.setContentsMargins(T.SPACE * 1.5, T.SPACE * 1.25, T.SPACE * 1.5, T.SPACE * 1.5)
    v.setSpacing(T.SPACE)
    h = QHBoxLayout()
    h.addWidget(label(title, size=10, bold=True))
    if key:
        h.addWidget(X.info(key))
    if badge_text:
        h.addWidget(badge(badge_text, badge_colour))
    h.addStretch(1)
    v.addLayout(h)
    return box, v, h


def dspin(lo, hi, dec, step=1.0, suffix=""):
    w = QDoubleSpinBox()
    w.setRange(lo, hi)
    w.setDecimals(dec)
    w.setSingleStep(step)
    w.setKeyboardTracking(False)
    if suffix:
        w.setSuffix(suffix)
    return w


def link_units(spin, combo, factors=(60.0, 3600.0)):
    """A value with a unit choice converts when the unit changes (relay 011): 0.5 hours switched
    to minutes reads 30 min, and back. The value typed last is remembered exactly, so 7 min shown
    as 0.12 hours comes back as 7 min, not 7.2."""
    state = {"i": combo.currentIndex(), "exact": None, "converting": False}

    def edited(_v=None):
        if not state["converting"]:
            state["exact"] = (combo.currentIndex(), spin.value())

    def unit(i):
        old = state["i"]
        state["i"] = i
        if old == i or old < 0:
            return
        ex = state["exact"]
        spin.blockSignals(True)
        if ex and ex[0] == i:
            spin.setValue(ex[1])
        else:
            spin.setValue(spin.value() * factors[old] / factors[i])
            if ex is None or ex[0] != old:
                state["exact"] = (old, spin.value() * factors[i] / factors[old])
        spin.blockSignals(False)
        state["converting"] = True
        try:
            spin.valueChanged.emit(spin.value())
        finally:
            state["converting"] = False

    def reset():
        state["i"], state["exact"] = combo.currentIndex(), None
    spin.valueChanged.connect(edited)
    combo.currentIndexChanged.connect(unit)
    return reset


def _word(name) -> str:
    name = str(name or "")
    return name.upper() if name.isalpha() else name


def clear_layout(lay) -> None:
    """Take every widget out of a layout now (deleteLater alone leaves them drawn until the
    event loop runs, so new rows painted over old ones)."""
    while lay.count():
        it = lay.takeAt(0)
        w = it.widget()
        if w is not None:
            w.hide()
            w.setParent(None)
            w.deleteLater()


def jsnum(v) -> str:
    v = float(v)
    return str(int(v)) if v.is_integer() else repr(v)


rig_checks_for_runs = R.checks_for_runs


# ---------------------------------------------------------------- the range strip

class RangeStrip(QWidget):
    """Choose a range on the receiver's whole tuning range, in three strips, each a zoom into
    the one above (one bin is far too small to draw on a whole sweep, as the Builder's map said):
    the tuner's range with its PLL gap and the selection; the scan with its hops, crop zones and
    hop-centre DC spikes; and one hop with its bins. Drag on the top strip to choose a range,
    drag an edge to move it, drag inside to slide it. `ranges` holds a list so multi-range
    ("section") scans can be added later without reshaping this; one range is used today."""
    rangeChanged = Signal(float, float)

    H = 46

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(self.H * 3 + 54)
        self.setMouseTracking(True)
        self.tuner = (24.0, 1766.0)
        self.gaps: list = []
        self.ranges = [(100.0, 200.0)]
        self.target = None
        self.geo: dict = {}
        self.crop = 0.25
        self.engine = "rtl_power"
        self._drag = None
        self.view = None              # the hop row's visible (lo, hi) MHz; None = the whole scan
        self._zoom_hops = 5.0         # how many hops the zoomed view shows, kept as settings change
        self._user_zoom = False
        self._buttons = {}

    def set_receiver(self, rng, gaps):
        self.tuner = (float(rng[0]), float(rng[1]))
        self.gaps = [(float(a), float(b)) for a, b in gaps or []]
        self.update()

    def set_range(self, lo, hi):
        self.ranges = [(float(lo), float(hi))]
        self.update()

    def set_plan(self, geo: dict, crop: float, engine: str, target):
        self.geo, self.crop, self.engine, self.target = geo or {}, crop, engine, target
        self._fit_view()
        self.update()

    # -- the hop row's zoom (relay 011): opens on a few hops around the target, or the middle
    def _hop_layout(self):
        """(centres, raw hop width, step between centres, span lo, span hi), from the engine's plan."""
        centres = list(self.geo.get("hop_centres_mhz") or [])
        lo, hi = self.ranges[0]
        hop = float(self.geo.get("hop_mhz") or 0)
        if self.engine == "rtl_power":
            step = hop
            width = hop / max(1e-9, 1 - self.crop) if hop else 0       # rtl_power's hop is wider than its kept part
        else:
            width = hop
            step = float(self.geo.get("hop_spacing_mhz") or hop)
        span_lo, span_hi = lo, hi
        if centres and width:
            span_lo, span_hi = min(lo, centres[0] - width / 2), max(hi, centres[-1] + width / 2)
        return centres, width, step, span_lo, span_hi

    def _fit_view(self):
        centres, width, step, a, b = self._hop_layout()
        if not centres or not step:
            self.view = None
            return
        want = self._zoom_hops * step
        if want >= (b - a) * 0.98:
            self.view = None
            return
        if self.view and self._user_zoom:
            c = (self.view[0] + self.view[1]) / 2
        else:
            c = self.target if self.target and a <= self.target <= b else (a + b) / 2
        c = min(max(c, a + want / 2), b - want / 2)
        self.view = (c - want / 2, c + want / 2)

    def zoom(self, factor: float, about: float | None = None):
        """factor < 1 zooms in. About a frequency (the pointer), else the view's middle."""
        centres, width, step, a, b = self._hop_layout()
        if not step:
            return
        va, vb = self.view or (a, b)
        about = (va + vb) / 2 if about is None else about
        span = min(b - a, max(step * 0.6, (vb - va) * factor))
        frac = (about - va) / max(1e-9, vb - va)
        na = about - frac * span
        na = min(max(na, a), b - span)
        self._user_zoom = True
        if span >= (b - a) * 0.98:
            self.view = None
            self._zoom_hops = (b - a) / step
        else:
            self.view = (na, na + span)
            self._zoom_hops = span / step
        self.update()

    def zoom_fit(self):
        self._user_zoom = True
        self.view = None
        centres, width, step, a, b = self._hop_layout()
        self._zoom_hops = (b - a) / step if step else 5.0
        self.update()

    def wheelEvent(self, ev):
        r = self._track(1)
        if not r.adjusted(0, -18, 0, 6).contains(ev.position()):
            return super().wheelEvent(ev)
        centres, width, step, a, b = self._hop_layout()
        va, vb = self.view or (a, b)
        f = self._f(ev.position().x(), va, vb, r)
        self.zoom(0.8 if ev.angleDelta().y() > 0 else 1.25, f)
        ev.accept()

    # geometry of the drawing
    def _track(self, row):
        m = 14
        return QRectF(m, 18 + row * (self.H + 18), self.width() - 2 * m, self.H - 14)

    def _fx(self, f, a, b, r):
        return r.left() + (f - a) / max(1e-9, b - a) * r.width()

    def _f(self, x, a, b, r):
        return a + (x - r.left()) / max(1e-9, r.width()) * (b - a)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setFont(T.ui_font(8))
        lo, hi = self.ranges[0]
        a, b = self.tuner
        # 1: the tuner
        r = self._track(0)
        self._strip(p, r, f"Tuner: {jsnum(a)}–{jsnum(b)} MHz")
        for ga, gb in self.gaps:
            x0, x1 = self._fx(max(a, ga), a, b, r), self._fx(min(b, gb), a, b, r)
            p.fillRect(QRectF(x0, r.top(), max(1, x1 - x0), r.height()),
                       QBrush(T.color("alert", 0.35), Qt.BrushStyle.BDiagPattern))
            p.setPen(T.color("alert_text"))
            p.drawText(QRectF(x0, r.bottom() + 1, max(60, x1 - x0), 12), Qt.AlignmentFlag.AlignLeft, "PLL gap")
        x0, x1 = self._fx(max(a, lo), a, b, r), self._fx(min(b, hi), a, b, r)
        p.fillRect(QRectF(x0, r.top(), max(2, x1 - x0), r.height()), T.color("survey", 0.30))
        p.setPen(QPen(T.color("survey"), 2))
        p.drawLine(QPointF(x0, r.top() - 3), QPointF(x0, r.bottom() + 3))
        p.drawLine(QPointF(x1, r.top() - 3), QPointF(x1, r.bottom() + 3))
        self._target(p, r, a, b)
        # 2: the scan, its hops, crop zones and DC spikes; zoomable
        self._paint_hops(p, self._track(1), lo, hi)
        # 3: one hop, its bins
        r = self._track(2)
        fft = int(self.geo.get("fft") or 0)
        kept = int(self.geo.get("kept_bins_per_hop") or 0)
        bin_k = float(self.geo.get("bin_khz_predicted") or 0)
        self._strip(p, r, f"One hop: {fft} bins of {bin_k:.4f} kHz, {kept} kept, the rest cropped; "
                          "the DC spike sits at its centre")
        if fft:
            edge = (fft - kept) / 2
            w = r.width() / fft
            p.fillRect(QRectF(r.left(), r.top(), edge * w, r.height()), T.color("anomaly", 0.22))
            p.fillRect(QRectF(r.right() - edge * w, r.top(), edge * w, r.height()), T.color("anomaly", 0.22))
            if fft <= 512:
                p.setPen(QPen(T.color("lines"), 1))
                for i in range(fft + 1):
                    x = r.left() + i * w
                    p.drawLine(QPointF(x, r.top() + r.height() * 0.55), QPointF(x, r.bottom()))
            p.setPen(QPen(T.color("anomaly"), 2))
            p.drawLine(QPointF(r.center().x(), r.top()), QPointF(r.center().x(), r.bottom()))
            p.setPen(T.color("muted"))
            p.drawText(QRectF(r.left(), r.bottom() + 1, r.width(), 12), Qt.AlignmentFlag.AlignLeft, "cropped")
            p.drawText(QRectF(r.left(), r.bottom() + 1, r.width(), 12), Qt.AlignmentFlag.AlignRight, "cropped")
            p.drawText(QRectF(r.left(), r.bottom() + 1, r.width(), 12), Qt.AlignmentFlag.AlignHCenter, "DC spike")
        p.end()

    def _paint_hops(self, p, r, lo, hi):
        centres, hop_w, step, span_lo, span_hi = self._hop_layout()
        va, vb = self.view or (span_lo, span_hi)
        n = len(centres)
        title = f"Scan: {jsnum(lo)}–{jsnum(hi)} MHz, {n} hop{'s' if n != 1 else ''}"
        vis = [c for c in centres if c + hop_w / 2 >= va and c - hop_w / 2 <= vb]
        if self.view:
            title += f"  ·  showing {va:.3f}–{vb:.3f} MHz ({len(vis)} hop{'s' if len(vis) != 1 else ''})"
        self._strip(p, r, title)
        self._zoom_buttons(p, r)
        p.save()
        p.setClipRect(r)
        fx = lambda f: self._fx(f, va, vb, r)
        close = bool(vis) and len(vis) <= 24
        if vis and len(vis) <= 400:
            fft = int(self.geo.get("fft") or 0)
            for i, c in enumerate(vis):
                xa, xb = fx(c - hop_w / 2), fx(c + hop_w / 2)
                cw = (xb - xa) * self.crop / 2
                p.fillRect(QRectF(xa, r.top() + 2, max(0.5, cw), r.height() - 4), T.color("anomaly", 0.18))
                p.fillRect(QRectF(xb - cw, r.top() + 2, max(0.5, cw), r.height() - 4), T.color("anomaly", 0.18))
                if close:
                    # overlap with the next hop: where both hops listen (one of them crops it away)
                    if i + 1 < len(vis):
                        oa, ob = vis[i + 1] - hop_w / 2, c + hop_w / 2
                        if ob > oa:
                            p.fillRect(QRectF(fx(oa), r.top() + 2, max(1.0, fx(ob) - fx(oa)), r.height() * 0.3),
                                       QBrush(T.color("survey", 0.55), Qt.BrushStyle.BDiagPattern))
                    # bins, when there is room to draw them
                    if fft and (xb - xa) / fft >= 3:
                        p.setPen(QPen(T.color("lines"), 1))
                        for k in range(fft + 1):
                            x = xa + (xb - xa) * k / fft
                            p.drawLine(QPointF(x, r.top() + r.height() * 0.7), QPointF(x, r.bottom() - 2))
                    p.setPen(QPen(T.color("text2"), 1.2))
                    p.drawLine(QPointF(xa, r.top() + 1), QPointF(xa, r.bottom() - 1))
                    p.drawLine(QPointF(xb, r.top() + 1), QPointF(xb, r.bottom() - 1))
                else:
                    p.setPen(QPen(T.color("lines"), 1))
                    p.drawLine(QPointF(xa, r.top() + 2), QPointF(xa, r.bottom() - 2))
            p.setPen(QPen(T.color("anomaly"), 1.3 if not close else 2.0))
            for c in vis:
                x = fx(c)
                p.drawLine(QPointF(x, r.top() + r.height() * (0.35 if not close else 0.15)), QPointF(x, r.bottom()))
        elif n:
            p.setPen(T.color("muted"))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, f"{n} hops: too many to draw one by one; zoom in to see them")
        x0, x1 = fx(lo), fx(hi)
        p.setPen(QPen(T.color("survey"), 1.5))
        p.drawRect(QRectF(x0, r.top(), x1 - x0, r.height()))
        self._target(p, r, va, vb)
        p.restore()
        if close and len(vis) <= 8:
            p.setPen(T.color("muted"))
            p.setFont(T.ui_font(7))
            for c in vis:
                x = fx(c)
                if r.left() + 45 <= x <= r.right() - 45:
                    p.drawText(QRectF(x - 45, r.bottom() + 1, 90, 11), Qt.AlignmentFlag.AlignHCenter,
                               f"centre {c:.3f}")
            p.setFont(T.ui_font(8))
        elif close:
            p.setPen(T.color("muted"))
            p.drawText(QRectF(r.left(), r.bottom() + 1, r.width(), 11), Qt.AlignmentFlag.AlignLeft,
                       "edges · centre spike · cropped edges shaded · overlap hatched")

    def _zoom_buttons(self, p, r):
        """Three small buttons on the hop row's title line: zoom out, fit, zoom in."""
        self._buttons = {}
        x = r.right()
        p.setFont(T.ui_font(8))
        for key, text in (("in", "+"), ("fit", "fit"), ("out", "−")):
            w = 22 if key != "fit" else 30
            rect = QRectF(x - w, r.top() - 16, w, 14)
            self._buttons[key] = rect
            p.setPen(QPen(T.color("lines"), 1))
            p.setBrush(T.color("raised"))
            p.drawRoundedRect(rect, 3, 3)
            p.setPen(T.color("text2"))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
            x -= w + 4
        p.setBrush(Qt.BrushStyle.NoBrush)

    def _strip(self, p, r, title):
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(T.color("raised"))
        p.drawRoundedRect(r, 3, 3)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(T.color("text2"))
        p.drawText(QRectF(r.left(), r.top() - 15, r.width(), 13), Qt.AlignmentFlag.AlignLeft, title)

    def _target(self, p, r, a, b):
        if self.target is None or not (a <= self.target <= b):
            return
        x = self._fx(self.target, a, b, r)
        p.setPen(QPen(T.color("open"), 1.6, Qt.PenStyle.DashLine))
        p.drawLine(QPointF(x, r.top() - 4), QPointF(x, r.bottom() + 4))

    # dragging on the tuner strip
    def mousePressEvent(self, ev):
        for key, rect in self._buttons.items():
            if rect.contains(ev.position()):
                {"in": lambda: self.zoom(0.5), "out": lambda: self.zoom(2.0), "fit": self.zoom_fit}[key]()
                return
        r = self._track(0)
        if not r.adjusted(0, -6, 0, 6).contains(ev.position()):
            return
        a, b = self.tuner
        lo, hi = self.ranges[0]
        x = ev.position().x()
        x0, x1 = self._fx(lo, a, b, r), self._fx(hi, a, b, r)
        f = self._f(x, a, b, r)
        if abs(x - x0) <= 6:
            self._drag = ("lo", f)
        elif abs(x - x1) <= 6:
            self._drag = ("hi", f)
        elif x0 < x < x1:
            self._drag = ("move", f, lo, hi)
        else:
            self._drag = ("new", f)

    def mouseMoveEvent(self, ev):
        r = self._track(0)
        a, b = self.tuner
        x = ev.position().x()
        if self._drag is None:
            lo, hi = self.ranges[0]
            near = min(abs(x - self._fx(lo, a, b, r)), abs(x - self._fx(hi, a, b, r))) <= 6 and \
                r.adjusted(0, -6, 0, 6).contains(ev.position())
            self.setCursor(Qt.CursorShape.SizeHorCursor if near else Qt.CursorShape.ArrowCursor)
            return
        f = min(b, max(a, self._f(x, a, b, r)))
        step = 1.0 if (b - a) > 200 else 0.1
        f = round(f / step) * step
        lo, hi = self.ranges[0]
        kind = self._drag[0]
        if kind == "lo":
            lo = min(f, hi - step)
        elif kind == "hi":
            hi = max(f, lo + step)
        elif kind == "move":
            d = round((f - self._drag[1]) / step) * step
            w = self._drag[3] - self._drag[2]
            lo = min(max(a, self._drag[2] + d), b - w)
            hi = lo + w
        else:
            s = round(self._drag[1] / step) * step
            lo, hi = (min(s, f), max(s, f)) if abs(f - s) >= step else (s, s + step)
        self.ranges = [(round(lo, 3), round(hi, 3))]
        self.update()

    def mouseReleaseEvent(self, ev):
        if self._drag is not None:
            self._drag = None
            self.rangeChanged.emit(*self.ranges[0])


# ---------------------------------------------------------------- shared panels

class CommandPanel(QFrame):
    """"Show command": the complete command, hidden until asked for, with a Copy button."""

    def __init__(self):
        super().__init__()
        self.setObjectName("TextPanel")
        v = QVBoxLayout(self)
        v.setContentsMargins(T.SPACE * 1.5, T.SPACE * 1.25, T.SPACE * 1.5, T.SPACE * 1.5)
        h = QHBoxLayout()
        h.addWidget(label("Command", size=10, bold=True))
        h.addStretch(1)
        self.copy = tool("Copy", "Copy the command to the clipboard")
        self.copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.text.toPlainText()))
        h.addWidget(self.copy)
        v.addLayout(h)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setFont(T.mono_font(8.5))
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        v.addWidget(self.text, 1)
        self.note = label("This is what is launched (as `marsad --station <folder> ...`, with the device found by "
                          "serial). Every run records it in its sidecar and scan_build.md.", muted=True, wrap=True)
        self.note.setFont(T.ui_font(8))
        v.addWidget(self.note)
        self.setMinimumWidth(320)

    def show_text(self, text: str):
        if self.text.toPlainText() != text:
            self.text.setPlainText(text)


class ChecksPanel(QFrame):
    """The checks before a start, in plain words: radio in use, radio found by serial, the chain
    (with "still built as drawn?" when due), the Chain screen's open checks, and the warnings."""
    changed = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName("TextPanel")
        self.v = QVBoxLayout(self)
        self.v.setContentsMargins(T.SPACE * 1.5, T.SPACE * 1.25, T.SPACE * 1.5, T.SPACE * 1.5)
        self.v.setSpacing(6)
        h = QHBoxLayout()
        h.addWidget(label("Before starting", size=10, bold=True))
        h.addWidget(X.info("checks"))
        h.addStretch(1)
        self.again = tool("Check again", "Ask again: radios in use, each radio by its serial (rtl_eeprom reads it), "
                                         "the chain")
        h.addWidget(self.again)
        self.v.addLayout(h)
        self.rows = QVBoxLayout()
        self.rows.setSpacing(5)
        self.v.addLayout(self.rows)
        self.confirm = QCheckBox("Yes, the rig is still built as drawn")
        self.confirm.toggled.connect(lambda *_: self.changed.emit())
        self.v.addWidget(self.confirm)
        self.problems = QCheckBox("Start despite the problems above")
        self.problems.toggled.connect(lambda *_: self.changed.emit())
        self.v.addWidget(self.problems)
        self.confirm.hide()
        self.problems.hide()
        self.blocked = True
        self.due = False
        self.bad = 0

    def _clear(self):
        clear_layout(self.rows)

    def _row(self, level, title, text):
        w = QLabel(f"<span style='color:{T.C[LEVEL_COLOUR.get(level, 'muted')]}; font-weight:600'>{title}</span>"
                   f"&nbsp; <span style='color:{T.C['text2']}'>{text}</span>")
        w.setWordWrap(True)
        w.setTextFormat(Qt.TextFormat.RichText)
        self.rows.addWidget(w)

    def show_checks(self, radios: dict, in_use: dict | None, warnings: list, rig_checks: list,
                    pending: str | None = None):
        """radios: {rx: {"radio": ..., "chain": ...}}."""
        self._clear()
        self.blocked, self.due, self.bad = False, False, 0
        if pending:
            self._row("warn", "Checking", pending)
        if in_use is not None:
            if in_use.get("blocked"):
                self.blocked = True
                self._row("bad", "Radio in use", in_use["text"])
            else:
                self._row("ok", "Radios", in_use.get("text", ""))
        for rx, c in radios.items():
            r = c.get("radio") or {}
            if r.get("found") is True:
                self._row("ok", f"{_word(rx)} by serial", r["text"])
            elif r.get("found") is False:
                self.blocked = True
                self._row("bad", f"{_word(rx)} disabled" if r.get("disabled") else f"{_word(rx)} not found", r["text"])
            else:
                self._row("warn", f"{_word(rx)} by serial", r.get("text") or "Not checked yet.")
            ch = c.get("chain") or {}
            if ch.get("problem"):
                self.blocked = True
                self._row("bad", f"Chain ({_word(rx)})", ch["problem"])
            elif ch.get("text"):
                extra = ""
                if ch.get("due"):
                    self.due = True
                    used = str(ch.get("used_at") or "").replace("T", " ")
                    extra = f" Already recorded by a run at {used}: is the rig still built as drawn?"
                self._row("warn" if ch.get("due") else "ok", f"Chain ({_word(rx)})",
                          f"from {ch.get('source')}: <span style='font-family:Consolas'>{ch['text']}</span>.{extra}")
        for c in rig_checks:
            self._row("warn", "Chain check", f"{c.get('text')} (rig “{c.get('rig')}”)")
        for lvl, title, text in warnings:
            if lvl == "bad":
                self.bad += 1
            self._row(lvl, title, text)
        self.confirm.setVisible(self.due)
        if not self.due:
            self.confirm.setChecked(False)
        self.problems.setVisible(self.bad > 0)
        if not self.bad:
            self.problems.setChecked(False)
        self.changed.emit()

    def ready(self) -> tuple[bool, str]:
        if self.blocked:
            return False, "A check above stops the start"
        if self.due and not self.confirm.isChecked():
            return False, "Confirm the rig is still built as drawn"
        if self.bad and not self.problems.isChecked():
            return False, "Confirm the problems first, or change the settings"
        return True, ""


class ConditionsBox(QFrame):
    """The Builder's conditions questions; prefilled from the last answers."""
    changed = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName("TextPanel")
        g = QGridLayout(self)
        g.setContentsMargins(T.SPACE * 1.5, T.SPACE * 1.25, T.SPACE * 1.5, T.SPACE * 1.5)
        g.setHorizontalSpacing(T.SPACE)
        hd = QHBoxLayout()
        hd.addWidget(label("Conditions", size=10, bold=True))
        hd.addStretch(1)
        g.addLayout(hd, 0, 0, 1, 4)
        rh = QHBoxLayout()
        rh.addWidget(label("Room (you enter)", bold=True))
        rh.addWidget(X.info("room"))
        rh.addWidget(badge("Recorded as typed", "muted"))
        rh.addStretch(1)
        self.same = tool("Same as last time", "Put back the conditions you gave last time")
        X.attach(self.same, "same_as_last")
        self.same.clicked.connect(self._same)
        rh.addWidget(self.same)
        g.addLayout(rh, 1, 0, 1, 4)
        self.temp, self.humid, self.notes = QLineEdit(), QLineEdit(), QLineEdit()
        self.temp.setPlaceholderText("°C")
        self.humid.setPlaceholderText("%")
        self.notes.setPlaceholderText("what is switched on in the room, anything unusual")
        self.where, self.sky = QComboBox(), QComboBox()
        self.where.addItems(["inside", "outside"])
        self.sky.addItems([s or "—" for s in SKY])
        for col, (t, key, w) in enumerate((("Temperature °C", "room_temp", self.temp),
                                           ("Humidity %", "room_humidity", self.humid),
                                           ("Measured where", "room_where", self.where),
                                           ("Sky", "room_sky", self.sky))):
            g.addWidget(X.titled(t, key), 2, col)
            g.addWidget(X.attach(w, key), 3, col)
        g.addWidget(X.titled("Anything else", "room_notes"), 4, 0, 1, 4)
        g.addWidget(X.attach(self.notes, "room_notes"), 5, 0, 1, 4)
        self.line = label("", muted=True, wrap=True)
        self.line.setFont(T.mono_font(8))
        g.addWidget(self.line, 6, 0, 1, 4)
        oh = QHBoxLayout()
        oh.addWidget(label("Outdoor (added automatically at start)", bold=True))
        oh.addWidget(X.info("outdoor"))
        oh.addStretch(1)
        g.addLayout(oh, 7, 0, 1, 4)
        self.outdoor = label("Outdoor weather (temperature, humidity, pressure, cloud, rain, wind) and the sun's "
                             "elevation are recorded with every run, at its start and end, from the station's "
                             "location, which is kept private and not shown here. Without internet, or with no "
                             "location set, the run carries on and its record says why they are missing.",
                             muted=True, wrap=True)
        g.addWidget(self.outdoor, 8, 0, 1, 4)
        self.last: dict = {}
        for w in (self.temp, self.humid, self.notes):
            w.textChanged.connect(lambda *_: self.changed.emit())
        for w in (self.where, self.sky):
            w.currentIndexChanged.connect(lambda *_: self.changed.emit())

    def set_values(self, d: dict):
        for w in (self.temp, self.humid, self.notes, self.where, self.sky):
            w.blockSignals(True)
        self.temp.setText(str(d.get("temp") or ""))
        self.humid.setText(str(d.get("humid") or ""))
        self.notes.setText(str(d.get("notes") or ""))
        self.where.setCurrentText(d.get("where") or "inside")
        self.sky.setCurrentIndex(SKY.index(d.get("sky")) if d.get("sky") in SKY else 0)
        for w in (self.temp, self.humid, self.notes, self.where, self.sky):
            w.blockSignals(False)

    def set_last(self, d: dict):
        """The answers given last time, for Same as last time."""
        self.last = dict(d or {})
        self.same.setEnabled(bool(self.last))

    def _same(self):
        if self.last:
            self.set_values(self.last)
            self.changed.emit()

    def values(self) -> dict:
        return {"temp": self.temp.text().strip(), "humid": self.humid.text().strip(),
                "where": self.where.currentText(), "sky": SKY[self.sky.currentIndex()],
                "notes": self.notes.text().strip()}


class FreeSettings(QFrame):
    """The settings that never change comparability: tag, duration, radio, analysis options."""
    changed = Signal()

    def __init__(self, title="Change for this run", show_radio=True):
        super().__init__()
        self.setObjectName("TextPanel")
        g = QGridLayout(self)
        g.setContentsMargins(T.SPACE * 1.5, T.SPACE * 1.25, T.SPACE * 1.5, T.SPACE * 1.5)
        g.setHorizontalSpacing(T.SPACE)
        g.addWidget(label(title, size=10, bold=True), 0, 0, 1, 2)
        g.addWidget(badge("Free to change", "open"), 0, 2, 1, 2,
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        self.tag = QLineEdit()
        self.tag.setPlaceholderText("my-run")
        self.dur = dspin(0.01, 10000, 2, 1)
        self.unit = QComboBox()
        self.unit.addItems(["min", "hours"])
        X.attach(self.unit, "duration_unit")
        self.rx = QComboBox()
        g.addWidget(X.titled("Tag", "tag"), 1, 0)
        g.addWidget(X.attach(self.tag, "tag"), 2, 0)
        g.addWidget(X.titled("Duration", "duration"), 1, 1)
        d = QHBoxLayout()
        d.addWidget(X.attach(self.dur, "duration"), 1)
        d.addWidget(self.unit)
        g.addLayout(d, 2, 1)
        self.unit_reset = link_units(self.dur, self.unit)
        self.rx_label = X.titled("Radio", "radio")
        g.addWidget(self.rx_label, 1, 2, 1, 2)
        g.addWidget(X.attach(self.rx, "radio"), 2, 2, 1, 2)
        self.rx_label.setVisible(show_radio)
        self.rx.setVisible(show_radio)
        self.analyze = QCheckBox("Analyse when recording finishes")
        self.useskip = QCheckBox("Discard warm-up")
        self.skipmin = dspin(0, 600, 0, 1, " min")
        X.attach(self.skipmin, "warmup")
        self.thresh = dspin(0, 60, 1, 0.5, " dB")
        self.floorwin = dspin(0, 100, 2, 0.1, " MHz")
        self.floorwin.setSpecialValueText("auto")
        g.addWidget(X.with_info(self.analyze, "analyze"), 3, 0, 1, 4)
        g.addWidget(X.with_info(self.useskip, "warmup"), 4, 0)
        g.addWidget(self.skipmin, 4, 1)
        g.addWidget(X.titled("Detection threshold", "threshold"), 5, 0)
        g.addWidget(X.attach(self.thresh, "threshold"), 5, 1)
        g.addWidget(X.titled("Floor window", "floor_window"), 5, 2)
        g.addWidget(X.attach(self.floorwin, "floor_window"), 5, 3)
        g.setRowStretch(6, 1)
        for w in (self.tag,):
            w.textChanged.connect(lambda *_: self.changed.emit())
        for w in (self.dur, self.skipmin, self.thresh, self.floorwin):
            w.valueChanged.connect(lambda *_: self.changed.emit())
        for w in (self.unit, self.rx):
            w.currentIndexChanged.connect(lambda *_: self.changed.emit())
        for w in (self.analyze, self.useskip):
            w.toggled.connect(lambda *_: self.changed.emit())

    def set_receivers(self, receivers: dict):
        self.rx.blockSignals(True)
        self.rx.clear()
        for name, r in receivers.items():
            self.rx.addItem(f"{_word(name)}  ({r.get('label', name)})", name)
        self.rx.blockSignals(False)

    def set_values(self, s: dict):
        ws = (self.tag, self.dur, self.unit, self.rx, self.analyze, self.useskip, self.skipmin, self.thresh,
              self.floorwin)
        for w in ws:
            w.blockSignals(True)
        self.tag.setText(s.get("tag") or "")
        self.dur.setValue(float(s.get("durVal") or 1))
        self.unit.setCurrentIndex(1 if s.get("unit", "h") == "h" else 0)
        i = self.rx.findData(s.get("rx"))
        self.rx.setCurrentIndex(max(0, i))
        self.analyze.setChecked(bool(s.get("analyze", True)))
        self.useskip.setChecked(bool(s.get("useskip", True)))
        self.skipmin.setValue(float(s.get("skipmin") or 0))
        self.thresh.setValue(float(s.get("thresh") if s.get("thresh") is not None else 10))
        self.floorwin.setValue(float(s.get("floorwin") or 0))
        for w in ws:
            w.blockSignals(False)
        self.unit_reset()

    def values(self) -> dict:
        v = self.dur.value()
        return {"tag": self.tag.text().strip(), "durVal": int(v) if float(v).is_integer() else v,
                "unit": "h" if self.unit.currentIndex() == 1 else "m", "rx": self.rx.currentData(),
                "analyze": self.analyze.isChecked(), "useskip": self.useskip.isChecked(),
                "skipmin": self.skipmin.value(), "thresh": self.thresh.value(),
                "floorwin": self.floorwin.value() or None}


CELL_KEYS = {"Hops": "cell_hops", "Sample rate": "cell_sample_rate", "FFT size": "cell_fft", "FFT bins": "cell_fft",
             "Actual bin": "cell_bin", "Bin width": "cell_bin", "Bins logged": "cell_bins", "Sweeps": "cell_sweeps",
             "Passes": "cell_passes", "Listening": "cell_listening", "Listen per frequency": "cell_per_freq",
             "Occupancy step": "cell_occupancy", "File size": "cell_file", "Finishes": "cell_finishes",
             "One pass": "cell_one_pass", "Spectra per hop": "cell_spectra"}

# What a change of setting is called, and which figures to report when they move (relay 011).
SETTING_WORDS = {"lo": ("start", " MHz"), "hiReq": ("stop", " MHz"), "bin": ("bin", " kHz"), "gain": ("gain", " dB"),
                 "integ": ("integration", " s"), "crop": ("crop", ""), "fir": ("FIR", ""), "engine": ("engine", ""),
                 "fbins": ("FFT bins", ""), "ftime": ("seconds per hop", " s"), "frate": ("sample rate", " S/s"),
                 "fover": ("overlap", " %"), "durVal": ("duration", ""), "unit": ("duration unit", ""),
                 "skipmin": ("warm-up", " min"), "useskip": ("discard warm-up", ""), "analyze": ("analysis", ""),
                 "rx": ("receiver", "")}
FIGURES = (("hops", "hops", "{:,.0f}"), ("sweeps", "sweeps", "{:,.0f}"), ("fft", "FFT size", "{:,.0f}"),
           ("bin_khz_predicted", "bin", "{:.4g} kHz"), ("bins", "bins per sweep", "{:,.0f}"),
           ("listening_frac", "listening", "{:.0%}"), ("listen_per_freq_s", "listen per frequency", "{:.1f} s"),
           ("csv_mb", "file", "{:.1f} MB"))


def changes_between(old: dict, new: dict) -> str:
    """"sweeps 120 → 60, listening 83% → 91%: integration 30 s → 60 s" from two plans."""
    if not old or not new:
        return ""
    os_, ns = old.get("settings") or {}, new.get("settings") or {}
    why = []
    fftw = ns.get("engine") == "rtl_power_fftw"
    skip = ("bin", "integ", "fir") if fftw else ("fbins", "ftime", "frate", "fover")
    for k, (word, unit) in SETTING_WORDS.items():
        if k in skip and os_.get("engine") == ns.get("engine"):
            continue                              # a setting the chosen engine does not use
        a, b = os_.get(k), ns.get(k)
        if a != b and not (a in (None, "") and b in (None, "")):
            if isinstance(a, bool) or isinstance(b, bool):
                why.append(f"{word} {'on' if b else 'off'}")
            else:
                why.append(f"{word} {a if a is not None else '—'}{unit if a is not None else ''} → "
                           f"{b if b is not None else '—'}{unit if b is not None else ''}")
    og, ng = old.get("geometry") or {}, new.get("geometry") or {}
    moved = []
    for k, word, fmt in FIGURES:
        a, b = og.get(k), ng.get(k)
        if a is None or b is None:
            continue
        try:
            sa, sb = fmt.format(a), fmt.format(b)
        except (ValueError, TypeError):
            continue
        if sa != sb:
            moved.append(f"{word} {sa} → {sb}")
    ot, nt = old.get("timeline") or {}, new.get("timeline") or {}
    if ot.get("analysed_s") != nt.get("analysed_s") and nt:
        moved.append(f"analysed {round(ot.get('analysed_s', 0) / 60, 1):g} → {round(nt.get('analysed_s', 0) / 60, 1):g} min")
    if old.get("fingerprint") != new.get("fingerprint"):
        moved.append("fingerprint changed")
    if not why and not moved:
        return ""
    if not moved:
        return "Changed " + ", ".join(why) + ": no figure moved."
    return ", ".join(moved) + (": because " + ", ".join(why) if why else "") + "."


class Timeline(QWidget):
    """The run's real timeline, to scale: warm-up left out of the analysis, then the part analysed.
    From the service's scan_timeline (the engine's own sweeps); nothing computed here."""

    def __init__(self):
        super().__init__()
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(3)
        h = QHBoxLayout()
        h.setSpacing(2)
        h.addWidget(label("Timeline", muted=True))
        h.addWidget(X.info("timeline"))
        h.addStretch(1)
        v.addLayout(h)
        self.bar = QWidget()
        self.bar.setFixedHeight(12)
        self.bar.paintEvent = self._paint
        v.addWidget(self.bar)
        self.text = label("", wrap=True)
        self.text.setTextFormat(Qt.TextFormat.RichText)
        v.addWidget(self.text)
        self.t: dict = {}

    def show_timeline(self, t: dict | None):
        self.t = t or {}
        colour = {"bad": "alert", "info": "muted"}.get(self.t.get("level"), "text2")
        self.text.setText(f"<span style='color:{T.C[colour]}'>{self.t.get('text', '')}</span>")
        self.bar.update()

    def _paint(self, _):
        t = self.t
        if not t:
            return
        p = QPainter(self.bar)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(0, 1, self.bar.width(), self.bar.height() - 2)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(T.color("raised"))
        p.drawRoundedRect(r, 3, 3)
        total = float(t.get("total_s") or 0) or 1.0
        if t.get("analysed"):
            w = r.width() * min(1.0, float(t.get("warm_s") or 0) / total)
            p.setBrush(QBrush(T.color("muted", 0.55), Qt.BrushStyle.BDiagPattern))
            p.drawRect(QRectF(r.left(), r.top(), w, r.height()))
            p.setBrush(T.color("alert" if t.get("level") == "bad" else "survey", 0.75))
            p.drawRect(QRectF(r.left() + w, r.top(), max(0.0, r.width() - w), r.height()))
        else:
            p.setBrush(T.color("muted", 0.4))
            p.drawRoundedRect(r, 3, 3)
        p.end()


class Cells(QFrame):
    """"What the program will really do": the engine's own figures, with the time split."""

    def __init__(self):
        super().__init__()
        self.setObjectName("TextPanel")
        v = QVBoxLayout(self)
        v.setContentsMargins(T.SPACE * 1.5, T.SPACE * 1.25, T.SPACE * 1.5, T.SPACE * 1.5)
        h = QHBoxLayout()
        self.title = label("What rtl_power will do", size=10, bold=True)
        h.addWidget(self.title)
        h.addWidget(badge("From the engine", "muted"))
        self.tl = Timeline()
        h.addStretch(1)
        v.addLayout(h)
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(T.SPACE * 2)
        self.grid.setVerticalSpacing(4)
        v.addLayout(self.grid)
        self.split = label("", muted=True, wrap=True)
        v.addWidget(self.split)
        self.review = label("", wrap=True)
        self.review.setStyleSheet(f"color: {T.C['anomaly']};")
        v.addWidget(self.review)
        v.addWidget(self.tl)
        ch = QHBoxLayout()
        ch.setSpacing(2)
        self.changed_line = label("", wrap=True)
        self.changed_line.setTextFormat(Qt.TextFormat.RichText)
        ch.addWidget(self.changed_line, 1)
        ch.addWidget(X.info("what_changed"))
        v.addLayout(ch)

    def show_plan(self, plan: dict):
        clear_layout(self.grid)
        self.title.setText("What rtl_power_fftw will do" if plan.get("engine") == "rtl_power_fftw"
                           else "What rtl_power will do")
        review = plan.get("listening_under_review")
        for i, (k, val, unit, key) in enumerate(plan.get("cells") or []):
            r, c = divmod(i, 4)
            name = QLabel(k + (" (under review)" if review and k == "Listening" else ""))
            name.setProperty("muted", True)
            name.setFont(T.ui_font(8))
            if CELL_KEYS.get(k):
                X.attach(name, CELL_KEYS[k])
            vv = QLabel(f"<b>{val}</b> <span style='color:{T.C['muted']}'>{unit}</span>")
            vv.setTextFormat(Qt.TextFormat.RichText)
            if key:
                vv.setStyleSheet(f"color: {T.C['survey_light']};")
            box = QVBoxLayout()
            box.setSpacing(0)
            w = QWidget()
            w.setLayout(box)
            nh = QHBoxLayout()
            nh.setSpacing(1)
            nh.addWidget(name)
            if CELL_KEYS.get(k):
                nh.addWidget(X.info(CELL_KEYS[k]))
            nh.addStretch(1)
            box.addLayout(nh)
            box.addWidget(vv)
            self.grid.addWidget(w, r, c)
        sp = plan.get("split")
        if sp:
            self.split.setText(f"Of every minute: {60 * sp['kept']:.1f} s hearing kept spectrum, "
                               f"{60 * sp['cropped']:.1f} s hearing hop edges that the crop discards, "
                               f"{60 * sp['retune']:.1f} s retuning.")
        else:
            self.split.setText("A sweep cannot finish inside -i: retuning alone takes longer than the interval.")
        self.review.setText("Listening share is under review: relay 001 measured rtl_power's real share at about "
                            "6 %, whatever -i is. These figures are the engine's dwell model, as `marsad run` "
                            "prints today; correcting it is station work." if review else "")
        self.review.setVisible(bool(review))
        self.tl.show_timeline(plan.get("timeline"))


class Tradeoff(QTableWidget):
    """The integration trade-off table: one row per -i (or -t for rtl_power_fftw). Click to adopt."""
    adopt = Signal(float)

    def __init__(self):
        super().__init__(0, 7)
        self.setHorizontalHeaderLabels(["Setting", "Listening", "Kept · cropped · retune", "Sweeps",
                                        "Listen per frequency", "Occupancy step", "File"])
        self.verticalHeader().hide()
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.setMinimumHeight(200)
        self.values: list = []
        self.cellClicked.connect(lambda r, _c: self.adopt.emit(float(self.values[r])) if r < len(self.values) else None)

    def show_rows(self, rows: list):
        self.setRowCount(len(rows))
        self.values = [r["value"] for r in rows]
        for i, r in enumerate(rows):
            sp = r.get("split")
            cells = [r["label"] + ("" if r["ok"] else " ✕"), f"{100 * r['listening']:.0f}%",
                     " · ".join(f"{100 * sp[k]:.0f}" for k in ("kept", "cropped", "retune")) if sp else "cannot finish",
                     f"{r['sweeps']:,}", r["per_freq"], f"{r['occ_step']:.2f}%", f"{r['mb']:.1f} MB"]
            for c, t in enumerate(cells):
                it = QTableWidgetItem(t)
                if r["now"]:
                    it.setBackground(T.color("survey_sel"))
                if not r["ok"]:
                    it.setForeground(T.color("muted"))
                self.setItem(i, c, it)


# ---------------------------------------------------------------- the scan dialog

class StartScanDialog(QDialog):
    """Start a scan: saved scans first, a full new scan one step away."""
    started = Signal(dict)

    def __init__(self, svc: Service, parent=None):
        super().__init__(parent)
        self.svc = svc
        self.setWindowTitle("Start a scan")
        self.resize(1320, 860)
        self.saved: dict = {}
        self.entry: dict | None = None          # the saved scan or preset picked
        self.scan: dict = {}                    # its full settings, as the service normalised them
        self.plan: dict = {}
        self.checks: dict = {}
        self.rig_checks: list = []
        self.show_archived = False
        self._plan_seq = 0
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(150)
        self.timer.timeout.connect(self._ask_plan)
        self._build()
        QTimer.singleShot(0, self.reload)

    # layout
    def _build(self):
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.split = QSplitter(Qt.Orientation.Horizontal)
        outer.addWidget(self.split)
        main = QWidget()
        mv = QVBoxLayout(main)
        mv.setContentsMargins(T.SPACE * 2, T.SPACE * 1.5, T.SPACE * 2, T.SPACE * 1.5)
        head = QHBoxLayout()
        self.title = label("Start a scan", size=13, bold=True)
        head.addWidget(self.title)
        head.addStretch(1)
        self.b_back = tool("‹ Saved scans", "Back to the saved scans")
        self.b_back.clicked.connect(lambda: self.show_page(0))
        self.b_new = tool("New scan…", "The full setup: range, engine, every setting, the trade-off table")
        self.b_new.clicked.connect(self.new_scan)
        self.b_cmd = tool("Show command", "Open the complete command in a side panel")
        self.b_cmd.setCheckable(True)
        self.b_cmd.toggled.connect(self._toggle_cmd)
        for b in (self.b_back, self.b_new, self.b_cmd):
            head.addWidget(b)
        mv.addLayout(head)
        self.pages = QStackedWidget()
        mv.addWidget(self.pages, 1)
        self.pages.addWidget(self._saved_page())
        self.pages.addWidget(self._setup_page())
        foot = QHBoxLayout()
        self.why = label("", muted=True, wrap=True)
        foot.addWidget(self.why, 1)
        self.b_dry = tool("Start (dry run)", "The engine's own dry run of the complete command: it parses for "
                                             "real and prints what it would record, and opens no radio")
        self.b_dry.clicked.connect(lambda: self.start(dry=True))
        self.b_start = tool("Start", "Launch the run outside the window and the service: closing either never "
                                     "stops it", "Primary")
        self.b_start.clicked.connect(lambda: self.start(dry=False))
        foot.addWidget(self.b_dry)
        foot.addWidget(self.b_start)
        mv.addLayout(foot)
        self.split.addWidget(main)
        self.cmd = CommandPanel()
        self.split.addWidget(self.cmd)
        self.cmd.hide()
        self.split.setStretchFactor(0, 4)
        self.split.setStretchFactor(1, 1)
        self.show_page(0)

    def _saved_page(self):
        page = QWidget()
        h = QHBoxLayout(page)
        h.setContentsMargins(0, T.SPACE, 0, 0)
        h.setSpacing(T.SPACE * 1.5)
        left = QFrame()
        left.setObjectName("TextPanel")
        lv = QVBoxLayout(left)
        lv.setContentsMargins(T.SPACE, T.SPACE, T.SPACE, T.SPACE)
        lv.addWidget(label("Saved scans", size=10, bold=True))
        self.list = QListWidget()
        self.list.setMinimumWidth(360)
        self.list.currentItemChanged.connect(self._picked)
        lv.addWidget(self.list, 1)
        row = QHBoxLayout()
        self.b_edit = tool("Edit…", "Change every setting of this saved scan (a locked change starts a new "
                                    "comparison set)")
        self.b_edit.clicked.connect(self.edit_scan)
        self.b_save = tool("Save…", "Save the scan as it is set now (a preset becomes your saved scan)")
        self.b_save.clicked.connect(self.save_as)
        self.b_ren = tool("Rename…")
        self.b_ren.clicked.connect(lambda: self._change("rename"))
        self.b_dup = tool("Duplicate…")
        self.b_dup.clicked.connect(lambda: self._change("duplicate"))
        self.b_arc = tool("Archive", "Hide it from this list. Nothing is deleted: Show archived brings it back")
        self.b_arc.clicked.connect(lambda: self._change("archive"))
        for b in (self.b_edit, self.b_save, self.b_ren, self.b_dup, self.b_arc):
            row.addWidget(b)
        row.addStretch(1)
        lv.addLayout(row)
        self.c_arch = QCheckBox("Show archived")
        self.c_arch.toggled.connect(self._toggle_archived)
        lv.addWidget(self.c_arch)
        h.addWidget(left, 2)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, T.SPACE, 0)
        rv.setSpacing(T.SPACE * 1.25)
        self.pick_head = label("", size=11, bold=True, wrap=True)
        rv.addWidget(self.pick_head)
        self.pick_sub = label("", muted=True, wrap=True)
        rv.addWidget(self.pick_sub)
        self.other_radio = label("", wrap=True)
        self.other_radio.setTextFormat(Qt.TextFormat.RichText)
        rv.addWidget(self.other_radio)
        er = QHBoxLayout()
        self.edited = label("", wrap=True)
        self.edited.setStyleSheet(f"color: {T.C['anomaly']};")
        er.addWidget(self.edited, 1)
        self.b_revert = tool("Revert to saved", "Throw away the changes made on the setup page")
        self.b_revert.clicked.connect(lambda: self._picked(self.list.currentItem()))
        er.addWidget(self.b_revert)
        rv.addLayout(er)
        self.fp_line = QLabel()
        self.fp_line.setFont(T.mono_font(8.5))
        self.fp_line.setWordWrap(True)
        X.attach(self.fp_line, "fingerprint")
        rv.addWidget(self.fp_line)
        self.newset = label("", wrap=True)
        self.newset.setObjectName("Notice")
        self.newset.hide()
        rv.addWidget(self.newset)
        self.free = FreeSettings()
        self.free.changed.connect(self._free_changed)
        rv.addWidget(self.free)
        self.timeline = Timeline()
        rv.addWidget(self.timeline)
        self.cond = ConditionsBox()
        self.cond.changed.connect(self._schedule)
        rv.addWidget(self.cond)
        chain_box, cv, ch = section("Chain for this run")
        self.chain_text = QLineEdit()
        self.chain_text.setFont(T.mono_font(8.5))
        self.chain_text.setPlaceholderText("from the rig used for runs (Chain screen)")
        self.chain_text.setReadOnly(True)
        self.chain_edit = QCheckBox("Change the chain text for this run only (a 50 Ω load, a pad…)")
        self.chain_edit.toggled.connect(self._chain_edit)
        self.chain_text.textEdited.connect(self._schedule)
        ch.addWidget(X.info("chain_text"))
        X.attach(self.chain_text, "chain_text")
        cv.addWidget(self.chain_text)
        cv.addWidget(X.with_info(self.chain_edit, "chain_override"))
        rv.addWidget(chain_box)
        self.checks_panel = ChecksPanel()
        self.checks_panel.again.clicked.connect(lambda: self._check(detect=True))
        self.checks_panel.changed.connect(self._ready)
        rv.addWidget(self.checks_panel)
        rv.addStretch(1)
        scroll.setWidget(right)
        h.addWidget(scroll, 3)
        return page

    def _setup_page(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, T.SPACE, T.SPACE, 0)
        v.setSpacing(T.SPACE * 1.25)
        box, bv, bh = section("Range", "Locks comparability", "anomaly", key="hop_strip")
        self.s_preset = QComboBox()
        X.attach(self.s_preset, "start_from")
        self.s_preset.activated.connect(self._from_preset)
        bh.addWidget(X.titled("Start from", "start_from"))
        bh.addWidget(self.s_preset)
        self.strip = RangeStrip()
        X.attach(self.strip, "hop_strip")
        self.strip.rangeChanged.connect(self._strip_moved)
        bv.addWidget(self.strip)
        g = QGridLayout()
        self.s_lo, self.s_hi = dspin(0, 10000, 3, 1, " MHz"), dspin(0, 10000, 3, 1, " MHz")
        self.s_rx = QComboBox()
        self.s_target = dspin(0, 10000, 3, 0.1, " MHz")
        self.s_target.setSpecialValueText("none (no target)")
        for i, (t, w, key) in enumerate((("Start", self.s_lo, "range_start"), ("Stop", self.s_hi, "range_stop"),
                                         ("Receiver", self.s_rx, "receiver"),
                                         ("Target frequency (optional)", self.s_target, "target"))):
            g.addWidget(X.titled(t, key), 0, i)
            g.addWidget(X.attach(w, key), 1, i)
        bv.addLayout(g)
        self.facts = label("", wrap=True)
        self.facts.setTextFormat(Qt.TextFormat.RichText)
        self.facts.setToolTip("What is known in this span: the receiver's range and station.json's known_mhz")
        bv.addWidget(self.facts)
        v.addWidget(box)

        cols = QHBoxLayout()
        cols.setSpacing(T.SPACE * 1.25)
        lock, lv, _ = section("Recording settings", "Locks comparability", "anomaly", key="fingerprint")
        lv.addWidget(label("Change any of these and this run can only be compared with runs that share the same "
                           "values.", muted=True, wrap=True))
        lg = QGridLayout()
        self.s_engine = QComboBox()
        self.s_engine.addItems(["rtl_power", "rtl_power_fftw"])
        self.s_gain = QComboBox()
        self.s_bin = dspin(0.1, 10000, 3, 1, " kHz")
        self.s_integ = QSpinBox()
        self.s_integ.setRange(1, 100000)
        self.s_integ.setSuffix(" s")
        self.s_integ.setKeyboardTracking(False)
        self.s_fir = QSpinBox()
        self.s_fir.setRange(0, 9)
        self.s_crop = dspin(0, 0.45, 2, 0.05)
        self.s_fbins = QComboBox()
        self.s_fbins.addItems([str(b) for b in FFT_BINS])
        self.s_fbins.setEditable(True)
        self.s_ftime = dspin(0.01, 100000, 3, 0.5, " s")
        self.s_frate = dspin(0.25, 3.2, 6, 0.1, " MS/s")
        self.s_fover = dspin(0, 90, 1, 1, " %")
        self.rp_widgets = [("Bin", self.s_bin), ("Integration", self.s_integ), ("FIR", self.s_fir)]
        self.fw_widgets = [("FFT bins", self.s_fbins), ("Seconds per hop", self.s_ftime), ("Sample rate", self.s_frate),
                           ("Overlap", self.s_fover)]
        self.lock_labels = {}
        items = [("Engine", self.s_engine), ("Gain dB", self.s_gain)] + self.rp_widgets + self.fw_widgets + \
            [("Crop", self.s_crop)]
        keys = {"Engine": "engine", "Gain dB": "gain", "Bin": "bin", "Integration": "integration", "FIR": "fir",
                "FFT bins": "fft_bins", "Seconds per hop": "seconds_per_hop", "Sample rate": "sample_rate",
                "Overlap": "overlap", "Crop": "crop"}
        for w in (self.s_lo, self.s_hi, self.s_target, self.s_bin, self.s_crop, self.s_ftime, self.s_frate,
                  self.s_fover, self.s_integ, self.s_fir):
            w.setKeyboardTracking(True)            # every figure follows as you type (relay 011)
        for i, (t, w) in enumerate(items):
            r, c = divmod(i, 3)
            X.attach(w, keys[t])
            lab = X.titled(t, keys[t])
            self.lock_labels[id(w)] = lab
            lg.addWidget(lab, r * 2, c)
            lg.addWidget(w, r * 2 + 1, c)
        lv.addLayout(lg)
        self.s_fp = QLabel()
        self.s_fp.setFont(T.mono_font(8.5))
        self.s_fp.setWordWrap(True)
        lv.addWidget(self.s_fp)
        self.s_newset = label("", wrap=True)
        X.attach(self.s_newset, "new_set")
        X.attach(self.s_fp, "fingerprint")
        self.s_newset.setObjectName("Notice")
        self.s_newset.hide()
        lv.addWidget(self.s_newset)
        self.engine_note = label("", muted=True, wrap=True)
        self.engine_note.setFont(T.ui_font(8))
        lv.addWidget(self.engine_note)
        cols.addWidget(lock, 1)
        self.s_free = FreeSettings("Run settings", show_radio=False)
        self.s_free.changed.connect(self._setup_changed)
        cols.addWidget(self.s_free, 1)
        v.addLayout(cols)
        self.cells = Cells()
        v.addWidget(self.cells)
        wbox, self.wv, _ = section("Warnings", key="warnings")
        self.w_rows = QVBoxLayout()
        self.wv.addLayout(self.w_rows)
        v.addWidget(wbox)
        tbox, tv, _ = section("Integration trade-off", "Locks comparability", "anomaly", key="tradeoff")
        self.table = Tradeoff()
        self.table.adopt.connect(self._adopt)
        tv.addWidget(self.table)
        tv.addWidget(label("Retuning costs the same per hop whatever the integration, so a longer -i buys dwell by "
                           "spending sweeps. Click a row to adopt it.", muted=True, wrap=True))
        v.addWidget(tbox)
        row = QHBoxLayout()
        row.addStretch(1)
        self.b_saveas = tool("Save as a saved scan…", "Keep these settings in your saved scans")
        self.b_saveas.clicked.connect(self.save_as)
        self.b_use = tool("Continue to start ›", "Back to the start panel with these settings: checks, conditions, "
                                                 "Start", "Primary")
        self.b_use.clicked.connect(lambda: self.show_page(0))
        row.addWidget(self.b_saveas)
        row.addWidget(self.b_use)
        v.addLayout(row)
        v.addStretch(1)
        for w in (self.s_lo, self.s_hi, self.s_target, self.s_bin, self.s_crop, self.s_ftime, self.s_frate,
                  self.s_fover):
            w.valueChanged.connect(self._setup_changed)
        for w in (self.s_integ, self.s_fir):
            w.valueChanged.connect(self._setup_changed)
        for w in (self.s_engine, self.s_gain, self.s_rx):
            w.currentIndexChanged.connect(self._setup_changed)
        self.s_fbins.currentTextChanged.connect(self._setup_changed)
        scroll.setWidget(page)
        return scroll

    # data
    def reload(self, keep: str | None = None):
        def got(d, err):
            if err:
                self.why.setText(f"Could not read the saved scans: {err}")
                return
            self.saved = d
            rx = d.get("receivers") or {}
            self.free.set_receivers(rx)
            self._ask_radios()
            self.s_rx.blockSignals(True)
            self.s_rx.clear()
            for name, r in rx.items():
                self.s_rx.addItem(f"{_word(name)}  ({r.get('label', name)})", name)
            self.s_rx.blockSignals(False)
            self.s_preset.clear()
            for p in d.get("presets") or []:
                self.s_preset.addItem(f"{p['name']}  ({p['range']}, {p['engine']})", p["name"])
            self.cond.set_values(d.get("conditions_last") or {})
            self.cond.set_last(d.get("conditions_last") or {})
            self._fill_list(keep)
        call(self.svc.saved, got)
        call(lambda: self.svc.chains(), self._got_chains)

    def _got_chains(self, d, err):
        self.rig_checks = rig_checks_for_runs(d) if not err else []

    def _fill_list(self, keep=None):
        self.list.blockSignals(True)
        self.list.clear()
        want = keep or (self.entry or {}).get("name")
        pick = None

        def head(text):
            it = QListWidgetItem(text)
            it.setFlags(Qt.ItemFlag.NoItemFlags)
            it.setForeground(T.color("muted"))
            f = T.ui_font(8.5, QFont.Weight.DemiBold)
            it.setFont(f)
            self.list.addItem(it)

        def add(e):
            nonlocal pick
            last = (e.get("last_ran") or "").replace("T", " ")[:16]
            tag = "  · archived" if e.get("archived") else ""
            it = QListWidgetItem(f"{e['name']}{tag}\n{e['range']} · {e['engine']} · {_word(e['rx'])} · "
                                 f"{e['duration']}\n" + (f"last ran {last}" if last else "never run with these settings"))
            it.setData(Qt.ItemDataRole.UserRole, e)
            it.setToolTip(e.get("fingerprint") or "")
            if e.get("archived"):
                it.setForeground(T.color("muted"))
            self.list.addItem(it)
            if want and e["name"] == want and (pick is None):
                pick = it
        scans = [e for e in self.saved.get("scans") or [] if self.show_archived or not e.get("archived")]
        head("YOUR SAVED SCANS" if scans else "YOUR SAVED SCANS: none yet (start from a preset, then Save…)")
        for e in scans:
            add(e)
        head("STATION PRESETS (station.json)")
        for e in self.saved.get("presets") or []:
            add(e)
        self.list.blockSignals(False)
        if pick is None:
            for i in range(self.list.count()):
                if self.list.item(i).data(Qt.ItemDataRole.UserRole):
                    pick = self.list.item(i)
                    break
        if pick is not None:
            self.list.setCurrentItem(pick)

    def _locked_changed(self) -> bool:
        """Settings changed on the setup page that are not the free ones (so not saved yet)."""
        if not self.entry:
            return False
        base = self.entry.get("settings") or {}
        skip = set(FREE_KEYS) | set(COND_KEYS) | {"device", "dryfirst", "nonint"}
        return any(self.scan.get(k) != base.get(k) for k in set(base) | set(self.scan) if k not in skip)

    def _show_edited(self):
        on = self._locked_changed() and self.entry.get("kind") != "new"
        self.edited.setText("Changed on the setup page and not saved: Start uses these settings; Save… keeps them."
                            if on else "")
        self.edited.setVisible(on)
        self.b_revert.setVisible(on)

    def _picked(self, cur, _prev=None):
        e = cur.data(Qt.ItemDataRole.UserRole) if cur else None
        if not e:
            return
        self.entry = e
        self.scan = copy.deepcopy(e["settings"])
        self.free.set_values(self.scan)
        self.pick_head.setText(e["name"] + ("  (station preset)" if e["kind"] == "preset" else ""))
        self.pick_sub.setText(f"{e['range']} · {e['engine']} · {_word(e['rx'])} · {e['duration']}"
                              + (f" · {e.get('note')}" if e.get("note") else ""))
        for b in (self.b_ren, self.b_dup):
            b.setEnabled(e["kind"] == "saved")
        self.b_arc.setEnabled(e["kind"] == "saved")
        self.b_arc.setText("Restore" if e.get("archived") else "Archive")
        self._load_setup()
        self._show_edited()
        self._show_other_radio()
        self._check(detect=False)
        self._schedule()

    def _toggle_archived(self, on):
        self.show_archived = on
        self._fill_list()

    def _ask_radios(self):
        call(self.svc.radios, lambda d, err: self._got_radios(d) if not err else None)

    def _got_radios(self, d: dict):
        """A scan uses one radio. Say which radios are plugged in, and what the other one could do."""
        from marsad_desktop.radios import word
        self.radios_d = d
        here = {r["name"]: r for r in d.get("radios") or []}
        cb = self.free.rx
        for i in range(cb.count()):
            r = here.get(cb.itemData(i)) or {}
            mark_radio_item(cb, i, r, f"{word(cb.itemData(i))}  ({r.get('label', cb.itemData(i))})"
                            + {True: "", False: " · not plugged in", None: ""}[r.get("present")])
        self._show_other_radio()

    def _show_other_radio(self):
        from marsad_desktop.radios import word
        d = getattr(self, "radios_d", None) or {}
        rx = self.scan.get("rx")
        me = next((r for r in d.get("radios") or [] if r["name"] == rx), None)
        others = [r for r in d.get("radios") or [] if r["name"] != rx]
        bits = []
        if me and me.get("enabled") is False:
            bits.append(f"<span style='color:{T.C['alert']}'>The {word(rx)} is disabled (the switch on its card on "
                        "Live Scan): this scan cannot start on it. Pick another radio or switch it back on.</span>")
        elif me and me.get("present") is False:
            bits.append(f"<span style='color:{T.C['alert']}'>The {word(rx)} is not plugged in: this scan cannot "
                        "start until it is.</span>")
        bits.append(f"This scan uses the {word(rx)} only; it surveys and nothing records IQ.")
        for o in others:
            if o.get("present"):
                bits.append(f"The {word(o['name'])} is {o['doing']['text']}"
                            + (": to record what this scan finds, use Start recording (survey + record) instead."
                               if o["doing"]["state"] == "ready" else "."))
            elif o.get("present") is False:
                bits.append(f"The {word(o['name'])} is not plugged in.")
        self.other_radio.setText(f"<span style='color:{T.C['text2']}'>" + " ".join(bits) + "</span>")

    def _free_changed(self):
        rx_before = self.scan.get("rx")
        self.scan.update(self.free.values())
        if self.scan.get("rx") != rx_before:
            self._check(detect=False)
            self._show_other_radio()
        self._schedule()

    def _chain_edit(self, on):
        self.chain_text.setReadOnly(not on)
        if not on:
            self.chain_text.setText((self.checks.get("chain") or {}).get("text") or "")
        self._schedule()

    def _current(self) -> dict:
        s = dict(self.scan)
        s.update(self.cond.values())
        return s

    def _chain(self) -> str:
        return self.chain_text.text().strip()

    def _schedule(self):
        self.timer.start()

    def _ask_plan(self):
        if not self.entry:
            return
        self._plan_seq += 1
        seq = self._plan_seq
        body = {"scan": self._current(), "chain": self._chain(), "base": self.entry["settings"]}

        def got(d, err):
            if seq != self._plan_seq:
                return
            if err:
                self.why.setText(f"The service could not plan this scan: {err}")
                return
            self.changes = changes_between(self.plan, d) or getattr(self, "changes", "")
            self.plan = d
            self._show_plan()
        call(lambda: self.svc.plan(body), got)

    def _show_plan(self):
        d = self.plan
        fp = d.get("fingerprint") or ""
        self.fp_line.setText(f"<span style='color:{T.C['muted']}'>fingerprint</span>&nbsp; {fp}")
        self.s_fp.setText(f"<span style='color:{T.C['muted']}'>fingerprint</span>&nbsp; {fp}")
        for w in (self.newset, self.s_newset):
            w.setText(NEW_SET_TEXT + (f"  (was {d.get('base_fingerprint')})" if d.get("new_set") else ""))
            w.setVisible(bool(d.get("new_set")))
        self.cmd.show_text(d.get("command") or "")
        self.cells.show_plan(d)
        self.timeline.show_timeline(d.get("timeline"))
        ch = getattr(self, "changes", "")
        self.cells.changed_line.setText(f"<span style='color:{T.C['muted']}'>Last change:</span> "
                                        f"<span style='color:{T.C['text2']}'>{ch}</span>" if ch else "")
        X.use_radio(self.svc, (d.get("settings") or {}).get("rx"))
        self.table.show_rows(d.get("tradeoff") or [])
        clear_layout(self.w_rows)
        for lvl, title, text in d.get("warnings") or []:
            w = QLabel(f"<span style='color:{T.C[LEVEL_COLOUR[lvl]]}; font-weight:600'>{title}</span>&nbsp; "
                       f"<span style='color:{T.C['text2']}'>{text}</span>")
            w.setWordWrap(True)
            w.setTextFormat(Qt.TextFormat.RichText)
            self.w_rows.addWidget(w)
        if not d.get("warnings"):
            self.w_rows.addWidget(label("No warnings for these settings.", muted=True))
        kinds = {"spur": "anomaly", "bad": "alert", "": "text2"}
        self.facts.setText(" &nbsp;·&nbsp; ".join(
            f"<b style='color:{T.C[kinds.get(k, 'text2')]}'>{a}</b> <span style='color:{T.C['muted']}'>{t}</span>"
            for k, a, t in d.get("facts") or []))
        st = d.get("settings") or {}
        self.strip.set_plan(d.get("geometry") or {}, float(st.get("crop") or 0.25), d.get("engine"), st.get("target"))
        self.cond.line.setText("--conditions " + (f'"{d.get("conditions")}"' if d.get("conditions") else "(nothing)"))
        self._show_checks()

    # the checks
    def _check(self, detect: bool):
        rx = self.scan.get("rx")
        if not rx:
            return
        pending = "asking each radio its serial (rtl_eeprom)…" if detect else None
        self.checks_panel.show_checks({rx: {"radio": {"found": None, "text": "…"}, "chain": {}}}, None, [], [],
                                      pending=pending)

        def got(d, err):
            if err:
                self.why.setText(f"Could not check: {err}")
                return
            if self.scan.get("rx") != rx:
                return
            self.checks = d
            if not self.chain_edit.isChecked():
                self.chain_text.setText((d.get("chain") or {}).get("text") or "")
                self.chain_text.setCursorPosition(0)
                self.chain_text.setToolTip(self.chain_text.text())
            self._show_checks()
            self._schedule()
        call(lambda: self.svc.check(rx, detect=detect), got)

    def _show_checks(self):
        if not self.checks:
            return
        rx = self.scan.get("rx")
        radio = self.checks.get("radio") or {}
        if radio.get("found") is None and not radio.get("text"):
            radio = {"found": None, "text": "Not checked yet: Check again asks each radio its serial."}
        chain = dict(self.checks.get("chain") or {})
        if self.chain_edit.isChecked():
            chain.update(due=False, source="the text typed for this run", text=self._chain(), problem=None)
        warn = [w for w in self.plan.get("warnings") or [] if w[0] != "ok"]
        if not self.plan.get("conditions"):
            warn.append(NO_CONDITIONS)
        self.checks_panel.show_checks({rx: {"radio": radio, "chain": chain}}, self.checks.get("in_use"), warn,
                                      self.rig_checks)

    def _ready(self):
        ok, why = self.checks_panel.ready()
        radio_unchecked = (self.checks.get("radio") or {}).get("found") is None
        self.b_start.setEnabled(ok and bool(self.entry))
        self.b_start.setToolTip(why or ("Checks every radio by serial, then launches the run outside the window and "
                                        "the service" if radio_unchecked else "Launch the run"))
        self.why.setText(why)

    # the setup page
    def show_page(self, i: int):
        self.pages.setCurrentIndex(i)
        self.b_back.setVisible(i == 1)
        self.b_new.setVisible(i == 0)
        self.title.setText("Start a scan" if i == 0 else "Set up a scan")
        if i == 0 and self.entry:
            self.free.set_values(self.scan)
            self._show_edited()
            self._check(detect=False)
            self._schedule()

    def new_scan(self):
        name = self.s_preset.currentData() or ((self.saved.get("presets") or [{}])[0].get("name"))
        e = next((p for p in self.saved.get("presets") or [] if p["name"] == name), None)
        if e:
            self.entry = {**e, "name": "New scan (not saved)", "kind": "new"}
            self.scan = copy.deepcopy(e["settings"])
            self.pick_head.setText("New scan (not saved)")
            self.pick_sub.setText(f"from the {name} preset")
        self._load_setup()
        self.show_page(1)
        self._schedule()

    def edit_scan(self):
        if self.entry:
            self._load_setup()
            self.show_page(1)

    def _from_preset(self, _i):
        name = self.s_preset.currentData()
        e = next((p for p in self.saved.get("presets") or [] if p["name"] == name), None)
        if not e:
            return
        keep = {k: self.scan.get(k) for k in FREE_KEYS + COND_KEYS if k in self.scan}
        self.scan = {**copy.deepcopy(e["settings"]), **{k: v for k, v in keep.items() if k != "rx"}}
        self._load_setup()
        self._schedule()

    def _load_setup(self):
        s = self.scan
        rx = (self.saved.get("receivers") or {}).get(s.get("rx")) or {}
        ws = (self.s_lo, self.s_hi, self.s_target, self.s_bin, self.s_crop, self.s_ftime, self.s_frate,
              self.s_fover, self.s_integ, self.s_fir, self.s_engine, self.s_gain, self.s_rx, self.s_fbins)
        for w in ws:
            w.blockSignals(True)
        self.s_preset.setCurrentIndex(max(0, self.s_preset.findData(s.get("preset"))))
        self.s_lo.setValue(float(s.get("lo") or 0))
        self.s_hi.setValue(float(s.get("hiReq") or 0))
        self.s_target.setValue(float(s.get("target") or 0))
        self.s_rx.setCurrentIndex(max(0, self.s_rx.findData(s.get("rx"))))
        self.s_engine.setCurrentText(s.get("engine") or "rtl_power")
        self.s_gain.clear()
        gains = rx.get("gains") or [s.get("gain")]
        for g in gains:
            self.s_gain.addItem(jsnum(g), g)
        near = min(gains, key=lambda g: abs(float(g) - float(s.get("gain") or 0)))
        self.s_gain.setCurrentIndex(self.s_gain.findData(near))
        self.s_bin.setValue(float(s.get("bin") or 1))
        self.s_integ.setValue(int(s.get("integ") or 1))
        self.s_fir.setValue(int(s.get("fir") if s.get("fir") is not None else 9))
        self.s_crop.setValue(float(s.get("crop") if s.get("crop") is not None else 0.25))
        self.s_fbins.setCurrentText(str(s.get("fbins") or 128))
        self.s_ftime.setValue(float(s.get("ftime") or 1))
        self.s_frate.setValue(float(s.get("frate") or 2400000) / 1e6)
        fover = s.get("fover")
        self.s_fover.setValue(float(fover if fover is not None else 100 * float(s.get("crop") or 0.25)))
        for w in ws:
            w.blockSignals(False)
        self.s_free.set_values(s)
        self.strip.set_receiver(rx.get("range") or (24, 1766), rx.get("gaps") or [])
        self.strip.set_range(float(s.get("lo") or 0), float(s.get("hiReq") or 0))
        self._engine_rows()

    def _engine_rows(self):
        fftw = self.s_engine.currentText() == "rtl_power_fftw"
        for _, w in self.rp_widgets:
            w.setVisible(not fftw)
            self.lock_labels[id(w)].setVisible(not fftw)
        for _, w in self.fw_widgets:
            w.setVisible(fftw)
            self.lock_labels[id(w)].setVisible(fftw)
        self.engine_note.setText(
            "rtl_power_fftw has no crop of its own: the recorder asks for an overlap of 100 × crop between hops and "
            "the analyser drops that share from each hop's edges. Its dB are its own scale, about 26 dB below "
            "rtl_power's 50 kHz scale at 2.4 MS/s: a run from one engine is never comparable with the other."
            if fftw else "Crop and FIR are 0.25 and 9 by station convention; changing either orphans this run.")

    def _setup_changed(self, *_):
        s = self.scan
        if s.get("rx") != self.s_rx.currentData():
            s["rx"] = self.s_rx.currentData()
            rx = (self.saved.get("receivers") or {}).get(s["rx"]) or {}
            self.strip.set_receiver(rx.get("range") or (24, 1766), rx.get("gaps") or [])
            gains = rx.get("gains") or []
            want = float(self.s_gain.currentData() or 0)
            self.s_gain.blockSignals(True)
            self.s_gain.clear()
            for g in gains:
                self.s_gain.addItem(jsnum(g), g)
            if gains:
                self.s_gain.setCurrentIndex(self.s_gain.findData(min(gains, key=lambda g: abs(g - want))))
            self.s_gain.blockSignals(False)
            self.free.set_values(s)
        try:
            fbins = int(float(self.s_fbins.currentText()))
        except ValueError:
            fbins = s.get("fbins") or 128
        s.update(lo=self.s_lo.value(), hiReq=self.s_hi.value(), target=self.s_target.value() or None,
                 engine=self.s_engine.currentText(), gain=self.s_gain.currentData(), bin=self.s_bin.value(),
                 integ=self.s_integ.value(), fir=self.s_fir.value(), crop=self.s_crop.value(), fbins=fbins,
                 ftime=self.s_ftime.value(), frate=int(round(self.s_frate.value() * 1e6)),
                 fover=self.s_fover.value())
        s.update({k: v for k, v in self.s_free.values().items() if k != "rx"})
        self.strip.set_range(s["lo"], s["hiReq"])
        self._engine_rows()
        self._schedule()

    def _strip_moved(self, lo, hi):
        self.s_lo.setValue(lo)
        self.s_hi.setValue(hi)

    def _adopt(self, value):
        if self.s_engine.currentText() == "rtl_power_fftw":
            self.s_ftime.setValue(value)
        else:
            self.s_integ.setValue(int(value))

    def _toggle_cmd(self, on):
        self.cmd.setVisible(on)
        self.b_cmd.setText("Hide command" if on else "Show command")

    # saving
    def save_as(self):
        if not self.entry:
            return
        start = self.entry["name"] if self.entry.get("kind") == "saved" else (self.scan.get("tag") or "")
        name, ok = QInputDialog.getText(self, "Save as a saved scan", "Name:", text=start)
        if not ok or not name.strip():
            return
        body = {"kind": "scan", "action": "save", "name": name.strip(), "settings": self.scan}

        def got(d, err):
            if err:
                QMessageBox.information(self, "Not saved", err)
                return
            self.saved = d
            self._fill_list(name.strip())
            self.show_page(0)
        call(lambda: self.svc.saved_change(body), got)

    def _change(self, action):
        e = self.entry
        if not e or e.get("kind") != "saved":
            return
        if e.get("archived") and action == "archive":
            action = "restore"
        body = {"kind": "scan", "action": action, "name": e["name"]}
        if action in ("rename", "duplicate"):
            new, ok = QInputDialog.getText(self, action.capitalize(), "New name:",
                                           text=e["name"] + (" (copy)" if action == "duplicate" else ""))
            if not ok or not new.strip():
                return
            body["new_name"] = new.strip()
        elif action == "archive":
            if QMessageBox.question(self, "Archive saved scan", f"Archive “{e['name']}”? It leaves this list; nothing "
                                    "is deleted, and Show archived brings it back.") != QMessageBox.StandardButton.Yes:
                return

        def got(d, err):
            if err:
                QMessageBox.information(self, "Not changed", err)
                return
            self.saved = d
            self._fill_list(body.get("new_name") or e["name"])
        call(lambda: self.svc.saved_change(body), got)

    # starting
    def start(self, dry: bool):
        if not self.entry:
            return
        body = {"scan": self._current(), "dry_run": dry, "detect": True,
                "chain_confirmed": self.checks_panel.confirm.isChecked(),
                "problems_confirmed": self.checks_panel.problems.isChecked()}
        if self.chain_edit.isChecked():
            body["chain"] = self._chain()
        self.b_start.setEnabled(False)
        self.b_dry.setEnabled(False)
        self.why.setText("Dry run: the engine is parsing the command…" if dry else
                         "Checking each radio by serial, then launching…")

        def got(d, err):
            self.b_dry.setEnabled(True)
            self._ready()
            if err:
                self.why.setText(err)
                QMessageBox.information(self, "Not started" if not dry else "Dry run refused", err)
                return
            if dry:
                self.cmd.show_text(d.get("line", "") + " --dry-run\n\n" + (d.get("output") or ""))
                if not self.b_cmd.isChecked():
                    self.b_cmd.setChecked(True)
                self.why.setText(f"Dry run done (exit {d.get('exit_code')}): nothing was recorded. "
                                 "The engine's output is in the command panel.")
                if d.get("checks"):
                    self.checks = d["checks"]
                    self._show_checks()
                return
            self.started.emit(d)
            self.accept()
        call(lambda: self.svc.start(body), got)


# ---------------------------------------------------------------- the recorder dialog

class StartWatchDialog(QDialog):
    """Start a recorder session (`marsad watch`): survey mode, one radio surveys and the other
    records; fixed mode, one radio parked on a frequency. Each radio's chain from its own path.
    The private-band table and the storage caps come from the engine and have no field here."""
    started = Signal(dict)

    def __init__(self, svc: Service, parent=None):
        super().__init__(parent)
        self.svc = svc
        self.setWindowTitle("Start recorder")
        self.resize(1180, 820)
        self.saved: dict = {}
        self.plan: dict = {}
        self.rig_checks: list = []
        self._seq = 0
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self._ask)
        self._build()
        QTimer.singleShot(0, self.reload)

    def _build(self):
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        split = QSplitter(Qt.Orientation.Horizontal)
        outer.addWidget(split)
        main = QWidget()
        mv = QVBoxLayout(main)
        mv.setContentsMargins(T.SPACE * 2, T.SPACE * 1.5, T.SPACE * 2, T.SPACE * 1.5)
        head = QHBoxLayout()
        head.addWidget(label("Start recorder", size=13, bold=True))
        head.addStretch(1)
        self.b_cmd = tool("Show command", "Open the complete command in a side panel")
        self.b_cmd.setCheckable(True)
        self.b_cmd.toggled.connect(lambda on: (self.cmd.setVisible(on),
                                               self.b_cmd.setText("Hide command" if on else "Show command")))
        head.addWidget(self.b_cmd)
        mv.addLayout(head)
        body = QHBoxLayout()
        left = QFrame()
        left.setObjectName("TextPanel")
        lv = QVBoxLayout(left)
        lv.addWidget(label("Saved sessions", size=10, bold=True))
        self.list = QListWidget()
        self.list.setMinimumWidth(260)
        self.list.currentItemChanged.connect(self._picked)
        lv.addWidget(self.list, 1)
        row = QHBoxLayout()
        self.b_save = tool("Save…")
        self.b_save.clicked.connect(self.save_as)
        self.b_ren = tool("Rename…")
        self.b_ren.clicked.connect(lambda: self._change("rename"))
        self.b_dup = tool("Duplicate…")
        self.b_dup.clicked.connect(lambda: self._change("duplicate"))
        self.b_arc = tool("Archive", "Hide it from the list; nothing is deleted")
        self.b_arc.clicked.connect(lambda: self._change("archive"))
        for b in (self.b_save, self.b_ren, self.b_dup, self.b_arc):
            row.addWidget(b)
        lv.addLayout(row)
        self.c_arch = QCheckBox("Show archived")
        self.c_arch.toggled.connect(lambda _on: self._fill())
        lv.addWidget(self.c_arch)
        body.addWidget(left, 2)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, T.SPACE, 0)
        rv.setSpacing(T.SPACE * 1.25)
        modes = QHBoxLayout()
        self.m_survey = QPushButton("Survey + record")
        self.m_fixed = QPushButton("Fixed frequency")
        grp = QButtonGroup(self)
        for b in (self.m_survey, self.m_fixed):
            b.setCheckable(True)
            b.setProperty("accent", True)
            grp.addButton(b)
            modes.addWidget(b)
        X.attach(self.m_survey, "record_mode_survey")
        X.attach(self.m_fixed, "record_mode_fixed")
        modes.addWidget(X.info("record_mode_survey"))
        self.m_survey.setChecked(True)
        self.m_survey.toggled.connect(self._mode)
        modes.addStretch(1)
        rv.addLayout(modes)
        self.mode_note = label("", muted=True, wrap=True)
        rv.addWidget(self.mode_note)

        # survey mode
        self.survey_box, sv, _ = section("Survey (the surveying radio)")
        sg = QGridLayout()
        self.w_scan = QComboBox()
        self.w_surveyor, self.w_recorder = QComboBox(), QComboBox()
        self.w_tag = QLineEdit()
        self.w_dur, self.w_unit = dspin(0.1, 10000, 2, 1), QComboBox()
        self.w_unit.addItems(["min", "hours"])
        sg.addWidget(X.titled("Saved scan or preset", "saved_scan_or_preset"), 0, 0, 1, 2)
        sg.addWidget(X.attach(self.w_scan, "saved_scan_or_preset"), 1, 0, 1, 2)
        sg.addWidget(X.titled("Surveys", "surveyor"), 0, 2)
        sg.addWidget(X.attach(self.w_surveyor, "surveyor"), 1, 2)
        sg.addWidget(X.titled("Records", "recorder_radio"), 0, 3)
        sg.addWidget(X.attach(self.w_recorder, "recorder_radio"), 1, 3)
        self.w_swap = tool("⇄", "Swap which radio surveys and which records")
        X.attach(self.w_swap, "swap")
        self.w_swap.clicked.connect(self._swap)
        sg.addWidget(self.w_swap, 1, 4)
        sg.addWidget(X.titled("Tag", "tag"), 2, 0)
        sg.addWidget(X.attach(self.w_tag, "tag"), 3, 0)
        sg.addWidget(X.titled("Duration", "duration"), 2, 1)
        dd = QHBoxLayout()
        dd.addWidget(X.attach(self.w_dur, "duration"), 1)
        dd.addWidget(X.attach(self.w_unit, "duration_unit"))
        sg.addLayout(dd, 3, 1)
        self.w_unit_reset = link_units(self.w_dur, self.w_unit)
        sv.addLayout(sg)
        from marsad_desktop.radios import CoverageMap
        self.cov_map = CoverageMap()
        X.attach(self.cov_map, "coverage_map")
        sv.addWidget(self.cov_map)
        self.survey_fp = QLabel()
        self.survey_fp.setFont(T.mono_font(8.5))
        self.survey_fp.setWordWrap(True)
        sv.addWidget(self.survey_fp)
        rv.addWidget(self.survey_box)

        # fixed mode
        self.fixed_box, fv, _ = section("Fixed frequency")
        fg = QGridLayout()
        self.f_rx = QComboBox()
        self.f_freq, self.f_width = QLineEdit(), QLineEdit()
        self.f_freq.setPlaceholderText("e.g. 433.92M")
        self.f_width.setPlaceholderText("whole band")
        self.f_gain = dspin(0, 60, 1, 0.1, " dB")
        self.f_gain.setSpecialValueText("recorder.gain_db")
        self.f_dur, self.f_unit = dspin(0.1, 10000, 2, 1), QComboBox()
        self.f_unit.addItems(["min", "hours"])
        self.f_dur.setValue(1)
        self.f_unit.setCurrentIndex(1)
        for i, (t, w, key) in enumerate((("Radio", self.f_rx, "fixed_radio"), ("Frequency", self.f_freq, "fixed_freq"),
                                         ("Width", self.f_width, "fixed_width"), ("Gain", self.f_gain, "fixed_gain"))):
            fg.addWidget(X.titled(t, key), 0, i)
            fg.addWidget(X.attach(w, key), 1, i)
        fg.addWidget(X.titled("Duration", "duration"), 2, 0)
        fd = QHBoxLayout()
        fd.addWidget(X.attach(self.f_dur, "duration"), 1)
        fd.addWidget(X.attach(self.f_unit, "duration_unit"))
        fg.addLayout(fd, 3, 0)
        self.f_unit_reset = link_units(self.f_dur, self.f_unit)
        fv.addLayout(fg)
        rv.addWidget(self.fixed_box)

        # recorder options
        rbox, rov, _ = section("Recorder options", "engine defaults when blank", "muted")
        og = QGridLayout()
        self.o = {}
        spec = [("keep_min_snr", "Keep level", " dB"), ("capture", "Capture", " s"), ("threshold", "Trigger", " dB"),
                ("cooldown", "Cooldown", " min"), ("passes", "Passes", ""), ("ambush", "Ambush", " dB"),
                ("recorder_gain", "Recorder gain", " dB")]
        okeys = {"keep_min_snr": "keep_level", "capture": "capture", "threshold": "trigger", "cooldown": "cooldown",
                 "passes": "passes_trigger", "ambush": "ambush", "recorder_gain": "recorder_gain"}
        for i, (k, t, suf) in enumerate(spec):
            w = dspin(0, 10000, 1, 1, suf)
            w.setSpecialValueText("default")
            self.o[k] = w
            r, c = divmod(i, 4)
            X.attach(w, okeys[k])
            og.addWidget(X.titled(t, okeys[k]), r * 2, c)
            og.addWidget(w, r * 2 + 1, c)
            w.valueChanged.connect(self._schedule)
        rov.addLayout(og)
        rov.addWidget(label("The private-band table and the storage caps come from the engine (station.json's "
                            "recorder block) and cannot be changed here.", muted=True, wrap=True))
        rv.addWidget(rbox)
        self.cond = ConditionsBox()
        self.cond.changed.connect(self._schedule)
        rv.addWidget(self.cond)
        self.checks_panel = ChecksPanel()
        self.checks_panel.again.clicked.connect(lambda: self._ask(detect=True))
        self.checks_panel.changed.connect(self._ready)
        rv.addWidget(self.checks_panel)
        rv.addStretch(1)
        scroll.setWidget(right)
        body.addWidget(scroll, 4)
        mv.addLayout(body, 1)
        foot = QHBoxLayout()
        self.why = label("", muted=True, wrap=True)
        foot.addWidget(self.why, 1)
        self.b_dry = tool("Start (dry run)", "`marsad watch ... --dry-run`: opens no radio")
        self.b_dry.clicked.connect(lambda: self.start(True))
        self.b_start = tool("Start", "Launch the recorder outside the window and the service", "Primary")
        self.b_start.clicked.connect(lambda: self.start(False))
        foot.addWidget(self.b_dry)
        foot.addWidget(self.b_start)
        mv.addLayout(foot)
        split.addWidget(main)
        self.cmd = CommandPanel()
        split.addWidget(self.cmd)
        self.cmd.hide()
        split.setStretchFactor(0, 4)
        for w in (self.w_scan, self.w_surveyor, self.w_recorder, self.w_unit, self.f_rx, self.f_unit):
            w.currentIndexChanged.connect(self._schedule)
        self.w_scan.currentIndexChanged.connect(self._scan_picked)
        for w in (self.w_tag, self.f_freq, self.f_width):
            w.textChanged.connect(self._schedule)
        for w in (self.w_dur, self.f_dur, self.f_gain):
            w.valueChanged.connect(self._schedule)
        self._mode(True)

    def _scan_picked(self, _i=None):
        """The survey's duration and tag start from the saved scan or preset picked."""
        e = self.w_scan.currentData() or {}
        st = e.get("settings") or {}
        for w in (self.w_dur, self.w_unit, self.w_tag):
            w.blockSignals(True)
        self.w_dur.setValue(float(st.get("durVal") or 1))
        self.w_unit.setCurrentIndex(1 if st.get("unit", "h") == "h" else 0)
        self.w_tag.setText(st.get("tag") or "")
        for w in (self.w_dur, self.w_unit, self.w_tag):
            w.blockSignals(False)

    def reload(self, keep=None):
        def got(d, err):
            if err:
                self.why.setText(f"Could not read the saved sessions: {err}")
                return
            self.saved = d
            for cb in (self.w_surveyor, self.w_recorder, self.f_rx):
                cb.blockSignals(True)
                cb.clear()
                for name, r in (d.get("receivers") or {}).items():
                    cb.addItem(_word(name), name)
                    cb.setItemData(cb.count() - 1, r.get("label", name), Qt.ItemDataRole.ToolTipRole)
                cb.setMinimumContentsLength(9)
                cb.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
                cb.blockSignals(False)
            if self.w_recorder.count() > 1:
                self.w_recorder.setCurrentIndex(1)
            self.w_scan.blockSignals(True)
            self.w_scan.clear()
            for e in [x for x in d.get("scans") or [] if not x.get("archived")] + (d.get("presets") or []):
                self.w_scan.addItem(f"{e['name']}  ({e['range']})", e)
            self.w_scan.blockSignals(False)
            if getattr(self, "radios_d", None):
                self._got_radios(self.radios_d)
            self._scan_picked()
            self.cond.set_values(d.get("conditions_last") or {})
            self.cond.set_last(d.get("conditions_last") or {})
            self._fill(keep)
            self._schedule()
        call(self.svc.saved, got)
        call(lambda: self.svc.chains(), lambda d, err: setattr(self, "rig_checks", rig_checks_for_runs(d) if not err else []))
        self._ask_radios()
        if not hasattr(self, "radio_timer"):
            self.radio_timer = QTimer(self)
            self.radio_timer.timeout.connect(self._ask_radios)
            self.radio_timer.start(4000)

    # -- the radios: who surveys, who records, and whether survey + record is possible now
    def _ask_radios(self):
        call(self.svc.radios, lambda d, err: self._got_radios(d) if not err else None)

    def _got_radios(self, d: dict):
        self.radios_d = d
        if not getattr(self, "_roles_applied", False) and self.w_surveyor.count():
            self._roles_applied = True             # the roles chosen in the Radios panel, once
            roles = d.get("roles") or {}
            for cb, k in ((self.w_surveyor, "surveyor"), (self.w_recorder, "recorder")):
                cb.blockSignals(True)
                cb.setCurrentIndex(max(0, cb.findData(roles.get(k))))
                cb.blockSignals(False)
        here = {r["name"]: r for r in d.get("radios") or []}
        for cb in (self.w_surveyor, self.w_recorder, self.f_rx):
            for i in range(cb.count()):
                r = here.get(cb.itemData(i)) or {}
                from marsad_desktop.radios import word
                mark_radio_item(cb, i, r, word(cb.itemData(i))
                                + {True: "", False: "  (not plugged in)", None: ""}[r.get("present")])
        ok = bool((d.get("mode") or {}).get("survey_record"))
        self.m_survey.setEnabled(ok)
        self.m_survey.setToolTip("" if ok else d["mode"]["text"])
        if not ok and self.m_survey.isChecked():
            self.m_fixed.setChecked(True)
        self._mode(self.m_survey.isChecked())
        self._schedule()

    def _swap(self):
        a, b = self.w_surveyor.currentIndex(), self.w_recorder.currentIndex()
        self.w_surveyor.setCurrentIndex(b)
        self.w_recorder.setCurrentIndex(a)
        sur, rec = self.w_surveyor.currentData(), self.w_recorder.currentData()
        if sur and rec and sur != rec:              # remembered for next time, as the Radios panel's Swap is
            call(lambda: self.svc.set_roles({"surveyor": sur, "recorder": rec}), lambda d, err: None)

    def _coverage_warning(self, w: dict, plan: dict):
        """Survey + record can only record what the recording radio can tune."""
        d = getattr(self, "radios_d", None) or {}
        if w["mode"] != "survey":
            return None
        st = (plan.get("settings") or {}).get("scan") or {}
        lo, hi = st.get("lo"), st.get("hiReq")
        rec = next((r for r in d.get("radios") or [] if r["name"] == w.get("recorder")), None)
        self.cov_map.show_status(d, (lo, hi) if lo is not None else None)
        if not rec or lo is None or hi is None or hi <= lo:
            return None
        ok = sum(max(0.0, min(b, hi) - max(a, lo)) for a, b in rec["tunable_mhz"]) / (hi - lo)
        from marsad_desktop.radios import word
        if w.get("surveyor") == w.get("recorder"):
            return ["bad", "Radios", "One radio cannot survey and record at once: pick two different radios."]
        if ok >= 0.999:
            return ["ok", "Coverage", f"The {word(rec['name'])} can tune all of this survey's {lo:g}–{hi:g} MHz."]
        span = ", ".join(f"{a:g}–{b:g}" for a, b in rec["tunable_mhz"])
        return ["bad" if ok <= 0 else "warn", "Coverage",
                f"The {word(rec['name'])} can tune {100 * ok:.0f}% of this survey's {lo:g}–{hi:g} MHz (it tunes {span} "
                "MHz): anything the survey finds outside that cannot be recorded."]

    def _fill(self, keep=None):
        self.list.blockSignals(True)
        self.list.clear()
        for e in self.saved.get("watches") or []:
            if e.get("archived") and not self.c_arch.isChecked():
                continue
            st = e.get("settings") or {}
            it = QListWidgetItem(f"{e['name']}{'  · archived' if e.get('archived') else ''}\n"
                                 f"{st.get('mode', 'survey')} mode")
            it.setData(Qt.ItemDataRole.UserRole, e)
            self.list.addItem(it)
            if keep and e["name"] == keep:
                self.list.setCurrentItem(it)
        self.list.blockSignals(False)

    def _picked(self, cur, _prev=None):
        e = cur.data(Qt.ItemDataRole.UserRole) if cur else None
        if not e:
            return
        st = e.get("settings") or {}
        (self.m_fixed if st.get("mode") == "fixed" else self.m_survey).setChecked(True)
        for k, w in self.o.items():
            w.setValue(float(st.get(k) or 0))
        if st.get("mode") == "fixed":
            self.f_rx.setCurrentIndex(max(0, self.f_rx.findData(st.get("receiver"))))
            self.f_freq.setText(st.get("freq") or "")
            self.f_width.setText(st.get("width") or "")
            self.f_gain.setValue(float(st.get("gain") or 0))
            self.f_dur.setValue(float(st.get("durVal") or 1))
            self.f_unit.setCurrentIndex(1 if st.get("unit", "h") == "h" else 0)
        else:
            sc = st.get("scan") or {}
            for i in range(self.w_scan.count()):
                if (self.w_scan.itemData(i) or {}).get("name") == st.get("scan_name"):
                    self.w_scan.setCurrentIndex(i)
            self.w_surveyor.setCurrentIndex(max(0, self.w_surveyor.findData(st.get("surveyor"))))
            self.w_recorder.setCurrentIndex(max(0, self.w_recorder.findData(st.get("recorder"))))
            self.w_tag.setText(sc.get("tag") or "")
            self.w_dur.setValue(float(sc.get("durVal") or 1))
            self.w_unit.setCurrentIndex(1 if sc.get("unit", "h") == "h" else 0)
        self._schedule()

    def _mode(self, survey: bool):
        survey = self.m_survey.isChecked()
        self.survey_box.setVisible(survey)
        self.fixed_box.setVisible(not survey)
        for k in ("passes", "ambush", "recorder_gain"):
            self.o[k].setEnabled(survey)
        both = bool(((getattr(self, "radios_d", None) or {}).get("mode") or {}).get("survey_record"))
        self.mode_note.setText(
            ("One radio surveys and the other records what the first one finds. Each radio's chain is its own path "
             "in the rig used for runs." if survey else
             "One radio parked on one frequency, recording every event over the trigger level. Works with one radio.")
            + ("" if both or getattr(self, "radios_d", None) is None else
               "  Survey + record is off: " + self.radios_d["mode"]["text"]))
        self._schedule()

    def _settings(self) -> dict:
        def val(k):
            v = self.o[k].value()
            return None if v == 0 else v
        w = {k: val(k) for k in ("keep_min_snr", "capture", "threshold", "cooldown")}
        if self.m_survey.isChecked():
            e = self.w_scan.currentData() or {}
            scan = dict(e.get("settings") or {})
            scan.update(self.cond.values())
            scan.update(tag=self.w_tag.text().strip(), durVal=self.w_dur.value(),
                        unit="h" if self.w_unit.currentIndex() == 1 else "m")
            w.update(mode="survey", scan=scan, scan_name=e.get("name"), surveyor=self.w_surveyor.currentData(),
                     recorder=self.w_recorder.currentData(), passes=val("passes"), ambush=val("ambush"),
                     recorder_gain=val("recorder_gain"))
        else:
            w.update(mode="fixed", receiver=self.f_rx.currentData(), freq=self.f_freq.text().strip(),
                     width=self.f_width.text().strip(), gain=self.f_gain.value() or None,
                     durVal=self.f_dur.value(), unit="h" if self.f_unit.currentIndex() == 1 else "m")
        return w

    def _schedule(self, *_):
        self.timer.start()

    def _ask(self, detect: bool = False):
        w = self._settings()
        if w["mode"] == "fixed" and not w["freq"]:
            self.checks_panel.show_checks({}, None, [["warn", "Frequency", "Type the frequency to park on."]], [])
            return
        if w["mode"] == "survey" and not w.get("scan"):
            return
        self._seq += 1
        seq = self._seq
        if detect:
            self.checks_panel.show_checks({}, None, [], [], pending="asking each radio its serial (rtl_eeprom)…")

        def got(d, err):
            if seq != self._seq:
                return
            if err:
                self.why.setText(f"The service could not plan this session: {err}")
                return
            self.plan = d
            self.cmd.show_text(d.get("line") or "")
            sv = d.get("survey") or {}
            self.survey_fp.setText(f"<span style='color:{T.C['muted']}'>survey fingerprint</span>&nbsp; "
                                   f"{sv.get('fingerprint', '')}" if sv else "")
            ch = d.get("checks") or {}
            warn = [x for x in sv.get("warnings") or [] if x[0] != "ok"]
            if w["mode"] == "survey" and not any(w["scan"].get(k) for k in COND_KEYS if k != "where"):
                warn.append(NO_CONDITIONS)
            cov = self._coverage_warning(w, d)
            if cov:
                warn.insert(0, cov)
            self.checks_panel.show_checks(ch.get("radios") or {}, ch.get("in_use"), warn, self.rig_checks)
        call(lambda: self.svc.watch_plan({"watch": w, "detect": detect}), got)

    def _ready(self):
        ok, why = self.checks_panel.ready()
        self.b_start.setEnabled(ok)
        self.why.setText(why)

    def save_as(self):
        name, ok = QInputDialog.getText(self, "Save recorder session", "Name:")
        if not ok or not name.strip():
            return
        body = {"kind": "watch", "action": "save", "name": name.strip(), "settings": self._settings()}

        def got(d, err):
            if err:
                QMessageBox.information(self, "Not saved", err)
                return
            self.saved = d
            self._fill(name.strip())
        call(lambda: self.svc.saved_change(body), got)

    def _change(self, action):
        it = self.list.currentItem()
        e = it.data(Qt.ItemDataRole.UserRole) if it else None
        if not e:
            return
        if e.get("archived") and action == "archive":
            action = "restore"
        body = {"kind": "watch", "action": action, "name": e["name"]}
        if action in ("rename", "duplicate"):
            new, ok = QInputDialog.getText(self, action.capitalize(), "New name:", text=e["name"])
            if not ok or not new.strip():
                return
            body["new_name"] = new.strip()

        def got(d, err):
            if err:
                QMessageBox.information(self, "Not changed", err)
                return
            self.saved = d
            self._fill(body.get("new_name") or e["name"])
        call(lambda: self.svc.saved_change(body), got)

    def start(self, dry: bool):
        body = {"watch": self._settings(), "dry_run": dry,
                "chain_confirmed": self.checks_panel.confirm.isChecked(),
                "problems_confirmed": self.checks_panel.problems.isChecked()}
        self.b_start.setEnabled(False)
        self.b_dry.setEnabled(False)
        self.why.setText("Dry run…" if dry else "Checking both radios by serial, then launching…")

        def got(d, err):
            self.b_dry.setEnabled(True)
            self._ready()
            if err:
                self.why.setText(err)
                QMessageBox.information(self, "Not started", err)
                return
            if dry:
                self.cmd.show_text(d.get("line", "") + " --dry-run\n\n" + (d.get("output") or ""))
                if not self.b_cmd.isChecked():
                    self.b_cmd.setChecked(True)
                self.why.setText(f"Dry run done (exit {d.get('exit_code')}): no radio was opened.")
                return
            self.started.emit(d)
            self.accept()
        call(lambda: self.svc.watch_start(body), got)
