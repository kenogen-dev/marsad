"""`marsad tuner-sweep`: where this radio's tuner locks, measured (relay 024, corrected by relay 025).

The tuner's synthesiser either locks on a frequency or it does not, and the driver says so: librtlsdr prints
"PLL not locked" (on its error output; the tuning call itself still returns success) when it does not. So the sweep
asks `rtl_sdr` to tune and read a few thousand samples at one frequency at a time, and reads what the driver said:

  lock      tuned, nothing said against it
  unlock    "PLL not locked" / "No valid PLL values", or the driver refused the frequency
  direct    the driver is in HF direct sampling instead of using the tuner (the RTL-SDR Blog driver does this below
            about 24 MHz on an R820T): not a tuner frequency at all, and never counted as tuner range
  error     the radio could not be opened or stopped answering (unplugged, another program has it, the USB lead)

How the R820T driver really talks (relay 025, read from 1,600 real tunes of this kind of radio): setting the sample
rate first tunes to 0 Hz, which switches the driver into direct sampling ("Enabled direct sampling mode"); the real
tune then switches it back out, and on the way out the driver prints a "PLL not locked" of its own before "Disabled
direct sampling mode", on every tune, however good the frequency. So the lock answer is what the driver says after the
last switch out of direct sampling (or after "Sampling at" when there was none), and a radio still in direct sampling
at the end is "direct" whatever it said before.

A coarse pass across the range (from half the expected low edge to a quarter above the expected high edge) finds
where locking starts and stops; each change between neighbouring points is then narrowed by halving the interval,
to `--precision` MHz. A "not locked" next to a locked point is asked twice, so one bad first tune cannot move an edge.
If the sweep's last point still locks, it carries on upward until it stops locking (to 3000 MHz at most); if its
first point locks, it carries on downward (to 1 MHz). Where direct sampling lies below the range, the low edge is that
switch, not a lock limit, and the output says so. A blind spot narrower than the coarse step can be missed, and the
output says how coarse the pass was. `--short` is the coarse pass alone.

While it runs it prints one progress line per tuning (what it is doing, how many of how many) and what the driver said,
so the setup guide can show progress and the log shows the driver's own words. `--stop-file` cancels it cleanly
between tunings.

Nothing needs to be connected: lock is internal to the tuner. The radio is found by its serial (rule 8) and the
command refuses while another program holds it (rule 7). It writes nothing: it prints its result, and the setup
guide keeps it with the radio's profile. Scans, analysis, fingerprints and every other command are unchanged.

    marsad tuner-sweep --receiver <name> [--short] [--step MHz] [--precision MHz] [--json FILE] [--stop-file PATH]
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
from pathlib import Path

from .config import Station

LOCK, UNLOCK, DIRECT, ERROR = "lock", "unlock", "direct", "error"
TOP_LIMIT_MHZ = 3000.0              # rtl_sdr takes Hz as a 32-bit number; no RTL-SDR tuner locks near this
BOTTOM_LIMIT_MHZ = 1.0
EXIT_CANCELLED = 4

_OPEN_FAIL = ("no supported devices", "failed to open", "usb_claim_interface error", "no matching devices")
_USB_FAIL = ("i2c wr failed", "i2c rd failed", "r82xx_init: failed", "rtlsdr_write_reg failed",
             "rtlsdr_read_reg failed", "libusb_error", "device lost")


class Cancelled(Exception):
    pass


def classify(stderr: str, returncode: int | None = 0) -> str:
    """What the driver said about one tuning, from rtl_sdr's error output (see the module's notes on the R820T).

    1. Open failures and USB/I2C failures are "error": the answer says nothing about the tuner.
    2. "direct" when the last word about direct sampling, anywhere, is "Enabled": the radio ended in direct sampling.
    3. Otherwise the lock answer is in what follows the last "Disabled direct sampling mode" (the R820T's switch out
       of direct sampling, which prints a harmless "PLL not locked" just before it), or what follows "Sampling at"
       (the E4000's set-up complaints, if any, come before that line)."""
    t = (stderr or "").lower()
    if any(w in t for w in _OPEN_FAIL) or any(w in t for w in _USB_FAIL):
        return ERROR
    on, off = t.rfind("enabled direct sampling"), t.rfind("disabled direct sampling")
    if on >= 0 and on > off:
        return DIRECT
    after_off = -1
    if off >= 0:                                         # the end of the "Disabled direct sampling mode" line
        nl = t.find("\n", off)
        after_off = nl if nl >= 0 else len(t)
    k = max(t.rfind("sampling at"), after_off)
    tune = t[k:] if k >= 0 else t
    if "pll not locked" in tune or "no valid pll" in tune or "failed to set center freq" in tune:
        return UNLOCK
    return LOCK


def trouble(stderr: str) -> str | None:
    """Plain words for a failure the driver reported, or None."""
    t = (stderr or "").lower()
    if "usb_claim_interface error" in t:
        return "another program has the radio open"
    if "no supported devices" in t or "no matching devices" in t or "failed to open" in t:
        return "the radio was not found (unplugged, or another program has it)"
    if any(w in t for w in _USB_FAIL):
        return "the radio stopped answering over USB (the lead, a hub or the isolator)"
    return None


def driver_words(stderr: str, limit: int = 6) -> str:
    """The driver's lines from the tune on (what the log shows for each point), shortened."""
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    i = next((n for n, ln in enumerate(lines) if ln.lower().startswith("sampling at")), 0)
    keep = [ln for ln in lines[max(0, i - 1):] if not ln.lower().startswith(("reading samples", "found 1", "  "))]
    return " | ".join(keep[:limit])


def rtl_sdr_probe(exe: str, device: str, timeout: float = 10.0):
    """A probe(hz) -> (state, driver's words) that runs rtl_sdr once per frequency on one radio (by serial)."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    def probe(hz: float):
        try:
            r = subprocess.run([exe, "-d", str(device), "-f", str(int(round(hz))), "-s", "1024000", "-n", "16384", "-"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=timeout,
                               creationflags=flags)
        except subprocess.TimeoutExpired:
            return ERROR, f"rtl_sdr did not finish within {timeout:g} s"
        text = r.stderr.decode("utf-8", "replace")
        return classify(text, r.returncode), text
    return probe


def _answer(got):
    """A probe may answer a state, or (state, driver's text)."""
    if isinstance(got, tuple):
        return got[0], got[1] if len(got) > 1 else ""
    return got, ""


def sweep(probe, lo_mhz: float, hi_mhz: float, step_mhz: float, precision_mhz: float | None,
          say=None, stop=None, top_limit: float = TOP_LIMIT_MHZ, bottom_limit: float = BOTTOM_LIMIT_MHZ) -> dict:
    """Coarse pass, extended past either end while it still locks, then each change narrowed by halving (precision
    None: coarse only). `say(event)` gets progress events; `stop()` returning True cancels (raises Cancelled).

    Returns {"segments": [[a, b], ...] MHz where it locks, "low", "high", "gaps", "low_kind", "high_kind",
    "direct_below", "direct_above_range", "swept", "extended", "tunes", "error", "error_why", "points"}.
    Edges are the midpoint of the last interval, so they are good to half the precision."""
    tunes = [0]
    seen: dict = {}
    words: dict = {}
    plan = {"total": 0, "phase": "coarse pass"}

    def tell(kind, **kw):
        if say:
            say(dict(kw, kind=kind, done=tunes[0], total=max(plan["total"], tunes[0]), phase=plan["phase"]))

    def raw(f):
        if stop and stop():
            raise Cancelled()
        tell("tuning", mhz=f)
        s, text = _answer(probe(f * 1e6))
        tunes[0] += 1
        words[f] = text
        tell("result", mhz=f, state=s, words=driver_words(text))
        return s

    def at(f, confirm=False):
        f = round(f, 4)
        if f not in seen:
            seen[f] = raw(f)
            if confirm and seen[f] == UNLOCK:                 # one bad tune must not move an edge: ask again
                plan["total"] += 1
                if raw(f) == LOCK:
                    seen[f] = LOCK
        return seen[f]

    def fail(f, s):
        why = trouble(words.get(round(f, 4), "")) or ("rtl_sdr did not finish in time" if "did not finish" in
                                                      words.get(round(f, 4), "") else None)
        return {"error": f"the radio did not answer at {f:g} MHz" + (f": {why}" if why else
                                                                     " (unplugged, or another program has it)"),
                "error_why": why, "tunes": tunes[0], "points": _points(seen)}

    n = int(round((hi_mhz - lo_mhz) / step_mhz)) + 1
    pts = [round(lo_mhz + i * step_mhz, 4) for i in range(n) if lo_mhz + i * step_mhz <= hi_mhz + 1e-9]
    if pts[-1] < hi_mhz:
        pts.append(round(hi_mhz, 4))
    halvings = math.ceil(math.log2(step_mhz / precision_mhz)) if precision_mhz else 0
    plan["total"] = len(pts) + 2 * halvings                   # the typical radio: two edges to narrow
    states = []
    for f in pts:
        s = at(f)
        if s == ERROR:
            return fail(f, s)
        states.append(s)
    extended = []
    # the high edge must be found: carry on upward while the last point still locks
    while states[-1] == LOCK and pts[-1] < top_limit:
        plan["phase"] = "looking for the high edge above the sweep"
        f = round(min(top_limit, pts[-1] + step_mhz), 4)
        plan["total"] += 1
        s = at(f)
        if s == ERROR:
            return fail(f, s)
        pts.append(f)
        states.append(s)
        extended.append("up")
    # and the low edge: carry on downward while the first point still locks (direct sampling is not a lock)
    while states[0] == LOCK and pts[0] > bottom_limit:
        plan["phase"] = "looking for the low edge below the sweep"
        f = round(max(bottom_limit, pts[0] - step_mhz), 4)
        plan["total"] += 1
        s = at(f)
        if s == ERROR:
            return fail(f, s)
        pts.insert(0, f)
        states.insert(0, s)
        extended.append("down")
    ok = [s == LOCK for s in states]
    # a "not locked" beside a locked point is asked again before it counts
    plan["phase"] = "checking each edge again"
    for i, s in enumerate(states):
        if s == UNLOCK and ((i > 0 and ok[i - 1]) or (i + 1 < len(ok) and ok[i + 1])):
            plan["total"] += 1
            again = raw(pts[i])
            if again == ERROR:
                return fail(pts[i], again)
            if again == LOCK:
                seen[pts[i]] = states[i] = LOCK
                ok[i] = True
    changes = [i for i in range(1, len(pts)) if ok[i] != ok[i - 1]]
    plan["total"] = tunes[0] + len(changes) * halvings
    edges = []                                   # (MHz, rising, what lies on the not-locking side)
    for i in changes:
        a, b = pts[i - 1], pts[i]
        other = states[i - 1] if ok[i] else states[i]       # the state on the side that does not lock
        if precision_mhz:
            plan["phase"] = f"narrowing the edge near {(a + b) / 2:,.0f} MHz"
            want = ok[i - 1]
            while b - a > precision_mhz:
                m = round((a + b) / 2, 4)
                s = at(m, confirm=True)
                if s == ERROR:
                    return fail(m, s)
                if (s == LOCK) == want:
                    a = m
                else:
                    b = m
                if s != LOCK:
                    other = s
        edges.append(((a + b) / 2, ok[i], other))
    segments, kinds = [], []
    start = (pts[0], "sweep start") if ok[0] else None
    for f, rising, other in edges:
        if rising:
            start = (f, other)
        elif start is not None:
            segments.append([round(start[0], 2), round(f, 2)])
            kinds.append((start[1], other))
            start = None
    if start is not None:
        segments.append([round(start[0], 2), round(pts[-1], 2)])
        kinds.append((start[1], "sweep end"))
    direct = sorted(f for f, s in seen.items() if s == DIRECT)
    out = {"segments": segments, "tunes": tunes[0], "error": None, "points": _points(seen),
           "swept": [pts[0], pts[-1]], "extended": sorted(set(extended)),
           "direct_below": None, "direct_above_range": []}
    if segments:
        out["low"], out["high"] = segments[0][0], segments[-1][1]
        out["gaps"] = [[segments[i][1], segments[i + 1][0]] for i in range(len(segments) - 1)]
        out["low_kind"] = {DIRECT: "direct", UNLOCK: "unlock"}.get(kinds[0][0], kinds[0][0])
        out["high_kind"] = {DIRECT: "direct", UNLOCK: "unlock"}.get(kinds[-1][1], kinds[-1][1])
        out["edge_at_start"] = ok[0]            # still locked at the lowest point it could reach
        out["edge_at_end"] = ok[-1]             # still locked at the highest point it could reach
        if out["low_kind"] == "direct":
            out["direct_below"] = out["low"]
        out["direct_above_range"] = [f for f in direct if f > out["low"]]
    return out


def _points(seen: dict) -> list:
    return [[f, s] for f, s in sorted(seen.items())]


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
    if res.get("low_kind") == "direct":
        lines.append(f"tuner-sweep: low edge {res['low']:.1f} MHz: where the driver switches to HF direct sampling "
                     "(below it the radio does not use its tuner), not a lock limit")
    elif res.get("low_kind") == "unlock":
        lines.append(f"tuner-sweep: low edge {res['low']:.1f} MHz: the tuner stops locking below it")
    if res.get("high_kind") in ("unlock", "direct"):
        lines.append(f"tuner-sweep: high edge {res['high']:.1f} MHz: the tuner stops locking above it")
    lines += [f"tuner-sweep: gap {a:.1f} to {b:.1f} MHz" for a, b in res["gaps"]] or ["tuner-sweep: no gaps"]
    if res.get("direct_above_range"):
        lines.append("tuner-sweep: UNEXPECTED: the driver reported HF direct sampling at "
                     + ", ".join(f"{f:g}" for f in res["direct_above_range"][:5])
                     + " MHz, inside or above the tuner's range: the driver's words for those points are in this log")
    if res.get("edge_at_start"):
        lines.append(f"tuner-sweep: it locked at the lowest point it could reach ({res['swept'][0]:g} MHz): "
                     "the true low edge is lower")
    if res.get("edge_at_end"):
        lines.append(f"tuner-sweep: it locked at the highest point it could reach ({res['swept'][1]:g} MHz): "
                     "the true high edge is higher")
    if "up" in (res.get("extended") or []):
        lines.append(f"tuner-sweep: carried on above the planned top, to {res['swept'][1]:g} MHz, to find the high "
                     "edge")
    if "down" in (res.get("extended") or []):
        lines.append(f"tuner-sweep: carried on below the planned bottom, to {res['swept'][0]:g} MHz, to find the low "
                     "edge")
    lines.append(f"tuner-sweep: {'edges to ' + format(precision, 'g') + ' MHz' if precision else 'coarse pass only'}"
                 f", {step:g} MHz between coarse points (a blind spot narrower than that can be missed), "
                 f"{res['tunes']} tunings")
    return lines


STATE_WORD = {LOCK: "locked", UNLOCK: "not locked", DIRECT: "direct sampling (not the tuner)", ERROR: "no answer"}


def progress_line(ev: dict) -> str | None:
    """One line per event, read back by the setup guide: `tuner-sweep: progress D of T: <activity>`."""
    if ev["kind"] == "tuning":
        return (f"tuner-sweep: progress {ev['done']} of {ev['total']}: tuning {ev['mhz']:,.1f} MHz, "
                f"{ev['phase']}")
    if ev["kind"] == "result":
        return (f"tuner-sweep: {ev['mhz']:,.1f} MHz {STATE_WORD.get(ev['state'], ev['state'])}"
                + (f"  [driver: {ev['words']}]" if ev.get("words") else ""))
    return None


def main(argv: list[str] | None = None, station: Station | None = None, prog: str = "marsad") -> int:
    ap = argparse.ArgumentParser(prog=f"{prog} tuner-sweep", description=__doc__.split("\n\n")[0])
    ap.add_argument("--receiver", required=True, help="the radio's name in station.json (found by its serial)")
    ap.add_argument("--short", action="store_true", help="the coarse pass only (a quick look first)")
    ap.add_argument("--step", type=float, help="MHz between coarse points (default 20, or 100 with --short)")
    ap.add_argument("--precision", type=float, default=0.5, help="narrow each edge to this many MHz (default 0.5)")
    ap.add_argument("--json", help="also write the result to this file")
    ap.add_argument("--stop-file", type=Path, help="stop cleanly between tunings once this file exists")
    a = ap.parse_args(argv)
    station = station or Station.load()
    if a.receiver not in station.receivers:
        print(f"  No radio called {a.receiver!r} in this station.", file=sys.stderr)
        return 2
    from . import capture as cap, radio_use, watch
    from .rtlpower import find_tool
    use = radio_use.check(station, a.receiver)
    if not use["ok"]:
        print(f"tuner-sweep: REFUSED: {use['text']} Nothing was tuned.", flush=True)
        print(f"  {use['text']} Nothing was tuned.", file=sys.stderr)
        return 3
    try:
        r = watch.resolve_receiver(station, a.receiver, cap.settings(station))
    except SystemExit as exc:
        print(f"tuner-sweep: REFUSED: {str(exc).strip()}", flush=True)
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

    def say(ev):
        line = progress_line(ev)
        if line:
            print(line, flush=True)

    def stop():
        if a.stop_file and a.stop_file.exists():
            try:
                a.stop_file.unlink()
            except OSError:
                pass
            return True
        return False

    try:
        res = sweep(rtl_sdr_probe(str(exe), r["serial"]), start, end, step, precision, say=say, stop=stop)
    except Cancelled:
        print(f"tuner-sweep: CANCELLED after {time.monotonic() - t0:.0f} s: stopped between tunings, nothing kept",
              flush=True)
        return EXIT_CANCELLED
    res.update(receiver=a.receiver, serial=r["serial"], planned=[start, end], step_mhz=step,
               precision_mhz=precision, seconds=round(time.monotonic() - t0, 1))
    for line in report_lines(res, step, precision):
        print(line, flush=True)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(res, fh, indent=1)
    return 1 if res.get("error") or not res.get("segments") else 0
