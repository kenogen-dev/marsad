"""Relay 021: New Station, Add Radio, the per-radio setup checklist and its scan lock. Made-up stations in temporary
folders with made-up serials and made-up coordinates; no radio is opened and no real station is read."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from marsad import live, setup as S
from marsad.config import Station

FAKE_LOC = {"lat": 12.34567, "lon": -45.67891}          # made up


def new_station(tmp_path, **kw) -> Station:
    folder = tmp_path / "Stations" / "Bench"
    S.create_station(folder, "Bench", "a made-up test station", **kw)
    return Station.load(folder)


def test_a_new_station_is_clean_and_tracked(tmp_path):
    st = new_station(tmp_path)
    raw = json.loads(st.file.read_text())
    assert raw["station_name"] == "Bench" and raw["receivers"] == {} and raw["recorder"]["serials"] == {}
    assert st.receivers == {}                                   # no radios, not the built-in templates
    assert raw["defaults"]["chain_order"] == ""                 # no rig: the template's chain is not a drawing
    assert S.tracked(st) and not (st.root / "station.local.json").exists()
    assert not (st.root / "saved_scans.json").exists()
    from marsad import chain
    assert chain.summary(st)["chains"] == {} and chain.summary(st)["current"] is None   # no rigs, not the examples
    assert S.station_info(st) == {"root": str(st.root), "name": "Bench", "description": "a made-up test station",
                                  "tracked": True, "location_set": False}


def test_the_location_goes_only_to_the_stations_own_local_file(tmp_path):
    st = new_station(tmp_path, location=FAKE_LOC)
    local = json.loads((st.root / "station.local.json").read_text())
    assert local == {"location": FAKE_LOC}
    assert "12.34567" not in st.file.read_text()                    # never in station.json
    info = S.station_info(st)
    assert info["location_set"] is True and "12.34567" not in json.dumps(info)   # shown as set, never its value
    S.set_location(st.root, None)
    assert not (st.root / "station.local.json").exists()
    with pytest.raises(ValueError):
        S.set_location(st.root, {"lat": 120, "lon": 0})


def test_the_folder_must_be_new_or_empty_and_outside_every_station(tmp_path):
    st = new_station(tmp_path)
    assert "inside the station" in S.folder_problem(st.root / "sub")
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "x.txt").write_text("x")
    assert "not empty" in S.folder_problem(busy)
    assert S.folder_problem(tmp_path / "fresh") is None
    with pytest.raises(ValueError):
        S.create_station(st.root / "inner", "Inner")
    assert S.default_folder("My: station?", tmp_path) == tmp_path / "Marsad" / "Stations" / "My- station"


def test_add_radio_writes_a_receiver_the_engine_reads(tmp_path):
    st = new_station(tmp_path)
    S.add_radio(st, "90000001", "bench-a", "Rafael Micro R820T", maker="Made Up", product="Test Dongle")
    st = Station.load(st.root)
    assert st.receivers["bench-a"]["tuner"] == "r820t" and st.receivers["bench-a"]["range_mhz"] == [24, 1766]
    assert st.data["recorder"]["serials"]["bench-a"] == "90000001" and st.default_receiver == "bench-a"
    doc = S.load(st)
    # relay 024: the range is labelled an estimate, typical for the tuner, not measured on this unit
    assert doc["radios"]["bench-a"]["facts"]["range_mhz"]["source"].startswith("estimate: typical for")
    with pytest.raises(ValueError, match="already this station"):
        S.add_radio(st, "90000001", "bench-b", "r820t")
    with pytest.raises(ValueError, match="already has a radio"):
        S.add_radio(st, "90000002", "bench-a", "r820t")
    r = subprocess.run([sys.executable, "-m", "marsad", "--station", str(st.root), "run", "custom", "10m", "--freq",
                        "400M:500M:50k", "--gain", "29.7", "--receiver", "bench-a", "--chain", "made-up > bench-a",
                        "--conditions", "made-up", "--non-interactive", "--dry-run"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    out = r.stdout + r.stderr
    assert "bench-a" in out and "400" in out, out[-1500:]


def test_duplicate_serials_are_named_with_how_to_fix_them():
    found = [{"device": 0, "serial": "00000001"}, {"device": 1, "serial": "00000001"}, {"device": 2, "serial": "7"}]
    assert S.duplicate_serials(found) == ["00000001"]
    assert "rtl_eeprom" in S.DUP_HELP and "does not do this" in S.DUP_HELP


def test_every_step_is_an_existing_command_or_says_none_exists(tmp_path):
    st = new_station(tmp_path)
    S.add_radio(st, "90000001", "e", "e4000")
    S.add_radio(st, "90000002", "r", "r820t")
    st = Station.load(st.root)
    # relay 024: the tuner sweep measures the range of any tuner (it finds the radio by serial itself)
    assert S.step_command(st, "e", "range", False, 0)["args"] == ["tuner-sweep", "--receiver", "e"]
    assert S.step_command(st, "r", "range", True, 1)["args"] == ["tuner-sweep", "--receiver", "r", "--short"]
    rt = S.step_command(st, "r", "retune", True, 1)["args"]
    assert rt[0] == "calibrate-retune" and "--save" in rt and rt[rt.index("--gain") + 1] == "29.7"
    w = S.step_command(st, "e", "warmup", False, 0)["args"]
    assert w[:3] == ["run", "custom", "60m"] and w[w.index("--chain") + 1] == "50 ohm load > e"
    lo, hi = S.tile(st.receivers["e"])
    assert not (lo < 1268 and hi > 1105)                         # clear of the E4000's typical gap
    assert "draw it" in S.step_command(st, "e", "baseline", False, 0, "")["missing"]
    a = S.step_command(st, "r", "adsb", False, 1, "ant > r")["args"]
    assert a[0] == "adsb-check" and a[a.index("--seconds") + 1] == "60"


def test_results_are_kept_with_date_and_source_and_the_range_reaches_the_engine(tmp_path):
    st = new_station(tmp_path)
    S.add_radio(st, "90000001", "e", "e4000")
    st = Station.load(st.root)
    log = st.root / "logs" / "r.log"
    log.write_text("Found Elonics E4000 tuner\nE4K range: 52 to 2186 MHz\nE4K L-band gap: 1094 to 1239 MHz\n")
    res = S.result_of(st, {"step": "range", "log": str(log), "short": False}, {"exit_code": 0, "lines": []})
    assert res["ok"] and res["range_mhz"] == [52.0, 2186.0] and res["gaps_mhz"] == [[1094.0, 1239.0]]
    S.record(st, "e", res)
    S.record(st, "e", dict(res, at="2099-01-01T00:00:00"))
    st = Station.load(st.root)
    assert st.receivers["e"]["range_mhz"] == [52.0, 2186.0]
    r = S.load(st)["radios"]["e"]
    assert len(r["results"]["range"]) == 2 and r["facts"]["range_mhz"]["source"].startswith("measured")
    log.write_text("No E4000 tuner found, aborting.\n")
    bad = S.result_of(st, {"step": "range", "log": str(log)}, {"exit_code": 1, "lines": [], "reason": "No E4000"})
    assert not bad["ok"] and "did not report" in bad["note"]
    log.write_text("  retune per hop        47.3 ms  (x)\n")
    assert S.result_of(st, {"step": "retune", "log": str(log)}, {"exit_code": 0})["retune_ms_per_hop"] == 47.3


def test_the_checklist_locks_scans_until_every_step_is_done(tmp_path):
    st = new_station(tmp_path)
    assert S.lock_reason(st, "x") and "step 1" in S.lock_reason(st, "x")
    S.add_radio(st, "90000002", "r", "r820t")
    st = Station.load(st.root)
    why = S.lock_reason(st, "r")
    assert "step 2, Measure the radio" in why
    S.accept_typical(st, "r")
    assert "step 2" in S.lock_reason(st, "r", "")             # relay 024: an estimate never unlocks scanning
    S.record(st, "r", {"at": S.now(), "step": "range", "ok": True, "short": False, "range_mhz": [24.0, 1766.0],
                       "gaps_mhz": [], "by": "tuner sweep"})
    st = Station.load(st.root)
    for step, extra in (("retune", {"retune_ms_per_hop": 47}), ("warmup", {"warmup_min": 20}),
                        ("floor", {"floor_db": -36})):
        S.record(st, "r", dict({"at": S.now(), "step": step, "ok": True, "short": False}, **extra))
    assert "step 3, Draw the rig" in S.lock_reason(st, "r", "")
    assert "step 4, Confirm" in S.lock_reason(st, "r", "ant > r")
    S.confirm_rig(st, "r", "ant > r")
    assert "step 5, Path checks" in S.lock_reason(st, "r", "ant > r")
    assert "step 4" in S.lock_reason(st, "r", "ant > notch > r")       # the drawing changed: confirm again
    for step in ("adsb", "baseline"):
        S.record(st, "r", {"at": S.now(), "step": step, "ok": True, "short": False})
    assert S.lock_reason(st, "r", "ant > r") is None
    S.record(st, "r", {"at": S.now(), "step": "adsb", "ok": False, "short": False, "note": "FAIL"})
    assert "needs attention" not in (S.lock_reason(st, "r", "ant > r") or "") and S.lock_reason(st, "r", "ant > r")


def test_an_older_station_is_never_locked(tmp_path):
    root = tmp_path / "old"
    root.mkdir()
    (root / "station.json").write_text((Path(__file__).parent / "data" / "builder_station.json").read_text())
    st = Station.load(root)
    assert not S.tracked(st) and S.lock_reason(st, "radio-a") is None and live.setup_lock(st, "radio-a") is None


def test_the_service_refuses_a_scan_on_a_radio_not_set_up(tmp_path, monkeypatch):
    st = new_station(tmp_path)
    S.add_radio(st, "90000002", "r", "r820t")
    st = Station.load(st.root)
    monkeypatch.setattr(live, "find_radio", lambda st_, rx: {"found": True, "device": 0, "rx": rx, "text": "found"})
    monkeypatch.setattr(live.host, "sdr_program_counts", lambda extra=(): {})
    monkeypatch.setattr(live.host, "running_sdr_programs", lambda: [])
    chk = live.scan_checks(st, "r", detect=True)
    assert chk["radio"]["found"] is False and "step 2" in chk["radio"]["text"]
    launched = []
    monkeypatch.setattr(live, "launch_outside", lambda *a: launched.append(a) or {"pid": 1, "log": "x"})
    with pytest.raises(ValueError, match="not set up yet"):
        live.scan_start(st, {"scan": {"preset": "custom", "rx": "r", "durVal": 10, "unit": "m"},
                             "chain": "ant > r", "chain_confirmed": True, "problems_confirmed": True})
    assert not launched


def test_detecting_the_tuner_waits_while_a_radio_program_runs(tmp_path, monkeypatch):
    st = new_station(tmp_path)
    opened = []
    monkeypatch.setattr(live, "detect_receivers", lambda st_: opened.append(1) or {"found": [], "error": None})
    monkeypatch.setattr(live.host, "running_sdr_programs", lambda: ["SDRPlusPlus.exe"])
    monkeypatch.setattr(live, "radio_holders", lambda st_: [])
    # relay 027: an answer that names the program (so the window can show it), not an error
    busy = live.setup_post(st, "/setup/detect", {})
    assert busy["kind"] == "busy" and "radio program is running" in busy["problem"] and busy["found"] == []
    assert not opened                                           # rule 7: nothing was opened
    monkeypatch.setattr(live.host, "running_sdr_programs", lambda: [])
    got = live.setup_post(st, "/setup/detect", {})
    assert opened and got["found"] == []
