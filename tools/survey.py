#!/usr/bin/env python3
"""survey.py - analyze rtl_power survey CSVs.

  python survey.py analyze  survey.csv [--threshold 10] [--out reports] [--mask 1095-1240]
  python survey.py compare  before.csv after.csv [--threshold 10] [--out reports]

The same as `sdr analyze` / `sdr compare`. The code lives in sdrstation/analyze.py;
this file stays so the recorded analysis commands keep working, and so
`from survey import load_rtl_power` still works.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sdrstation.config import Station          # noqa: E402
from sdrstation import analyze as _analyze     # noqa: E402
from sdrstation.analyze import *               # noqa: E402,F401,F403

STATION = Station.load(ROOT)
_analyze.configure(STATION)
RECEIVER_GAPS_MHZ = _analyze.RECEIVER_GAPS_MHZ
SPUR_FAMILIES_MHZ = _analyze.SPUR_FAMILIES_MHZ


def main(argv=None):
    return _analyze.main(argv, STATION, prog="survey.py")


if __name__ == "__main__":
    main()
