"""Compare two surveys the way this station's protocol says to.

Everything that has been done by hand for every A/B block so far, in one
command: the fingerprint check, the whole-band median, the floor block by
block, the pre-registered harmonic-family statistic with a real error bar, a
verdict against the measured detection floor, and any periodicity in either
run.

    py tools/compare_runs.py <before> <after>
    py tools/compare_runs.py cyc8-usb6-white casc2-jumper --periodicity
    py tools/compare_runs.py a b --band 1600 1700 --handling untouched

<before> and <after> may be a report directory, a bins.csv path, or just the
tag of a survey - the tag is matched against reports/ and the newest wins.

Stdlib only, like the rest of the analysis tools.
"""
import argparse
import csv
import json
import math
import statistics as st
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Measured on this station (CLAUDE.md, 2026-10-17). Two runs with the dongle
# untouched in between agree to hundredths of a dB; lift it in and out and the
# per-line scatter is 3.4 dB. Which floor applies is a fact about the
# EXPERIMENT, not about the data, so the caller has to say.
FLOORS = {"handled": 0.5, "untouched": 0.05}


# ---------------------------------------------------------------- loading

def resolve(token):
    """Accept a bins.csv, a report dir, or a survey tag."""
    p = Path(token)
    if p.is_file():
        return p.parent
    if p.is_dir():
        return p
    reports = ROOT / "reports"
    hits = sorted((d for d in reports.iterdir()
                   if d.is_dir() and token in d.name),
                  key=lambda d: d.name)
    if not hits:
        raise SystemExit("no report matches %r in %s" % (token, reports))
    if len(hits) > 1:
        print("note: %r matched %d reports, using %s"
              % (token, len(hits), hits[-1].name))
    return hits[-1]


def load_bins(report_dir):
    path = report_dir / "bins.csv"
    freqs, med, floor = [], [], []
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            try:
                freqs.append(float(r["freq_mhz"]))
                med.append(float(r["median_db"]))
                floor.append(float(r["floor_db"]))
            except (KeyError, ValueError):
                continue
    if not freqs:
        raise SystemExit("no usable rows in %s" % path)
    order = sorted(range(len(freqs)), key=lambda i: freqs[i])
    return ([freqs[i] for i in order],
            [med[i] for i in order],
            [floor[i] for i in order])


def load_sidecar(report_dir):
    """The sidecar sits in surveys/, named after the CSV, not the report."""
    cand = ROOT / "surveys" / (report_dir.name + ".json")
    if cand.exists():
        return json.loads(cand.read_text(encoding="utf-8"))
    return {}


def fingerprint(side):
    rp = side.get("rtl_power", {})
    if not rp:
        return None
    return ("%s-%s MHz | %s kHz | g%s | i%s | c%s | F%s | %s"
            % (rp.get("freq_low_mhz"), rp.get("freq_high_mhz"),
               rp.get("bin_khz"), rp.get("gain_db"), rp.get("integration_s"),
               rp.get("crop"), rp.get("fir"), side.get("receiver")))


# ---------------------------------------------------------------- statistic

def bin_width(freqs):
    """Median spacing between adjacent bins.

    NOT freqs[1] - freqs[0]: after --band or --exclude the list has gaps, and
    if one lands at the start that difference is the width of the GAP, not of a
    bin. It silently turned the harmonic tolerance into tens of MHz, so every
    harmonic matched something. Median spacing is immune to however many gaps
    the filter leaves.
    """
    if len(freqs) < 2:
        return 0.0
    return st.median(freqs[i + 1] - freqs[i] for i in range(len(freqs) - 1))


def family_levels(freqs, levels, fundamental):
    """Level of each harmonic above the whole-band median.

    A harmonic counts only if a bin lands within half a bin width of the exact
    multiple; otherwise we would be reading a neighbour and calling it a line.
    """
    band_median = st.median(levels)
    bw = bin_width(freqs)
    tol = bw / 2.0
    out = {}
    for n in range(max(1, int(freqs[0] / fundamental) + 1),
                   int(freqs[-1] / fundamental) + 1):
        target = n * fundamental
        i = min(range(len(freqs)), key=lambda j: abs(freqs[j] - target))
        if abs(freqs[i] - target) <= tol:
            out[n] = levels[i] - band_median
    return out, band_median, bw


def paired(a, b):
    """Paired difference over harmonics present in both runs.

    Pairing is the whole point: line-to-line spread is ~3.7 dB and essentially
    identical in both runs, so differencing per line cancels it and leaves only
    what changed between them.
    """
    shared = sorted(set(a) & set(b))
    diffs = [b[k] - a[k] for k in shared]
    n = len(diffs)
    if n < 3:
        return None
    mean = st.mean(diffs)
    sd = st.stdev(diffs)
    return {"shared": shared, "diffs": diffs, "n": n,
            "mean": mean, "sd": sd, "se": sd / math.sqrt(n)}


