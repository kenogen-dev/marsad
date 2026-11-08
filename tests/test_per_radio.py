"""Relay 012: one program per radio, not per station. A start checks only the radio it will use;
when the other radio is busy the run goes ahead and records what that radio was doing, outside
the fingerprint. A program no record places on a radio still refuses every start. Made-up
station, stand-in processes and a stand-in rtl_power; no radio is involved."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from marsad import capture as cap, live, radio_use, record, rtlpower
from marsad.config import Station

DATA = Path(__file__).parent / "data"
CHAIN = "made-up antenna > Coax > radio-b > USB > PC"


@pytest.fixture
def station(tmp_path):
    root = tmp_path / "st"
    root.mkdir()
    shutil.copy(DATA / "builder_station.json", root / "station.json")
    (root / "surveys").mkdir()
    return Station.load(str(root))


def busy_survey(station, rx="radio-a", engine=None, name="survey_band1_1h_busy_20010101-0000"):
    """A recording being written right now on `rx`: its sidecar has no finished_local, the file is fresh."""
    ext = ".dat" if engine == "rtl_power_fftw" else ".csv"
    f = station.surveys / (name + ext)
    side = {"receiver": rx, "rtl_power": {"integration_s": 30}}
    if engine:
        side["engine"] = engine
    f.with_suffix(".json").write_text(json.dumps(side))
    f.write_text("x\n")
    return f


def busy_recorder(station, rx="radio-b", serial="00000022"):
    pool = cap.pool_dir(station, cap.settings(station))
    pool.mkdir(parents=True, exist_ok=True)
    (pool / f"recorder_status_{serial}.json").write_text(json.dumps(
        {"state": "running", "updated": time.time(), "receiver": rx, "serial": serial, "mode": "fixed",
         "job": {"target_mhz": 300.5}}))


# ---- the rule itself

def test_other_radio_busy_allows_the_free_one_and_refuses_the_busy_one(station):
    busy_survey(station)
    progs = {"rtl_power_gainfix.exe": 1}
    b = radio_use.check(station, "radio-b", programs=progs)
    assert b["ok"] and [e["radio"] for e in b["others"]] == ["radio-a"]
    assert "radio-b is free" in b["text"] and "radio-a surveying" in b["text"]
    a = radio_use.check(station, "radio-a", programs=progs)
    assert not a["ok"] and "radio-a is in use" in a["reason"] and "rtl_power" in a["reason"]


def test_a_running_recorder_holds_its_radio_by_serial(station):
    busy_recorder(station)
    progs = {"rtl_sdr.exe": 1}
    assert radio_use.check(station, "radio-a", programs=progs)["ok"]
    b = radio_use.check(station, "radio-b", programs=progs)
    assert not b["ok"] and "marsad watch fixed on 300.5000 MHz" in b["reason"]


def test_a_recorder_between_captures_still_holds_its_radio(station):
    """A survey watch's recorder runs rtl_sdr only during a capture; it still owns the radio."""
    busy_recorder(station)
    assert not radio_use.check(station, "radio-b", programs={})["ok"]


def test_a_program_no_record_explains_refuses_every_start(station):
    for progs in ({"sdrpp.exe": 1}, {"rtl_tcp.exe": 1}):
        for rx in ("radio-a", "radio-b"):
            r = radio_use.check(station, rx, programs=progs)
            assert not r["ok"] and "cannot tell which radio" in r["reason"], (progs, rx)


def test_more_processes_than_records_is_an_unknown_program(station):
    busy_survey(station)
    r = radio_use.check(station, "radio-b", programs={"rtl_power_gainfix.exe": 1, "rtl_power.exe": 1})
    assert not r["ok"] and len(r["unknown"]) == 1                  # one rtl_power no record explains


def test_an_fftw_run_is_placed_too(station):
    busy_survey(station, engine="rtl_power_fftw")
    assert radio_use.check(station, "radio-b", programs={"rtl_power_fftw.exe": 1})["ok"]
    assert not radio_use.check(station, "radio-a", programs={"rtl_power_fftw.exe": 1})["ok"]


