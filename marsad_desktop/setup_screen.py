"""Relay 021: New Station, the first run, Add Radio, and "Set up this station".

  NewStationDialog   name, description, where to keep it (Documents\\Marsad\\Stations\\<name> by default) and an
                     optional location that is written only to that station's own station.local.json and never shown
                     again: the window says "set" or "not set".
  FirstRunDialog     the first time the window has no station: New Station, or open an existing one.
  SetupDialog        each radio's checklist (add, measure, draw, confirm, path checks), each step's state and a button
                     to do it. Each measurement says what it does, what to connect, how long and what to expect before
                     it runs, offers a short test first, and keeps every result with its date.
  AddRadioDialog     the RTL-SDRs Windows sees, by serial; the tuner found by the engine's own probe once the owner
                     says go (it opens the radio for a moment); the tuner's typical figures marked "typical, not yet
                     measured"; a nickname. Saved into station.json the way the engine reads it.

Everything goes through the station's service; nothing here touches another station.
"""
from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton, QScrollArea,
                               QToolButton, QVBoxLayout, QWidget)

from marsad_desktop import theme as T
from marsad_desktop.scan_start import call

STATE_COLOUR = {"done": "open", "in progress": "survey_light", "needs attention": "anomaly", "not started": "muted"}
STATE_MARK = {"done": "✓", "in progress": "…", "needs attention": "!", "not started": "○"}


def _label(text, muted=False, size=None, bold=False, wrap=True):
    w = QLabel(text)
    if muted:
        w.setProperty("muted", True)
    if size or bold:
        w.setFont(T.ui_font(size or 10, QFont.Weight.DemiBold if bold else None))
    w.setWordWrap(wrap)
    w.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return w


def _button(text, primary=False):
    b = QPushButton(text)
    if primary:
        b.setObjectName("Primary")
    return b


def _float(text):
    try:
        return float(str(text).strip())
    except ValueError:
        return None


# ---------------------------------------------------------------- New Station

class LocationBox(QFrame):
    """Optional, private. Typed once; never shown again."""

    def __init__(self, note_set: str | None = None):
        super().__init__()
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        self.on = QCheckBox("Add location for outdoor weather and sun height")
        v.addWidget(self.on)
        v.addWidget(_label("Private: kept only on this PC, in this station's own station.local.json, used internally, "
                           "never shown or exported. You can skip it.", muted=True))
        row = QHBoxLayout()
        self.lat = QLineEdit()
        self.lat.setPlaceholderText("latitude, decimal degrees")
        self.lon = QLineEdit()
        self.lon.setPlaceholderText("longitude, decimal degrees, west negative")
        for w in (self.lat, self.lon):
            row.addWidget(w)
        v.addLayout(row)
        if note_set is not None:
            v.addWidget(_label(note_set, muted=True))
        self.on.toggled.connect(self._enable)
        self._enable(False)

    def _enable(self, on):
        self.lat.setEnabled(on)
        self.lon.setEnabled(on)

    def value(self):
        """None (skipped), or {"lat", "lon"}; raises ValueError in words."""
        if not self.on.isChecked():
            return None
        lat, lon = _float(self.lat.text()), _float(self.lon.text())
        if lat is None or lon is None:
            raise ValueError("Type both latitude and longitude in decimal degrees, or untick the location.")
        return {"lat": lat, "lon": lon}

    def clear(self):
        self.lat.clear()
        self.lon.clear()


