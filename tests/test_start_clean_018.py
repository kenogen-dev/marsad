"""Relay 018: clean starts, the menu bar's read-only tools, checks by level, the format bar's font, and the PC's USB
inputs. The logic under the window, on made-up rigs and a made-up station; no Qt, no radio."""
from __future__ import annotations

import datetime as dt
import json
import shutil
from pathlib import Path

import pytest

from marsad import chain as EC
from marsad import live
from marsad.config import Station
from marsad_desktop import live_data as L
from marsad_desktop import rig as R

DATA = Path(__file__).parent / "data"


def part(type_, name="", detail="", **kw):
    return {"type": type_, "name": name, "detail": detail, **kw}


def straight(*parts, cables=None):
    """A rig of parts in a row; cables: {index of the wire: [cable dicts]}."""
    rig = R.new_rig()
    for i, p in enumerate(parts):
        rig["nodes"].append({"id": f"n{i + 1}", "part": p, "x": i * 200, "y": 0})
        if i:
            w = {"from": f"n{i}", "port": 0, "to": f"n{i + 1}"}
            if cables and (i - 1) in cables:
                w["cables"] = cables[i - 1]
            rig["wires"].append(w)
    return rig


# ------------------------------------------------------------------ A1: what runs while Live Scan is blank
NOW = dt.datetime(2026, 1, 1, 12, 0, 0)


def test_banner_names_a_running_scan_and_its_time():
    p = {"file": "x.csv", "finished": False, "last_finish_age_s": 20, "integration_s": 30,
         "facts": {"tag": "night-test", "started_local": "2026-01-01T10:48:00", "receiver": "radio-a"}}
    b = L.running_banner(p, [], NOW)
    assert b["kind"] == "scan"
    assert "A scan is running: night-test, 1 h 12 min" in b["text"] and "radio-a" in b["text"]


def test_banner_is_quiet_when_nothing_runs():
    assert L.running_banner({"file": "x.csv", "finished": True}, [], NOW) is None
    assert L.running_banner({}, [], NOW) is None
    # a killed run that never wrote an end stops counting by the clock
    assert L.running_banner({"file": "x.csv", "finished": False, "last_finish_age_s": 900, "integration_s": 30},
                            [], NOW) is None


def test_banner_names_recorders_and_never_calls_a_silent_one_running():
    views = [{"view": "running", "receiver": "radio-b", "mode": "fixed", "job": {"target_mhz": 1000.5}},
             {"view": "silent", "receiver": "radio-a", "silent_s": 40}]
    b = L.running_banner({}, views, NOW)
    assert "A recorder is running: radio-b (fixed) on 1000.5000 MHz" in b["text"]
    assert "radio-a has stopped reporting (silent 40 s)" in b["text"]
    assert "radio-a (" not in b["text"].split("stopped")[0]
    assert L.running_banner({}, [{"view": "silent", "receiver": "radio-a", "silent_s": 400}], NOW)["kind"] == "silent"


# ------------------------------------------------------------------ C2: checks by level
def test_checks_have_levels_problems_first():
    rig = straight(part("filter", "Notch"), part("sdr", "Radio", rx="radio-a"))          # no source reaches the radio
    rig["nodes"].append({"id": "n9", "part": part("lna", "Amp"), "x": 0, "y": 300})        # alone and unpowered
    got = R.checks_by_level(rig)
    levels = [c["level"] for c in got]
    assert levels == sorted(levels, key=R.CHECK_LEVELS.index)
    assert got[0]["level"] == "problem" and "No antenna" in got[0]["text"]
    assert {c["level"] for c in got if "no power feed" in c["text"]} == {"warning"}
    assert {c["level"] for c in got if "on its own" in c["text"]} == {"note"}
    counts = R.check_counts(got)
    assert counts["problem"] == 1 and counts["warning"] >= 1 and counts["note"] >= 1


def test_male_to_male_is_a_warning_and_an_unused_output_a_note():
    assert R.check_level({"kind": "wiring", "text": "male to male at X: these won't screw together"}) == "warning"
    assert R.check_level({"kind": "wiring", "text": "Splitter has 1 output with nothing on it"}) == "note"
    assert R.check_level({"kind": "radio", "text": "2 radios in this rig are all x"}) == "problem"


# ------------------------------------------------------------------ C1: the format bar's font
def test_font_size_and_bold_are_drawing_only_and_checked():
    rig = straight(part("antenna", "Dipole"), part("sdr", "Radio", rx="radio-a"))
    before = R.rig_texts("r", rig)
    rig["nodes"][0]["font_size"] = 14
    rig["nodes"][0]["bold"] = True
    assert R.rig_texts("r", rig) == before
    assert R.font_size(rig["nodes"][0]) == 14.0 and R.font_size(rig["nodes"][1]) is None
    lay = {"schema": 1, "rigs": {"r": {**rig, "chains": {}}}}
    assert R.check_layout(lay)["rigs"]["r"]["nodes"][0]["font_size"] == 14
    for bad in (3, 40, "big", True):
        rig["nodes"][0]["font_size"] = bad
        with pytest.raises(ValueError, match="font size"):
            R.check_layout(lay)
    rig["nodes"][0]["font_size"] = 12
    rig["nodes"][0]["bold"] = "yes"
    with pytest.raises(ValueError, match="bold"):
        R.check_layout(lay)


