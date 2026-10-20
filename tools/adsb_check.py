"""ADS-B receive check at 1090 MHz: a positive control for the antenna/chain.

    py tools/adsb_check.py --seconds 600 --gain 40.2 --label "v5 46mm"

The same as `sdr adsb-check`. The code lives in sdrstation/adsb.py.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sdrstation.config import Station                    # noqa: E402
from sdrstation.adsb import main as _main, parse_frames, verdict  # noqa: E402,F401


def main():
    return _main(None, Station.load(ROOT), prog="py tools/adsb_check.py")


if __name__ == "__main__":
    sys.exit(main())
