"""`py -m sdrstation ...` is the same as `sdr ...`."""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
