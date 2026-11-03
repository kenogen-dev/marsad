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

    Not `tasklist`: started from Python it can fail to return on some Windows setups, a
    20-30 s timeout every time even with stdin closed, which leaves the SDR-in-use check
    answering "can't tell". This is one system call."""
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
    call: `taskkill` started from Python can hang the way tasklist does, which would make
    `marsad web stop` time out and leave the web app running."""
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


def _windows_volume(path) -> str | None:
    """'C:\\' for a path, from the system's own idea of its volume."""
    import ctypes
    buf = ctypes.create_unicode_buffer(300)
    if ctypes.windll.kernel32.GetVolumePathNameW(str(path), buf, 300):
        return buf.value
    return None


def _windows_disks(path) -> set | None:
    """The physical disk numbers a path's volume sits on, or None if Windows will not say."""
    import ctypes
    from ctypes import wintypes
    vol = _windows_volume(path)
    if not vol or not vol[1:2] == ":":
        return None
    k = ctypes.windll.kernel32
    k.CreateFileW.restype = wintypes.HANDLE
    h = k.CreateFileW(f"\\\\.\\{vol[:2]}", 0, 3, None, 3, 0, None)     # no access needed for this query
    if h in (None, wintypes.HANDLE(-1).value):
        return None
    try:
        out = ctypes.create_string_buffer(8 + 24 * 8)
        got = wintypes.DWORD()
        if not k.DeviceIoControl(wintypes.HANDLE(h), 0x560000, None, 0, out, len(out), ctypes.byref(got), None):
            return None
        n = int.from_bytes(out.raw[:4], "little")
        return {int.from_bytes(out.raw[8 + 24 * i:12 + 24 * i], "little") for i in range(min(n, 8))}
    finally:
        k.CloseHandle(wintypes.HANDLE(h))


def same_physical_drive(a, b) -> bool | None:
    """True or False, or None when it cannot be told. Two partitions of one disk are the
    same drive here, because they fail together."""
    try:
        if IS_WINDOWS:
            da, db = _windows_disks(a), _windows_disks(b)
            if da and db:
                return bool(da & db)
            va, vb = _windows_volume(a), _windows_volume(b)
            return True if va and va == vb else None
        return os.stat(a).st_dev == os.stat(b).st_dev
    except (OSError, AttributeError, ValueError):
        return None


def filesystem_name(path) -> str | None:
    """'NTFS', 'exFAT', 'FAT32'... on Windows; None where it cannot be told."""
    if not IS_WINDOWS:
        return None
    try:
        import ctypes
        vol = _windows_volume(path)
        name = ctypes.create_unicode_buffer(64)
        if vol and ctypes.windll.kernel32.GetVolumeInformationW(vol, None, 0, None, None, None, name, 64):
            return name.value
    except (OSError, AttributeError, ValueError):
        pass
    return None


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