def test_a_finished_or_stale_recording_holds_nothing(station):
    f = busy_survey(station)
    side = json.loads(f.with_suffix(".json").read_text())
    f.with_suffix(".json").write_text(json.dumps({**side, "finished_local": "2001-01-01T01:00:00"}))
    assert radio_use.check(station, "radio-a", programs={})["ok"]


def test_nothing_running_records_nothing(station):
    r = radio_use.check(station, "radio-a", programs={})
    assert r["ok"] and r["others"] == [] and radio_use.busy_record(r) == []


def test_busy_record_and_its_words(station):
    busy_survey(station)
    rec = radio_use.busy_record(radio_use.check(station, "radio-b", programs={"rtl_power_gainfix.exe": 1}))
    assert rec == [{"radio": "radio-a", "serial": "00000011", "doing": "surveying", "program": "rtl_power",
                    "what": "marsad run, survey_band1_1h_busy_20010101-0000.csv"}]
    assert radio_use.describe(rec) == "radio-a surveying (marsad run, survey_band1_1h_busy_20010101-0000.csv)"
    assert radio_use.table_rows({"other_radios_busy": rec})[0].startswith("| Other radio busy at start | radio-a")
    assert radio_use.table_rows({}) == []                        # old runs: no row at all


# ---- the engine's check, as `marsad run` calls it

def test_check_device_free_refuses_the_busy_radio_without_a_terminal(station, monkeypatch, capsys):
    busy_survey(station)
    monkeypatch.setattr(record.host, "sdr_program_counts", lambda extra=(): {"rtl_power_gainfix.exe": 1})
    with pytest.raises(SystemExit) as e:
        record.check_device_free(True, station, "radio-a")
    assert "Refusing to start" in str(e.value) and "radio-a is in use" in capsys.readouterr().out
    others = record.check_device_free(True, station, "radio-b")
    assert [o["radio"] for o in others] == ["radio-a"]


# ---- the command line, end to end with a stand-in rtl_power

FAKE = r'''
import signal, sys, time, datetime as dt
args = sys.argv[1:]
lo, hi, _ = args[args.index("-f") + 1].split(":")
out = args[-1]
mhz = lambda t: float(t.rstrip("M")) * 1e6
lo, hi = mhz(lo), mhz(hi)
stop = []
for name in ("SIGBREAK", "SIGINT"):
    if hasattr(signal, name):
        signal.signal(getattr(signal, name), lambda *a: stop.append(1))
n, step = 64, (hi - lo) / 2 / 64
with open(out, "w") as f:
    k = 0
    while not stop and k < 400:
        now = dt.datetime.now()
        for h in range(2):
            a = lo + h * (hi - lo) / 2
            vals = ", ".join(f"{-40 + ((i * 7 + k) % 5) * 0.3:.2f}" for i in range(n))
            f.write(f"{now:%Y-%m-%d}, {now:%H:%M:%S}, {int(a)}, {int(a + n * step)}, {step:.2f}, 100, {vals}\n")
        f.flush()
        k += 1
        time.sleep(0.2)
sys.exit(0)
'''


def run_cli(station, tmp_path, monkeypatch, rx, tag, progs):
    """`marsad run band1 1h --receiver rx` with a stand-in rtl_power; stopped once its sidecar is
    written. Returns (exit or SystemExit text, sidecar)."""
    fake = tmp_path / "fake_rtl_power.py"
    fake.write_text(FAKE)
    real_popen = subprocess.Popen

    def popen(cmd, *a, **k):
        if isinstance(cmd, list) and cmd and cmd[0] == "FAKE_RTL_POWER":
            cmd = [sys.executable, str(fake), *cmd[1:]]
        return real_popen(cmd, *a, **k)
    monkeypatch.setattr(record.subprocess, "Popen", popen)
    monkeypatch.setattr(Station, "rtl_power", lambda self: (Path("FAKE_RTL_POWER"), None))
    monkeypatch.setattr(record, "device_check", lambda st, device: None)              # no rtl_eeprom
    monkeypatch.setattr(record.host, "sdr_program_counts", lambda extra=(): dict(progs))
    out = []

    def go():
        try:
            out.append(record.main(["band1", "1h", "--receiver", rx, "--gain", "29.7" if rx == "radio-b" else "29", "--tag", tag, "--chain", CHAIN,
                                    "--conditions", "made-up conditions", "--non-interactive", "--no-notify",
                                    "--no-live", "--no-analyze"], station))
        except SystemExit as exc:
            out.append(str(exc))
    t = threading.Thread(target=go)
    t.start()
    side = None
    for _ in range(300):
        js = list(station.surveys.glob(f"*_{tag}_*.json"))
        if js and js[0].with_suffix(".csv").exists():
            side = json.loads(js[0].read_text())
            Path(side["stop_file"]).parent.mkdir(parents=True, exist_ok=True)
            Path(side["stop_file"]).write_text("stop\n")
            break
        if out:
            break
        time.sleep(0.1)
    t.join(60)
    assert not t.is_alive()
    if side:
        side = json.loads(js[0].read_text())
    return out[0] if out else None, side


