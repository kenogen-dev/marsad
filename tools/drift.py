"""Thermal / time drift of a survey, from the per-sweep whole-band median.

    py tools/drift.py <tag-or-csv-or-report> [--threshold 0.1] [--plot-text]

The same as `sdr drift`. The code lives in sdrstation/drift.py.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sdrstation.config import Station              # noqa: E402
from sdrstation.drift import main as _main, settle_index  # noqa: E402,F401


def main():
    return _main(None, Station.load(ROOT), prog="py tools/drift.py")


if __name__ == "__main__":
    sys.exit(main())
