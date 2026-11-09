"""Talking to the background service (`marsad live`). The window holds nothing of its
own: it reads and saves through these calls, so closing it changes nothing.

The address comes from MARSAD_SERVICE (e.g. http://127.0.0.1:8742), else the
station's usual http://127.0.0.1:8731.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from urllib.parse import quote

DEFAULT = "http://127.0.0.1:8731"


class ServiceError(Exception):
    """The service refused (its own words) or did not answer."""


class Service:
    def __init__(self, base: str | None = None, timeout: float = 5.0):
        self.base = (base or os.environ.get("MARSAD_SERVICE") or DEFAULT).rstrip("/")
        self.timeout = timeout

    def _call(self, path: str, body: dict | None = None, timeout: float | None = None) -> dict:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data,
                                     headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                out = json.loads(r.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            try:
                msg = json.loads(exc.read().decode("utf-8")).get("error")
            except Exception:  # noqa: BLE001
                msg = None
            raise ServiceError(msg or f"the service answered {exc.code}") from None
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise ServiceError(f"the service at {self.base} is not answering ({exc})") from None
        if isinstance(out, dict) and out.get("error"):
            raise ServiceError(str(out["error"]))
        return out

    def ping(self) -> bool:
        try:
            self._call("/ping")
            return True
        except ServiceError:
            return False

    def chains(self) -> dict:
        """{"chain": the chain summary, "layout": drawings, "serials", "roles"}."""
        return self._call("/chain/layout")

    def save(self, chain_doc: dict, layout: dict) -> dict:
        return self._call("/chain/layout/save", {"chains": chain_doc, "layout": layout})

    def use(self, chain_name: str) -> dict:
        """The same route the web page's "Use for runs" calls."""
        return self._call("/chain/use", {"name": chain_name})

    def use_rig(self, chain_names: list[str]) -> dict:
        """A split rig: every path made current at once; each run records its radio's own."""
        return self._call("/chain/use", {"name": chain_names[0], "rig": list(chain_names)})

    # -- Live Scan (relay 009): reads only, plus the existing Stop route
    def progress(self, file: str | None = None) -> dict:
        """The light question, asked often: where the run is, no sweep rows (/live/progress)."""
        return self._call("/live/progress" + (f"?file={quote(file)}" if file else ""))

    def data(self, since: int = 0, file: str | None = None) -> dict:
        """New sweeps since `since` (the same /data the web page reads)."""
        q = f"?since={int(since)}" + (f"&file={quote(file)}" if file else "")
        return self._call("/data" + q, timeout=240)     # opening a big finished survey takes a while

    def surveys(self) -> dict:
        return self._call("/surveys")

    def config(self) -> dict:
        return self._call("/config")

    def recorders(self) -> dict:
        """Every recorder's status file, as the Captures strip reads it (small files only)."""
        return self._call("/captures/recorders")

    def internal(self, file: str | None) -> dict:
        """The waterfall's internal layer: the receiver's own families and, when one matches, the
        floor of a terminated run with the same fingerprint (or why none matched)."""
        return self._call("/internal" + (f"?file={quote(file)}" if file else ""), timeout=60)

    def other(self, main: str | None, since: int = 0, file: str | None = None) -> dict:
        """Whatever else is recording besides `main` (the web page's second-radio panel)."""
        q = f"?since={int(since)}" + (f"&main={quote(main)}" if main else "") + (f"&file={quote(file)}" if file else "")
        return self._call("/other" + q)

    def stop_scan(self, file: str) -> dict:
        """The web page's own Stop: the recorder finishes its pass, keeps, analyses and archives."""
        return self._call("/scan/stop", {"file": file})

    # -- Starting scans (relay 010): the service composes, checks and launches; the window asks.
    # Long timeouts: these run off the window's thread, and the service can be busy for a minute
    # reading a big recording for the first time (every request waits for it meanwhile).
    def saved(self) -> dict:
        """Saved scans, the station presets, recorder sessions, last conditions, the receivers."""
        return self._call("/scan/saved", timeout=120)

    def saved_change(self, body: dict) -> dict:
        """save / rename / duplicate / archive / restore one saved scan or recorder session."""
        return self._call("/scan/saved", body, timeout=120)

    def plan(self, body: dict) -> dict:
        """Geometry, fingerprint, warnings, trade-off table and the full command for one scan."""
        return self._call("/scan/plan", body, timeout=120)

    def check(self, rx: str, detect: bool = True) -> dict:
        """The checks before a start: radio in use, radio by serial, the chain and its question."""
        return self._call("/scan/check", {"rx": rx, "detect": detect}, timeout=120)

    def start(self, body: dict) -> dict:
        """Start a scan (or its dry run). A real start is launched outside every process tree."""
        return self._call("/scan/start", body, timeout=150)

    def watch_plan(self, body: dict) -> dict:
        return self._call("/watch/plan", body, timeout=120)

    def watch_start(self, body: dict) -> dict:
        return self._call("/watch/start", body, timeout=150)

    # -- Relay 018: read-only, for the menu bar's Tools and File menus
    def last_runs(self) -> dict:
        """Each radio's most recent run: the chain text it recorded, when, its tag and device index."""
        return self._call("/chain/last_runs", timeout=30)

    def station_json(self) -> dict:
        """station.json exactly as it is on disk ({"text"}); station.local.json is never read."""
        return self._call("/station/json", timeout=30)

    # -- The radios (owner, 2026-11-07): presence by serial from Windows' device list, nothing opened
    def radios(self) -> dict:
        return self._call("/radios", timeout=30)

    def default_sources(self, rx: str) -> dict:
        """Where each radio-dependent default comes from (station.json, built-in, or the window's)."""
        return self._call(f"/explain/defaults?rx={quote(rx)}", timeout=30)

    def set_enabled(self, name: str, enabled: bool) -> dict:
        """Switch a radio on or off in the window: off, it is never offered for new work; nothing
        already running on it stops."""
        return self._call("/radios/enable", {"name": name, "enabled": bool(enabled)}, timeout=30)

    def set_roles(self, body: dict) -> dict:
        """{"surveyor", "recorder"} or {"swap": true}: who surveys and who records."""
        return self._call("/radios/roles", body, timeout=30)