def test_cli_start_on_the_free_radio_records_the_busy_one(station, tmp_path, monkeypatch):
    busy_survey(station)
    rc, side = run_cli(station, tmp_path, monkeypatch, "radio-b", "freeb", {"rtl_power_gainfix.exe": 1})
    assert side, f"the run did not start: {rc}"
    assert side["receiver"] == "radio-b"
    assert side["other_radios_busy"][0]["radio"] == "radio-a"
    assert side["other_radios_busy"][0]["doing"] == "surveying"
    assert side["chain"]["chain_order"] == CHAIN                      # the record's chain text, byte for byte


def test_cli_start_on_the_busy_radio_is_refused_and_names_the_program(station, tmp_path, monkeypatch, capsys):
    busy_survey(station)
    rc, side = run_cli(station, tmp_path, monkeypatch, "radio-a", "busya", {"rtl_power_gainfix.exe": 1})
    assert side is None and "Refusing to start" in str(rc)
    assert "rtl_power" in capsys.readouterr().out
    assert not list(station.surveys.glob("*_busya_*"))                # nothing written


def test_record_field_is_outside_the_fingerprint(station, tmp_path, monkeypatch):
    """The same run with the other radio busy and idle: identical fingerprint, and the idle run's
    sidecar has no new field at all (byte-for-byte the old shape)."""
    rc1, idle = run_cli(station, tmp_path, monkeypatch, "radio-b", "idle", {})
    busy_survey(station)
    rc2, busy = run_cli(station, tmp_path, monkeypatch, "radio-b", "withbusy", {"rtl_power_gainfix.exe": 1})
    assert idle and busy
    assert "other_radios_busy" not in idle and busy["other_radios_busy"]
    bin_khz = idle["rtl_power"]["bin_khz"]
    assert rtlpower.fingerprint(idle, bin_khz) == rtlpower.fingerprint(busy, bin_khz)
    stripped = {k: v for k, v in busy.items() if k != "other_radios_busy"}
    assert rtlpower.fingerprint(stripped, bin_khz) == rtlpower.fingerprint(busy, bin_khz)
    assert set(busy) - set(idle) == {"other_radios_busy"}


# ---- the window's view (the service)

def test_scan_checks_free_radio_is_not_blocked(station, monkeypatch):
    busy_survey(station)
    monkeypatch.setattr(live.host, "running_sdr_programs", lambda: ["rtl_power_gainfix.exe"])
    monkeypatch.setattr(live, "detect_receivers", lambda st: [])
    b = live.scan_checks(station, "radio-b", detect=False)
    assert not b["in_use"]["blocked"] and b["in_use"]["other_busy"]
    a = live.scan_checks(station, "radio-a", detect=False)
    assert a["in_use"]["blocked"] and "radio-a is in use" in a["in_use"]["text"]


def test_radio_strip_says_ready_other_radio_busy(station, monkeypatch):
    busy_survey(station)
    monkeypatch.setattr(live.host, "running_sdr_programs", lambda: ["rtl_power_gainfix.exe"])
    monkeypatch.setattr(live, "usb_radios", lambda: {"00000011": "x", "00000022": "y"})
    r = {x["name"]: x for x in live.radios_status(station)["radios"]}
    assert r["radio-a"]["doing"]["state"] == "surveying"
    assert r["radio-b"]["doing"]["state"] == "ready" and r["radio-b"]["doing"]["other_busy"]
    assert r["radio-b"]["doing"]["text"].startswith("ready, other radio busy")