class NewStationDialog(QDialog):
    """File > New Station…: a clean station from the engine's own `marsad init`, nothing copied from another."""

    def __init__(self, parent=None, documents: Path | None = None):
        super().__init__(parent)
        from marsad import setup as S
        self.S = S
        self.documents = documents
        self.setWindowTitle("New Station")
        self.setObjectName("NewStationDialog")
        self.folder_chosen: Path | None = None
        self.created: dict | None = None
        v = QVBoxLayout(self)
        v.setContentsMargins(T.SPACE * 3, T.SPACE * 2, T.SPACE * 3, T.SPACE * 2)
        v.setSpacing(T.SPACE)
        v.addWidget(_label("New Station", size=13, bold=True))
        v.addWidget(_label("A new station starts empty: no radios, no rigs, no saved scans. Nothing is copied from "
                           "another station, and nothing done here changes one.", muted=True))
        g = QGridLayout()
        g.addWidget(_label("Station name", wrap=False), 0, 0)
        self.name = QLineEdit()
        self.name.setPlaceholderText("required")
        g.addWidget(self.name, 0, 1)
        g.addWidget(_label("Description", wrap=False), 1, 0)
        self.desc = QLineEdit()
        self.desc.setPlaceholderText("optional")
        g.addWidget(self.desc, 1, 1)
        g.addWidget(_label("Where to keep it", wrap=False), 2, 0)
        fr = QHBoxLayout()
        self.folder = _label("", wrap=True)
        fr.addWidget(self.folder, 1)
        ch = _button("Change…")
        ch.clicked.connect(self._pick)
        fr.addWidget(ch)
        g.addLayout(fr, 2, 1)
        v.addLayout(g)
        self.loc = LocationBox()
        v.addWidget(self.loc)
        self.why = _label("", muted=True)
        self.why.setObjectName("NewStationWhy")
        v.addWidget(self.why)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = _button("Cancel")
        cancel.clicked.connect(self.reject)
        self.ok = _button("Create station", primary=True)
        self.ok.clicked.connect(self.create)
        row.addWidget(cancel)
        row.addWidget(self.ok)
        v.addLayout(row)
        self.name.textChanged.connect(self._update)
        self.resize(620, self.sizeHint().height())
        self._update()

    def target(self) -> Path:
        return self.folder_chosen or self.S.default_folder(self.name.text().strip() or "station", self.documents)

    def _pick(self):
        d = QFileDialog.getExistingDirectory(self, "Where to keep the station (a new or empty folder)",
                                             str(self.target().parent))
        if d:
            self.folder_chosen = Path(d)
            self._update()

    def _update(self, *_):
        t = self.target()
        self.folder.setText(str(t) + ("" if self.folder_chosen else "   (default)"))
        problem = None if not self.name.text().strip() else self.S.folder_problem(t)
        self.why.setText(problem or ("" if self.name.text().strip() else "Give the station a name."))
        self.ok.setEnabled(bool(self.name.text().strip()) and not problem)

    def create(self):
        try:
            loc = self.loc.value()
            self.created = self.S.create_station(self.target(), self.name.text(), self.desc.text(), loc)
        except (ValueError, OSError) as exc:
            self.why.setText(str(exc))
            return
        self.loc.clear()                    # typed once, never kept in the window
        self.accept()


def pick_existing_station(parent=None, start: str | None = None) -> str | None:
    """Open an existing station: any folder holding a station.json."""
    d = QFileDialog.getExistingDirectory(parent, "Open an existing station (its folder holds station.json)",
                                         start or str(Path.home()))
    if not d:
        return None
    if not (Path(d) / "station.json").exists():
        QMessageBox.information(parent, "Not a station", f"{d} has no station.json, so it is not a station. Pick the "
                                "station's own folder, or make a new one.")
        return None
    return d


class FirstRunDialog(QDialog):
    """The first time the window has no station at all."""

    def __init__(self, parent=None, home: str | None = None):
        super().__init__(parent)
        self.setWindowTitle("Welcome to Marsad")
        self.setObjectName("FirstRunDialog")
        self.choice = None
        self.folder = None
        v = QVBoxLayout(self)
        v.setContentsMargins(T.SPACE * 3, T.SPACE * 3, T.SPACE * 3, T.SPACE * 2)
        v.addWidget(_label("Welcome to Marsad", size=14, bold=True))
        v.addWidget(_label("There is no station open yet. A station is a folder holding its radios, rigs, surveys and "
                           "recordings. Make a new one, or open one you already have" + (f" (such as {home})" if home
                                                                                        else "") + ".", muted=True))
        row = QHBoxLayout()
        b1 = _button("New Station…", primary=True)
        b2 = _button("Open an existing station…")
        b3 = _button("Quit")
        b1.clicked.connect(self._new)
        b2.clicked.connect(lambda: self._open(home))
        b3.clicked.connect(self.reject)
        for b in (b1, b2):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(b3)
        v.addLayout(row)
        self.resize(560, self.sizeHint().height())

    def _new(self):
        d = NewStationDialog(self)
        if d.exec() and d.created:
            self.choice, self.folder = "new", d.created["folder"]
            self.accept()

    def _open(self, home):
        f = pick_existing_station(self, home)
        if f:
            self.choice, self.folder = "open", f
            self.accept()


