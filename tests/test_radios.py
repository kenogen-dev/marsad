"""The radios panel's service side (owner, 2026-11-07): which radios are plugged in (by serial,
from the device list, nothing opened), what each is doing, who surveys and who records, what each
can tune, and whether survey + record is possible. Made-up station; no radio involved."""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pytest

from marsad import live
from marsad.config import Station

DATA = Path(__file__).parent / "data"


@pytest.fixture
def station(tmp_path, monkeypatch):
    root = tmp_path / "st"
    root.mkdir()
    shutil.copy(DATA / "builder_station.json", root / "station.json")
    (root / "surveys").mkdir()
    monkeypatch.setattr(live.host, "running_sdr_programs", lambda: [])
    return Station.load(str(root))


def plugged(monkeypatch, *serials):
    monkeypatch.setattr(live, "usb_radios", lambda: {s: "made-up dongle" for s in serials})


def by_name(d):
    return {r["name"]: r for r in d["radios"]}


def test_two_radios_plugged_in_allow_survey_and_record(station, monkeypatch):
    plugged(monkeypatch, "00000011", "00000022")
    d = live.radios_status(station)
    r = by_name(d)
    assert r["radio-a"]["present"] and r["radio-b"]["present"]
    assert r["radio-a"]["doing"]["state"] == "ready" and r["radio-a"]["role"] == "survey"
    assert r["radio-b"]["role"] == "record"
    assert d["mode"]["radios"] == 2 and d["mode"]["survey_record"] is True
    assert "radio-a surveys and the radio-b records" in d["mode"]["text"]


def test_one_radio_says_what_it_can_and_cannot_do(station, monkeypatch):
    plugged(monkeypatch, "00000011")
    d = live.radios_status(station)
    assert by_name(d)["radio-b"]["doing"]["state"] == "absent"
    assert d["mode"]["survey_record"] is False and d["mode"]["radios"] == 1
    assert "no IQ is captured" in d["mode"]["text"] and "Fixed-frequency recording works" in d["mode"]["text"]


def test_presence_unknown_when_windows_will_not_say(station, monkeypatch):
    monkeypatch.setattr(live, "usb_radios", lambda: None)
    d = live.radios_status(station)
    assert all(r["present"] is None for r in d["radios"]) and d["mode"]["survey_record"] is False


def test_what_each_radio_is_doing(station, monkeypatch):
    plugged(monkeypatch, "00000011", "00000022")
    csv = station.surveys / "survey_band1_1h_t_20010101-0000.csv"
    csv.write_text("x\n")
    csv.with_suffix(".json").write_text(json.dumps({"receiver": "radio-a", "rtl_power": {"integration_s": 30}}))
    pool = station.root / "recordings" / "captures"
    pool.mkdir(parents=True)
    (pool / "recorder_status_00000022.json").write_text(json.dumps(
        {"state": "running", "updated": time.time(), "serial": "00000022", "receiver": "radio-b", "mode": "fixed",
         "job": {"target_mhz": 433.92}}))
    r = by_name(live.radios_status(station))
    assert r["radio-a"]["doing"] == {"state": "surveying", "text": "surveying: survey_band1_1h_t_20010101-0000"}
    assert r["radio-b"]["doing"]["state"] == "recording" and "433.9200 MHz" in r["radio-b"]["doing"]["text"]
    monkeypatch.setattr(live.host, "running_sdr_programs", lambda: ["sdrpp.exe"])
    csv.unlink()
    assert by_name(live.radios_status(station))["radio-a"]["doing"]["state"] == "blocked"


def test_roles_swap_and_refuse_one_radio_for_both(station, monkeypatch):
    plugged(monkeypatch, "00000011", "00000022")
    d = live.set_radio_roles(station, {"swap": True})
    assert d["roles"]["surveyor"] == "radio-b" and d["roles"]["recorder"] == "radio-a"
    assert live.saved_doc(station)["roles"] == {"surveyor": "radio-b", "recorder": "radio-a"}
    assert json.loads((station.root / "station.json").read_text())["recorder"]["roles"]["surveyor"] == "radio-a"
    with pytest.raises(ValueError):
        live.set_radio_roles(station, {"surveyor": "radio-a", "recorder": "radio-a"})


def test_what_each_radio_can_tune_and_both_together(station, monkeypatch):
    plugged(monkeypatch, "00000011", "00000022")
    d = live.radios_status(station)
    a = by_name(d)["radio-a"]
    assert a["tunable_mhz"] == [[52.0, 1100.0], [1250.0, 2200.0]]
    assert "blind 1100–1250 MHz (PLL gap)" in a["facts"] and any("rtl_power_fftw" in f for f in a["facts"])
    assert d["both_cover_mhz"] == [[52.0, 1100.0], [1250.0, 1766.0]]
    assert live.coverage_of(station, "radio-b", 1700, 1800) == pytest.approx(0.66)
    assert [n["title"] for n in a["notes"]] == ["Gain note"]                       # notes keyed on it (by tuner)
    assert by_name(d)["radio-b"]["notes"][0]["where"] == "300–400 MHz"


def test_a_disabled_radio_is_never_offered_and_keeps_its_role(station, monkeypatch):
    """Relay 011: the switch on a radio's card. Disabled: dimmed, never offered, survey + record says
    why, the role is kept, and re-enabling restores everything. The engine never reads it."""
    plugged(monkeypatch, "00000011", "00000022")
    d = live.set_radio_enabled(station, {"name": "radio-b", "enabled": False})
    r = by_name(d)
    assert r["radio-b"]["enabled"] is False and r["radio-b"]["doing"]["state"] == "disabled"
    assert r["radio-b"]["role"] == "record"                                        # kept in its role
    assert d["mode"]["survey_record"] is False and "radio-b is disabled" in d["mode"]["text"]
    chk = live.scan_checks(station, "radio-b", detect=False)
    assert chk["radio"]["found"] is False and chk["radio"].get("disabled")         # Start refuses on it
    with pytest.raises(ValueError, match="disabled"):
        live.scan_start(station, {"scan": dict(live.scan_from_preset(station, "band1"), rx="radio-b")})
    assert json.loads((station.root / "station.json").read_text()).get("disabled") is None   # never station.json
    d = live.set_radio_enabled(station, {"name": "radio-b", "enabled": True})
    assert by_name(d)["radio-b"]["enabled"] and d["mode"]["survey_record"] is True


def test_disabling_a_busy_radio_stops_nothing(station, monkeypatch):
    plugged(monkeypatch, "00000011", "00000022")
    csv = station.surveys / "survey_band1_1h_t_20010101-0000.csv"
    csv.write_text("x\n")
    csv.with_suffix(".json").write_text(json.dumps({"receiver": "radio-a", "rtl_power": {"integration_s": 30}}))
    r = by_name(live.set_radio_enabled(station, {"name": "radio-a", "enabled": False}))["radio-a"]
    assert r["doing"]["state"] == "surveying" and "keeps going" in r["doing"]["text"]
    assert csv.exists()
