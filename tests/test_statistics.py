"""The A/B statistics through the command line, on generated surveys with known answers:
a harmonic family lifted by a known amount, the floor moved by a known amount, and a
warm-up of a known size. Also: the analyser is deterministic, so a report regenerates
byte for byte.
"""
import filecmp
import re

import numpy as np
import pandas as pd
import pytest

from conftest import PY, run

SDR = [PY, "-m", "marsad"]
LO, HI, STEP = 700e6, 1700e6, 50e3
FAMILY = 28.8e6


def survey(path, seed=1, family_db=0.0, floor_db=0.0, sweeps=30, settle_db=0.0, minutes=2):
    """An rtl_power CSV, one hop per sweep: -40 dB noise, every 28.8 MHz harmonic 10 dB up."""
    rng = np.random.default_rng(seed)
    f = np.arange(LO, HI + 1, STEP)
    lines = 10.0 + 0.5 * rng.standard_normal(len(f))         # a per-line level, as real lines have
    harmonic = np.abs(f / FAMILY - np.round(f / FAMILY)) * FAMILY <= STEP / 2
    times = pd.date_range("2031-01-15 00:00", periods=sweeps, freq=f"{minutes}min")
    with open(path, "w", newline="\n") as fh:
        for i, t in enumerate(times):
            p = -40 + floor_db + 0.2 * rng.standard_normal(len(f)) + settle_db * np.exp(-i / 4.0)
            p[harmonic] += lines[harmonic] + family_db
            vals = ", ".join(f"{v:.2f}" for v in p)
            fh.write(f"{t:%Y-%m-%d}, {t:%H:%M:%S}, {int(LO)}, {int(HI)}, {STEP:.2f}, 10, {vals}\n")
    return path


@pytest.fixture(scope="module")
def reports(tmp_path_factory):
    root = tmp_path_factory.mktemp("stats")
    out = root / "reports"
    made = {}
    for name, kw in (("before", {"seed": 1}), ("after", {"seed": 2, "family_db": 1.0, "floor_db": 0.3})):
        csv = survey(root / f"survey_custom_1h_{name}_20310115-0000.csv", **kw)
        run([*SDR, "analyze", csv, "--out", out])
        made[name] = out / csv.stem
    return made


def test_spur_stats_recovers_the_known_lift(reports):
    text = run([*SDR, "spur-stats", reports["before"] / "bins.csv", reports["after"] / "bins.csv"])
    change = float(re.search(r"mean change\s+([+-][\d.]+) dB", text).group(1))
    assert change == pytest.approx(1.0, abs=0.25)            # relative to each run's median, so the floor cancels
    n = int(re.search(r"n = (\d+) harmonics", text).group(1))
    assert n == 35                                           # 28.8 MHz x 25..59 in 700-1700


def test_compare_runs_reports_the_floor_shift(reports):
    text = run([*SDR, "compare-runs", reports["before"], reports["after"]])
    change = float(re.search(r"change ([+-][\d.]+) dB", text).group(1))
    assert change == pytest.approx(0.3, abs=0.05)


def test_drift_finds_a_known_warm_up(tmp_path):
    csv = survey(tmp_path / "survey_custom_1h_warm_20310115-0000.csv", settle_db=2.0, sweeps=60)
    text = run([*SDR, "drift", csv])
    sweep = int(re.search(r"settled by sweep\s+(\d+) of 60", text).group(1))
    assert 8 <= sweep <= 25
    assert float(re.search(r"total change\s+([+-][\d.]+) dB", text).group(1)) == pytest.approx(-2.0, abs=0.3)


def test_analysis_regenerates_byte_for_byte(reports, tmp_path):
    before = reports["before"]
    csv = before.parent.parent / f"{before.name}.csv"
    run([*SDR, "analyze", csv, "--out", tmp_path])
    for name in ("bins.csv", "peaks.csv", "events.csv"):
        assert filecmp.cmp(before / name, tmp_path / before.name / name, shallow=False), name
