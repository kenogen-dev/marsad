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
                               QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QProgressBar,
                               QPushButton, QScrollArea, QToolButton, QVBoxLayout, QWidget)

from marsad_desktop import theme as T
from marsad_desktop.scan_start import call

from marsad.setup import STEP_ORDER, STEP_TITLES, STEPS as STEPS_TEXT                         # noqa: E402
from marsad_desktop.setup_words import (PAGES, PASSED, TOUCHED, BTN_SHORT, BTN_FULL, BTN_ESTIMATE,  # noqa: E402
                                        page_group, finish_words)

STATE_COLOUR = {"done": "open", "in progress": "survey_light", "needs attention": "alert_text",
                "needs re-checking": "anomaly", "estimate kept": "anomaly", "to redo": "anomaly", "not started": "muted"}
STATE_MARK = {"done": "✓", "in progress": "…", "needs attention": "!", "needs re-checking": "↻", "estimate kept": "≈",
              "to redo": "↺", "not started": "○"}


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
    b.setAutoDefault(False)          # relay 026: Enter is the dialog's main action only (setDefault), never a stray one
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

    def __init__(self, parent=None, home: str | None = None, note: str | None = None):
        super().__init__(parent)
        self.setWindowTitle("Welcome to Marsad")
        self.setObjectName("FirstRunDialog")
        self.choice = None
        self.folder = None
        v = QVBoxLayout(self)
        v.setContentsMargins(T.SPACE * 3, T.SPACE * 3, T.SPACE * 3, T.SPACE * 2)
        v.addWidget(_label("Welcome to Marsad", size=14, bold=True))
        if note:                                      # relay 028: after Close Station
            n = _label(note)
            n.setObjectName("ClosedNote")
            v.addWidget(n)
        v.addWidget(_label("There is no station open yet. A station is a folder holding its radios, rigs, surveys and "
                           "recordings. Make a new one, or open one you already have" + (f" (such as {home})" if home
                                                                                        else "") + ".", muted=True))
        row = QHBoxLayout()
        b1 = _button("New Station…", primary=True)
        b2 = _button("Open an existing station…")
        b4 = _button("Stations…")                     # relay 028: the known list, with missing ones and Remove from list
        b4.setObjectName("KnownStations")
        b3 = _button("Quit")
        b1.clicked.connect(self._new)
        b2.clicked.connect(lambda: self._open(home))
        b4.clicked.connect(self._known)
        b3.clicked.connect(self.reject)
        for b in (b1, b2, b4):
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

    def _known(self):
        from marsad_desktop.stations_dialog import StationsDialog
        d = StationsDialog(self)
        self._stations_dlg = d
        if d.exec() and d.open_folder:
            self.choice, self.folder = "open", d.open_folder
            self.accept()


# ---------------------------------------------------------------- Add Radio

