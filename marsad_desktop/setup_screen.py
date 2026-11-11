"""Relay 021: New Station, the first run, Add Radio, and "Set up this station".

  NewStationDialog   name, description, where to keep it (Documents\\Marsad\\Stations\\<name> by default) and an
                     optional location that is written only to that station's own station.local.json and never shown
                     again: the window says "set" or "not set".
  FirstRunDialog     the first time the window has no station: New Station, or open an existing one.
  SetupDialog        relay 022: a step-by-step guide per radio (a step list down the side, Back and Next, a page per
                     step: add, measure x4, draw, confirm, path checks x2). Each measurement says what it does, what to
                     connect, how long and what to expect before it runs, offers a short test first, and keeps every
                     result with its date. Changing a finished step marks the later ones resting on it "needs
                     re-checking"; closing keeps the progress and the page; a run in the background keeps running.
  EditRadioDialog    relay 022: step 1 reopened, to correct the radio.
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

from marsad.setup import STEP_ORDER, STEP_TITLES, STEPS as STEPS_TEXT                         # noqa: E402

STATE_COLOUR = {"done": "open", "in progress": "survey_light", "needs attention": "alert_text",
                "needs re-checking": "anomaly", "not started": "muted"}
STATE_MARK = {"done": "✓", "in progress": "…", "needs attention": "!", "needs re-checking": "↻", "not started": "○"}


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
    """Optional, private. Typed once (or found by Windows, relay 022); never shown again.

    Relay 023: the two fields are always open and checked as they are typed. Both empty means no location; both
    valid means a location; anything else is said in a short line and never blocks the form (New Station then saves
    without a location)."""

    changed = Signal()

    def __init__(self, note_set: str | None = None):
        super().__init__()
        self.setObjectName("LocationBox")
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.addWidget(_label("Location (optional, private): for outdoor weather and the sun's height. Kept only on this "
                           "PC, in this station's own station.local.json, never shown or exported.", muted=True))
        row = QHBoxLayout()
        self.lat = QLineEdit()
        self.lat.setObjectName("LocLat")
        self.lat.setPlaceholderText("latitude, decimal degrees")
        self.lon = QLineEdit()
        self.lon.setObjectName("LocLon")
        self.lon.setPlaceholderText("longitude, decimal degrees, west negative")
        for w in (self.lat, self.lon):
            w.textChanged.connect(self._check)
            row.addWidget(w)
        # relay 022: Windows' own location service fills both fields for the owner to confirm; nothing is saved here
        self.b_find = _button("Find my location")
        self.b_find.setObjectName("FindLocation")
        self.b_find.setAutoDefault(False)
        self.b_find.setToolTip("Asks Windows' own location service on this PC (GPS or nearby Wi-Fi, as Windows "
                               "decides). Marsad sends nothing anywhere and uses no internet address lookup. The two "
                               "fields are filled for you to check; nothing is kept until you save.")
        self.b_find.clicked.connect(self.find)
        row.addWidget(self.b_find)
        v.addLayout(row)
        self.round = QCheckBox("Round to about 1 km (two decimal places)")
        self.round.setObjectName("LocRound")
        self.round.setChecked(True)
        self.round.setToolTip("Plenty for outdoor weather and the sun's height, and the value kept is less exact. "
                              "Untick to keep every decimal.")
        self.round.toggled.connect(self._reround)
        v.addWidget(self.round)
        self.found = _label("", muted=True)
        self.found.setObjectName("LocFound")
        v.addWidget(self.found)
        self.check = _label("", muted=True)
        self.check.setObjectName("LocCheck")
        v.addWidget(self.check)
        if note_set is not None:
            v.addWidget(_label(note_set, muted=True))
        self._raw = None                        # what Windows reported, in memory only, until saved or closed
        self._filling = False

    def find(self):
        from marsad_desktop import locate as L
        self.b_find.setEnabled(False)
        self.found.setText("Asking Windows' location service…")

        def got(d, err):
            self.b_find.setEnabled(True)
            if err or not d:
                self.found.setText(f"Windows' location service did not answer ({err}). Type the values by hand.")
                return
            if not d.get("ok"):
                self.found.setText(d.get("text") or "No position. Type the values by hand.")
                return
            self._raw = (d["lat"], d["lon"])
            self._fill()
            src = {"WiFi": "nearby Wi-Fi", "Satellite": "GPS", "Cellular": "the mobile network",
                   "IPAddress": "the network address"}.get(d.get("source") or "", d.get("source") or "Windows")
            self.found.setText(f"Found by Windows from {src}, {L.accuracy_words(d.get('accuracy_m'))}. Check the two "
                               "values, change them if you like, then save. Nothing is kept until you do.")
        call(lambda: L.find(), got)

    def _fill(self):
        from marsad_desktop import locate as L
        if self._raw is None:
            return
        on = self.round.isChecked()
        self._filling = True
        self.lat.setText(f"{L.rounded(self._raw[0], on):g}" if on else repr(self._raw[0]))
        self.lon.setText(f"{L.rounded(self._raw[1], on):g}" if on else repr(self._raw[1]))
        self._filling = False
        self._check()

    def _reround(self, on):
        """The privacy option: Windows' figures shown rounded or in full. Typed figures are rounded when saved."""
        if self._raw is not None:
            self._fill()

    def problem(self) -> str | None:
        """None when the two fields make a location or are both empty; else what is wrong, in a few words."""
        a, b = self.lat.text().strip(), self.lon.text().strip()
        if not a and not b:
            return None
        lat, lon = _float(a), _float(b)
        if not a or not b:
            return "Type both latitude and longitude, or leave both empty."
        if lat is None or lon is None:
            return "Latitude and longitude are decimal degrees, like 51.48 and -0.01."
        if not (-90 <= lat <= 90):
            return "Latitude runs from -90 to 90."
        if not (-180 <= lon <= 180):
            return "Longitude runs from -180 to 180."
        return None

    def _check(self, *_):
        if not self._filling and self._raw is not None and self.sender() in (self.lat, self.lon):
            self._raw = None                     # typed over Windows' figures: the typed ones count
        p = self.problem()
        empty = not self.lat.text().strip() and not self.lon.text().strip()
        self.check.setText("" if empty and p is None else (p or "Location looks right."))
        self.check.setProperty("bad", bool(p))
        self.changed.emit()

    def value(self):
        """None (both empty), or {"lat", "lon"}, rounded to about 1 km when that is ticked; raises ValueError in words
        when the two fields do not make a location."""
        from marsad_desktop import locate as L
        p = self.problem()
        if p:
            raise ValueError(p)
        a, b = self.lat.text().strip(), self.lon.text().strip()
        if not a and not b:
            return None
        on = self.round.isChecked()
        return {"lat": L.rounded(float(a), on), "lon": L.rounded(float(b), on)}

    def clear(self):
        self._filling = True
        self.lat.clear()
        self.lon.clear()
        self._filling = False
        self._raw = None
        self.found.setText("")
        self.check.setText("")


