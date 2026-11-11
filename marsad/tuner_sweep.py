"""`marsad tuner-sweep`: where this radio's tuner locks, measured (relay 024).

The tuner's synthesiser either locks on a frequency or it does not, and the driver says so: librtlsdr prints
"PLL not locked" (on its error output; the tuning call itself still returns success) when it does not. So the sweep
asks `rtl_sdr` to tune and read a few thousand samples at one frequency at a time, and reads what the driver said:

  lock      tuned, nothing said
  unlock    "PLL not locked", or the driver refused the frequency
  direct    the driver switched to HF direct sampling instead of using the tuner (the RTL-SDR Blog driver does this
            below about 24 MHz on an R820T): not a tuner frequency at all
  error     the radio could not be opened (unplugged, or another program has it)

A coarse pass across the range (from half the expected low edge to a quarter above the expected high edge) finds
where locking starts and stops; each change between neighbouring points is then narrowed by halving the interval,
to `--precision` MHz. A blind spot narrower than the coarse step can be missed, and the output says how coarse the
pass was. `--short` is the coarse pass alone.

Nothing needs to be connected: lock is internal to the tuner. The radio is found by its serial (rule 8) and the
command refuses while another program holds it (rule 7). It writes nothing: it prints its result, and the setup
guide keeps it with the radio's profile. Scans, analysis, fingerprints and every other command are unchanged.

    marsad tuner-sweep --receiver <name> [--short] [--step MHz] [--precision MHz] [--json FILE]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time

from .config import Station

LOCK, UNLOCK, DIRECT, ERROR = "lock", "unlock", "direct", "error"


def classify(stderr: str, returncode: int | None = 0) -> str:
    """What the driver said about one tuning, from rtl_sdr's error output.

    Only what follows "Sampling at" counts: setting the sample rate on an R820T first tunes to 0 Hz, which makes the
    driver announce HF direct sampling before any real tuning (harmless, as CLAUDE.md records for rtl_test). Direct
    sampling counts only when the last word about it is "enabled"."""
    t = (stderr or "").lower()
    if "no supported devices" in t or "failed to open" in t or "usb_claim_interface error" in t or \
            "no matching devices" in t:
        return ERROR
    k = t.rfind("sampling at")
    tune = t[k:] if k >= 0 else t
    on, off = tune.rfind("enabled direct sampling"), tune.rfind("disabled direct sampling")
    if on >= 0 and on > off:
        return DIRECT
    if "pll not locked" in tune or "failed to set center freq" in tune:
        return UNLOCK
    return LOCK


def rtl_sdr_probe(exe: str, device: str, timeout: float = 10.0):
    """A probe(hz) -> state that runs rtl_sdr once per frequency on one radio (by serial)."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    def probe(hz: float) -> str:
        try:
            r = subprocess.run([exe, "-d", str(device), "-f", str(int(round(hz))), "-s", "1024000", "-n", "16384", "-"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=timeout,
                               creationflags=flags)
        except subprocess.TimeoutExpired:
            return ERROR
        return classify(r.stderr.decode("utf-8", "replace"), r.returncode)
    return probe


def sweep(probe, lo_mhz: float, hi_mhz: float, step_mhz: float, precision_mhz: float | None) -> dict:
    """Coarse pass, then each lock/unlock change narrowed by halving (precision None: coarse only).

    Returns {"segments": [[a, b], ...] MHz where it locks, "low", "high", "gaps", "direct_below", "tunes",
    "error"}. Edges are the midpoint of the last interval, so they are good to half the precision."""
    tunes = [0]
    seen: dict = {}

    def at(f):
        f = round(f, 4)
        if f not in seen:
            seen[f] = probe(f * 1e6)
            tunes[0] += 1
        return seen[f]

    n = int((hi_mhz - lo_mhz) / step_mhz) + 1
    pts = [round(lo_mhz + i * step_mhz, 4) for i in range(n)]
    if pts[-1] < hi_mhz:
        pts.append(round(hi_mhz, 4))
    states = []
    for f in pts:
        s = at(f)
        if s == ERROR:
            return {"error": f"the radio did not answer at {f:g} MHz (unplugged, or another program has it)",
                    "tunes": tunes[0]}
        states.append(s)
    ok = [s == LOCK for s in states]
    edges = []                                   # (MHz, rising: True where locking starts)
    for i in range(1, len(pts)):
        if ok[i] == ok[i - 1]:
            continue
        a, b = pts[i - 1], pts[i]
        if precision_mhz:
            want = ok[i - 1]
            while b - a > precision_mhz:
                m = round((a + b) / 2, 4)
                s = at(m)
                if s == ERROR:
                    return {"error": f"the radio did not answer at {m:g} MHz", "tunes": tunes[0]}
                if (s == LOCK) == want:
                    a = m
                else:
                    b = m
        edges.append(((a + b) / 2, ok[i]))
    segments = []
    start = pts[0] if ok[0] else None
    for f, rising in edges:
        if rising:
            start = f
        elif start is not None:
            segments.append([round(start, 2), round(f, 2)])
            start = None
    if start is not None:
        segments.append([round(start, 2), round(pts[-1], 2)])
    direct = [f for f, s in seen.items() if s == DIRECT]
    out = {"segments": segments, "tunes": tunes[0], "error": None,
           "direct_below": round(max(direct), 2) if direct else None}
    if segments:
        out["low"], out["high"] = segments[0][0], segments[-1][1]
        out["gaps"] = [[segments[i][1], segments[i + 1][0]] for i in range(len(segments) - 1)]
        out["edge_at_start"] = ok[0]            # locked at the very first point: the low edge was not reached
        out["edge_at_end"] = ok[-1]             # locked at the last point: the high edge was not reached
    return out


def expected(station: Station, rx: str) -> tuple:
    r = station.receivers.get(rx) or {}
    lo, hi = (r.get("range_mhz") or [24, 1766])[:2]
    return float(lo), float(hi)


def report_lines(res: dict, step: float, precision: float | None) -> list:
    if res.get("error"):
        return [f"tuner-sweep: FAILED: {res['error']}"]
    if not res.get("segments"):
        return ["tuner-sweep: FAILED: the tuner locked nowhere in the sweep (is this an RTL-SDR?)"]
    lines = [f"tuner-sweep: range {res['low']:.1f} to {res['high']:.1f} MHz"]
    lines += [f"tuner-sweep: gap {a:.1f} to {b:.1f} MHz" for a, b in res["gaps"]] or ["tuner-sweep: no gaps"]
    if res.get("direct_below") is not None:
        lines.append(f"tuner-sweep: at {res['direct_below']:.1f} MHz and below, the driver switched to HF direct "
                     "sampling: the low edge is that switch, not a lock limit")
    if res.get("edge_at_start"):
        lines.append("tuner-sweep: it locked at the sweep's lowest point: the true low edge is lower")
    if res.get("edge_at_end"):
        lines.append("tuner-sweep: it locked at the sweep's highest point: the true high edge is higher")
    lines.append(f"tuner-sweep: {'edges to ' + format(precision, 'g') + ' MHz' if precision else 'coarse pass only'}"
                 f", {step:g} MHz between coarse points (a blind spot narrower than that can be missed), "
                 f"{res['tunes']} tunings")
    return lines


def main(argv: list[str] | None = None, station: Station | None = None, prog: str = "marsad") -> int:
    ap = argparse.ArgumentParser(prog=f"{prog} tuner-sweep", description=__doc__.split("\n\n")[0])
    ap.add_argument("--receiver", required=True, help="the radio's name in station.json (found by its serial)")
    ap.add_argument("--short", action="store_true", help="the coarse pass only (a quick look first)")
    ap.add_argument("--step", type=float, help="MHz between coarse points (default 20, or 100 with --short)")
    ap.add_argument("--precision", type=float, default=0.5, help="narrow each edge to this many MHz (default 0.5)")
    ap.add_argument("--json", help="also write the result to this file")
    a = ap.parse_args(argv)
    station = station or Station.load()
    if a.receiver not in station.receivers:
        print(f"  No radio called {a.receiver!r} in this station.", file=sys.stderr)
        return 2
    from . import capture as cap, radio_use, watch
    from .rtlpower import find_tool
    use = radio_use.check(station, a.receiver)
    if not use["ok"]:
        print(f"  {use['text']} Nothing was tuned.", file=sys.stderr)
        return 3
    try:
        r = watch.resolve_receiver(station, a.receiver, cap.settings(station))
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 3
    exe = find_tool("rtl_sdr", station.tool_dirs())
    if exe is None:
        print("  rtl_sdr was not found in rtl-sdr/ or on PATH.", file=sys.stderr)
        return 2
    lo, hi = expected(station, a.receiver)
    step = a.step or (100.0 if a.short else 20.0)
    precision = None if a.short else a.precision
    start, end = max(1.0, round(lo * 0.5)), round(hi * 1.25)
    print(f"tuner-sweep: {a.receiver} (serial {r['serial']}, {r.get('tuner') or 'tuner not said'}), "
          f"{start:g} to {end:g} MHz, expected about {lo:g} to {hi:g}", flush=True)
    t0 = time.monotonic()
    res = sweep(rtl_sdr_probe(str(exe), r["serial"]), start, end, step, precision)
    res.update(receiver=a.receiver, serial=r["serial"], swept=[start, end], step_mhz=step, precision_mhz=precision,
               seconds=round(time.monotonic() - t0, 1))
    for line in report_lines(res, step, precision):
        print(line, flush=True)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(res, fh, indent=1)
    return 1 if res.get("error") or not res.get("segments") else 0
