"""Relay 024: `marsad tuner-sweep` (where a radio's tuner locks), honest labels for what is and is not known about a new
radio, and proof that a new station inherits nothing. Simulated radios (a lock model in place of rtl_sdr), made-up
stations in temporary folders and made-up serials; no radio is opened."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from marsad import cli, config, probe, setup as S, tuner_sweep as TS
from marsad.config import Station


# ---------------------------------------------------------------- what the driver said

@pytest.mark.parametrize("text,state", [
    ("Found 1 device(s):\nSampling at 1024000 S/s.\nTuned to 100000000 Hz.\n", TS.LOCK),
    ("Sampling at 1024000 S/s.\n[R82XX] PLL not locked!\nTuned to 1900000000 Hz.\n", TS.UNLOCK),
    ("Sampling at 1024000 S/s.\n[E4K] PLL not locked for 1150000000 Hz!\nTuned to 1150000000 Hz.\n", TS.UNLOCK),
    ("Sampling at 1024000 S/s.\nWARNING: Failed to set center freq.\n", TS.UNLOCK),
    # the R820T's harmless announcement while the sample rate is set (tuned to 0 Hz first), then a real tune
    ("Enabled direct sampling mode, input 2\nSampling at 1024000 S/s.\nDisabled direct sampling mode\n"
     "Tuned to 100000000 Hz.\n", TS.LOCK),
    # relay 025: never switched back out of direct sampling, so still in it (the real driver always prints the switch
    # back out on a tuner frequency; see tests/test_tuner_sweep_025.py)
    ("Enabled direct sampling mode, input 2\nSampling at 1024000 S/s.\nTuned to 10000000 Hz.\n", TS.DIRECT),
    ("Sampling at 1024000 S/s.\nEnabled direct sampling mode, input 2\nTuned to 10000000 Hz.\n", TS.DIRECT),
    ("No supported devices found.\n", TS.ERROR),
    ("usb_claim_interface error -6\nFailed to open rtlsdr device #0.\n", TS.ERROR),
])
def test_what_the_driver_said_is_read_from_its_own_words(text, state):
    assert TS.classify(text) == state


# ---------------------------------------------------------------- simulated tuners

def r820t_model(hz):
    f = hz / 1e6
    if f < 24.0:
        return TS.DIRECT                                    # the Blog driver's automatic HF direct sampling
    return TS.LOCK if f <= 1766.3 else TS.UNLOCK


def e4000_model(hz):
    """The E4000 that relay 021's stand-in for `rtl_test -t` describes: 52 to 2186 MHz, gap 1094 to 1239."""
    f = hz / 1e6
    return TS.LOCK if (52.0 <= f <= 2186.0 and not 1094.0 < f < 1239.0) else TS.UNLOCK


def test_an_r820t_sweep_finds_its_edges_and_no_gap():
    res = TS.sweep(r820t_model, 12, 2208, 20, 0.5)
    assert abs(res["low"] - 24.0) <= 0.5 and abs(res["high"] - 1766.3) <= 0.5 and res["gaps"] == []
    assert res["direct_below"] == res["low"] and res["low_kind"] == "direct"   # relay 025: the low edge is the switch
    lines = TS.report_lines(res, 20, 0.5)
    assert any("no gaps" in x for x in lines) and any("direct sampling" in x for x in lines)
    assert res["tunes"] < 140                               # a coarse pass and two narrowed edges, not a fine scan


