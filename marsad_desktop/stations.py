"""Relay 021: the stations the window knows, and a service for whichever one is open.

The list lives in AppData\\Local\\Marsad\\stations.json (MARSAD_APPDATA moves it, for tests): each station's name and
folder, and the one opened last. Nothing in it is a measurement; losing it means opening the stations again.

Each station is served by its own `marsad live --station <folder>`:
  - the home station (the folder ~/.marsad_home names) by the station's own app on 8731, as always;
  - any other station by a service the window starts on a free port in 8750-8759, with --exit-after-idle 5, kept alive
    by the window's 30 s check and gone 5 minutes after the window closes.
A service already answering for a folder (its /ping names the folder) is used rather than a second one started.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOME_PORT = 8731
OWN_PORTS = list(range(8750, 8760))
SEARCH_PORTS = [HOME_PORT] + list(range(8740, 8750)) + OWN_PORTS
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def appdata() -> Path:
    d = Path(os.environ.get("MARSAD_APPDATA") or Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Marsad")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _file() -> Path:
    return appdata() / "stations.json"


def registry() -> dict:
    try:
        d = json.loads(_file().read_text(encoding="utf-8"))
        if isinstance(d, dict):
            d.setdefault("stations", [])
            return d
    except (OSError, ValueError):
        pass
    return {"stations": [], "last": None}


def _same(a, b) -> bool:
    try:
        return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))
    except (OSError, ValueError):
        return False


def remember(folder, name: str | None = None, opened: bool = True) -> dict:
    """Add or refresh a station in the list; the one opened last is reopened next time."""
    d = registry()
    folder = str(Path(folder).resolve())
    e = next((s for s in d["stations"] if _same(s["folder"], folder)), None)
    if e is None:
        e = {"folder": folder, "name": name or Path(folder).name, "added": dt.datetime.now().isoformat(timespec="seconds")}
        d["stations"].append(e)
    if name:
        e["name"] = name
    if opened:
        e["last_opened"] = dt.datetime.now().isoformat(timespec="seconds")
        d["last"] = folder
    tmp = _file().with_suffix(".json.part")
    tmp.write_text(json.dumps(d, indent=1), encoding="utf-8")
    os.replace(tmp, _file())
    return e


def home_station() -> Path | None:
    """The folder ~/.marsad_home names: the station whose app is on 8731."""
    try:
        t = (Path.home() / ".marsad_home").read_text(encoding="utf-8").strip()
        return Path(t) if t else None
    except OSError:
        return None


def ping(port: int, timeout: float = 0.6) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8") or "{}")
            return d if d.get("app") == "marsad" else None
    except (OSError, ValueError):
        return None


def in_use(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) == 0


def serving(folder) -> str | None:
    """The address of a service already answering for this folder, if any."""
    home = home_station()
    for port in SEARCH_PORTS:
        if not in_use(port):
            continue
        d = ping(port)
        if not d:
            continue
        if d.get("station") and _same(d["station"], folder):
            return f"http://127.0.0.1:{port}"
        if port == HOME_PORT and not d.get("station") and home and _same(home, folder):
            return f"http://127.0.0.1:{port}"         # a service from before relay 021 names no station
    return None


def engine_python() -> str:
    """The Python that runs the engine (marsad) for a station's service."""
    env = os.environ.get("MARSAD_ENGINE_PYTHON")
    if env:
        return env
    v = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return str(v) if v.exists() else sys.executable



def _port_of(base: str) -> int | None:
    try:
        return int(base.rsplit(":", 1)[1].split("/")[0])
    except (IndexError, ValueError):
        return None


MISSING = "missing (folder not found)"


class MissingStation(RuntimeError):
    """Relay 028: a listed station whose folder (or its station.json) has gone. It is never re-created."""


def is_missing(folder) -> bool:
    return not (Path(folder) / "station.json").is_file()


def missing_text(folder) -> str:
    return (f"This station's folder was not found ({folder}). Marsad does not re-create it. If you deleted it, use "
            "Remove from list; if you moved it, open it from its new place.")


def forget(folder) -> bool:
    """Relay 028: Remove from list. Forgets the station in the window's settings and touches no files. True when it was
    listed."""
    d = registry()
    keep = [s for s in d["stations"] if not _same(s["folder"], folder)]
    if len(keep) == len(d["stations"]):
        return False
    d["stations"] = keep
    if d.get("last") and _same(d["last"], folder):
        d["last"] = None
    tmp = _file().with_suffix(".json.part")
    tmp.write_text(json.dumps(d, indent=1), encoding="utf-8")
    os.replace(tmp, _file())
    return True


def forget_last() -> None:
    """After Close Station: the next start opens the welcome dialog, not the station just closed."""
    d = registry()
    d["last"] = None
    tmp = _file().with_suffix(".json.part")
    tmp.write_text(json.dumps(d, indent=1), encoding="utf-8")
    os.replace(tmp, _file())


