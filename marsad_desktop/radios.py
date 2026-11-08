"""The radios, in the window (owner, 2026-11-07).

  RadiosStrip   on Live Scan: one small card per radio, its role and what it is doing now
                (not plugged in, ready, surveying, recording, blocked by another program).
                Click it for the Radios panel.
  RadiosDialog  each radio's facts (serial, tuner, range, blind spots, gains, its chain, the
                station's notes about it), a coverage map of both on one frequency axis with the
                band both can cover, what the station can do right now in words, and who
                surveys and who records, with Swap.
  CoverageMap   that map; also used by the recorder panel to show a survey range against it.

Everything comes from the service's /radios, which learns presence from Windows' own device
list by serial, without opening a radio, so it is safe to ask every few seconds.
"""
from __future__ import annotations

import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QFont, QPainter, QPen
from PySide6.QtWidgets import (QComboBox, QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QScrollArea,
                               QSizePolicy, QToolButton, QVBoxLayout, QWidget)

from marsad_desktop import explain as X
from marsad_desktop import theme as T
from marsad_desktop.service import Service

STATE_COLOUR = {"ready": "open", "surveying": "survey", "recording": "recorder", "blocked": "anomaly",
                "absent": "muted", "unknown": "muted", "disabled": "muted"}
ROLE_COLOUR = {"survey": "survey", "record": "recorder"}
ROLE_WORD = {"survey": "survey radio", "record": "record radio", None: "no role"}


def word(name) -> str:
    """How a radio is named in words: an all-letter name in capitals ("abc" -> "ABC"), any other as written."""
    name = str(name or "")
    return name.upper() if name.isalpha() else name


def jsnum(v) -> str:
    v = float(v)
    return str(int(v)) if v.is_integer() else f"{v:g}"


def call(fn, then):
    from marsad_desktop.scan_start import call as _call      # the shared pool helper
    _call(fn, then)


# ---------------------------------------------------------------- the strip on Live Scan

class Switch(QWidget):
    """A small, quiet on/off switch in the palette (a pill with a knob), not a traffic light."""
    toggled = Signal(bool)

    def __init__(self, on=True):
        super().__init__()
        self.on = on
        self.setFixedSize(26, 14)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_on(self, on: bool):
        if on != self.on:
            self.on = on
            self.update()

    def mousePressEvent(self, ev):
        ev.accept()                                # the card behind it does not open the panel
        self.on = not self.on
        self.update()
        self.toggled.emit(self.on)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        p.setPen(QPen(T.color("lines"), 1))
        p.setBrush(T.color("text2", 0.45) if self.on else T.color("raised"))
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        d = r.height() - 4
        x = r.right() - d - 2 if self.on else r.left() + 2
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(T.color("text") if self.on else T.color("muted"))
        p.drawEllipse(QRectF(x, r.top() + 2, d, d))
        p.end()


