"""Pre-registered statistic for comparing station configurations.

    py tools/spur_stats.py reports/<run>/bins.csv
    py tools/spur_stats.py reports/<before>/bins.csv reports/<after>/bins.csv

The same as `sdr spur-stats`. The code lives in sdrstation/stats.py.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sdrstation.stats import family_levels_pairs as family_levels, load_bins_pairs as load_bins  # noqa: E402,F401
from sdrstation.stats import spur_stats_main  # noqa: E402


def main():
    return spur_stats_main(None, None, prog="py tools/spur_stats.py")


if __name__ == "__main__":
    sys.exit(main())
