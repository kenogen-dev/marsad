"""Keep the station's web app up on 8731, with no window to close by accident.

    marsad web status            is it up, which process, is a Probe-tab run recording
    marsad web start             start it in the background if nothing answers
    marsad web stop              stop the program listening on 8731
    marsad web restart [--force] stop it and start it again (refused while recording)

`marsad run` and the double-click launcher call `ensure_running` before a scan, so
the page is there whenever a scan is.

Why a hidden console and not DETACHED_PROCESS: the web app starts Probe-tab scans as
its children, and those stop rtl_power with Ctrl-Break. Ctrl-Break only travels inside
a console. Tested on this PC 2026-10-27: a child started with DETACHED_PROCESS could not
send Ctrl-Break at all ("The handle is invalid", grandchild never heard it); one started
with CREATE_NEW_CONSOLE and the window hidden sent it and the grandchild stopped cleanly,
with no window and no Windows Terminal tab appearing. Either way the app belongs to no
PowerShell window, so closing one never takes it down.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from . import host
from .config import Station

PORT = 8731
LOG_NAME = "live_view.log"
BUSY_STATES = ("starting", "recording", "stopping", "analysing")


def _get_json(port: int, path: str, timeout: float = 1.5):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
            return json.load(r)
    except Exception:
        return None


def viewer_answers(port: int = PORT) -> dict | None:
    """The web app's /surveys answer if a Marsad web app serves `port`, else None."""
    data = _get_json(port, "/surveys")
    return data if isinstance(data, dict) else None


def probe_state(port: int = PORT) -> str | None:
    """The Probe tab's run state ('idle', 'recording', ...), or None if it cannot be read."""
    data = _get_json(port, "/probe/status")
    return data.get("state") if isinstance(data, dict) else None


def listener_pid(port: int = PORT) -> int | None:
    """The process listening on 127.0.0.1:`port` (or all addresses), from netstat."""
    if not host.IS_WINDOWS:
        try:
            out = subprocess.run(["lsof", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"],
                                 capture_output=True, text=True, timeout=10).stdout
            return int(out.split()[0]) if out.split() else None
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                             text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        parts = line.split()
        if (len(parts) >= 5 and parts[3].upper() == "LISTENING"
                and re.search(rf"[:.]{port}$", parts[1])):
            try:
                return int(parts[4])
            except ValueError:
                pass
    return None


def launch_background(station: Station, port: int = PORT) -> subprocess.Popen:
    """Start `marsad live` on `port` with no window, own working folder, and no idle
    timeout on 8731 (any other port is a test viewer and gets 5 min).

    Output goes to reports/live_view.log so a failure to start can be read afterwards.
    """
    cmd = host.self_command("live", "--station", str(station.root), "--port", str(port))
    if port != PORT:
        # A test viewer (8740-8749, e.g. the launcher under MARSAD_PORT) must not be
        # left running: the house rule is that it exits 5 min after the last scan.
        cmd += ["--exit-after-idle", "5"]
    log_path = Path(station.reports) / LOG_NAME
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = open(log_path, "a", encoding="utf-8")
    log.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')}  {subprocess.list2cmdline(cmd)}\n")
    log.flush()
    kw: dict = {"stdout": log, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL,
                "cwd": str(station.root)}
    if host.IS_WINDOWS:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0                                   # SW_HIDE
        kw["startupinfo"] = si
        kw["creationflags"] = subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    try:
        return subprocess.Popen(cmd, **kw)
    finally:
        log.close()                                          # the child holds its own handle


def wait_until(check, timeout: float, step: float = 0.25) -> bool:
    end = time.time() + timeout
    while True:
        if check():
            return True
        if time.time() >= end:
            return False
        time.sleep(step)


def start(station: Station, port: int = PORT, timeout: float = 20.0) -> tuple[str, int | None]:
    """Start the web app unless something already answers. Returns (outcome, pid).

    outcome: 'up' (was already running), 'started', 'blocked' (port held by
    something that is not the web app), 'failed' (did not answer in time).
    """
    if viewer_answers(port) is not None:
        return "up", listener_pid(port)
    if host.port_in_use(port):
        return "blocked", listener_pid(port)
    try:
        proc = launch_background(station, port)
    except (OSError, subprocess.SubprocessError):
        return "failed", None
    if not wait_until(lambda: viewer_answers(port) is not None, timeout):
        return "failed", proc.pid
    return "started", listener_pid(port) or proc.pid


def ensure_running(station: Station, port: int = PORT) -> str:
    """The check before every scan: print one line, never raise, never stop the scan."""
    try:
        outcome, pid = start(station, port)
    except Exception as exc:                     # nothing here may cost a recording
        outcome, pid = "failed", None
        print(f"  web app: could not check or start it ({exc}); the scan goes on.")
        return outcome
    url = f"http://127.0.0.1:{port}/"
    if outcome == "up":
        info = viewer_answers(port) or {}
        note = "" if info.get("follow", True) else \
            " - but it is pinned to one survey and will not switch to this scan"
        print(f"  web app: already up on {port}   {url}{note}")
    elif outcome == "started":
        print(f"  web app: started on {port}, pid {pid}   {url}   (no window; `marsad web stop` ends it)")
    elif outcome == "blocked":
        print(f"  web app: port {port} is held by another program (pid {pid}), not the web app;"
              " the scan goes on without a page.")
    else:
        print(f"  web app: did not start on {port} (see reports/{LOG_NAME}); the scan goes on.")
    return outcome


def stop(port: int = PORT, timeout: float = 10.0) -> tuple[bool, int | None]:
    """Stop whatever listens on `port`. Returns (port now free, pid that was stopped)."""
    pid = listener_pid(port)
    if pid is None:
        return not host.port_in_use(port), None
    try:
        if host.IS_WINDOWS:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=15)
        else:
            import os
            import signal
            os.kill(pid, signal.SIGTERM)
    except (OSError, subprocess.SubprocessError):
        pass
    return wait_until(lambda: not host.port_in_use(port), timeout), pid


