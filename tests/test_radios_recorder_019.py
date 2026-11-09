"""Relay 019: the recorder list's session events (concise, one session, private events a fingerprint only), the
setups for the two radios and what is possible with the radios enabled now, and each setup's What if. Made-up
stations; no radio, no Qt."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from marsad import capture as cap
from marsad import live
from marsad.config import Station
from marsad_desktop import live_data as L
from marsad_desktop import setups as SU

from conftest import copy_station

DATA = Path(__file__).parent / "data"


# ------------------------------------------------------------------ the recorder list: which session, its line
def view(serial, state, started, **kw):
    return {"serial": serial, "receiver": f"radio-{serial[-1]}", "view": state, "started": started, "mode": "fixed",
            "events": kw.pop("events", 3), "captures": kw.pop("captures", 1), **kw}


def test_the_running_session_is_shown_else_only_the_latest():
    a, b = view("00000011", "stopped", 100.0, stopped=200.0), view("00000022", "stopped", 300.0, stopped=400.0)
    sel = L.pick_session([a, b])
    assert sel["current"] == [] and sel["last"] is b                    # older sessions belong in Captures
    run = view("00000011", "running", 500.0)
    sel = L.pick_session([a, b, run])
    assert sel["current"] == [run] and sel["last"] is None
    silent = view("00000022", "silent", 600.0, silent_s=180)
    assert L.pick_session([a, silent])["current"] == [silent]           # still this session, said to be silent
    assert L.pick_session([]) == {"current": [], "last": None}


def test_session_lines_say_the_state():
    assert L.session_summary(None) == "No recorder session yet"
    assert "running since" in L.session_summary(view("1", "running", 1e9))
    assert "silent 3 min" in L.session_summary(view("1", "silent", 1e9, silent_s=180))
    s = L.session_summary(view("1", "stopped", 1e9, stopped=1e9 + 3600, events=1, captures=0))
    assert s.startswith("Last session:") and "1 event, 0 captures" in s


def test_one_badge_per_row_private_first():
    assert L.badge_of({"private": True, "anomaly": True, "new": True}) == "private"
    assert L.badge_of({"anomaly": True, "new": True}) == "anomaly"
    assert L.badge_of({"new": True}) == "new"
    assert L.badge_of({}) == "open"


EVENTS = [{"id": 1, "at": 10, "mhz": 433.92, "snr_db": 25, "band": None},
          {"id": 2, "at": 11, "mhz": 880.5, "snr_db": 14, "private": True, "band": "cellular 850"},
          {"id": 3, "at": 12, "mhz": 881.0, "snr_db": 30, "private": True, "band": "cellular 850"},
          {"id": 4, "at": 13, "mhz": 1265.4, "snr_db": 8, "new": True, "band": None},
          {"id": 5, "at": 14, "mhz": 1900.0, "snr_db": 9, "private": True, "band": "pcs"}]


def test_private_events_fold_into_one_count_per_band_by_default():
    rows, hidden = L.filter_events(EVENTS)
    groups = [r for r in rows if r["kind"] == "private_group"]
    assert {(g["band"], g["n"]) for g in groups} == {("cellular 850", 2), ("pcs", 1)} and hidden == 0
    assert all(r["kind"] != "event" or not r.get("private") for r in rows)
    rows, _ = L.filter_events(EVENTS, collapse_private=False)
    assert [r["id"] for r in rows if r["kind"] == "event"] == [1, 2, 3, 4, 5]


def test_filters_hide_and_count():
    rows, hidden = L.filter_events(EVENTS, badges=("open", "new"))
    assert [r["id"] for r in rows] == [1, 4] and hidden == 3
    rows, hidden = L.filter_events(EVENTS, min_db=20)
    assert hidden == 3 and {r.get("id") or r["band"] for r in rows} == {1, "cellular 850"}
    rows, hidden = L.filter_events(EVENTS, band="open band")
    assert [r["id"] for r in rows] == [1, 4] and hidden == 3
    assert L.bands_of(EVENTS) == ["cellular 850", "open band", "pcs"]


# ------------------------------------------------------------------ the session's events, from the catalogue
pytestmark = pytest.mark.xdist_group("filled_station")


def test_session_events_from_the_catalogue(filled_template, tmp_path):
    st = copy_station(filled_template, tmp_path / "rec")
    con = cap.connect(st)
    rows = [dict(r) for r in con.execute("SELECT * FROM captures ORDER BY started_epoch")]
    con.close()
    serial = rows[0]["serial"]
    first = min(r["started_epoch"] for r in rows)
    got = live.recorder_session(st, serial, first)
    evs = got["events"]
    assert got["total"] == len([r for r in rows if r["serial"] == serial]) and evs
    assert [e["at"] for e in evs] == sorted((e["at"] for e in evs), reverse=True)        # newest first
    priv = [e for e in evs if e["private"]]
    assert priv and all(e["tier"] == "private" for e in priv)
    assert all(e["thumb"] and e["thumb"]["rows"] <= live.THUMB_ROWS and e["thumb"]["cols"] <= live.THUMB_COLS
               for e in evs if e["thumb"])
    assert any(e["thumb"] for e in evs)                     # a fingerprint picture, private ones included
    assert all(e["new"] for e in evs)                       # nothing in the catalogue before this session
    later = live.recorder_session(st, serial, max(r["started_epoch"] for r in rows))
    assert later["events"] and not later["events"][0]["new"]       # heard before, within its width
    assert live.recorder_session(st, serial, 1e12)["events"] == []
    assert live.recorder_session(st, "nobody", 0)["events"] == []


# ------------------------------------------------------------------ setups and what is possible
def radios(*rs, both=None, presence=True):
    return {"radios": [{"name": n, "enabled": e, "present": p, "range_mhz": [24, 1766], "gaps_mhz": []}
                       for n, e, p in rs], "both_cover_mhz": both or [], "roles": {},
            "presence_source": "Windows device list" if presence else None}


def test_two_enabled_radios_allow_every_setup():
    d = radios(("radio-a", True, True), ("radio-b", True, True))
    av = SU.availability(d)
    assert all(av[s]["ok"] and av[s]["supported"] for s in SU.ORDER)
    assert SU.possible_text(d).startswith("Two radios: survey + record")
    assert SU.chosen(d) == "survey_record"


def test_one_radio_disabled_describes_one_radio_use_and_says_why():
    d = radios(("radio-a", True, True), ("radio-b", False, True))
    t = SU.possible_text(d)
    assert t.startswith("One radio (radio-a): survey, or a fixed recording") and "radio-b is disabled" in t
    av = SU.availability(d)
    assert not av["survey_record"]["ok"] and "the radio-b is disabled" in av["survey_record"]["why"]
    assert not av["both_survey"]["ok"] and not av["both_record"]["ok"]
    assert av["one_survey"]["ok"] and av["one_record"]["ok"]


def test_unplugged_and_nothing_usable():
    d = radios(("radio-a", True, False), ("radio-b", True, True))
    assert "not plugged in" in SU.availability(d)["survey_record"]["why"]
    d = radios(("radio-a", False, True), ("radio-b", False, True))
    assert SU.possible_text(d).startswith("No radio can take new work now")
    assert not SU.availability(d)["one_survey"]["ok"]


def test_a_chosen_setup_is_read_from_the_roles():
    d = radios(("radio-a", True, True), ("radio-b", True, True))
    d["roles"] = {"setup": "both_record"}
    assert SU.chosen(d) == "both_record"
    d["roles"] = {"setup": "nonsense"}
    assert SU.chosen(d) == "survey_record"
    assert SU.chosen(radios(("radio-a", True, True))) == "one_survey"


def test_every_setup_has_its_what_if_in_the_explanations():
    t = SU.whatif_texts()
    for s in SU.ORDER:
        assert all(t[s].get(sec) for sec in SU.SECTIONS), s
        assert t[s]["tip"]
    assert "rule 5" in t["both_survey"]["Comparability"]


def test_what_if_takes_station_facts_and_says_not_measured():
    d = radios(("radio-a", True, True), ("radio-b", True, True), both=[[52, 1100], [1250, 1766]])
    rig = {"nodes": [{"id": "s", "part": {"type": "splitter", "name": "Divider"}, "gain_db": -6.2}]}
    w = SU.whatif("both_survey", d, rig)
    facts = " ".join(w["facts"])
    assert "Divider in the rig used for runs: -6.2 dB on each output" in facts
    assert "Both can tune 52–1100, 1250–1766 MHz" in facts and "not measured" in facts
    rig["nodes"][0].pop("gain_db")
    assert "not typed on the part (not measured)" in " ".join(SU.whatif("both_survey", d, rig)["facts"])
    assert "no splitter" in " ".join(SU.splitter_facts({"nodes": []}))
    assert "radio-a tunes 24–1766 MHz" in " ".join(SU.whatif("one_record", d, None)["facts"])


# ------------------------------------------------------------------ the setup is kept with the roles
@pytest.fixture
def station(tmp_path, monkeypatch):
    root = tmp_path / "st"
    root.mkdir()
    shutil.copy(DATA / "builder_station.json", root / "station.json")
    (root / "surveys").mkdir()
    monkeypatch.setattr(live.host, "running_sdr_programs", lambda: [])
    monkeypatch.setattr(live, "usb_radios", lambda: {"00000011": "x", "00000022": "y"})
    return Station.load(str(root))


def test_setup_is_stored_with_the_roles_and_starts_nothing(station):
    d = live.set_radio_roles(station, {"setup": "both_survey"})
    assert d["roles"]["setup"] == "both_survey"
    raw = json.loads((station.root / live.SAVED_SCANS_FILE).read_text())
    assert raw["roles"]["setup"] == "both_survey" and raw["roles"]["surveyor"] == d["roles"]["surveyor"]
    d = live.set_radio_roles(station, {"swap": True})
    assert d["roles"]["setup"] == "both_survey"                 # a swap keeps the setup
    d = live.set_radio_roles(station, {"setup": "one_record", "one": "radio-b"})
    assert d["roles"]["setup"] == "one_record" and d["roles"]["one"] == "radio-b"
    with pytest.raises(ValueError):
        live.set_radio_roles(station, {"setup": "everything"})
    assert not list(station.surveys.glob("*"))                   # nothing was started
