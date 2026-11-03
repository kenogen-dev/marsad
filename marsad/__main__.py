"""`py -m marsad ...` is the same as `marsad ...` (and `sdr ...`, the short alias)."""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