class RadioChip(QFrame):
    clicked = Signal()
    enable = Signal(str, bool)

    def __init__(self):
        super().__init__()
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setObjectName("RadioChip")
        h = QHBoxLayout(self)
        h.setContentsMargins(10, 3, 8, 4)
        h.setSpacing(8)
        v = QVBoxLayout()
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self.top = QLabel()
        self.top.setFont(T.ui_font(9, QFont.Weight.DemiBold))
        self.state = QLabel()
        self.state.setFont(T.ui_font(8))
        v.addWidget(self.top)
        v.addWidget(self.state)
        h.addLayout(v)
        self.switch = Switch()
        X.attach(self.switch, "radio_switch")
        self.switch.toggled.connect(lambda on: self.enable.emit(self.name, on))
        h.addWidget(self.switch, 0, Qt.AlignmentFlag.AlignVCenter)
        self.name = ""
        self._key = None

    def show_radio(self, r: dict):
        st = r["doing"]["state"]
        colour = STATE_COLOUR.get(st, "muted")
        role = r.get("role")
        enabled = r.get("enabled", True)
        key = (r["name"], role, st, r["doing"]["text"], enabled)
        if key == self._key:                       # restyled only on a change: the strip refreshes often
            return
        self._key = key
        self.name = r["name"]
        self.switch.set_on(enabled)
        busy = st in ("surveying", "recording")
        self.switch.setToolTip(
            ("On: offered for scans and recording. Switch off to keep it out of new work"
             + (" (what it is doing now keeps going)." if busy else ".")) if enabled else
            "Off (disabled): never offered for a scan or a recording; it keeps its role. Switch on to restore it.")
        rc = T.C[ROLE_COLOUR.get(role, "muted")] if enabled else T.C["muted"]
        name_c = T.C["text"] if enabled else T.C["muted"]
        self.top.setText(f"<span style='color:{name_c}'>{word(r['name'])}</span> "
                         f"<span style='color:{rc}; font-weight:400'>· {ROLE_WORD[role]}</span>")
        short = {"ready": "ready", "absent": "not plugged in", "blocked": "blocked", "unknown": "not known",
                 "surveying": "surveying", "recording": "recording", "disabled": "disabled"}[st]
        if not enabled and busy:
            short += " · disabled for new work"
        self.state.setText(short)
        self.state.setStyleSheet(f"color: {T.C[colour]};")
        self.setStyleSheet(f"#RadioChip {{ background: {T.C['panel']}; border: 1px solid {T.C['lines']}; "
                           f"border-left: 3px solid {T.C[colour]}; border-radius: 6px; }} "
                           f"#RadioChip:hover {{ background: {T.C['raised']}; }} #RadioChip QLabel {{ background: transparent; }}")
        self.setToolTip(f"{r['label']}\nserial {r.get('serial') or '?'} · {r['doing']['text']}\n"
                        f"{ROLE_WORD[role]} · click for the Radios panel")

    def mousePressEvent(self, ev):
        self.clicked.emit()


class RadiosStrip(QWidget):
    """One chip per radio, then Swap. Refreshed by its owner from /radios; asks its owner to swap
    roles or to switch a radio on or off (the owner confirms and calls the service)."""
    open_panel = Signal()
    swap = Signal()
    enable = Signal(str, bool)

    def __init__(self):
        super().__init__()
        self.h = QHBoxLayout(self)
        self.h.setContentsMargins(0, 0, 0, 0)
        self.h.setSpacing(6)
        self.chips: list = []
        self.data: dict = {}
        self.setToolTip("The radios: who surveys, who records, and what each is doing. Click for details.")
        self.swap_btn = QToolButton()
        self.swap_btn.setObjectName("Tool")
        self.swap_btn.setText("⇄ Swap")
        X.attach(self.swap_btn, "swap")
        self.swap_btn.clicked.connect(self.swap.emit)
        self.h.addWidget(self.swap_btn)
        self.swap_btn.hide()

    def show_status(self, d: dict):
        self.data = d
        rs = d.get("radios") or []
        while len(self.chips) < len(rs):
            c = RadioChip()
            c.clicked.connect(self.open_panel.emit)
            c.enable.connect(self.enable.emit)
            self.h.insertWidget(len(self.chips), c)
            self.chips.append(c)
        for i, c in enumerate(self.chips):
            if i < len(rs):
                c.show_radio(rs[i])
                c.show()
            else:
                c.hide()
        self.swap_btn.setVisible(len(rs) >= 2)


# ---------------------------------------------------------------- the coverage map

