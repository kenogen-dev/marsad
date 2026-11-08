"""Relay 016: long tooltips wrap. Qt shows a plain-text tooltip on one line however long it is, so a long tip
ran off the screen. One event filter on the application catches tooltip events and, for a long plain-text tip,
shows the same words as a wrapped block of a fixed width. Short tips and tips that are already rich text are left
to Qt. Works for widgets, menu entries and the parts and wires on the Chain grid."""
from __future__ import annotations

import html

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QApplication, QGraphicsView, QMenu, QToolTip, QWidget

WRAP_AT = 64             # characters: a plain tip longer than this is shown wrapped
WIDTH = 380              # px: the width a wrapped tip is set to


def wrapped(text: str) -> str:
    """A plain-text tip as rich text of a fixed width, so Qt wraps it. Line breaks are kept."""
    body = "<br>".join(html.escape(line) for line in text.split("\n"))
    return f"<table width='{WIDTH}' cellspacing='0' cellpadding='0'><tr><td>{body}</td></tr></table>"


def needs_wrap(text: str | None) -> bool:
    """A plain tip with a line longer than WRAP_AT. A tip that is already rich text (it starts with a tag) is
    Qt's to lay out."""
    if not text or text.lstrip().startswith("<"):
        return False
    return max((len(line) for line in text.split("\n")), default=0) > WRAP_AT


def _tip_of(obj, ev) -> str:
    if isinstance(obj, QMenu):
        a = obj.actionAt(ev.pos())
        if a is None or not obj.toolTipsVisible():
            return ""
        tip = a.toolTip()
        return tip if tip and tip.replace("&", "") != a.text().replace("&", "").split("\t")[0] else ""
    parent = obj.parent() if isinstance(obj, QWidget) else None
    if isinstance(parent, QGraphicsView) and parent.viewport() is obj:
        for it in parent.items(ev.pos()):
            if it.toolTip():
                return it.toolTip()
        return ""
    return obj.toolTip() if isinstance(obj, QWidget) else ""


class TipWrap(QObject):
    def eventFilter(self, obj, ev):
        if ev.type() != QEvent.Type.ToolTip:
            return False
        try:                          # a tip is never worth an error: anything odd is left to Qt
            text = _tip_of(obj, ev)
            if not needs_wrap(text):
                return False
            QToolTip.showText(ev.globalPos(), wrapped(text), obj if isinstance(obj, QWidget) else None)
            return True
        except Exception:
            return False


_INSTALLED: list = []


def install(app: QApplication | None = None) -> None:
    """Once per application."""
    app = app or QApplication.instance()
    if app is None or _INSTALLED:
        return
    f = TipWrap(app)
    app.installEventFilter(f)
    _INSTALLED.append(f)
