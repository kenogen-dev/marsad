"""Relay 022: View > Theme…, the window's palette picker.

Nine themes (the eight of relay 022 and the Original), each previewed live on a small sample (a toolbar, a card, a
row of badges, a spectrum strip and a waterfall strip) drawn in that theme's own palette, without touching the
window. Apply makes it the window's palette (the window rebuilds itself on it) and remembers it in the window's
preferences ("view/theme"). The waterfall strip uses Live Scan's own colour map choice, never the theme's: the two are
separate choices.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QDialog, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton,
                               QToolButton, QVBoxLayout, QWidget)

from marsad_desktop import theme as T

PREF = "view/theme"


def saved() -> str:
    """The theme the window should open with (Original when none was chosen, or the name is unknown)."""
    from marsad_desktop.chain_screen import prefs
    name = str(prefs().value(PREF, T.THEME_DEFAULT) or T.THEME_DEFAULT)
    return name if name in T.THEMES else T.THEME_DEFAULT


def remember(name: str) -> None:
    from marsad_desktop.chain_screen import prefs
    prefs().setValue(PREF, name)


def _cmap():
    """Live Scan's waterfall colour map (its own choice, kept apart from the theme)."""
    from marsad_desktop.chain_screen import prefs
    from marsad_desktop.live_screen import COLOUR_MAPS
    name = str(prefs().value("live/cmap", "Classic") or "Classic")
    return name if name in COLOUR_MAPS else "Classic", COLOUR_MAPS.get(name, COLOUR_MAPS["Classic"])


class Strip(QWidget):
    """A spectrum strip (two traces, survey and recorder, over the floor) and a waterfall strip, painted in palette p."""

    def __init__(self, kind: str):
        super().__init__()
        self.kind = kind
        self.p = T.palette(T.THEME_DEFAULT)
        self.setMinimumHeight(70 if kind == "spectrum" else 54)

    def set_palette(self, p: dict):
        self.p = p
        self.update()

    @staticmethod
    def _trace(x: float, seed: float) -> float:
        """A made-up spectrum: a floor with two carriers and a broad hump (no real data)."""
        return (0.18 + 0.05 * math.sin(x * 37 + seed) * math.sin(x * 11) + 0.55 * math.exp(-((x - 0.3) / 0.012) ** 2)
                + 0.35 * math.exp(-((x - 0.72) / 0.02) ** 2) + 0.12 * math.exp(-((x - 0.55) / 0.12) ** 2))

    def paintEvent(self, ev):
        p = self.p
        qp = QPainter(self)
        qp.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        qp.setPen(QPen(QColor(p["lines"]), 1))
        qp.setBrush(QColor(p["plot"]))
        qp.drawRoundedRect(r, 6, 6)
        w, h = r.width() - 8, r.height() - 8
        if self.kind == "spectrum":
            qp.setPen(QPen(QColor(p["lines"]), 1, Qt.PenStyle.DotLine))
            qp.drawLine(QPointF(4, 4 + h * 0.8), QPointF(4 + w, 4 + h * 0.8))
            for colour, seed, lift in (("recorder", 2.0, 0.06), ("survey", 0.0, 0.0)):
                path = QPainterPath()
                for i in range(int(w) + 1):
                    x = i / max(1, w)
                    y = 4 + h * (1 - min(0.98, self._trace(x, seed) + lift))
                    path.moveTo(4 + i, y) if i == 0 else path.lineTo(4 + i, y)
                qp.setPen(QPen(QColor(p[colour]), 1.6))
                qp.setBrush(Qt.BrushStyle.NoBrush)
                qp.drawPath(path)
        else:
            _, lut = _cmap()
            cols, rows = max(2, int(w)), max(2, int(h))
            img = QImage(cols, rows, QImage.Format.Format_RGB32)
            for y in range(rows):
                for x in range(cols):
                    v = self._trace(x / cols, y * 0.37) * (0.85 + 0.15 * math.sin(y * 0.9 + x * 0.05))
                    c = lut[max(0, min(255, int(v * 255)))]
                    img.setPixel(x, y, (0xFF << 24) | (int(c[0]) << 16) | (int(c[1]) << 8) | int(c[2]))
            qp.drawImage(QRectF(4, 4, w, h), img)
        qp.end()


