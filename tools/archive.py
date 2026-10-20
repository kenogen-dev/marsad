"""Long-term occupancy archive in occupancy.db.

    py tools/archive.py add --all | add reports/<name> [--replace]
    py tools/archive.py list | new | trend <MHz> | band <lo> <hi> | seen | mark <survey> --terminated|--antenna

The same as `sdr archive ...`. The code lives in sdrstation/archive.py.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sdrstation.config import Station                          # noqa: E402
from sdrstation.archive import main as _main, looks_terminated  # noqa: E402,F401


def main():
    return _main(None, Station.load(ROOT), prog="py tools/archive.py")


if __name__ == "__main__":
    raise SystemExit(main())
