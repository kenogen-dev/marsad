"""Measured conditions: what the station records about itself at each run.

The hand-typed `--conditions` text goes stale because it is copied from run to
run. This module gathers the parts a program can know without anyone typing
them, and `marsad run` stores them in the sidecar as `auto_conditions`, once at
the start of the run and once at the end:

  weather  outdoor temperature, humidity, pressure, cloud, rain, wind, from
           Open-Meteo's free no-key API (https://open-meteo.com), for the
           station's `location` in station.json. One request, 5 s timeout.
  sun      sun elevation and azimuth and day / twilight / night, computed here
           from the location and the clock (no network).
  pc       Windows uptime, seconds since the last keyboard or mouse input (the
           displays sleep after a set idle time, so this is the cheap proxy),
           and how warm the SDR probably is: when the previous recording ended
           and how long it ran.
  indoor   null until there is a room thermometer to read.

Nothing here may stop or delay a run beyond the one 5 s timeout: every part
catches its own errors and records the reason instead of a value.

Stdlib only.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import sys
import urllib.parse
import urllib.request
from pathlib import Path

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
WEATHER_FIELDS = ("temperature_2m", "relative_humidity_2m", "surface_pressure",
                  "cloud_cover", "precipitation", "wind_speed_10m",
                  "wind_direction_10m", "is_day", "weather_code")
TIMEOUT_S = 5.0


# ----------------------------------------------------------------------------
# location
# ----------------------------------------------------------------------------
def location_of(station_data: dict) -> tuple[float, float] | None:
    """(lat, lon) from station.json's `location`, or None if absent or malformed."""
    loc = (station_data or {}).get("location")
    if not isinstance(loc, dict):
        return None
    try:
        lat, lon = float(loc["lat"]), float(loc["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


# ----------------------------------------------------------------------------
# weather (network)
# ----------------------------------------------------------------------------
def fetch_weather(lat: float, lon: float, timeout: float = TIMEOUT_S, opener=None) -> dict:
    """Current outdoor weather from Open-Meteo. Never raises.

    Returns the values with units, or {"error": "..."} saying why there are none.
    `opener` is urllib.request.urlopen unless a test passes a stand-in.
    """
    q = urllib.parse.urlencode({
        "latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}",
        "current": ",".join(WEATHER_FIELDS),
        "wind_speed_unit": "kmh", "timezone": "auto",
    })
    url = f"{OPEN_METEO}?{q}"
    try:
        with (opener or urllib.request.urlopen)(url, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        cur = data["current"]
        units = data.get("current_units", {})
    except Exception as exc:                       # offline, timeout, bad JSON: all the same here
        return {"error": f"{type(exc).__name__}: {exc}"[:200], "source": "open-meteo"}
    out = {"source": "open-meteo", "observed": cur.get("time")}
    names = {"temperature_2m": "temperature", "relative_humidity_2m": "humidity",
             "surface_pressure": "pressure", "cloud_cover": "cloud_cover",
             "precipitation": "precipitation", "wind_speed_10m": "wind_speed",
             "wind_direction_10m": "wind_direction", "is_day": "is_day",
             "weather_code": "weather_code"}
    for key, name in names.items():
        if key in cur:
            out[name] = cur[key]
            if units.get(key) and key not in ("is_day", "weather_code"):
                out[name + "_unit"] = units[key]
    if "is_day" in out:
        out["is_day"] = bool(out["is_day"])
    return out


# ----------------------------------------------------------------------------
# sun (local arithmetic)
# ----------------------------------------------------------------------------
def sun_position(lat: float, lon: float, when: dt.datetime) -> dict:
    """Sun elevation and azimuth in degrees (NOAA's approximation, ~0.1 deg).

    `when` must be timezone-aware. Elevation is geometric, without refraction;
    the day/twilight/night cut uses the usual -0.833 and -6 degrees.
    """
    t = when.astimezone(dt.timezone.utc)
    doy = t.timetuple().tm_yday
    hour = t.hour + t.minute / 60 + t.second / 3600
    g = 2 * math.pi / 365 * (doy - 1 + (hour - 12) / 24)      # fractional year, radians
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                       - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g)
            - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
            - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    tst = hour * 60 + eqtime + 4 * lon                         # true solar time, minutes
    ha = math.radians(tst / 4 - 180)
    phi = math.radians(lat)
    cos_zen = (math.sin(phi) * math.sin(decl)
               + math.cos(phi) * math.cos(decl) * math.cos(ha))
    zen = math.acos(max(-1.0, min(1.0, cos_zen)))
    elev = 90 - math.degrees(zen)
    az = math.degrees(math.atan2(math.sin(ha),
                                 math.cos(ha) * math.sin(phi) - math.tan(decl) * math.cos(phi))) + 180
    if elev > -0.833:
        state = "day"
    elif elev > -6:
        state = "civil twilight"
    elif elev > -18:
        state = "twilight"
    else:
        state = "night"
    return {"elevation_deg": round(elev, 2), "azimuth_deg": round(az % 360, 1), "state": state}


# ----------------------------------------------------------------------------
# PC and SDR state (local)
# ----------------------------------------------------------------------------
def _win_uptime_s() -> float | None:
    if sys.platform != "win32":
        return None
    import ctypes
    k32 = ctypes.windll.kernel32
    k32.GetTickCount64.restype = ctypes.c_ulonglong
    return k32.GetTickCount64() / 1000.0


def _win_idle_s() -> float | None:
    """Seconds since the last keyboard or mouse input to this session."""
    if sys.platform != "win32":
        return None
    import ctypes

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return None
    now = ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF
    return ((now - info.dwTime) & 0xFFFFFFFF) / 1000.0


def last_recording(surveys: Path, before: dt.datetime, exclude: str | None = None) -> dict | None:
    """The newest finished recording in surveys/ that ended before `before`.

    The E4000's PLL range slides 10-16 MHz as the dongle warms, so how long ago
    the SDR last streamed, and for how long, is part of the conditions.
    """
    best = None
    try:
        files = list(Path(surveys).glob("*.json"))
    except OSError:
        return None
    for p in files:
        if exclude and p.stem == exclude:
            continue
        try:
            sc = json.loads(p.read_text(encoding="utf-8"))
            end = dt.datetime.fromisoformat(sc["finished_local"])
        except Exception:
            continue
        if end > before:
            continue
        if best is None or end > best[0]:
            best = (end, p.stem, sc)
    if best is None:
        return None
    end, stem, sc = best
    return {"survey": stem, "receiver": sc.get("receiver"),
            "finished_local": end.isoformat(timespec="seconds"),
            "streamed_s": sc.get("duration_actual_s"),
            "ended_s_ago": round((before - end).total_seconds())}


def pc_state(surveys: Path | None = None, now: dt.datetime | None = None,
             exclude: str | None = None) -> dict:
    now = now or dt.datetime.now()
    out = {}
    for name, fn in (("uptime_s", _win_uptime_s), ("user_idle_s", _win_idle_s)):
        try:
            v = fn()
            if v is not None:
                out[name] = round(v)
        except Exception as exc:
            out[name + "_error"] = f"{type(exc).__name__}: {exc}"[:120]
    out["displays"] = ("not read: Windows offers no cheap query; user_idle_s against the "
                       "display-sleep timeout is the proxy")
    if surveys is not None:
        try:
            out["previous_recording"] = last_recording(surveys, now, exclude)
        except Exception as exc:
            out["previous_recording_error"] = f"{type(exc).__name__}: {exc}"[:120]
    return out


# ----------------------------------------------------------------------------
# the block
# ----------------------------------------------------------------------------
def snapshot(station_data: dict, surveys: Path | None = None, *, stem: str | None = None,
             fetch=fetch_weather, now: dt.datetime | None = None) -> dict:
    """One `auto_conditions` snapshot. Never raises, never waits more than ~5 s."""
    now = now or dt.datetime.now().astimezone()
    snap = {"taken_local": now.isoformat(timespec="seconds")}
    loc = location_of(station_data)
    if loc is None:
        reason = ("no `location` in station.json; add "
                  '"location": {"lat": ..., "lon": ...} to record weather and sun')
        snap["weather"] = {"error": reason}
        snap["sun"] = {"error": reason}
    else:
        try:
            snap["weather"] = fetch(*loc)
        except Exception as exc:                     # a stand-in fetch could still raise
            snap["weather"] = {"error": f"{type(exc).__name__}: {exc}"[:200]}
        try:
            snap["sun"] = sun_position(*loc, now)
        except Exception as exc:
            snap["sun"] = {"error": f"{type(exc).__name__}: {exc}"[:200]}
    try:
        snap["pc"] = pc_state(surveys, now.replace(tzinfo=None), stem)
    except Exception as exc:
        snap["pc"] = {"error": f"{type(exc).__name__}: {exc}"[:200]}
    snap["indoor"] = None
    return snap


def new_block(station_data: dict, surveys: Path | None = None, stem: str | None = None,
              fetch=fetch_weather) -> dict:
    """The sidecar's `auto_conditions`, with the start snapshot filled in."""
    return {"_note": "measured by marsad, not typed; the hand-typed text is `conditions`",
            "location": (dict(zip(("lat", "lon"), location_of(station_data)))
                         if location_of(station_data) else None),
            "start": snapshot(station_data, surveys, stem=stem, fetch=fetch),
            "end": None}


def summary(snap: dict | None) -> str:
    """One line for a human: '12.3 C, 81% RH, 1012 hPa, cloud 90%, ...'."""
    if not snap:
        return "(not taken)"
    parts = []
    w = snap.get("weather") or {}
    if w.get("error") and "`location`" in w["error"]:
        parts.append("weather and sun: no location set in station.json")
    elif w.get("error"):
        parts.append(f"weather: {w['error']}")
    else:
        if w.get("temperature") is not None:
            parts.append(f"{w['temperature']:g} C outside")
        if w.get("humidity") is not None:
            parts.append(f"{w['humidity']:g}% RH")
        if w.get("pressure") is not None:
            parts.append(f"{w['pressure']:g} hPa")
        if w.get("cloud_cover") is not None:
            parts.append(f"cloud {w['cloud_cover']:g}%")
        if w.get("precipitation") is not None:
            parts.append(f"rain {w['precipitation']:g} mm")
        if w.get("wind_speed") is not None:
            parts.append(f"wind {w['wind_speed']:g} km/h"
                         + (f" from {w['wind_direction']:g} deg" if w.get("wind_direction") is not None else ""))
    s = snap.get("sun") or {}
    if s.get("elevation_deg") is not None:
        parts.append(f"sun {s['elevation_deg']:+.1f} deg ({s['state']})")
    pc = snap.get("pc") or {}
    if pc.get("uptime_s") is not None:
        parts.append(f"PC up {pc['uptime_s'] / 3600:.1f} h")
    if pc.get("user_idle_s") is not None:
        parts.append(f"idle {pc['user_idle_s'] / 60:.0f} min")
    prev = pc.get("previous_recording")
    if prev:
        streamed = prev.get("streamed_s")
        parts.append(f"SDR last streamed {streamed / 60:.0f} min, ended {prev['ended_s_ago'] / 60:.0f} min ago"
                     if streamed else f"previous run ended {prev['ended_s_ago'] / 60:.0f} min ago")
    if snap.get("indoor") is None:
        parts.append("indoor: no sensor")
    return ", ".join(parts)


def table_rows(block: dict | None) -> list[str]:
    """Markdown table rows for report.md and scan_build.md."""
    if not block:
        return []
    rows = [f"| Conditions, measured at start | {summary(block.get('start'))} |"]
    if block.get("end"):
        rows.append(f"| Conditions, measured at end | {summary(block.get('end'))} |")
    return rows