def test_font_size_is_kept_in_my_parts():
    node = {"id": "n1", "part": part("lna", "Amp"), "x": 0, "y": 0, "font_size": 11, "bold": True}
    e = R.lib_entry(node, [])
    assert e["font_size"] == 11 and e["bold"] is True
    R.check_library([e])
    dropped = R.node_from_lib(R.new_rig(), e, 0, 0)
    assert dropped["font_size"] == 11 and dropped["bold"] is True


# ------------------------------------------------------------------ E1: check rig against the last run
def test_compare_texts_marks_wording_only():
    old = 'Dipole > 6" SMA jumper (S-bend) > Radio'
    new = "Dipole > 6 in coax (S-bend) > Radio"
    d = R.compare_texts(old, new)
    changed = [x for x in d if x["kind"] != "same"]
    assert len(changed) == 1 and changed[0]["wording_only"]
    assert R.compare_words(d) == ['Wording only: 6" SMA jumper (S-bend)  →  6 in coax (S-bend)']


def test_compare_texts_reports_real_changes_part_by_part():
    d = R.compare_texts("Dipole > Notch > Amp > Radio", "Dipole > Amp > 10 dB pad > Radio")
    words = R.compare_words(d)
    assert "Removed: Notch" in words
    assert any(w.startswith("Added: 10 dB pad") for w in words)
    assert R.compare_words(R.compare_texts("A > B", "A > B")) == []


def test_split_text_keeps_a_note_with_an_arrow_whole():
    assert R.split_text("A (in > out) > B") == ["A (in > out)", "B"]


def test_check_against_runs_per_radio():
    rig = straight(part("antenna", "Dipole"), part("lna", "Amp"), part("sdr", "Radio A", rx="radio-a"),
                   cables={1: [R.sync_cable({**R.new_cable("coax"), "length": "6 in"})]})
    now = R.rig_texts("r", rig)["r"]
    runs = {"radio-a": {"run": "survey_x", "tag": "test", "started": "2026-01-01T00:00",
                        "chain": now.replace("6 in coax", '6" SMA jumper')}}
    got = R.check_against_runs("r", rig, runs)
    assert len(got) == 1 and got[0]["rx"] == "radio-a" and got[0]["wording_only"] and not got[0]["same"]
    runs["radio-a"]["chain"] = now
    assert R.check_against_runs("r", rig, runs)[0]["same"]
    assert R.check_against_runs("r", rig, {})[0]["run"] is None


# ------------------------------------------------------------------ E2: compare rig versions
def test_rig_diff_names_added_removed_renamed_and_cables():
    a = straight(part("antenna", "Dipole"), part("filter", "Notch"), part("sdr", "Radio", rx="radio-a"))
    b = json.loads(json.dumps(a))
    b["nodes"][1]["part"]["name"] = "FM notch"
    b["nodes"].append({"id": "n9", "part": part("lna", "Amp"), "x": 0, "y": 300})
    b["wires"][0]["cables"] = [R.sync_cable({**R.new_cable("coax"), "length": "3 ft"})]
    d = R.rig_diff(a, b, name="r")
    assert d["added"] == ["Amp"]
    assert d["renamed"] == ["Notch  →  FM notch"]
    assert d["cables"] and "3 ft coax" in d["cables"][0]
    assert any("Radio" in t for t in d["texts"])
    assert R.rig_diff(a, a, name="r") == {k: [] for k in d}


def test_rig_diff_names_a_part_moved_between_paths():
    a = R.new_rig()
    a["nodes"] = [{"id": "s", "part": part("splitter", "Split", ports=2), "x": 0, "y": 0},
                  {"id": "f", "part": part("filter", "Notch"), "x": 200, "y": 0},
                  {"id": "r1", "part": part("sdr", "A", rx="radio-a"), "x": 400, "y": 0},
                  {"id": "r2", "part": part("sdr", "B", rx="radio-b"), "x": 400, "y": 200}]
    a["wires"] = [{"from": "s", "port": 0, "to": "f"}, {"from": "f", "port": 0, "to": "r1"},
                  {"from": "s", "port": 1, "to": "r2"}]
    b = json.loads(json.dumps(a))
    b["wires"] = [{"from": "s", "port": 0, "to": "r1"}, {"from": "s", "port": 1, "to": "f"},
                  {"from": "f", "port": 0, "to": "r2"}]
    moved = R.rig_diff(a, b, name="r")["moved"]
    assert any(m.startswith("Notch: on the path to A, now to B") for m in moved)


