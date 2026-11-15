"""Relay 020: every start ends visibly. The service's side: the launcher records how a run ended, /launch/status reads
a launched run's own log (the recording it names, or how it ended and in whose words), a gain the radio's tuner has no
step for is refused before anything is launched, and a run in its first seconds holds its own radio instead of
blocking both. Made-up station; no radio is opened."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from marsad import live, radio_use
from marsad.config import Station

DATA = Path(__file__).parent / "data"


@pytest.fixture
def station(tmp_path):
    root = tmp_path / "st"
    root.mkdir()
    shutil.copy(DATA / "builder_station.json", root / "station.json")
    (root / "surveys").mkdir()
    (root / "reports").mkdir()
    (root / live.LAUNCH_LOG_DIR).mkdir()
    return Station.load(str(root))


def run_launcher(tmp: Path, code: str, log: Path) -> int:
    """The launcher wrapper as launch_outside starts it, on a harmless child."""
    spec = tmp / "spec.json"
    spec.write_text(json.dumps({"argv": [sys.executable, "-c", code], "cwd": str(tmp), "log": str(log)}))
    return subprocess.call([sys.executable, "-c", live._LAUNCHER, str(spec)])


def test_launcher_writes_the_exit_code(station, tmp_path):
    log = station.root / "logs" / "x_run.log"
    rc = run_launcher(tmp_path, "print('refused: made-up reason'); raise SystemExit(3)", log)
    assert rc == 3
    ended = json.loads((log.parent / (log.name + ".exit")).read_text())
    assert ended["exit_code"] == 3 and ended["ended"] > 0
    assert "refused: made-up reason" in log.read_text()


def test_status_of_a_run_refused_at_once(station, tmp_path):
    log = station.root / "logs" / "y_run.log"
    run_launcher(tmp_path, "print('gain 29 dB is not a R820T/R860 step. Nearest is 29.7.'); raise SystemExit(1)", log)
    st = live.launch_status(station, str(log))
    assert st["running"] is False and st["exit_code"] == 1 and st["recording"] is None
    assert "not a R820T/R860 step" in st["reason"]


def test_status_names_the_recording_once_the_sidecar_is_written(station):
    log = station.root / "logs" / "z_run.log"
    side = station.surveys / "survey_band1_1h_20010101-0900.json"
    side.write_text(json.dumps({"csv": "survey_band1_1h_20010101-0900.csv", "receiver": "radio-a"}))
    log.write_text("  SDR is free\n  Sidecar written: surveys/survey_band1_1h_20010101-0900.json\n"
                   "    1:00 elapsed, 9:00 left\n")
    st = live.launch_status(station, str(log))
    assert st["running"] is True and st["exit_code"] is None
    assert st["recording"] == "survey_band1_1h_20010101-0900.csv" and st["recording_exists"] is False
    (station.surveys / "survey_band1_1h_20010101-0900.csv").write_text("x")
    assert live.launch_status(station, str(log))["recording_exists"] is True
    assert "elapsed" not in live.launch_status(station, str(log))["reason"]      # progress lines are not "what it said"


def test_status_reads_only_this_stations_launch_logs(station, tmp_path):
    other = tmp_path / "elsewhere.log"
    other.write_text("secret")
    with pytest.raises(ValueError):
        live.launch_status(station, str(other))
    with pytest.raises(ValueError):
        live.launch_status(station, str(station.root / "station.json"))


def test_a_gain_the_tuner_has_no_step_for(station):
    assert live.gain_problem(station, "radio-a", 29) is None                 # an E4000 step
    msg = live.gain_problem(station, "radio-b", 29)                          # radio-b is an R820T
    assert msg and "29 dB is not a gain step" in msg and "Nearest is 29.7 dB" in msg
    assert live.gain_problem(station, "radio-b", 29.7) is None


def test_a_watch_survey_with_the_wrong_gain_is_refused_before_launch(station, monkeypatch):
    launched = []
    monkeypatch.setattr(live, "launch_outside", lambda st_, argv, label: launched.append(argv) or {"pid": 1, "log": "x"})
    monkeypatch.setattr(live, "find_radio", lambda st_, rx: {"found": True, "device": 0, "rx": rx, "text": "found"})
    monkeypatch.setattr(live.host, "sdr_program_counts", lambda extra=(): {})
    monkeypatch.setattr(live.host, "running_sdr_programs", lambda: [])
    form = {"watch": {"mode": "survey", "surveyor": "radio-b", "recorder": "radio-a",
                      "scan": {"preset": "band1", "durVal": 10, "unit": "m"}},
            "chains": {"radio-a": "made-up > radio-a", "radio-b": "made-up > radio-b"},
            "chain_confirmed": True, "problems_confirmed": True}
    plan = live.watch_plan(station, form)
    assert any(w[0] == "bad" and w[1] == "Gain" for w in plan["survey"]["warnings"])
    with pytest.raises(ValueError, match="not a gain step"):
        live.watch_start(station, form)
    assert not launched


def test_a_run_still_starting_holds_its_own_radio_only(station):
    now = time.time()
    side = station.surveys / "survey_band1_1h_20010101-0900.json"
    side.write_text(json.dumps({"csv": "survey_band1_1h_20010101-0900.csv", "receiver": "radio-a",
                                "engine": "rtl_power"}))
    progs = {"rtl_power.exe": 1}                     # its program runs; its recording is not written yet
    a = radio_use.check(station, "radio-a", programs=progs, now=now)
    b = radio_use.check(station, "radio-b", programs=progs, now=now)
    assert a["ok"] is False and "still starting" in a["reason"]
    assert b["ok"] is True                           # it used to refuse both: "cannot tell which radio it holds"
    late = radio_use.check(station, "radio-b", programs=progs, now=now + radio_use.STARTING_S + 5)
    assert late["ok"] is False                       # past the starting window, an unexplained program blocks again


def test_a_recorders_own_gain_is_checked_against_its_tuner(station, monkeypatch):
    """Relay 030 (known issue 6): survey + record checked the survey's gain only; the recording radio's
    --recorder-gain went to the engine unchecked."""
    launched = []
    monkeypatch.setattr(live, "launch_outside", lambda st_, argv, label: launched.append(argv) or {"pid": 1, "log": "x"})
    monkeypatch.setattr(live, "find_radio", lambda st_, rx: {"found": True, "device": 0, "rx": rx, "text": "found"})
    monkeypatch.setattr(live.host, "sdr_program_counts", lambda extra=(): {})
    monkeypatch.setattr(live.host, "running_sdr_programs", lambda: [])
    form = {"watch": {"mode": "survey", "surveyor": "radio-a", "recorder": "radio-b", "recorder_gain": 29,
                      "scan": {"preset": "band1", "durVal": 10, "unit": "m"}},
            "chains": {"radio-a": "made-up > radio-a", "radio-b": "made-up > radio-b"},
            "chain_confirmed": True, "problems_confirmed": True}
    plan = live.watch_plan(station, form)
    assert not any(w[1] == "Gain" for w in plan["survey"]["warnings"])           # the survey's gain is fine
    assert any(w[0] == "bad" and w[1] == "Recorder gain" for w in plan["survey"]["warnings"])
    with pytest.raises(ValueError, match="The recorder: 29 dB is not a gain step"):
        live.watch_start(station, form)
    assert not launched
    form["watch"]["recorder_gain"] = 29.7                                         # an R820T step: no refusal
    assert not any(w[1] == "Recorder gain" for w in live.watch_plan(station, form)["survey"]["warnings"])