class NewStationDialog(QDialog):
    """Station > New Station…: a clean station from the engine's own `marsad init`, nothing copied from another.

    Relay 023: the folder is shown at once (Documents\\Marsad\\Stations\\<name>, following the name as it is typed,
    the next free one when that name is taken); Browse… opens Windows' folder picker; Create is open as soon as the
    name is given and the folder is acceptable; the location never blocks it (a half-typed one is left out, and the
    form says so); Enter creates, Esc cancels. The folder is made only when the station is created."""

    def __init__(self, parent=None, documents: Path | None = None):
        super().__init__(parent)
        from marsad import setup as S
        self.S = S
        self.documents = documents
        self.setWindowTitle("New Station")
        self.setObjectName("NewStationDialog")
        self.folder_chosen: Path | None = None
        self.created: dict | None = None
        self.saved_without_location = False
        v = QVBoxLayout(self)
        v.setContentsMargins(T.SPACE * 3, T.SPACE * 2, T.SPACE * 3, T.SPACE * 2)
        v.setSpacing(T.SPACE)
        v.addWidget(_label("New Station", size=13, bold=True))
        v.addWidget(_label("A new station starts empty: no radios, no rigs, no saved scans. Nothing is copied from "
                           "another station, and nothing done here changes one.", muted=True))
        g = QGridLayout()
        g.addWidget(_label("Station name", wrap=False), 0, 0)
        self.name = QLineEdit()
        self.name.setObjectName("StationName")
        self.name.setPlaceholderText("required")
        g.addWidget(self.name, 0, 1)
        g.addWidget(_label("Description", wrap=False), 1, 0)
        self.desc = QLineEdit()
        self.desc.setPlaceholderText("optional")
        g.addWidget(self.desc, 1, 1)
        g.addWidget(_label("Where to keep it", wrap=False), 2, 0, Qt.AlignmentFlag.AlignTop)
        fr = QHBoxLayout()
        self.folder = _label("", wrap=True)
        self.folder.setObjectName("StationFolder")
        fr.addWidget(self.folder, 1)
        self.b_browse = _button("Browse…")
        self.b_browse.setObjectName("BrowseFolder")
        self.b_browse.setAutoDefault(False)            # Enter means Create, never the folder picker
        self.b_browse.setToolTip("Choose another place with Windows' folder picker: a new or empty folder, outside "
                                 "every station. The folder is made when the station is created.")
        self.b_browse.clicked.connect(self._pick)
        fr.addWidget(self.b_browse)
        g.addLayout(fr, 2, 1)
        self.folder_note = _label("", muted=True)
        self.folder_note.setObjectName("FolderNote")
        g.addWidget(self.folder_note, 3, 1)
        v.addLayout(g)
        self.loc = LocationBox()
        v.addWidget(self.loc)
        self.why = _label("", muted=True)
        self.why.setObjectName("NewStationWhy")
        v.addWidget(self.why)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = _button("Cancel")
        cancel.setAutoDefault(False)
        cancel.clicked.connect(self.reject)                 # Esc does the same (a dialog's own key)
        self.ok = _button("Create station", primary=True)
        self.ok.setDefault(True)                            # Enter anywhere in the form, when it is enabled
        self.ok.clicked.connect(self.create)
        row.addWidget(cancel)
        row.addWidget(self.ok)
        v.addLayout(row)
        self.name.textChanged.connect(self._update)
        self.loc.changed.connect(self._update)
        self.resize(660, self.sizeHint().height())
        self._update()

    def target(self) -> Path:
        if self.folder_chosen:
            return self.folder_chosen
        return self.S.free_default_folder(self.name.text().strip() or "station", self.documents)

    def _pick(self):
        start = self.folder_chosen or self.S.default_folder("x", self.documents).parent
        d = QFileDialog.getExistingDirectory(self, "Where to keep the station (a new or empty folder)",
                                             str(start if start.exists() else Path.home()))
        if d:
            self.folder_chosen = Path(d)
            self._update()

    def _update(self, *_):
        name = self.name.text().strip()
        t = self.target()
        self.folder.setText(str(t))
        notes = []
        if self.folder_chosen:
            notes.append("chosen with Browse…")
        else:
            safe, changed = self.S.safe_name(name or "station")
            if not name:
                notes.append("the default, named after the station")
            if changed:
                notes.append("characters Windows does not allow in a folder name were replaced")
            if name and t.name != safe:
                notes.append(f"\"{safe}\" is already used there, so the next free name")
        text = "; ".join(notes)
        self.folder_note.setText((text[:1].upper() + text[1:] + ". " if text else "")
                                 + "It is made when the station is created.")
        problem = self.S.folder_problem(t) if name else None
        loc_problem = self.loc.problem()
        if not name:
            self.why.setText("Give the station a name.")
        elif problem:
            self.why.setText(problem)
        elif loc_problem:
            self.why.setText("The station will be created without a location (location not set): "
                             + loc_problem[0].lower() + loc_problem[1:])
        else:
            self.why.setText("")
        self.ok.setEnabled(bool(name) and not problem)

    def create(self):
        if not self.ok.isEnabled():
            return
        try:
            loc = self.loc.value()
        except ValueError:
            loc = None                                      # never blocks: saved without, and said so
            self.saved_without_location = True
        try:
            self.created = self.S.create_station(self.target(), self.name.text(), self.desc.text(), loc)
        except (ValueError, OSError) as exc:
            self.why.setText(str(exc))
            return
        if self.created is not None:
            self.created["location_set"] = loc is not None
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