class CoverageMap(QWidget):
    """Each radio's tunable range on one frequency axis, blind spots hatched, coloured by role; a
    last row for the band both can cover. An optional span (a survey's range) is drawn across."""

    ROW = 22

    def __init__(self):
        super().__init__()
        self.d: dict = {}
        self.span = None
        self.setMinimumHeight(40)

    def show_status(self, d: dict, span=None):
        self.d, self.span = d or {}, span
        rows = len(self.d.get("radios") or []) + (1 if len(self.d.get("radios") or []) >= 2 else 0)
        self.setMinimumHeight(30 + rows * (self.ROW + 6))
        self.update()

    def paintEvent(self, _):
        rs = self.d.get("radios") or []
        if not rs:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setFont(T.ui_font(8))
        top = max(float(r["range_mhz"][1]) for r in rs)
        if self.span:
            top = max(top, float(self.span[1]))
        top = top * 1.03
        left, right = 92, self.width() - 12
        fx = lambda f: left + f / top * (right - left)
        y = 6
        rows = [(word(r["name"]) + "  " + ROLE_WORD[r.get("role")].replace(" radio", ""), r["tunable_mhz"], r["gaps_mhz"],
                 ROLE_COLOUR.get(r.get("role"), "muted"), r) for r in rs]
        if len(rs) >= 2:
            rows.append(("both", self.d.get("both_cover_mhz") or [], [], "open", None))
        for name, spans, gaps, colour, r in rows:
            p.setPen(T.color("text2") if r is None or r.get("present") is not False else T.color("muted"))
            p.drawText(QRectF(4, y, left - 10, self.ROW), Qt.AlignmentFlag.AlignVCenter, name)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(T.color("raised"))
            p.drawRoundedRect(QRectF(left, y + 3, right - left, self.ROW - 6), 3, 3)
            alpha = 0.35 if (r is not None and r.get("present") is False) else 0.75
            for a, b in spans:
                p.setBrush(T.color(colour, alpha))
                p.drawRect(QRectF(fx(a), y + 3, max(1.0, fx(b) - fx(a)), self.ROW - 6))
            for a, b in gaps:
                p.setBrush(QBrush(T.color("alert", 0.8), Qt.BrushStyle.BDiagPattern))
                p.drawRect(QRectF(fx(float(a)), y + 3, max(2.0, fx(float(b)) - fx(float(a))), self.ROW - 6))
            y += self.ROW + 6
        if self.span:
            a, b = self.span
            p.setPen(QPen(T.color("text"), 1.2, Qt.PenStyle.DashLine))
            p.setBrush(T.color("text", 0.07))
            p.drawRect(QRectF(fx(a), 3, max(2.0, fx(b) - fx(a)), y - 6))
        p.setPen(T.color("muted"))
        step = 250 if top < 2000 else 500
        f = 0
        while f <= top:
            x = fx(f)
            p.drawLine(QPointF(x, y - 2), QPointF(x, y + 2))
            p.drawText(QRectF(x - 30, y + 2, 60, 14), Qt.AlignmentFlag.AlignHCenter, f"{f}")
            f += step
        p.drawText(QRectF(right - 40, y + 2, 40, 14), Qt.AlignmentFlag.AlignRight, "MHz")
        p.end()


# ---------------------------------------------------------------- the panel

def _label(text, muted=False, size=None, bold=False, wrap=True):
    w = QLabel(text)
    if muted:
        w.setProperty("muted", True)
    if size or bold:
        w.setFont(T.ui_font(size or 10, QFont.Weight.DemiBold if bold else None))
    w.setWordWrap(wrap)
    w.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return w


