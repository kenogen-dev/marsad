"""Shared fixtures for the regression suite.

These tests pin the numbers the measurement programme depends on. Most of them
need the station's own raw surveys (surveys/*.csv) and the reports already
generated from them (reports/*/bins.csv), which are not in git. On a machine
without that data those tests SKIP rather than fail; the pure-arithmetic tests
(scan geometry, fingerprint wording) run anywhere.
"""
from __future__ import annotations

import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
SURVEYS = ROOT / "surveys"
REPORTS = ROOT / "reports"
PY = sys.executable          # pytest runs under the venv, which has numpy/pandas


def survey_csv(name: str) -> Path:
    p = SURVEYS / f"{name}.csv"
    if not p.exists():
        pytest.skip(f"station data not present: surveys/{name}.csv")
    return p


def report_dir(name: str) -> Path:
    p = REPORTS / name
    if not (p / "bins.csv").exists():
        pytest.skip(f"station report not present: reports/{name}")
    return p


def run(cmd: list, cwd: Path = ROOT) -> str:
    """Run a command line the way the owner types it and return stdout+stderr."""
    r = subprocess.run([str(c) for c in cmd], cwd=str(cwd), capture_output=True,
                       text=True, timeout=1800)
    assert r.returncode == 0, f"exit {r.returncode}\n{r.stdout}\n{r.stderr}"
    return r.stdout + r.stderr


# ---------------------------------------------------------------- one filled recorder station
# The Captures tab and capture-archive tests all start from the same synthetic station,
# filled by the real recorder with a replayed radio. Building it takes a couple of seconds,
# so it is built once per test process and copied for each test that changes it.

@pytest.fixture(scope="session")
def filled_template(tmp_path_factory):
    """One open signal (433.92 MHz, captured and characterised) and one private one
    (880.5 MHz, 850 cellular downlink, fingerprints only). Do not modify: use
    `copy_station` to get a private copy."""
    from marsad import capture as cap
    from marsad import watch as W
    from test_watch import V5, Launches, scene, station
    root = tmp_path_factory.mktemp("filled")
    st = station(root, {"capture_s": 4})
    cfg = cap.settings(st)
    W.fixed_watch(W.Recorder(st, cfg, V5, ["rtl_sdr"], "test chain", 29.7,
                             Launches(scene(10.0, 0.4, 2.5, kind="fsk"), 433.92e6), quiet=True,
                             inline_characterise=True), 433.92e6, 9.0, width_hz=20e3)
    W.fixed_watch(W.Recorder(st, cfg, V5, ["rtl_sdr"], "test chain", 29.7,
                             Launches(scene(6.0, 0.6, 2.5), 880.5e6), quiet=True),
                  880.5e6, 6.0, width_hz=20e3)
    return root


def copy_station(src: Path, dest: Path):
    """Copy a station folder and point the catalogue's file paths at the copy."""
    from marsad.config import Station
    shutil.copytree(src, dest, dirs_exist_ok=True)
    con = sqlite3.connect(dest / "occupancy.db")
    cols = [r[1] for r in con.execute("PRAGMA table_info(captures)") if r[2].upper() == "TEXT"]
    for old, new in ((str(src), str(dest)), (src.as_posix(), dest.as_posix())):
        for c in cols:
            con.execute(f"UPDATE captures SET {c} = replace({c}, ?, ?) WHERE {c} LIKE ?",
                        (old, new, f"%{old}%"))
    con.commit()
    con.close()
    return Station.load(dest)
