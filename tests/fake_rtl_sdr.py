"""Stands in for rtl_sdr in the recorder tests: replays a file of uint8 IQ to stdout.

    python fake_rtl_sdr.py --replay FILE [--offset BYTES] [--argv-log LOG] [--loops N] -- <rtl_sdr args>

The rtl_sdr arguments after `--` are appended to --argv-log (one JSON list per
launch) so a test can check what the recorder asked the radio for. No dongle.
"""
import argparse
import json
import sys

ap = argparse.ArgumentParser()
ap.add_argument("--replay", required=True)
ap.add_argument("--offset", type=int, default=0)
ap.add_argument("--argv-log")
ap.add_argument("--loops", type=int, default=1)
ap.add_argument("rest", nargs=argparse.REMAINDER)
a = ap.parse_args()
if a.argv_log:
    with open(a.argv_log, "a") as f:
        f.write(json.dumps([x for x in a.rest if x != "--"]) + "\n")
out = sys.stdout.buffer
try:
    for _ in range(a.loops):
        with open(a.replay, "rb") as f:
            f.seek(a.offset)
            while True:
                b = f.read(1 << 20)
                if not b:
                    break
                out.write(b)
    out.flush()
except (BrokenPipeError, OSError):
    pass