class RadioCard(QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName("TextPanel")
        self.v = QVBoxLayout(self)
        self.v.setContentsMargins(T.SPACE * 1.5, T.SPACE * 1.25, T.SPACE * 1.5, T.SPACE * 1.5)
        self.v.setSpacing(6)
        self._key = None

    def show_radio(self, r: dict):
        key = repr(r)
        if key == self._key:
            return
        self._key = key
        while self.v.count():
            it = self.v.takeAt(0)
            w = it.widget()
            if w is not None:
                w.hide()
                w.setParent(None)
                w.deleteLater()
        role = r.get("role")
        st = r["doing"]["state"]
        head = _label(f"{word(r['name'])} <span style='color:{T.C[ROLE_COLOUR.get(role, 'muted')]}; font-size:10pt'>"
                      f"· {ROLE_WORD[role]}</span>", size=13, bold=True)
        head.setTextFormat(Qt.TextFormat.RichText)
        self.v.addWidget(head)
        self.v.addWidget(_label(r["label"], muted=True))
        now = _label(f"<span style='color:{T.C[STATE_COLOUR.get(st, 'muted')]}; font-weight:600'>Now</span>&nbsp; "
                     f"{r['doing']['text']}")
        now.setTextFormat(Qt.TextFormat.RichText)
        self.v.addWidget(now)
        g = QGridLayout()
        g.setHorizontalSpacing(T.SPACE * 1.5)
        g.setVerticalSpacing(3)
        present = {True: f"plugged in ({r.get('usb_name') or 'by serial'})", False: "not plugged in",
                   None: "not known"}[r.get("present")]
        if not r.get("enabled", True):
            self.v.addWidget(_label("Disabled in this window: never offered for a scan or a recording. Its role is "
                                    "kept. The switch on its card on Live Scan turns it back on.", muted=True))
        rows = [("Serial", (r.get("serial") or "not in station.json") + f" · {present}", "presence"),
                ("Tuner", r.get("tuner") or "?", "radio_range"),
                ("Can tune", "; ".join(r.get("facts") or []), "pll_gap"),
                ("Gain", r.get("gain_text") or "", "gain")]
        for i, (k, v, key) in enumerate(rows):
            g.addWidget(X.titled(k, key, rx=r["name"]), i, 0, Qt.AlignmentFlag.AlignTop)
            g.addWidget(_label(v), i, 1)
        g.setColumnStretch(1, 1)
        self.v.addLayout(g)
        ch = r.get("chain") or {}
        self.v.addWidget(X.titled("Chain this radio records", "radio_chain"))
        c = _label(ch.get("text") or ch.get("problem") or "(none)")
        c.setFont(T.mono_font(8))
        self.v.addWidget(c)
        if ch.get("source"):
            self.v.addWidget(_label(f"from {ch['source']}" + (" · already used by a run: Start asks \"still built as "
                                                              "drawn?\"" if ch.get("due") else ""), muted=True))
        notes = r.get("notes") or []
        if notes:
            self.v.addWidget(X.titled(f"What the station knows about it ({len(notes)})", "radio_notes"))
            for n in notes:
                colour = {"bad": "alert", "warn": "anomaly", "ok": "open"}.get(n["level"], "text2")
                w = _label(f"<span style='color:{T.C[colour]}; font-weight:600'>{n['title']}</span>"
                           + (f" <span style='color:{T.C['muted']}'>({n['where']})</span>" if n.get("where") else "")
                           + f"<br><span style='color:{T.C['text2']}'>{n['text']}</span>")
                w.setTextFormat(Qt.TextFormat.RichText)
                self.v.addWidget(w)
        self.v.addStretch(1)


class RadiosDialog(QDialog):
    """The radios: what each is, what it is doing, who surveys and who records."""
    changed = Signal(dict)

    def __init__(self, svc: Service, parent=None):
        super().__init__(parent)
        self.svc = svc
        self.setWindowTitle("Radios")
        self.resize(1180, 820)
        self.d: dict = {}
        outer = QVBoxLayout(self)
        outer.setContentsMargins(T.SPACE * 2, T.SPACE * 1.5, T.SPACE * 2, T.SPACE * 1.5)
        outer.setSpacing(T.SPACE * 1.25)
        h = QHBoxLayout()
        h.addWidget(_label("Radios", size=13, bold=True, wrap=False))
        h.addStretch(1)
        self.checked = _label("", muted=True, wrap=False)
        h.addWidget(self.checked)
        outer.addLayout(h)
        self.mode = _label("", size=10.5)
        X.attach(self.mode, "radios_mode")
        mh = QHBoxLayout()
        mh.addWidget(self.mode, 1)
        mh.addWidget(X.info("radios_mode"), 0, Qt.AlignmentFlag.AlignTop)
        outer.addLayout(mh)
        roles = QFrame()
        roles.setObjectName("TextPanel")
        rh = QHBoxLayout(roles)
        rh.setContentsMargins(T.SPACE * 1.5, T.SPACE, T.SPACE * 1.5, T.SPACE)
        rh.addWidget(_label("Survey + record:", bold=True, wrap=False))
        rh.addWidget(X.info("roles"))
        rh.addWidget(_label("surveys", muted=True, wrap=False))
        self.sur = QComboBox()
        rh.addWidget(self.sur)
        rh.addWidget(_label("records", muted=True, wrap=False))
        self.rec = QComboBox()
        rh.addWidget(self.rec)
        self.swap = QToolButton()
        self.swap.setObjectName("Tool")
        self.swap.setText("⇄ Swap")
        X.attach(self.swap, "swap")
        self.swap.clicked.connect(lambda: self._set({"swap": True}))
        rh.addWidget(self.swap)
        rh.addStretch(1)
        self.roles_note = _label("", muted=True)
        rh.addWidget(self.roles_note, 2)
        outer.addWidget(roles)
        self.sur.activated.connect(lambda _i: self._pick("surveyor"))
        self.rec.activated.connect(lambda _i: self._pick("recorder"))
        cov = QFrame()
        cov.setObjectName("TextPanel")
        cv = QVBoxLayout(cov)
        cv.setContentsMargins(T.SPACE * 1.5, T.SPACE, T.SPACE * 1.5, T.SPACE)
        ct = QHBoxLayout()
        ct.addWidget(_label("What each radio can tune", bold=True, wrap=False))
        ct.addWidget(X.info("coverage_map"))
        ct.addStretch(1)
        cv.addLayout(ct)
        self.map = CoverageMap()
        cv.addWidget(self.map)
        self.cov_note = _label("", muted=True)
        cv.addWidget(self.cov_note)
        outer.addWidget(cov)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        self.cards_box = QHBoxLayout(inner)
        self.cards_box.setContentsMargins(0, 0, 0, 0)
        self.cards_box.setSpacing(T.SPACE * 1.25)
        self.cards: list = []
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)
        self.foot = _label("", muted=True)
        outer.addWidget(self.foot)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(3000)
        QTimer.singleShot(0, self.refresh)

    def refresh(self):
        call(self.svc.radios, lambda d, err: self.show_status(d) if not err else self.mode.setText(
            f"The service could not say: {err}"))

    def show_status(self, d: dict):
        self.d = d
        self.checked.setText("checked " + time.strftime("%H:%M:%S", time.localtime(d.get("checked") or time.time())))
        self.mode.setText(d["mode"]["text"])
        rs = d.get("radios") or []
        roles = d.get("roles") or {}
        for cb, key in ((self.sur, "surveyor"), (self.rec, "recorder")):
            cb.blockSignals(True)
            cb.clear()
            for r in rs:
                cb.addItem(word(r["name"]), r["name"])
            cb.setCurrentIndex(max(0, cb.findData(roles.get(key))))
            cb.blockSignals(False)
        two = len(rs) >= 2
        for w in (self.sur, self.rec, self.swap):
            w.setEnabled(two)
        self.roles_note.setText(
            ("Only used when both radios run together; a plain scan uses the radio you pick for it. "
             + ("Both are plugged in." if d["mode"].get("survey_record") else
                "Right now survey + record is not possible: both radios must be plugged in."))
            if two else "Only one radio is configured in station.json.")
        self.map.show_status(d)
        for r in rs:
            X.fetch(self.svc, r["name"])
        both = d.get("both_cover_mhz") or []
        self.cov_note.setText(
            ("Both can tune " + ", ".join(f"{jsnum(a)}–{jsnum(b)}" for a, b in both) + " MHz: in survey + record, "
             "only signals found there can be recorded. Hatched: a radio's blind spot (PLL gap).")
            if two else "Hatched: a blind spot (PLL gap).")
        while len(self.cards) < len(rs):
            c = RadioCard()
            self.cards_box.addWidget(c, 1)
            self.cards.append(c)
        for i, c in enumerate(self.cards):
            c.setVisible(i < len(rs))
            if i < len(rs):
                c.show_radio(rs[i])
        prog = ", ".join(d.get("programs") or []) or "none"
        self.foot.setText(f"Plugged in or not: {d.get('presence_source') or 'unknown'}. Radio programs running: {prog}. "
                          "The USB index is still found by serial when a run starts.")
        self.changed.emit(d)

    def _pick(self, which):
        sur, rec = self.sur.currentData(), self.rec.currentData()
        if sur == rec:                       # picking the other radio for one role moves the first to the other
            other = next((r["name"] for r in self.d.get("radios") or [] if r["name"] != sur), None)
            if which == "surveyor":
                rec = other
            else:
                sur = next((r["name"] for r in self.d.get("radios") or [] if r["name"] != rec), None)
        self._set({"surveyor": sur, "recorder": rec})

    def _set(self, body):
        call(lambda: self.svc.set_roles(body), lambda d, err: self.show_status(d) if not err else self.mode.setText(err))
