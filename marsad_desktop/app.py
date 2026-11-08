"""Marsad's window: the shell (navigation across the top) and its screens.

Only Chain works in this pilot. The window is a viewer and editor: it reads and
saves through the background service, so closing it never touches a scan.

    .venv-desktop\\Scripts\\python.exe -m marsad_desktop.app
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import Qt, QTimer                             # noqa: E402
from PySide6.QtGui import QFont, QFontMetrics                    # noqa: E402
from PySide6.QtWidgets import (QApplication, QButtonGroup, QFrame, QHBoxLayout, QLabel,  # noqa: E402
                               QMainWindow, QPushButton, QStackedWidget, QVBoxLayout, QWidget)

from marsad_desktop import theme as T                            # noqa: E402
from marsad_desktop.chain_screen import ChainScreen              # noqa: E402
from marsad_desktop.live_screen import LiveScreen                # noqa: E402
from marsad_desktop.service import Service                       # noqa: E402

SCREENS = ["Live Scan", "Captures", "Chain", "Probe", "Archive"]


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


class Window(QMainWindow):
    def __init__(self, service: Service, start: str = "Live Scan"):
        super().__init__()
        self.svc = service
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
        h = QHBoxLayout(top)
        h.setContentsMargins(20, 8, 20, 8)
        h.setSpacing(4)
        name = QLabel("MARSAD")
        name.setFont(T.ui_font(12.5, QFont.Weight.Bold))
        sub = QLabel("observatory")
        sub.setProperty("muted", True)
        h.addWidget(name)
        h.addSpacing(8)
        h.addWidget(sub)
        h.addSpacing(20)
        self.stack = QStackedWidget()
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.chain = ChainScreen(service)
        self.live = LiveScreen(service)            # the home screen (relay 009)
        built = {"Chain": self.chain, "Live Scan": self.live}
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
            self.group.addButton(b, i)
            h.addWidget(b)
            self.stack.addWidget(built.get(s) or NotYet(s))
        h.addStretch(1)
        from PySide6.QtWidgets import QMenu, QToolButton
        from marsad_desktop import explain as X
        self.settings_btn = QToolButton()
        self.settings_btn.setObjectName("Tool")
        self.settings_btn.setText("Settings")
        self.settings_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.settings_btn)
        self.act_explain = menu.addAction("Show explanations")
        self.act_explain.setCheckable(True)
        self.act_explain.setChecked(X.shown())
        self.act_explain.setToolTip("The ⓘ marks and the longer tips. Off leaves the one-line tips.")
        self.act_explain.toggled.connect(X.set_shown)
        self.settings_btn.setMenu(menu)
        h.addWidget(self.settings_btn)
        h.addSpacing(12)
        self.conn = QLabel()
        self.conn.setProperty("muted", True)
        h.addWidget(self.conn)
        self.group.idClicked.connect(self.stack.setCurrentIndex)
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
        i = SCREENS.index(start) if start in SCREENS else SCREENS.index("Live Scan")
        self.group.button(i).setChecked(True)
        self.stack.setCurrentIndex(i)

        self.chain.status.connect(lambda m: self.statusBar().showMessage(m, 8000))
        self.live.status.connect(lambda m: self.statusBar().showMessage(m, 8000))
        self.live.open_rig_version.connect(self.open_rig_version)
        self.statusBar().setStyleSheet(f"color: {T.C['muted']}; border-top: 1px solid {T.C['lines']};")
        # A light check every 30 s: says whether the service answers, and keeps a
        # test service (started with an idle timeout) alive while the window is open.
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._ping)
        self.timer.start(30000)
        QTimer.singleShot(0, self._first_load)

    def open_rig_version(self, info: dict):
        """Relay 012: from a run's facts, its rig as it was, read-only on the Chain screen."""
        i = SCREENS.index("Chain")
        self.group.button(i).setChecked(True)
        self.stack.setCurrentIndex(i)
        self.chain.open_version(info)

    def _first_load(self):
        self._ping()
        self.chain.reload()

    def _ping(self):
        ok = self.svc.ping()
        dot = T.C["open"] if ok else T.C["alert"]
        self.conn.setText(f"<span style='color:{dot}'>●</span> service {self.svc.base.replace('http://', '')}"
                          + ("" if ok else " not answering"))

    def closeEvent(self, ev):
        if self.chain.can_close():
            self.live.shutdown()
            ev.accept()
        else:
            ev.ignore()


def main(argv=None) -> int:
    app = QApplication(sys.argv if argv is None else argv)
    app.setApplicationName("Marsad")
    app.setFont(T.ui_font())
    app.setStyleSheet(T.stylesheet())
    w = Window(Service(), start=os.environ.get("MARSAD_START_SCREEN", "Live Scan"))
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