# ---------------------------------------------------------------- Set up this station (relay 022: a guide)

class EditRadioDialog(QDialog):
    """Relay 022: step 1 reopened. Correct the radio's serial, tuner, range, blind spots, maker or product. The later
    steps that rest on what changes then read "needs re-checking"."""

    def __init__(self, svc, radio: dict, parent=None):
        super().__init__(parent)
        self.svc = svc
        self.nick = radio["radio"]
        self.result = None
        e = radio.get("entry") or {}
        self.setWindowTitle(f"Change {self.nick}")
        self.setObjectName("EditRadioDialog")
        v = QVBoxLayout(self)
        v.setContentsMargins(T.SPACE * 3, T.SPACE * 2, T.SPACE * 3, T.SPACE * 2)
        v.setSpacing(T.SPACE)
        v.addWidget(_label(f"Change {self.nick}", size=13, bold=True))
        v.addWidget(_label("Steps that rest on what you change (the measurements run on the radio's frequencies, the "
                           "path checks through the rig) will read \"needs re-checking\" until they are run again. "
                           "Nothing already recorded is removed.", muted=True))
        g = QGridLayout()
        self.serial = QLineEdit(str(((radio.get("facts") or {}).get("serial") or {}).get("value") or
                                    _serial_of(radio) or ""))
        self.tuner = QComboBox()
        self.tuner.addItem("Elonics E4000 (zero-IF)", "e4000")
        self.tuner.addItem("Rafael Micro R820T / R860 (low-IF)", "r820t")
        self.tuner.setCurrentIndex(max(0, self.tuner.findData(e.get("tuner"))))
        lo, hi = (e.get("range_mhz") or [0, 0])[:2]
        self.lo, self.hi = QLineEdit(f"{lo:g}"), QLineEdit(f"{hi:g}")
        self.gaps = QLineEdit(", ".join(f"{a:g}-{b:g}" for a, b in e.get("gaps_mhz") or []))
        usb = e.get("usb") or {}
        self.maker, self.product = QLineEdit(usb.get("maker") or ""), QLineEdit(usb.get("product") or "")
        for i, (k, w) in enumerate([("Serial", self.serial), ("Tuner", self.tuner), ("Range low, MHz", self.lo),
                                    ("Range high, MHz", self.hi), ("Blind spots (PLL gap), MHz", self.gaps),
                                    ("Maker", self.maker), ("Product", self.product)]):
            g.addWidget(_label(k, wrap=False), i, 0)
            g.addWidget(w, i, 1)
        v.addLayout(g)
        self.why = _label("", muted=True)
        v.addWidget(self.why)
        row = QHBoxLayout()
        row.addStretch(1)
        c = _button("Cancel")
        c.clicked.connect(self.reject)
        self.ok = _button("Save changes", primary=True)
        self.ok.clicked.connect(self.save)
        row.addWidget(c)
        row.addWidget(self.ok)
        v.addLayout(row)
        self.resize(560, self.sizeHint().height())

    def save(self):
        try:
            gaps = []
            for part in [p for p in self.gaps.text().replace(" ", "").split(",") if p]:
                a, b = part.split("-")
                gaps.append([float(a), float(b)])
        except ValueError:
            self.why.setText("Blind spots are written like 1105-1268, comma separated.")
            return
        lo, hi = _float(self.lo.text()), _float(self.hi.text())
        if lo is None or hi is None:
            self.why.setText("The range needs a low and a high frequency in MHz.")
            return
        body = {"radio": self.nick, "serial": self.serial.text().strip(), "tuner": self.tuner.currentData(),
                "range_mhz": [lo, hi], "gaps_mhz": gaps, "maker": self.maker.text().strip(),
                "product": self.product.text().strip()}
        self.ok.setEnabled(False)

        def got(d, err):
            self.ok.setEnabled(True)
            if err:
                self.why.setText(err)
                return
            self.result = d
            self.accept()
        call(lambda: self.svc.setup_post("radio_update", body), got)


