"""Shared fixtures for the regression suite.

These tests pin the numbers the measurement programme depends on. Most of them
need the station's own raw surveys (surveys/*.csv) and the reports already
generated from them (reports/*/bins.csv), which are not in git. On a machine
without that data those tests SKIP rather than fail; the pure-arithmetic tests
(scan geometry, fingerprint wording) run anywhere.
"""
from __future__ import annotations

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
