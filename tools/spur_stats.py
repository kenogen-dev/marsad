"""Pre-registered statistic for comparing station configurations.

The level of any single spur line is noisy: measured run-to-run std is 1.36 dB,
so nothing under ~2.7 dB is real for one line. Averaged across a whole harmonic
family the standard error falls to ~0.24 dB, which is what makes sub-dB changes
detectable at all. This tool computes that family statistic, and for two runs
the PAIRED difference - pairing removes the large line-to-line variation and
leaves only what changed between runs.

    py tools/spur_stats.py reports/<run>/bins.csv
    py tools/spur_stats.py reports/<before>/bins.csv reports/<after>/bins.csv
    py tools/spur_stats.py a/bins.csv b/bins.csv --family 24 --verbose
"""
import argparse
import csv
import math
import statistics as st
from pathlib import Path


def load_bins(path):
    """Return (freqs_mhz, median_db) sorted by frequency."""
    rows = []
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            try:
                rows.append((float(r["freq_mhz"]), float(r["median_db"])))
            except (KeyError, ValueError):
                continue
    if not rows:
        raise SystemExit(f"no usable rows in {path}")
    rows.sort()
    return rows


def family_levels(rows, fundamental_mhz):
    """Level of each harmonic above the whole-band median.

    A harmonic only counts if a bin lands within half a bin width of the exact
    multiple - otherwise we would be reading a neighbouring bin and calling it
    a line. Returns {harmonic_number: level_above_median_db}.
    """
    freqs = [f for f, _ in rows]
    levels = [v for _, v in rows]
    band_median = st.median(levels)
    bw = (freqs[1] - freqs[0]) if len(freqs) > 1 else 0.0
    tol = bw / 2.0

    out = {}
    lo, hi = freqs[0], freqs[-1]
    n_start = int(lo / fundamental_mhz) + 1
    n_end = int(hi / fundamental_mhz)
    for n in range(max(1, n_start), n_end + 1):
        target = n * fundamental_mhz
        # nearest bin by binary-search-free scan over a narrow window
        best_i, best_d = None, None
        for i, f in enumerate(freqs):
            d = abs(f - target)
            if best_d is None or d < best_d:
                best_i, best_d = i, d
            elif f > target + tol:
                break
        if best_i is not None and best_d <= tol:
            out[n] = levels[best_i] - band_median
    return out, band_median, bw


def describe(name, levels, band_median, bw, verbose=False):
    vals = list(levels.values())
    n = len(vals)
    mean = st.mean(vals)
    sd = st.stdev(vals) if n > 1 else 0.0
    se = sd / math.sqrt(n) if n else 0.0
    print(f"\n{name}")
    print(f"  bin width          {bw * 1000:.2f} kHz")
    print(f"  whole-band median  {band_median:+.3f} dB")
    print(f"  harmonics found    {n}")
    print(f"  family mean        {mean:+.2f} dB above band median")
    print(f"  std / SE           {sd:.2f} / {se:.2f} dB")
    if verbose:
        for k in sorted(levels):
            print(f"      x{k:<4d} {k * FUND:10.4f} MHz   {levels[k]:+6.2f} dB")
    return mean, sd, se, n


def main():
    ap = argparse.ArgumentParser(description="Harmonic-family spur statistic")
    ap.add_argument("bins", nargs="+", help="one or two bins.csv paths")
    ap.add_argument("--family", type=float, default=28.8,
                    help="fundamental in MHz (default 28.8, the RTL2832U clock)")
    ap.add_argument("--verbose", action="store_true", help="list every harmonic")
    args = ap.parse_args()

    global FUND
    FUND = args.family

    if len(args.bins) > 2:
        raise SystemExit("one or two files, not more")

    stats = []
    tables = []
    for p in args.bins:
        rows = load_bins(p)
        levels, band_median, bw = family_levels(rows, args.family)
        if not levels:
            raise SystemExit(f"no {args.family} MHz harmonics inside the range of {p}")
        tables.append(levels)
        stats.append(describe(Path(p).parent.name or p, levels, band_median, bw, args.verbose))

    if len(tables) != 2:
        return

    # Paired difference over harmonics present in BOTH runs. Pairing cancels the
    # line-to-line spread, which is large and identical in both, leaving only the
    # run-to-run change.
    shared = sorted(set(tables[0]) & set(tables[1]))
    if len(shared) < 3:
        raise SystemExit("fewer than 3 harmonics common to both runs - not comparable")
    diffs = [tables[1][k] - tables[0][k] for k in shared]
    n = len(diffs)
    mean = st.mean(diffs)
    sd = st.stdev(diffs)
    se = sd / math.sqrt(n)

    print("\n" + "=" * 58)
    print(f"PAIRED DIFFERENCE  (second minus first), n = {n} harmonics")
    print(f"  mean change      {mean:+.2f} dB")
    print(f"  std / SE         {sd:.2f} / {se:.2f} dB")
    print(f"  95% interval     {mean - 1.96 * se:+.2f} to {mean + 1.96 * se:+.2f} dB")

    band_shift = stats[1][0] - stats[0][0]
    if abs(mean) > 2 * se:
        call = f"SIGNIFICANT: the family moved by {mean:+.2f} dB"
    else:
        call = (f"NOT SIGNIFICANT: consistent with no change "
                f"(would need |{mean:+.2f}| > {2 * se:.2f})")
    print(f"  verdict          {call}")
    print(f"\n  (family-mean difference computed independently: {band_shift:+.2f} dB)")
    if args.verbose:
        print("\n  per-harmonic change:")
        for k in shared:
            print(f"      x{k:<4d} {k * FUND:10.4f} MHz   {tables[1][k] - tables[0][k]:+6.2f} dB")


if __name__ == "__main__":
    main()
