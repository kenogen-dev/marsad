"""Thermal / time drift of a survey, from the per-sweep whole-band median.

You cannot measure the enclosure's temperature with an SDR, and you do not need
to. What matters is whether the receiver's OUTPUT moves as it warms, and a
thermal gain shift moves the whole floor together - which is exactly what a
median across every bin in one sweep detects. This station's whole-band median
repeats to 0.03-0.05 dB, so it is the most sensitive instrument available here.

    py tools/drift.py <tag-or-csv-or-report>
    py tools/drift.py xtr-alu-baseline --threshold 0.1 --plot-text

IMPORTANT: this reads the RAW CSV and never skips warm-up sweeps. `--skip-warmup`
exists to keep drift OUT of a comparison; this tool exists to look straight at it,
so the two must not be confused.
"""
import argparse
import math
import statistics as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import numpy as np                      # noqa: E402
from survey import load_rtl_power       # noqa: E402  (reuses the tested sweep grouping)


def resolve(token):
    """A raw CSV path, a report dir, or a survey tag."""
    p = Path(token)
    if p.is_file() and p.suffix.lower() == ".csv":
        return p
    name = p.name if (p.is_dir() or p.suffix) else token
    hits = sorted((f for f in (ROOT / "surveys").glob("*.csv") if token in f.name or name in f.name),
                  key=lambda f: f.name)
    if not hits:
        raise SystemExit(f"no raw CSV in surveys/ matches {token!r}")
    if len(hits) > 1:
        print(f"note: {token!r} matched {len(hits)} surveys, using {hits[-1].name}")
    return hits[-1]


def settle_index(series, budget):
    """Earliest sweep from which the whole remainder stays inside `budget`.

    Defined as the pass criterion itself rather than as distance from the final
    value: a single late excursion should not be allowed to claim the run never
    settled, which is exactly what a "within tol of the end" rule does on any
    series with real wander in it. Suffix min/max keeps it linear.
    """
    n = len(series)
    suf_hi = [0.0] * n
    suf_lo = [0.0] * n
    suf_hi[-1] = suf_lo[-1] = series[-1]
    for i in range(n - 2, -1, -1):
        suf_hi[i] = max(series[i], suf_hi[i + 1])
        suf_lo[i] = min(series[i], suf_lo[i + 1])
    for i in range(n - 1):
        if suf_hi[i] - suf_lo[i] <= budget:
            return i
    return n - 2


def main():
    ap = argparse.ArgumentParser(description="Per-sweep drift of a survey")
    ap.add_argument("survey", help="tag, raw CSV path, or report dir name")
    ap.add_argument("--threshold", type=float, default=0.1,
                    help="pass if total drift after settling stays within this (dB, default 0.1)")
    ap.add_argument("--plot-text", action="store_true", help="draw the series as text")
    args = ap.parse_args()

    path = resolve(args.survey)
    s = load_rtl_power(path)
    with np.errstate(all="ignore"):
        med = np.nanmedian(s.power_db, axis=1)
    med = [float(v) for v in med if math.isfinite(float(v))]
    n = len(med)
    if n < 3:
        raise SystemExit(f"only {n} usable sweeps - not enough to see drift")

    k = settle_index(med, args.threshold)
    after = med[k:] if k < n - 1 else med[-2:]
    minutes = None
    if len(s.times) >= 2:
        minutes = (s.times[-1] - s.times[0]).total_seconds() / 60.0

    print("=" * 66)
    print(f"DRIFT  {path.name}")
    print("=" * 66)
    print(f"  sweeps                {n}" + (f"   over {minutes:.0f} min" if minutes else ""))
    print(f"  first sweep           {med[0]:+.3f} dB")
    print(f"  last sweep            {med[-1]:+.3f} dB")
    print(f"  total change          {med[-1] - med[0]:+.3f} dB  (includes warm-up)")
    print()
    print(f"  settled by sweep      {k + 1} of {n}"
          + (f"   (~{minutes * k / n:.0f} min in)" if minutes else ""))
    print(f"  after settling:")
    print(f"    mean                {st.mean(after):+.3f} dB")
    print(f"    std                 {st.stdev(after) if len(after) > 1 else 0.0:.3f} dB")
    print(f"    range               {max(after) - min(after):.3f} dB")
    print(f"    net drift           {after[-1] - after[0]:+.3f} dB")

    worst = max(abs(after[-1] - after[0]), max(after) - min(after))
    print()
    if worst <= args.threshold:
        print(f"  VERDICT: PASS - settled behaviour within {args.threshold} dB "
              f"(worst {worst:.3f}). Thermal path is adequate.")
    else:
        print(f"  VERDICT: FAIL - {worst:.3f} dB after settling, over the {args.threshold} dB "
              f"budget.")
        if abs(after[-1] - after[0]) > 0.7 * worst:
            print("           It is still trending at the end of the run, which is what an "
                  "inadequate\n           heat path looks like - the part never reaches "
                  "equilibrium.")
        else:
            print("           It is wandering rather than trending, so this is more likely the "
                  "room\n           or the PC than the enclosure. Compare against a terminated "
                  "run elsewhere.")

    if args.plot_text:
        lo, hi = min(med), max(med)
        span = (hi - lo) or 1.0
        print(f"\n  {lo:+.2f} dB {'.' * 46} {hi:+.2f} dB")
        for i, v in enumerate(med):
            col = int(round(46 * (v - lo) / span))
            mark = "|" if i == k else " "
            print(f"  {i + 1:>4}{mark}{' ' * col}*")


if __name__ == "__main__":
    main()
