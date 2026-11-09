"""Shared fixtures for the regression suite.

Everything here runs on synthetic data, with no radio and no station folder: an empty
temporary station is set for the whole run, so a station.json lying beside the code
cannot change a result.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
PY = sys.executable          # pytest runs under the venv, which has numpy/pandas


def run(cmd: list, cwd: Path = ROOT) -> str:
    """Run a command line and return stdout+stderr."""
    r = subprocess.run([str(c) for c in cmd], cwd=str(cwd), capture_output=True,
                       text=True, timeout=1800)
    assert r.returncode == 0, f"exit {r.returncode}\n{r.stdout}\n{r.stderr}"
    return r.stdout + r.stderr


@pytest.fixture(autouse=True, scope="session")
def empty_station(tmp_path_factory):
    """Every Station.load() and every `python -m marsad` child sees built-in defaults."""
    root = tmp_path_factory.mktemp("station")
    old = os.environ.get("SDR_STATION")
    os.environ["SDR_STATION"] = str(root)
    os.environ["MARSAD_HOME"] = ""            # no home station, whatever this computer has
    yield root
    if old is None:
        os.environ.pop("SDR_STATION", None)
    else:
        os.environ["SDR_STATION"] = old


@pytest.fixture(autouse=True)
def no_usb_details(monkeypatch):
    """Relay 018: where each radio is plugged comes from Windows' device list. Never this computer's own in the
    suite; a test that wants USB places patches marsad.live.usb_details itself."""
    from marsad import live
    monkeypatch.setattr(live, "usb_details", lambda: {})


_RIG = {}


def rig():
    """A station with an E4000 as its default receiver and a chain with an LNA in it, at
    the gains the Probe tab's ranges are written for. One folder per test process."""
    from marsad.config import Station, builtin_defaults
    if "root" not in _RIG:
        root = Path(tempfile.mkdtemp(prefix="marsad-rig-"))
        presets = builtin_defaults()["presets"]
        for name in ("wide", "deep", "custom"):
            presets[name] = {**presets[name], "gain": 29.0, "receiver": "e4000"}
        (root / "station.json").write_text(json.dumps({
            "default_receiver": "e4000", "presets": presets,
            "probe": {"chain_tail": {
                "e4000": "SMA adapter > SMA DC block > 8 ft coax > FM notch > LNA (at the antenna) > "
                         "3 ft RG316 coax > E4000 > 16 ft USB > USB isolator > PC",
                "r820t": "SMA adapter > SMA DC block > 8 ft coax > r820t (no LNA) > USB cable > PC"}},
        }), encoding="utf-8")
        _RIG["root"] = root
    return Station.load(_RIG["root"])


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
    from test_watch import R820T, Launches, scene, station
    root = tmp_path_factory.mktemp("filled")
    st = station(root, {"capture_s": 4})
    cfg = cap.settings(st)
    W.fixed_watch(W.Recorder(st, cfg, R820T, ["rtl_sdr"], "test chain", 29.7,
                             Launches(scene(10.0, 0.4, 2.5, kind="fsk"), 433.92e6), quiet=True,
                             inline_characterise=True), 433.92e6, 9.0, width_hz=20e3)
    W.fixed_watch(W.Recorder(st, cfg, R820T, ["rtl_sdr"], "test chain", 29.7,
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


@pytest.fixture(autouse=True)
def process_counts_follow_the_program_list(monkeypatch):
    """Relay 012: the per-radio check counts SDR processes (host.sdr_program_counts). A test that
    fakes running programs fakes host.running_sdr_programs; derive the counts from it, so one
    stand-in drives both and no test reads this computer's real process list for the new check."""
    from marsad import host

    def counts(extra=()):
        names = host.running_sdr_programs()
        return None if names is None else {n: 1 for n in names}
    monkeypatch.setattr(host, "sdr_program_counts", counts)