def test_an_e4000_sweep_agrees_with_the_existing_e4000_gap_test(tmp_path):
    res = TS.sweep(e4000_model, 26, 2750, 20, 0.5)
    assert abs(res["low"] - 52) <= 0.5 and abs(res["high"] - 2186) <= 0.5
    assert len(res["gaps"]) == 1
    (g0, g1), = res["gaps"]
    assert abs(g0 - 1094) <= 0.5 and abs(g1 - 1239) <= 0.5
    # the same unit through the old path: rtl_test -t's own words, read by the setup's existing parser
    st = new_station(tmp_path)
    S.add_radio(st, "90000001", "e", "e4000")
    st = Station.load(st.root)
    log = tmp_path / "rtl_test.log"
    log.write_text("E4K range: 52 to 2186 MHz\nE4K L-band gap: 1094 to 1239 MHz\n")
    old = S.result_of(st, {"step": "range", "log": str(log)}, {"exit_code": 0})
    assert abs(old["range_mhz"][0] - res["low"]) <= 0.5 and abs(old["range_mhz"][1] - res["high"]) <= 0.5
    assert all(abs(a - b) <= 0.5 for a, b in zip(old["gaps_mhz"][0], res["gaps"][0]))


def test_the_short_test_is_a_coarse_pass_only():
    res = TS.sweep(r820t_model, 12, 2208, 100, None)
    assert res["tunes"] <= 24 and abs(res["high"] - 1766.3) <= 100     # relay 025: an edge's "not locked" asked twice
    assert any("coarse pass only" in x for x in TS.report_lines(res, 100, None))


def test_a_gap_narrower_than_the_coarse_step_can_be_missed_and_the_output_says_so():
    model = lambda hz: TS.UNLOCK if 1000 < hz / 1e6 < 1005 else TS.LOCK
    res = TS.sweep(model, 100, 1500, 20, 0.5)
    assert res["gaps"] == [] and any("narrower than that can be missed" in x for x in TS.report_lines(res, 20, 0.5))


def test_an_edge_outside_the_sweep_is_flagged_and_does_not_complete_the_step(tmp_path):
    res = TS.sweep(lambda hz: TS.LOCK, 100, 500, 20, 0.5)
    lines = TS.report_lines(res, 20, 0.5)
    assert any("true low edge is lower" in x for x in lines) and any("true high edge is higher" in x for x in lines)
    st = new_station(tmp_path)
    S.add_radio(st, "90000002", "r", "r820t")
    log = tmp_path / "sweep.log"
    log.write_text("\n".join(lines) + "\n")
    out = S.result_of(Station.load(st.root), {"step": "range", "log": str(log)}, {"exit_code": 0})
    assert out["ok"] is False and "outside the sweep" in out["note"]


def test_a_radio_that_stops_answering_ends_the_sweep_in_words():
    res = TS.sweep(lambda hz: TS.ERROR if hz > 500e6 else TS.LOCK, 100, 1000, 100, 0.5)
    assert "did not answer" in res["error"]
    assert TS.report_lines(res, 100, 0.5)[0].startswith("tuner-sweep: FAILED")


# ---------------------------------------------------------------- the command: rules 7 and 8, writes nothing

def new_station(tmp_path) -> Station:
    folder = tmp_path / "Stations" / "Bench"
    S.create_station(folder, "Bench", "a made-up test station")
    return Station.load(folder)


def tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        h.update(str(p.relative_to(root)).encode())
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()


@pytest.fixture
def bench(tmp_path, monkeypatch):
    st = new_station(tmp_path)
    S.add_radio(st, "90000002", "r", "r820t")
    st = Station.load(st.root)
    from marsad import radio_use, watch
    seen = {}
    monkeypatch.setattr(radio_use, "check", lambda station, rx, *a, **k: {"ok": True, "text": "free", "mine": [],
                                                                           "others": [], "unknown": []})
    monkeypatch.setattr(watch, "resolve_receiver", lambda station, rx, cfg, detect=None:
                        seen.setdefault("resolved", {"role": rx, "serial": "90000002", "device": 1, "tuner": "r820t"}))
    import marsad.rtlpower as RP
    monkeypatch.setattr(RP, "find_tool", lambda tool, dirs: Path("rtl_sdr.exe"))

    def fake_probe(exe, device, timeout=10.0):
        seen["device"] = device
        return r820t_model
    monkeypatch.setattr(TS, "rtl_sdr_probe", fake_probe)
    return st, seen


