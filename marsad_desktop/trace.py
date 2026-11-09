"""Relay 020: a light timing trace, off by default (Settings > Diagnostics, or MARSAD_TRACE=1).

When on, it writes one line per event to a file beside the window's preferences:

  click   a mouse release or key press, timed from the event to the moment the event loop is idle again
          (the handler and everything it queued for this turn have run, so the screen can repaint)
  call    each request to the service: path, milliseconds, the thread it ran on (main = the window froze for it)
  stall   any gap of more than 33 ms in a 10 ms heartbeat: a frame the window could not draw on time
  note    anything a screen chooses to mark (a start's outcome, a setting confirmed)

It holds nothing else and changes nothing: the file is the only output. `stats()` gives the slowest of each kind
for the smoke test and for the result table.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

_ON = [False]
_FILE: list = [None]
_LOCK = threading.Lock()
EVENTS: list = []                  # (kind, name, ms, thread) kept in memory while on, for stats()
STALL_MS = 33.0


def on() -> bool:
    return _ON[0]


def path() -> Path:
    from marsad_desktop.chain_screen import prefs
    return Path(prefs().fileName()).parent / "marsad_window_trace.log"


def set_on(flag: bool) -> None:
    """Switch the trace on or off now; the choice is kept in the window's preferences."""
    from marsad_desktop.chain_screen import prefs
    prefs().setValue("diag/trace", bool(flag))
    _apply(bool(flag))


def start_from_prefs() -> None:
    from marsad_desktop.chain_screen import prefs
    _apply(os.environ.get("MARSAD_TRACE") == "1" or prefs().value("diag/trace", False, type=bool))


def _apply(flag: bool) -> None:
    with _LOCK:
        _ON[0] = flag
        if flag and _FILE[0] is None:
            try:
                _FILE[0] = open(path(), "a", encoding="utf-8", buffering=1)
                _FILE[0].write(f"# trace started {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            except OSError:
                _FILE[0] = None
        elif not flag and _FILE[0] is not None:
            try:
                _FILE[0].close()
            except OSError:
                pass
            _FILE[0] = None
    _monitor_apply(flag)


def log(kind: str, name: str, ms: float | None = None) -> None:
    if not _ON[0]:
        return
    th = "main" if threading.current_thread() is threading.main_thread() else "pool"
    with _LOCK:
        EVENTS.append((kind, name, ms, th))
        if len(EVENTS) > 20000:
            del EVENTS[:5000]
        f = _FILE[0]
        if f is not None:
            try:
                f.write(f"{time.strftime('%H:%M:%S')} {kind:5} {th:4} "
                        f"{'' if ms is None else f'{ms:8.1f} ms'}  {name}\n")
            except OSError:
                pass


def stats(kind: str, top: int = 10) -> list:
    with _LOCK:
        rows = [e for e in EVENTS if e[0] == kind and e[2] is not None]
    return sorted(rows, key=lambda e: -e[2])[:top]


# ---------------------------------------------------------------- the click and stall monitor (Qt)

_MON: list = [None]


def _monitor_apply(flag: bool) -> None:
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        return
    app = QApplication.instance()
    if app is None:
        return
    if flag and _MON[0] is None:
        _MON[0] = Monitor(app)
    elif not flag and _MON[0] is not None:
        _MON[0].stop()
        _MON[0] = None


def _monitor_class():
    from PySide6.QtCore import QEvent, QObject, QTimer

    class _Monitor(QObject):
        """App-wide: a click or key, timed to the next idle turn of the event loop; and a 10 ms heartbeat whose
        late beats are the frames the window could not draw on time."""

        def __init__(self, app):
            super().__init__()
            self.app = app
            self.app.installEventFilter(self)
            self.beat = QTimer()
            self.beat.setInterval(10)
            self.beat.timeout.connect(self._beat)
            self.last = time.perf_counter()
            self.beat.start()
            self.pending: list = []

        def stop(self):
            self.beat.stop()
            self.app.removeEventFilter(self)

        def _beat(self):
            t = time.perf_counter()
            gap = (t - self.last) * 1000
            self.last = t
            if gap > STALL_MS + 10:            # 10 ms is the beat itself
                log("stall", "heartbeat late", gap - 10)

        def eventFilter(self, obj, ev):
            et = ev.type()
            if et in (QEvent.Type.MouseButtonRelease, QEvent.Type.KeyPress) and obj.isWidgetType() \
                    and ev.timestamp() != getattr(self, "last_ts", None):   # once per event, not per parent it reaches
                self.last_ts = ev.timestamp()
                name = type(obj).__name__
                try:
                    w = self.app.widgetAt(ev.globalPosition().toPoint()) if et == QEvent.Type.MouseButtonRelease \
                        else self.app.focusWidget()
                    if w is not None:
                        name = (w.objectName() or type(w).__name__) + (f" “{w.text()[:30]}”" if hasattr(w, "text")
                                                                       and callable(w.text) else "")
                except Exception:  # noqa: BLE001
                    pass
                t0 = time.perf_counter()
                QTimer.singleShot(0, lambda n=name, t0=t0: log("click", n, (time.perf_counter() - t0) * 1000))
            return False

    return _Monitor


def Monitor(app):                                   # noqa: N802 - built lazily so this module imports without Qt
    return _monitor_class()(app)
