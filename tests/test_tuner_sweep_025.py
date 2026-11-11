"""Relay 025: the tuner sweep's lock reading corrected, the high edge found, and progress, Cancel and a plain end
result for every setup measurement.

The simulated radios here speak the driver's own words (the shapes below are what librtlsdr and rtl_sdr print for an
R820T and an E4000: open, sample rate, the R820T's switch out of direct sampling with its harmless "PLL not locked",
the real tune), and every answer goes through `classify`, as on the radio. Relay 024's tests fed states straight into
the sweep, which is how a wrong reading of the driver's words got past them. Made-up stations and serials; no radio is
opened."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from marsad import live, setup as S, tuner_sweep as TS
from marsad.config import Station

HEAD = "Found 1 device(s):\n  0:  Maker, Product, SN: 90000002\n\nUsing device 0: Generic RTL2832U OEM\n"


def r820t_words(hz, top=1766.3):
    """What rtl_sdr prints for one tune of an R820T on the RTL-SDR Blog driver."""
    f = hz / 1e6
    s = HEAD + "Found Rafael Micro R820T tuner\nEnabled direct sampling mode, input 2\nSampling at 1024000 S/s.\n"
    if f < 24.0:                                  # stays in HF direct sampling: nothing more about the tuner
        return s + f"Tuned to {int(hz)} Hz.\nReading samples in sync mode...\n"
    s += "[R82XX] PLL not locked!\nDisabled direct sampling mode\n"        # harmless, on every tune
    if f > top:
        s += "[R82XX] PLL not locked!\n"                                  # the real answer
    return s + f"Tuned to {int(hz)} Hz.\nReading samples in sync mode...\n"


def e4000_words(hz):
    f = hz / 1e6
    s = HEAD + "Found Elonics E4000 tuner\n[E4K] PLL not locked for 0 Hz!\nSampling at 1024000 S/s.\n"
    if not (52.0 <= f <= 2186.0 and not 1094.0 < f < 1239.0):
        s += f"[E4K] PLL not locked for {int(hz)} Hz!\n"
    return s + f"Tuned to {int(hz)} Hz.\n"


def through_driver(words):
    """A probe as rtl_sdr_probe gives: the state read from the driver's words, and the words."""
    return lambda hz: (TS.classify(words(hz)), words(hz))


# ---------------------------------------------------------------- the lock reading

def test_the_r820ts_harmless_pll_complaint_on_the_way_out_of_direct_sampling_is_not_an_unlock():
    """The recorder's own log of an R820T holds this exact shape on every one of its ~1,600 good tunes."""
    good = ("Found Rafael Micro R820T tuner\nEnabled direct sampling mode, input 2\nSampling at 2400000 S/s.\n"
            "[R82XX] PLL not locked!\nDisabled direct sampling mode\nTuned to 1265440000 Hz.\n"
            "Tuner gain set to 29.70 dB.\nReading samples in async mode...\n")
    assert TS.classify(good) == TS.LOCK
    assert TS.classify(r820t_words(1900e6)) == TS.UNLOCK            # a real complaint after the switch counts
    assert TS.classify(r820t_words(12e6)) == TS.DIRECT              # still in direct sampling: not the tuner
    assert TS.classify(e4000_words(100e6)) == TS.LOCK               # the E4000's set-up complaint is before the tune
    assert TS.classify(e4000_words(1150e6)) == TS.UNLOCK


def test_a_usb_failure_is_no_answer_not_an_unlock():
    """Also from the recorder's log: an I2C write failing mid-tune says nothing about the tuner."""
    bad = ("Found Rafael Micro R820T tuner\nEnabled direct sampling mode, input 2\nSampling at 2400000 S/s.\n"
           "[R82XX] PLL not locked!\nr82xx_write: i2c wr failed=-9 reg=06 len=1\nr82xx_init: failed=-9\n"
           "Disabled direct sampling mode\nWARNING: Failed to set center freq.\n")
    assert TS.classify(bad) == TS.ERROR and "USB" in TS.trouble(bad)


