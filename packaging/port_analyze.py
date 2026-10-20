"""Generate sdrstation/analyze.py from tools/survey.py.

Only the module header and main() change; every function body between the
Loading section and the CLI is copied verbatim so the numbers cannot move.
"""
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
src = (root / "tools" / "survey.py").read_text(encoding="utf-8")
start = src.index("# ----------------------------------------------------------------------------\n# Loading")
end = src.index("# ----------------------------------------------------------------------------\n# CLI")
body = src[start:end]
cli = src[end:src.index('if __name__ == "__main__":')]
head = '''"""Analyse rtl_power survey CSVs.

  sdr analyze  survey.csv [--threshold 10] [--out reports] [--mask 1095-1240]
  sdr compare  before.csv after.csv [--threshold 10] [--out reports]

What it produces (in <out>/<survey name>/):
  heatmap.png      waterfall: frequency across, time down, colour = power
  spectrum.png     max / median / floor / threshold across frequency, detected signals marked
  occupancy.png    % of sweeps each frequency was above the threshold
  combined.png     the three above stacked on one frequency axis
  peaks.csv        every detected signal with level, SNR, occupancy, class and spur tags
  bins.csv         per-bin floor, median, max and occupancy (for your own analysis)
  report.md        readable summary

Requires numpy, pandas, matplotlib (scipy optional, but it changes which peaks
are found, so a shipped build always includes it).

rtl_power CSV format, one line per hop per sweep:
  date, time, Hz low, Hz high, Hz step, samples, dB, dB, dB, ...

The receiver gaps and spur families come from station.json via `configure()`;
the module-level tables below are only the fallback for a bare import.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import warnings
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .config import Station

# Filled in by configure(station). The defaults match station.default.json so
# a bare import behaves like the original tool.
RECEIVER_GAPS_MHZ: dict = {"xtr": [(1095.0, 1240.0)], "v5": []}
SPUR_FAMILIES_MHZ: list = [
    (28.8, "28.8 MHz SDR clock harmonic"),
    (0.6, "600 kHz comb (switching converter?)"),
    (480.0, "USB 2.0 (480 Mb/s) harmonic"),
    (24.0, "24 MHz crystal harmonic"),
    (25.0, "25 MHz clock harmonic"),
    (100.0, "100 MHz PCIe clock harmonic"),
]


def configure(station: Station) -> None:
    """Take the gap table and spur families from the station file."""
    global RECEIVER_GAPS_MHZ, SPUR_FAMILIES_MHZ
    RECEIVER_GAPS_MHZ = station.receiver_gaps()
    SPUR_FAMILIES_MHZ = station.spur_families


'''
cli = cli.replace(
    "def main(argv=None):",
    'def main(argv=None, station: Station | None = None, prog: str = "sdr"):\n'
    "    station = station or Station.load()\n"
    "    configure(station)")
cli = cli.replace(
    'p = argparse.ArgumentParser(description="Analyze rtl_power survey CSVs.")',
    'p = argparse.ArgumentParser(prog=prog, description="Analyze rtl_power survey CSVs.")')
assert "configure(station)" in cli and "prog=prog" in cli
out = head + body + cli
(root / "sdrstation" / "analyze.py").write_text(out, encoding="utf-8")
print(len(out.splitlines()), "lines written")