class HoldersBox(QFrame):
    """Relay 027: why Detect (or a measurement) could not finish, in plain words, with what to do. When Marsad's own
    setup measurement has been left behind holding a radio, "Stop the leftover measurement…" stops that one process
    after a confirmation; scans, recorders, SDR++ and other programs are only named, never touched (rule 1)."""

    def __init__(self, svc, parent=None):
        super().__init__(parent)
        self.svc = svc
        self.setObjectName("HoldersBox")
        self.setProperty("panel", True)
        v = QVBoxLayout(self)
        v.setContentsMargins(T.SPACE, T.SPACE // 2, T.SPACE, T.SPACE // 2)
        self.text = _label("", wrap=True)
        self.text.setObjectName("HoldersText")
        v.addWidget(self.text)
        row = QHBoxLayout()
        row.addStretch(1)
        self.b_stop = _button("Stop the leftover measurement…")
        self.b_stop.setObjectName("StopLeftover")
        self.b_stop.clicked.connect(self.stop)
        row.addWidget(self.b_stop)
        v.addLayout(row)
        self.leftovers: list = []
        self.hide()

    def show_answer(self, d: dict | None):
        d = d or {}
        if not d.get("problem"):
            self.hide()
            return
        self.leftovers = [h for h in d.get("holders") or [] if h.get("stoppable")]
        self.text.setText(d["problem"] + (f"\n\n{d['by_hand']}" if d.get("by_hand") else ""))
        self.b_stop.setVisible(bool(self.leftovers))
        self.show()

    def stop(self):
        if not self.leftovers:
            return
        h = self.leftovers[0]
        if QMessageBox.question(self, "Stop the leftover measurement?",
                                f"This stops {h['program']} (process {h['pid']}), which Marsad started for "
                                f"{h.get('purpose') or 'a setup measurement'} and which has outlived it. Nothing is "
                                "recording through it. Scans, recorders and other programs are never stopped.\n\n"
                                "Stop it?") != QMessageBox.StandardButton.Yes:
            return
        self.b_stop.setEnabled(False)

        def got(d, err):
            self.b_stop.setEnabled(True)
            self.leftovers = [x for x in (d or {}).get("holders") or [] if x.get("stoppable")]
            self.b_stop.setVisible(bool(self.leftovers))
            self.text.setText((err or (d or {}).get("text") or "") + ("\nThen try Detect again." if not err else ""))
        call(lambda: self.svc.setup_post("stop_leftover", {"pid": h["pid"], "confirmed": True}), got)


class AddRadioDialog(QDialog):
    """Add Radio. Relay 024: only what is known is shown as a value, and every value says where it came from:
    read from the radio (serial, product and maker strings, the detected tuner), fixed by the tuner type (the
    driver's gain table), an estimate (the tuner's typical range, greyed), or not measured yet (blind spots, retune
    time, warm-up, floor: never a value). Nothing here comes from another station.

    Relay 026: Detect fills in everything it found and selects the tuner, with no typing first. Several radios are
    listed by serial and product; after one is added the others are offered one at a time ("Add it" / "Skip"). A
    radio already in this station offers Edit; one removed earlier offers Restore. Cancel closes at any moment: a
    radio already added stays added, and the window says so."""

    def __init__(self, svc, status: dict, parent=None):
        super().__init__(parent)
        from marsad import setup as S
        self.S = S
        self.svc = svc
        self.status = status
        self.detected: dict = {}                # serial -> what the radio said (rtl_eeprom), in the order found
        self.added = None                       # the last radio added (or restored)
        self.added_all: list = []               # every radio added or restored in this window
        self.skipped: set = set()
        self.want_edit = None                   # a radio already in this station, to open in Edit
        self.offer = None                       # the detected radio offered next, after one was added
        self.tuner_from = None                  # "detected" once rtl_eeprom named it; "chosen" when picked by hand
        self.setWindowTitle("Add Radio")
        self.setObjectName("AddRadioDialog")
        v = QVBoxLayout(self)
        v.setContentsMargins(T.SPACE * 3, T.SPACE * 2, T.SPACE * 3, T.SPACE * 2)
        v.setSpacing(T.SPACE)
        v.addWidget(_label("Add Radio", size=13, bold=True))
        self.lead = _label("The RTL-SDRs Windows can see, by serial (nothing is opened to list them). Detect reads "
                           "each one's serial, product, maker and tuner from the radio itself and fills them in.",
                           muted=True)
        v.addWidget(self.lead)
        self.list = QListWidget()
        self.list.setObjectName("RadioList")
        self.list.setMaximumHeight(120)
        self.list.setMinimumHeight(64)          # relay 027: never squeezed to half a line when Detect explains itself
        v.addWidget(self.list)
        det = QHBoxLayout()
        self.b_detect = _button("Detect the radios…")
        self.b_detect.setToolTip("Asks each plugged-in radio its serial, maker, product and tuner with the engine's own "
                                 "probe (rtl_eeprom). It opens each radio for a moment, so nothing else may be using "
                                 "one.")
        self.b_detect.clicked.connect(self.detect)
        det.addWidget(self.b_detect)
        self.det_note = _label("", muted=True)
        det.addWidget(self.det_note, 1)
        v.addLayout(det)
        self.holders = HoldersBox(svc, self)          # relay 027: why Detect could not finish, and what to do
        v.addWidget(self.holders)
        # relay 026: after a radio is added, the next detected one is offered here, never in a box that traps
        self.next_box = QFrame()
        self.next_box.setObjectName("NextRadio")
        self.next_box.setProperty("panel", True)
        nb = QHBoxLayout(self.next_box)
        nb.setContentsMargins(T.SPACE, T.SPACE // 2, T.SPACE, T.SPACE // 2)
        self.next_text = _label("", wrap=True)
        nb.addWidget(self.next_text, 1)
        self.b_other = _button("Add it", primary=True)
        self.b_other.clicked.connect(self.take_other)
        self.b_skip = _button("Skip")
        self.b_skip.clicked.connect(self.skip_other)
        nb.addWidget(self.b_skip)
        nb.addWidget(self.b_other)
        self.next_box.hide()
        v.addWidget(self.next_box)
        g = QGridLayout()
        g.setColumnStretch(1, 1)
        g.setHorizontalSpacing(T.SPACE * 2)
        self.serial = QLineEdit()
        self.serial.setObjectName("RadioSerial")
        self.serial.setPlaceholderText("filled in by Detect, or type it")
        self.nick = QLineEdit()
        self.nick.setObjectName("RadioNick")
        self.nick.setPlaceholderText("a short name, e.g. radio1")
        self.tuner = QComboBox()
        self.tuner.setObjectName("RadioTuner")
        self.tuner.addItem("Elonics E4000 (zero-IF)", "e4000")
        self.tuner.addItem("Rafael Micro R820T / R860 (low-IF)", "r820t")
        self.product = QLineEdit()
        self.product.setReadOnly(True)
        self.maker = QLineEdit()
        self.maker.setPlaceholderText("optional: type the maker if you know it")
        self.src = {}
        rows = [("Serial", self.serial, "serial"), ("Nickname", self.nick, "nick"), ("Product", self.product, "product"),
                ("Maker", self.maker, "maker"), ("Tuner", self.tuner, "tuner")]
        for i, (k, w, key) in enumerate(rows):
            g.addWidget(_label(k, wrap=False), i, 0)
            g.addWidget(w, i, 1)
            self.src[key] = _label("", muted=True, wrap=False)
            g.addWidget(self.src[key], i, 2)
        v.addLayout(g)
        v.addWidget(_label("Known before any measurement", bold=True))
        self.known = QGridLayout()
        self.known.setColumnStretch(1, 1)
        self.known.setHorizontalSpacing(T.SPACE * 2)
        self.k_rows = {}
        for i, (key, word) in enumerate((("gains", "Gain steps"), ("range", "Tuning range"),
                                         ("gaps", "Blind spots (PLL gap)"), ("retune", "Retune time"),
                                         ("warmup", "Warm-up"), ("floor", "Its own floor"))):
            name = _label(word, wrap=False)
            val = _label("", wrap=True)
            val.setObjectName(f"Known_{key}")
            how = _label("", muted=True, wrap=True)
            self.known.addWidget(name, i, 0)
            self.known.addWidget(val, i, 1)
            self.known.addWidget(how, i, 2)
            self.k_rows[key] = (val, how)
        v.addLayout(self.known)
        self.typical = _label("", muted=True)
        self.typical.setObjectName("TypicalNote")
        v.addWidget(self.typical)
        self.why = _label("", muted=True)
        self.why.setObjectName("AddRadioWhy")
        v.addWidget(self.why)
        row = QHBoxLayout()
        row.addStretch(1)
        self.b_cancel = _button("Cancel")
        self.b_cancel.clicked.connect(self.reject)
        self.ok = _button("Add this radio", primary=True)
        self.ok.setDefault(True)                # Enter: the main action, when it is enabled
        self.ok.clicked.connect(self.save)
        row.addWidget(self.b_cancel)
        row.addWidget(self.ok)
        v.addLayout(row)
        self.tuner.activated.connect(self._tuner_chosen)
        self.tuner.currentIndexChanged.connect(self._typical)
        self.list.currentItemChanged.connect(self._picked)
        self.serial.textChanged.connect(self._serial_typed)
        self._fill()
        self._typical()
        self.resize(760, self.sizeHint().height())

    # ---- what the list shows
    def _rows(self) -> list:
        """One row per radio: what Detect found (serial order as found), else Windows' list. Each row says whether the
        radio is new, already this station's, or removed earlier."""
        mine = {str(r.get("serial")): r.get("radio") for r in (self.status.get("visible") or []) if r.get("radio")}
        for r in self.status.get("radios") or []:
            s = str(((r.get("facts") or {}).get("serial") or {}).get("value") or _serial_of(r) or "")
            if s:
                mine.setdefault(s, r["radio"])
        for nick, s in self._added_serials().items():
            mine[s] = nick
        gone = {str(a.get("serial")): a for a in self.status.get("archived") or []}
        out = []
        if self.detected:
            for s, f in self.detected.items():
                out.append({"serial": s, "product": f.get("product") or "", "tuner": f.get("tuner") or "",
                            "radio": f.get("radio") or mine.get(s), "archived": f.get("archived") or gone.get(s),
                            "detected": True})
        else:
            for r in self.status.get("visible") or []:
                s = str(r["serial"])
                out.append({"serial": s, "product": r.get("name") or "", "tuner": "",
                            "radio": r.get("radio") or mine.get(s), "archived": gone.get(s), "detected": False})
        for o in out:
            if o["serial"] in {a.get("serial") for a in self.added_all if a.get("restored")}:
                o["archived"] = None
        return out

    def _added_serials(self) -> dict:
        return {a["radio"]: a["serial"] for a in self.added_all if a.get("serial")}

    def _fill(self, select: str | None = None):
        self.list.blockSignals(True)
        self.list.clear()
        rows = self._rows()
        if not rows:
            vis = self.status.get("visible")
            it = QListWidgetItem("Windows would not list the radios here: use Detect, or type the serial below."
                                 if vis is None and not self.detected else "No RTL-SDR is plugged in.")
            it.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list.addItem(it)
        for o in rows:
            bits = [f"serial {o['serial']}", o["product"] or "RTL-SDR"]
            if o["tuner"]:
                bits.append(o["tuner"])
            if o["radio"]:
                bits.append(f"already added as {o['radio']}: Edit")
            elif o["archived"]:
                bits.append(f"removed earlier as {o['archived']['radio']}: Restore")
            elif o["serial"] in self.skipped:
                bits.append("skipped")
            it = QListWidgetItem("   ·   ".join(bits))
            it.setData(Qt.ItemDataRole.UserRole, o)
            self.list.addItem(it)
        self.list.blockSignals(False)
        want = select or self.serial.text().strip()
        row = next((i for i, o in enumerate(rows) if o["serial"] == want), None)
        if row is None:
            # nothing chosen yet: the first new radio, else the first one listed
            row = next((i for i, o in enumerate(rows) if not o["radio"] and not o["archived"]
                        and o["serial"] not in self.skipped), 0 if rows else None)
        if row is not None:
            self.list.setCurrentRow(row)
            self._picked(self.list.item(row))

    def _row_for(self, serial: str) -> dict | None:
        """The listed radio with this serial; for a typed serial that is not listed, still whether it was removed
        earlier (Restore first) or is already this station's."""
        o = next((o for o in self._rows() if o["serial"] == serial), None)
        if o is None and serial:
            gone = next((a for a in self.status.get("archived") or [] if str(a.get("serial")) == serial), None)
            mine = next((r["radio"] for r in self.status.get("radios") or []
                         if _serial_of(r) == serial), None) or {v: k for k, v in self._added_serials().items()}.get(serial)
            if gone or mine:
                o = {"serial": serial, "product": "", "tuner": "", "radio": mine, "archived": None if mine else gone,
                     "detected": False}
        return o

    def _picked(self, cur, _prev=None):
        o = cur.data(Qt.ItemDataRole.UserRole) if cur else None
        if not o:
            return
        if self.serial.text().strip() != o["serial"]:
            self.serial.blockSignals(True)
            self.serial.setText(o["serial"])
            self.serial.blockSignals(False)
            if not (o.get("archived") and not o.get("radio")):
                self.nick.clear()
        self._apply_detected()

    def _serial_typed(self, *_):
        self._apply_detected()

    def _apply_detected(self):
        """Everything the radio itself said (rtl_eeprom) for the serial in the box, filled in: product, maker (or
        that it reports none) and the tuner, selected in the list."""
        s = self.serial.text().strip()
        f = self.detected.get(s)
        if f:
            self.product.setText(f.get("product") or "")
            if (f.get("manufacturer") or "").strip():
                self.maker.setText(f["manufacturer"].strip())
                self.maker.setReadOnly(True)
            else:
                self.maker.clear()
                self.maker.setReadOnly(False)
                self.maker.setPlaceholderText("this radio doesn't report one: type it if you know it")
            if f.get("tuner_key"):
                self.tuner.blockSignals(True)
                self.tuner.setCurrentIndex(self.tuner.findData(f["tuner_key"]))
                self.tuner.blockSignals(False)
                self.tuner_from = "detected"
        else:
            if self.tuner_from == "detected":
                self.tuner_from = None
            self.maker.setReadOnly(False)
        o = self._row_for(s) or {}
        if o.get("radio"):
            self.ok.setText(f"Edit {o['radio']}…")
            self.why.setText(f"Serial {s} is already this station's {o['radio']}. Edit changes its nickname, tuner, "
                             "maker or product.")
        elif o.get("archived"):
            a = o["archived"]
            if not self.nick.text().strip():
                self.nick.setText(a["radio"])
            self.ok.setText(f"Restore {a['radio']}")
            self.why.setText(f"Serial {s} was this station's {a['radio']} until it was removed "
                             f"({str(a.get('removed') or '')[:16].replace('T', ' ')}). Restore brings it back with "
                             "its measurements and history.")
        else:
            self.ok.setText("Add this radio")
            self.why.setText("")
        self._typical()

    def _tuner_chosen(self, *_):
        f = self.detected.get(self.serial.text().strip()) or {}
        self.tuner_from = "detected" if f.get("tuner_key") == self.tuner.currentData() else "chosen"
        self._typical()

    def _typical(self, *_):
        """Every value with where it came from (relay 024)."""
        t = self.S.typical(self.tuner.currentData())
        f = self.detected.get(self.serial.text().strip())
        vis = {str(r.get("serial")): r for r in (self.status.get("visible") or [])}.get(self.serial.text().strip())
        self.src["serial"].setText("read from the radio" if f else ("from Windows' device list" if vis else
                                                                    "typed" if self.serial.text().strip() else ""))
        self.src["nick"].setText("yours to choose")
        if f:
            self.src["product"].setText("read from the radio")
        elif vis and not self.product.text():
            self.product.setText(vis.get("name") or "")
            self.src["product"].setText("from Windows' device list")
        elif not self.product.text():
            self.src["product"].setText("not read yet: Detect reads it")
        if f and not (f.get("manufacturer") or "").strip():
            self.src["maker"].setText("this radio doesn't report a maker name")
        elif f:
            self.src["maker"].setText("read from the radio")
        else:
            self.src["maker"].setText("not read yet: Detect reads it" if not self.maker.text() else "typed")
        self.src["tuner"].setText({"detected": "detected: read from the radio",
                                   "chosen": "chosen by you: tuner not confirmed by Detect"}.get(
            self.tuner_from or "", "not detected yet: Detect reads it, or pick it from the list (then it is "
                                   "\"tuner not confirmed by Detect\")"))
        label = t["tuner_label"]
        val, how = self.k_rows["gains"]
        val.setText(", ".join(f"{g:g}" for g in t["gains"]) + " dB")
        how.setText(f"from the {label}'s driver table (exact for any {label})")
        val, how = self.k_rows["range"]
        val.setText(f"about {t['range_mhz'][0]:g} to {t['range_mhz'][1]:g} MHz")
        val.setProperty("muted", True)
        val.setStyleSheet(f"color: {T.C['muted']};")
        how.setText(f"estimate: typical for {label}, not measured on this unit. The tuner sweep measures it.")
        for key, step in (("gaps", "the tuner sweep"), ("retune", "Retune time per hop"), ("warmup", "Warm-up drift"),
                          ("floor", "Its own floor")):
            val, how = self.k_rows[key]
            val.setText("not measured yet")
            val.setStyleSheet(f"color: {T.C['muted']};")
            how.setText(f"measured by {step} in Set up this station")
        self.typical.setText(f"Nothing here comes from another station. The range is an estimate (typical for "
                             f"{label}, not measured on this unit) until Measure the radio replaces it; no estimate "
                             "unlocks scanning.")

    # ---- Detect
    def detect(self):
        if QMessageBox.question(self, "Detect the radios?", "This opens each plugged-in radio for a moment to ask its "
                                "serial, maker, product and tuner (rtl_eeprom). It is refused while any radio program "
                                "is running.\n\nGo ahead?") != QMessageBox.StandardButton.Yes:
            return
        self.b_detect.setEnabled(False)
        self.det_note.setText("asking each radio (up to 15 s)…")
        self.holders.show_answer(None)

        def got(d, err):
            self.b_detect.setEnabled(True)
            if err:
                self.det_note.setText(f"Not detected: {err}")
                return
            if d.get("problem"):                         # relay 027: timed out, or a radio is held: why, in words
                self.det_note.setText("Not detected: see why below. The radio can still be added by hand.")
                self.holders.show_answer(d)
                self._typical()
                return
            self.detected = {str(f.get("serial")): f for f in d.get("found") or []}
            words = [f"index {f['device']}: serial {f.get('serial')}, {f.get('tuner') or 'tuner not said'}"
                     for f in d.get("found") or []]
            new = [s for s, f in self.detected.items() if not f.get("radio")]
            lead = (f"{len(self.detected)} radios found: pick one to add first; the others are offered after it. "
                    if len(new) > 1 else "")
            self.det_note.setText(lead + ("; ".join(words) or (d.get("error") or "no radio answered"))
                                  + (f"\n{d['duplicate_help']}" if d.get("duplicates") else ""))
            keep = self.serial.text().strip()
            self._fill(select=keep if keep in self.detected and not self.detected[keep].get("radio") else None)
        call(lambda: self.svc.setup_post("detect", {}), got)

    # ---- the main action: add, restore or edit
    def save(self):
        s = self.serial.text().strip()
        o = self._row_for(s) or {}
        if o.get("radio"):
            self.want_edit = o["radio"]
            self.accept()
            return
        self.ok.setEnabled(False)
        if o.get("archived"):
            body, route = {"id": o["archived"]["id"], "nickname": self.nick.text().strip()}, "radio_restore"
        else:
            body = {"serial": s, "nickname": self.nick.text().strip(), "tuner": self.tuner.currentData(),
                    "maker": self.maker.text().strip(), "product": self.product.text().strip(),
                    # relay 027: read from the radio by Detect, or picked by hand ("tuner not confirmed by Detect")
                    "tuner_confirmed": self.tuner_from == "detected"}
            route = "radio"

        def got(d, err):
            self.ok.setEnabled(True)
            if err:
                self.why.setText(err)
                return
            self.added = d
            self.added_all.append({"radio": d.get("radio"), "serial": s, "restored": route == "radio_restore"})
            if d.get("status"):
                self.status = d["status"]
            for f in self.detected.values():
                if str(f.get("serial")) == s:
                    f["radio"] = d.get("radio")
                    f["archived"] = None
            self._offer_next()
        call(lambda: self.svc.setup_post(route, body), got)

    def _others(self) -> list:
        return [o for o in self._rows() if o["detected"] and not o["radio"] and o["serial"] not in self.skipped]

    def _offer_next(self):
        """After one radio is added: the next detected one, one at a time, with Skip. None left: done."""
        left = self._others()
        if not left:
            self.offer = None
            self.accept()
            return
        o = self.offer = left[0]
        done = ", ".join(a["radio"] for a in self.added_all)
        what = ("Restore it" if o["archived"] else "Add it")
        self.b_other.setText(what)
        self.next_text.setText(f"✓ {done} added. "
                               + ("Add the other detected radio too? " if len(left) == 1 else
                                  f"Add the other {len(left)} detected radios too? Next: ")
                               + f"serial {o['serial']} · {o['product'] or 'RTL-SDR'}"
                               + (f" · {o['tuner']}" if o['tuner'] else "")
                               + (f" (removed earlier as {o['archived']['radio']})" if o["archived"] else ""))
        self.next_box.show()
        self.b_cancel.setText("Close")
        self._fill(select=self.serial.text().strip())
        self._set_form(False)
        self.why.setText("")                    # the offer above says it all; no "already added: Edit" below it
        self.ok.setText("Add this radio")

    def _set_form(self, on: bool):
        for w in (self.serial, self.nick, self.tuner, self.maker, self.ok, self.list, self.b_detect):
            w.setEnabled(on)

    def take_other(self):
        o = self.offer
        if not o:
            return
        self.next_box.hide()
        self._set_form(True)
        self.nick.clear()
        self._fill(select=o["serial"])
        self.nick.setFocus()

    def skip_other(self):
        if self.offer:
            self.skipped.add(self.offer["serial"])
        self.next_box.hide()
        self._set_form(True)
        self._offer_next()

    def reject(self):
        """Close / Cancel at any moment. Radios already added stay added: said here, never silently."""
        if self.added_all and not self.next_box.isVisible():
            names = ", ".join(a["radio"] for a in self.added_all)
            self.why.setText(f"{names} stay added; nothing else was saved.")
        super().reject()


# ---------------------------------------------------------------- Set up this station (relay 022: a guide)

class EditRadioDialog(QDialog):
    """Relay 022: step 1 reopened. Relay 026: Edit a radio's nickname, tuner, maker and product; the serial is shown
    read-only (it is how the radio is found, rule 8) with Re-detect, which reads the radio again and refreshes the rest.
    The range and blind spots can still be corrected by hand (relay 022). The steps that rest on what changes then
    read "needs re-checking"."""

    def __init__(self, svc, radio: dict, parent=None):
        super().__init__(parent)
        self.svc = svc
        self.nick = radio["radio"]
        self.result = None
        e = radio.get("entry") or {}
        self.setWindowTitle(f"Edit {self.nick}")
        self.setObjectName("EditRadioDialog")
        v = QVBoxLayout(self)
        v.setContentsMargins(T.SPACE * 3, T.SPACE * 2, T.SPACE * 3, T.SPACE * 2)
        v.setSpacing(T.SPACE)
        v.addWidget(_label(f"Edit {self.nick}", size=13, bold=True))
        v.addWidget(_label("Steps that rest on what you change (the measurements run on the radio's frequencies, the "
                           "path checks through the rig) will read \"needs re-checking\" until they are run again. "
                           "Nothing already recorded is removed.", muted=True))
        g = QGridLayout()
        g.setColumnStretch(1, 1)
        self.serial = QLineEdit(str(((radio.get("facts") or {}).get("serial") or {}).get("value") or
                                    _serial_of(radio) or ""))
        self.serial.setObjectName("EditSerial")
        self.serial.setReadOnly(True)
        self.serial.setToolTip("The serial can't be edited: it is how marsad finds this radio. Re-detect reads it, and "
                               "the rest, from the radio again.")
        self.b_redetect = QToolButton()
        self.b_redetect.setObjectName("Tool")
        self.b_redetect.setText("Re-detect…")
        self.b_redetect.setToolTip("Opens the plugged-in radios for a moment (rtl_eeprom) and refreshes the product, "
                                   "maker and tuner of this serial from the radio.")
        self.b_redetect.clicked.connect(self.redetect)
        ser = QHBoxLayout()
        ser.addWidget(self.serial, 1)
        ser.addWidget(self.b_redetect)
        self.nickname = QLineEdit(self.nick)
        self.nickname.setObjectName("EditNick")
        self.nick_note = _label("checking whether the nickname can still change…", muted=True)
        self.tuner = QComboBox()
        self.tuner.addItem("Elonics E4000 (zero-IF)", "e4000")
        self.tuner.addItem("Rafael Micro R820T / R860 (low-IF)", "r820t")
        self.tuner.setCurrentIndex(max(0, self.tuner.findData(e.get("tuner"))))
        lo, hi = (e.get("range_mhz") or [0, 0])[:2]
        self.lo, self.hi = QLineEdit(f"{lo:g}"), QLineEdit(f"{hi:g}")
        self.gaps = QLineEdit(", ".join(f"{a:g}-{b:g}" for a, b in e.get("gaps_mhz") or []))
        usb = e.get("usb") or {}
        self.maker, self.product = QLineEdit(usb.get("maker") or ""), QLineEdit(usb.get("product") or "")
        r = 0
        for k, w in [("Serial", ser), ("Nickname", self.nickname), ("", self.nick_note), ("Tuner", self.tuner),
                     ("Maker", self.maker), ("Product", self.product), ("Range low, MHz", self.lo),
                     ("Range high, MHz", self.hi), ("Blind spots (PLL gap), MHz", self.gaps)]:
            g.addWidget(_label(k, wrap=False), r, 0)
            if isinstance(w, QHBoxLayout):
                g.addLayout(w, r, 1)
            else:
                g.addWidget(w, r, 1)
            r += 1
        v.addLayout(g)
        self.det_note = _label("", muted=True)
        v.addWidget(self.det_note)
        self.holders = HoldersBox(svc, self)          # relay 027
        v.addWidget(self.holders)
        self.tuner_was = e.get("tuner")
        self.detected_key = None                      # the tuner Detect read from this radio, once it has
        if radio.get("tuner_confirmed") is False:
            self.det_note.setText("Tuner not confirmed by Detect (it was picked by hand). Re-detect confirms it.")
        self.why = _label("", muted=True)
        self.why.setObjectName("EditRadioWhy")
        v.addWidget(self.why)
        row = QHBoxLayout()
        row.addStretch(1)
        c = _button("Cancel")
        c.clicked.connect(self.reject)
        self.ok = _button("Save changes", primary=True)
        self.ok.setDefault(True)
        self.ok.clicked.connect(self.save)
        row.addWidget(c)
        row.addWidget(self.ok)
        v.addLayout(row)
        self.resize(600, self.sizeHint().height())
        self.nickname.setEnabled(False)
        call(lambda: self.svc.setup_post("radio_uses", {"radio": self.nick}), self._uses)

    def _uses(self, d, err):
        uses = (d or {}).get("uses") or []
        if err:
            self.nick_note.setText(f"The nickname stays as it is: the service could not say where it is used ({err}).")
        elif uses:
            self.nick_note.setText(f"The nickname stays {self.nick}: it is already used by " + ", ".join(uses) + ". "
                                   "Changing it would cut those links and stop later runs comparing with earlier ones.")
        else:
            self.nickname.setEnabled(True)
            self.nick_note.setText("Nothing uses this nickname yet (no run, rig, preset or saved scan), so it can still "
                                   "change.")

    def redetect(self):
        if QMessageBox.question(self, "Re-detect?", "This opens each plugged-in radio for a moment to read its serial, "
                                "maker, product and tuner again (rtl_eeprom). It is refused while any radio program "
                                "is running.\n\nGo ahead?") != QMessageBox.StandardButton.Yes:
            return
        self.b_redetect.setEnabled(False)
        self.det_note.setText("asking each radio (up to 15 s)…")
        self.holders.show_answer(None)

        def got(d, err):
            self.b_redetect.setEnabled(True)
            if err:
                self.det_note.setText(f"Not detected: {err}")
                return
            if d.get("problem"):                         # relay 027
                self.det_note.setText("Not detected: see why below. Nothing was changed.")
                self.holders.show_answer(dict(d, by_hand=""))
                return
            f = next((x for x in (d or {}).get("found") or [] if str(x.get("serial")) == self.serial.text().strip()),
                     None)
            if f is None:
                self.det_note.setText(f"No plugged-in radio reports serial {self.serial.text().strip()}: nothing was "
                                      "changed. Plug it in, or if it is a different radio, remove this one and add "
                                      "that one.")
                return
            self.product.setText(f.get("product") or "")
            self.maker.setText((f.get("manufacturer") or "").strip())
            if f.get("tuner_key"):
                self.tuner.setCurrentIndex(self.tuner.findData(f["tuner_key"]))
                self.detected_key = f["tuner_key"]
            self.det_note.setText(f"Read from the radio: {f.get('product') or 'no product name'}, "
                                  f"{(f.get('manufacturer') or '').strip() or 'no maker name'}, "
                                  f"{f.get('tuner') or 'tuner not said'}. Save changes keeps them.")
        call(lambda: self.svc.setup_post("detect", {}), got)

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
        body = {"radio": self.nick, "tuner": self.tuner.currentData(),
                "range_mhz": [lo, hi], "gaps_mhz": gaps, "maker": self.maker.text().strip(),
                "product": self.product.text().strip()}
        if self.nickname.isEnabled() and self.nickname.text().strip() != self.nick:
            body["nickname"] = self.nickname.text().strip()
        # relay 027: Detect read the tuner (confirmed), or it was changed by hand (not confirmed by Detect)
        if self.detected_key:
            body["tuner_confirmed"] = self.tuner.currentData() == self.detected_key
        elif self.tuner.currentData() != self.tuner_was:
            body["tuner_confirmed"] = False
        self.ok.setEnabled(False)

        def got(d, err):
            self.ok.setEnabled(True)
            if err:
                self.why.setText(err + " Nothing was changed.")
                return
            self.result = d
            self.accept()
        call(lambda: self.svc.setup_post("radio_update", body), got)


class RemovedRadiosDialog(QDialog):
    """Relay 026: Archived radios, each with Restore. Removed radios keep their measurements and history."""

    def __init__(self, svc, archived: list, parent=None):
        super().__init__(parent)
        self.svc = svc
        self.result = None
        self.setWindowTitle("Removed radios")
        self.setObjectName("RemovedRadiosDialog")
        v = QVBoxLayout(self)
        v.setContentsMargins(T.SPACE * 3, T.SPACE * 2, T.SPACE * 3, T.SPACE * 2)
        v.setSpacing(T.SPACE)
        v.addWidget(_label("Archived: removed radios", size=13, bold=True))
        v.addWidget(_label("Each one kept its measurements and setup history, and every run that used it is "
                           "untouched. Restore puts it back on this station's list.", muted=True))
        self.list = QListWidget()
        self.list.setObjectName("ArchivedRadios")
        for a in archived:
            it = QListWidgetItem(f"{a['radio']}   ·   serial {a.get('serial') or '?'}   ·   "
                                 f"{a.get('product') or a.get('tuner') or 'RTL-SDR'}   ·   removed "
                                 f"{str(a.get('removed') or '')[:16].replace('T', ' ')}   ·   "
                                 f"{a.get('results') or 0} results kept")
            it.setData(Qt.ItemDataRole.UserRole, a)
            self.list.addItem(it)
        if archived:
            self.list.setCurrentRow(0)
        v.addWidget(self.list)
        self.why = _label("", muted=True)
        v.addWidget(self.why)
        row = QHBoxLayout()
        row.addStretch(1)
        c = _button("Close")
        c.clicked.connect(self.reject)
        self.ok = _button("Restore", primary=True)
        self.ok.setDefault(True)
        self.ok.setEnabled(bool(archived))
        self.ok.clicked.connect(self.restore)
        row.addWidget(c)
        row.addWidget(self.ok)
        v.addLayout(row)
        self.resize(640, 320)

    def restore(self):
        it = self.list.currentItem()
        a = it.data(Qt.ItemDataRole.UserRole) if it else None
        if not a:
            return
        self.ok.setEnabled(False)

        def got(d, err):
            self.ok.setEnabled(True)
            if err:
                self.why.setText(err)
                return
            self.result = d
            self.accept()
        call(lambda: self.svc.setup_post("radio_restore", {"id": a["id"]}), got)


PROFILE = (("range_mhz", "tuning range"), ("gaps_mhz", "blind spots"), ("retune_ms_per_hop", "retune time"),
           ("warmup_min", "warm-up"), ("floor_db", "its own floor"))


def profile_words(radio: dict) -> str:
    """Relay 024: the radio's profile in words, each figure with what it is (measured, an estimate, entered), and
    every figure not measured yet said so, never left out."""
    facts = radio.get("facts") or {}
    read = radio.get("read") or {}
    bits = []
    for key, word in PROFILE:
        f = facts.get(key)
        if not f or f.get("kind") == "not measured" or (key == "gaps_mhz" and f.get("source") == "not measured yet"):
            bits.append(f"{word}: not measured yet")
            continue
        v = f.get("value")
        if key == "range_mhz":
            v = f"{v[0]:g}–{v[1]:g} MHz"
        elif key == "gaps_mhz":
            v = ", ".join(f"{a:g}–{b:g} MHz" for a, b in v) or "none"
        elif key == "retune_ms_per_hop":
            v = f"{v} ms per hop"
        elif key == "warmup_min":
            v = f"{v} min"
        elif key == "floor_db":
            v = f"{v} dB (relative)"
        bits.append(f"{word}: {v} ({f.get('source')})")
    if "maker" in read and not read.get("maker"):
        bits.append("maker: this radio doesn't report a maker name")
    return "This radio's profile: " + "; ".join(bits) + "."


def _serial_of(radio: dict) -> str:
    detail = next((s.get("detail") or "" for s in radio.get("steps") or [] if s.get("step") == "add"), "")
    return detail.replace("serial ", "", 1) if detail.startswith("serial ") else ""


def _clock(sec) -> str:
    if sec is None:
        return "?"
    sec = int(round(max(0.0, float(sec))))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


SPINNER = "◐◓◑◒"
OUTCOME_LOOK = {"done": ("✓", "open", "Done"), "attention": ("⚠", "anomaly", "Needs attention"),
                "failed": ("✗", "alert_text", "Failed"), "cancelled": ("■", "muted", "Cancelled")}


class RunPanel(QFrame):
    """Relay 025: one measurement's run, shown the same way for every step.

    While it runs: a progress bar with %, what it is doing now, the time gone and left (and where those figures come
    from: the run's own clock, the pace so far, or an estimate from the time), Cancel, and the last lines of its log
    (collapsible) with Open full log. When it ends: one result, done (with the figures), needs attention (what went
    wrong, why it probably did, what to do) or failed (the error and the log). Updated in place, so the log stays
    open or closed as left."""
    cancel = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("RunPanel")
        self.view: dict = {}
        self._log_open = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(T.SPACE, T.SPACE * 0.75, T.SPACE, T.SPACE * 0.75)
        lay.setSpacing(T.SPACE * 0.5)
        self.head = _label("", size=11, bold=True)
        self.head.setObjectName("RunHead")
        lay.addWidget(self.head)
        self.bar = QProgressBar()
        self.bar.setObjectName("RunProgress")
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(True)
        lay.addWidget(self.bar)
        self.activity = _label("")
        self.activity.setObjectName("RunActivity")
        lay.addWidget(self.activity)
        self.times = _label("", muted=True)
        self.times.setObjectName("RunTimes")
        lay.addWidget(self.times)
        self.what = _label("")
        self.what.setObjectName("RunWhat")
        lay.addWidget(self.what)
        self.why = _label("", muted=True)
        self.why.setObjectName("RunWhy")
        lay.addWidget(self.why)
        self.todo = _label("")
        self.todo.setObjectName("RunAdvice")
        self.todo.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(self.todo)
        row = QHBoxLayout()
        self.b_cancel = _button("Cancel this run")
        self.b_cancel.setObjectName("RunCancel")
        self.b_cancel.clicked.connect(self.cancel.emit)
        self.b_log = QToolButton()
        self.b_log.setObjectName("RunLogToggle")
        self.b_log.clicked.connect(self._toggle_log)
        self.b_full = _button("Open full log")
        self.b_full.setObjectName("RunOpenLog")
        self.b_full.clicked.connect(self.open_log)
        row.addWidget(self.b_cancel)
        row.addStretch(1)
        row.addWidget(self.b_log)
        row.addWidget(self.b_full)
        lay.addLayout(row)
        self.log = QPlainTextEdit()
        self.log.setObjectName("RunLog")
        self.log.setReadOnly(True)
        self.log.setFont(T.mono_font(8.5))
        self.log.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.log.setFixedHeight(130)
        lay.addWidget(self.log)
        self._log_shown()

    def _toggle_log(self):
        self._log_open = not self._log_open
        self._log_shown()

    def _log_shown(self):
        self.log.setVisible(self._log_open)
        self.b_log.setText("Hide log ▴" if self._log_open else "Show log ▾")

    def open_log(self):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        p = self.view.get("log")
        if p and Path(p).exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(p))

    def show_view(self, v: dict | None, spin: str = ""):
        self.view = v or {}
        self.setVisible(bool(v))
        if not v:
            return
        kind = "short test" if v.get("short") else "measurement"
        running = v.get("state") in ("running", "cancelling")
        for w in (self.bar, self.activity, self.times, self.b_cancel):
            w.setVisible(running)
        for w in (self.what, self.why, self.todo):
            w.setVisible(not running)
        lines = v.get("lines") or v.get("error_lines") or []
        if self.log.toPlainText() != "\n".join(lines):
            self.log.setPlainText("\n".join(lines))
            self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())
        self.b_log.setVisible(bool(lines))
        self.b_full.setEnabled(bool(v.get("log")) and Path(v.get("log") or "").exists())
        if running:
            p = v.get("progress") or {}
            pct = p.get("pct")
            est = p.get("basis") == "estimated from the time"
            if v["state"] == "cancelling":
                self.head.setText(f"{spin} Cancelling this {kind}: stopping it cleanly…")
            else:
                self.head.setText(f"{spin} Running: {kind}")
            if pct is None:
                self.bar.setRange(0, 0)                          # busy, no figure yet
            else:
                self.bar.setRange(0, 1000)
                self.bar.setValue(int(pct * 10))
                self.bar.setFormat(f"{pct:.0f}%" + (" (estimated)" if est else ""))
            self.activity.setText("Now: " + (p.get("activity") or "working"))
            left = p.get("remaining_s")
            basis = p.get("basis")
            self.times.setText(f"{_clock(p.get('elapsed_s'))} gone"
                               + (f" · about {_clock(left)} left" if left is not None else " · time left not known yet")
                               + (f" ({basis})" if basis else "")
                               + ". It keeps running if you close this guide or the window.")
            self.b_cancel.setEnabled(v["state"] == "running")
            return
        icon, colour, word = OUTCOME_LOOK.get(v.get("outcome"), ("•", "muted", "Ended"))
        col = T.C.get(colour, T.C["muted"])
        at = f" ({str(v.get('at'))[:16].replace('T', ' ')})" if v.get("at") else ""
        if v.get("short"):           # relay 029: a short test is a check, never the step's result
            word = "Short test OK" if v.get("outcome") == "done" else f"Short test: {word.lower()}"
            self.head.setText(f"<span style='color:{col}'>{icon}</span>&nbsp; {word}{at}. It is a check only and "
                              "doesn't finish the step.")
        else:
            self.head.setText(f"<span style='color:{col}'>{icon}</span>&nbsp; {word}: last {kind}{at}")
        self.what.setText(v.get("what") or "")
        self.why.setText(("Why: " + v["why"]) if v.get("why") else "")
        self.why.setVisible(bool(v.get("why")))
        adv = v.get("advice") or []
        self.todo.setText(("<b>What to do:</b><ul style='margin:0'>" + "".join(f"<li>{a}</li>" for a in adv) + "</ul>")
                          if adv else "")
        self.todo.setVisible(bool(adv))