def test_relay_024s_reading_of_these_words_gave_the_owners_12_to_24_mhz():
    """The root cause, pinned: read the old way (everything after "Sampling at", direct sampling only when announced
    after it), the R820T locks only where it is in direct sampling."""
    def old_classify(t):
        t = t.lower()
        tune = t[t.rfind("sampling at"):]
        on, off = tune.rfind("enabled direct sampling"), tune.rfind("disabled direct sampling")
        if on >= 0 and on > off:
            return TS.DIRECT
        return TS.UNLOCK if "pll not locked" in tune else TS.LOCK
    assert old_classify(r820t_words(12e6)) == TS.LOCK and old_classify(r820t_words(100e6)) == TS.UNLOCK
    res = TS.sweep(lambda hz: old_classify(r820t_words(hz)), 12, 2208, 20, 0.5, bottom_limit=12)   # 024 never extended
    assert res["low"] == 12 and abs(res["high"] - 24) <= 0.5        # what the owner saw
    new = TS.sweep(through_driver(r820t_words), 12, 2208, 20, 0.5)
    assert abs(new["low"] - 24) <= 0.5 and abs(new["high"] - 1766.3) <= 0.5


# ---------------------------------------------------------------- the sweep on simulated radios

def test_an_r820t_reads_from_the_direct_sampling_switch_to_its_real_top_with_no_gap():
    res = TS.sweep(through_driver(r820t_words), 12, 2208, 20, 0.5)
    assert abs(res["low"] - 24) <= 0.5 and abs(res["high"] - 1766.3) <= 0.5 and res["gaps"] == []
    assert res["low_kind"] == "direct" and res["high_kind"] == "unlock"
    assert not res["edge_at_start"] and not res["edge_at_end"] and not res["direct_above_range"]
    lines = TS.report_lines(res, 20, 0.5)
    assert any("low edge 24" in x and "switches to HF direct sampling" in x for x in lines)
    assert any("high edge 1766" in x for x in lines) and "tuner-sweep: no gaps" in lines


def test_an_e4000_still_agrees_with_the_rtl_test_gap_test(tmp_path):
    res = TS.sweep(through_driver(e4000_words), 26, 2750, 20, 0.5)
    assert abs(res["low"] - 52) <= 0.5 and abs(res["high"] - 2186) <= 0.5 and res["low_kind"] == "unlock"
    (g0, g1), = res["gaps"]
    st = new_station(tmp_path, "e4000")
    log = tmp_path / "rtl_test.log"
    log.write_text("E4K range: 52 to 2186 MHz\nE4K L-band gap: 1094 to 1239 MHz\n")
    old = S.result_of(st, {"step": "range", "radio": "r", "log": str(log)}, {"exit_code": 0})
    assert abs(old["range_mhz"][0] - res["low"]) <= 0.5 and abs(old["range_mhz"][1] - res["high"]) <= 0.5
    assert abs(old["gaps_mhz"][0][0] - g0) <= 0.5 and abs(old["gaps_mhz"][0][1] - g1) <= 0.5


def test_the_high_edge_is_found_above_the_planned_top_instead_of_reported_missing():
    res = TS.sweep(through_driver(lambda hz: r820t_words(hz, top=2350.0)), 12, 2208, 20, 0.5)
    assert abs(res["high"] - 2350) <= 0.5 and not res["edge_at_end"] and res["extended"] == ["up"]
    assert any("carried on above the planned top" in x for x in TS.report_lines(res, 20, 0.5))


def test_one_bad_tune_beside_an_edge_does_not_move_it():
    flaky = {"n": 0}

    def words(hz):
        if abs(hz / 1e6 - 1600) < 1e-6 and flaky["n"] == 0:        # the first tune at 1600 MHz says not locked
            flaky["n"] += 1
            return r820t_words(1900e6).replace("1900000000", str(int(hz)))
        return r820t_words(hz)
    res = TS.sweep(through_driver(words), 12, 2208, 20, 0.5)
    assert abs(res["high"] - 1766.3) <= 0.5 and res["gaps"] == []


def test_direct_sampling_inside_the_range_is_flagged_not_counted_as_range():
    def words(hz):
        if 900 <= hz / 1e6 <= 1000:
            return r820t_words(12e6)
        return r820t_words(hz)
    res = TS.sweep(through_driver(words), 12, 2208, 20, 0.5)
    assert res["direct_above_range"] and any("UNEXPECTED" in x for x in TS.report_lines(res, 20, 0.5))


# ---------------------------------------------------------------- the command: progress, Cancel

def new_station(tmp_path, tuner="r820t") -> Station:
    folder = tmp_path / "Stations" / "Bench"
    S.create_station(folder, "Bench", "a made-up test station")
    st = Station.load(folder)
    S.add_radio(st, "90000002", "r", tuner)
    return Station.load(folder)


