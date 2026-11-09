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
from PySide6.QtGui import QBrush, QFont, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import (QComboBox, QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QScrollArea,
                               QSizePolicy, QToolButton, QVBoxLayout, QWidget)

from marsad_desktop import explain as X
from marsad_desktop import practice
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
        self.r: dict | None = None
        self.rec: dict | None = None               # relay 019: this radio's recorder session, its view (rule 9)
        self._pressed = False

    def set_recorder(self, view: dict | None):
        """The recorder state shown on the card: running, starting, silent N min (the alert colour) or stopped,
        for the session shown in the recorder list. Never hidden by closing or minimising that list."""
        if view == self.rec:
            return
        self.rec = view
        if self.r is not None:
            self._key = None
            self.show_radio(self.r)

    def show_radio(self, r: dict):
        st = r["doing"]["state"]
        colour = STATE_COLOUR.get(st, "muted")
        role = r.get("role")
        enabled = r.get("enabled", True)
        rv = self.rec or {}
        key = (r["name"], role, st, r["doing"]["text"], enabled, r["doing"].get("other_busy"), rv.get("view"),
               int(float(rv.get("silent_s") or 0) // 60))
        self.r = r
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
        simulated = practice.on() and st in ("absent", "unknown", "ready")
        if simulated:                                  # relay 014: the practice station's made-up radios
            short, colour = practice.SIMULATED, "muted"
        if st == "ready" and r["doing"].get("other_busy"):    # relay 012: free while the other radio works
            short = "ready, other radio busy"
        if not enabled and busy:
            short += " · disabled for new work"
        view = rv.get("view")
        if view in ("running", "starting"):
            short, colour = f"recorder {view}", "recorder"
        elif view == "silent":                      # rule 9: a recorder that stopped reporting looks stopped
            from marsad_desktop.live_data import silent_text
            short, colour = f"recorder {silent_text(rv.get('silent_s') or 0)}", "alert"
        elif view in ("stopped", "seen") and st not in ("surveying", "recording"):
            import datetime as _dt
            end = rv.get("stopped") or rv.get("updated")
            when = _dt.datetime.fromtimestamp(float(end)).strftime("%H:%M") if end else ""
            short += f" · recorder stopped {when}".rstrip()
        self.state.setText(short)
        self.state.setStyleSheet(f"color: {T.C[colour]};")
        # relay 016: a raised card, lit from above, its state colour down the left edge
        self.setStyleSheet(f"#RadioChip {{ background: {T.gradient(T.C['seg_top'], T.C['seg_bottom'])}; "
                           f"{T.lit_edges('raised')} border-left: 3px solid {T.C[colour]}; border-radius: 6px; }} "
                           f"#RadioChip:hover {{ background: {T.gradient(T.C['btn_hover_top'], T.C['btn_hover_bottom'])}; }} "
                           f"#RadioChip QLabel {{ background: transparent; }}")
        self.setToolTip(f"{r['label']}\nserial {r.get('serial') or '?'} · {r['doing']['text']}\n"
                        f"{ROLE_WORD[role]} · click for the Radios panel")

    # relay 019: the panel opens on the release, in one step. (It opened on the press, while the card was still held
    # down, as an empty dialog that then filled itself in: the flicker before the panel settled.)
    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self._pressed = True
        ev.accept()

    def mouseReleaseEvent(self, ev):
        was, self._pressed = self._pressed, False
        if was and ev.button() == Qt.MouseButton.LeftButton and self.rect().contains(ev.position().toPoint()):
            self.clicked.emit()
        ev.accept()


class RadiosStrip(QWidget):
    """One chip per radio, then Swap. Refreshed by its owner from /radios; asks its owner to swap
    roles or to switch a radio on or off (the owner confirms and calls the service)."""
    open_panel = Signal()
    swap = Signal()
    enable = Signal(str, bool)
    set_setup = Signal(str)            # relay 019: a setup chosen from the strip's Setup ▾

    def __init__(self):
        super().__init__()
        self.h = QHBoxLayout(self)
        self.h.setContentsMargins(0, 0, 0, 0)
        self.h.setSpacing(6)
        self.chips: list = []
        self.data: dict = {}
        self.recs: dict = {}
        self.setToolTip("The radios: who surveys, who records, and what each is doing. Click for details.")
        self.swap_btn = QToolButton()
        self.swap_btn.setObjectName("Tool")
        self.swap_btn.setText("⇄ Swap")
        X.attach(self.swap_btn, "swap")
        self.swap_btn.clicked.connect(self.swap.emit)
        self.h.addWidget(self.swap_btn)
        self.swap_btn.hide()
        # relay 019: the setup for the two radios, beside Swap
        self.setup_btn = QToolButton()
        self.setup_btn.setObjectName("Tool")
        self.setup_btn.setText("Setup ▾")
        X.attach(self.setup_btn, "setup")
        self.setup_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        from marsad_desktop.chain_screen import Menu
        self.setup_menu = Menu(self)
        self.setup_menu.aboutToShow.connect(self._fill_setup)
        self.setup_btn.setMenu(self.setup_menu)
        self.h.addWidget(self.setup_btn)

    def _fill_setup(self):
        from marsad_desktop import setups as SU
        m = self.setup_menu
        m.clear()
        d = self.data or {}
        cur = SU.chosen(d)
        av = SU.availability(d)
        m.add_heading("Setup for the radios")
        for s in SU.ORDER:
            a = m.addAction(SU.TITLES[s])
            a.setCheckable(True)
            a.setChecked(s == cur)
            info = av[s]
            a.setEnabled(info["ok"])
            tip = SU.whatif_texts().get(s, {}).get("tip", "")
            a.setToolTip(tip if info["ok"] else f"{tip}\nNot available now: {info['why']}")
            if not info["ok"]:
                a.setText(f"{SU.TITLES[s]}  ·  {'not supported yet' if not info['supported'] else 'not available'}")
            a.triggered.connect(lambda _=False, s=s: self.set_setup.emit(s))
        m.addSeparator()
        a = m.addAction("What if… (the Radios panel)")
        a.triggered.connect(lambda *_: self.open_panel.emit())

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
                c.rec = self.recs.get(str(rs[i].get("serial") or ""))
                c.show_radio(rs[i])
                c.show()
            else:
                c.hide()
        self.swap_btn.setVisible(len(rs) >= 2)
        from marsad_desktop import setups as SU
        self.setup_btn.setText(SU.TITLES[SU.chosen(d)] + " ▾")
        self.setup_btn.setVisible(bool(rs))

    def set_recorders(self, views: list):
        """Relay 019: each radio card shows its recorder's state for the session the recorder list shows (the one
        running now, else the most recent): running, starting, silent N min, or stopped."""
        from marsad_desktop.live_data import pick_session
        sel = pick_session(views)
        shown = sel["current"] or ([sel["last"]] if sel["last"] else [])
        self.recs = {str(v.get("serial") or ""): v for v in shown}
        rs = (self.data or {}).get("radios") or []
        for i, c in enumerate(self.chips):
            if i < len(rs):
                c.set_recorder(self.recs.get(str(rs[i].get("serial") or "")))


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
        doing = r["doing"]["text"]
        if practice.on() and st in ("absent", "unknown", "ready"):
            doing = practice.SIMULATED + ": a made-up radio on the practice station; nothing is plugged in"
        now = _label(f"<span style='color:{T.C[STATE_COLOUR.get(st, 'muted')]}; font-weight:600'>Now</span>&nbsp; "
                     f"{doing}")
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
        # relay 012: short on screen; the full text on hover, and Copy puts it on the clipboard
        short = (ch.get("summary") or {}).get("short") or ch.get("problem") or "(none)"
        row = QHBoxLayout()
        c = _label(short)
        c.setToolTip(ch.get("text") or "")
        row.addWidget(c, 1)
        if ch.get("text"):
            cp = QToolButton()
            cp.setObjectName("Tool")
            cp.setText("Copy chain")
            cp.setToolTip("Copy the full chain text this radio's runs record:\n" + ch["text"])
            cp.clicked.connect(lambda *_, t=ch["text"]: QGuiApplication.clipboard().setText(t))
            row.addWidget(cp, 0, Qt.AlignmentFlag.AlignTop)
        self.v.addLayout(row)
        if ch.get("source"):
            self.v.addWidget(_label(f"from {ch['source']}" + (" · already used by a run: Start asks \"still built as "
                                                              "drawn?\"" if ch.get("due") else ""), muted=True))
        notes = r.get("notes") or []
        if notes:
            # relay 019: a clear, larger section title, so the eye goes to it first
            t = X.titled(f"What the station knows about it ({len(notes)})", "radio_notes", muted=False)
            t.setObjectName("KnowsTitle")
            for lab in [t] + t.findChildren(QLabel):
                if isinstance(lab, QLabel):
                    lab.setFont(T.ui_font(12, QFont.Weight.DemiBold))
                    lab.setStyleSheet(f"color: {T.C['survey_light']};")
            t.setContentsMargins(0, T.SPACE, 0, 2)
            self.v.addWidget(t)
            for n in notes:
                colour = {"bad": "alert", "warn": "anomaly", "ok": "open"}.get(n["level"], "text2")
                w = _label(f"<span style='color:{T.C[colour]}; font-weight:600'>{n['title']}</span>"
                           + (f" <span style='color:{T.C['muted']}'>({n['where']})</span>" if n.get("where") else "")
                           + f"<br><span style='color:{T.C['text2']}'>{n['text']}</span>")
                w.setTextFormat(Qt.TextFormat.RichText)
                self.v.addWidget(w)
        self.v.addStretch(1)


class RadiosDialog(QDialog):
    """The radios: what each is, what it is doing, what the station can do with the radios enabled now, and the
    setup for the two (relay 019: survey + record either way round, both survey, both record, one radio alone), with
    each setup's "What if". Opened with the strip's last answer, so it shows complete at once (relay 019: no empty
    panel that then fills itself in)."""
    changed = Signal(dict)

    def __init__(self, svc: Service, parent=None, initial: dict | None = None):
        super().__init__(parent)
        self.svc = svc
        self.setWindowTitle("Radios")
        self.resize(1180, 860)
        self.d: dict = {}
        self.rig = None                           # the rig used for runs: the splitter's figure for What if
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
        self.mode.setObjectName("RadiosPossible")
        X.attach(self.mode, "radios_mode")
        mh = QHBoxLayout()
        mh.addWidget(self.mode, 1)
        mh.addWidget(X.info("radios_mode"), 0, Qt.AlignmentFlag.AlignTop)
        outer.addLayout(mh)

        # relay 019: the setup, and its What if
        setup = QFrame()
        setup.setObjectName("TextPanel")
        sv = QVBoxLayout(setup)
        sv.setContentsMargins(T.SPACE * 1.5, T.SPACE, T.SPACE * 1.5, T.SPACE)
        sv.setSpacing(T.SPACE // 2 + 2)
        st = QHBoxLayout()
        st.addWidget(_label("Setup", bold=True, size=11, wrap=False))
        st.addWidget(X.info("setup"))
        st.addSpacing(T.SPACE)
        from marsad_desktop import setups as SU
        self.setup_btns = {}
        from PySide6.QtWidgets import QButtonGroup, QPushButton
        grp = QButtonGroup(self)
        grp.setExclusive(True)
        for s in SU.ORDER:
            b = QPushButton(SU.TITLES[s])
            b.setObjectName("Setup_" + s)
            b.setCheckable(True)
            b.setProperty("accent", True)
            b.clicked.connect(lambda _=False, s=s: self._choose(s))
            grp.addButton(b)
            st.addWidget(b)
            self.setup_btns[s] = b
        st.addStretch(1)
        sv.addLayout(st)
        self.setup_why = _label("", muted=True)
        self.setup_why.setObjectName("SetupWhy")
        sv.addWidget(self.setup_why)
        roles = QHBoxLayout()
        self.roles_lbl = _label("Survey + record:", wrap=False)
        roles.addWidget(self.roles_lbl)
        self.roles_info = X.info("roles")
        roles.addWidget(self.roles_info)
        self.sur_lbl = _label("surveys", muted=True, wrap=False)
        roles.addWidget(self.sur_lbl)
        self.sur = QComboBox()
        roles.addWidget(self.sur)
        self.rec_lbl = _label("records", muted=True, wrap=False)
        roles.addWidget(self.rec_lbl)
        self.rec = QComboBox()
        roles.addWidget(self.rec)
        self.swap = QToolButton()
        self.swap.setObjectName("Tool")
        self.swap.setText("⇄ Swap")
        X.attach(self.swap, "swap")
        self.swap.clicked.connect(lambda: self._set({"swap": True}))
        roles.addWidget(self.swap)
        self.one_lbl = _label("the radio", muted=True, wrap=False)
        roles.addWidget(self.one_lbl)
        self.one = QComboBox()
        self.one.setObjectName("OneRadio")
        roles.addWidget(self.one)
        roles.addStretch(1)
        self.roles_note = _label("", muted=True)
        roles.addWidget(self.roles_note, 2)
        sv.addLayout(roles)
        self.sur.activated.connect(lambda _i: self._pick("surveyor"))
        self.rec.activated.connect(lambda _i: self._pick("recorder"))
        self.one.activated.connect(lambda _i: self._set({"one": self.one.currentData()}))
        self.whatif = _label("")
        self.whatif.setObjectName("WhatIf")
        self.whatif.setTextFormat(Qt.TextFormat.RichText)
        sv.addWidget(self.whatif)
        outer.addWidget(setup)

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
        if initial:                          # the strip's last answer: the panel shows complete at once
            self.show_status(initial)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(3000)
        QTimer.singleShot(0, self.refresh)
        call(lambda: svc.chains(), self._got_rig)

    def _got_rig(self, d, err):
        if err:
            return
        from marsad_desktop import rig as R
        self.rig = R.used_rig(d)[1]
        if self.d:
            self._show_setup(self.d)

    def refresh(self):
        call(self.svc.radios, lambda d, err: self.show_status(d) if not err else self.mode.setText(
            f"The service could not say: {err}"))

    def show_status(self, d: dict):
        from marsad_desktop import setups as SU
        self.d = d
        self.checked.setText("checked " + time.strftime("%H:%M:%S", time.localtime(d.get("checked") or time.time())))
        # relay 019: the top says what is possible with the radios enabled now
        self.mode.setText(SU.possible_text(d) + ((" (Practice station: the radios here are made up, simulated.)")
                                                 if practice.on() else ""))
        rs = d.get("radios") or []
        roles = d.get("roles") or {}
        for cb, key in ((self.sur, "surveyor"), (self.rec, "recorder")):
            cb.blockSignals(True)
            cb.clear()
            for r in rs:
                cb.addItem(word(r["name"]), r["name"])
            cb.setCurrentIndex(max(0, cb.findData(roles.get(key))))
            cb.blockSignals(False)
        self.one.blockSignals(True)
        self.one.clear()
        for r in rs:
            self.one.addItem(word(r["name"]) + ("" if r.get("enabled", True) else "  (disabled)"), r["name"])
        one = roles.get("one") or (SU.usable(d)[0]["name"] if SU.usable(d) else (rs[0]["name"] if rs else None))
        self.one.setCurrentIndex(max(0, self.one.findData(one)))
        self.one.blockSignals(False)
        self._show_setup(d)
        self.map.show_status(d)
        for r in rs:
            X.fetch(self.svc, r["name"])
        two = len(rs) >= 2
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

    def _show_setup(self, d: dict):
        """The setup buttons (one checked; one not possible now greyed, saying why), the roles or the one radio it
        uses, and its What if."""
        from marsad_desktop import setups as SU
        cur = SU.chosen(d)
        av = SU.availability(d)
        for s, b in self.setup_btns.items():
            info = av[s]
            b.setChecked(s == cur)
            b.setEnabled(info["ok"] or s == cur)
            tip = SU.whatif_texts().get(s, {}).get("tip", "")
            b.setToolTip(tip + ("" if info["ok"] else f"\n\n{'Not supported yet' if not info['supported'] else 'Not available now'}: {info['why']}"))
        why = av[cur]["why"]
        off = [f"{SU.TITLES[s]}: {av[s]['why']}" for s in SU.ORDER if not av[s]["ok"] and s != cur]
        self.setup_why.setText((f"{SU.TITLES[cur]} is chosen but not possible now: {why}. " if why else "")
                               + ("Not available now: " + "; ".join(off) + "." if off else ""))
        self.setup_why.setVisible(bool(why or off))
        rs = d.get("radios") or []
        pair = cur in ("survey_record",)
        for w in (self.roles_lbl, self.sur_lbl, self.sur, self.rec_lbl, self.rec, self.swap):
            w.setVisible(pair)
        self.roles_info.setVisible(pair and X.shown())
        self.roles_lbl.setText("Survey + record:")
        for w in (self.one_lbl, self.one):
            w.setVisible(cur in ("one_survey", "one_record"))
        self.one_lbl.setText("surveys with" if cur == "one_survey" else "records with")
        self.sur.setEnabled(len(rs) >= 2)
        self.rec.setEnabled(len(rs) >= 2)
        self.swap.setEnabled(len(rs) >= 2)
        self.roles_note.setText(
            "Survey + record needs both radios enabled and plugged in now: " + why if (pair and why) else
            ("Each scan or recording is started on its own radio; the setup starts nothing." if cur in
             ("both_survey", "both_record") else "A setup starts nothing: the Start panels follow it."))
        w = SU.whatif(cur, d, self.rig)
        html = [f"<p style='margin:6px 0 2px 0'><b>What if: {w['title']}</b>"
                f" <span style='color:{T.C['muted']}'>· {w['tip']}</span></p>"]
        for title, text in w["sections"]:
            html.append(f"<div style='margin:2px 0'><span style='color:{T.C['survey_light']}; font-weight:600'>"
                        f"{title}:</span> {text}</div>")
        if w["facts"]:
            html.append(f"<div style='margin:4px 0 0 0'><span style='color:{T.C['survey_light']}; font-weight:600'>"
                        f"This station:</span> <span style='color:{T.C['text2']}'>{' '.join(w['facts'])}</span></div>")
        self.whatif.setText("".join(html))

    def _choose(self, setup: str):
        from marsad_desktop import setups as SU
        if not SU.availability(self.d).get(setup, {}).get("ok") and setup != SU.chosen(self.d):
            self._show_setup(self.d)
            return
        body = {"setup": setup}
        if setup in ("one_survey", "one_record") and self.one.currentData():
            body["one"] = self.one.currentData()
        self._set(body)

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
