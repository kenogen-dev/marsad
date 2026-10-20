"""Measure how long rtl_power spends retuning between hops on THIS machine.

The scan geometry assumes a fixed cost per hop (47 ms was measured on one
Windows PC in 2026). It depends on the USB stack, the driver and the dongle,
so a shipped tool should measure it rather than assume it.

Method: run rtl_power over a span of about 100 hops with an integration far
shorter than a sweep can take, so it simply runs as fast as it can. Every
sweep then lasts hops x retune, and the timestamps rtl_power writes give the
sweep period directly. Over a minute that resolves the per-hop cost to about
a millisecond.

    marsad calibrate-retune                 # measure and report
    marsad calibrate-retune --save          # also write retune_ms_per_hop to station.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import math
import statistics as st
import subprocess
import tempfile
from pathlib import Path

from .config import Station
from .rtlpower import RTL_MAX_SAMPLE_HZ, CROP, find_tool


def sweep_times(csv_path: Path) -> tuple[list[dt.datetime], int]:
    """Timestamps of each sweep's first row, and the hops per sweep.

    A new sweep starts when a hop's start frequency is not above the previous
    hop's, the same rule the analyser uses.
    """
    stamps: list[dt.datetime] = []
    prev_lo = -math.inf
    rows_this = 0
    hops = 0
    with open(csv_path, "r", errors="replace") as fh:
        for line in fh:
            parts = line.split(",")
            if len(parts) < 7:
                continue
            try:
                lo = float(parts[2])
                ts = dt.datetime.fromisoformat(parts[0].strip() + "T" + parts[1].strip())
            except ValueError:
                continue
            if lo <= prev_lo:
                hops = hops or rows_this
                rows_this = 0
                stamps.append(ts)
            elif not stamps:
                stamps.append(ts)
            prev_lo = lo
            rows_this += 1
    hops = hops or rows_this
    return stamps, hops


def retune_from_csv(csv_path: Path) -> dict:
    """Per-hop retune cost from a fast-as-possible rtl_power run."""
    stamps, hops = sweep_times(csv_path)
    if len(stamps) < 3 or not hops:
        raise SystemExit(f"only {len(stamps)} sweeps in {csv_path.name}; run it longer")
    total = (stamps[-1] - stamps[0]).total_seconds()
    sweeps = len(stamps) - 1
    per_sweep = total / sweeps
    gaps = [(b - a).total_seconds() for a, b in zip(stamps, stamps[1:])]
    return {
        "hops": hops,
        "sweeps": sweeps,
        "seconds": total,
        "sweep_s": per_sweep,
        "sweep_s_sd": st.stdev(gaps) if len(gaps) > 1 else 0.0,
        "retune_ms_per_hop": 1000.0 * per_sweep / hops,
        # one second of timestamp resolution spread over the whole run
        "uncertainty_ms": 1000.0 / sweeps / hops,
    }


def main(argv=None, station: Station | None = None, prog: str = "marsad calibrate-retune") -> int:
    station = station or Station.load()
    ap = argparse.ArgumentParser(prog=prog, description=__doc__.split("\n")[0])
    ap.add_argument("--seconds", type=int, default=90, help="how long to run (default 90)")
    ap.add_argument("--lo", type=float, default=400.0, help="start MHz (default 400)")
    ap.add_argument("--hops", type=int, default=100, help="how many hops to time (default 100)")
    ap.add_argument("--gain", default="29", help="tuner gain to use (any valid step)")
    ap.add_argument("--device", type=int, default=0)
    ap.add_argument("--save", action="store_true",
                    help="write the result into station.json as retune_ms_per_hop")
    ap.add_argument("--csv", help="skip the recording and read an existing fast-run CSV")
    args = ap.parse_args(argv)

    if args.csv:
        csv_path = Path(args.csv)
    else:
        rtl_power = find_tool("rtl_power", station.tool_dirs())
        if rtl_power is None:
            raise SystemExit("rtl_power not found (rtl-sdr/ folder or PATH)")
        hop_mhz = RTL_MAX_SAMPLE_HZ * (1 - CROP) / 1e6
        hi = args.lo + args.hops * hop_mhz
        csv_path = Path(tempfile.gettempdir()) / "sdr_retune_calibration.csv"
        cmd = [str(rtl_power), "-f", f"{args.lo:g}M:{hi:g}M:100k", "-g", str(args.gain),
               "-i", "1", "-c", str(CROP), "-F", "9", "-e", f"{args.seconds}s",
               "-d", str(args.device), str(csv_path)]
        print("  " + subprocess.list2cmdline(cmd))
        print(f"  {args.hops} hops, -i 1 so rtl_power runs flat out; {args.seconds} s ...")
        csv_path.unlink(missing_ok=True)
        rc = subprocess.run(cmd).returncode
        if not csv_path.exists() or csv_path.stat().st_size == 0:
            raise SystemExit(f"rtl_power wrote nothing (exit {rc}); is the receiver free?")

    r = retune_from_csv(csv_path)
    print()
    print(f"  hops per sweep        {r['hops']}")
    print(f"  sweeps timed          {r['sweeps']} over {r['seconds']:.0f} s")
    print(f"  sweep period          {r['sweep_s']:.3f} s  (sd {r['sweep_s_sd']:.3f})")
    print(f"  retune per hop        {r['retune_ms_per_hop']:.1f} ms  "
          f"(+/- {r['uncertainty_ms']:.1f} ms from timestamp resolution)")
    print(f"  station.json has      {station.retune_ms_per_hop:g} ms")
    if args.save:
        station.set_value("retune_ms_per_hop", round(r["retune_ms_per_hop"], 1))
        print(f"\n  saved retune_ms_per_hop = {round(r['retune_ms_per_hop'], 1)} to {station.file}")
    else:
        print("\n  (re-run with --save to write it into station.json)")
    return 0