def show_in_explorer(folder) -> str:
    """Open the station's folder in File Explorer (a missing one: the nearest folder above it that exists)."""
    p = Path(folder)
    target = p if p.exists() else next((q for q in p.parents if q.exists()), Path.home())
    if os.name == "nt":
        subprocess.Popen(["explorer", str(target)], creationflags=NO_WINDOW)
    return str(target)


def _start_own(folder, port: int) -> None:
    if is_missing(folder):                       # relay 028: never create a station's folder by opening it
        raise MissingStation(missing_text(folder))
    logs = Path(folder) / "logs"
    logs.mkdir(exist_ok=True)
    with open(logs / f"window_service_{port}_{dt.datetime.now():%Y%m%d-%H%M%S}.log", "w", encoding="utf-8") as log:
        subprocess.Popen([engine_python(), "-m", "marsad", "--station", str(folder), "live", "--port", str(port),
                          "--exit-after-idle", "5"], cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)


def release_station(base: str | None, folder, wait_s: float = 15.0) -> dict:
    """Relay 028: the window is leaving this station (Close Station, a switch, quitting): stop the station's own
    service so nothing holds its folder. Only a service the window starts (8750-8759) that serves this folder, and only
    when nothing runs on the station (rule 1). The home station's app on 8731 and anything else are left alone.
    {"released", "text", "busy"}."""
    port = _port_of(base) if base else None
    if port not in OWN_PORTS:
        return {"released": False, "busy": [],
                "text": "Its service is not one this window started, so it is left running."}
    d = ping(port, 2.0)
    if not d:
        return {"released": True, "busy": [], "text": "Nothing holds the station's folder."}
    if d.get("station") and not _same(d["station"], folder):
        return {"released": False, "busy": [], "text": "That service serves another station; left alone."}
    if not any((Path(folder) / "logs").glob(f"window_service_{port}_*.log")):
        # started by something other than a window (a check, a person): not the window's to stop
        return {"released": False, "busy": [], "text": "Its service was not started by a Marsad window, so it is left "
                                                       "running."}
    busy = station_busy(folder, port, d) if not is_missing(folder) else []
    if busy:
        return {"released": False, "busy": busy,
                "text": "Still running on this station: " + "; ".join(busy) + ". Its service stays until that ends "
                        "and closes 5 minutes after; until then Windows says the folder is in use."}
    ok = stop_listener(port, wait_s)
    return {"released": ok, "busy": [],
            "text": "Nothing holds its folder now." if ok else
                    f"The station's service on {port} did not stop; its folder may still be in use."}


def _wait_answer(folder, port: int, wait_s: float, build: str | None = None) -> str:
    end = time.time() + wait_s
    while time.time() < end:
        d = ping(port, 1.0)
        if d and (not d.get("station") or _same(d["station"], folder)) and (build is None or d.get("build") == build):
            return f"http://127.0.0.1:{port}"
        time.sleep(0.25)
    raise RuntimeError(f"The service for {folder} did not answer on {port} within {wait_s:.0f} s. Its log is in the "
                       "station's logs folder.")


# ---- relay 027: a service older than this window

def own_build() -> str | None:
    from marsad_desktop.service import own_build as ob
    return ob()


def check_service(base: str, folder) -> dict:
    """Whether the service at `base` runs this window's code, and what is running on its station:
    {"stale", "home", "own_port", "busy": [words], "build", "mine"}. A service from before relay 027 reports no build,
    so it reads as stale."""
    port = _port_of(base)
    d = ping(port, 2.0) if port else None
    mine, theirs = own_build(), (d or {}).get("build")
    out = {"stale": bool(mine) and bool(d) and theirs != mine, "home": port == HOME_PORT,
           "own_port": port in OWN_PORTS, "build": theirs, "mine": mine, "busy": []}
    if out["stale"]:
        out["busy"] = station_busy(folder, port, d)
    return out


def station_busy(folder, port: int | None, ping_answer: dict | None = None) -> list[str]:
    """What is running on the station now, in words (marsad.service_info.busy). The Probe tab's run is known only to
    the service: from /ping on new services, from /probe/status on older ones."""
    from marsad.config import Station
    from marsad.service_info import busy
    probe = bool((ping_answer or {}).get("probe_running"))
    if ping_answer is not None and "probe_running" not in ping_answer and port:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/probe/status", timeout=2.0) as r:
                st = json.loads(r.read().decode("utf-8") or "{}").get("state")
            from marsad.webapp import BUSY_STATES
            probe = st in BUSY_STATES
        except (OSError, ValueError, ImportError):
            pass
    try:
        return busy(Station.load(Path(folder)), probe_running=probe)
    except Exception:  # noqa: BLE001 - when it cannot be read, count it as busy: never restart blind
        return ["(could not read what is running on this station)"]


