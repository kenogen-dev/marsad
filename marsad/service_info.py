"""Relay 027: which code a station's service runs, and whether anything is running on the station.

The window and a station's service are started at different times. A service started before an update keeps serving
the old code (it lived on for hours on a station with no recordings), and the window then asks it for things it does
not know ("unknown route /setup/radio_remove"). So the service reports a build stamp on /ping, the window compares it
with its own, and restarts a station's service only when nothing is running on it.

build_stamp()   a short hash of the engine's own source files, read once when the process starts. Two processes
                started from the same files give the same stamp; an edit to any engine file changes it.
busy(station)   what is running on a station now, in words (empty: nothing). Read from the station's files, so it
                also answers for a service from before this relay, which cannot say.

Stdlib only; nothing here opens a radio or stops anything.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from .config import Station

_STAMP: list = []


def build_stamp() -> str:
    """The engine's code version: 12 hex characters of a hash over every marsad/*.py (relative name and bytes), read
    once per process. A frozen build hashes what it ships the same way, or falls back to its version number."""
    if _STAMP:
        return _STAMP[0]
    from . import __version__
    stamp = stamp_of(Path(__file__).resolve().parent) or (
        f"v{__version__}" + ("-frozen" if getattr(sys, "frozen", False) else ""))
    _STAMP.append(stamp)
    return stamp


def stamp_of(pkg: Path) -> str | None:
    """12 hex characters over every .py under `pkg` (relative name and bytes, in name order); None with none."""
    h = hashlib.sha1()
    files = sorted(p for p in Path(pkg).rglob("*.py") if "__pycache__" not in p.parts)
    for p in files:
        try:
            h.update(p.relative_to(pkg).as_posix().encode())
            h.update(b"\0")
            h.update(p.read_bytes())
        except OSError:
            continue
    return h.hexdigest()[:12] if files else None


def busy(station: Station, probe_running: bool = False) -> list[str]:
    """What is running on this station now, each in a few words. Empty means a restart of its service interrupts
    nothing. Counted: a setup measurement or path check not yet ended, a recording still being written, a recorder
    (marsad watch) reporting, and a Probe-tab run (only the service knows that one: pass `probe_running`)."""
    out: list[str] = []
    if probe_running:
        out.append("a Probe-tab run")
    for j in setup_jobs_running(station):
        out.append(f"the {j.get('step')} measurement on {j.get('radio')}")
    try:
        from .radio_use import recording_active
        if station.surveys.exists():
            for f in list(station.surveys.glob("*.csv")) + list(station.surveys.glob("*.dat")):
                if recording_active(f):
                    out.append(f"the recording {f.name}")
    except Exception:  # noqa: BLE001 - a broken sidecar must not hide the rest
        pass
    try:
        from . import catalogue
        for d in catalogue.live_recorders(station):
            if d.get("running"):
                out.append(f"a recorder (marsad watch, {d.get('receiver') or d.get('serial') or 'a radio'})")
    except Exception:  # noqa: BLE001
        pass
    return out


def setup_jobs_running(station: Station) -> list[dict]:
    """Setup measurements and path checks started and not yet ended (no exit file from their wrapper)."""
    try:
        doc = json.loads((station.root / "station_setup.json").read_text(encoding="utf-8"))
        return [j for j in doc.get("jobs") or [] if isinstance(j, dict) and not j.get("done")
                and not Path(str(j.get("log") or "") + ".exit").exists()]
    except (OSError, ValueError, TypeError, AttributeError):
        return []


def stale_words() -> str:
    """What the window says when the service does not know a request: never "unknown route"."""
    return "This feature needs the station service to restart (it's from an older version)."