@pytest.fixture
def bench(tmp_path, monkeypatch):
    st = new_station(tmp_path)
    from marsad import radio_use, watch
    monkeypatch.setattr(radio_use, "check", lambda station, rx, *a, **k: {"ok": True, "text": "free"})
    monkeypatch.setattr(watch, "resolve_receiver", lambda station, rx, cfg, detect=None:
                        {"role": rx, "serial": "90000002", "device": 1, "tuner": "r820t"})
    import marsad.rtlpower as RP
    monkeypatch.setattr(RP, "find_tool", lambda tool, dirs: Path("rtl_sdr.exe"))
    monkeypatch.setattr(TS, "rtl_sdr_probe", lambda exe, device, timeout=10.0: through_driver(r820t_words))
    return st


def test_the_command_prints_progress_the_guide_can_read(bench, capsys):
    assert TS.main(["--receiver", "r", "--short"], bench) == 0
    out = capsys.readouterr().out.splitlines()
    prog = [x for x in out if x.startswith("tuner-sweep: progress")]
    assert prog and all(" of " in x and "MHz" in x for x in prog)
    assert any("MHz locked" in x and "[driver:" in x for x in out)         # the driver's own words, per point
    job = {"step": "range", "started": S.now()}
    p = S.progress_of(job, {"lines": out[:len(out) // 2]})
    assert 0 < p["pct"] < 100 and p["activity"].startswith("tuning") and p["basis"] == "the pace so far"


def test_a_stop_file_cancels_cleanly_between_tunings(bench, tmp_path, capsys):
    stop = tmp_path / "x.stop"
    stop.write_text("cancel")
    assert TS.main(["--receiver", "r", "--stop-file", str(stop)], bench) == TS.EXIT_CANCELLED
    assert "CANCELLED" in capsys.readouterr().out and not stop.exists()


# ---------------------------------------------------------------- plain end results

def _res(st, text, code=0, short=False, radio="r"):
    log = st.root / "logs" / f"t{time.time_ns()}.log"
    log.parent.mkdir(exist_ok=True)
    log.write_text(text)
    return S.result_of(st, {"step": "range", "radio": radio, "log": str(log), "short": short, "id": "1"},
                       {"exit_code": code, "lines": text.splitlines()})


def test_a_good_sweep_ends_done_with_its_figures(tmp_path):
    st = new_station(tmp_path)
    res = _res(st, "tuner-sweep: range 24.0 to 1766.3 MHz\ntuner-sweep: low edge 24.0 MHz: where the driver switches "
                   "to HF direct sampling (below it ...), not a lock limit\ntuner-sweep: no gaps\n")
    assert res["ok"] and res["outcome"] == "done" and "24 to 1766.3" in res["what"]
    assert res["low_edge_kind"] == "direct sampling switch" and res["job"] == "1"


def test_the_owners_12_to_24_answer_now_needs_attention_with_why_and_what_to_do(tmp_path):
    st = new_station(tmp_path)
    res = _res(st, "tuner-sweep: range 12.0 to 24.0 MHz\ntuner-sweep: no gaps\n")
    assert not res["ok"] and res["outcome"] == "attention"
    assert "12 to 24" in res["what"] and "about 24 to 1766" in res["what"]
    assert res["why"] and res["advice"] and "run it again" not in res["note"].lower()


@pytest.mark.parametrize("text,code,want,word", [
    ("tuner-sweep: REFUSED: SDR++ is running. Nothing was tuned.\n", 3, "failed", "Close other SDR programs"),
    ("tuner-sweep: FAILED: the radio did not answer at 100 MHz: the radio stopped answering over USB (the lead)\n", 1,
     "failed", "USB lead"),
    ("tuner-sweep: FAILED: the radio did not answer at 12 MHz (unplugged, or another program has it)\n", 1, "failed",
     "plugged in"),
    ("Traceback (most recent call last):\n  File x\nKeyError: 'a'\n", 1, "failed", "Claude Code"),
])
def test_failures_say_what_why_and_what_to_do(tmp_path, text, code, want, word):
    res = _res(new_station(tmp_path), text, code)
    assert res["outcome"] == want and res["what"] and res["why"] and any(word in a for a in res["advice"])
    assert res["error_lines"]


def test_progress_from_a_runs_own_clock_and_an_estimate_from_the_time():
    job = {"step": "warmup", "started": S.now(), "argv": ["run", "custom", "60m"]}
    p = S.progress_of(job, {"lines": ["    5:00 elapsed, 55:00 left (  8%), 1.0 MB, done ~10:00"]})
    assert p["pct"] == 8 and p["remaining_s"] == 3300 and p["basis"] == "the run's own clock"
    started = (__import__("datetime").datetime.now() - __import__("datetime").timedelta(seconds=30)).isoformat()
    p = S.progress_of({"step": "adsb", "started": started, "argv": ["adsb-check", "--seconds", "60"]}, {"lines": []})
    assert p["basis"] == "estimated from the time" and 0 < p["pct"] < 95 and p["activity"].startswith("listening")


# ---------------------------------------------------------------- the service: run, Cancel, the step's run shown

@pytest.fixture
def svc(tmp_path, monkeypatch):
    st = new_station(tmp_path)
    monkeypatch.setattr(live, "find_radio", lambda st_, rx: {"found": True, "device": 0, "rx": rx, "text": "found"})
    monkeypatch.setattr(live, "radio_in_use", lambda st_, rx: {"blocked": False, "text": ""})
    monkeypatch.setattr(live, "usb_radios", lambda: None)
    killed = []
    monkeypatch.setattr(live, "_end_setup_job", lambda st_, job: killed.append(job["id"]) or Path(
        job["log"] + ".exit").write_text(json.dumps({"exit_code": -2, "ended": time.time()})))

    def fake_launch(station, argv, label):
        log = station.root / "logs" / f"{label}.log"
        log.parent.mkdir(exist_ok=True)
        log.write_text("tuner-sweep: progress 3 of 30: tuning 52.0 MHz, coarse pass\n")
        return {"pid": 4242, "log": str(log)}
    monkeypatch.setattr(live, "launch_outside", fake_launch)
    return st, killed


def test_a_running_step_shows_progress_and_cancel_keeps_no_result(svc):
    st, killed = svc
    out = live.setup_post(st, "/setup/run", {"radio": "r", "step": "range", "short": False})
    job = out["job"]
    assert job["pid"] == 4242 and job["stop_file"] and "--stop-file" in job["argv"]
    sub = next(x for s in out["status"]["radios"][0]["steps"] for x in s["sub"] if x["step"] == "range")
    assert sub["run"]["state"] == "running" and sub["run"]["progress"]["pct"] == 10
    assert "tuning 52.0 MHz" in sub["run"]["progress"]["activity"] and sub["run"]["lines"]
    live.setup_post(st, "/setup/cancel", {"radio": "r", "step": "range"})
    assert Path(job["stop_file"]).exists() and not killed          # a clean stop first, no kill
    Path(job["log"] + ".exit").write_text(json.dumps({"exit_code": TS.EXIT_CANCELLED, "ended": time.time()}))
    status = live.setup_status(st)
    sub = next(x for s in status["radios"][0]["steps"] for x in s["sub"] if x["step"] == "range")
    assert sub["run"]["outcome"] == "cancelled" and sub["state"] == "not started"
    assert not (S.load(st)["radios"]["r"].get("results") or {}).get("range")       # nothing kept


def test_a_step_with_no_stop_file_is_ended_by_its_own_process_tree(svc):
    st, killed = svc
    job = live.setup_post(st, "/setup/run", {"radio": "r", "step": "adsb", "short": True})
    assert "--stop-file" not in job["job"]["argv"] and job["job"]["stop_file"] is None
    live.setup_post(st, "/setup/cancel", {"radio": "r", "step": "adsb"})
    assert killed == [job["job"]["id"]]


def test_an_ended_step_shows_its_result_in_plain_words(svc):
    st, _ = svc
    job = live.setup_post(st, "/setup/run", {"radio": "r", "step": "range", "short": False})["job"]
    Path(job["log"]).write_text("tuner-sweep: range 12.0 to 24.0 MHz\ntuner-sweep: no gaps\n")
    Path(job["log"] + ".exit").write_text(json.dumps({"exit_code": 0, "ended": time.time()}))
    status = live.setup_status(st)
    sub = next(x for s in status["radios"][0]["steps"] for x in s["sub"] if x["step"] == "range")
    assert sub["state"] == "needs attention" and sub["run"]["outcome"] == "attention"
    assert sub["run"]["why"] and sub["run"]["advice"]


def test_cancel_with_nothing_running_is_refused_in_words(svc):
    st, _ = svc
    with pytest.raises(ValueError, match="Nothing is running"):
        live.setup_post(st, "/setup/cancel", {"radio": "r", "step": "range"})