def restart_refusal(port: int = PORT, force: bool = False) -> str | None:
    """Why a restart must not happen now, or None if it may.

    A Probe-tab scan is a child of the web app: stopping the app ends that scan.
    A scan started from PowerShell (`marsad run`) is not affected, and the new
    app picks it up again on its own.
    """
    if force:
        return None
    state = probe_state(port)
    if state in BUSY_STATES:
        return (f"a Probe-tab run is {state} - it belongs to the web app and would be cut off. "
                "Stop it first, or wait for it, or use --force.")
    return None


def main(argv: list, station: Station, prog: str = "marsad web") -> int:
    ap = argparse.ArgumentParser(prog=prog, description="Start, stop or check the web app on 8731.")
    ap.add_argument("action", choices=["start", "stop", "status", "restart"])
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--force", action="store_true",
                    help="restart/stop even while a Probe-tab run is recording")
    a = ap.parse_args(argv)
    port = a.port

    if a.action == "status":
        info = viewer_answers(port)
        pid = listener_pid(port)
        if info is None:
            held = f"held by another program (pid {pid})" if host.port_in_use(port) else "nothing"
            print(f"  web app: not running on {port} ({held})")
            return 1
        print(f"  web app: up on {port}, pid {pid}   http://127.0.0.1:{port}/")
        print(f"  follows the newest scan: {'yes' if info.get('follow', True) else 'no (pinned)'}")
        print(f"  Probe-tab run: {probe_state(port) or 'unknown'}")
        return 0

    if a.action == "start":
        return 0 if ensure_running(station, port) in ("up", "started") else 1

    why = restart_refusal(port, a.force)
    if why:
        print(f"  web app: not {'restarted' if a.action == 'restart' else 'stopped'}: {why}")
        return 1
    if viewer_answers(port) is None and host.port_in_use(port) and not a.force:
        print(f"  web app: port {port} is held by a program that is not the web app; "
              "not touching it without --force.")
        return 1
    free, pid = stop(port)
    if not free:
        print(f"  web app: could not free port {port} (pid {pid}).")
        return 1
    print(f"  web app: stopped (pid {pid})" if pid else f"  web app: was not running on {port}")
    if a.action == "stop":
        return 0
    outcome, pid = start(station, port)
    if outcome == "started":
        print(f"  web app: started on {port}, pid {pid}   http://127.0.0.1:{port}/")
        return 0
    print(f"  web app: did not come back on {port} ({outcome}; see reports/{LOG_NAME})")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], Station.load(None)))