class Preview(QFrame):
    """The sample, styled with the candidate palette's own style sheet (the window's is left alone)."""

    def __init__(self):
        super().__init__()
        self.setObjectName("ThemePreview")
        v = QVBoxLayout(self)
        v.setContentsMargins(T.SPACE * 2, T.SPACE * 2, T.SPACE * 2, T.SPACE * 2)
        v.setSpacing(T.SPACE)
        bar = QFrame()
        bar.setObjectName("Toolbar")
        h = QHBoxLayout(bar)
        h.setContentsMargins(0, 0, 0, T.SPACE)
        seg = QFrame()
        seg.setObjectName("Segment")
        sh = QHBoxLayout(seg)
        sh.setContentsMargins(2, 2, 2, 2)
        for text, checked in (("Spectrum", True), ("Waterfall", False), ("Marks", False)):
            b = QToolButton()
            b.setObjectName("Tool")
            b.setText(text)
            b.setCheckable(True)
            b.setChecked(checked)
            b.setFixedHeight(T.TOOL_H)
            sh.addWidget(b)
        h.addWidget(seg)
        h.addStretch(1)
        start = QToolButton()
        start.setObjectName("Primary")
        start.setText("Start a scan")
        start.setFixedHeight(T.TOOL_H)
        stop = QToolButton()
        stop.setObjectName("Danger")
        stop.setText("Stop")
        stop.setFixedHeight(T.TOOL_H)
        h.addWidget(start)
        h.addWidget(stop)
        v.addWidget(bar)
        card = QFrame()
        card.setObjectName("Card")
        cv = QVBoxLayout(card)
        cv.setContentsMargins(T.SPACE * 1.5, T.SPACE, T.SPACE * 1.5, T.SPACE)
        t = QLabel("radio1 · surveying")
        t.setFont(T.ui_font(10.5, QFont.Weight.DemiBold))
        m = QLabel("antenna > notch > LNA > coax > radio1 > USB > PC")
        m.setProperty("muted", True)
        cv.addWidget(t)
        cv.addWidget(m)
        self.badges = QHBoxLayout()
        self.badges.setSpacing(6)
        self.pills = []
        for word, key in (("survey", "survey"), ("recorder", "recorder"), ("OPEN", "open"), ("PRIVATE", "private"),
                          ("ANOMALY", "anomaly"), ("ALERT", "alert")):
            pill = QLabel(word)
            pill.setObjectName("Pill")
            pill.setProperty("tone", key)
            self.pills.append(pill)
            self.badges.addWidget(pill)
        self.badges.addStretch(1)
        cv.addLayout(self.badges)
        v.addWidget(card)
        self.spectrum = Strip("spectrum")
        self.waterfall = Strip("waterfall")
        v.addWidget(self.spectrum)
        v.addWidget(self.waterfall)
        self.cmap_note = QLabel("")
        self.cmap_note.setProperty("muted", True)
        self.cmap_note.setWordWrap(True)
        v.addWidget(self.cmap_note)

    def set_palette(self, p: dict):
        sheet = T.stylesheet(p) + f" #ThemePreview {{ background: {p['elev_ground']}; border: 1px solid " \
                                  f"{p['line_strong']}; border-radius: {T.RADIUS}px; }}"
        for pill in self.pills:
            k = pill.property("tone")
            pill.setStyleSheet(f"background: {T._mix(p['panel'], p[k], 0.18)}; color: {p[k]}; "
                               f"border: 1px solid {T._mix(p['panel'], p[k], 0.55)};")
        self.setStyleSheet(sheet)
        self.spectrum.set_palette(p)
        self.waterfall.set_palette(p)
        name, _ = _cmap()
        self.cmap_note.setText(f"The waterfall uses Live Scan's own colour map ({name}), chosen in Live Scan's View "
                               "menu: it does not change with the theme.")


class ThemeDialog(QDialog):
    """View > Theme…: pick, preview, apply."""

    def __init__(self, parent=None, current: str | None = None):
        super().__init__(parent)
        self.setWindowTitle("Theme")
        self.setObjectName("ThemeDialog")
        self.chosen: str | None = None
        cur = current or T.current()
        v = QVBoxLayout(self)
        v.setContentsMargins(T.SPACE * 3, T.SPACE * 2, T.SPACE * 3, T.SPACE * 2)
        v.setSpacing(T.SPACE)
        head = QLabel("Theme")
        head.setFont(T.ui_font(13, QFont.Weight.DemiBold))
        v.addWidget(head)
        sub = QLabel("All dark and high-contrast. Each one passes the same checks: text 4.5:1 on every surface, icons "
                     "3:1, survey and recorder different in lightness, and open, private, anomaly and alert distinct "
                     "from each other and from the accent. Applied to the whole window at once.")
        sub.setProperty("muted", True)
        sub.setWordWrap(True)
        v.addWidget(sub)
        row = QHBoxLayout()
        self.list = QListWidget()
        self.list.setObjectName("ThemeList")
        self.list.setFixedWidth(360)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        for name, d in T.THEMES.items():
            probs = T.problems(T.palette(name), name)
            exempt = T.THEME_EXEMPT.get(name, {})
            mark = "passes every check" if not probs else f"{len(probs)} checks fail"
            if exempt:
                mark += " but survey/recorder lightness, kept as it was"
            it = QListWidgetItem(f"{name}{'   (in use)' if name == cur else ''}\n{d.get('note', '')}\n{mark}")
            it.setData(Qt.ItemDataRole.UserRole, name)
            it.setToolTip("\n".join(probs) if probs else mark + (" (" + "; ".join(exempt.values()) + ")" if exempt else ""))
            self.list.addItem(it)
        self.list.setWordWrap(True)
        row.addWidget(self.list)
        self.preview = Preview()
        row.addWidget(self.preview, 1)
        v.addLayout(row, 1)
        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        self.apply = QPushButton("Apply")
        self.apply.setObjectName("Primary")
        self.apply.clicked.connect(self._apply)
        btns.addWidget(cancel)
        btns.addWidget(self.apply)
        v.addLayout(btns)
        self.list.currentItemChanged.connect(self._show)
        names = list(T.THEMES)
        self.list.setCurrentRow(names.index(cur) if cur in names else 0)
        self.resize(940, 620)

    def selected(self) -> str:
        it = self.list.currentItem()
        return it.data(Qt.ItemDataRole.UserRole) if it else T.THEME_DEFAULT

    def _show(self, *_):
        self.preview.set_palette(T.palette(self.selected()))

    def _apply(self):
        self.chosen = self.selected()
        self.accept()