def _steps_key(r: dict | None):
    """The steps as far as the page's layout is concerned: a run's progress changes every poll and is updated in
    place, so only whether a run is going and how it ended rebuild the page."""
    if not r:
        return None
    out = []
    for st in r.get("steps") or []:
        sub = []
        for x in st.get("sub") or []:
            run = x.get("run") or {}
            sub.append({k: v for k, v in x.items() if k != "run"}
                       | {"run": (run.get("job"), run.get("state"), run.get("outcome"))})
        out.append({k: v for k, v in st.items() if k != "sub"} | {"sub": sub})
    return out


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
        self.b_removed = _button("Removed radios…")               # relay 026: the Archived section, with Restore
        self.b_removed.setObjectName("RemovedRadios")
        self.b_removed.clicked.connect(self.removed_radios)
        self.b_removed.hide()
        top.addWidget(self.b_removed)
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
        self.b_next.setDefault(True)                # relay 026: Enter means Next, when it is enabled
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
        self.run_panel = RunPanel()                 # relay 025: kept across refreshes, moved onto each step's page
        self.run_panel.hide()
        self.run_panel.cancel.connect(self.cancel_run)
        self._spin_i = 0
        self.spin_timer = QTimer(self)
        self.spin_timer.timeout.connect(self._spin)
        self.spin_timer.start(160)
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
        n_arch = len(d.get("archived") or [])
        self.b_removed.setVisible(bool(n_arch))
        self.b_removed.setText(f"Removed radios ({n_arch})…")
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
        return all(self.page_state(k)[0] in PASSED for k in keys[:keys.index(key)])

    def blocker(self, key: str) -> str | None:
        """The first unfinished page before this one, in words, or None."""
        keys = [k for _, k in PAGES]
        for k in keys[:keys.index(key)]:
            if self.page_state(k)[0] not in PASSED:
                return self._page_title(k)
        return None

    def frontier(self) -> str:
        """The first page not done (the last page when all are)."""
        return next((k for _, k in PAGES if self.page_state(k)[0] not in PASSED), PAGES[-1][1])

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
        if i + 1 < len(keys) and self.page_state(self.page)[0] in PASSED:
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
        key = repr((self.radio, self.page, _steps_key(r), r and r.get("facts"), r and r.get("entry"),
                    [(j.get("id"), j.get("cancel_requested")) for j in self.d.get("jobs") or []]))
        if key == self._key:
            self._update_run()
            return
        self._key = key
        self.body.setUpdatesEnabled(False)
        try:
            self._fill_list()
            self.run_panel.hide()
            self.run_panel.setParent(None)          # out of the page before it is cleared, so it survives
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
        self._update_run()

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
            mark = self._mark(key, state) if open_ else "\U0001F512"
            tail = name + ("   ◀" if key == self.page else "")
            it = QListWidgetItem(f"{mark}  {tail}")
            it.setData(Qt.ItemDataRole.UserRole, key)
            it.setData(Qt.ItemDataRole.UserRole + 1, state)
            it.setData(Qt.ItemDataRole.UserRole + 2, tail if open_ and state == "in progress" else None)
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
        self.b_next.setEnabled(state in PASSED and not last)
        r = self._radio()
        if r is not None and r.get("ready"):
            text = "Every step is done: scanning is unlocked for this radio."
        elif state == "done":
            text = "Done. Next goes on to " + (self._page_title(keys[i + 1]) if not last else "the end") + "."
        elif state == "estimate kept":
            text = ("The estimate is kept, so you can go on. Scanning stays locked until the tuner sweep measures the "
                    "range: come back and run it.")
        elif state == "in progress":
            text = ("Still needed: this run to finish. It keeps running in the background if you go Back or close "
                    "the guide.")
        else:
            words = finish_words(self.page, state, self._sub(self.page).get("run"))
            if state == "needs re-checking":
                text = (("Still needed: do it again" if self.page in ("add", "draw", "confirm") else
                         "Still needed: run it again") + " (what changed is said above). "
                        + words["finish"].replace("To finish this step: ", "To finish it: "))
            elif state == "to redo":
                text = ("Still needed: run it again. This radio's setup is being redone; the earlier result is kept "
                        "in its history.")
            elif state == "needs attention" and (self._sub(self.page).get("run") or {}).get("state") != "ended" \
                    and detail and self.page not in ("add", "draw", "confirm"):
                text = words["needed"] + " " + detail
            else:
                text = words["needed"]
            b = self.blocker(self.page)
            if b and state not in TOUCHED:
                text = f"Locked: finish {b} first."
        self.needed.setText(text)
        self.needed.setProperty("state", state)
        self.b_next.setToolTip("" if self.b_next.isEnabled() else text)   # relay 029: the same words as the line

    @staticmethod
    def _empty(lay):
        while lay.count():
            it = lay.takeAt(0)
            if it.widget() is not None:
                it.widget().hide()
                it.widget().deleteLater()
            elif it.layout() is not None:
                SetupDialog._empty(it.layout())

    def _page_box(self, key, title, state, detail, actions, actions_last=False, mark=None):
        f = QFrame()
        f.setObjectName("TextPanel")
        lay = QVBoxLayout(f)
        lay.setContentsMargins(T.SPACE * 1.5, T.SPACE, T.SPACE * 1.5, T.SPACE)
        h = QHBoxLayout()
        col = T.C[STATE_COLOUR.get(state, "muted")]
        h.addWidget(_label(f"<span style='color:{col}'>{mark or STATE_MARK.get(state, '○')}</span>&nbsp; {title}",
                           size=11.5, bold=True, wrap=False))
        st = _label("failed" if mark == "✗" else state, wrap=False)       # relay 025: the word follows the mark
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
            if r.get("tuner_confirmed") is False:          # relay 027: added by hand when Detect could not finish
                lines.append("tuner not confirmed by Detect (Edit this radio, then Re-detect, confirms it)")
            busy = self._radio_running(r)
            box = self._page_box("add", f"Step 1. {STEP_TITLES['add']}: {self.radio}", state, "; ".join(lines),
                                 [("Edit this radio…", self.edit_radio, not busy),
                                  ("Redo this radio's setup…", self.redo_radio, not busy),
                                  ("Remove…", self.remove_radio, not busy),
                                  ("Add another radio…", self.add_radio, True)])
            box.layout().addWidget(_label(profile_words(r), muted=True))
            box.layout().addWidget(_label("Editing the radio marks the steps that rest on it \"needs re-checking\": "
                                          "the measurements if its tuner or frequencies change, the path checks if its "
                                          "tuner does. Redo starts its checklist again from step 1, keeping every "
                                          "earlier result in its history. Remove takes it off this station's list; its "
                                          "measurements and every run that used it are kept, and Restore brings it "
                                          "back." + ("\nA setup step is running on this radio: Edit, Redo and Remove "
                                                     "wait until it ends or is cancelled." if busy else ""),
                                          muted=True))
            self.steps.addWidget(box)
            return
        if key in ("draw", "confirm"):
            acts = ([("Open Chain", self._open_chain, True)] if key == "draw" else
                    [("It's built as drawn…", self.confirm_rig, self.page_state("draw")[0] == "done")])
            box = self._page_box(key, f"Step {3 if key == 'draw' else 4}. {STEP_TITLES[key]}", state, detail, acts,
                                 actions_last=True)
            if state != "done":
                box.layout().addWidget(self._finish_label(key, state))
            box.layout().addLayout(self._actions(acts))
            self.steps.addWidget(box)
            return
        s = (r.get("texts") or {}).get(key) or STEPS_TEXT.get(key) or {}     # relay 024: for this radio's tuner
        n = STEP_ORDER.index(next(st for st, k in PAGES if k == key)) + 1
        group = page_group(key)
        acts = [(BTN_SHORT, lambda *_, k=key: self.run_step(k, True), not running and not locked),
                (BTN_FULL.get(group, BTN_FULL["measure"]), lambda *_, k=key: self.run_step(k, False),
                 not running and not locked)]
        if key == "range" and state != "done":
            acts.append((BTN_ESTIMATE, self.accept_typical, not running and not locked))
        ended = (self._sub(key).get("run") or {}).get("state") in ("running", "cancelling", "ended")
        box = self._page_box(key, f"Step {n}. {s.get('title', key)}", state,
                             "" if ended and state in ("needs attention", "in progress") else detail, acts,
                             actions_last=True, mark=None if state == "in progress" else self._mark(key, state))
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
        box.layout().addWidget(self._finish_label(key, state))      # relay 029: right above the buttons
        box.layout().addLayout(self._actions(acts))         # the buttons after the explanation
        box.layout().addWidget(self.run_panel)              # relay 025: this step's run, running or ended
        self.steps.addWidget(box)

    def _finish_label(self, key: str, state: str):
        w = _label(finish_words(key, state, self._sub(key).get("run"))["finish"])
        w.setObjectName("StepFinish")
        return w

    def _sub(self, key: str) -> dict:
        r = self._radio() or {}
        return next((x for st in r.get("steps") or [] for x in st.get("sub") or [] if x["step"] == key), {})

    def _mark(self, key: str, state: str) -> str:
        """Relay 025: a spinner while a step runs; ✓, ⚠ or ✗ by how its last full run ended."""
        if state == "in progress":
            return SPINNER[self._spin_i % len(SPINNER)]
        run = self._sub(key).get("run") or {}
        if state == "needs attention":
            return "✗" if run.get("outcome") == "failed" and not run.get("short") else "⚠"
        return STATE_MARK.get(state, "○")

    def _spin(self):
        self._spin_i += 1
        ch = SPINNER[self._spin_i % len(SPINNER)]
        for i in range(self.steps_list.count()):
            it = self.steps_list.item(i)
            tail = it.data(Qt.ItemDataRole.UserRole + 2)
            if tail:
                it.setText(f"{ch}  {tail}")
        v = self.run_panel.view
        if self.run_panel.isVisible() and v.get("state") in ("running", "cancelling"):
            self.run_panel.show_view(v, ch)

    def _update_run(self):
        """Fill the run panel for the page shown, and poll faster while anything runs."""
        r = self._radio()
        running = any(x.get("state") == "in progress" for st in (r or {}).get("steps") or []
                      for x in st.get("sub") or [])
        want = 1000 if running else 2500
        if self.timer.interval() != want:
            self.timer.setInterval(want)
        if self.run_panel.parent() is None:
            return
        self.run_panel.show_view(self._sub(self.page).get("run"), SPINNER[self._spin_i % len(SPINNER)])

    def cancel_run(self):
        step = self.page
        if QMessageBox.question(self, "Cancel this run?", "Stop this measurement now?\n\nOnly this run stops: any "
                                "other scan or recording carries on. Nothing it found so far is kept, and the step "
                                "stays as it was before.") != QMessageBox.StandardButton.Yes:
            return
        self.run_panel.b_cancel.setEnabled(False)

        def got(d, err):
            if err:
                QMessageBox.information(self, "Not cancelled", err)
                self.run_panel.b_cancel.setEnabled(True)
                return
            self.show_status(d)
        call(lambda: self.svc.setup_post("cancel", {"radio": self.radio, "step": step}), got)

    def _radio_running(self, r: dict) -> bool:
        return any(sub["state"] == "in progress" for s in r.get("steps") or [] for sub in s.get("sub") or [])

    # ---- actions
    def add_radio(self):
        d = AddRadioDialog(self.svc, self.d, self)
        d.exec()
        if d.added:                               # relay 026: added or restored, even if the window was then closed
            self.radio = (d.added_all[0] if d.added_all else d.added).get("radio")
            self.page = None
            self._key = None
            if d.added.get("status"):
                self.show_status(d.added["status"])
            else:
                self.refresh()
        if d.want_edit:                           # a radio already in this station: Edit it
            self.radio = d.want_edit
            self.page = "add"
            self._key = None
            self.refresh()
            QTimer.singleShot(0, self.edit_radio)

    def edit_radio(self):
        r = next((x for x in self.d.get("radios") or [] if x["radio"] == self.radio), None)
        if r is None:
            return
        d = EditRadioDialog(self.svc, r, self)
        if d.exec() and d.result:
            self._key = None
            self.radio = d.result.get("radio") or self.radio
            if d.result.get("status"):
                self.show_status(d.result["status"])
            else:
                self.refresh()

    def redo_radio(self):
        if QMessageBox.question(self, "Redo this radio's setup?", f"Start {self.radio}'s checklist again from step 1?"
                                "\n\nEvery earlier measurement and check is kept in its history, with its date; the "
                                "steps read \"to redo\" until they are run again, and scanning on this radio is "
                                "locked until they are.") != QMessageBox.StandardButton.Yes:
            return

        def got(d, err):
            if err:
                QMessageBox.information(self, "Not redone", err)
                return
            self.page = None
            self._key = None
            self.show_status(d)
            self.go("add")
        call(lambda: self.svc.setup_post("radio_redo", {"radio": self.radio}), got)

    def remove_radio(self):
        nick = self.radio
        if QMessageBox.question(self, "Remove radio", f"Remove radio '{nick}'?\n\nIts measurements and every run that "
                                "used it are kept. You can restore it from Archived (Removed radios…). Rigs with a path "
                                "to it keep that path, flagged in Checks.") != QMessageBox.StandardButton.Yes:
            return

        def got(d, err):
            if err:
                QMessageBox.information(self, "Not removed", err)
                return
            self.radio = None
            self.page = None
            self._key = None
            self.foot.setText(f"{nick} was removed; it is kept under Removed radios, with its history.")
            self.show_status(d.get("status") or {})
        call(lambda: self.svc.setup_post("radio_remove", {"radio": nick}), got)

    def removed_radios(self):
        d = RemovedRadiosDialog(self.svc, self.d.get("archived") or [], self)
        if d.exec() and d.result:
            self.radio = d.result.get("radio")
            self.page = None
            self._key = None
            self.foot.setText(f"{self.radio} is back, with its measurements and history.")
            self.show_status(d.result.get("status") or {})

    def _open_chain(self):
        self.open_chain.emit()
        self.accept()

    def accept_typical(self):
        if QMessageBox.question(self, "Keep the estimate?", "Keep the radio's range as it is now, an estimate (the "
                                "tuner's typical figures, or what you entered), instead of measuring it?\n\nThe guide "
                                "can go on, but scanning stays locked until the tuner sweep measures the range: no "
                                "estimate unlocks scanning.") != QMessageBox.StandardButton.Yes:
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
        r = self._radio() or {}
        s = (r.get("texts") or {}).get(step) or (self.d.get("steps") or {}).get(step) or {}
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
                self._not_started(err)
                return
            self.foot.setText(("Started the short test" if short else "Started the full measurement")
                              + ": its progress shows on this page. Its log: "
                              + Path((d.get("job") or {}).get("log", "") or "").name)
            if d.get("status"):
                self.show_status(d["status"])
        call(lambda: self.svc.setup_post("run", {"radio": self.radio, "step": step, "short": short}), got)

    def _not_started(self, err: str):
        """Relay 027: a measurement refused because a radio is held. When Marsad's own leftover setup process is what
        holds it, offer to stop that one (asked first); anything else is only named."""
        def got(d, e2):
            left = [h for h in (d or {}).get("leftovers") or []]
            if e2 or not left:
                QMessageBox.information(self, "Not started", err)
                return
            h = left[0]
            if QMessageBox.question(self, "Not started",
                                    f"{err}\n\nWhat holds the radio is Marsad's own leftover: {h['text']}\n\nStop the "
                                    "leftover measurement? Scans, recorders and other programs are never stopped.") \
                    != QMessageBox.StandardButton.Yes:
                return
            call(lambda: self.svc.setup_post("stop_leftover", {"pid": h["pid"], "confirmed": True}),
                 lambda r, e3: self.foot.setText(e3 or (r or {}).get("text") or ""))
        call(lambda: self.svc.setup_post("holders", {}), got)

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
        save.setDefault(True)                # relay 026: Enter saves
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