def stop_listener(port: int, wait_s: float = 15.0) -> bool:
    """Stop the service listening on one of the window's own station ports (never 8731, never a test viewer), and
    its venv launcher with it. True once the port is free."""
    if port not in OWN_PORTS:
        raise ValueError(f"The window only restarts the services it starts (ports 8750-8759), not {port}.")
    from marsad import webapp
    pid = webapp.listener_pid(port)
    if pid:
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"] if os.name == "nt" else ["kill", str(pid)],
                           capture_output=True, timeout=20, stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            pass
    end = time.time() + wait_s
    while time.time() < end:
        if not in_use(port):
            return True
        time.sleep(0.25)
    return not in_use(port)


def restart_service(base: str, folder, force_home: bool = False) -> dict:
    """Restart a station's service on this window's code: {"ok", "base", "text"}.

    A station the window serves (8750-8759): only when nothing runs on it (rule 1); stopped and started again on the
    same port. The home station (8731): `marsad web restart`, which refuses while a Probe-tab run records; the window
    offers it and never does it by itself. Anything else: refused in words."""
    port = _port_of(base)
    if port == HOME_PORT:
        if not force_home:
            return {"ok": False, "base": base, "text": "The home station's service is restarted only when you press "
                                                       "Restart now."}
        busy = station_busy(folder, port, ping(port, 2.0))
        if busy:
            return {"ok": False, "base": base, "busy": busy,
                    "text": "Not restarted: " + "; ".join(busy) + " is running. Restart it once that has ended."}
        r = subprocess.run([engine_python(), "-m", "marsad", "--station", str(folder), "web", "restart"],
                           cwd=str(ROOT), capture_output=True, text=True, timeout=120, creationflags=NO_WINDOW)
        said = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()
        if r.returncode != 0:
            return {"ok": False, "base": base, "text": "Not restarted: " + (" ".join(said[-2:]) or f"exit {r.returncode}")}
        return {"ok": True, "base": base, "text": "Station service updated."}
    if port not in OWN_PORTS:
        return {"ok": False, "base": base, "text": f"The service on {port} was not started by this window, so it is "
                                                   "left alone."}
    busy = station_busy(folder, port, ping(port, 2.0))
    if busy:
        return {"ok": False, "base": base, "busy": busy,
                "text": "Not restarted: " + "; ".join(busy) + " is running on this station. It restarts when that ends."}
    if not stop_listener(port):
        return {"ok": False, "base": base, "text": f"The old service on {port} did not stop."}
    _start_own(folder, port)
    return {"ok": True, "base": _wait_answer(folder, port, 60.0, build=own_build()), "text": "Station service updated."}


def ensure_service_ex(folder, wait_s: float = 60.0) -> dict:
    """An address serving this station, and what the window should say about it:
    {"base", "note", "stale", "home", "busy"}. Relay 027: a found service on older code is restarted at once when it
    is one of the window's own (8750-8759) and nothing runs on its station ("Station service updated"); while a run is
    on, or for the home station on 8731, it is kept and `stale` says so (the window shows why, and Restart now for the
    home station). Raises RuntimeError in words; MissingStation (relay 028) for a folder that has gone, which is never
    re-created."""
    if is_missing(folder):
        raise MissingStation(missing_text(folder))
    found = serving(folder)
    if found:
        st = check_service(found, folder)
        if not st["stale"]:
            return {"base": found, "note": "", "stale": False, "home": st["home"], "busy": []}
        if st["own_port"] and not st["busy"]:
            r = restart_service(found, folder)
            if r["ok"]:
                return {"base": r["base"], "note": r["text"], "stale": False, "home": False, "busy": []}
            return {"base": found, "note": r["text"], "stale": True, "home": False, "busy": r.get("busy") or []}
        return {"base": found, "note": "", "stale": True, "home": st["home"], "busy": st["busy"]}
    home = home_station()
    if home and _same(home, folder):
        subprocess.run([engine_python(), "-m", "marsad", "--station", str(folder), "web", "start"], cwd=str(ROOT),
                       capture_output=True, timeout=90, creationflags=NO_WINDOW)
        port = HOME_PORT
    else:
        port = next((p for p in OWN_PORTS if not in_use(p)), None)
        if port is None:
            raise RuntimeError("Every port the window uses for stations (8750-8759) is taken. Close another window "
                               "and try again.")
        _start_own(folder, port)
    return {"base": _wait_answer(folder, port, wait_s), "note": "", "stale": False, "home": port == HOME_PORT,
            "busy": []}


def station_of(base: str) -> str | None:
    """Which folder the service at this address serves (None: it does not say)."""
    try:
        port = int(base.rsplit(":", 1)[1].split("/")[0])
    except (IndexError, ValueError):
        return None
    d = ping(port, 2.0)
    if d and d.get("station"):
        return d["station"]
    home = home_station()
    return str(home) if (d and port == HOME_PORT and home) else None
