"""The Live Scan screen (relay 009): the home screen. It shows scans; it never starts one.

Everything comes through the background service (rule 1): closing the window affects no scan. A
small poller thread asks the service, so the window never waits on it:

- /live/progress about four times a second (no sweep rows: where the run is, its facts);
- /data only when the run has new sweeps (the same route the web page reads);
- /captures/recorders every second, /other every two seconds (only the other run's new sweeps);
- /internal once per recording (the waterfall's internal layer).

Between answers the sweep bar moves on with the clock, so it flows rather than jumps. Drawing is
pyqtgraph through OpenGL, and the waterfall keeps a pyramid of max-reduced zoom levels, as relay 001
found was needed to stay at the display's rate. Levels are relative dB, not calibrated (rule 11).
"""
from __future__ import annotations

import datetime as dt
import os
import threading
import time
import warnings

import numpy as np
import pyqtgraph as pg
import json

from PySide6.QtCore import QObject, QPointF, QRectF, QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QBrush, QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QDoubleSpinBox, QFrame, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QMessageBox, QPushButton, QScrollArea, QSplitter, QToolButton,
                               QVBoxLayout, QWidget)

from . import live_data as L
from . import scan_calc as S
from . import theme as T
from .service import Service, ServiceError

pg.setConfigOptions(antialias=False, background=T.C["plot"], foreground=T.C["muted"], useNumba=False)

FFTW_NOTE = ("rtl_power_fftw's own scale: about 26 dB below rtl_power's at 2.4 MS/s (measured at 1.93 GHz, "
             "g29). Never read the two engines on one axis.")
QUIET_S = 0.35                       # a gesture counts as over this long after the view last moved
HOLD_MAX_S = 5.0                     # never hold arriving sweeps back longer than this
HISTORY_SHOWN = 160                  # rows on screen at 1 px per sweep before the box is measured
SWEEP_HEIGHTS = ["fit", 1, 2, 4, 8, 16]
MARK_COLOURS = {"signal": "#4fcf96", "spur": "#e8b14f", "artefact": "#8599a8"}
SELECTED = "#c58bff"                 # the sweep clicked in the waterfall, as on the page
MARK_DEFAULTS = {"signal": True, "spur": True, "artefact": True, "grid": False, "hops": False}
OTHER_KEEP = 120                     # sweeps of the other recording held, as on the page


# ---------------------------------------------------------------- colour maps
def _lut(stops) -> np.ndarray:
    xs = [s for s, _ in stops]
    cs = np.array([[int(c[i:i + 2], 16) for i in (1, 3, 5)] for _, c in stops], dtype=float)
    x = np.linspace(0, 1, 256)
    return np.stack([np.interp(x, xs, cs[:, k]) for k in range(3)], axis=1).astype(np.ubyte)


COLOUR_MAPS = {
    "Classic": _lut([(0, "#0B1020"), (0.25, "#1C3F94"), (0.5, "#1FB5C9"), (0.72, "#F2D43B"), (0.9, "#F2542D"),
                     (1, "#FFF4E8")]),
    "Thermal": _lut([(0, "#000000"), (0.35, "#7A0B0B"), (0.65, "#F06A00"), (0.88, "#FFD84A"), (1, "#FFFFFF")]),
    "Ocean": _lut([(0, "#03111A"), (0.4, "#0B4F6C"), (0.7, "#1FA6A6"), (0.9, "#9BE3D0"), (1, "#F2FFFB")]),
    "Viridis": _lut([(0, "#440154"), (0.25, "#3B528B"), (0.5, "#21918C"), (0.75, "#5EC962"), (1, "#FDE725")]),
    "Greyscale": _lut([(0, "#000000"), (1, "#FFFFFF")]),
}


# ---------------------------------------------------------------- the poller
class _Blank(Exception):
    """The poller's way out of a cycle while Live Scan is blank (relay 018)."""


class Poller(QObject):
    """Asks the service from its own thread and hands each answer to the window as a signal, so a
    slow answer (opening a big finished survey) never freezes zooming or the recorder panel."""
    got = Signal(str, object)
    failed = Signal(str, str)

    def __init__(self, svc: Service):
        super().__init__()
        self.svc = svc
        self.view_file: str | None = None       # None: follow the newest recording
        self.blank = False                       # relay 018: nothing open; progress is still asked, no sweeps fetched
        self.have = 0                            # sweeps the window holds for the current file
        self.file_seen: str | None = None
        self.running = False
        self._wake = threading.Event()
        self.lock = threading.Lock()
        self.busy_data = False
        self.int_for: str | None = None          # the recording whose internal layer was fetched
        self.o_file: str | None = None           # the other recording, and how many of its sweeps we hold
        self.o_have = 0

    def start(self):
        if self.running:
            return
        self.running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self.running = False
        self._wake.set()

    def poke(self):
        self._wake.set()

    def reset(self, view_file: str | None, blank: bool = False):
        with self.lock:
            self.view_file = view_file
            self.blank = blank
            self.have = 0
            self.file_seen = None
            self.int_for = None
        self._wake.set()

    def _loop(self):
        last_rec = last_other = 0.0
        cfg_sent = False
        while self.running:
            t = time.monotonic()
            try:
                if not cfg_sent:
                    self.got.emit("config", self.svc.config())
                    cfg_sent = True
                with self.lock:
                    vf, blank = self.view_file, self.blank
                p = self.svc.progress(vf)
                if blank:                                # relay 018: only "is something running?" while blank
                    self.got.emit("blank", p)
                    raise _Blank
                self.got.emit("progress", (vf, p))      # labelled with the view it was asked for
                f = p.get("file")
                with self.lock:
                    if f != self.file_seen:
                        self.file_seen, self.have = f, 0
                    need = f is not None and int(p.get("total") or 0) > self.have or (f and self.have == 0)
                    # A finished survey nobody has opened has no reader in the service yet, so progress
                    # knows nothing of it; /data is what opens it (the slow read), as the page does.
                    need = need or (vf is not None and f is None)
                    since = self.have
                if need and not self.busy_data:
                    self.busy_data = True
                    try:
                        d = self.svc.data(since, vf if vf else None)
                    finally:
                        self.busy_data = False
                    with self.lock:
                        if self.file_seen is None and vf is not None and d.get("file"):
                            self.file_seen = d["file"]
                        if d.get("file") == self.file_seen:
                            self.have = int(d.get("total") or 0)
                    self.got.emit("data", (vf, d))
                with self.lock:
                    want_int = self.file_seen if self.have and self.int_for != self.file_seen else None
                if want_int:
                    self.int_for = want_int
                    try:
                        self.got.emit("internal", (want_int, self.svc.internal(want_int)))
                    except ServiceError as exc:
                        self.got.emit("internal", (want_int, {"found": False, "reason": f"could not ask the service: {exc}"}))
            except _Blank:
                pass
            except ServiceError as exc:
                self.failed.emit("progress", str(exc))
            if t - last_rec >= 1.0:
                last_rec = t
                try:
                    self.got.emit("recorders", self.svc.recorders())
                except ServiceError as exc:
                    self.failed.emit("recorders", str(exc))
            if t - last_other >= 2.0:
                last_other = t
                try:
                    with self.lock:
                        main = self.file_seen
                    o = self.svc.other(main, self.o_have, self.o_file)
                    oo = o.get("other") or {}
                    if oo.get("file"):
                        if oo["file"] != self.o_file:
                            oo["restart"] = True
                        self.o_file, self.o_have = oo["file"], int(oo.get("total") or 0)
                    else:
                        self.o_file, self.o_have = None, 0
                    self.got.emit("other", o)
                except ServiceError:
                    pass
            self._wake.wait(0.25)
            self._wake.clear()


# ---------------------------------------------------------------- the plots
class XViewBox(pg.ViewBox):
    """Wheel zooms the frequency axis round the cursor, a drag pans it, Shift+wheel scrolls back
    through the waterfall's history, Ctrl+wheel makes its sweeps taller or shorter, a click on the
    waterfall picks that sweep, a double-click resets the view (the page's own gestures)."""

    def __init__(self, screen, history: bool = False):
        super().__init__(enableMenu=False)
        self.screen = screen
        self.history = history
        self.setMouseEnabled(x=True, y=False)

    def wheelEvent(self, ev, axis=None):
        mods = ev.modifiers()
        if mods & Qt.KeyboardModifier.ShiftModifier:
            self.screen.scroll_history(1 if ev.delta() < 0 else -1)
            ev.accept()
            return
        if mods & Qt.KeyboardModifier.ControlModifier:
            self.screen.step_height(1 if ev.delta() > 0 else -1)
            ev.accept()
            return
        s = 0.85 if ev.delta() > 0 else 1 / 0.85
        c = self.mapToView(ev.pos())
        self.scaleBy(x=s, center=c)
        ev.accept()

    def mouseDoubleClickEvent(self, ev):
        self.screen.reset_view(clear_selection=True)
        ev.accept()

    def mouseClickEvent(self, ev):
        if self.history and ev.button() == Qt.MouseButton.LeftButton and not ev.double():
            self.screen.click_row(self.mapToView(ev.pos()).y())
            ev.accept()
            return
        ev.ignore()

    def mouseDragEvent(self, ev, axis=None):
        if ev.button() != Qt.MouseButton.LeftButton:
            ev.ignore()
            return
        ev.accept()
        d = self.mapToView(ev.lastPos()) - self.mapToView(ev.pos())
        self.translateBy(x=d.x())


class SweepAxis(pg.AxisItem):
    """The waterfall's left edge: sweep number and start time, newest at the top."""

    def __init__(self, screen):
        super().__init__("left")
        self.screen = screen

    def tickValues(self, minVal, maxVal, size):
        """One level of evenly spaced rows, at least ~36 px apart, so every label is drawn."""
        minVal, maxVal = sorted((minVal, maxVal))     # the waterfall runs newest-at-top: its range comes inverted
        span = max(1e-9, maxVal - minVal)
        step = next((k for k in (1, 2, 5, 10, 20, 25, 50, 100, 200, 500) if k * size / span >= 36), 1000)
        first = int(np.ceil(minVal / step) * step)
        return [(step, [float(v) for v in range(first, int(maxVal) + 1, step)])]

    def tickStrings(self, values, scale, spacing):
        out = []
        for v in values:
            k = self.screen.sweep_at_row(v)
            out.append("" if k is None else f"#{k[0]}  {k[1][-8:-3]}")
        return out


class SweepBar(QWidget):
    """The thin bar along the spectrum's top: "sweep 85 in progress · 14 s left" (rtl_power), or
    the pass and its hops (rtl_power_fftw), redrawn often and cheaply from the clock."""

    def __init__(self):
        super().__init__()
        self.setFixedHeight(18)
        self.fraction = None
        self.text = ""
        self.kind = "waiting"
        self.setToolTip("rtl_power writes a whole sweep at once at the end of its interval, so its position "
                        "within a sweep is shown from the clock. rtl_power_fftw writes hop by hop, so its real "
                        "current hop is shown on the spectrum.")

    def set(self, info: dict):
        if info.get("kind") == "busy" or self.kind == "busy":
            self.fraction, self.text, self.kind = info.get("fraction"), info.get("text", ""), info.get("kind")
            self.update()
        elif (info.get("fraction"), info.get("text")) != (self.fraction, self.text):
            self.fraction, self.text, self.kind = info.get("fraction"), info.get("text", ""), info.get("kind")
            self.update()

    def paintEvent(self, ev):
        qp = QPainter(self)
        r = self.rect()
        qp.fillRect(r, T.color("plot"))
        if self.kind == "busy":                     # opening a survey: a segment sliding along
            t = (time.monotonic() * 0.6) % 1.0
            w = r.width() * 0.22
            qp.fillRect(QRectF(0, r.height() - 3, r.width(), 3), T.color("lines"))
            qp.fillRect(QRectF(-w + (r.width() + w) * t, r.height() - 3, w, 3), T.color("survey"))
        if self.fraction is not None and self.kind in ("sweep", "done"):
            qp.fillRect(QRectF(0, r.height() - 3, r.width(), 3), T.color("lines"))
            qp.fillRect(QRectF(0, r.height() - 3, r.width() * float(self.fraction), 3), T.color("survey"))
        qp.setPen(T.color("survey_light") if self.kind != "done" else T.color("muted"))
        qp.setFont(T.ui_font(8))
        qp.drawText(r.adjusted(8, 0, -8, -3), int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                    self.text)
        qp.end()


