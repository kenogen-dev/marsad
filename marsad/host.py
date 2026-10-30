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


def windows_process_names() -> list[str]:
    """Executable names of every running process, from the ToolHelp snapshot API.

    Not `tasklist`: on this PC (2026-10-29) tasklist started from Python never
    returns - 20-30 s and a timeout every time, even with stdin closed - so the
    SDR-in-use check was quietly answering "can't tell". This is one system call."""
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)              # TH32CS_SNAPPROCESS
    if snap in (None, wintypes.HANDLE(-1).value):
        raise OSError(ctypes.get_last_error(), "CreateToolhelp32Snapshot failed")
    names = []
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(e)
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            names.append(e.szExeFile.lower())
            ok = k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    return names


def kill_pid(pid: int) -> None:
    """End a process now. On Windows os.kill with SIGTERM is TerminateProcess, a direct
    call: `taskkill` started from Python hangs on this PC the way tasklist does, which
    made `marsad web stop` time out and leave 8731 running (2026-10-29)."""
    os.kill(pid, signal.SIGTERM)


def running_sdr_programs() -> list[str] | None:
    """Names of SDR programs running now. [] means clear, None means can't tell."""
    try:
        if IS_WINDOWS:
            names = windows_process_names()
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


def port_in_use(port: int, host_addr: str = "127.0.0.1") -> bool:
    """True when something already answers on the port (a running viewer, usually)."""
    import socket
    with socket.socket() as sk:
        sk.settimeout(0.3)
        return sk.connect_ex((host_addr, port)) == 0


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