def _serial_of(radio: dict) -> str:
    detail = next((s.get("detail") or "" for s in radio.get("steps") or [] if s.get("step") == "add"), "")
    return detail.replace("serial ", "", 1) if detail.startswith("serial ") else ""


# The guide's pages, in order: (checklist step, page key). Measure and Path checks have one page per part.
PAGES = [("add", "add"), ("measure", "range"), ("measure", "warmup"), ("measure", "retune"), ("measure", "floor"),
         ("draw", "draw"), ("confirm", "confirm"), ("paths", "adsb"), ("paths", "baseline")]
TOUCHED = ("done", "needs re-checking", "in progress", "needs attention")


class SetupDialog(QDialog):
    """Set up this station, as a step-by-step guide (relay 022; relay 021 made it a checklist).

    A step list down the side (Add the radio, Measure x4, Draw the rig, Confirm, Path checks x2) shows where you are
    and each step's state. Back is always there; a finished step can be reopened, and changing it marks the later
    steps that rest on it "needs re-checking" (marsad.setup.DEPENDS). Next opens only when this step is complete,
    with a line saying what is still needed. A finished step in the list can be jumped to; a later unfinished one
    stays locked. Closing keeps the progress (results live with the station) and the page (the window's preferences);
    a measurement keeps running in the background whatever the guide does, and shows as running.
    """
    open_chain = Signal()
    changed = Signal(dict)

    def __init__(self, svc, parent=None, station_folder: str | None = None):
        super().__init__(parent)
        self.svc = svc
        self.folder = station_folder
        self.d: dict = {}
        self.radio = None
        self.page = None                        # the page key shown
        self.setWindowTitle("Set up this station")
        self.setObjectName("SetupDialog")
        self.resize(1040, 760)
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
                            "scan and Start recording stay locked for a radio until every step is done.", muted=True)
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
        mid = QHBoxLayout()
        self.steps_list = QListWidget()
        self.steps_list.setObjectName("SetupSteps")
        self.steps_list.setFixedWidth(280)
        self.steps_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.steps_list.itemClicked.connect(self._clicked)
        mid.addWidget(self.steps_list)
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setFrameShape(QFrame.Shape.NoFrame)
        self.body = QWidget()
        self.steps = QVBoxLayout(self.body)          # the page shown (named as in relay 021, for its checks)
        self.steps.setContentsMargins(T.SPACE, 0, 0, 0)
        self.steps.setSpacing(T.SPACE)
        sc.setWidget(self.body)
        mid.addWidget(sc, 1)
        v.addLayout(mid, 1)
        self.needed = _label("", wrap=True)
        self.needed.setObjectName("SetupNeeded")
        v.addWidget(self.needed)
        self.foot = _label("", muted=True)
        v.addWidget(self.foot)
        row = QHBoxLayout()
        self.b_back = _button("← Back")
        self.b_back.setObjectName("SetupBack")
        self.b_back.clicked.connect(self.back)
        self.b_next = _button("Next →", primary=True)
        self.b_next.clicked.connect(self.next)
        close = _button("Close")
        close.clicked.connect(self.accept)
        row.addWidget(self.b_back)
        row.addStretch(1)
        row.addWidget(self.b_next)
        row.addWidget(close)
        v.addLayout(row)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(2500)
        self._busy = False
        self._key = None
        self.refresh()

    # ---- data
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
                               "The steps can still be run for any radio.")
        names = [r["radio"] for r in d.get("radios") or []]
        cur = self.radio if self.radio in names else (names[0] if names else None)
        self.picker.blockSignals(True)
        if [self.picker.itemData(i) for i in range(self.picker.count())] != names:
            self.picker.clear()
            for r in d.get("radios") or []:
                self.picker.addItem(r["radio"], r["radio"])
        self.picker.setCurrentIndex(max(0, self.picker.findData(cur)))
        self.picker.blockSignals(False)
        if cur != self.radio:
            self.page = None
        self.radio = cur
        self._show()
        self.changed.emit(d)

    def _pick(self, *_):
        self.radio = self.picker.currentData()
        self.page = None
        self._key = None
        self._show()

    def _radio(self) -> dict | None:
        return next((r for r in self.d.get("radios") or [] if r["radio"] == self.radio), None)

    # ---- the pages and their states
    def page_state(self, key: str) -> tuple[str, str]:
        """(state, detail) of one page, from the service's checklist."""
        r = self._radio()
        if r is None:
            return "not started", ""
        for s in r.get("steps") or []:
            if s["step"] == key:
                return s["state"], s.get("detail") or ""
            for sub in s.get("sub") or []:
                if sub["step"] == key:
                    return sub["state"], sub.get("detail") or ""
        return "not started", ""

    def reachable(self, key: str) -> bool:
        """A page may be opened from the list when it has been started before, or every page before it is done."""
        keys = [k for _, k in PAGES]
        if self.page_state(key)[0] in TOUCHED:
            return True
        return all(self.page_state(k)[0] == "done" for k in keys[:keys.index(key)])

    def blocker(self, key: str) -> str | None:
        """The first unfinished page before this one, in words, or None."""
        keys = [k for _, k in PAGES]
        for k in keys[:keys.index(key)]:
            if self.page_state(k)[0] != "done":
                return self._page_title(k)
        return None

    def frontier(self) -> str:
        """The first page not done (the last page when all are)."""
        return next((k for _, k in PAGES if self.page_state(k)[0] != "done"), PAGES[-1][1])

    def _page_title(self, key: str) -> str:
        step = next(s for s, k in PAGES if k == key)
        n = STEP_ORDER.index(step) + 1
        if key in ("add", "draw", "confirm"):
            return f"step {n}, {STEP_TITLES[key]}"
        return f"step {n}, {STEPS_TEXT.get(key, {}).get('title', key)}"

    def _pref_key(self) -> str:
        return f"setup/page/{(self.folder or '').lower()}/{self.radio or ''}"

    def _remember_page(self):
        from marsad_desktop.chain_screen import prefs
        if self.radio and self.page:
            prefs().setValue(self._pref_key(), self.page)

    def _restore_page(self) -> str:
        """The page this radio's guide was left on (closing keeps it), when it can still be opened; else where the
        work is."""
        from marsad_desktop.chain_screen import prefs
        saved = str(prefs().value(self._pref_key(), "") or "")
        keys = [k for _, k in PAGES]
        if saved in keys and self.reachable(saved):
            return saved
        return self.frontier()

    def go(self, key: str):
        self.page = key
        self.foot.setText("")
        self._remember_page()
        self._key = None
        self._show()

    def back(self):
        keys = [k for _, k in PAGES]
        i = keys.index(self.page or keys[0])
        if i > 0:
            self.go(keys[i - 1])

    def next(self):
        keys = [k for _, k in PAGES]
        i = keys.index(self.page or keys[0])
        if i + 1 < len(keys) and self.page_state(self.page)[0] == "done":
            self.go(keys[i + 1])

    def _clicked(self, it):
        key = it.data(Qt.ItemDataRole.UserRole)
        if key and self.reachable(key):
            self.go(key)
        elif key:
            self.foot.setText(f"Locked: finish {self.blocker(key)} first.")

    # ---- drawing (rebuilt only when what it shows changed; painting held off meanwhile)
    def _show(self):
        r = self._radio()
        if self.page is None:
            self.page = self._restore_page() if r is not None else "add"
            self._remember_page()
        key = repr((self.radio, self.page, r and r.get("steps"), r and r.get("facts"), r and r.get("entry"),
                    self.d.get("jobs")))
        if key == self._key:
            return
        self._key = key
        self.body.setUpdatesEnabled(False)
        try:
            self._fill_list()
            self._empty(self.steps)
            if r is None:
                self.summary.setText("")
                self.steps.addWidget(self._page_box("add", "Add a radio", "not started",
                                                    "No radio yet. Add one: Marsad lists the RTL-SDRs Windows sees.",
                                                    [("Add a radio…", self.add_radio, True)]))
                self.steps.addStretch(1)
                self._nav()
                return
            m = r.get("missing")
            self.summary.setText("Ready: scans can start on this radio" if r.get("ready") else
                                 f"Next: step {m['number']}, {m['title']}")
            self._fill_page(r)
            self.steps.addStretch(1)
            self._nav()
        finally:
            self.body.setUpdatesEnabled(True)

    def _fill_list(self):
        self.steps_list.blockSignals(True)
        self.steps_list.clear()
        last_step = None
        for step, key in PAGES:
            if step != last_step and step in ("measure", "paths"):
                n = STEP_ORDER.index(step) + 1
                h = QListWidgetItem(f"{n}. {STEP_TITLES[step]}")
                h.setFlags(Qt.ItemFlag.NoItemFlags)
                f = h.font()
                f.setBold(True)
                h.setFont(f)
                h.setForeground(T.color("text2"))
                self.steps_list.addItem(h)
            last_step = step
            state, _ = self.page_state(key) if self._radio() is not None else ("not started", "")
            n = STEP_ORDER.index(step) + 1
            name = (f"{n}. {STEP_TITLES[key]}" if key in ("add", "draw", "confirm")
                    else f"     {STEPS_TEXT[key]['title'].split(' (')[0]}")
            open_ = self.reachable(key) if self._radio() is not None else key == "add"
            mark = STATE_MARK.get(state, "○") if open_ else "\U0001F512"
            it = QListWidgetItem(f"{mark}  {name}" + ("   ◀" if key == self.page else ""))
            it.setData(Qt.ItemDataRole.UserRole, key)
            it.setData(Qt.ItemDataRole.UserRole + 1, state)
            it.setForeground(T.color(STATE_COLOUR.get(state, "muted")) if open_ else T.color("faint"))
            it.setToolTip(f"{state}" if open_ else f"Locked: finish {self.blocker(key)} first")
            if key == self.page:
                f = it.font()
                f.setBold(True)
                it.setFont(f)
            self.steps_list.addItem(it)
            if key == self.page:
                self.steps_list.setCurrentItem(it)
        self.steps_list.blockSignals(False)

    def _nav(self):
        """Back always there; Next only when this page is done, with what is still needed in words."""
        keys = [k for _, k in PAGES]
        i = keys.index(self.page)
        self.b_back.setEnabled(i > 0)
        state, detail = self.page_state(self.page) if self._radio() is not None else ("not started", "")
        last = i + 1 >= len(keys)
        self.b_next.setEnabled(state == "done" and not last)
        r = self._radio()
        if r is not None and r.get("ready"):
            text = "Every step is done: scanning is unlocked for this radio."
        elif state == "done":
            text = "Done. Next goes on to " + (self._page_title(keys[i + 1]) if not last else "the end") + "."
        elif state == "in progress":
            text = ("Still needed: this run to finish. It keeps running in the background if you go Back or close "
                    "the guide.")
        elif state == "needs re-checking":
            text = "Still needed: run it again (what changed is said above)."
        elif state == "needs attention":
            text = "Still needed: a run that succeeds. The last one did not: " + (detail or "see its result.")
        else:
            text = "Still needed: " + {
                "add": "add the radio.",
                "draw": "draw the rig on the Chain screen and use it for runs.",
                "confirm": "check the rig against the drawing, then confirm it.",
                "range": "run the full measurement, or keep the typical range (a short test does not count).",
            }.get(self.page, "run the full measurement (a short test does not count).")
            b = self.blocker(self.page)
            if b:
                text = f"Locked: finish {b} first."
        self.needed.setText(text)
        self.needed.setProperty("state", state)

    @staticmethod
    def _empty(lay):
        while lay.count():
            it = lay.takeAt(0)
            if it.widget() is not None:
                it.widget().hide()
                it.widget().deleteLater()
            elif it.layout() is not None:
                SetupDialog._empty(it.layout())

    def _page_box(self, key, title, state, detail, actions, actions_last=False):
        f = QFrame()
        f.setObjectName("TextPanel")
        lay = QVBoxLayout(f)
        lay.setContentsMargins(T.SPACE * 1.5, T.SPACE, T.SPACE * 1.5, T.SPACE)
        h = QHBoxLayout()
        col = T.C[STATE_COLOUR.get(state, "muted")]
        h.addWidget(_label(f"<span style='color:{col}'>{STATE_MARK.get(state, '○')}</span>&nbsp; {title}",
                           size=11.5, bold=True, wrap=False))
        st = _label(state, wrap=False)
        st.setStyleSheet(f"color: {col};")
        st.setObjectName("StepState")
        h.addWidget(st)
        h.addStretch(1)
        lay.addLayout(h)
        if detail:
            lay.addWidget(_label(detail, muted=True))
        if actions and not actions_last:
            lay.addLayout(self._actions(actions))
        f.setProperty("step_state", state)
        f.setProperty("page", key)
        return f

    @staticmethod
    def _actions(actions):
        row = QHBoxLayout()
        for text, fn, on in actions:
            b = _button(text)
            b.setEnabled(on)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        return row

    def _fill_page(self, r: dict):
        key = self.page
        state, detail = self.page_state(key)
        locked = self.blocker(key) if state not in TOUCHED else None
        running = state == "in progress"
        if key == "add":
            e = r.get("entry") or {}
            facts = r.get("facts") or {}
            lines = [f"serial {_serial_of(r) or 'not known'}, tuner {e.get('tuner') or '?'}",
                     "range " + "–".join(f"{x:g}" for x in e.get("range_mhz") or []) + " MHz"
                     + (", blind spots " + ", ".join(f"{a:g}–{b:g}" for a, b in e.get("gaps_mhz") or [])
                        if e.get("gaps_mhz") else "")]
            box = self._page_box("add", f"Step 1. {STEP_TITLES['add']}: {self.radio}", state, "; ".join(lines),
                                 [("Change this radio…", self.edit_radio, not self._radio_running(r)),
                                  ("Add another radio…", self.add_radio, True)])
            if facts:
                box.layout().addWidget(_label("This radio's profile: " + "; ".join(
                    f"{k.replace('_', ' ')} {v.get('value')} ({v.get('source')})" for k, v in facts.items()),
                    muted=True))
            box.layout().addWidget(_label("Changing the radio marks the steps that rest on it \"needs re-checking\": "
                                          "the measurements if its tuner, serial or frequencies change, the path "
                                          "checks if its tuner or serial does.", muted=True))
            self.steps.addWidget(box)
            return
        if key == "draw":
            self.steps.addWidget(self._page_box("draw", f"Step 3. {STEP_TITLES['draw']}", state, detail,
                                                [("Open Chain", self._open_chain, True)]))
            self.steps.addWidget(_label("Draw the rig on the Chain screen and press Use for runs. This page turns done "
                                        "when the rig used for runs has a path to this radio.", muted=True))
            return
        if key == "confirm":
            self.steps.addWidget(self._page_box(
                "confirm", f"Step 4. {STEP_TITLES['confirm']}", state, detail,
                [("It's built as drawn…", self.confirm_rig, self.page_state("draw")[0] == "done")]))
            return
        s = STEPS_TEXT.get(key) or {}
        n = STEP_ORDER.index(next(st for st, k in PAGES if k == key)) + 1
        acts = []
        if key == "range" and (r.get("entry") or {}).get("tuner") != "e4000":
            acts.append(("Keep the typical range…", self.accept_typical, not running and not locked))
        else:
            acts.append(("Short test", lambda *_, k=key: self.run_step(k, True), not running and not locked))
            acts.append(("Run…", lambda *_, k=key: self.run_step(k, False), not running and not locked))
            if key == "range":
                acts.append(("Keep the range as it is…", self.accept_typical,
                             not running and not locked and state == "needs re-checking"))
        box = self._page_box(key, f"Step {n}. {s.get('title', key)}", state, detail, acts, actions_last=True)
        g = QGridLayout()
        g.setColumnStretch(1, 1)
        g.setHorizontalSpacing(T.SPACE * 2)
        for i, (k, word) in enumerate((("what", "What it does"), ("connect", "What to connect"),
                                       ("duration", "How long"), ("expect", "What to expect"))):
            g.addWidget(_label(word, bold=True, wrap=False), i, 0, Qt.AlignmentFlag.AlignTop)
            g.addWidget(_label(s.get(k, ""), muted=True), i, 1)
        box.layout().addLayout(g)
        sub = next((x for st in r.get("steps") or [] for x in st.get("sub") or [] if x["step"] == key), {})
        kept = int(sub.get("results") or 0)
        box.layout().addWidget(_label(f"{kept} result{'s' if kept != 1 else ''} kept for this step (never replaced; "
                                      "the newest full run counts, short tests never complete it)." if kept else
                                      "No result yet.", muted=True))
        if locked:
            box.layout().addWidget(_label(f"Locked until {locked} is done.", muted=True))
        box.layout().addLayout(self._actions(acts))         # the buttons after the explanation
        self.steps.addWidget(box)

    def _radio_running(self, r: dict) -> bool:
        return any(sub["state"] == "in progress" for s in r.get("steps") or [] for sub in s.get("sub") or [])

    # ---- actions
    def add_radio(self):
        d = AddRadioDialog(self.svc, self.d, self)
        if d.exec() and d.added:
            self.radio = (d.added or {}).get("radio")
            self.page = None
            self._key = None
            if d.added.get("status"):
                self.show_status(d.added["status"])
            else:
                self.refresh()

    def edit_radio(self):
        r = self._radio()
        if r is None:
            return
        d = EditRadioDialog(self.svc, r, self)
        if d.exec() and d.result:
            self._key = None
            if d.result.get("status"):
                self.show_status(d.result["status"])
            else:
                self.refresh()

    def _open_chain(self):
        self.open_chain.emit()
        self.accept()

    def accept_typical(self):
        if QMessageBox.question(self, "Keep the range?", "Keep the radio's range and blind spots as they are now? "
                                "They stay marked as typical or as entered, not measured.") != \
                QMessageBox.StandardButton.Yes:
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
                   if short else "Run the full measurement now? It opens the radio. It keeps running in the "
                                 "background if you move Back or close this guide."))
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

    def done(self, r):
        self._remember_page()
        super().done(r)

    def change_location(self):
        """Change or clear the location: written only to this station's station.local.json, never read back."""
        from marsad import setup as S
        d = QDialog(self)
        d.setWindowTitle("Station location")
        d.setObjectName("LocationDialog")
        v = QVBoxLayout(d)
        box = LocationBox("The current location is never shown. Type a new one, find it, or press Clear.")
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
        self._loc_dlg = d

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
                why.setText("Type a location or find it, or use Clear.")
                return
            do(loc)
        save.clicked.connect(on_save)
        clear.clicked.connect(lambda: do(None))
        d.finished.connect(lambda *_: box.clear())         # nothing found or typed outlives the dialog
        d.exec()