# ---------------------------------------------------------------- the recorder panel
class RecorderCard(QFrame):
    """One recorder: running, starting, silent (the alert state, rule 9) or stopped."""

    def __init__(self, screen):
        super().__init__()
        self.screen = screen
        self.setObjectName("RecCard")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(T.SPACE * 1.5, T.SPACE, T.SPACE * 1.5, T.SPACE)
        lay.setSpacing(4)
        head = QHBoxLayout()
        self.title = QLabel()
        self.title.setFont(T.ui_font(9.5, QFont.Weight.DemiBold))
        self.chip = QLabel()
        self.chip.setObjectName("StateChip")
        head.addWidget(self.title, 1)
        head.addWidget(self.chip)
        lay.addLayout(head)
        self.facts = QLabel()
        self.facts.setWordWrap(True)
        self.facts.setFont(T.mono_font(8.5))
        self.facts.setProperty("muted", True)
        lay.addWidget(self.facts)
        self.plot = pg.PlotWidget()
        self.plot.setFixedHeight(96)
        self.plot.setMenuEnabled(False)
        self.plot.hideButtons()
        self.plot.setMouseEnabled(False, False)
        self.plot.getPlotItem().hideAxis("left")
        self.plot.getPlotItem().getAxis("bottom").setStyle(tickFont=T.ui_font(7))
        self.c_max = self.plot.plot(pen=pg.mkPen(T.color("recorder", 0.45), width=1))
        self.c_mean = self.plot.plot(pen=pg.mkPen(T.color("recorder"), width=1))
        self.floor = pg.InfiniteLine(angle=0, pen=pg.mkPen(T.color("muted"), style=Qt.PenStyle.DotLine))
        self.trig = pg.InfiniteLine(angle=0, pen=pg.mkPen(T.color("anomaly"), style=Qt.PenStyle.DashLine),
                                    label="trigger", labelOpts={"color": T.C["anomaly"], "position": 0.92})
        for it in (self.floor, self.trig):
            self.plot.addItem(it)
        lay.addWidget(self.plot)
        self.events = QListWidget()
        self.events.setObjectName("EventFeed")
        self.events.setFont(T.mono_font(8.5))
        self.events.setFixedHeight(130)
        self.events.setToolTip("Recent events, newest first: time, frequency, level over the floor. A PRIVATE event "
                               "is a fingerprint only: nothing plays or exports it.")
        lay.addWidget(self.events)
        self.seen_btn = QPushButton("Mark as seen")
        self.seen_btn.setToolTip("You have seen that this recorder went silent: show it quietly, as stopped")
        self.seen_btn.clicked.connect(lambda: self.screen.mark_seen(self.serial))
        lay.addWidget(self.seen_btn)
        self.serial = None

    def show_status(self, st: dict):
        self.serial = st.get("serial")
        view = st.get("view")
        who = f"{st.get('receiver') or 'recorder'}  ·  SN {st.get('serial') or '?'}"
        self.title.setText(who)
        chip = {"running": ("Running", "open"), "starting": ("Starting", "survey"),
                "silent": (L.silent_text(st.get("silent_s", 0)), "alert"), "stopped": ("Stopped", "muted"),
                "seen": ("Stopped (silent, seen)", "muted")}[view]
        self.chip.setText(chip[0])
        if view != getattr(self, "_styled", None):
            self._restyle(view, chip)
        self._fill(st, view)

    def _restyle(self, view, chip):
        """Style sheets are costly to apply (Qt recomputes the widget's style), so only on a change."""
        self._styled = view
        self.chip.setStyleSheet(f"QLabel#StateChip {{ color: {T.C[chip[1]] if chip[1] != 'muted' else T.C['muted']};"
                                f" border: 1px solid {T.C[chip[1]] if chip[1] != 'muted' else T.C['lines']};"
                                f" border-radius: 8px; padding: 1px 8px; }}")
        # relay 016: a recorder's card sits at the raised level, lit from above (an alert keeps its own red edge)
        self.setStyleSheet(f"QFrame#RecCard {{ background: {T.C['alert_bg'] if view == 'silent' else T.gradient(T.C['seg_top'], T.C['seg_bottom'])};"
                           f" {('border: 1px solid ' + T.C['alert'] + ';') if view == 'silent' else T.lit_edges('raised')}"
                           f" border-radius: {T.RADIUS}px; }}")

    def _fill(self, st, view):
        job = st.get("job") or {}
        sp = st.get("spectrum") or {}
        lines = []
        if view in ("running", "starting"):
            lines.append(f"{st.get('mode') or ''}{' · ' + job.get('kind') if job.get('kind') else ''}")
            if job:
                lines.append(f"tuned {job.get('tuned_mhz', 0):.3f} MHz · watching {job.get('target_mhz', 0):.3f}")
            if sp.get("trigger_db") is not None:
                lines.append(f"trigger {sp['trigger_db']:+.1f} dB (floor {sp.get('floor_db', 0):+.1f}, "
                             f"+{sp.get('threshold_db', 12):g}) · recorder's own scale")
            lines.append(f"{st.get('events', 0)} events · {st.get('captures', 0)} captures")
        elif view == "silent":
            heard = st.get("updated")
            lines.append(f"Its status has not changed for {L.silent_text(st.get('silent_s', 0))[7:]}: closed, killed "
                         "or hung. Whatever its file says, it is not recording.")
            if heard:
                lines.append("last heard " + dt.datetime.fromtimestamp(float(heard)).strftime("%H:%M:%S"))
        else:
            stopped = st.get("stopped") or st.get("updated")
            started = st.get("started")
            if stopped:
                lines.append("stopped " + dt.datetime.fromtimestamp(float(stopped)).strftime("%Y-%m-%d %H:%M"))
            if started and stopped:
                mins = (float(stopped) - float(started)) / 60
                lines.append(f"ran {mins:.0f} min · {st.get('events', 0)} events · {st.get('captures', 0)} captures")
        self.facts.setText("\n".join(lines))
        live = view in ("running", "starting") and sp.get("mean_db")
        self.plot.setVisible(bool(live))
        if live:
            n = len(sp["mean_db"])
            x = np.linspace(sp["lo_mhz"], sp["hi_mhz"], n)
            self.c_mean.setData(x, np.array(sp["mean_db"], dtype=float))
            self.c_max.setData(x, np.array(sp["max_db"], dtype=float))
            if sp.get("floor_db") is not None:
                self.floor.setPos(sp["floor_db"])
                self.trig.setPos(sp["trigger_db"])
        evs = st.get("recent_events") or []
        if evs != getattr(self, "_evs", None):             # rebuild the feed only when it changed
            self._evs = list(evs)
            self._feed(evs)
        self.events.setVisible(view in ("running", "starting", "silent") or bool(evs))
        self.seen_btn.setVisible(view == "silent")

    def _feed(self, evs):
        self.events.clear()
        for e in reversed(evs):
            t = dt.datetime.fromtimestamp(float(e.get("at") or 0)).strftime("%H:%M:%S")
            badge = "PRIVATE" if e.get("private") else ("IQ" if str(e.get("iq", "")).startswith("written") else "")
            it = QListWidgetItem(f"{t}  {float(e.get('mhz') or 0):9.4f} MHz  {float(e.get('snr_db') or 0):+5.1f} dB  {badge}")
            if e.get("private"):
                it.setForeground(QBrush(T.color("private")))
                it.setToolTip("PRIVATE band: only its fingerprint was kept. Nothing plays or exports it (rule 2).")
            self.events.addItem(it)


