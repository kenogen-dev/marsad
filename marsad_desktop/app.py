"""Marsad's window: the shell (a menu bar, the header with the screens) and its screens.

The window is a viewer and editor: it reads and saves through the background service, so closing it never touches
a scan.

    .venv-desktop\\Scripts\\python.exe -m marsad_desktop.app

Relay 018: a Windows menu bar along the top, whose menus follow the screen shown (Chain's File, Edit, View, Arrange,
Tools and Help; Live Scan's own File, View, Tools and Help); a larger MARSAD with "observatory" as a second line; the
header's middle used for a station summary (radios plugged in, the service); tabs, Settings and the service
indicator on one line. Both screens open blank (Settings can change that).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QRectF, QSize, Qt, QTimer                          # noqa: E402
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QFontMetricsF, QLinearGradient, QPainter,  # noqa: E402
                           QPainterPath)
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QDialog, QFrame, QHBoxLayout,  # noqa: E402
                               QLabel, QMainWindow, QPushButton, QStackedWidget, QToolButton, QVBoxLayout, QWidget)

from marsad_desktop import theme as T                            # noqa: E402
from marsad_desktop.chain_screen import ChainScreen, prefs       # noqa: E402
from marsad_desktop.live_screen import LiveScreen                # noqa: E402
from marsad_desktop.service import Service                       # noqa: E402

SCREENS = ["Live Scan", "Captures", "Chain", "Probe", "Archive"]
HEADER_H = 52                       # px: the header's one line (tabs, summary, Settings, service), centred on it


class NotYet(QWidget):
    def __init__(self, name: str):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.addStretch(1)
        t = QLabel(f"{name} is not built yet")
        t.setFont(T.ui_font(15, QFont.Weight.DemiBold))
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        s = QLabel("Until it is, the web page does this, as it always has.")
        s.setProperty("muted", True)
        s.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(t)
        lay.addWidget(s)
        lay.addStretch(2)

    def build_menus(self, settings, about) -> list:
        from marsad_desktop.chain_screen import Menu
        m = Menu(self)
        m.setTitle("&Help")
        a = m.addAction("About Marsad")
        a.triggered.connect(lambda *_: about())
        t = Menu(self)
        t.setTitle("&Tools")
        a = t.addAction("Settings…")
        a.triggered.connect(lambda *_: settings())
        return [t, m]


class Logo(QWidget):
    """Relay 018: MARSAD larger, with a soft highlight within the palette (white easing into the light accent, never
    glossy) and "observatory" as a refined second line, letter-spaced, in the muted colour."""

    def __init__(self):
        super().__init__()
        self.f1 = T.ui_font(17, QFont.Weight.Bold)
        self.f1.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 2.4)
        self.f2 = T.ui_font(7.5, QFont.Weight.DemiBold)
        self.f2.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 2.2)
        m1, m2 = QFontMetricsF(self.f1), QFontMetricsF(self.f2)
        self._w = int(max(m1.horizontalAdvance("MARSAD"), m2.horizontalAdvance("OBSERVATORY")) + 6)
        self.setFixedSize(self._w, HEADER_H - 8)
        self.setToolTip("Marsad (المرصد): the observatory")
        self.setAccessibleName("Marsad observatory")

    def sizeHint(self):
        return QSize(self._w, HEADER_H - 8)

    def paintEvent(self, ev):
        qp = QPainter(self)
        qp.setRenderHint(QPainter.RenderHint.Antialiasing)
        qp.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        m1 = QFontMetricsF(self.f1)
        base = 4 + m1.ascent()
        path = QPainterPath()
        path.addText(1, base, self.f1, "MARSAD")
        r = path.boundingRect()
        g = QLinearGradient(r.topLeft(), r.bottomLeft())
        g.setColorAt(0.0, QColor(T.C["text"]))
        g.setColorAt(0.55, QColor(T.mix(T.C["text"], T.C["survey_light"], 0.45)))
        g.setColorAt(1.0, QColor(T.mix(T.C["survey_light"], T.C["survey"], 0.35)))
        qp.setPen(Qt.PenStyle.NoPen)
        qp.setBrush(T.color("shadow", 0.45))                  # a soft depth: the word's own shadow, 1 px down
        qp.drawPath(path.translated(0, 1.2))
        qp.setBrush(g)
        qp.drawPath(path)
        qp.setFont(self.f2)
        qp.setPen(T.color("muted"))
        qp.drawText(QRectF(1.5, base + 3, self.width(), 14), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop),
                    "OBSERVATORY")
        qp.end()


class SettingsDialog(QDialog):
    """Tools > Settings (and the header's Settings button): the window's own choices, kept in its preferences
    file in the home folder, never in the station."""

    def __init__(self, parent=None):
        super().__init__(parent)
        from marsad_desktop import explain as X
        self.setWindowTitle("Settings")
        self.setObjectName("SettingsDialog")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(T.SPACE * 3, T.SPACE * 3, T.SPACE * 3, T.SPACE * 2)
        lay.setSpacing(T.SPACE)
        head = QLabel("Settings")
        head.setFont(T.ui_font(12, QFont.Weight.DemiBold))
        lay.addWidget(head)
        q = prefs()

        def box(text, tip, key, default, slot=None):
            c = QCheckBox(text)
            c.setToolTip(tip)
            c.setChecked(q.value(key, default, type=bool) if key else default)
            c.toggled.connect(slot or (lambda on, k=key: prefs().setValue(k, on)))
            lay.addWidget(c)
            sub = QLabel(tip)
            sub.setProperty("muted", True)
            sub.setWordWrap(True)
            sub.setFont(T.ui_font(8.5))
            sub.setContentsMargins(26, 0, 0, T.SPACE)
            lay.addWidget(sub)
            return c

        self.explain = box("Show explanations", "The ⓘ marks and the longer tips. Off leaves the one-line tips.",
                           None, X.shown(), X.set_shown)
        self.live_follow = box("Live Scan: open on the running scan automatically",
                               "Off (the default), Live Scan opens blank and a banner names any scan or recorder "
                               "running, one click away. On, it opens straight onto a running scan.",
                               "start/live_follow", False)
        self.chain_reopen = box("Chain: reopen the last rig",
                                "Off (the default), Chain opens with no rig loaded: New rig, Open rig… and your last "
                                "three rigs. On, it opens the rig you worked on last.", "start/chain_reopen", False)
        # relay 020: Diagnostics, off by default
        from marsad_desktop import trace
        dh = QLabel("Diagnostics")
        dh.setFont(T.ui_font(10, QFont.Weight.DemiBold))
        dh.setContentsMargins(0, T.SPACE, 0, 0)
        lay.addWidget(dh)
        self.trace = box("Timing trace",
                         "Writes how long each click takes to show its result, each call to the service and any frame "
                         f"the window could not draw on time, to {trace.path()}. Off by default; it changes nothing "
                         "else.", "diag/trace", False, trace.set_on)
        note = QLabel("These are the window's own choices, kept in .marsad_desktop.ini in your home folder. Nothing "
                      "here changes station.json, a run or a saved rig.")
        note.setProperty("muted", True)
        note.setWordWrap(True)
        lay.addWidget(note)
        row = QHBoxLayout()
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addStretch(1)
        row.addWidget(close)
        lay.addLayout(row)
        self.resize(520, self.sizeHint().height())


class Window(QMainWindow):
    def __init__(self, service: Service, start: str = "Live Scan", station_folder: str | None = None):
        super().__init__()
        self.svc = service
        self.station_folder = station_folder          # relay 021: the station open here (None: the service's own)
        self.station = {}                             # /station/info
        self.setup = {}
        from marsad_desktop import tips
        tips.install()                    # relay 016: long tooltips wrap
        from marsad_desktop import practice
        self.practice = practice.on()
        self.setWindowTitle("Marsad — Practice station" if self.practice else "Marsad")
        self.resize(1440, 860)
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        top = QFrame()
        top.setObjectName("TopBar")
        top.setFixedHeight(HEADER_H)
        h = QHBoxLayout(top)
        h.setContentsMargins(20, 0, 16, 0)
        h.setSpacing(4)
        mid = Qt.AlignmentFlag.AlignVCenter
        self.logo = Logo()
        h.addWidget(self.logo, 0, mid)
        h.addSpacing(24)
        self.stack = QStackedWidget()
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.chain = ChainScreen(service)
        self.live = LiveScreen(service)            # the home screen (relay 009)
        # relay 020: one radio state for the whole window (it lives with Live Scan, which shows it most)
        self.radio_state = self.live.radio_state
        self.radio_state.changed.connect(lambda d: self._radios_changed(d))
        built = {"Chain": self.chain, "Live Scan": self.live}
        self.screens = []
        for i, s in enumerate(SCREENS):
            b = QPushButton(s)
            b.setProperty("nav", True)
            b.setCheckable(True)
            b.setToolTip({"Chain": "Chain: draw the rig each run records",
                          "Live Scan": "Live Scan: the scan being written now, or any finished survey"}.get(
                s, f"{s}: not built in this window yet; the web page does it"))
            # The selected tab is drawn bold, which is wider than the plain text Qt sized the
            # button for, so it clipped at both ends (relay 006). Size every tab for its bold
            # text plus the style sheet's padding, in the same logical pixels at any scaling.
            bold = QFont(b.font())
            bold.setWeight(QFont.Weight.DemiBold)
            b.setMinimumWidth(QFontMetrics(bold).horizontalAdvance(s) + 2 * T.NAV_PAD_X + 6)
            b.setFixedHeight(T.TOOL_H + 4)
            self.group.addButton(b, i)
            h.addWidget(b, 0, mid)
            w = built.get(s) or NotYet(s)
            self.screens.append(w)
            self.stack.addWidget(w)
        h.addSpacing(T.SPACE * 3)
        # relay 018: the header's middle says something useful: the station at a glance
        self.summary = QLabel()
        self.summary.setObjectName("StationSummary")
        self.summary.setFont(T.ui_font(9))
        self.summary.setTextFormat(Qt.TextFormat.RichText)
        self.summary.setToolTip("The station at a glance: each radio by serial, from Windows' device list (nothing "
                                "is opened), and the rig runs record")
        h.addWidget(self.summary, 1, mid)
        self.settings_btn = QToolButton()
        self.settings_btn.setObjectName("Tool")
        self.settings_btn.setText("Settings")
        self.settings_btn.setFixedHeight(T.TOOL_H)
        self.settings_btn.setToolTip("The window's own settings: explanations, and what each screen opens on")
        self.settings_btn.clicked.connect(lambda *_: self.open_settings())
        from PySide6.QtGui import QAction
        from marsad_desktop import explain as X
        # "Show explanations" as one action of the window (the Settings dialog and each View menu set the same switch)
        self.act_explain = QAction("Show explanations", self, checkable=True)
        self.act_explain.setChecked(X.shown())
        self.act_explain.toggled.connect(X.set_shown)
        self.addAction(self.act_explain)
        h.addWidget(self.settings_btn, 0, mid)
        h.addSpacing(12)
        self.conn = QLabel()
        self.conn.setProperty("muted", True)
        self.conn.setFont(T.ui_font(9))
        h.addWidget(self.conn, 0, mid)
        self.group.idClicked.connect(self._show_screen)
        root.addWidget(top)
        if self.practice:                 # relay 014: never mistaken for the real station
            band = QLabel("Practice station · made-up radios and rigs · nothing here touches the real station")
            band.setObjectName("PracticeBand")
            band.setAlignment(Qt.AlignmentFlag.AlignCenter)
            band.setFixedHeight(20)
            band.setStyleSheet(f"background: {T.rgba('anomaly', 0.18)}; color: {T.C['anomaly']}; "
                               f"font-size: 8.5pt; border-bottom: 1px solid {T.rgba('anomaly', 0.45)};")
            root.addWidget(band)
        root.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        # relay 018: the menu bar. Each screen builds its own menus; only the shown screen's are visible.
        self.menu_sets = []
        bar = self.menuBar()
        bar.setObjectName("MenuBar")
        from marsad_desktop.chain_screen import Menu
        for w in self.screens:
            menus = w.build_menus(self.open_settings, self.about) if hasattr(w, "build_menus") else []
            # relay 022: View > Theme… on every screen (a View menu is made where a screen has none)
            view = next((m for m in menus if m.title().replace("&", "") == "View"), None)
            if view is None:
                view = Menu(self)
                view.setTitle("&View")
                menus.insert(0, view)
            else:
                view.addSeparator()
            a = view.addAction("Theme…")
            a.setObjectName("ThemeAction")
            a.triggered.connect(lambda *_: self.open_theme())
            acts = [bar.addMenu(m) for m in menus]
            self.menu_sets.append(acts)

        # relay 021: the Station menu, on every screen: new, open, switch, set up
        from marsad_desktop.chain_screen import Menu
        sm = Menu(self)
        sm.setTitle("&Station")
        sm.setObjectName("StationMenu")
        sm.aboutToShow.connect(lambda: self._fill_station_menu(sm))
        self.station_menu = sm
        first = bar.actions()[0] if bar.actions() else None
        if first is not None:
            bar.insertMenu(first, sm)
        else:
            bar.addMenu(sm)
        i = SCREENS.index(start) if start in SCREENS else SCREENS.index("Live Scan")
        self.group.button(i).setChecked(True)
        self._show_screen(i)

        self.chain.status.connect(lambda m: self.statusBar().showMessage(m, 8000))
        # relay 020: "Use for runs" shows at once in the header and on every radio's chain (Radios panel, Start panels)
        self.chain.used.connect(self._summary)
        self.chain.used.connect(self.radio_state.refresh)
        self.live.setup_requested.connect(self.open_setup)
        self.setup_timer = QTimer(self)                # relay 021: whether scanning is locked, and why
        self.setup_timer.timeout.connect(self._ask_setup)
        self.setup_timer.start(8000)
        self.live.status.connect(lambda m: self.statusBar().showMessage(m, 8000))
        self.live.open_rig_version.connect(self.open_rig_version)
        self.statusBar().setStyleSheet(f"color: {T.C['muted']}; border-top: 1px solid {T.C['lines']};")
        # A light check every 30 s: says whether the service answers, and keeps a
        # test service (started with an idle timeout) alive while the window is open.
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._ping)
        self.timer.start(30000)
        self._radio_data = None
        self._ok = None
        QTimer.singleShot(0, self._first_load)

    def _show_screen(self, i: int):
        """A tab: its screen, and its menus in the menu bar (relay 018: menus open only on the screens they apply to)."""
        self.stack.setCurrentIndex(i)
        for k, acts in enumerate(self.menu_sets):
            for a in acts:
                a.setVisible(k == i)

    def open_rig_version(self, info: dict):
        """Relay 012: from a run's facts, its rig as it was, read-only on the Chain screen."""
        i = SCREENS.index("Chain")
        self.group.button(i).setChecked(True)
        self._show_screen(i)
        self.chain.open_version(info)

    def open_settings(self):
        d = SettingsDialog(self)
        self._settings_dlg = d
        d.exec()

    def about(self):
        """Help > About: the version and the service's address live here."""
        from PySide6.QtWidgets import QMessageBox
        try:
            from marsad import __version__ as ver
        except ImportError:
            ver = "?"
        ok = self._ok                      # relay 020: the last check, not a new call on the window's thread
        box = QMessageBox(self)
        box.setWindowTitle("About Marsad")
        box.setText(f"<b>Marsad</b> {ver}<br>al-marsad, the observatory")
        box.setInformativeText(
            f"The window reads and saves through the service at {self.svc.base}"
            f" ({'answering' if ok else 'not checked yet' if ok is None else 'not answering'}). Closing the window "
            "never stops a scan.\n\n"
            "Levels are relative dB, never calibrated dBm.")
        self._about_box = box
        box.exec()

    def _first_load(self):
        self._ping()
        self._radios()
        self._ask_station()
        self._ask_setup()
        self.chain.reload(first=True)

    def _ping(self):
        """Relay 020: asked off the window's thread. It ran on it every 30 s, and while the service was busy (reading a
        big recording) each check could hold the window for up to its 5 s timeout."""
        from marsad_desktop.scan_start import call
        if getattr(self, "_pinging", False):
            return
        self._pinging = True

        def got(ok, _err):
            self._pinging = False
            self._ok = bool(ok)
            dot = T.C["open"] if ok else T.C["alert"]
            self.conn.setText(f"<span style='color:{dot}'>●</span> service {self.svc.base.replace('http://', '')}"
                              + ("" if ok else " not answering"))
            self._summary()
        call(self.svc.ping, got)

    def _radios(self):
        self.radio_state.refresh()

    def _radios_changed(self, d: dict):
        self._radio_data = d
        self._summary()

    def _summary(self):
        """The header's middle: each radio in a word (plugged in, busy, not plugged in) and the rig runs record."""
        d = self._radio_data
        bits = []
        if d and d.get("radios"):
            for r in d["radios"]:
                st = (r.get("doing") or {}).get("state")
                colour, word = {"recording": ("recorder", "recording"), "surveying": ("open", "surveying"),
                                "ready": ("survey_light", "ready"), "absent": ("muted", "not plugged in"),
                                "disabled": ("muted", "disabled"), "blocked": ("anomaly", "in use")}.get(
                    st, ("muted", "not known"))
                name = str(r.get("name") or "")
                name = name.upper() if name.isalpha() else name
                bits.append(f"<span style='color:{T.C[colour]}'>●</span> {name} {word}")
        elif self._ok is False:
            bits.append(f"<span style='color:{T.C['muted']}'>radios not known: the service is not answering</span>")
        if self.station.get("name"):
            bits.insert(0, f"<b>{self.station['name']}</b>")
        used = self.chain.used_rig_name() if self.chain.display else None
        if used:
            bits.append(f"<span style='color:{T.C['muted']}'>runs record</span> {used}")
        self.summary.setText("&nbsp;&nbsp;&nbsp;".join(bits))

    # -- relay 021: the station open here
    def _ask_station(self):
        from marsad_desktop.scan_start import call

        def got(d, err):
            if err or not d:
                return
            self.station = d
            self.station_folder = self.station_folder or d.get("root")
            name = d.get("name") or ""
            self.setWindowTitle(("Marsad \u2014 Practice station" if self.practice else "Marsad")
                                + (f" \u2014 {name}" if name else ""))
            self._summary()
        call(self.svc.station_info, got)

    def _ask_setup(self):
        from marsad_desktop.scan_start import call
        if getattr(self, "_setup_busy", False):
            return
        self._setup_busy = True

        def got(d, err):
            self._setup_busy = False
            if not err and d:
                self.setup = d
                self.live.show_setup(d)
        call(self.svc.setup_status, got)

    def _fill_station_menu(self, m):
        from marsad_desktop import stations as ST
        m.clear()
        a = m.addAction("New Station\u2026")
        a.triggered.connect(lambda *_: self.new_station())
        a = m.addAction("Open Station\u2026")
        a.triggered.connect(lambda *_: self.open_station_dialog())
        sub = m.addMenu("Switch Station")
        sub.setObjectName("SwitchStation")
        here = self.station_folder
        for e in ST.registry().get("stations") or []:
            cur = bool(here) and ST._same(e["folder"], here)
            b = sub.addAction(f"{e['name']}   \u00b7   {e['folder']}" + ("   (open now)" if cur else ""))
            b.setEnabled(not cur)
            b.triggered.connect(lambda _=False, f=e["folder"]: self.open_station(f))
        if not sub.actions():
            b = sub.addAction("No other station yet")
            b.setEnabled(False)
        m.addSeparator()
        a = m.addAction("Set up this station\u2026")
        a.triggered.connect(lambda *_: self.open_setup())

    def new_station(self):
        from marsad_desktop.setup_screen import NewStationDialog
        d = NewStationDialog(self)
        if d.exec() and d.created:
            self.open_station(d.created["folder"], name=d.created["name"])

    def open_station_dialog(self):
        from marsad_desktop.setup_screen import pick_existing_station
        f = pick_existing_station(self, str(self.station_folder or ""))
        if f:
            self.open_station(f)

    def open_station(self, folder: str, name: str | None = None):
        """Switch to another station: its service (found answering, or started), then a fresh window on it. This window
        closes; nothing in the station being left changes."""
        from marsad_desktop import stations as ST
        from marsad_desktop.scan_start import call
        if not self.chain.can_close():
            return
        self.statusBar().showMessage(f"Opening the station at {folder}\u2026", 15000)

        def got(base, err):
            if err:
                from PySide6.QtWidgets import QMessageBox
                QMessageBox.information(self, "Station not opened", err)
                return
            ST.remember(folder, name)
            w = Window(Service(base), start="Live Scan", station_folder=str(folder))
            w.resize(self.size())
            w.move(self.pos())
            _OPEN.append(w)
            w.show()
            self.switched_to = w
            self.close()
        call(lambda: ST.ensure_service(folder), got)

    def open_theme(self):
        """Relay 022: View > Theme…. Apply makes the theme the window's palette, remembers it, and rebuilds the window
        on it (every screen, plot and panel), on the same screen and station; a scan is never touched."""
        from marsad_desktop import theme_picker as TP
        d = TP.ThemeDialog(self, T.current())
        self._theme_dlg = d
        if not d.exec() or not d.chosen or d.chosen == T.current():
            return
        if not self.chain.can_close():
            return
        apply_theme(d.chosen)
        TP.remember(d.chosen)
        w = Window(self.svc, start=SCREENS[self.stack.currentIndex()], station_folder=self.station_folder)
        w.resize(self.size())
        w.move(self.pos())
        if self.isMaximized():
            w.showMaximized()
        _OPEN.append(w)
        w.show()
        self.rebuilt_as = w
        self.close()

    def open_setup(self):
        from marsad_desktop.setup_screen import SetupDialog
        d = SetupDialog(self.svc, self, station_folder=self.station_folder)
        d.open_chain.connect(lambda: (self.group.button(SCREENS.index("Chain")).setChecked(True),
                                      self._show_screen(SCREENS.index("Chain"))))
        d.changed.connect(self.live.show_setup)
        self._setup_dlg = d
        d.exec()
        self._ask_setup()
        self.radio_state.refresh()

    def closeEvent(self, ev):
        if self.chain.can_close():
            self.live.shutdown()
            ev.accept()
        else:
            ev.ignore()


_OPEN: list = []                    # windows opened by a station switch or a theme change, kept alive


def apply_theme(name: str) -> str:
    """Relay 022: the palette, the application's style sheet and the plots' colours, all at once."""
    used = T.use(name)
    app = QApplication.instance()
    if app is not None:
        app.setStyleSheet(T.stylesheet())
    from marsad_desktop import live_screen
    live_screen.apply_plot_theme()
    return used


def choose_station():
    """Relay 021: the station to open. With MARSAD_SERVICE (the launchers and the checks) that service's station; else
    the one opened last; else, the first time, New Station or Open an existing station. Returns (Service, folder), or
    (None, None) when the owner quits."""
    from marsad_desktop import stations as ST
    if os.environ.get("MARSAD_SERVICE"):
        svc = Service()
        folder = ST.station_of(svc.base)
        if folder:
            ST.remember(folder)
        return svc, folder
    reg = ST.registry()
    folder = reg.get("last")
    if not folder or not (Path(folder) / "station.json").exists():
        from marsad_desktop.setup_screen import FirstRunDialog
        home = ST.home_station()
        d = FirstRunDialog(home=str(home) if home else None)
        if not d.exec() or not d.folder:
            return None, None
        folder = d.folder
    try:
        base = ST.ensure_service(folder)
    except RuntimeError as exc:
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.information(None, "Station not opened", str(exc))
        return None, None
    ST.remember(folder)
    return Service(base), folder


def main(argv=None) -> int:
    app = QApplication(sys.argv if argv is None else argv)
    app.setApplicationName("Marsad")
    app.setFont(T.ui_font())
    from marsad_desktop import theme_picker
    apply_theme(theme_picker.saved())              # relay 022: the theme chosen last (Original until one is)
    from marsad_desktop import trace
    trace.start_from_prefs()                       # relay 020: Settings > Diagnostics (off by default)
    svc, folder = choose_station()
    if svc is None:
        return 0
    w = Window(svc, start=os.environ.get("MARSAD_START_SCREEN", "Live Scan"), station_folder=folder)
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