# ---------------------------------------------------------------- Add Radio

class AddRadioDialog(QDialog):
    def __init__(self, svc, status: dict, parent=None):
        super().__init__(parent)
        from marsad import setup as S
        self.S = S
        self.svc = svc
        self.status = status
        self.detected: dict = {}
        self.added = None
        self.setWindowTitle("Add Radio")
        self.setObjectName("AddRadioDialog")
        v = QVBoxLayout(self)
        v.setContentsMargins(T.SPACE * 3, T.SPACE * 2, T.SPACE * 3, T.SPACE * 2)
        v.setSpacing(T.SPACE)
        v.addWidget(_label("Add Radio", size=13, bold=True))
        v.addWidget(_label("The RTL-SDRs Windows can see, by serial (nothing is opened to list them). Pick one.",
                           muted=True))
        self.list = QListWidget()
        self.list.setMaximumHeight(130)
        v.addWidget(self.list)
        det = QHBoxLayout()
        self.b_detect = _button("Detect its tuner…")
        self.b_detect.setToolTip("Asks each plugged-in radio its serial and tuner with the engine's own probe "
                                 "(rtl_eeprom). It opens each radio for a moment, so nothing else may be using one.")
        self.b_detect.clicked.connect(self.detect)
        det.addWidget(self.b_detect)
        self.det_note = _label("", muted=True)
        det.addWidget(self.det_note, 1)
        v.addLayout(det)
        g = QGridLayout()
        self.serial = QLineEdit()
        self.nick = QLineEdit()
        self.nick.setPlaceholderText("a short name, e.g. radio1")
        self.tuner = QComboBox()
        self.tuner.addItem("Elonics E4000 (zero-IF)", "e4000")
        self.tuner.addItem("Rafael Micro R820T / R860 (low-IF)", "r820t")
        self.lo, self.hi, self.gaps = QLineEdit(), QLineEdit(), QLineEdit()
        self.maker, self.product = QLineEdit(), QLineEdit()
        rows = [("Serial", self.serial), ("Nickname", self.nick), ("Tuner", self.tuner),
                ("Range low, MHz", self.lo), ("Range high, MHz", self.hi),
                ("Blind spots (PLL gap), MHz", self.gaps), ("Maker", self.maker), ("Product", self.product)]
        for i, (k, w) in enumerate(rows):
            g.addWidget(_label(k, wrap=False), i, 0)
            g.addWidget(w, i, 1)
        v.addLayout(g)
        self.typical = _label("", muted=True)
        self.typical.setObjectName("TypicalNote")
        v.addWidget(self.typical)
        self.why = _label("", muted=True)
        v.addWidget(self.why)
        row = QHBoxLayout()
        row.addStretch(1)
        c = _button("Cancel")
        c.clicked.connect(self.reject)
        self.ok = _button("Add this radio", primary=True)
        self.ok.clicked.connect(self.save)
        row.addWidget(c)
        row.addWidget(self.ok)
        v.addLayout(row)
        self.tuner.currentIndexChanged.connect(self._typical)
        self.list.currentItemChanged.connect(self._picked)
        self._fill()
        self._typical()
        self.resize(640, self.sizeHint().height())

    def _fill(self):
        self.list.clear()
        vis = self.status.get("visible")
        if vis is None:
            it = QListWidgetItem("Windows would not list the radios here: type the serial below, or use Detect.")
            it.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list.addItem(it)
            return
        if not vis:
            it = QListWidgetItem("No RTL-SDR is plugged in.")
            it.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list.addItem(it)
        for r in vis:
            it = QListWidgetItem(f"serial {r['serial']}   ·   {r.get('name') or 'RTL-SDR'}"
                                 + (f"   ·   already this station's {r['radio']}" if r.get("radio") else ""))
            it.setData(Qt.ItemDataRole.UserRole, r)
            if r.get("radio"):
                it.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list.addItem(it)

    def _picked(self, cur, _prev=None):
        r = cur.data(Qt.ItemDataRole.UserRole) if cur else None
        if not r:
            return
        self.serial.setText(str(r["serial"]))
        if not self.product.text():
            self.product.setText(r.get("name") or "")
        f = self.detected.get(str(r["serial"]))
        if f and f.get("tuner_key"):
            self.tuner.setCurrentIndex(self.tuner.findData(f["tuner_key"]))

    def _typical(self, *_):
        t = self.S.typical(self.tuner.currentData())
        self.lo.setText(f"{t['range_mhz'][0]:g}")
        self.hi.setText(f"{t['range_mhz'][1]:g}")
        self.gaps.setText(", ".join(f"{a:g}-{b:g}" for a, b in t["gaps_mhz"]))
        self.typical.setText(f"Range, blind spots and gain steps ({', '.join(f'{g:g}' for g in t['gains'])} dB) are "
                             f"the {t['tuner_label']}'s typical figures, not yet measured. Measure the radio replaces "
                             "them with this unit's own.")

    def detect(self):
        if QMessageBox.question(self, "Detect the tuner?", "This opens each plugged-in radio for a moment to ask its "
                                "serial and tuner (rtl_eeprom). It is refused while any radio program is running.\n\n"
                                "Go ahead?") != QMessageBox.StandardButton.Yes:
            return
        self.b_detect.setEnabled(False)
        self.det_note.setText("asking each radio…")

        def got(d, err):
            self.b_detect.setEnabled(True)
            if err:
                self.det_note.setText(f"Not detected: {err}")
                return
            self.detected = {str(f.get("serial")): f for f in d.get("found") or []}
            words = [f"index {f['device']}: serial {f.get('serial')}, {f.get('tuner') or 'tuner not said'}"
                     for f in d.get("found") or []]
            self.det_note.setText(("; ".join(words) or (d.get("error") or "no radio answered"))
                                  + (f"\n{d['duplicate_help']}" if d.get("duplicates") else ""))
            cur = self.list.currentItem()
            if cur:
                self._picked(cur)
        call(lambda: self.svc.setup_post("detect", {}), got)

    def save(self):
        try:
            gaps = []
            for part in [p for p in self.gaps.text().replace(" ", "").split(",") if p]:
                a, b = part.split("-")
                gaps.append([float(a), float(b)])
            lo, hi = _float(self.lo.text()), _float(self.hi.text())
            if lo is None or hi is None:
                raise ValueError("The range needs a low and a high frequency in MHz.")
        except ValueError as exc:
            self.why.setText(str(exc) if "range" in str(exc) else "Blind spots are written like 1105-1268, comma "
                                                                   "separated.")
            return
        t = self.S.typical(self.tuner.currentData())
        body = {"serial": self.serial.text().strip(), "nickname": self.nick.text().strip(),
                "tuner": self.tuner.currentData(), "maker": self.maker.text().strip(),
                "product": self.product.text().strip()}
        if [lo, hi] != [float(x) for x in t["range_mhz"]] or gaps != t["gaps_mhz"]:
            body.update(range_mhz=[lo, hi], gaps_mhz=gaps)       # corrected by the owner; else marked typical
        self.ok.setEnabled(False)

        def got(d, err):
            self.ok.setEnabled(True)
            if err:
                self.why.setText(err)
                return
            self.added = d
            self.accept()
        call(lambda: self.svc.setup_post("radio", body), got)


