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


def ensure_service(folder, wait_s: float = 60.0) -> str:
    """An address serving this station: an answering one, or one started now. Raises RuntimeError in words."""
    found = serving(folder)
    if found:
        return found
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
        logs = Path(folder) / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        log = open(logs / f"window_service_{port}_{dt.datetime.now():%Y%m%d-%H%M%S}.log", "w", encoding="utf-8")
        subprocess.Popen([engine_python(), "-m", "marsad", "--station", str(folder), "live", "--port", str(port),
                          "--exit-after-idle", "5"], cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
    end = time.time() + wait_s
    while time.time() < end:
        d = ping(port, 1.0)
        if d and (not d.get("station") or _same(d["station"], folder)):
            return f"http://127.0.0.1:{port}"
        time.sleep(0.25)
    raise RuntimeError(f"The service for {folder} did not answer on {port} within {wait_s:.0f} s. Its log is in the "
                       "station's logs folder.")


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