# ------------------------------------------------------------------ E3: Find
def test_find_boxes_by_name_kind_and_receiver():
    rig = straight(part("antenna", "Dipole"), part("lna", "Amp", "at the window"), part("sdr", "Radio", rx="radio-a"))
    assert R.find_boxes(rig, "dip") == ["n1"]
    assert R.find_boxes(rig, "WINDOW") == ["n2"]
    assert R.find_boxes(rig, "radio-a") == ["n3"]
    assert R.find_boxes(rig, "") == []


# ------------------------------------------------------------------ D: the PC's USB inputs
def test_usb_moved_text():
    a = {"location": "Port_#0003.Hub_#0001", "port": 3, "hub": 1}
    b = {"location": "Port_#0005.Hub_#0001", "port": 5, "hub": 1}
    assert R.usb_moved_text("radio-b", a, b, "since run t") == "radio-b moved from port 3 to port 5 since run t"
    c = {"location": "Port_#0002.Hub_#0004", "port": 2, "hub": 4}
    assert "port 3 (hub 1) to port 2 (hub 4)" in R.usb_moved_text("x", a, c, "since then")
    assert R.usb_moved_text("x", a, dict(a), "since") is None
    assert R.usb_moved_text("x", None, a, "since") is None


def test_upstream_radio_walks_back_from_each_pc_input():
    rig = R.new_rig()
    rig["nodes"] = [{"id": "a", "part": part("sdr", "A", rx="radio-a"), "x": 0, "y": 0},
                    {"id": "u", "part": part("usb", "USB"), "x": 200, "y": 0},
                    {"id": "b", "part": part("sdr", "B", rx="radio-b"), "x": 0, "y": 200},
                    {"id": "pc", "part": part("pc", "PC"), "x": 400, "y": 0}]
    rig["wires"] = [{"from": "a", "port": 0, "to": "u"}, {"from": "u", "port": 0, "to": "pc"},
                    {"from": "b", "port": 0, "to": "pc", "in": 1}]
    assert R.upstream_radio(rig, "pc", 0)["id"] == "a"
    assert R.upstream_radio(rig, "pc", 1)["id"] == "b"
    assert R.upstream_radio(rig, "pc", 2) is None


def test_usb_location_words():
    assert live.usb_location_words("Port_#0004.Hub_#0003") == {"port": 4, "hub": 3}
    assert live.usb_location_words("somewhere") == {}


# ------------------------------------------------------------------ the service's read-only routes
@pytest.fixture
def station(tmp_path, monkeypatch):
    root = tmp_path / "st"
    root.mkdir()
    shutil.copy(DATA / "builder_station.json", root / "station.json")
    (root / "surveys").mkdir()
    monkeypatch.setattr(live.host, "running_sdr_programs", lambda: [])
    return Station.load(str(root))


def test_radios_carry_their_usb_place(station, monkeypatch):
    monkeypatch.setattr(live, "usb_radios", lambda: {"00000011": "x", "00000022": "y"})
    monkeypatch.setattr(live, "usb_details", lambda: {"00000011": {"location": "Port_#0004.Hub_#0003", "port": 4,
                                                                   "hub": 3, "speed": "high speed (USB 2.0, 480 Mb/s)"}})
    r = {x["name"]: x for x in live.radios_status(station)["radios"]}
    assert r["radio-a"]["usb"]["port"] == 4 and "high speed" in r["radio-a"]["usb"]["speed"]
    assert r["radio-b"]["usb"] is None


def test_last_runs_takes_each_radios_newest_record(station):
    s = station.surveys
    for stem, rx, start, text in (("old", "radio-a", "2026-01-01T10:00:00", "A > old"),
                                  ("new", "radio-a", "2026-01-02T10:00:00", "A > new"),
                                  ("other", "radio-b", "2026-01-01T09:00:00", "B > x")):
        (s / f"{stem}.json").write_text(json.dumps({"receiver": rx, "started_local": start, "preset": stem,
                                                    "chain": {"chain_order": text},
                                                    "rtl_power": {"device": 1}}), encoding="utf-8")
    (s / "broken.json").write_text("{", encoding="utf-8")
    got = live.radio_last_runs(station)["radios"]
    assert got["radio-a"]["run"] == "new" and got["radio-a"]["chain"] == "A > new" and got["radio-a"]["device"] == 1
    assert got["radio-b"]["chain"] == "B > x"


def test_station_json_copy_never_carries_the_local_file(station):
    root = Path(station.root)
    (root / "station.local.json").write_text(json.dumps({"location": {"lat": 1.5, "lon": 2.5}}), encoding="utf-8")
    st = Station.load(str(root))
    got = live.station_json_copy(st)
    assert got["text"] == (root / "station.json").read_text(encoding="utf-8")
    assert "location" not in json.loads(got["text"]) and "1.5" not in got["text"]