def test_the_command_finds_the_radio_by_serial_and_prints_its_result(bench, capsys):
    st, seen = bench
    assert TS.main(["--receiver", "r"], st) == 0
    out = capsys.readouterr().out
    assert seen["device"] == "90000002"                     # rtl_sdr is given the serial, never a guessed index
    assert "tuner-sweep: range 24" in out and "tuner-sweep: no gaps" in out


def test_the_command_writes_nothing_and_changes_no_result(bench, tmp_path):
    st, _ = bench
    before = tree_hash(st.root)
    assert TS.main(["--receiver", "r", "--short"], st) == 0
    assert tree_hash(st.root) == before                     # station.json, results, surveys: untouched
    j = tmp_path / "out.json"
    TS.main(["--receiver", "r", "--json", str(j)], st)
    assert json.loads(j.read_text())["serial"] == "90000002" and tree_hash(st.root) == before


def test_the_command_refuses_a_radio_another_program_holds(bench, monkeypatch, capsys):
    st, seen = bench
    from marsad import radio_use
    monkeypatch.setattr(radio_use, "check", lambda station, rx, *a, **k: {"ok": False, "text": "SDR++ is running."})
    assert TS.main(["--receiver", "r"], st) == 3
    assert "SDR++ is running" in capsys.readouterr().err and "device" not in seen


def test_existing_commands_are_all_still_there():
    for c in ("run", "analyze", "compare", "compare-runs", "drift", "adsb-check", "archive", "calibrate-retune",
              "watch", "init", "config"):
        assert c in cli.COMMANDS
    assert "tuner-sweep" in cli.COMMANDS


# ---------------------------------------------------------------- the guide's first measurement

def test_the_guide_runs_the_sweep_and_keeps_its_result_with_history(tmp_path):
    st = new_station(tmp_path)
    S.add_radio(st, "90000002", "r", "r820t")
    st = Station.load(st.root)
    log = tmp_path / "sweep.log"
    for hi in ("1766.3", "1768.0"):
        log.write_text(f"tuner-sweep: range 24.0 to {hi} MHz\ntuner-sweep: no gaps\n"
                       "tuner-sweep: at 23.5 MHz and below, the driver switched to HF direct sampling\n")
        res = S.result_of(st, {"step": "range", "log": str(log), "basis": S.basis(st, "r")}, {"exit_code": 0})
        assert res["ok"] and res["range_mhz"] == [24.0, float(hi)] and res["gaps_mhz"] == []
        S.record(st, "r", res)
        st = Station.load(st.root)
    assert st.receivers["r"]["range_mhz"] == [24.0, 1768.0]
    doc = S.load(st)
    assert len(doc["radios"]["r"]["results"]["range"]) == 2                     # history kept (rule 4)
    f = doc["radios"]["r"]["facts"]["range_mhz"]
    assert f["kind"] == "measured" and "by tuner sweep" in f["source"] and f["source"].startswith("measured on")
    from marsad import live
    assert live.range_kind(st, "r") == {"kind": "measured", "word": "measured"}


def test_an_estimate_never_unlocks_scanning(tmp_path):
    st = new_station(tmp_path)
    S.add_radio(st, "90000002", "r", "r820t")
    st = Station.load(st.root)
    S.accept_typical(st, "r")
    st = Station.load(st.root)
    c = S.checklist(st, "r", "", set())
    rng = next(x for s in c["steps"] for x in s["sub"] if x["step"] == "range")
    assert rng["state"] == "estimate kept" and "locked" in rng["detail"]
    assert "step 2" in S.lock_reason(st, "r", "")


@pytest.mark.parametrize("tuner,words", [("r820t", "24 to 1766 MHz, normally with no blind spots"),
                                         ("e4000", "52 to 2200 MHz")])