# ---------------------------------------------------------------- periodicity

def rolling_median(vals, win):
    """Detrend against a local median, so a broad pedestal is removed but a
    comb riding on it survives."""
    half = max(1, win // 2)
    out = []
    for i in range(len(vals)):
        lo, hi = max(0, i - half), min(len(vals), i + half + 1)
        out.append(vals[i] - st.median(vals[lo:hi]))
    return out


def autocorr_peaks(freqs, levels, win_mhz, max_lag_mhz, hop_mhz, top=5):
    bw = bin_width(freqs)
    x = rolling_median(levels, int(win_mhz / bw))
    mu = st.mean(x)
    x = [v - mu for v in x]
    denom = sum(v * v for v in x)
    if denom <= 0:
        return []
    max_lag = min(len(x) - 2, int(max_lag_mhz / bw))
    r = [sum(x[i] * x[i + k] for i in range(len(x) - k)) / denom
         for k in range(1, max_lag + 1)]
    # local maxima only - a broad shoulder is not a period
    peaks = [(r[k], (k + 1) * bw) for k in range(1, len(r) - 1)
             if r[k] > r[k - 1] and r[k] > r[k + 1]]
    peaks.sort(reverse=True)
    out = []
    for val, lag in peaks[:top]:
        flag = ""
        if hop_mhz:
            ratio = lag / hop_mhz
            # loose tolerance: the lag is quantised to whole bins, which is
            # already ~1% of the lag at two hop widths
            if abs(ratio - round(ratio)) < 0.04 and round(ratio) >= 1:
                flag = "  <- %dx hop width, instrument not signal" % round(ratio)
        out.append((val, lag, flag))
    return out


# ---------------------------------------------------------------- report

def main():
    ap = argparse.ArgumentParser(description="Compare two surveys end to end")
    ap.add_argument("before")
    ap.add_argument("after")
    ap.add_argument("--family", type=float, default=14.4,
                    help="harmonic fundamental in MHz (default 14.4, the real "
                         "RTL2832U clock fundamental)")
    ap.add_argument("--block", type=float, default=100.0,
                    help="floor table block width in MHz (default 100)")
    ap.add_argument("--band", nargs=2, type=float, metavar=("LO", "HI"),
                    help="restrict every statistic to this band")
    ap.add_argument("--exclude", nargs=2, type=float, metavar=("LO", "HI"),
                    help="drop this band from every statistic")
    ap.add_argument("--handling", choices=sorted(FLOORS), default="handled",
                    help="was hardware touched between the runs? sets the "
                         "detection floor (default handled = 0.5 dB)")
    ap.add_argument("--periodicity", action="store_true",
                    help="also autocorrelate both runs for combs")
    ap.add_argument("--verbose", action="store_true",
                    help="list every harmonic's change")
    args = ap.parse_args()

    dirs = [resolve(args.before), resolve(args.after)]
    names = [d.name for d in dirs]
    sides = [load_sidecar(d) for d in dirs]

    print("=" * 72)
    print("BEFORE  %s" % names[0])
    print("AFTER   %s" % names[1])
    print("=" * 72)

    fps = [fingerprint(s) for s in sides]
    if all(fps):
        if fps[0] == fps[1]:
            print("\nfingerprint  %s  (match)" % fps[0])
        else:
            print("\n*** FINGERPRINTS DIFFER - these runs are NOT comparable ***")
            print("  before  %s" % fps[0])
            print("  after   %s" % fps[1])
    for lbl, s in zip(("before", "after "), sides):
        chain = s.get("chain", {}).get("chain_order")
        if chain:
            print("\n%s chain  %s" % (lbl, chain))
    for lbl, s in zip(("before", "after "), sides):
        if s.get("conditions"):
            print("%s cond   %s" % (lbl, s["conditions"]))

    data = [load_bins(d) for d in dirs]
    if args.band or args.exclude:
        kept = []
        for freqs, med, floor in data:
            idx = list(range(len(freqs)))
            if args.band:
                lo, hi = args.band
                idx = [i for i in idx if lo <= freqs[i] <= hi]
            if args.exclude:
                lo, hi = args.exclude
                idx = [i for i in idx if not (lo <= freqs[i] <= hi)]
            kept.append(([freqs[i] for i in idx], [med[i] for i in idx],
                         [floor[i] for i in idx]))
        data = kept
        where = []
        if args.band:
            where.append("restricted to %.0f-%.0f MHz" % tuple(args.band))
        if args.exclude:
            where.append("excluding %.0f-%.0f MHz" % tuple(args.exclude))
        print("\n" + "; ".join(where))

    # ---- whole-band median: the instrument that repeats to 0.05 dB
    meds = [st.median(d[1]) for d in data]
    print("\nWHOLE-BAND MEDIAN   %+.3f -> %+.3f dB   change %+.3f dB"
          % (meds[0], meds[1], meds[1] - meds[0]))

    # ---- floor block by block
    print("\nFLOOR BY %.0f MHz BLOCK   (median of median_db)" % args.block)
    print("  %-16s%10s%10s%10s" % ("band", "before", "after", "change"))
    lo_all = max(data[0][0][0], data[1][0][0])
    hi_all = min(data[0][0][-1], data[1][0][-1])
    edge = lo_all
    biggest = []
    while edge < hi_all:
        top = min(edge + args.block, hi_all)
        vals, counts = [], []
        for freqs, med, _ in data:
            sel = [med[i] for i in range(len(freqs)) if edge <= freqs[i] < top]
            counts.append(len(sel))
            vals.append(st.median(sel) if sel else None)
        # a filter boundary can leave a block holding a handful of bins; its
        # "median" is then one reading, not a floor
        thin = any(c < 20 for c in counts)
        if all(v is not None for v in vals) and not thin:
            d = vals[1] - vals[0]
            biggest.append((abs(d), edge, top, d))
            mark = "  *" if abs(d) >= FLOORS[args.handling] else ""
            print("  %6.0f-%-9.0f%10.2f%10.2f%+10.2f%s"
                  % (edge, top, vals[0], vals[1], d, mark))
        edge = top
    print("  (* = at or beyond the %s dB detection floor for '%s' experiments)"
          % (FLOORS[args.handling], args.handling))

    # ---- the pre-registered family statistic
    tabs = []
    for freqs, med, _ in data:
        t, _bm, _bw = family_levels(freqs, med, args.family)
        tabs.append(t)
    print("\n%g MHz FAMILY   (level above each run's own band median)"
          % args.family)
    for nm, t in zip(names, tabs):
        vals = list(t.values())
        if vals:
            sd = st.stdev(vals) if len(vals) > 1 else 0.0
            print("  %-48s%+6.2f dB  n=%d  sd=%.2f"
                  % (nm[:46], st.mean(vals), len(vals), sd))

    pr = paired(tabs[0], tabs[1])
    if pr is None:
        print("  fewer than 3 shared harmonics - no paired statistic")
    else:
        print("\n  PAIRED DIFFERENCE   n = %d harmonics" % pr["n"])
        print("    mean change    %+.2f dB" % pr["mean"])
        print("    std / SE       %.2f / %.2f dB" % (pr["sd"], pr["se"]))
        print("    95%% interval   %+.2f to %+.2f dB"
              % (pr["mean"] - 1.96 * pr["se"], pr["mean"] + 1.96 * pr["se"]))
        sigma = abs(pr["mean"]) / pr["se"] if pr["se"] else float("inf")
        if abs(pr["mean"]) <= 2 * pr["se"]:
            verdict = ("NOT SIGNIFICANT (%.1f sigma) - consistent with no change"
                       % sigma)
        elif abs(pr["mean"]) < FLOORS[args.handling]:
            verdict = ("significant at %.1f sigma but SMALLER THAN THE %s dB "
                       "floor for a '%s' experiment - do not believe it from "
                       "one cycle"
                       % (sigma, FLOORS[args.handling], args.handling))
        else:
            verdict = "SIGNIFICANT: %+.2f dB at %.1f sigma" % (pr["mean"], sigma)
        print("    verdict        %s" % verdict)
        if args.verbose:
            for k in pr["shared"]:
                print("      x%-4d %10.4f MHz   %+6.2f dB"
                      % (k, k * args.family, tabs[1][k] - tabs[0][k]))

    # ---- worst block, named
    if biggest:
        biggest.sort(reverse=True)
        _, lo, hi, d = biggest[0]
        print("\nLARGEST BLOCK CHANGE   %.0f-%.0f MHz   %+.2f dB" % (lo, hi, d))

    # ---- periodicity
    if args.periodicity:
        print("\nPERIODICITY   (autocorrelation of the detrended median spectrum)")
        for nm, (freqs, med, _), side in zip(names, data, sides):
            hop = side.get("geometry", {}).get("hop_mhz")
            print("  %s" % nm)
            peaks = autocorr_peaks(freqs, med, win_mhz=2.0, max_lag_mhz=5.0,
                                   hop_mhz=hop)
            if not peaks:
                print("    nothing")
            for val, lag, flag in peaks:
                print("    r=%+.3f  spacing %9.1f kHz%s" % (val, lag * 1000, flag))


if __name__ == "__main__":
    main()
