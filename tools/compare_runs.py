"""Compare two surveys the way this station's protocol says to.

    py tools/compare_runs.py <before> <after> [--family 14.4] [--handling untouched]

The same as `marsad compare-runs`. The code lives in marsad/stats.py.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from marsad.config import Station        # noqa: E402
from marsad.stats import (autocorr_peaks, bin_width, compare_runs_main,  # noqa: E402,F401
                              family_levels, load_bins, paired)


def main():
    return compare_runs_main(None, Station.load(ROOT), prog="py tools/compare_runs.py")


if __name__ == "__main__":
    sys.exit(main())
