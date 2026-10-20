"""Entry point for the frozen build: `marsad.exe` is this file, bundled with Python.

Everything else is identical to `py -m marsad`. The station folder is
found the same way (station.json beside the exe, the current folder, or
--station), so the exe can live anywhere.
"""
import multiprocessing
import sys

from marsad.cli import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