def test_each_steps_words_are_for_the_radios_own_tuner(tuner, words):
    assert words in S.step_text("range", tuner)["expect"]
    assert "Nothing needs to be connected: lock is internal" in S.step_text("range", tuner)["connect"]
    assert "E4000" not in S.step_text("range", "r820t")["expect"]


# ---------------------------------------------------------------- what Add Radio knows

def test_add_radio_keeps_only_what_is_known(tmp_path):
    st = new_station(tmp_path)
    S.add_radio(st, "90000002", "r", "r820t", product="Made-up Dongle")
    st = Station.load(st.root)
    assert st.receivers["r"]["gaps_mhz"] == []              # blind spots: not measured yet, so none stored
    facts = S.load(st)["radios"]["r"]["facts"]
    assert facts["range_mhz"]["kind"] == "estimate" and "not measured on this unit" in facts["range_mhz"]["source"]
    assert facts["gaps_mhz"]["source"] == "not measured yet"
    assert S.load(st)["radios"]["r"]["read"] == {"maker": "", "product": "Made-up Dongle"}
    from marsad import live
    assert live.range_kind(st, "r") == {"kind": "estimate", "word": "estimate"}


def test_the_maker_string_is_read_from_the_radio():
    text = ("Found 1 device(s):\n  0:  Generic RTL2832U OEM\n\nUsing device 0: Generic RTL2832U OEM\n"
            "Found Rafael Micro R820T tuner\n\nCurrent configuration:\n__________________________________________\n"
            "Vendor ID:\t\t0x0bda\nProduct ID:\t\t0x2838\nManufacturer:\t\tMadeUp\nProduct:\t\tRTL2838UHIDIR\n"
            "Serial number:\t\t90000002\n")
    info = probe.parse_eeprom(text)
    assert info["manufacturer"] == "MadeUp" and info["product"] == "RTL2838UHIDIR" and info["serial"] == "90000002"
    assert probe.parse_eeprom(text.replace("Manufacturer:\t\tMadeUp\n", ""))["manufacturer"] == ""


def test_typical_figures_come_from_the_engines_generic_template():
    assert config.DEFAULT_FILE.parent == Path(config.__file__).parent          # inside the marsad package
    t = S.typical("r820t")
    tpl = json.loads(config.DEFAULT_FILE.read_text(encoding="utf-8"))["receivers"]["r820t"]
    assert t["range_mhz"] == tpl["range_mhz"]


# ---------------------------------------------------------------- a new station inherits nothing

def test_a_new_station_holds_only_what_init_and_the_form_make(tmp_path):
    st = new_station(tmp_path)
    files = sorted(str(p.relative_to(st.root)).replace("\\", "/") for p in st.root.rglob("*") if p.is_file())
    assert files == ["chain.json", "station.json", "station_setup.json"]
    assert sorted(p.name for p in st.root.iterdir() if p.is_dir()) == ["logs", "recordings", "reports", "surveys"]
    raw = json.loads(st.file.read_text(encoding="utf-8"))
    tpl = json.loads(config.DEFAULT_FILE.read_text(encoding="utf-8"))
    form = {"station_name", "description", "receivers", "default_receiver", "recorder", "defaults"}
    for k, v in raw.items():
        if k not in form:
            assert v == tpl.get(k), k                       # every other value is the engine's generic one
    assert raw["receivers"] == {} and raw["default_receiver"] in (None, "")
    mine = ("serials", "ppm", "roles")                     # this station's radios: all empty until Add Radio
    assert all(raw["recorder"].get(k) == {} for k in mine)
    assert {k: v for k, v in raw["recorder"].items() if k not in mine} == \
        {k: v for k, v in tpl["recorder"].items() if k not in mine}
    assert raw["defaults"]["chain_order"] == ""
    assert json.loads((st.root / "chain.json").read_text())["chains"] == {}
    assert json.loads((st.root / "station_setup.json").read_text())["radios"] == {}
