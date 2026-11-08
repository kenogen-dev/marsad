"""Relay 014: the practice station. Its launcher sets MARSAD_PRACTICE=1; the window then says so in its title
and a thin band, and its made-up radios read "practice radio (simulated)" instead of "not plugged in", so it is
never mistaken for the real station. Window only: the engine and the service know nothing of it."""
from __future__ import annotations

import os

SIMULATED = "practice radio (simulated)"


def on() -> bool:
    return os.environ.get("MARSAD_PRACTICE", "").strip() not in ("", "0")
