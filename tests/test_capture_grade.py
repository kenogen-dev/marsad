"""Capture strength grade, the watch's trigger flags and the keep-IQ minimum.

Measured that day: of 187 IQ captures in occupancy.db, 135 had a recorder SNR under
12 dB and the characteriser called 31 "nothing found" or "too weak to classify"; the
operator saw captures with nothing in the waterfall, and the first L-band watch wrote
2.37 GB in an hour. No radio is opened here: the same replayed scenes as test_watch.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from marsad import capture as cap
from marsad import catalogue as C
from marsad import watch as W
from test_watch import R820T, Launches, WriteSpy, scene, snapshots, station


# ---------------------------------------------------------------- the grade

def test_grade_thresholds_and_empty_families():
    assert cap.grade_for(25) == "strong" and cap.grade_for(20) == "strong"
    assert cap.grade_for(19.9) == "medium" and cap.grade_for(12) == "medium"
    assert cap.grade_for(11.9) == "weak" and cap.grade_for(6) == "weak"
    assert cap.grade_for(5.9) == "empty" and cap.grade_for(-3) == "empty"
    assert cap.grade_for(30, "nothing found") == "empty"
    assert cap.grade_for(30, "too weak to classify") == "empty"
    assert cap.grade_for(None) is None


def test_grade_prefers_the_characteriser_then_the_recorder_peak(tmp_path):
    md = tmp_path / "cap_x.characterise.md"
    md.write_text("report")
    (tmp_path / "cap_x.characterise.json").write_text(json.dumps({"pulses": {"snr_db_max": 23.0}}))
    row = {"snr_peak_db": 9.0, "characterised": str(md), "family": "pulsed"}
    assert cap.grade_row(row) == ("strong", 23.0, "characteriser")       # read from the old report
    assert cap.grade_row({**row, "capture_snr_db": 14.0})[:2] == ("medium", 14.0)   # stored wins
    assert cap.grade_row({"snr_peak_db": 9.0}) == ("weak", 9.0, "recorder event peak")
    assert cap.grade_row({"snr_peak_db": 30.0, "family": "nothing found"})[0] == "empty"
    assert cap.grade_row({"snr_peak_db": 30.0, "receiver": "survey"})[0] is None   # the survey's, not ours
    (tmp_path / "c.characterise.json").write_text(json.dumps({"continuous": {"snr_db": 4.0}}))
    assert cap.characterised_snr(str(tmp_path / "c.characterise.md")) == 4.0


def test_old_catalogue_gains_grade_columns_and_still_grades_read_only(tmp_path):
    st = station(tmp_path)
    c = sqlite3.connect(st.db)
    old = cap.SCHEMA.replace("  target_hz       REAL,\n", "").replace(
        ",\n  capture_snr_db  REAL,\n  grade           TEXT,\n  settings        TEXT\n", "\n")
    c.executescript(old)
    c.execute("INSERT INTO captures (started, started_epoch, freq_hz, snr_peak_db, iq_status, receiver) "
              "VALUES ('2031-10-31T19:20:00', 1.0, 1300.00e6, 10.3, 'written', 'r820t')")
    c.commit(); c.close()
    s = C.summary(st)                                  # read-only: the old table as it is
    r = s["groups"][0]["rows"][0]
    assert r["grade"] == "weak" and r["grade_snr_db"] == 10.3 and s["groups"][0]["grade"] == "weak"
    con = cap.connect(st)
    have = {r[1] for r in con.execute("PRAGMA table_info(captures)")}
    assert {"capture_snr_db", "grade", "settings", "target_hz"} <= have


def test_catalogue_filters_by_grade_and_groups_take_the_best(tmp_path):
    st = station(tmp_path)
    con = cap.connect(st)
    for i, snr in enumerate([25.0, 8.0, 15.0, 3.0]):
        cap.add_row(con, {"started": f"2031-10-31T20:0{i}:00", "started_epoch": 100.0 + i, "freq_hz": 433.92e6,
                          "target_hz": 433.92e6, "receiver": "r820t", "snr_peak_db": snr, "iq_status": "written",
                          "tier": "open"})
    con.close()
    s = C.summary(st)
    assert s["groups"][0]["grade"] == "strong"
    assert s["groups"][0]["grades"] == {"strong": 1, "medium": 1, "weak": 1, "empty": 1}
    assert [r["snr_peak_db"] for r in C.summary(st, grade="weak")["groups"][0]["rows"]] == [8.0]
    assert C.summary(st, grade="ungraded")["total"] == 0


# ---------------------------------------------------------------- keep-IQ minimum

def test_keep_level_never_reached_writes_no_iq_at_all(tmp_path, monkeypatch):
    """The first seconds are held in memory, so a capture that stays under the keep level
    leaves nothing on disk: not a partial file, not a temp file. Nothing is deleted."""
    st = station(tmp_path, {"capture_s": 4, "keep_min_snr_db": 80})
    cfg = cap.settings(st)
    spy = WriteSpy(monkeypatch)
    rec = W.Recorder(st, cfg, R820T, ["rtl_sdr"], "c", 29.7, Launches(scene(9.0, 0.4, 2.5, kind="fsk"), 433.92e6),
                     quiet=True, inline_characterise=True)
    tot = W.fixed_watch(rec, 433.92e6, 8.0, width_hz=20e3)
    rows = rec.con.execute("SELECT * FROM captures ORDER BY started_epoch").fetchall()
    assert len(rows) >= 2 and tot["captures"] == 0
    assert all(r["iq_status"] == cap.BELOW_KEEP for r in rows)       # each event tried, none reached 80 dB
    assert all("stayed under the keep level 80 dB" in r["notes"] for r in rows)
    assert all(Path(r["fingerprint"]).exists() for r in rows)
    assert not [p for p in spy.opened if ".sigmf" in p]
    assert json.loads(rows[0]["settings"])["keep_min_snr_db"] == 80
    assert rows[0]["grade"] in ("strong", "medium")                  # the event itself was strong


def test_keep_level_reached_keeps_the_whole_capture(tmp_path):
    st = station(tmp_path, {"capture_s": 4, "keep_min_snr_db": 15})
    cfg = cap.settings(st)
    rec = W.Recorder(st, cfg, R820T, ["rtl_sdr"], "c", 29.7, Launches(scene(9.0, 0.4, 2.5, kind="fsk"), 433.92e6),
                     quiet=True, inline_characterise=True)
    W.fixed_watch(rec, 433.92e6, 8.0, width_hz=20e3)
    w = rec.con.execute("SELECT * FROM captures WHERE iq_status = 'written'").fetchall()
    assert len(w) == 1
    secs = Path(w[0]["iq_path"]).stat().st_size / 4 / w[0]["store_rate_hz"]
    assert secs == pytest.approx(4 + cap.PRE_S, abs=0.2)             # pre-trigger second and all
    assert w[0]["capture_snr_db"] is not None and w[0]["grade"] in ("strong", "medium", "weak")


def test_keep_off_by_default(tmp_path):
    cfg = cap.settings(station(tmp_path))
    assert cfg["keep_min_snr_db"] == 0 and cfg["keep_check_s"] == 3.0


# ---------------------------------------------------------------- flags, dry run, status

def test_flags_override_station_json_and_reach_every_row(tmp_path, monkeypatch):
    st = station(tmp_path, {"threshold_db": 12, "cooldown_min": 30})
    got = snapshots(monkeypatch)
    rc = W.main(["fixed", "--freq", "433.92M", "--duration", "6s", "--width", "20k", "--threshold", "15",
                 "--cooldown", "0.05", "--capture", "3", "--keep-min-snr", "13"], st,
                detect=lambda s: {"found": [{"device": 0, "product": "r820t", "serial": "00000202", "tuner": "R820T"}],
                                  "error": None},
                launch=Launches(scene(6.0, 0.4, 2.5, kind="fsk"), 433.92e6), exe=["rtl_sdr"])
    assert rc == 0
    con = cap.connect(st)
    rows = con.execute("SELECT settings FROM captures").fetchall()
    assert rows
    s = json.loads(rows[0]["settings"])
    assert s == {"threshold_db": 15.0, "passes": 2, "ambush_db": 20, "cooldown_min": 0.05, "capture_s": 30,
                 "keep_min_snr_db": 13.0, "capture_override_s": 3.0, "known_cooldown_min": 0, "look_s": 0}
    assert got[0]["settings"]["threshold_db"] == 15.0 and got[0]["queue"] == 0


def test_fixed_dry_run_prints_settings_and_disk(tmp_path, capsys):
    st = station(tmp_path, {"daily_gb": 10})
    W.main(["fixed", "--freq", "1300.00M", "--duration", "1h", "--cooldown", "10", "--capture", "20",
            "--dry-run"], st)
    out = capsys.readouterr().out
    assert "settings: threshold 12 dB, cooldown 10.0 min, capture 20 s (over any recipe), keep level off" in out
    assert "each IQ capture about 202 MB" in out                      # 21 s x 2.4 MS/s x 4 bytes
    assert "GB/h per signal at most" in out and "of the 10 GB daily cap" in out


def test_survey_dry_run_measures_the_last_watch_on_the_same_range(tmp_path, capsys):
    st = station(tmp_path, {"daily_gb": 10})
    name = "survey_milair_4h_watch-x_20311030-1312"
    st.surveys.mkdir(parents=True, exist_ok=True)
    (st.surveys / f"{name}.json").write_text(json.dumps({
        "preset": "milair", "command": ["rtl_power", "-f", st.presets["milair"]["freq"], "-g", "19"],
        "started_local": "2031-10-30T13:12:00", "duration_actual_s": 7200}))
    con = cap.connect(st)
    for snr in (25.0, 9.0, 10.0, 30.0):
        cap.add_row(con, {"started": "2031-10-30T14:00:00", "started_epoch": 0.0, "freq_hz": 300e6,
                          "receiver": "r820t", "survey": name, "snr_peak_db": snr, "iq_bytes": 500_000_000,
                          "iq_status": "written"})
    con.close()
    W.main(["survey", "milair", "4h", "--keep-min-snr", "12", "--dry-run"], st)
    out = capsys.readouterr().out
    assert "4 captures, 2.00 GB in 2.0 h = 1.00 GB/h" in out
    assert "2 of them reached the keep level 12 dB, so about 0.50 GB/h with it" in out
    assert "settings: threshold 12 dB, 2 passes, ambush 20 dB" in out


def test_daily_cap_time(tmp_path):
    st = station(tmp_path, {"daily_gb": 10})
    now = time.mktime((2031, 10, 31, 19, 0, 0, 0, 0, -1))
    lines = W.storage_lines(st, cap.settings(st), None,
                            {"survey": "s", "hours": 1.0, "captures": 37, "bytes": 2.5e9, "snrs": []}, now=now)
    assert lines[-1].endswith("the cap is reached about 23:00")