def test_radio_strip_still_blocks_both_for_an_unknown_program(station, monkeypatch):
    monkeypatch.setattr(live.host, "running_sdr_programs", lambda: ["sdrpp.exe"])
    monkeypatch.setattr(live, "usb_radios", lambda: {"00000011": "x", "00000022": "y"})
    r = {x["name"]: x for x in live.radios_status(station)["radios"]}
    assert r["radio-a"]["doing"]["state"] == r["radio-b"]["doing"]["state"] == "blocked"
    assert "cannot tell which radio" in r["radio-b"]["doing"]["text"]


def test_watch_two_radio_start_is_unchanged(station, monkeypatch):
    busy_survey(station)
    monkeypatch.setattr(live.host, "running_sdr_programs", lambda: ["rtl_power_gainfix.exe"])
    assert live.radio_in_use(station)["blocked"]                     # no radio named: any program blocks


def test_run_facts_carry_the_busy_note_and_a_short_chain(station):
    f = busy_survey(station, rx="radio-b", name="survey_band1_1h_f_20010101-0100")
    side = json.loads(f.with_suffix(".json").read_text())
    side.update(chain={"chain_order": CHAIN}, other_radios_busy=[{"radio": "radio-a", "doing": "recording",
                                                                    "what": "marsad watch fixed"}])
    f.with_suffix(".json").write_text(json.dumps(side))
    facts = live.run_facts(station, f)
    assert facts["other_radios_busy"] == "radio-a recording (marsad watch fixed)"
    assert facts["chain"]["full"] == CHAIN and facts["chain"]["short"] == "Chain → radio-b · 5 parts"


def test_chain_summary_uses_the_rig_name_and_the_open_check(station):
    s = live.chain_summary(station, "a > b > c", "radio-a", due=True)
    assert s["short"] == "Chain → radio-a · 3 parts · check due: still built as drawn?" and s["full"] == "a > b > c"
    assert live.chain_summary(station, "a > b", "radio-a")["short"].endswith("· ✓")
    assert live.chain_summary(station, "", "radio-a", problem="none saved")["short"] == "no chain · none saved"


def test_rig_name_drops_only_the_radio_suffix():
    assert live._rig_name("Test Rig (Made-up dongle (full name))") == "Test Rig"
    assert live._rig_name("Window rig") == "Window rig"
    assert live._rig_name("A (b) c") == "A (b) c"


def watch_checks(station, form):
    return live.watch_plan(station, {"watch": form})["checks"]["in_use"]


def test_fixed_recorder_on_the_free_radio_is_allowed(station, monkeypatch):
    """Owner, 2026-11-07: the per-radio rule covers a one-radio fixed watch too."""
    busy_survey(station)                                             # radio-a surveying
    monkeypatch.setattr(live.host, "running_sdr_programs", lambda: ["rtl_power_gainfix.exe"])
    free = watch_checks(station, {"mode": "fixed", "receiver": "radio-b", "freq": "300.5M"})
    assert not free["blocked"] and free["other_busy"]
    busy = watch_checks(station, {"mode": "fixed", "receiver": "radio-a", "freq": "300.5M"})
    assert busy["blocked"] and "radio-a is in use" in busy["text"]


def test_fixed_recorder_start_refuses_the_busy_radio(station, monkeypatch):
    busy_survey(station)
    monkeypatch.setattr(live.host, "running_sdr_programs", lambda: ["rtl_power_gainfix.exe"])
    with pytest.raises(ValueError, match="radio-a is in use"):
        live.watch_start(station, {"watch": {"mode": "fixed", "receiver": "radio-a", "freq": "300.5M"}})


def test_survey_and_record_still_needs_both_radios_free(station, monkeypatch):
    busy_survey(station)
    monkeypatch.setattr(live.host, "running_sdr_programs", lambda: ["rtl_power_gainfix.exe"])
    assert watch_checks(station, {"mode": "survey", "scan": live.scan_from_preset(station, "band1")})["blocked"]
