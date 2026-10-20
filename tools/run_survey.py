#!/usr/bin/env python3
"""run_survey.py - record a survey from a preset, then analyse it.

  py tools/run_survey.py <preset> <duration> [--tag x] [--gain N] [--integration N]
                         [--freq LO:HI:BIN] [--conditions "..."] [--chain "src > ... > PC"]
                         [--skip-warmup MIN] [--dry-run] [--no-analyze] [--no-notify]
                         [--non-interactive] [--list]

The same as `marsad run`. The recorder lives in marsad/record.py and the scan
geometry in marsad/rtlpower.py; the presets and the measured retune time come
from station.json. This file stays so the commands in RUNSHEET.md keep working,
and so the regression tests can still
`from run_survey import scan_geometry, fingerprint, RTL_POWER_MAX_MHZ`.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from marsad.config import Station                              # noqa: E402
from marsad import rtlpower as _rtl                            # noqa: E402
from marsad.rtlpower import RTL_POWER_MAX_MHZ, fingerprint     # noqa: E402,F401
from marsad.record import main as _main                        # noqa: E402

STATION = Station.load(ROOT)
PRESETS = STATION.presets
RETUNE_MS_PER_HOP = STATION.retune_ms_per_hop


def scan_geometry(lo_mhz, hi_mhz, bin_khz, integration_s, duration_s, crop=0.25):
    """The pre-packaging signature: the station's measured retune time is filled in."""
    return _rtl.scan_geometry(lo_mhz, hi_mhz, bin_khz, integration_s, duration_s,
                              crop=crop, retune_ms_per_hop=RETUNE_MS_PER_HOP)


def main(argv=None):
    return _main(argv, STATION, prog="py tools/run_survey.py")


if __name__ == "__main__":
    raise SystemExit(main())
