#!/usr/bin/env python3
"""live_view.py - the station's local web app: Scan (live and archived), Builder,
Archive and Roadmap tabs.

    py tools/live_view.py [--csv FILE] [--port 8731] [--pin] [--open]
                          [--exit-after-idle MIN]

The same as `sdr live`. The server lives in sdrstation/live.py and the page in
sdrstation/web/index.html (edit the page there now; this file no longer holds it).
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sdrstation.config import Station          # noqa: E402
from sdrstation.live import main as _main      # noqa: E402


def main(argv=None):
    return _main(argv, Station.load(ROOT), prog="py tools/live_view.py")


if __name__ == "__main__":
    raise SystemExit(main())
