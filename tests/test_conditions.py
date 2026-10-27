"""auto_conditions: the sidecar block marsad measures for itself (2026-10-26).

The weather fetch is never made for real here: a stand-in opener returns a
canned Open-Meteo reply, or raises, so the suite runs offline.
"""
from __future__ import annotations

import datetime as dt
import io
import json

from marsad import analyze, conditions

LOC = {"location": {"lat": 51.5, "lon": -0.12}}

REPLY = {
    "current_units": {"temperature_2m": "°C", "relative_humidity_2m": "%",
                      "surface_pressure": "hPa", "cloud_cover": "%", "precipitation": "mm",
                      "wind_speed_10m": "km/h", "wind_direction_10m": "°"},
    "current": {"time": "2026-10-26T14:00", "temperature_2m": 11.4,
                "relative_humidity_2m": 82, "surface_pressure": 1008.6, "cloud_cover": 90,
                "precipitation": 0.2, "wind_speed_10m": 17.3, "wind_direction_10m": 230,
                "is_day": 1, "weather_code": 61},
}


def canned(url, timeout):
    assert "api.open-meteo.com" in url and "latitude=51.5000" in url
    assert timeout <= 5
    return io.BytesIO(json.dumps(REPLY).encode())


def offline(url, timeout):
    raise OSError("network unreachable")


def test_fetch_parses_reply():
    w = conditions.fetch_weather(51.5, -0.12, opener=canned)
    assert w["temperature"] == 11.4 and w["temperature_unit"] == "°C"
    assert w["humidity"] == 82 and w["pressure"] == 1008.6
    assert w["is_day"] is True and w["weather_code"] == 61
    assert "error" not in w


def test_fetch_failure_is_recorded_not_raised():
    w = conditions.fetch_weather(51.5, -0.12, opener=offline)
    assert "network unreachable" in w["error"]


def test_no_location_records_the_reason():
    snap = conditions.snapshot({}, None, fetch=lambda *a: 1 / 0)
    assert "location" in snap["weather"]["error"]
    assert "location" in snap["sun"]["error"]
    assert snap["indoor"] is None


def test_broken_fetch_cannot_break_a_run():
    def explodes(lat, lon):
        raise RuntimeError("boom")
    snap = conditions.snapshot(LOC, None, fetch=explodes)
    assert "boom" in snap["weather"]["error"]
    assert "elevation_deg" in snap["sun"]


def test_sun_noon_and_midnight():
    # Greenwich, equinox: elevation near 90 - 51.5 = 38.5 at solar noon, far below at midnight.
    tz = dt.timezone.utc
    noon = conditions.sun_position(51.48, 0.0, dt.datetime(2026, 3, 20, 12, 7, tzinfo=tz))
    assert abs(noon["elevation_deg"] - 38.5) < 1.0 and noon["state"] == "day"
    assert 170 < noon["azimuth_deg"] < 190
    night = conditions.sun_position(51.48, 0.0, dt.datetime(2026, 3, 20, 0, 7, tzinfo=tz))
    assert night["elevation_deg"] < -30 and night["state"] == "night"


def test_previous_recording_found(tmp_path):
    (tmp_path / "old.json").write_text(json.dumps(
        {"finished_local": "2026-10-26T10:00:00", "duration_actual_s": 3600, "receiver": "xtr"}))
    (tmp_path / "later.json").write_text(json.dumps({"finished_local": "2026-10-26T13:00:00"}))
    (tmp_path / "broken.json").write_text("{not json")
    prev = conditions.last_recording(tmp_path, dt.datetime(2026, 10, 26, 12, 0))
    assert prev["survey"] == "old" and prev["ended_s_ago"] == 7200 and prev["streamed_s"] == 3600


def test_block_and_report_rows(tmp_path):
    block = conditions.new_block(LOC, tmp_path, "this-run",
                                 fetch=lambda la, lo: conditions.fetch_weather(la, lo, opener=canned))
    assert block["location"] == {"lat": 51.5, "lon": -0.12}
    assert block["end"] is None
    block["end"] = block["start"]
    rows = conditions.table_rows(block)
    assert len(rows) == 2 and "11.4 C outside" in rows[0] and "82% RH" in rows[0]
    assert "measured at end" in rows[1]
    json.dumps(block)                         # must be storable in the sidecar

    sc = {"preset": "LS", "rtl_power": {"gain_db": 29}, "conditions": "typed text",
          "auto_conditions": block}
    text = "\n".join(analyze.sidecar_lines(sc))
    assert "Conditions, measured at start" in text and "typed text" in text
    sc_fftw = dict(sc, engine="rtl_power_fftw", rtl_power_fftw={"gain_db": 29})
    assert "measured at start" in "\n".join(analyze.sidecar_lines(sc_fftw))


def test_old_sidecars_unchanged():
    assert not any("measured" in line for line in analyze.sidecar_lines({"preset": "LS"}))