# ---------------------------------------------------------------- Set up this station

class SetupDialog(QDialog):
    """Each radio's checklist. Opened from File (or Station) > Set up this station…, and from the line Live Scan shows
    while scanning is locked."""
    open_chain = Signal()
    changed = Signal(dict)

    def __init__(self, svc, parent=None, station_folder: str | None = None):
        super().__init__(parent)
        self.svc = svc
        self.folder = station_folder
        self.d: dict = {}
        self.radio = None
        self.setWindowTitle("Set up this station")
        self.setObjectName("SetupDialog")
        self.resize(980, 760)
        v = QVBoxLayout(self)
        v.setContentsMargins(T.SPACE * 2, T.SPACE * 2, T.SPACE * 2, T.SPACE * 2)
        v.setSpacing(T.SPACE)
        head = QHBoxLayout()
        self.title = _label("Set up this station", size=13, bold=True, wrap=False)
        head.addWidget(self.title)
        head.addStretch(1)
        self.loc_lbl = _label("", muted=True, wrap=False)
        head.addWidget(self.loc_lbl)
        self.b_loc = QToolButton()
        self.b_loc.setObjectName("Tool")
        self.b_loc.setText("Change…")
        self.b_loc.clicked.connect(self.change_location)
        head.addWidget(self.b_loc)
        v.addLayout(head)
        self.intro = _label("Each radio is measured and its rig drawn and checked before its first scan. Start a "
                            "scan and Start recording stay locked for a radio until all five steps are done.",
                            muted=True)
        v.addWidget(self.intro)
        top = QHBoxLayout()
        self.picker = QComboBox()
        self.picker.setObjectName("SetupRadio")
        self.picker.setMinimumContentsLength(14)
        self.picker.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.picker.currentIndexChanged.connect(self._pick)
        top.addWidget(_label("Radio", wrap=False))
        top.addWidget(self.picker)
        self.b_add = _button("Add a radio…")
        self.b_add.clicked.connect(self.add_radio)
        top.addWidget(self.b_add)
        top.addStretch(1)
        self.summary = _label("", wrap=False)
        top.addWidget(self.summary)
        v.addLayout(top)
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setFrameShape(QFrame.Shape.NoFrame)
        self.body = QWidget()
        self.steps = QVBoxLayout(self.body)
        self.steps.setContentsMargins(0, 0, 0, 0)
        self.steps.setSpacing(T.SPACE)
        sc.setWidget(self.body)
        v.addWidget(sc, 1)
        self.foot = _label("", muted=True)
        v.addWidget(self.foot)
        row = QHBoxLayout()
        row.addStretch(1)
        close = _button("Close")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        v.addLayout(row)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(2500)
        self._busy = False
        self._key = None
        self.refresh()

    # data
    def refresh(self):
        if self._busy:
            return
        self._busy = True

        def got(d, err):
            self._busy = False
            if err:
                self.foot.setText(f"The service could not say: {err}")
                return
            self.show_status(d)
        call(self.svc.setup_status, got)

    def show_status(self, d: dict):
        self.d = d
        info = d.get("info") or {}
        self.title.setText(f"Set up this station: {info.get('name') or ''}")
        self.loc_lbl.setText("location set" if info.get("location_set") else "location not set")
        self.b_loc.setVisible(bool(self.folder))
        if not d.get("tracked"):
            self.intro.setText("This station was set up before the setup checklist existed, so nothing is locked here. "
                               "The steps below can still be run for any radio.")
        names = [r["radio"] for r in d.get("radios") or []]
        cur = self.radio if self.radio in names else (names[0] if names else None)
        self.picker.blockSignals(True)
        if [self.picker.itemData(i) for i in range(self.picker.count())] != names:
            self.picker.clear()
            for r in d.get("radios") or []:
                self.picker.addItem(r["radio"], r["radio"])
        self.picker.setCurrentIndex(max(0, self.picker.findData(cur)))
        self.picker.blockSignals(False)
        self.radio = cur
        self._show()
        self.changed.emit(d)

    def _pick(self, *_):
        self.radio = self.picker.currentData()
        self._key = None
        self._show()

    def _radio(self) -> dict | None:
        return next((r for r in self.d.get("radios") or [] if r["radio"] == self.radio), None)

    # drawing the steps (rebuilt only when what they show changed; painting held off meanwhile)
    def _show(self):
        r = self._radio()
        key = repr((self.radio, r and r.get("steps"), r and r.get("facts"), self.d.get("jobs")))
        if key == self._key:
            return
        self._key = key
        self.body.setUpdatesEnabled(False)
        try:
            self._empty(self.steps)
            if r is None:
                self.summary.setText("")
                self.steps.addWidget(self._step_box(1, "Add a radio", "not started",
                                                    "No radio yet. Add one: Marsad lists the RTL-SDRs Windows sees.",
                                                    [("Add a radio…", self.add_radio)]))
                self.steps.addStretch(1)
                return
            m = r.get("missing")
            self.summary.setText("Ready: scans can start on this radio" if r.get("ready") else
                                 f"Next: step {m['number']}, {m['title']}")
            for i, s in enumerate(r["steps"], 1):
                acts = []
                if s["step"] == "add":
                    acts = [("Add another radio…", self.add_radio)]
                elif s["step"] == "draw":
                    acts = [("Open Chain", self._open_chain)]
                elif s["step"] == "confirm":
                    acts = [("It's built as drawn…", self.confirm_rig)] if r["steps"][2]["state"] == "done" else []
                box = self._step_box(i, s["title"], s["state"], s["detail"], acts)
                for sub in s.get("sub") or []:
                    box.layout().addWidget(self._sub_row(r, sub))
                self.steps.addWidget(box)
            facts = r.get("facts") or {}
            if facts:
                self.steps.addWidget(_label("This radio's profile: " + "; ".join(
                    f"{k.replace('_', ' ')} {v.get('value')} ({v.get('source')})" for k, v in facts.items()),
                    muted=True))
            self.steps.addStretch(1)
        finally:
            self.body.setUpdatesEnabled(True)

    @staticmethod
    def _empty(lay):
        while lay.count():
            it = lay.takeAt(0)
            if it.widget() is not None:
                it.widget().hide()
                it.widget().deleteLater()
            elif it.layout() is not None:
                SetupDialog._empty(it.layout())

    def _step_box(self, n, title, state, detail, actions):
        f = QFrame()
        f.setObjectName("TextPanel")
        lay = QVBoxLayout(f)
        lay.setContentsMargins(T.SPACE * 1.5, T.SPACE, T.SPACE * 1.5, T.SPACE)
        h = QHBoxLayout()
        col = T.C[STATE_COLOUR.get(state, "muted")]
        h.addWidget(_label(f"<span style='color:{col}'>{STATE_MARK.get(state, '○')}</span>&nbsp; {n}. {title}",
                           bold=True, wrap=False))
        st = _label(state, wrap=False)
        st.setStyleSheet(f"color: {col};")
        st.setObjectName("StepState")
        h.addWidget(st)
        h.addStretch(1)
        for text, fn in actions:
            b = _button(text)
            b.clicked.connect(fn)
            h.addWidget(b)
        lay.addLayout(h)
        if detail:
            lay.addWidget(_label(detail, muted=True))
        f.setProperty("step_state", state)
        return f

    def _sub_row(self, r, sub):
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(T.SPACE * 2, 0, 0, 0)
        col = T.C[STATE_COLOUR.get(sub["state"], "muted")]
        h.addWidget(_label(f"<span style='color:{col}'>{STATE_MARK.get(sub['state'], '○')}</span>&nbsp; {sub['title']}",
                           wrap=False))
        h.addWidget(_label(sub.get("detail") or sub["state"], muted=True), 1)
        running = sub["state"] == "in progress"
        if sub["step"] == "range" and (r.get("entry") or {}).get("tuner") != "e4000" and sub["state"] != "done":
            b = _button("Keep the typical range…")
            b.setToolTip("No command measures this tuner's range here. Its typical figures are kept and stay marked "
                         "\"typical, not measured\".")
            b.clicked.connect(lambda *_: self.accept_typical())
            h.addWidget(b)
        else:
            for text, short in (("Short test", True), ("Run…", False)):
                b = _button(text)
                b.setEnabled(not running)
                b.clicked.connect(lambda *_, s=sub["step"], sh=short: self.run_step(s, sh))
                h.addWidget(b)
        w.setProperty("sub_step", sub["step"])
        return w

    # actions
    def add_radio(self):
        d = AddRadioDialog(self.svc, self.d, self)
        if d.exec() and d.added:
            self.radio = (d.added or {}).get("radio")
            self._key = None
            if d.added.get("status"):
                self.show_status(d.added["status"])
            else:
                self.refresh()

    def _open_chain(self):
        self.open_chain.emit()
        self.accept()

    def accept_typical(self):
        if QMessageBox.question(self, "Keep the typical range?", "No command here measures this tuner's tuning range. "
                                "Keep its typical figures? They stay marked \"typical, not measured\" in the radio's "
                                "profile.") != QMessageBox.StandardButton.Yes:
            return
        call(lambda: self.svc.setup_post("accept_typical", {"radio": self.radio}),
             lambda d, err: self.show_status(d) if not err else self.foot.setText(err))

    def confirm_rig(self):
        r = self._radio() or {}
        text = (r.get("steps") or [{}] * 3)[2].get("detail") or ""
        if QMessageBox.question(self, "Built as drawn?", f"Is the rig for the {self.radio} built exactly as drawn?\n\n"
                                f"{text}\n\nCheck each part and connection first. If the drawing changes, this is asked "
                                "again.") != QMessageBox.StandardButton.Yes:
            return
        call(lambda: self.svc.setup_post("confirm_rig", {"radio": self.radio}),
             lambda d, err: self.show_status(d) if not err else self.foot.setText(err))

    def run_step(self, step: str, short: bool):
        s = (self.d.get("steps") or {}).get(step) or {}
        text = (f"{s.get('what', '')}\n\nWhat to connect: {s.get('connect', '')}\nHow long: {s.get('duration', '')}"
                f"\nWhat to expect: {s.get('expect', '')}\n\n"
                + ("This is the short test: a quick run of the same command, to see it works before the full one."
                   if short else "Run the full measurement now? It opens the radio."))
        if QMessageBox.question(self, s.get("title", step) + (" (short test)" if short else ""),
                                text) != QMessageBox.StandardButton.Yes:
            return

        def got(d, err):
            if err:
                QMessageBox.information(self, "Not started", err)
                return
            self.foot.setText(f"Started: {d.get('line', '')}. Its log: {(d.get('job') or {}).get('log', '')}")
            if d.get("status"):
                self.show_status(d["status"])
        call(lambda: self.svc.setup_post("run", {"radio": self.radio, "step": step, "short": short}), got)

    def change_location(self):
        """Change or clear the location: written only to this station's station.local.json, never read back."""
        from marsad import setup as S
        d = QDialog(self)
        d.setWindowTitle("Station location")
        v = QVBoxLayout(d)
        box = LocationBox("The current location is never shown. Type a new one, or tick nothing and press Clear.")
        v.addWidget(box)
        row = QHBoxLayout()
        clear = _button("Clear the location")
        save = _button("Save", primary=True)
        cancel = _button("Cancel")
        row.addWidget(clear)
        row.addStretch(1)
        row.addWidget(cancel)
        row.addWidget(save)
        v.addLayout(row)
        why = _label("", muted=True)
        v.addWidget(why)
        cancel.clicked.connect(d.reject)

        def do(loc):
            try:
                S.set_location(self.folder, loc)
            except (ValueError, OSError) as exc:
                why.setText(str(exc))
                return
            box.clear()
            d.accept()
            self.refresh()

        def on_save():
            try:
                loc = box.value()
            except ValueError as exc:
                why.setText(str(exc))
                return
            if loc is None:
                why.setText("Tick the box and type a location, or use Clear.")
                return
            do(loc)
        save.clicked.connect(on_save)
        clear.clicked.connect(lambda: do(None))
        d.exec()
