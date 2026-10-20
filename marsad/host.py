"""The parts that differ between Windows and Linux/macOS.

Everything else in the package is plain Python. These four things are not:
listing running processes, starting a child in its own process group, asking
it to stop gracefully, and working out how to re-invoke ourselves.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

from .rtlpower import SDR_PROGRAMS, frozen

IS_WINDOWS = os.name == "nt"


def running_sdr_programs() -> list[str] | None:
    """Names of SDR programs running now. [] means clear, None means can't tell."""
    try:
        if IS_WINDOWS:
            out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                                 capture_output=True, text=True, timeout=30).stdout
            names = [line.split('","')[0].strip('"').strip().lower()
                     for line in out.splitlines() if line.startswith('"')]
        else:
            out = subprocess.run(["ps", "-eo", "comm="],
                                 capture_output=True, text=True, timeout=30).stdout
            names = [Path(line.strip()).name.lower() for line in out.splitlines() if line.strip()]
    except (OSError, subprocess.SubprocessError):
        return None
    wanted = {p.lower() + (".exe" if IS_WINDOWS else "") for p in SDR_PROGRAMS}
    found: list[str] = []
    for name in names:
        if name in wanted and name not in found:
            found.append(name)
    return found


def popen_group_flags() -> int:
    """Flags that put a child in its own process group.

    On Windows that is what makes CTRL_BREAK deliverable to just that child;
    on POSIX a plain SIGINT to the pid does the job and no flag is needed.
    """
    return subprocess.CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0


def ask_to_stop(proc: subprocess.Popen) -> None:
    """Ask a child to finish cleanly, so block-buffered output gets flushed.

    rtl_adsb and rtl_power install console handlers: Ctrl-Break on Windows and
    SIGINT elsewhere make them flush and exit. A hard kill would discard up to
    ~4 KB of buffered frames, which is most of a quiet run.
    """
    if IS_WINDOWS:
        proc.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        proc.send_signal(signal.SIGINT)


def self_command(*args: str, prefer_venv: Path | None = None) -> list[str]:
    """A command line that runs this same program with `args`.

    Frozen: the executable itself. From source: the interpreter running now,
    or the station's venv when one is given (the recording path is stdlib-only
    and may be running under a bare system Python without numpy).
    """
    if frozen():
        return [sys.executable, *args]
    py = sys.executable
    if prefer_venv and Path(prefer_venv).exists():
        py = str(prefer_venv)
    return [py, "-m", "marsad", *args]