class RecorderPanel(QFrame):
    """Always visible (owner, 2026-11-05): every recorder, and anything else that is recording."""

    def __init__(self, screen):
        super().__init__()
        self.screen = screen
        self.setObjectName("TextPanel")
        self.setMinimumWidth(280)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(T.SPACE * 1.5, T.SPACE * 1.5, T.SPACE * 1.5, T.SPACE * 1.5)
        outer.setSpacing(T.SPACE)
        t = QLabel("Recorder")
        t.setFont(T.ui_font(10, QFont.Weight.DemiBold))
        th = QHBoxLayout()
        th.addWidget(t)
        th.addStretch(1)
        self.btn_rec = QToolButton()
        self.btn_rec.setObjectName("Tool")
        self.btn_rec.setText("Start recorder…")
        self.btn_rec.setToolTip("Start `marsad watch`: one radio surveys and the other records, or one radio parked "
                                "on a frequency. Launched outside this window")
        self.btn_rec.clicked.connect(screen.open_watch)
        th.addWidget(self.btn_rec)
        outer.addLayout(th)
        self.none = QLabel("No recorder has reported. When `marsad watch` runs, its radio, tuning, trigger level and "
                           "events show here.")
        self.none.setWordWrap(True)
        self.none.setProperty("muted", True)
        outer.addWidget(self.none)
        self.box = QVBoxLayout()
        self.box.setSpacing(T.SPACE)
        outer.addLayout(self.box)
        self.cards: list = []
        self.other_title = QLabel()
        self.other_title.setWordWrap(True)
        self.other_title.setFont(T.ui_font(8.5))
        outer.addWidget(self.other_title)
        self.other_plot = pg.PlotWidget()
        self.other_plot.setFixedHeight(96)
        self.other_plot.setMenuEnabled(False)
        self.other_plot.hideButtons()
        self.other_plot.setMouseEnabled(False, False)
        self.other_plot.getPlotItem().hideAxis("left")
        self.other_plot.getPlotItem().getAxis("bottom").setStyle(tickFont=T.ui_font(7))
        self.other_plot.setToolTip("The other recording: latest sweep (blue), median of the sweeps held (grey), "
                                   "max-hold (amber). Its own scale and fingerprint, not this run's.")
        self.o_max = self.other_plot.plot(pen=pg.mkPen(QColor(232, 177, 79, 150), width=1))
        self.o_med = self.other_plot.plot(pen=pg.mkPen(T.color("muted"), width=1))
        self.o_latest = self.other_plot.plot(pen=pg.mkPen(T.color("survey"), width=1.4))
        outer.addWidget(self.other_plot)
        self.other_fall = pg.PlotWidget()
        self.other_fall.setFixedHeight(90)
        self.other_fall.setMenuEnabled(False)
        self.other_fall.hideButtons()
        self.other_fall.setMouseEnabled(False, False)
        for side in ("left", "bottom"):
            self.other_fall.getPlotItem().hideAxis(side)
        self.other_fall.getPlotItem().getViewBox().invertY(True)
        self.o_img = pg.ImageItem()
        self.o_img.setLookupTable(COLOUR_MAPS["Classic"])
        self.other_fall.addItem(self.o_img)
        self.other_fall.setToolTip(f"Its waterfall, newest at the top, the last {OTHER_KEEP} sweeps")
        outer.addWidget(self.other_fall)
        outer.addStretch(1)
        self.o_file = None
        self.o_rows: list = []
        for w in (self.other_title, self.other_plot, self.other_fall):
            w.hide()

    def show_recorders(self, views: list):
        while len(self.cards) < len(views):
            c = RecorderCard(self.screen)
            self.box.addWidget(c)
            self.cards.append(c)
        for k, c in enumerate(self.cards):
            if k < len(views):
                c.show_status(views[k])
                c.show()
            else:
                c.hide()
        self.none.setVisible(not views)

    def show_other(self, other: dict | None):
        """The page's second-radio panel: another recording being written. Sweeps arrive a few at a
        time (the poller asks from where it got to); the panel keeps the last 120."""
        if not other or not other.get("file"):
            self.o_file, self.o_rows = None, []
            for w in (self.other_title, self.other_plot, self.other_fall):
                w.hide()
            return
        if other.get("restart") or other["file"] != self.o_file:
            self.o_file, self.o_rows = other["file"], []
        self.o_rows += [[np.nan if v is None else v for v in r] for r in other.get("rows") or [] if r]
        self.o_rows = self.o_rows[-OTHER_KEEP:]
        m = other.get("meta") or {}
        eng = m.get("engine") or ""
        self.other_title.setText(
            f"<b>Also recording</b>: {(m.get('receiver') or '?').upper()} · {m.get('tag') or m.get('preset') or ''}<br>"
            f"<span style='color:{T.C['muted']}'>{other['file']} · pass {other.get('total', 0)}"
            + (f" of {other['expected']}" if other.get("expected") else "")
            + (f" <span style='color:{T.C['anomaly']}'>(rtl_power_fftw scale, not rtl_power's)</span>"
               if eng == "rtl_power_fftw" else "")
            + f"<br>{other.get('fingerprint') or ''}</span>")
        self.other_title.show()
        if other.get("lo") is None or not (self.o_rows or other.get("partial")):
            self.other_plot.hide()
            self.other_fall.hide()
            return
        held = np.array(self.o_rows, dtype=float) if self.o_rows else None
        latest = held[-1] if held is not None else np.array(
            [np.nan if v is None else v for v in other["partial"]], dtype=float)
        x = np.linspace(other["lo"], other["hi"], len(latest))
        self.o_latest.setData(x, latest, connect="finite")
        if held is not None:
            with np.errstate(all="ignore"), warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                srt = np.sort(held, axis=0)
                n = np.sum(~np.isnan(held), axis=0)
                med = np.take_along_axis(srt, np.minimum(n // 2, len(held) - 1)[None, :], 0)[0]   # the page's v[floor(n/2)]
                med[n == 0] = np.nan
                mx = np.nanmax(held, axis=0)
            self.o_med.setData(x, med, connect="finite")
            self.o_max.setData(x, mx, connect="finite")
            flat = held[np.isfinite(held)]
            lo, hi = (float(np.percentile(flat, 5)), float(np.percentile(flat, 99.5))) if flat.size else (-40, 0)
            img = held[::-1]                                         # newest at the top
            self.o_img.setImage(np.nan_to_num(img, nan=lo).T, autoLevels=False, levels=(lo, max(hi, lo + 1)))
            self.o_img.setRect(QRectF(other["lo"], 0, other["hi"] - other["lo"], len(img)))
            self.other_fall.getPlotItem().getViewBox().setRange(xRange=(other["lo"], other["hi"]), yRange=(0, len(img)),
                                                                padding=0)
            self.other_fall.show()
        self.other_plot.show()


# ---------------------------------------------------------------- the screen
class LiveScreen(QWidget):
    status = Signal(str)
    open_rig_version = Signal(dict)        # relay 012: {"ref": the run's rig_reference, "run": its tag}

    def __init__(self, svc: Service):
        super().__init__()
        self.svc = svc
        self.cfg: dict = {}
        self.prog: dict = {}
        self.prog_at = time.monotonic()
        self.file: str | None = None
        self.view_file: str | None = None          # None: following live
        self.rows = np.zeros((0, 0), dtype=np.float32)
        self.stamps: list = []
        self.medians: list = []
        self.first_index = 0                       # sweep number of row 0 (oldest kept)
        self.lo = self.hi = None
        self.maxhold = None
        self.peaks: list = []
        self.hop_centres: list = []
        self.refused: list = []
        self.engine = "rtl_power"
        self.scroll_back = 0                       # rows scrolled back through history
        self.pyramid: list = []                    # (factor, image) max-reduced along frequency
        self.levels = (-40.0, 0.0)
        from .chain_screen import prefs
        self.prefs = prefs()
        self.marks = dict(MARK_DEFAULTS)
        try:                                       # remembered per viewer, as the page keeps them
            self.marks.update({k: bool(v) for k, v in json.loads(self.prefs.value("live/marks", "{}")).items()
                               if k in MARK_DEFAULTS})
        except (TypeError, ValueError):
            pass
        self.cmap = self.prefs.value("live/cmap", "Classic")
        if self.cmap not in COLOUR_MAPS:
            self.cmap = "Classic"
        self.layers = {"noise": True, "signal": True, "internal": True}
        self.sig_thr = 6.0
        self.intdata: dict | None = None           # /internal for this recording
        self.mask = None                           # columns holding a receiver line
        self.step_k = None                         # bin width, kHz: the spur tolerance
        self.layered = None                        # rows as drawn under the layer choice (None: all three on)
        self.fit = True                            # sweep height: every sweep held, squeezed to fit
        self.ry = 1                                # or this many pixels per sweep
        self.sel_sweep: int | None = None          # the sweep clicked in the waterfall
        self.floor_db = None                       # the spectrum's noise floor (20th percentile, last 40)
        self.opening = False                       # a finished survey is being read
        self.recwatch = L.RecorderWatch()
        self.rec_views: list = []
        # relay 018: opens with no scan loaded. MARSAD_LIVE_FOLLOW=1 opens following the newest recording, as before
        # (for the older window checks, which were written for that).
        self.blank = os.environ.get("MARSAD_LIVE_FOLLOW") != "1"
        self.blank_prog: dict = {}
        self._build()
        self.poller = Poller(svc)
        self.poller.blank = self.blank
        self.poller.got.connect(self._got)
        self.poller.failed.connect(self._failed)
        self.clock = QTimer(self)
        self.clock.timeout.connect(self._tick)
        self.clock.start(100)                      # the sweep bar and the "silent" count move on with the clock
        # Sweeps that arrive while the view is being moved wait until the gesture pauses: redrawing the
        # whole waterfall then would land inside a moving frame (relay 009, the one ~40 ms frame).
        self._moved_at = -1e9
        self._held: list = []
        self._held_since = None
        self._release = QTimer(self)
        self._release.setSingleShot(True)
        self._release.timeout.connect(self._release_held)

    # -- layout
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        bar = QFrame()
        bar.setObjectName("Toolbar")
        rows = QVBoxLayout(bar)
        rows.setContentsMargins(T.SPACE * 2, T.SPACE, T.SPACE * 2, T.SPACE)
        rows.setSpacing(4)
        top = QHBoxLayout()
        top.setSpacing(T.SPACE)
        self.follow_lbl = QLabel()
        self.follow_lbl.setFont(T.ui_font(9, QFont.Weight.DemiBold))
        top.addWidget(self.follow_lbl)
        self.btn_follow = QToolButton()
        self.btn_follow.setObjectName("Tool")
        self.btn_follow.setText("Follow live")
        self.btn_follow.setToolTip("Go back to following the newest recording as it is written")
        self.btn_follow.clicked.connect(lambda: self.open_survey(None))
        top.addWidget(self.btn_follow)
        self.btn_list = QToolButton()
        self.btn_list.setObjectName("Tool")
        self.btn_list.setText("Surveys ▾")
        self.btn_list.setToolTip("Open any finished survey instead of the live one")
        self.btn_list.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        from .chain_screen import Menu
        self.list_menu = Menu(self)
        self.list_menu.aboutToShow.connect(self._fill_list)
        self.btn_list.setMenu(self.list_menu)
        top.addWidget(self.btn_list)
        self.tag = QLabel()
        self.tag.setFont(T.ui_font(11, QFont.Weight.DemiBold))
        top.addWidget(self.tag)
        self.engine_lbl = QLabel()
        self.engine_lbl.setObjectName("Pill")
        top.addWidget(self.engine_lbl)
        top.addStretch(1)
        from .radios import RadiosStrip
        self.radios = RadiosStrip()
        self.radios.open_panel.connect(self.open_radios)
        self.radios.swap.connect(self.swap_radios)
        self.radios.enable.connect(self.enable_radio)
        top.addWidget(self.radios)
        top.addSpacing(T.SPACE)
        self.btn_start = QToolButton()
        self.btn_start.setObjectName("Primary")
        self.btn_start.setText("Start a scan")
        self.btn_start.setToolTip("Pick a saved scan (or set up a new one), check, and start it. The run is "
                                  "launched outside this window, so closing the window never stops it")
        self.btn_start.clicked.connect(self.open_start)
        top.addWidget(self.btn_start)
        self.btn_record = QToolButton()
        self.btn_record.setObjectName("Tool")
        self.btn_record.setText("Start recording…")
        from . import explain as X
        X.attach(self.btn_record, "start_recording")
        self.btn_record.clicked.connect(self.open_watch)
        top.addWidget(self.btn_record)
        X.attach(self.btn_start, "start_scan")
        X.attach(self.btn_follow, "follow")
        self.btn_view = QToolButton()
        self.btn_view.setObjectName("Tool")
        self.btn_view.setText("View ▾")
        self.btn_view.setToolTip("Marks on the spectrum, and the waterfall's colours")
        self.btn_view.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.btn_view.setMenu(self._view_menu(Menu))
        top.addWidget(self.btn_view)
        self.btn_stop = QToolButton()
        self.btn_stop.setObjectName("Danger")
        self.btn_stop.setText("Stop run")
        self.btn_stop.clicked.connect(self.stop_run)
        top.addWidget(self.btn_stop)
        rows.addLayout(top)
        self.facts = QLabel()
        self.facts.setFont(T.ui_font(8.5))
        self.facts.setProperty("muted", True)
        frow = QHBoxLayout()
        frow.setSpacing(8)
        frow.addWidget(self.facts, 1)
        self.btn_rig = QToolButton()              # relay 012: the drawing this run recorded, read-only in Chain
        self.btn_rig.setObjectName("Tool")
        self.btn_rig.setText("Open rig as it was")
        self.btn_rig.clicked.connect(lambda: self.open_rig_version.emit(dict(self._rig_ref)))
        self.btn_rig.hide()
        self._rig_ref = {}
        frow.addWidget(self.btn_rig, 0, Qt.AlignmentFlag.AlignTop)
        rows.addLayout(frow)
        self.fp = QLabel()
        self.fp.setFont(T.mono_font(8.5))
        self.fp.setStyleSheet(f"color: {T.C['text2']};")
        self.fp.setToolTip("The comparability fingerprint: two runs compare only if theirs match")
        rows.addWidget(self.fp)
        root.addWidget(bar)
        # relay 018: a run in progress is never hidden by the blank start
        self.banner = QFrame()
        self.banner.setObjectName("Banner")
        bl = QHBoxLayout(self.banner)
        bl.setContentsMargins(T.SPACE * 2, T.SPACE - 2, T.SPACE * 2, T.SPACE - 2)
        bl.setSpacing(T.SPACE)
        self.banner_dot = QLabel("●")
        self.banner_text = QLabel()
        self.banner_text.setObjectName("BannerText")
        self.banner_text.setWordWrap(True)
        self.banner_show = QToolButton()
        self.banner_show.setObjectName("Primary")
        self.banner_show.setText("Show it")
        self.banner_show.setToolTip("Follow the scan being written now")
        self.banner_show.clicked.connect(lambda *_: self.open_survey(None))
        bl.addWidget(self.banner_dot)
        bl.addWidget(self.banner_text, 1)
        bl.addWidget(self.banner_show)
        self.banner.hide()
        root.addWidget(self.banner)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.setHandleWidth(6)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(T.SPACE * 1.5, T.SPACE, T.SPACE, T.SPACE)
        lv.setSpacing(0)
        self.bar = SweepBar()
        lv.addWidget(self.bar)
        self.glw = pg.GraphicsLayoutWidget()
        self.gl = os.environ.get("MARSAD_NO_GL") != "1"     # OpenGL unless told not to (pictures, a broken driver)
        self.glw.useOpenGL(self.gl)
        self.glw.setBackground(T.C["plot"])
        self.vb_spec = XViewBox(self)
        self.vb_wf = XViewBox(self, history=True)
        self.p_spec = self.glw.addPlot(row=0, col=0, viewBox=self.vb_spec)
        self.p_wf = self.glw.addPlot(row=1, col=0, viewBox=self.vb_wf, axisItems={"left": SweepAxis(self)})
        self.vb_med = pg.ViewBox(enableMenu=False)
        self.vb_med.setMouseEnabled(False, False)
        self.p_med = self.glw.addPlot(row=2, col=0, viewBox=self.vb_med)
        lay = self.glw.ci.layout
        lay.setRowStretchFactor(0, 40)
        lay.setRowStretchFactor(1, 50)
        lay.setRowStretchFactor(2, 10)
        self.p_spec.setLabel("left", "relative dB, not calibrated", **{"font-size": "8pt"})
        self.p_spec.getAxis("left").setMinimumHeight(170)       # rule 11: the whole label always shows
        self.p_spec.setLabel("bottom", "MHz")
        self.p_spec.showGrid(x=True, y=True, alpha=0.12)
        self.p_wf.hideAxis("bottom")
        self.p_wf.setXLink(self.p_spec)
        self.vb_wf.invertY(True)
        self.p_med.setLabel("left", "median")
        self.p_med.setLabel("bottom", "sweep (whole-band median per sweep)")
        for p in (self.p_spec, self.p_wf, self.p_med):
            p.setMenuEnabled(False)
            p.hideButtons()
            p.getAxis("left").setWidth(96)              # room for "#1234  14:02" on the waterfall
            for side in ("left", "bottom"):
                p.getAxis(side).setStyle(tickFont=T.ui_font(8))   # labels wider than the axis are dropped
        self.c_max = self.p_spec.plot(pen=pg.mkPen(QColor(232, 177, 79, 150), width=1), name="max-hold")
        self.c_latest = self.p_spec.plot(pen=pg.mkPen(T.color("survey"), width=1), name="latest")
        self.c_partial = self.p_spec.plot(pen=pg.mkPen(T.color("survey", 0.45), width=1))
        self.c_sel = self.p_spec.plot(pen=pg.mkPen(SELECTED, width=1.2))
        for c in (self.c_max, self.c_latest, self.c_partial, self.c_sel):
            c.setClipToView(True)
            c.setDownsampling(auto=True, method="peak")
        self.floor_line = pg.InfiniteLine(angle=0, movable=False,
                                          pen=pg.mkPen(T.color("muted"), width=1, style=Qt.PenStyle.DashLine),
                                          label="noise floor", labelOpts={"color": T.C["muted"], "position": 0.02,
                                                                          "anchors": [(0, 1), (0, 1)]})
        self.floor_line.setToolTip("The noise floor: the 20th percentile of the last 40 sweeps, as the page draws it")
        self.p_spec.addItem(self.floor_line)
        self.floor_line.hide()
        self.sel_label = pg.TextItem("", color=SELECTED, anchor=(1, 0))
        self.p_spec.addItem(self.sel_label, ignoreBounds=True)
        self.sel_box = pg.LinearRegionItem(orientation="horizontal", movable=False, brush=pg.mkBrush(None),
                                           pen=pg.mkPen(SELECTED, width=1))
        for ln in self.sel_box.lines:
            ln.setPen(pg.mkPen(SELECTED, width=1))
            ln.setHoverPen(pg.mkPen(SELECTED, width=1))
        self.sel_box.hide()
        self.marks_sig = pg.ScatterPlotItem(symbol="t1", size=9, brush=pg.mkBrush("#4fcf96"), pen=None)
        self.marks_spur = pg.ScatterPlotItem(symbol="t1", size=9, brush=pg.mkBrush("#e8b14f"), pen=None)
        self.marks_art = pg.ScatterPlotItem(symbol="t1", size=9, brush=pg.mkBrush("#8599a8"), pen=None)
        self.ticks_hop = pg.PlotDataItem(pen=pg.mkPen(QColor(133, 153, 168, 180)), connect="pairs")
        for it in (self.marks_sig, self.marks_spur, self.marks_art, self.ticks_hop):
            self.p_spec.addItem(it)
        self.hop_band = pg.LinearRegionItem(movable=False, brush=pg.mkBrush(T.color("survey", 0.16)),
                                            pen=pg.mkPen(None))
        self.hop_band.setZValue(-5)
        self.p_spec.addItem(self.hop_band)
        self.hop_band.hide()
        self.hop_label = pg.TextItem("", color=T.C["survey_light"], anchor=(0.5, 0))
        self.p_spec.addItem(self.hop_label)
        self.rec_lines: list = []
        self.refused_items: list = []
        self.img = pg.ImageItem()
        self.img.setLookupTable(COLOUR_MAPS[self.cmap])
        self.img.setAutoDownsample(True)              # "fit" squeezes many sweeps into few pixel rows
        self.p_wf.addItem(self.img)
        self.p_wf.addItem(self.sel_box)
        self.c_med = self.p_med.plot(pen=pg.mkPen(T.color("text2"), width=1))
        self.vb_wf.sigXRangeChanged.connect(lambda *_: self.update_waterfall())
        self.vb_wf.sigResized.connect(lambda *_: self.update_waterfall())
        self.vb_spec.sigXRangeChanged.connect(lambda *_: self._draw_grid())   # the grid thins and labels with zoom
        self.vb_spec.sigXRangeChanged.connect(lambda *_: self._moving())
        self.glw.scene().sigMouseMoved.connect(self._hover)
        lv.addWidget(self.glw, 1)
        lv.addWidget(self._waterfall_controls())
        foot = QHBoxLayout()
        foot.setContentsMargins(4, 4, 0, 0)
        self.readout = QLabel(" ")
        self.readout.setFont(T.mono_font(9))
        self.readout.setStyleSheet(f"color: {T.C['text2']};")
        self.readout.setTextFormat(Qt.TextFormat.RichText)
        self.readout.setMinimumHeight(QFontMetrics(T.mono_font(9)).lineSpacing() * 3 + 4)
        self.det_lbl = QLabel(" ")
        self.det_lbl.setFont(T.ui_font(8.5))
        self.det_lbl.setStyleSheet(f"color: {T.C['text2']};")
        self.det_lbl.setToolTip("Detections: max-hold at least the threshold above the latest sweep's floor, tagged the "
                                "way the analyser tags them. The report is the authority; this is its preview.")
        self.hops_lbl = QLabel()
        self.hops_lbl.setFont(T.ui_font(8.5))
        self.hops_lbl.setStyleSheet(f"color: {T.C['alert_text']};")
        self.scale_note = QLabel()
        self.scale_note.setFont(T.ui_font(8))
        self.scale_note.setProperty("muted", True)
        self.scale_note.setWordWrap(True)
        foot.addWidget(self.readout, 1)
        foot.addWidget(self.hops_lbl, 0, Qt.AlignmentFlag.AlignTop)
        lv.addWidget(self.det_lbl)
        lv.addLayout(foot)
        lv.addWidget(self.scale_note)
        split.addWidget(left)
        self.rec_panel = RecorderPanel(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(self.rec_panel)
        scroll.setMinimumWidth(300)
        split.addWidget(scroll)
        split.setStretchFactor(0, 1)
        split.setSizes([1100, 340])
        root.addWidget(split, 1)
        # relay 018: the quiet centre message while no scan is open
        self.blank_msg = QLabel("No scan open.\nStart a scan, or open a finished one (File > Open a finished survey).",
                                self.glw)
        self.blank_msg.setObjectName("BlankMessage")
        self.blank_msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.blank_msg.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.blank_msg.setStyleSheet(f"QLabel#BlankMessage {{ color: {T.C['muted']}; background: {T.C['panel']};"
                                     f" border: 1px dashed {T.C['line_strong']}; border-radius: {T.RADIUS}px;"
                                     f" padding: {T.SPACE * 2}px {T.SPACE * 3}px; font-size: 10.5pt; }}")
        self.glw.installEventFilter(self)
        self._show_mode()
        self._show_blank()

    def _waterfall_controls(self) -> QWidget:
        """Under the waterfall, as on the page: sweep height, the three layers with the signal
        threshold, and the notes saying what the picture is."""
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(4, 6, 0, 0)
        v.setSpacing(3)
        h = QHBoxLayout()
        h.setSpacing(4)
        from . import explain as X
        lab = X.titled("sweep height", "sweep_height")
        h.addWidget(lab)
        self.height_btns = QButtonGroup(w)
        for k, ry in enumerate(SWEEP_HEIGHTS):
            b = QToolButton()
            b.setObjectName("Tool")
            b.setCheckable(True)
            b.setText("fit" if ry == "fit" else f"{ry} px")
            b.setToolTip("Every sweep held, squeezed to fit" if ry == "fit" else
                         f"{ry} pixel{'s' if ry != 1 else ''} per sweep; Shift+wheel scrolls back through the rest")
            self.height_btns.addButton(b, k)
            h.addWidget(b)
        self.height_btns.idClicked.connect(lambda k: self.set_height(SWEEP_HEIGHTS[k]))
        h.addSpacing(14)
        lab = QLabel("layers")
        lab.setProperty("muted", True)
        h.addWidget(lab)
        self.ly_boxes = {}
        for key, text, tip in (("noise", "noise", "The floor of each sweep on its own: a running median over about "
                                "2 MHz of that sweep, so it follows the floor moving in time"),
                               ("signal", "signal ≥ floor +", "Power above that sweep's floor, where it is at least "
                                "the threshold over it"),
                               ("internal", "internal", "The receiver's own lines (14.4 MHz clock family, hop-centre "
                                "DC spikes) and, when one matches, a terminated run's floor")):
            cb = QCheckBox(text)
            cb.setChecked(True)
            cb.setToolTip(tip)
            from . import explain as X
            X.attach(cb, {"noise": "layer_noise", "signal": "layer_signal", "internal": "layer_internal"}[key])
            cb.toggled.connect(lambda on, k=key: self.set_layer(k, on))
            self.ly_boxes[key] = cb
            h.addWidget(cb)
            if key == "signal":
                self.thr_box = QDoubleSpinBox()
                self.thr_box.setRange(0, 40)
                self.thr_box.setDecimals(0)
                self.thr_box.setValue(self.sig_thr)
                self.thr_box.setSuffix(" dB")
                X.attach(self.thr_box, "signal_threshold")
                self.thr_box.valueChanged.connect(self.set_threshold)
                h.addWidget(self.thr_box)
        self.ly_note = QLabel()
        self.ly_note.setProperty("muted", True)
        self.ly_note.setFont(T.ui_font(8))
        h.addSpacing(8)
        h.addWidget(self.ly_note, 1)
        v.addLayout(h)
        self.ly_int = QLabel()
        self.ly_int.setWordWrap(True)
        self.ly_int.setFont(T.ui_font(8))
        self.ly_int.setProperty("muted", True)
        v.addWidget(self.ly_int)
        sel = QHBoxLayout()
        self.sel_lbl = QLabel()
        self.sel_lbl.setStyleSheet(f"color: {SELECTED};")
        self.sel_lbl.setFont(T.ui_font(8.5))
        self.sel_clear = QToolButton()
        self.sel_clear.setObjectName("Tool")
        self.sel_clear.setText("clear")
        self.sel_clear.setToolTip("Take the selected sweep off the spectrum")
        self.sel_clear.clicked.connect(lambda: self.select_sweep(None))
        sel.addWidget(self.sel_lbl)
        sel.addWidget(self.sel_clear)
        sel.addStretch(1)
        self.vinfo = QLabel()
        self.vinfo.setProperty("muted", True)
        self.vinfo.setFont(T.ui_font(8))
        sel.addWidget(self.vinfo)
        v.addLayout(sel)
        self._layer_note()
        self._sync_height()
        self.select_sweep(None)
        return w

    def _view_menu(self, Menu):
        m = Menu(self)
        m.add_heading("Marks on the spectrum")
        for key, text in (("signal", "Untagged detections (possible signals)"), ("spur", "Known spur families"),
                          ("artefact", "Hop-centre DC spikes and hop-wide impulses"),
                          ("grid", "Spur family grid (ticks at the top)"), ("hops", "Hop centres (ticks at the bottom)")):
            a = QAction(text, m, checkable=True)
            a.setChecked(self.marks[key])
            a.toggled.connect(lambda on, k=key: self._set_mark(k, on))
            m.addAction(a)
        m.add_heading("Waterfall colours")
        grp = QActionGroup(m)
        self.cmap_actions = {}
        for name in COLOUR_MAPS:
            a = QAction(name, m, checkable=True)
            a.setChecked(name == self.cmap)
            a.triggered.connect(lambda _=False, n=name: self.set_cmap(n))
            grp.addAction(a)
            m.addAction(a)
            self.cmap_actions[name] = a
        return m

    # -- life
    def showEvent(self, ev):
        super().showEvent(ev)
        self.poller.start()
        if not hasattr(self, "radio_timer"):
            self.radio_timer = QTimer(self)
            self.radio_timer.timeout.connect(self._ask_radios)
        self.radio_timer.start(5000)
        self._ask_radios()

    def _ask_radios(self):
        """The radio strip: presence by serial from Windows' device list, nothing opened (cheap)."""
        from .scan_start import call
        call(self.svc.radios, lambda d, err: self.radios.show_status(d) if not err and self.isVisible() else None)

    def swap_radios(self):
        """Swap on the strip: asks first, naming both radios' new jobs."""
        from PySide6.QtWidgets import QMessageBox
        from .radios import word
        from .scan_start import call
        roles = (self.radios.data or {}).get("roles") or {}
        sur, rec = roles.get("surveyor"), roles.get("recorder")
        if not sur or not rec:
            return
        if QMessageBox.question(
                self, "Swap the radios' jobs?",
                f"After the swap the {word(rec)} surveys and the {word(sur)} records what it finds.\n\n"
                "This applies to recorder sessions started from now on (survey + record). Nothing already "
                "running changes, and a plain scan still uses the radio you pick for it.") \
                != QMessageBox.StandardButton.Yes:
            return
        call(lambda: self.svc.set_roles({"swap": True}),
             lambda d, err: self.radios.show_status(d) if not err else QMessageBox.warning(self, "Swap refused", str(err)))

    def enable_radio(self, name: str, on: bool):
        from .scan_start import call
        call(lambda: self.svc.set_enabled(name, on),
             lambda d, err: self.radios.show_status(d) if not err else self._ask_radios())

    def open_radios(self):
        from .radios import RadiosDialog
        d = RadiosDialog(self.svc, self)
        d.changed.connect(self.radios.show_status)
        d.exec()

    def hideEvent(self, ev):
        super().hideEvent(ev)
        if hasattr(self, "radio_timer"):
            self.radio_timer.stop()

    def shutdown(self):
        self.poller.stop()

    # -- from the poller
    def _failed(self, what: str, msg: str):
        if what == "progress":
            self.follow_lbl.setText("● service not answering")
            self.follow_lbl.setStyleSheet(f"color: {T.C['alert']};")
            self.status.emit(f"Live Scan: {msg}")

    # -- relay 018: the blank start
    def _show_blank(self):
        """No scan loaded: empty spectrum and waterfall, no title, a quiet centre message. The radio cards and the
        recorder panel go on showing their state; the banner names anything running."""
        on = self.blank
        self.blank_msg.setVisible(on)
        self.facts.setVisible(not on)                # no empty rows in the header while blank
        self.fp.setVisible(not on)
        if on:
            self._place_blank()
            self.tag.setText("")
            self.facts.setText("")
            self.fp.setText("")
            self.engine_lbl.setText("")
            self.engine_lbl.setStyleSheet("")
            self.btn_rig.hide()
            self.follow_lbl.setText("◻ No scan open")
            self.follow_lbl.setStyleSheet(f"color: {T.C['muted']};")
            self.btn_follow.setVisible(True)
            self.btn_follow.setText("Follow live")
            self.btn_stop.setEnabled(False)
            self.btn_stop.setToolTip("No scan is open here. A running scan shows in the banner; Show it, then Stop")
        self._update_banner()

    def _place_blank(self):
        m = self.blank_msg
        m.adjustSize()
        m.move(max(8, (self.glw.width() - m.width()) // 2), max(8, (self.glw.height() - m.height()) // 3))
        m.raise_()

    def eventFilter(self, obj, ev):
        if obj is self.glw and ev.type() == ev.Type.Resize and self.blank_msg.isVisible():
            self._place_blank()
        return super().eventFilter(obj, ev)

    def _update_banner(self):
        """The banner while blank: a scan or recorder running is named, with how long it has run and one click to
        follow it; a recorder that stopped reporting is said to be silent (rule 9)."""
        if not self.blank:
            self.banner.hide()
            return
        b = L.running_banner(self.blank_prog, self.rec_views, dt.datetime.now())
        if not b:
            self.banner.hide()
            return
        colour = {"scan": "open", "recorder": "recorder", "silent": "anomaly"}[b["kind"]]
        self.banner_dot.setStyleSheet(f"color: {T.C[colour]};")
        self.banner_text.setText(b["text"])
        self.banner_show.setVisible(b["kind"] == "scan")
        self.banner.setStyleSheet(f"QFrame#Banner {{ background: {T.rgba(colour, 0.12)};"
                                  f" border-bottom: 1px solid {T.rgba(colour, 0.45)}; }}")
        self.banner.show()

    def close_survey(self):
        """File > Close: back to the blank screen (nothing stops; a running scan shows in the banner)."""
        self.blank = True
        self.view_file = None
        self._held, self._held_since = [], None
        self._clear(None)
        self.prog = {}
        self.opening = False
        self.poller.reset(None, blank=True)
        self._show_mode()
        self._show_blank()

    def _got(self, what: str, d):
        if what == "blank":
            self.blank_prog = d or {}
            if self.blank and self.prefs.value("start/live_follow", False, type=bool) \
                    and L.scan_running(self.blank_prog):
                self.open_survey(None)          # Settings: open on the running scan automatically
                return
            self._update_banner()
            return
        if what == "config":
            self.cfg = d or {}
            self._draw_grid()
        elif what in ("progress", "data"):
            vf, d = d
            if self.blank:
                return      # an answer asked before the screen went blank
            if vf != self.view_file:
                return      # asked for the view just left (the poller was mid-cycle when the survey changed)
            if what == "progress":
                self._progress(d)
            elif self._held or self._is_moving():
                self._hold(("data", d))
            else:
                self._data(d)
        elif what == "internal":
            f, got = d
            if f == self.file:
                self.intdata = got or {}
                self.mask = None
                self._relayer()
                self._layer_note()
                self.update_waterfall()
        elif what == "recorders":
            # freshness is noted the moment the answer arrives (the 15 s rule stays exact);
            # only the redraw waits while the view is moving
            self.rec_views = self.recwatch.update(d.get("recorders") or [])
            if self.blank:
                self._update_banner()
            if self._held or self._is_moving():
                self._hold(("recorders", None))
            else:
                self._show_recorders()
        elif what == "other":
            if self._held or self._is_moving():
                self._hold(("other", d))         # incremental: kept in order
            else:
                self.rec_panel.show_other((d or {}).get("other"))

    def _progress(self, p: dict):
        if not (p or {}).get("file") and self.view_file and self.file:
            return          # an answer from before the survey was opened, arriving after it: keep what is shown
        self.prog, self.prog_at = p or {}, time.monotonic()
        f = self.prog.get("file")
        if f != self.file:
            self._clear(f)
        self._show_mode()
        self._facts()

    def _clear(self, f):
        self.file = f
        self.rows = np.zeros((0, 0), dtype=np.float32)
        self.stamps, self.medians, self.peaks, self.hop_centres, self.refused = [], [], [], [], []
        self.maxhold = None
        self.first_index = 0
        self.scroll_back = 0
        self.lo = self.hi = None
        self.pyramid = []
        self.layered = None
        self.intdata = None
        self.mask = None
        self.step_k = None
        self.floor_db = None
        for c in (self.c_max, self.c_latest, self.c_partial, self.c_med, self.c_sel):
            c.setData([], [])
        self.img.clear()
        self.floor_line.hide()
        self.det_lbl.setText(" ")
        self.select_sweep(None)
        self._layer_note()

    def _data(self, d: dict):
        if d.get("file") != self.file:
            self._clear(d.get("file"))
        self.opening = False
        self.engine = d.get("engine") or "rtl_power"
        lo, hi = d.get("lo"), d.get("hi")
        if lo is None or hi is None:
            return
        first_view = self.lo is None
        self.lo, self.hi = float(lo), float(hi)
        if d.get("step_khz") and d["step_khz"] != self.step_k:
            self.step_k = d["step_khz"]
            self.mask = None
        if d.get("hop_centres_mhz"):
            self.hop_centres = list(d["hop_centres_mhz"])
        new = d.get("rows") or []
        if new:
            arr = np.array([[np.nan if v is None else v for v in r] for r in new], dtype=np.float32)
            if self.rows.size == 0 or self.rows.shape[1] != arr.shape[1]:
                self.rows = arr
                self.first_index = int(d.get("from") or 0)
                self.stamps = list(d.get("stamps") or [])
                self.medians = list(d.get("medians") or [])
                self.layered = None
            else:
                self.rows = np.vstack([self.rows, arr])
                self.stamps += list(d.get("stamps") or [])
                self.medians += list(d.get("medians") or [])
            keep = 2000                            # what the window holds; the service keeps its own ring
            if len(self.rows) > keep:
                cut = len(self.rows) - keep
                self.rows, self.stamps, self.medians = self.rows[cut:], self.stamps[cut:], self.medians[cut:]
                self.first_index += cut
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)        # a column with no reading yet
                mh = np.nanmax(arr, axis=0)
            self.maxhold = mh if self.maxhold is None or len(self.maxhold) != len(mh) else np.fmax(self.maxhold, mh)
            self._since_levels = getattr(self, "_since_levels", 99) + len(arr)
            if self._since_levels >= 10 or len(arr) > 1:      # colour levels barely move sweep to sweep
                self._since_levels = 0
                self._levels()
            self._relayer(len(arr))
        self.refused = list(d.get("refused_mhz") or [])
        if d.get("peaks") is not None:
            self.peaks = d["peaks"]
            self.det_lbl.setText(S.detection_line(self.peaks, self.cfg.get("peak_threshold_db") or 10))
        self._draw_spectrum(d.get("partial"))
        self._draw_refused()
        self._draw_marks()
        self.update_waterfall()
        self.c_med.setData(np.arange(self.first_index + 1, self.first_index + len(self.medians) + 1),
                           np.array([np.nan if m is None else m for m in self.medians], dtype=float), connect="finite")
        if first_view:
            self.reset_view()
        if not self.prog.get("file"):
            self.poller.poke()          # a survey just opened: ask where it stands now, not next cycle
        self._facts()

    # -- drawing
    def _x(self, n: int) -> np.ndarray:
        return np.linspace(self.lo, self.hi, n)

    def _draw_spectrum(self, partial=None):
        if self.rows.size == 0:
            return
        x = self._x(self.rows.shape[1])
        self.c_latest.setData(x, self.rows[-1], connect="finite")
        if self.maxhold is not None:
            self.c_max.setData(x, self.maxhold, connect="finite")
        if partial:
            pa = np.array([np.nan if v is None else v for v in partial], dtype=float)
            self.c_partial.setData(self._x(len(pa)), pa, connect="finite")
        else:
            self.c_partial.setData([], [])
        self.floor_db, _top = S.display_floor(self.rows[-40:])
        if self.floor_db is not None:
            self.floor_line.setPos(self.floor_db)
            self.floor_line.show()
        lo, hi = self.levels
        self.vb_spec.setYRange(lo - 3, max(hi + 6, lo + 10), padding=0)
        self._draw_selected()

    def _levels(self):
        v = self.rows[-min(len(self.rows), 200):]
        v = v[np.isfinite(v)]
        if v.size:
            lo, hi = np.percentile(v, [5, 99.7])           # one pass for both
            self.levels = (float(lo), float(hi))

    # -- the layers (the page's split, ported in scan_calc)
    def _all_layers(self) -> bool:
        return all(self.layers.values())

    def _relayer(self, new_rows: int | None = None):
        """Work the layered picture out again: for the newest `new_rows` only when sweeps arrive,
        for every sweep held when the choice changes. All three on is the measurement itself."""
        if self._all_layers() or self.rows.size == 0 or self.lo is None:
            self.layered = None
            self._build_pyramid(new_rows)
            return
        if self.mask is None:
            fams = (self.intdata or {}).get("families_mhz") or [14.4]
            self.mask = S.build_mask(self.lo, self.hi, self.rows.shape[1], self.step_k, fams, self.hop_centres)
        term = None
        if (self.intdata or {}).get("found") and self.intdata.get("floor"):
            term = np.array([np.nan if v is None else v for v in self.intdata["floor"]], dtype=float)
            if len(term) != self.rows.shape[1]:
                term = None

        def split(rows):
            return S.layer_rows(rows, S.sweep_floors(rows, self.lo, self.hi), self.mask, term,
                                self.layers["noise"], self.layers["signal"], self.layers["internal"], self.sig_thr)
        if (new_rows and self.layered is not None and self.layered.shape[1] == self.rows.shape[1]
                and len(self.layered) + new_rows >= len(self.rows)):
            self.layered = np.vstack([self.layered, split(self.rows[-new_rows:])])[-len(self.rows):]
            self._build_pyramid(new_rows)
        else:
            self.layered = split(self.rows)
            self._build_pyramid()

    def set_layer(self, key: str, on: bool):
        self.layers[key] = on
        self._relayer()
        self._layer_note()
        self.update_waterfall()

    def set_threshold(self, v: float):
        self.sig_thr = float(v)
        if not self._all_layers():
            self._relayer()
            self.update_waterfall()

    def _layer_note(self):
        if not hasattr(self, "ly_note"):
            return
        self.ly_note.setText("all three on = the raw measurement" if self._all_layers() else
                             "an estimate of the split, composed in linear power; all three on is the raw measurement")
        d = self.intdata
        if not d:
            self.ly_int.setText("")
            return
        self.ly_int.setText("Internal layer: 14.4 MHz clock family and hop centres (hop centres only where a column is "
                            "finer than a quarter hop); "
                            + (f"receiver floor from the terminated run {d.get('name')}." if d.get("found")
                               else f"{d.get('reason') or 'no terminated run matches'}."))

    def _levels_of(self, rows) -> list:
        img = np.nan_to_num(rows, nan=self.levels[0] - 6).astype(np.float32)
        pyr = [(1, img)]
        f = 1
        while img.shape[1] > 256:
            w = img.shape[1] // 2 * 2
            img = np.maximum(img[:, 0:w:2], img[:, 1:w:2])
            f *= 2
            pyr.append((f, img))
        return pyr

    def _build_pyramid(self, new_rows: int | None = None):
        """Max-reduced copies of the waterfall along frequency (1, 2, 4, 8...), so a wide view never
        squeezes more columns into a pixel than the GPU should draw (relay 001). Each row reduces on
        its own, so new sweeps are reduced alone and appended; the rest is built once."""
        src = self.rows if self.layered is None else self.layered
        if src.size == 0:
            self.pyramid = []
            return
        if (new_rows and self.pyramid and self.pyramid[0][1].shape[1] == src.shape[1]
                and len(self.pyramid[0][1]) + new_rows >= len(src)):
            fresh = self._levels_of(src[-new_rows:])
            self.pyramid = [(f, np.vstack([old, add])[-len(src):]) for (f, old), (_, add) in zip(self.pyramid, fresh)]
        else:
            self.pyramid = self._levels_of(src)

    # -- the waterfall: sweep height, history, the selected sweep
    def _rows_shown(self) -> int:
        """How many sweeps the waterfall shows: all held in "fit", else what fits at ry px each."""
        if self.fit:
            return max(1, len(self.rows))
        return max(1, int(self.vb_wf.height() // self.ry) or HISTORY_SHOWN)

    def update_waterfall(self):
        if not self.pyramid or self.lo is None:
            return
        (x0, x1), _ = self.vb_wf.viewRange()
        px = max(1.0, self.vb_wf.width())
        cols = self.pyramid[0][1].shape[1]
        shown = cols * max(1e-9, (x1 - x0)) / max(1e-9, self.hi - self.lo)
        per_px = shown / px
        level = self.pyramid[0]
        for p in self.pyramid:                         # the coarsest level still at least a column per pixel
            if p[0] <= max(1.0, per_px):
                level = p
        f, img = level
        n = img.shape[0]
        want = self._rows_shown()
        self.scroll_back = 0 if self.fit else int(min(max(0, self.scroll_back), max(0, n - want)))
        end = n - self.scroll_back
        start = max(0, end - want)
        part = img[start:end][::-1]                    # newest at the top
        if part.size == 0:
            return
        self.img.setImage(part.T, autoLevels=False, levels=self.levels)
        self.img.setRect(QRectF(self.lo, 0, self.hi - self.lo, part.shape[0]))
        self.vb_wf.setYRange(0, part.shape[0] if self.fit else max(want, part.shape[0]), padding=0)
        self._shown = (start, end)
        self._draw_selected()
        self._view_info()

    def _view_info(self):
        if not hasattr(self, "vinfo") or self.lo is None:
            return
        (x0, x1), _ = self.vb_spec.viewRange()
        zoom = (self.hi - self.lo) / max(1e-9, x1 - x0)
        start, end = getattr(self, "_shown", (0, 0))
        total = self.first_index + len(self.rows)
        what = (f"all {len(self.rows)} sweeps held" if self.fit else
                f"sweeps #{self.first_index + start + 1}–#{self.first_index + end} of {total}, {self.ry} px per sweep")
        self.vinfo.setText((f"zoom {zoom:.1f}×  {x0:.1f}–{x1:.1f} MHz  ·  " if zoom > 1.01 else "") + what)

    def sweep_at_row(self, v: float):
        """(sweep number, stamp) for a row of the waterfall as drawn (0 = the newest shown)."""
        if not self.stamps or not hasattr(self, "_shown"):
            return None
        start, end = self._shown
        k = end - 1 - int(v)
        if not (start <= k < end):
            return None
        return self.first_index + k + 1, self.stamps[k] if k < len(self.stamps) else ""

    # -- holding sweeps back while the view moves
    def _moving(self):
        self._moved_at = time.monotonic()

    def _is_moving(self) -> bool:
        return time.monotonic() - self._moved_at < QUIET_S

    def _show_recorders(self):
        self.rec_panel.show_recorders(self.rec_views)
        self._recorder_lines()

    def _hold(self, item: tuple):
        """Keep a redraw - new sweeps, the recorder panel, the second radio - until the gesture
        pauses (HOLD_MAX_S at most, so a long gesture can't starve the screen). In order: the sweep
        and second-radio answers are incremental."""
        if not self._held:
            self._held_since = time.monotonic()
        self._held.append(item)
        self._release.start(int(QUIET_S * 1000) + 20)

    def _release_held(self):
        waited = time.monotonic() - (self._held_since or 0)
        if self._is_moving() and waited < HOLD_MAX_S:
            self._release.start(int(QUIET_S * 1000) + 20)
            return
        held, self._held, self._held_since = self._held, [], None
        recs = False
        for what, d in held:
            if what == "data":
                self._data(d)
            elif what == "other":
                self.rec_panel.show_other((d or {}).get("other"))
            else:
                recs = True
        if recs:
            self._show_recorders()                 # its newest state; the older ones are already past

    def scroll_history(self, step: int):
        self._moving()
        if self.fit:                                    # as on the page: scrolling back leaves "fit"
            self.fit, self.ry = False, 1
            self._sync_height()
        n, want = len(self.rows), self._rows_shown()
        self.scroll_back = int(min(max(0, self.scroll_back + step * max(1, want // 8)), max(0, n - want)))
        self.update_waterfall()

    def set_height(self, ry):
        if ry == "fit":
            self.fit = True
        else:
            self.fit, self.ry = False, int(ry)
        self._sync_height()
        self.update_waterfall()

    def step_height(self, direction: int):
        """Ctrl+wheel: taller (up) or shorter (down) sweeps, 1 to 16 px, as on the page."""
        if not len(self.rows):
            return
        ry = 1 if self.fit else self.ry
        self.set_height(min(16, ry * 2) if direction > 0 else max(1, ry // 2))

    def _sync_height(self):
        if not hasattr(self, "height_btns"):
            return
        k = 0 if self.fit else (SWEEP_HEIGHTS.index(self.ry) if self.ry in SWEEP_HEIGHTS else 1)
        self.height_btns.button(k).setChecked(True)

    def click_row(self, y: float):
        """A click on the waterfall picks that sweep; clicking it again clears it (the page's rule)."""
        k = self.sweep_at_row(y)
        if k is None:
            return
        self.select_sweep(None if k[0] == self.sel_sweep else k[0])

    def select_sweep(self, n: int | None):
        self.sel_sweep = n
        if hasattr(self, "sel_lbl"):
            i = self._sel_index()
            stamp = self.stamps[i] if i is not None and i < len(self.stamps) else "?"
            self.sel_lbl.setText("" if n is None else f"▬ sweep #{n}, started {stamp} (clicked in the waterfall)")
            self.sel_clear.setVisible(n is not None)
        self._draw_selected()

    def _sel_index(self):
        if self.sel_sweep is None:
            return None
        i = self.sel_sweep - 1 - self.first_index
        return i if 0 <= i < len(self.rows) else None

    def _draw_selected(self):
        if not hasattr(self, "c_sel"):
            return
        i = self._sel_index()
        if i is None or self.lo is None:
            self.c_sel.setData([], [])
            self.sel_label.setText("")
            self.sel_box.hide()
            return
        self.c_sel.setData(self._x(self.rows.shape[1]), self.rows[i], connect="finite")
        stamp = self.stamps[i][11:16] if i < len(self.stamps) and len(self.stamps[i]) >= 16 else ""
        self.sel_label.setText(f"sweep #{self.sel_sweep}  {stamp}")
        (_x0, x1), (_y0, y1) = self.vb_spec.viewRange()
        self.sel_label.setPos(x1, y1)
        start, end = getattr(self, "_shown", (0, 0))
        if start <= i < end:
            top = end - 1 - i
            self.sel_box.setRegion((top, top + 1))
            self.sel_box.show()
        else:
            self.sel_box.hide()

    def reset_view(self, clear_selection: bool = False):
        if self.lo is None:
            return
        self.scroll_back = 0
        if clear_selection:                             # the page's double-click: back to fit, nothing selected
            self.fit, self.ry = True, 1
            self._sync_height()
            self.select_sweep(None)
        self.vb_spec.setXRange(self.lo, self.hi, padding=0)
        self.update_waterfall()

    def set_cmap(self, name: str):
        self.cmap = name
        self.img.setLookupTable(COLOUR_MAPS[name])
        self.prefs.setValue("live/cmap", name)

    # -- marks: detections, the family grid, hop centres
    def _set_mark(self, key: str, on: bool):
        self.marks[key] = on
        self.prefs.setValue("live/marks", json.dumps(self.marks))
        self._draw_marks()

    def _draw_marks(self):
        kinds = {"signal": self.marks_sig, "spur": self.marks_spur, "artefact": self.marks_art}
        for k, item in kinds.items():
            pts = [(p["f"], p.get("db", 0)) for p in self.peaks if p.get("kind") == k] if self.marks[k] else []
            item.setData([a for a, _ in pts], [b + 1.2 for _, b in pts])
        y0 = self.levels[0] - 3
        if self.marks["hops"] and self.hop_centres:
            xs = np.repeat(self.hop_centres, 2)
            ys = np.tile([y0, y0 + 1.5], len(self.hop_centres))
            self.ticks_hop.setData(xs, ys)
        else:
            self.ticks_hop.setData([], [])
        self._draw_grid()

    def _clear_grid(self):
        for it in getattr(self, "grid_items", []):
            self.p_spec.removeItem(it)
        self.grid_items = []
        self._grid_key = None

    def _draw_grid(self):
        """The spur family grid along the top, by the page's rule (scan_calc.family_ticks): a family
        too dense for the zoom is left out, and labels appear once there is room for them."""
        if not self.marks["grid"] or self.lo is None:
            self._clear_grid()
            return
        (x0, x1), (_y0, y1) = self.vb_spec.viewRange()
        x0, x1 = max(x0, self.lo), min(x1, self.hi)
        if x1 <= x0:
            return
        fams = self.cfg.get("readout_families") or [14.4, 28.8, 24, 25, 100]
        ticks = S.family_ticks(fams, x0, x1, max(1.0, self.vb_spec.width()) / (x1 - x0))
        key = (tuple((t["mhz"], t["labelled"]) for t in ticks), y1)
        if key == getattr(self, "_grid_key", None) and self.grid_items:
            return                                   # the same ticks: zooming within them redraws nothing
        self._clear_grid()
        self._grid_key = key
        by_colour: dict = {}
        for t in ticks:
            by_colour.setdefault(t["colour"], []).append(t)
        for col, ts in by_colour.items():
            it = pg.PlotDataItem(np.repeat([t["mhz"] for t in ts], 2), np.tile([y1, y1 - 1.6], len(ts)),
                                 connect="pairs", pen=pg.mkPen(QColor(col), width=1))
            it.setOpacity(0.6)
            self.p_spec.addItem(it, ignoreBounds=True)
            self.grid_items.append(it)
            for t in ts:
                if t["labelled"]:
                    lab = pg.TextItem(f"{t['base']:g}×{t['n']}", color=col, anchor=(0.5, 0))
                    lab.setFont(T.mono_font(7))
                    lab.setPos(t["mhz"], y1 - 1.6)
                    self.p_spec.addItem(lab, ignoreBounds=True)
                    self.grid_items.append(lab)

    def _draw_refused(self):
        for it in self.refused_items:
            for p in (self.p_spec, self.p_wf):
                try:
                    p.removeItem(it)
                except Exception:  # noqa: BLE001
                    pass
        self.refused_items = []
        brush = QBrush(QColor(232, 120, 120, 140), Qt.BrushStyle.BDiagPattern)
        for lo, hi in self.refused:
            for p in (self.p_spec, self.p_wf):
                r = pg.LinearRegionItem((lo, hi), movable=False, brush=brush, pen=pg.mkPen(QColor(232, 120, 120, 150)))
                r.setToolTip("hop the tuner refused (PLL would not lock): no data there")
                p.addItem(r)
                self.refused_items.append(r)
        txt = L.hops_text(self.prog.get("planned_hops"), self.prog.get("tuned_hops"), self.prog.get("refused_mhz"))
        self.hops_lbl.setText(txt if txt and self.refused else "")

    def _recorder_lines(self):
        for it in self.rec_lines:
            for p in (self.p_spec, self.p_wf):
                try:
                    p.removeItem(it)
                except Exception:  # noqa: BLE001
                    pass
        self.rec_lines = []
        for v in self.rec_views:
            job = v.get("job") or {}
            mhz = job.get("target_mhz")
            if v.get("view") not in ("running", "starting") or mhz is None:
                continue
            for p, label in ((self.p_spec, f"recorder {mhz:.3f} MHz"), (self.p_wf, None)):
                line = pg.InfiniteLine(pos=mhz, angle=90, movable=False,
                                       pen=pg.mkPen(T.color("recorder"), width=1.4, style=Qt.PenStyle.DashLine),
                                       label=label, labelOpts={"color": T.C["recorder"], "position": 0.88,
                                                               "fill": T.color("plot", 0.8)} if label else None)
                p.addItem(line)
                self.rec_lines.append(line)

    # -- the header, the bar and the hover readout
    def _show_mode(self):
        if self.blank:
            return
        live = self.view_file is None
        text, colour = ("● Following live", "open") if live else ("◼ Finished survey", "muted")
        if self.follow_lbl.text() != text:                  # restyled only on a change (runs four times a second)
            self.follow_lbl.setText(text)
            self.follow_lbl.setStyleSheet(f"color: {T.C[colour]};")
        self.btn_follow.setVisible(not live)
        can = bool(self.prog.get("can_stop")) and live and not self.prog.get("finished")
        self.btn_stop.setEnabled(can)
        self.btn_stop.setToolTip("Stop this run cleanly: the partial run is kept, analysed and archived" if can else
                                 ("Nothing is running to stop" if self.prog.get("finished") or not self.prog.get("file")
                                  else ("A finished survey is open: follow live to stop the run" if not live else
                                        "This run cannot be stopped from here: it was started by an older marsad, "
                                        "without a stop file. Stop it in its own window")))

    def _facts(self):
        p = self.prog
        f = p.get("facts") or {}
        if not p.get("file"):
            self.tag.setText("No recording yet")
            self.facts.setText("When a scan starts, it shows here as it is written.")
            self.fp.setText("")
            self.btn_rig.hide()
            self.engine_lbl.setText("")
            return
        self.tag.setText(f.get("tag") or p["file"])
        eng = f.get("engine") or p.get("engine") or "rtl_power"
        if self.engine_lbl.text() != eng:                    # styled once: this runs four times a second
            self.engine_lbl.setText(eng)
            self.engine_lbl.setStyleSheet(f"background: {T.rgba('survey', 0.14)}; color: {T.C['survey_light']};")
        bits = []
        done, exp = int(p.get("total") or 0), p.get("expected")
        word = "pass" if eng == "rtl_power_fftw" else "sweep"
        bits.append(f"{word} {done} of {exp}" if exp else f"{done} {word}{'es' if word == 'pass' else 's'}")
        st = f.get("started_local")
        if st:
            try:
                t0 = dt.datetime.fromisoformat(st)
                end = dt.datetime.fromisoformat(f["finished_local"]) if f.get("finished_local") else dt.datetime.now()
                bits.append(f"started {t0:%Y-%m-%d %H:%M} · {'ran' if f.get('finished_local') else 'elapsed'} "
                            f"{L._clock((end - t0).total_seconds())}")
            except ValueError:
                pass
        if p.get("parsed_rows") is not None:
            bits.append(f"{p['parsed_rows']} rows read" + (f", {p['nonfinite_pct']} % non-finite"
                                                            if p.get("nonfinite_pct") else ""))
        if eng == "rtl_power_fftw" and p.get("planned_hops"):
            bits.append(L.hops_text(p.get("planned_hops"), p.get("tuned_hops"), p.get("refused_mhz")))
        if p.get("receiver") or f.get("receiver"):
            bits.append(f"radio {f.get('receiver')}")
        ch = f.get("chain") or {}
        if ch.get("full"):                                   # relay 012: short here, the full text on hover
            bits.append(f"chain: {ch.get('short')}")
        if f.get("other_radios_busy"):
            bits.append(f"started while the other radio was busy: {f['other_radios_busy']}")
        ref = f.get("rig_reference")
        if isinstance(ref, dict) and not ref.get("rig"):
            bits.append("no rig drawing recorded")
        has_ref = isinstance(ref, dict) and bool(ref.get("rig")) and bool(ref.get("version"))
        self._rig_ref = {"ref": ref, "run": f.get("tag") or p.get("file")} if has_ref else {}
        if self.btn_rig.isVisible() != has_ref:
            self.btn_rig.setVisible(has_ref)
        if has_ref:
            self.btn_rig.setToolTip(f"Open “{ref['rig']}” in Chain exactly as drawn when this run started "
                                    f"(version {ref['version']}), read-only")
        text = "   ·   ".join(b for b in bits if b)
        if self.facts.text() != text:
            self.facts.setText(text)
            self.facts.setToolTip(("Chain, as recorded:\n" + ch["full"]) if ch.get("full") else "")
        self.fp.setText(f.get("fingerprint") or "fingerprint not known")
        label = "relative dB, not calibrated" + (" · fftw scale" if eng == "rtl_power_fftw" else "")
        if label != getattr(self, "_axis_label", None):     # rule 11; set only on a change (this runs 4 times a second)
            self._axis_label = label
            self.p_spec.setLabel("left", label, **{"font-size": "8pt"})
        self.scale_note.setText(("Levels are relative dB, not calibrated. " + FFTW_NOTE) if eng == "rtl_power_fftw"
                                else "Levels are relative dB on rtl_power's scale, not calibrated.")

    def _tick(self):
        if self.blank:
            self.bar.set({"kind": "waiting", "fraction": None, "text": "no scan open"})
            if any(v.get("view") in ("silent", "running", "starting") for v in self.rec_views):
                self.rec_views = self.recwatch.update([{k: v for k, v in r.items() if k not in ("view", "silent_s")}
                                                       for r in self.rec_views])
            return
        if self.opening:
            self.bar.set({"kind": "busy", "fraction": None,
                          "text": "reading the survey…" if self.view_file else "finding the newest recording…"})
            return
        info = L.progress(self.prog, self.prog_at, time.monotonic())
        self.bar.set(info)
        if self.engine == "rtl_power_fftw" and self.prog.get("cursor") is not None and not self.prog.get("finished"):
            half = None
            band = L.current_hop(self.prog, self.hop_centres, half)
            if band:
                self.hop_band.setRegion(band)
                self.hop_band.show()
                self.hop_label.setText("current hop")
                self.hop_label.setPos((band[0] + band[1]) / 2, self.levels[1] + 5)
        else:
            self.hop_band.hide()
            self.hop_label.setText("")
        if any(v.get("view") in ("silent", "running", "starting") for v in self.rec_views):
            self.rec_views = self.recwatch.update([{k: v for k, v in r.items() if k not in ("view", "silent_s")}
                                                   for r in self.rec_views])
            if any(v["view"] == "silent" for v in self.rec_views) and not self._is_moving():
                self.rec_panel.show_recorders(self.rec_views)

    def _hover(self, pos):
        """The page's readout: frequency, latest, max-hold, over the floor and the nearest family;
        the waterfall row under the mouse; the detection within 6 px."""
        if self.lo is None or self.rows.size == 0:
            return
        for vb in (self.vb_spec, self.vb_wf):
            if not vb.sceneBoundingRect().contains(pos):
                continue
            pt = vb.mapSceneToView(pos)
            mhz = pt.x()
            if not (self.lo <= mhz <= self.hi):
                return
            cols = self.rows.shape[1]
            c = S.jsround((mhz - self.lo) / (self.hi - self.lo) * (cols - 1))
            cur, hold = self.rows[-1][c], (self.maxhold[c] if self.maxhold is not None else np.nan)
            ink, amber = T.C["text"], "#e8b14f"
            html = f"<b style='color:{ink}'>{mhz:.3f} MHz</b>"
            if np.isfinite(cur):
                html += f"&nbsp; latest <b style='color:{ink}'>{cur:.1f}</b> dB"
            if np.isfinite(hold):
                html += f"&nbsp; max-hold <b style='color:{ink}'>{hold:.1f}</b> dB"
            if np.isfinite(cur) and self.floor_db is not None:
                html += f"&nbsp; over floor <b style='color:{ink}'>{cur - self.floor_db:.1f}</b> dB"
            fam = S.nearest_family(mhz, self.cfg.get("readout_families") or [14.4, 28.8, 24, 25, 100])
            if fam:
                html += f"&nbsp; <span style='color:{amber}'>≈ {fam[0]:g} MHz × {fam[1]}</span>"
            html += "&nbsp; <span style='color:{}'>(relative dB, not calibrated)</span>".format(T.C["muted"])
            if vb is self.vb_wf:
                k = self.sweep_at_row(pt.y())
                if k is not None:
                    v = self.rows[k[0] - 1 - self.first_index][c]
                    html += (f"<br><span style='color:{T.C['muted']}'>waterfall row: <b style='color:{ink}'>sweep "
                             f"#{k[0]}</b>&nbsp; started {k[1] or '?'}"
                             + (f"&nbsp; · &nbsp;<b style='color:{ink}'>{v:.1f}</b> dB here" if np.isfinite(v) else "")
                             + "</span>")
            (x0, x1), _ = self.vb_spec.viewRange()
            per_px = (x1 - x0) / max(1.0, self.vb_spec.width())
            near = [p for p in self.peaks if abs(p["f"] - mhz) <= 6 * per_px]
            if near:
                pk = min(near, key=lambda p: abs(p["f"] - mhz))
                html += (f"<br><span style='color:{MARK_COLOURS.get(pk.get('kind'), ink)}'>▲ {pk['f']:.4f} MHz&nbsp; "
                         f"{pk.get('snr', 0):.1f} dB over floor&nbsp; {pk.get('w', 0)} kHz wide &nbsp;—&nbsp; "
                         f"{pk.get('tags') or 'untagged: possible signal'}</span>")
            self.readout.setText(html)
            return

    # -- actions
    def _fill_list(self):
        m = self.list_menu
        m.clear()
        try:
            got = self.svc.surveys()
        except ServiceError as exc:
            a = m.addAction(f"Cannot read the list: {exc}")
            a.setEnabled(False)
            return
        a = m.addAction("● Follow live (the newest recording)")
        a.triggered.connect(lambda: self.open_survey(None))
        m.add_heading("Surveys, newest first")
        for s in (got.get("surveys") or [])[:60]:
            label = f"{s['name']}   ·   {s.get('mb', 0):g} MB"
            a = m.addAction(label)
            a.setToolTip(f"{s.get('engine')} · {s.get('lo')}–{s.get('hi')} MHz · started {s.get('started')}")
            a.triggered.connect(lambda _=False, n=s["name"]: self.open_survey(n))

    def open_survey(self, name: str | None):
        """Show a finished survey (name), or go back to following the newest recording (None). A big
        file takes a while to read (relay C will make it faster); the header says so meanwhile."""
        self.view_file = name
        self.blank = False                          # relay 018: something is open now
        self.blank_msg.hide()
        self.facts.show()
        self.fp.show()
        self.banner.hide()
        self.btn_follow.setText("Follow live")
        self._held, self._held_since = [], None
        self._clear(None)
        self.prog = {}
        self.opening = True
        if name:
            self.tag.setText(name)
            self.facts.setText("Opening… a big survey can take most of a minute to read (relay C will make it faster)")
        self._show_mode()
        self.poller.reset(name)

    # -- relay 018: the menu bar's Live Scan menus
    def build_menus(self, settings, about) -> list:
        """File (open a finished survey, follow live, close), View (marks and colours, layers, sweep height), Tools
        (start a scan, start recording, the radios, stop the run, Settings) and Help."""
        from .chain_screen import Menu
        out = []

        def menu(title):
            m = Menu(self)
            m.setTitle(title)
            out.append(m)
            return m

        f = menu("&File")
        sub = Menu(f)
        sub.setTitle("Open a finished survey")
        sub.aboutToShow.connect(lambda: self._fill_survey_menu(sub))
        f.addMenu(sub)
        a = f.addAction("Follow live (the scan being written now)")
        a.triggered.connect(lambda *_: self.open_survey(None))
        a = f.addAction("Close (back to a blank screen)")
        f.aboutToShow.connect(lambda a=a: a.setEnabled(not self.blank))
        a.setToolTip("Nothing stops: a running scan carries on, and the banner names it")
        a.triggered.connect(lambda *_: self.close_survey())
        f.addSeparator()
        a = f.addAction("Exit")
        a.triggered.connect(lambda *_: self.window().close())

        v = menu("&View")
        marks = self.btn_view.menu()
        marks.setTitle("Marks and waterfall colours")
        v.addAction(marks.menuAction())
        v.add_heading("Layers")
        for key, text in (("noise", "Noise"), ("signal", "Signal over the floor"), ("internal", "Internal")):
            a = v.addAction(text)
            a.setCheckable(True)
            v.aboutToShow.connect(lambda a=a, k=key: a.setChecked(self.ly_boxes[k].isChecked()))
            a.triggered.connect(lambda on, k=key: self.ly_boxes[k].setChecked(on))
        v.add_heading("Sweep height")
        for k, ry in enumerate(SWEEP_HEIGHTS):
            a = v.addAction("Fit every sweep" if ry == "fit" else f"{ry} px per sweep")
            a.setCheckable(True)
            v.aboutToShow.connect(lambda a=a, ry=ry: a.setChecked((self.fit and ry == "fit") or
                                                                  (not self.fit and ry == self.ry)))
            a.triggered.connect(lambda _=False, ry=ry: self.set_height(ry))
        v.addSeparator()
        from . import explain as X
        a = v.addAction("Explanations (the ⓘ marks)")
        a.setCheckable(True)
        v.aboutToShow.connect(lambda a=a: a.setChecked(X.shown()))
        a.triggered.connect(X.set_shown)

        t = menu("&Tools")
        for text, slot in (("Start a scan…", self.open_start), ("Start recording…", self.open_watch),
                           ("Radios…", self.open_radios)):
            a = t.addAction(text)
            a.triggered.connect(lambda *_, s=slot: s())
        a = t.addAction("Stop run…")
        t.aboutToShow.connect(lambda a=a: (a.setEnabled(self.btn_stop.isEnabled()), a.setToolTip(self.btn_stop.toolTip())))
        a.triggered.connect(lambda *_: self.stop_run())
        t.addSeparator()
        a = t.addAction("Settings…")
        a.triggered.connect(lambda *_: settings())

        h = menu("&Help")
        a = h.addAction("About Marsad")
        a.triggered.connect(lambda *_: about())
        return out

    def _fill_survey_menu(self, m):
        m.clear()
        try:
            got = self.svc.surveys()
        except ServiceError as exc:
            a = m.addAction(f"Cannot read the list: {exc}")
            a.setEnabled(False)
            return
        for s in (got.get("surveys") or [])[:60]:
            a = m.addAction(f"{s['name']}   ·   {s.get('mb', 0):g} MB")
            a.setToolTip(f"{s.get('engine')} · {s.get('lo')}–{s.get('hi')} MHz · started {s.get('started')}")
            a.triggered.connect(lambda _=False, n=s["name"]: self.open_survey(n))
        if not m.actions():
            a = m.addAction("No surveys yet")
            a.setEnabled(False)

    # -- starting scans and recorder sessions (relay 010)
    def open_start(self):
        from .scan_start import StartScanDialog
        d = StartScanDialog(self.svc, self)
        d.started.connect(self._launched)
        d.exec()

    def open_watch(self):
        from .scan_start import StartWatchDialog
        d = StartWatchDialog(self.svc, self)
        d.started.connect(self._launched)
        d.exec()

    def _launched(self, out: dict):
        """A run was launched outside the window: follow the newest recording, so the view picks it
        up as soon as it writes its first sweep."""
        self.status.emit(f"Started outside the window (pid {out.get('pid')}); what it prints goes to "
                         f"{out.get('log')}")
        if self.view_file is not None or self.blank:
            self.open_survey(None)

    def mark_seen(self, serial):
        self.recwatch.mark_seen(serial)
        self.rec_views = self.recwatch.update([{k: v for k, v in r.items() if k not in ("view", "silent_s")}
                                               for r in self.rec_views])
        self.rec_panel.show_recorders(self.rec_views)

    def stop_run(self):
        f = self.prog.get("file")
        if not f:
            return
        eng = (self.prog.get("facts") or {}).get("engine") or self.engine       # the page's own wording
        b = QMessageBox.question(self, "Stop run", f"Stop this {eng} run? It stops cleanly (rtl_power_fftw finishes "
                                 "the pass in progress), keeps what it has recorded, then analyses and archives it.")
        if b != QMessageBox.StandardButton.Yes:
            return
        try:
            out = self.svc.stop_scan(f)
            self.status.emit(out.get("note") or "asked the run to stop")
        except ServiceError as exc:
            QMessageBox.information(self, "Not stopped", str(exc))
