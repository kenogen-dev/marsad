"""Explanations for every control (relay 011): a one-line hover tip, and an ⓘ mark that opens a
plain-language explanation. The texts live in one file, explanations.md beside this module, so they
can be reviewed and edited without touching code.

    tip(key)            the one-line tip
    attach(w, key)      give a widget its tip (longer when explanations are shown)
    titled(text, key)   a field's label with its ⓘ mark
    info(key)           just the ⓘ mark
    use_radio(svc, rx)  look up where this radio's defaults come from (shown in the explanations)

"Show explanations" (Settings, on by default) hides every ⓘ and shortens the tips to one line.
It is kept in the window's own preferences file, never in the station.
"""
from __future__ import annotations

import re
import weakref
from pathlib import Path

from PySide6.QtCore import QPoint, QSettings, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QToolButton, QVBoxLayout, QWidget

from marsad_desktop import theme as T

FILE = Path(__file__).with_name("explanations.md")
LOCKS = {"yes": "Locks comparability: changing it starts a new comparison set, so the run can only be compared "
                "with runs that use the same value.",
         "no": "Free to change: it does not affect which runs this one can be compared with.",
         "n/a": ""}
_TEXTS: dict | None = None
_BUTTONS: "weakref.WeakSet" = weakref.WeakSet()
_TIPPED: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
SOURCES: dict = {}               # rx -> /explain/defaults items
CURRENT = {"rx": None}


def prefs() -> QSettings:
    from pathlib import Path as _P
    return QSettings(str(_P.home() / ".marsad_desktop.ini"), QSettings.Format.IniFormat)


def texts() -> dict:
    """Parse explanations.md: {key: {"tip", "locks", "default", "body"}}."""
    global _TEXTS
    if _TEXTS is None:
        _TEXTS = parse(FILE.read_text(encoding="utf-8"))
    return _TEXTS


def parse(md: str) -> dict:
    out, key, lines = {}, None, []

    def done():
        if key is None:
            return
        meta, body, in_meta = {}, [], True
        for ln in lines:
            m = re.match(r"^(tip|locks|default):\s*(.*)$", ln) if in_meta else None
            if m:
                meta[m.group(1)] = m.group(2).strip()
                continue
            if in_meta and not ln.strip():
                continue
            in_meta = False
            body.append(ln)
        out[key] = {"tip": meta.get("tip", ""), "locks": meta.get("locks", "n/a"), "default": meta.get("default"),
                    "body": "\n".join(body).strip()}

    for ln in md.splitlines():
        m = re.match(r"^##\s+(\S+)\s*$", ln)
        if m:
            done()
            key, lines = m.group(1), []
        elif re.match(r"^#\s", ln):
            done()
            key, lines = None, []
        elif key is not None:
            lines.append(ln)
    done()
    return out


def shown() -> bool:
    return prefs().value("explain/show", True, type=bool)


def set_shown(on: bool) -> None:
    prefs().setValue("explain/show", bool(on))
    for b in list(_BUTTONS):
        b.setVisible(on)
    for w, key in list(_TIPPED.items()):
        w.setToolTip(_tooltip(key))


def tip(key: str) -> str:
    return (texts().get(key) or {}).get("tip") or ""


def _first_para(body: str) -> str:
    para = body.split("\n\n")[0].replace("\n", " ").strip()
    return re.sub(r"\*\*|`", "", para)


def _tooltip(key: str) -> str:
    t = texts().get(key) or {}
    one = t.get("tip") or ""
    if not shown() or not t.get("body"):
        return one
    more = _first_para(t["body"])
    return one + ("\n\n" + more if more and more != one else "") + "\n\nⓘ for the full explanation"


def attach(w: QWidget, key: str) -> QWidget:
    """Give a widget its tip; it follows the Show explanations switch."""
    if key in texts():
        _TIPPED[w] = key
        w.setToolTip(_tooltip(key))
    return w


def info(key: str, rx: str | None = None) -> QToolButton:
    b = QToolButton()
    b.setObjectName("Info")
    b.setText("ⓘ")
    b.setAutoRaise(True)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.setFont(T.ui_font(9))
    b.setStyleSheet(f"QToolButton#Info {{ color: {T.C['muted']}; border: none; padding: 0 2px; background: transparent; }}"
                    f"QToolButton#Info:hover {{ color: {T.C['text']}; }}")
    b.setToolTip(f"What this is: {tip(key) or key}")
    b.clicked.connect(lambda: Popup.open_for(key, b, rx))
    b.setVisible(shown())
    _BUTTONS.add(b)
    return b


def titled(text: str, key: str, muted: bool = True, wrap: bool = False, rx: str | None = None) -> QWidget:
    """A field's label with its ⓘ mark beside it, and the tip on both."""
    w = QWidget()
    w.setObjectName("InfoRow")
    w.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    w.setStyleSheet("QWidget#InfoRow { background: transparent; }")
    h = QHBoxLayout(w)
    h.setContentsMargins(0, 0, 0, 0)
    h.setSpacing(2)
    lab = QLabel(text)
    if muted:
        lab.setProperty("muted", True)
    lab.setWordWrap(wrap)
    attach(lab, key)
    h.addWidget(lab)
    h.addWidget(info(key, rx))
    h.addStretch(1)
    w.label = lab
    return w


def with_info(widget: QWidget, key: str) -> QWidget:
    """A widget (a check box, a button) followed by its ⓘ mark."""
    attach(widget, key)
    w = QWidget()
    w.setObjectName("InfoRow")
    w.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    w.setStyleSheet("QWidget#InfoRow { background: transparent; }")
    h = QHBoxLayout(w)
    h.setContentsMargins(0, 0, 0, 0)
    h.setSpacing(2)
    h.addWidget(widget)
    h.addWidget(info(key))
    h.addStretch(1)
    return w


def use_radio(svc, rx: str | None) -> None:
    """Fetch where this radio's defaults come from (the service reads station.json, never the
    location file); explanations with a `default:` show it."""
    CURRENT["rx"] = rx
    fetch(svc, rx)


def fetch(svc, rx: str | None) -> None:
    """Look up one radio's default sources once, without making it the current radio."""
    if not svc or not rx or rx in SOURCES:
        return
    from marsad_desktop.scan_start import call
    call(lambda: svc.default_sources(rx), lambda d, err: SOURCES.__setitem__(rx, (d or {}).get("items") or {})
         if not err else None)


def source_text(dkey: str, rx: str | None = None) -> str:
    items = SOURCES.get(rx or CURRENT["rx"]) or {}
    it = items.get(dkey)
    if not it:
        return ""
    s = f"**Where the default comes from:** {it['value']}, {it['text']}."
    if it.get("scope") == "station":
        s += " It is one figure for every radio on this station, not set per radio."
    if it.get("note"):
        s += f" The station's own note: “{it['note']}”"
    if it.get("measure"):
        s += f"\n\nHow it can be measured or chosen: {it['measure']}."
    return s


def markdown_for(key: str, rx: str | None = None) -> str:
    t = texts().get(key) or {}
    parts = [f"**{t.get('tip') or key}**", t.get("body") or ""]
    if LOCKS.get(t.get("locks") or "n/a"):
        parts.append("*" + LOCKS[t["locks"]] + "*")
    if t.get("default"):
        src = source_text(t["default"], rx)
        parts.append(src or "*Where the default comes from is being looked up; open this again in a moment.*")
    return "\n\n".join(p for p in parts if p)


class Popup(QFrame):
    """The full explanation, beside the ⓘ that opened it. Closes on a click elsewhere or Esc."""
    current = None

    @classmethod
    def open_for(cls, key: str, anchor: QWidget, rx: str | None = None):
        if cls.current is not None:
            cls.current.close()
        p = cls(key, rx)
        cls.current = p
        g = anchor.mapToGlobal(QPoint(0, anchor.height() + 4))
        p.adjustSize()
        scr = anchor.screen().availableGeometry() if anchor.screen() else None
        x, y = g.x(), g.y()
        if scr is not None:
            x = max(scr.left() + 8, min(x, scr.right() - p.width() - 8))
            if y + p.height() > scr.bottom() - 8:
                y = max(scr.top() + 8, anchor.mapToGlobal(QPoint(0, 0)).y() - p.height() - 4)
        p.move(x, y)
        p.show()
        return p

    def __init__(self, key: str, rx: str | None = None):
        super().__init__(None, Qt.WindowType.Popup)
        self.key = key
        self.setObjectName("ExplainPopup")
        self.setStyleSheet(f"#ExplainPopup {{ background: {T.C['raised']}; border: 1px solid {T.C['lines']}; "
                           f"border-radius: 8px; }} #ExplainPopup QLabel {{ background: transparent; }}")
        v = QVBoxLayout(self)
        v.setContentsMargins(T.SPACE * 1.5, T.SPACE * 1.25, T.SPACE * 1.5, T.SPACE * 1.5)
        self.text = QLabel()
        self.text.setTextFormat(Qt.TextFormat.MarkdownText)
        self.text.setWordWrap(True)
        self.text.setFixedWidth(460)
        self.text.setFont(T.ui_font(9.5))
        self.text.setText(markdown_for(key, rx))
        self.text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(self.text)
        foot = QLabel("Settings › Show explanations turns these marks off.")
        foot.setProperty("muted", True)
        foot.setFont(T.ui_font(8, QFont.Weight.Normal))
        v.addWidget(foot)

    def closeEvent(self, ev):
        if Popup.current is self:
            Popup.current = None
        super().closeEvent(ev)
