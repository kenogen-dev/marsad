"""Analyse rtl_power survey CSVs.

  marsad analyze  survey.csv [--threshold 10] [--out reports] [--mask 1095-1240]
  marsad compare  before.csv after.csv [--threshold 10] [--out reports]

What it produces (in <out>/<survey name>/):
  heatmap.png      waterfall: frequency across, time down, colour = power
  spectrum.png     max / median / floor / threshold across frequency, detected signals marked
  occupancy.png    % of sweeps each frequency was above the threshold
  combined.png     the three above stacked on one frequency axis
  peaks.csv        every detected signal with level, SNR, occupancy, class and spur tags
  bins.csv         per-bin floor, median, max and occupancy (for your own analysis)
  report.md        readable summary

Requires numpy, pandas, matplotlib (scipy optional, but it changes which peaks
are found, so a shipped build always includes it).

rtl_power CSV format, one line per hop per sweep:
  date, time, Hz low, Hz high, Hz step, samples, dB, dB, dB, ...

rtl_power_fftw output (surveys/*.dat, recorded by `marsad run --engine
rtl_power_fftw`): one block per hop, a `#` header with UTC start/end
timestamps, then `frequency_Hz power_dB` lines, blank line between hops, two
blank lines between passes. load_survey() tells the two apart by their first
line and the rest of the analysis is the same.

The receiver gaps and spur families come from station.json via `configure()`;
the module-level tables below are only the fallback for a bare import.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
import warnings
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from . import conditions as auto_cond
from .config import Station
from .rtlpower import build_token, program_line

# Filled in by configure(station). The defaults match station.default.json so
# a bare import behaves like the original tool.
RECEIVER_GAPS_MHZ: dict = {"xtr": [(1095.0, 1240.0)], "v5": []}
SPUR_FAMILIES_MHZ: list = [
    (28.8, "28.8 MHz SDR clock harmonic"),
    (0.6, "600 kHz comb (switching converter?)"),
    (480.0, "USB 2.0 (480 Mb/s) harmonic"),
    (24.0, "24 MHz crystal harmonic"),
    (25.0, "25 MHz clock harmonic"),
    (100.0, "100 MHz PCIe clock harmonic"),
]


def configure(station: Station) -> None:
    """Take the gap table and spur families from the station file."""
    global RECEIVER_GAPS_MHZ, SPUR_FAMILIES_MHZ
    RECEIVER_GAPS_MHZ = station.receiver_gaps()
    SPUR_FAMILIES_MHZ = station.spur_families


# ----------------------------------------------------------------------------
# Loading
# ----------------------------------------------------------------------------
@dataclass
class Survey:
    name: str
    freqs_hz: np.ndarray          # (F,) bin frequencies, ascending
    times: pd.DatetimeIndex       # (T,) sweep timestamps
    power_db: np.ndarray          # (T, F) dB, NaN where missing
    step_hz: float
    hop_centres_hz: np.ndarray    # centre of each tuning hop (zero-IF DC artefacts sit here)
    dropped_sweeps: int = 0       # warm-up sweeps discarded by --skip-sweeps
    truncated_from: int = 0       # sweeps before --max-sweeps cut it down


def _reading(x: str) -> float:
    """One dB reading, or NaN if it is not a finite number.

    rtl_power emits non-finite values on the strongest bins, and the MSVC
    build spells them `-nan(ind)`, which `float()` rejects. A bad *bin* must
    not cost the whole *hop*: one unparseable value used to discard all 240
    readings on that line, which silently threw away 40% of an FM survey.
    """
    try:
        v = float(x)
    except (ValueError, TypeError):
        return np.nan
    return v if math.isfinite(v) else np.nan


def load_rtl_power(path: str | Path) -> Survey:
    path = Path(path)
    rows = []
    hop_centres = set()
    step_hz = None
    nonfinite = 0
    total_bins = 0
    with open(path, "r", newline="") as fh:
        for ln, line in enumerate(fh, 1):
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 7:
                continue
            try:
                ts = pd.Timestamp(f"{parts[0]} {parts[1]}")
                lo, hi, step = float(parts[2]), float(parts[3]), float(parts[4])
            except (ValueError, TypeError):
                print(f"  skipping malformed line {ln}", file=sys.stderr)
                continue
            vals = np.array([_reading(x) for x in parts[6:]], dtype=float)
            total_bins += vals.size
            nonfinite += int(np.isnan(vals).sum())
            if step <= 0 or len(vals) == 0:
                continue
            step_hz = step if step_hz is None else step_hz
            freqs = lo + np.arange(len(vals)) * step
            hop_centres.add(round((lo + hi) / 2.0))
            rows.append((ts, freqs, vals))
    if not rows:
        raise SystemExit(f"No rtl_power data found in {path}")

    if nonfinite:
        pct = 100.0 * nonfinite / max(total_bins, 1)
        print(f"  note: {nonfinite} of {total_bins} readings ({pct:.1f}%) were not finite "
              f"and are treated as missing.", file=sys.stderr)
        if pct >= 1.0:
            print("        rtl_power produces these on its strongest bins, so a figure this "
                  "high\n        usually means the front end is being driven very hard. The "
                  "peak levels\n        in this survey are therefore LOWER BOUNDS.",
                  file=sys.stderr)

    # rtl_power gives each hop its own timestamp. Group hops into sweeps:
    # a new sweep starts when a hop's start frequency is not above the previous hop's.
    sweep_id = []
    sid, prev_lo = 0, -math.inf
    for ts, freqs, _ in rows:
        if freqs[0] <= prev_lo:
            sid += 1
        prev_lo = freqs[0]
        sweep_id.append(sid)

    # Build a common frequency grid snapped to the step.
    all_f = np.unique(np.round(np.concatenate([r[1] for r in rows]) / step_hz).astype(np.int64))
    grid = all_f * step_hz
    index = {k: i for i, k in enumerate(all_f)}
    n_sweeps = sid + 1
    P = np.full((n_sweeps, len(grid)), np.nan)
    t0 = [None] * n_sweeps
    for (ts, freqs, vals), s in zip(rows, sweep_id):
        if t0[s] is None:
            t0[s] = ts
        cols = [index[k] for k in np.round(freqs / step_hz).astype(np.int64)]
        # where hops overlap, keep the lower value (edge bins are the unreliable ones)
        cur = P[s, cols]
        P[s, cols] = np.where(np.isnan(cur), vals, np.fmin(cur, vals))
    return Survey(
        name=path.stem,
        freqs_hz=grid,
        times=pd.DatetimeIndex(t0),
        power_db=P,
        step_hz=step_hz,
        hop_centres_hz=np.array(sorted(hop_centres), dtype=float),
    )


def load_rtl_power_fftw(path: str | Path, crop: float = 0.0) -> Survey:
    """Read rtl_power_fftw's text output into the same Survey as rtl_power's CSV.

    Each hop is a block: five `#` header lines (the program's name, acquisition
    start and end in UTC to the second, a blank comment, the column names), then
    N lines of `frequency power`, then a blank line; a pass over every hop ends
    with a second blank line. Frequencies are bin CENTRES, tuned_freq + (i - N/2)
    * rate / N, so the bin at index N/2 is the hop centre (the DC bin, which the
    program has already replaced by its neighbours' mean).

    `crop` is the counterpart of rtl_power's -c: drop that fraction of each hop,
    half from each edge, using rtl_power's own index arithmetic (int(N*crop/2)
    bins off each end). The recorder asks for a matching -o overlap so the kept
    parts still tile the span. A single-hop run is cropped the same way, which
    removes the anti-alias roll-off at the edges.

    Sweeps are grouped exactly as for rtl_power: a new one starts when a hop's
    first frequency is not above the previous hop's, so a hop the program
    dropped as untunable shifts nothing. Timestamps are converted from UTC to
    local time so reports read like rtl_power's.
    """
    path = Path(path)
    blocks: list[tuple[pd.Timestamp | None, np.ndarray, np.ndarray]] = []
    freqs: list[float] = []
    vals: list[float] = []
    start: pd.Timestamp | None = None
    nonfinite = 0
    total = 0

    def close_block():
        nonlocal freqs, vals, start
        if freqs:
            f = np.asarray(freqs, dtype=float)
            v = np.asarray(vals, dtype=float)
            n = len(f)
            k = int(n * crop * 0.5) if crop > 0 else 0
            if k > 0 and n - 2 * k >= 2:
                f, v = f[k:n - k], v[k:n - k]
            blocks.append((start, f, v))
        freqs, vals, start = [], [], None

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for ln, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                close_block()
                continue
            if line.startswith("#"):
                m = line.partition("Acquisition start:")[2].strip()
                if m:
                    try:
                        start = pd.Timestamp(m.replace(" UTC", ""), tz="UTC")
                    except (ValueError, TypeError):
                        start = None
                continue
            parts = line.split()
            if len(parts) < 2:
                print(f"  skipping malformed line {ln}", file=sys.stderr)
                continue
            try:
                f = float(parts[0])
            except ValueError:
                print(f"  skipping malformed line {ln}", file=sys.stderr)
                continue
            v = _reading(parts[1])
            total += 1
            nonfinite += int(math.isnan(v))
            freqs.append(f)
            vals.append(v)
    close_block()
    if not blocks:
        raise SystemExit(f"No rtl_power_fftw data found in {path}")

    if nonfinite:
        pct = 100.0 * nonfinite / max(total, 1)
        print(f"  note: {nonfinite} of {total} readings ({pct:.1f}%) were not finite "
              f"and are treated as missing.", file=sys.stderr)

    # The bin width is rate / N: take it from the spacing inside the first block,
    # which the program prints to enough digits for this to be exact.
    f0 = blocks[0][1]
    step_hz = float(np.median(np.diff(f0))) if len(f0) > 1 else 1.0
    # Hop centres: the middle bin of each uncropped block is the tuned frequency.
    # After cropping, the middle of the kept run is still that bin.
    hop_centres = set()
    for _, f, _v in blocks:
        hop_centres.add(round(float(f[len(f) // 2])))

    sweep_id = []
    sid, prev_lo = 0, -math.inf
    for _, f, _v in blocks:
        if f[0] <= prev_lo:
            sid += 1
        prev_lo = f[0]
        sweep_id.append(sid)

    all_k = np.unique(np.round(np.concatenate([b[1] for b in blocks]) / step_hz).astype(np.int64))
    grid = all_k * step_hz
    index = {k: i for i, k in enumerate(all_k)}
    n_sweeps = sid + 1
    P = np.full((n_sweeps, len(grid)), np.nan)
    t0: list = [None] * n_sweeps
    local_tz = dt.datetime.now().astimezone().tzinfo
    for (ts, f, v), sw in zip(blocks, sweep_id):
        if t0[sw] is None and ts is not None:
            t0[sw] = ts.tz_convert(local_tz).tz_localize(None)
        cols = [index[k] for k in np.round(f / step_hz).astype(np.int64)]
        cur = P[sw, cols]
        # where hops overlap, keep the lower value (edge bins are the unreliable ones)
        P[sw, cols] = np.where(np.isnan(cur), v, np.fmin(cur, v))
    # A pass with no readable timestamp gets the previous one plus nothing: keep
    # the index monotonic rather than invent a time.
    for i in range(n_sweeps):
        if t0[i] is None:
            t0[i] = t0[i - 1] if i and t0[i - 1] is not None else pd.Timestamp(0)
    return Survey(
        name=path.stem,
        freqs_hz=grid,
        times=pd.DatetimeIndex(t0),
        power_db=P,
        step_hz=step_hz,
        hop_centres_hz=np.array(sorted(hop_centres), dtype=float),
    )


def survey_engine(path: str | Path) -> str:
    """'rtl_power_fftw' if the file is that program's output, else 'rtl_power'.

    rtl_power_fftw starts every block with `# rtl-power-fftw output`; rtl_power
    CSVs start with a date. Judged on the first non-blank line.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.strip():
                    return "rtl_power_fftw" if line.lstrip().startswith("#") else "rtl_power"
    except OSError:
        pass
    return "rtl_power"


def load_survey(path: str | Path, crop: float | None = None) -> Survey:
    """Load either engine's output. For rtl_power_fftw the crop comes from the
    argument, else the sidecar's `crop`, else 0 (nothing dropped)."""
    if survey_engine(path) != "rtl_power_fftw":
        return load_rtl_power(path)
    if crop is None:
        sc = load_sidecar(path) or {}
        crop = float((sc.get("rtl_power_fftw") or {}).get("crop", 0.0) or 0.0)
    return load_rtl_power_fftw(path, crop=crop)


def drop_sweeps(s: Survey, n: int) -> None:
    """Discard the first n sweeps, in place.

    Gain and noise floor settle over the first several minutes of a run. Measured
    2026-10-14 on this station: ~0.5 dB over the first ~8 sweeps of a 2 min/sweep
    survey, even after a 20 min warm-up. Those sweeps skew the 20th-percentile floor
    and sit in the occupancy denominator, so a long run is cleaner without them.
    """
    if n <= 0:
        return
    if n >= len(s.times):
        raise SystemExit(f"--skip-sweeps {n}: {s.name} only has {len(s.times)} sweeps")
    s.times = s.times[n:]
    s.power_db = s.power_db[n:]
    s.dropped_sweeps += n


def keep_sweeps(s: Survey, n: int) -> None:
    """Keep only the first n sweeps, in place.

    Max-hold is extreme-value statistics: the more sweeps you take the higher the peak
    you happen to catch, so a long run reads systematically hotter than a short one for
    the same signal. Measured 2026-10-16 on 1799.98 MHz, a steady external carrier: the
    gap between max-hold and the median over sweeps was 0.15 dB at 22 sweeps and 1.18 dB
    at 472. Truncating a long run to a short one's sweep count makes the two comparable
    without re-recording anything.

    The sweep-count-robust alternatives are the median over sweeps (steady signals) and
    occupancy percent (intermittent ones); both are already in peaks.csv. Use this when a
    max-hold comparison is what you need.
    """
    if n <= 0 or n >= len(s.times):
        return
    s.truncated_from = len(s.times)
    s.times = s.times[:n]
    s.power_db = s.power_db[:n]


def apply_mask(s: Survey, ranges_mhz: list[tuple[float, float]]) -> None:
    f = s.freqs_hz / 1e6
    for a, b in ranges_mhz:
        s.power_db[:, (f >= a) & (f <= b)] = np.nan


# ----------------------------------------------------------------------------
# Sidecars: the <survey>.json written by run_survey.py, holding the settings
# and station conditions that the CSV itself cannot record.
# ----------------------------------------------------------------------------
# Settings that must match for two surveys to be comparable at all.
CRITICAL_SETTINGS = [
    ("gain_db", "gain", "dB"),
    ("integration_s", "integration", "s"),
    ("crop", "crop", ""),
    ("fir", "FIR", ""),
    ("bin_khz", "bin width requested", "kHz"),
]
# Station changes: usually the very thing a comparison is testing.
CHAIN_FIELDS = [("antenna_shape", "antenna"), ("whip_length_mm", "whip mm"),
                ("lna", "LNA"), ("filter", "filter")]


def load_sidecar(csv_path: str | Path) -> dict | None:
    """Read <same name>.json beside the CSV, or None if there isn't one."""
    p = Path(csv_path).with_suffix(".json")
    if not p.exists():
        return None
    try:
        sc = json.loads(p.read_text(encoding="utf-8"))
        return sc if isinstance(sc, dict) else None
    except (OSError, ValueError) as exc:
        print(f"  warning: could not read sidecar {p.name}: {exc}")
        return None


def sidecar_lines(sc: dict | None) -> list[str]:
    """The 'Survey settings' section of report.md, from the sidecar."""
    if not sc:
        return ["## Survey settings", "",
                "_No sidecar JSON found beside this CSV, so the gain, crop and integration "
                "time are unknown. Surveys recorded with `tools/run_survey.py` get one "
                "automatically._", ""]
    chain = sc.get("chain", {})
    if sc.get("engine") == "rtl_power_fftw":
        return _sidecar_lines_fftw(sc)
    rp = sc.get("rtl_power", {})
    rows = [
        ("Preset", sc.get("preset")),
        ("Recorded", sc.get("started_local")),
        ("Duration", f"{sc.get('duration_requested')} requested"
                     + (f", {sc['duration_actual_s'] / 60:.1f} min actual"
                        if sc.get("duration_actual_s") else "")),
        ("Gain", f"{rp.get('gain_db')} dB fixed" if rp.get("gain_db") is not None else None),
        ("Integration", f"{rp.get('integration_s')} s/sweep" if rp.get("integration_s") else None),
        ("Bin width", f"{rp.get('bin_khz')} kHz requested"
                      + (f", {rp['bin_khz_actual']:g} kHz actual"
                         if rp.get("bin_khz_actual") else "")
                      if rp.get("bin_khz") else None),
        ("Crop / FIR", f"-c {rp.get('crop')} -F {rp.get('fir')}"
                       if rp.get("crop") is not None else None),
        ("Receiver", sc.get("receiver")),
        ("Program", program_line(rp)),
        ("Antenna", f"{chain.get('antenna_shape')}, whips {chain.get('whip_length_mm')} mm each"
                    if chain.get("antenna_shape") else None),
        ("LNA", chain.get("lna")),
        ("Filter", chain.get("filter")),
    ]
    out = ["## Survey settings", "", "| Setting | Value |", "|---|---|"]
    out += [f"| {k} | {v} |" for k, v in rows if v not in (None, "", "None")]
    out += _conditions_rows(sc)
    if sc.get("interrupted"):
        out.append("| Note | **run was interrupted**, this is a partial survey |")
    out += ["", f"Command: `{sc.get('command_line', 'unknown')}`", ""]
    # An amended sidecar should say so, not just quietly read differently.
    for note in sc.get("corrections", []):
        out += [f"**Corrected after the fact:** {note}", ""]
    if sc.get("conditions"):
        out += ["**Conditions:** " + sc["conditions"], ""]
    return out


def _conditions_rows(sc: dict) -> list[str]:
    """Measured conditions beside the settings; the typed text stays below the table.

    Sidecars from before 2026-10-26 have no auto_conditions and get no rows.
    """
    return auto_cond.table_rows(sc.get("auto_conditions"))


def _sidecar_lines_fftw(sc: dict) -> list[str]:
    """The 'Survey settings' section for an rtl_power_fftw run."""
    rp = sc.get("rtl_power_fftw", {})
    chain = sc.get("chain", {})
    rows = [
        ("Engine", "rtl_power_fftw (dB are a power spectral density on its own scale, "
                   "not rtl_power's)"),
        ("Preset", sc.get("preset")),
        ("Recorded", sc.get("started_local")),
        ("Duration", f"{sc.get('duration_requested')} requested"
                     + (f", {sc['duration_actual_s'] / 60:.1f} min actual"
                        if sc.get("duration_actual_s") else "")),
        ("Gain", f"{rp.get('gain_db')} dB fixed" if rp.get("gain_db") is not None else None),
        ("Integration", f"{rp.get('time_per_hop_s')} s per hop" if rp.get("time_per_hop_s") else None),
        ("FFT bins per hop", rp.get("bins")),
        ("Sample rate", f"{rp['rate_hz'] / 1e6:g} MS/s"
                        + (f" ({rp['rate_hz_actual'] / 1e6:.6f} actual)" if rp.get("rate_hz_actual") else "")
                        if rp.get("rate_hz") else None),
        ("Bin width", f"{rp['bin_khz']:.4f} kHz"
                      + (f" ({rp['bin_khz_actual']:g} kHz actual)" if rp.get("bin_khz_actual") else "")
                      if rp.get("bin_khz") else None),
        ("Overlap / crop", f"-o {rp.get('overlap_pct')}, crop {rp.get('crop')} at analysis"
                           if rp.get("crop") is not None else None),
        ("Baseline", rp.get("baseline")),
        ("Window", rp.get("window")),
        ("Receiver", sc.get("receiver")),
        ("Antenna", f"{chain.get('antenna_shape')}, whips {chain.get('whip_length_mm')} mm each"
                    if chain.get("antenna_shape") else None),
        ("LNA", chain.get("lna")),
        ("Filter", chain.get("filter")),
    ]
    out = ["## Survey settings", "", "| Setting | Value |", "|---|---|"]
    out += [f"| {k} | {v} |" for k, v in rows if v not in (None, "", "None")]
    out += _conditions_rows(sc)
    if sc.get("interrupted"):
        out.append("| Note | **run was interrupted**, this is a partial survey |")
    out += ["", f"Command: `{sc.get('command_line', 'unknown')}`", ""]
    for note in sc.get("corrections", []):
        out += [f"**Corrected after the fact:** {note}", ""]
    if sc.get("conditions"):
        out += ["**Conditions:** " + sc["conditions"], ""]
    return out


# rtl_power_fftw settings that must match for two of its runs to be comparable.
CRITICAL_SETTINGS_FFTW = [
    ("gain_db", "gain", "dB"),
    ("time_per_hop_s", "integration per hop", "s"),
    ("bins", "FFT bins", ""),
    ("rate_hz", "sample rate", "Hz"),
    ("overlap_pct", "overlap", "%"),
    ("crop", "crop", ""),
]


def compare_sidecars(sa_name, sca, sb_name, scb, step_khz_a, step_khz_b) -> list[str]:
    """Warn about setting differences that make a comparison meaningless.

    The actual bin width comes from the CSVs themselves, so that check works
    even with no sidecars; the rest needs them.
    """
    problems, changes, notes = [], [], []

    if abs(step_khz_a - step_khz_b) > 1e-6:
        problems.append(f"**bin width differs**: {step_khz_a:g} kHz vs {step_khz_b:g} kHz "
                        "(measured from the CSVs)")

    if not sca or not scb:
        missing = [n for n, sc in ((sa_name, sca), (sb_name, scb)) if not sc]
        notes.append("No sidecar for " + ", ".join(missing)
                     + ", so gain, crop and integration could not be checked.")
    else:
        ea, eb = sca.get("engine") or "rtl_power", scb.get("engine") or "rtl_power"
        if ea != eb:
            problems.append(f"**engine differs**: {ea} vs {eb} (their dB scales are not the "
                            "same; levels cannot be compared at all)")
        ra, rb = sca.get(ea, {}), scb.get(eb, {})
        settings = CRITICAL_SETTINGS_FFTW if ea == eb == "rtl_power_fftw" else CRITICAL_SETTINGS
        for key, label, unit in settings:
            va, vb = ra.get(key), rb.get(key)
            if va is None or vb is None:
                continue
            if va != vb:
                u = f" {unit}" if unit else ""
                problems.append(f"**{label} differs**: {va}{u} vs {vb}{u}")
        if ea == eb == "rtl_power" and build_token(sca) != build_token(scb):
            problems.append(f"**rtl_power build differs**: {build_token(sca) or 'stock'} vs "
                            f"{build_token(scb) or 'stock'} (the stock build ignored the "
                            "R820T/R860 gain, so levels cannot be compared)")
        if sca.get("receiver") != scb.get("receiver"):
            problems.append(f"**receiver differs**: {sca.get('receiver')} vs {scb.get('receiver')}")

        ca, cb = sca.get("chain", {}), scb.get("chain", {})
        for key, label in CHAIN_FIELDS:
            if ca.get(key) != cb.get(key):
                changes.append(f"{label}: `{ca.get(key)}` -> `{cb.get(key)}`")
        for sc, name in ((sca, sa_name), (scb, sb_name)):
            if sc.get("interrupted"):
                notes.append(f"{name} was interrupted and is a partial survey.")

    lines = []
    if problems:
        lines += ["## Settings mismatch", "",
                  "These differences change the measurement itself, so floor and signal "
                  "changes below may reflect the settings rather than the station:", ""]
        lines += [f"- {p}" for p in problems] + [""]
    elif sca and scb:
        lines += ["## Settings", "",
                  "Recording settings match, so differences below should be real.", ""]
    else:
        # Only the bin width could be checked, so don't claim more than that.
        lines += ["## Settings", "",
                  "Bin width matches, but gain, crop and integration could not be "
                  "verified without sidecars.", ""]
    if changes:
        lines += ["**Station changes between the two runs** (probably what you were testing):", ""]
        lines += [f"- {c}" for c in changes] + [""]
    if notes:
        lines += [f"_{n}_" for n in notes] + [""]
    return lines


# ----------------------------------------------------------------------------
# Analysis
# ----------------------------------------------------------------------------
def rolling_median(x: np.ndarray, win: int) -> np.ndarray:
    s = pd.Series(x)
    return s.rolling(win, center=True, min_periods=max(3, win // 4)).median().to_numpy()


@dataclass
class Analysis:
    floor: np.ndarray       # local noise floor per bin (dB)
    median: np.ndarray      # per-bin median over time
    maxhold: np.ndarray     # per-bin max over time
    occupancy: np.ndarray   # fraction of sweeps above floor + threshold
    threshold_db: float
    peaks: pd.DataFrame


def analyze(s: Survey, threshold_db: float = 10.0, floor_window_mhz: float = 2.0,
            min_snr_db: float | None = None) -> Analysis:
    P = s.power_db
    # A bin that rtl_power never managed to report in any sweep is all-NaN down
    # its column, and numpy warns on each one. That is expected on a saturating
    # survey - load_rtl_power already reports the count - so don't shout about it
    # once per bin here.
    with np.errstate(all="ignore"), warnings.catch_warnings():
        warnings.filterwarnings("ignore", r"All-NaN slice encountered", RuntimeWarning)
        warnings.filterwarnings("ignore", r"All-NaN slice encountered", RuntimeWarning,
                                module=r"numpy\..*")
        med = np.nanmedian(P, axis=0)
        mx = np.nanmax(P, axis=0)
        # Noise floor: a low percentile over time (ignores bursts), then a rolling
        # median across frequency (ignores steady carriers).
        p20 = np.nanpercentile(P, 20, axis=0)
    win = max(5, int(round(floor_window_mhz * 1e6 / s.step_hz)) | 1)
    floor = rolling_median(p20, win)
    floor = pd.Series(floor).interpolate(limit_direction="both").to_numpy()
    with np.errstate(invalid="ignore"):
        above = P > (floor + threshold_db)[None, :]
        valid = ~np.isnan(P)
        occ = np.where(valid.sum(0) > 0, above.sum(0) / np.maximum(valid.sum(0), 1), np.nan)
    peaks = find_signals(s, floor, med, mx, occ, min_snr_db if min_snr_db is not None else threshold_db)
    return Analysis(floor, med, mx, occ, threshold_db, peaks)


def _local_maxima(y: np.ndarray, prominence: float, distance: int) -> np.ndarray:
    try:
        from scipy.signal import find_peaks
        yy = np.nan_to_num(y, nan=np.nanmin(y))
        idx, _ = find_peaks(yy, prominence=prominence, distance=distance)
        return idx
    except ImportError:
        idx = []
        for i in range(1, len(y) - 1):
            if np.isnan(y[i]):
                continue
            lo, hi = max(0, i - distance), min(len(y), i + distance + 1)
            if y[i] == np.nanmax(y[lo:hi]) and y[i] - np.nanmin(y[lo:hi]) >= prominence:
                idx.append(i)
        return np.array(idx, dtype=int)


def spur_tags(f_mhz: float, tol_mhz: float, hop_centres_mhz: np.ndarray) -> list[str]:
    tags = []
    for base, label in SPUR_FAMILIES_MHZ:
        n = round(f_mhz / base)
        if n >= 1 and abs(f_mhz - n * base) <= tol_mhz:
            tags.append(f"{label} (x{n})")
    if len(hop_centres_mhz) and np.min(np.abs(hop_centres_mhz - f_mhz)) <= tol_mhz:
        tags.append("hop centre: zero-IF DC artefact, likely not real")
    return tags


def find_signals(s: Survey, floor, med, mx, occ, min_snr_db: float) -> pd.DataFrame:
    f_mhz = s.freqs_hz / 1e6
    snr_max = mx - floor
    snr_med = med - floor
    dist = max(1, int(round(25e3 / s.step_hz)))   # peaks at least 25 kHz apart
    idx = _local_maxima(snr_max, prominence=min_snr_db / 2, distance=dist)
    idx = [i for i in idx if not np.isnan(snr_max[i]) and snr_max[i] >= min_snr_db]
    tol = max(s.step_hz * 1.5, 10e3) / 1e6
    hc = s.hop_centres_hz / 1e6
    # One tuning hop is the widest thing a single impulse can smear across: rtl_power
    # samples a hop as one block, so a click during that block lifts the whole hop's FFT
    # at once. A detection that wide is almost never a transmission - measured
    # 2026-10-15, a 16 h LS survey produced 147 "signals", many exactly one hop across.
    hop_mhz = float(np.median(np.diff(hc))) if len(hc) > 1 else 0.0
    out = []
    for i in idx:
        # width: contiguous bins within 6 dB of the peak (max-hold)
        thr = mx[i] - 6
        a = i
        while a > 0 and not np.isnan(mx[a - 1]) and mx[a - 1] >= thr:
            a -= 1
        b = i
        while b < len(mx) - 1 and not np.isnan(mx[b + 1]) and mx[b + 1] >= thr:
            b += 1
        o = float(occ[i]) if not np.isnan(occ[i]) else 0.0
        kind = "steady" if o >= 0.9 else "intermittent" if o >= 0.05 else "rare burst"
        tags = spur_tags(f_mhz[i], tol, hc)
        width_mhz = (b - a + 1) * s.step_hz / 1e6
        if hop_mhz and width_mhz >= 0.8 * hop_mhz:
            tags.insert(0, f"hop-wide ({width_mhz/hop_mhz:.1f} hops): impulse, not a signal")
        out.append({
            "freq_mhz": round(f_mhz[i], 4),
            "width_khz": round((b - a + 1) * s.step_hz / 1e3, 1),
            "max_db": round(float(mx[i]), 1),
            "median_db": round(float(med[i]), 1),
            "floor_db": round(float(floor[i]), 1),
            "snr_max_db": round(float(snr_max[i]), 1),
            "snr_median_db": round(float(snr_med[i]), 1),
            "occupancy_pct": round(100 * o, 1),
            "type": kind,
            "spur_tags": "; ".join(tags),
        })
    df = pd.DataFrame(out)
    if not df.empty:
        df = df.sort_values("snr_max_db", ascending=False).reset_index(drop=True)
    return df


# ----------------------------------------------------------------------------
# Plots
# ----------------------------------------------------------------------------
def _plt():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    return plt


def _freq_axis(ax, f_mhz):
    ax.set_xlim(f_mhz[0], f_mhz[-1])
    ax.set_xlabel("Frequency (MHz)")


def plot_heatmap(s: Survey, a: Analysis, path: Path):
    plt = _plt()
    f = s.freqs_hz / 1e6
    P = s.power_db
    lo = np.nanpercentile(a.floor, 5) - 3
    hi = np.nanpercentile(a.maxhold, 99.5)
    fig, ax = plt.subplots(figsize=(14, 6), dpi=110)
    ext = [f[0], f[-1], len(s.times), 0]
    im = ax.imshow(P, aspect="auto", extent=ext, cmap="magma", vmin=lo, vmax=hi, interpolation="nearest")
    _freq_axis(ax, f)
    n = len(s.times)
    ticks = np.linspace(0, n - 1, min(n, 8)).astype(int)
    ax.set_yticks(ticks + 0.5)
    ax.set_yticklabels([s.times[t].strftime("%H:%M:%S") for t in ticks])
    ax.set_ylabel("Time (sweep start)")
    ax.set_title(f"{s.name}: waterfall ({n} sweeps, {s.step_hz/1e3:g} kHz bins)")
    cb = fig.colorbar(im, ax=ax, pad=0.01)
    cb.set_label("Power (dB, relative)")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def plot_spectrum(s: Survey, a: Analysis, path: Path, top_n: int = 25):
    plt = _plt()
    f = s.freqs_hz / 1e6
    fig, ax = plt.subplots(figsize=(14, 5.5), dpi=110)
    ax.plot(f, a.maxhold, lw=0.6, color="#d9822b", label="Max hold")
    ax.plot(f, a.median, lw=0.8, color="#2f6db5", label="Median")
    ax.plot(f, a.floor, lw=1.4, color="#1f7a45", label="Noise floor (estimated)")
    ax.plot(f, a.floor + a.threshold_db, lw=0.9, ls="--", color="#888888", label=f"Threshold (+{a.threshold_db:g} dB)")
    if not a.peaks.empty:
        spur = a.peaks["spur_tags"].astype(bool)
        sp = a.peaks[spur]
        top = a.peaks[~spur].head(top_n)
        ax.scatter(sp["freq_mhz"], sp["max_db"], s=14, marker="x", color="#7a4fb0", zorder=4, label=f"Likely spur ({len(sp)})")
        ax.scatter(top["freq_mhz"], top["max_db"], s=26, color="#b03434", zorder=5, label=f"Signal ({int((~spur).sum())})")
        for _, r in top.iterrows():
            ax.annotate(f"{r.freq_mhz:.3f}", (r.freq_mhz, r.max_db), textcoords="offset points", xytext=(0, 5),
                        ha="center", fontsize=7, color="#333333")
    _freq_axis(ax, f)
    ax.set_ylabel("Power (dB, relative)")
    ax.set_title(f"{s.name}: spectrum summary")
    ax.grid(alpha=0.25)
    ax.legend(loc="upper right", fontsize=8, ncol=3)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def plot_occupancy(s: Survey, a: Analysis, path: Path):
    plt = _plt()
    f = s.freqs_hz / 1e6
    fig, ax = plt.subplots(figsize=(14, 3.6), dpi=110)
    ax.fill_between(f, 100 * np.nan_to_num(a.occupancy), step="mid", color="#2f6db5", alpha=0.7, lw=0)
    _freq_axis(ax, f)
    ax.set_ylim(0, 100)
    ax.set_ylabel("% of sweeps above threshold")
    ax.set_title(f"{s.name}: occupancy (threshold floor + {a.threshold_db:g} dB)")
    ax.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def plot_combined(s: Survey, a: Analysis, path: Path, top_n: int = 12):
    """Spectrum, occupancy and waterfall stacked on one shared frequency axis.

    The three separate plots answer different questions about the same column of
    spectrum, and flipping between them loses the correspondence. Stacked with a
    shared x-axis you can read a feature downward: strong in max hold, how often
    it was there, and whether the waterfall shows a continuous stripe (steady) or
    one short dash (a burst).

    Occupancy is drawn on a log axis here, unlike the standalone plot. With N
    sweeps the smallest non-zero value is 100/N %, so on a 0-100 linear axis a
    one-sweep burst is a 3% stub next to a 100% spur and reads as empty. Log
    makes "once" and "always" both legible, which is the whole point of putting
    it next to the waterfall.
    """
    plt = _plt()
    f = s.freqs_hz / 1e6
    n = len(s.times)

    fig = plt.figure(figsize=(14, 11.5), dpi=110)
    gs = fig.add_gridspec(3, 2, width_ratios=[60, 1], height_ratios=[2.2, 1.0, 2.2],
                          hspace=0.07, wspace=0.015)
    ax1 = fig.add_subplot(gs[0, 0])
    ax2 = fig.add_subplot(gs[1, 0], sharex=ax1)
    ax3 = fig.add_subplot(gs[2, 0], sharex=ax1)
    cax = fig.add_subplot(gs[2, 1])
    for row in (0, 1):  # keep the plot column aligned across all three panels
        fig.add_subplot(gs[row, 1]).axis("off")

    # Guide lines at the strongest non-spur signals, drawn through all panels so
    # the eye can follow one frequency from spectrum down to waterfall.
    guides = []
    if not a.peaks.empty:
        spur = a.peaks["spur_tags"].astype(bool)
        guides = list(a.peaks[~spur].head(top_n)["freq_mhz"])
    for g in guides:
        for ax in (ax1, ax2, ax3):
            ax.axvline(g, color="#b03434", lw=0.7, alpha=0.3, zorder=1)

    # --- spectrum -----------------------------------------------------------
    ax1.plot(f, a.maxhold, lw=0.6, color="#d9822b", label="Max hold", zorder=3)
    ax1.plot(f, a.median, lw=0.8, color="#2f6db5", label="Median", zorder=3)
    ax1.plot(f, a.floor, lw=1.4, color="#1f7a45", label="Noise floor", zorder=3)
    ax1.plot(f, a.floor + a.threshold_db, lw=0.9, ls="--", color="#888888",
             label=f"Threshold (+{a.threshold_db:g} dB)", zorder=3)
    if not a.peaks.empty:
        sp = a.peaks[spur]
        top = a.peaks[~spur].head(top_n)
        ax1.scatter(sp["freq_mhz"], sp["max_db"], s=14, marker="x", color="#7a4fb0",
                    zorder=4, label=f"Likely spur ({len(sp)})")
        ax1.scatter(top["freq_mhz"], top["max_db"], s=26, color="#b03434", zorder=5,
                    label=f"Signal ({int((~spur).sum())})")
        for _, r in top.iterrows():
            ax1.annotate(f"{r.freq_mhz:.2f}", (r.freq_mhz, r.max_db),
                         textcoords="offset points", xytext=(0, 6), ha="center",
                         fontsize=7, color="#333333", rotation=90)
    ax1.set_ylabel("Power (dB, relative)")
    ax1.grid(alpha=0.25)
    ax1.legend(loc="upper right", fontsize=8, ncol=3)
    ax1.set_title(f"{s.name}: spectrum, occupancy and waterfall on one frequency axis")
    ax1.tick_params(labelbottom=False)

    # --- occupancy, log scale ----------------------------------------------
    occ = 100.0 * np.nan_to_num(a.occupancy)
    one = 100.0 / max(1, n)          # what a single sweep is worth
    nz = occ > 0
    if nz.any():
        ax2.vlines(f[nz], one * 0.55, occ[nz], color="#2f6db5", lw=0.9, zorder=3)
    ax2.axhline(one, color="#555555", ls=":", lw=0.9, zorder=2)
    ax2.annotate(f"1 of {n} sweeps", (f[0], one), textcoords="offset points",
                 xytext=(4, 3), fontsize=7, color="#555555")
    ax2.set_yscale("log")
    ax2.set_ylim(one * 0.55, 160)
    ax2.set_yticks([one, 10, 50, 100])
    ax2.set_yticklabels([f"{one:.1f}%", "10%", "50%", "100%"])
    ax2.set_ylabel("Occupancy\n(log)")
    ax2.grid(alpha=0.25, which="both")
    ax2.tick_params(labelbottom=False)

    # --- waterfall ----------------------------------------------------------
    lo = np.nanpercentile(a.floor, 5) - 3
    hi = np.nanpercentile(a.maxhold, 99.5)
    im = ax3.imshow(s.power_db, aspect="auto", extent=[f[0], f[-1], n, 0],
                    cmap="magma", vmin=lo, vmax=hi, interpolation="nearest", zorder=0)
    ticks = np.linspace(0, n - 1, min(n, 8)).astype(int)
    ax3.set_yticks(ticks + 0.5)
    ax3.set_yticklabels([s.times[t].strftime("%H:%M:%S") for t in ticks])
    ax3.set_ylabel("Time (sweep start)")
    _freq_axis(ax3, f)
    cb = fig.colorbar(im, cax=cax)
    cb.set_label("Power (dB, relative)")

    ax1.set_xlim(f[0], f[-1])
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


# ----------------------------------------------------------------------------
# Reports
# ----------------------------------------------------------------------------
def band_table(s: Survey, a: Analysis, band_mhz: float) -> pd.DataFrame:
    f = s.freqs_hz / 1e6
    edges = np.arange(math.floor(f[0] / band_mhz) * band_mhz, f[-1] + band_mhz, band_mhz)
    rows = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (f >= lo) & (f < hi)
        if not m.any() or np.all(np.isnan(a.floor[m])):
            continue
        n_sig = 0 if a.peaks.empty else int(((a.peaks.freq_mhz >= lo) & (a.peaks.freq_mhz < hi)).sum())
        rows.append({
            "band_mhz": f"{lo:g}-{hi:g}",
            "floor_db": round(float(np.nanmedian(a.floor[m])), 1),
            "occupied_pct": round(float(100 * np.nanmean(a.occupancy[m] > 0.05)), 1),
            "signals": n_sig,
        })
    return pd.DataFrame(rows)


def md_table(df: pd.DataFrame) -> str:
    """Markdown table without needing the optional 'tabulate' package."""
    if df is None or df.empty:
        return "_none_"
    cols = list(df.columns)
    fmt = lambda v: "" if (v is None or (isinstance(v, float) and math.isnan(v))) else str(v)
    out = ["| " + " | ".join(cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
    for _, r in df.iterrows():
        out.append("| " + " | ".join(fmt(r[c]).replace("|", "/") for c in cols) + " |")
    return "\n".join(out)


def spur_summary(pk: pd.DataFrame) -> pd.DataFrame:
    rows = {}
    for _, r in pk[pk.spur_tags != ""].iterrows():
        for tag in r.spur_tags.split("; "):
            fam = tag.split(" (x")[0]
            d = rows.setdefault(fam, {"pattern": fam, "count": 0, "strongest_mhz": r.freq_mhz, "strongest_snr_db": r.snr_max_db})
            d["count"] += 1
            if r.snr_max_db > d["strongest_snr_db"]:
                d["strongest_mhz"], d["strongest_snr_db"] = r.freq_mhz, r.snr_max_db
    return pd.DataFrame(sorted(rows.values(), key=lambda d: -d["count"]))


def write_report(s: Survey, a: Analysis, out: Path, mask, sidecar: dict | None = None):
    f = s.freqs_hz / 1e6
    span = f[-1] - f[0]
    band = 1.0 if span <= 20 else 10.0 if span <= 300 else 50.0
    bt = band_table(s, a, band)
    dur = (s.times[-1] - s.times[0]) if len(s.times) > 1 else pd.Timedelta(0)
    pk = a.peaks
    lines = [
        f"# Survey report: {s.name}",
        "",
        f"- Range: {f[0]:.3f} to {f[-1]:.3f} MHz, {s.step_hz/1e3:g} kHz bins, {len(f):,} bins",
        f"- Sweeps: {len(s.times)} over {dur}"
        + (f" (of {s.truncated_from}, truncated by --max-sweeps)" if s.truncated_from else "")
        + (f" (first {s.dropped_sweeps} discarded as warm-up)" if s.dropped_sweeps else ""),
        f"- Threshold: noise floor + {a.threshold_db:g} dB",
        f"- Masked (excluded): {', '.join(f'{x:g}-{y:g} MHz' for x, y in mask) if mask else 'none'}",
        f"- Median noise floor: {np.nanmedian(a.floor):.1f} dB (relative)",
        f"- Signals detected: {len(pk)}"
        + ("" if pk.empty else f" ({int((pk.spur_tags != '').sum())} tagged as likely spurs)"),
        "",
        *sidecar_lines(sidecar),
        "## Images",
        "",
        "![combined: spectrum + occupancy + waterfall, shared frequency axis](combined.png)",
        "",
        "![waterfall](heatmap.png)",
        "",
        "![spectrum](spectrum.png)",
        "",
        "![occupancy](occupancy.png)",
        "",
        f"## Bands ({band:g} MHz)",
        "",
        md_table(bt) if not bt.empty else "_no data_",
        "",
        "## Signals not matching a known spur pattern",
        "",
        md_table(pk[pk.spur_tags == ""].head(60)) if not pk.empty else "_none above threshold_",
        "",
        "## Likely spurs by pattern",
        "",
        md_table(spur_summary(pk)) if not pk.empty else "_none_",
        "",
        ("Notes: levels are relative dB from rtl_power_fftw (a power spectral density on its own scale, not rtl_power's and not dBm). 'steady' = above threshold in at least 90% of sweeps;"
         if (sidecar or {}).get("engine") == "rtl_power_fftw" else
         "Notes: levels are relative dB from rtl_power, not calibrated dBm. 'steady' = above threshold in at least 90% of sweeps;"),
        "'intermittent' = 5-90%; 'rare burst' = under 5%. Hop-centre tags mark the zero-IF DC spot of each tuning hop;",
        "use rtl_power -c 0.25 (crop) to remove most of them.",
    ]
    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")


def save_tables(s: Survey, a: Analysis, out: Path):
    a.peaks.to_csv(out / "peaks.csv", index=False)
    pd.DataFrame({
        "freq_mhz": np.round(s.freqs_hz / 1e6, 6),
        "floor_db": np.round(a.floor, 2),
        "median_db": np.round(a.median, 2),
        "max_db": np.round(a.maxhold, 2),
        "occupancy_pct": np.round(100 * a.occupancy, 2),
    }).to_csv(out / "bins.csv", index=False)


# ----------------------------------------------------------------------------
# Compare two surveys
# ----------------------------------------------------------------------------
def compare(sa: Survey, aa: Analysis, sb: Survey, ab: Analysis, out: Path, match_khz: float = 50.0,
            sca: dict | None = None, scb: dict | None = None):
    plt = _plt()
    fa, fb = sa.freqs_hz / 1e6, sb.freqs_hz / 1e6
    lo, hi = max(fa[0], fb[0]), min(fa[-1], fb[-1])
    if hi <= lo:
        raise SystemExit("The two surveys don't overlap in frequency.")
    grid = np.arange(lo, hi, max(sa.step_hz, sb.step_hz) / 1e6)
    floor_a = np.interp(grid, fa, np.nan_to_num(aa.floor, nan=np.nan))
    floor_b = np.interp(grid, fb, np.nan_to_num(ab.floor, nan=np.nan))
    med_a = np.interp(grid, fa, aa.median)
    med_b = np.interp(grid, fb, ab.median)
    d_floor = floor_b - floor_a

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 7), dpi=110, sharex=True,
                                   gridspec_kw={"height_ratios": [2, 1]})
    ax1.plot(grid, med_a, lw=0.7, color="#7a4fb0", label=f"{sa.name} median")
    ax1.plot(grid, med_b, lw=0.7, color="#d9822b", label=f"{sb.name} median")
    ax1.plot(grid, floor_a, lw=1.3, color="#7a4fb0", ls="--", label=f"{sa.name} floor")
    ax1.plot(grid, floor_b, lw=1.3, color="#d9822b", ls="--", label=f"{sb.name} floor")
    ax1.set_ylabel("Power (dB, relative)")
    ax1.legend(fontsize=8, ncol=2)
    ax1.grid(alpha=0.25)
    ax1.set_title(f"Compare: {sa.name} vs {sb.name}")
    ax2.axhline(0, color="#888", lw=0.8)
    ax2.plot(grid, d_floor, lw=1.0, color="#1f7a45")
    ax2.set_ylabel("Floor change (dB)")
    ax2.set_xlabel("Frequency (MHz)")
    ax2.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(out / "compare.png")
    plt.close(fig)

    tol = match_khz / 1e3
    pa = aa.peaks if not aa.peaks.empty else pd.DataFrame(columns=["freq_mhz", "snr_max_db"])
    pb = ab.peaks if not ab.peaks.empty else pd.DataFrame(columns=["freq_mhz", "snr_max_db"])
    pa = pa[(pa.freq_mhz >= lo) & (pa.freq_mhz <= hi)]
    pb = pb[(pb.freq_mhz >= lo) & (pb.freq_mhz <= hi)]
    rows, used = [], set()
    for _, r in pa.iterrows():
        d = (pb.freq_mhz - r.freq_mhz).abs() if not pb.empty else pd.Series(dtype=float)
        j = d.idxmin() if len(d) and d.min() <= tol else None
        if j is not None:
            used.add(j)
            rows.append({"freq_mhz": r.freq_mhz, "status": "in both", "snr_before": r.snr_max_db,
                         "snr_after": pb.loc[j, "snr_max_db"], "change_db": round(pb.loc[j, "snr_max_db"] - r.snr_max_db, 1),
                         "spur_tags": r.get("spur_tags", "")})
        else:
            rows.append({"freq_mhz": r.freq_mhz, "status": "gone", "snr_before": r.snr_max_db,
                         "snr_after": None, "change_db": None, "spur_tags": r.get("spur_tags", "")})
    for j, r in pb.iterrows():
        if j not in used:
            rows.append({"freq_mhz": r.freq_mhz, "status": "new", "snr_before": None,
                         "snr_after": r.snr_max_db, "change_db": None, "spur_tags": r.get("spur_tags", "")})
    ct = pd.DataFrame(rows).sort_values("freq_mhz") if rows else pd.DataFrame()
    ct.to_csv(out / "compare_signals.csv", index=False)
    settings = compare_sidecars(sa.name, sca, sb.name, scb,
                                sa.step_hz / 1e3, sb.step_hz / 1e3)
    summary = [
        f"# Compare: {sa.name} -> {sb.name}",
        "",
        *settings,
        f"- Overlap: {lo:.3f} to {hi:.3f} MHz",
        f"- Median floor change: {np.nanmedian(d_floor):+.1f} dB (positive = floor rose)",
        f"- Signals: {int((ct.status == 'in both').sum()) if not ct.empty else 0} in both, "
        f"{int((ct.status == 'gone').sum()) if not ct.empty else 0} gone, {int((ct.status == 'new').sum()) if not ct.empty else 0} new",
        "",
        "![compare](compare.png)",
        "",
        md_table(ct) if not ct.empty else "_no signals in either survey_",
        "",
        "Only compare surveys taken with the same gain, bin size, crop and integration time;",
        "otherwise floor changes reflect settings, not the station.",
    ]
    (out / "compare.md").write_text("\n".join(summary), encoding="utf-8")


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def parse_mask(items, receiver):
    rng = list(RECEIVER_GAPS_MHZ.get(receiver, [])) if receiver else []
    for it in items or []:
        a, b = it.split("-")
        rng.append((float(a), float(b)))
    return rng


def run_one(csv, args, base_out: Path):
    s = load_survey(csv, crop=getattr(args, "crop", None))
    drop_sweeps(s, args.skip_sweeps)
    keep_sweeps(s, getattr(args, 'max_sweeps', 0))
    sc = load_sidecar(csv)
    mask = parse_mask(args.mask, args.receiver)
    if mask:
        apply_mask(s, mask)
    a = analyze(s, threshold_db=args.threshold, floor_window_mhz=args.floor_window)
    out = base_out / s.name
    out.mkdir(parents=True, exist_ok=True)
    plot_heatmap(s, a, out / "heatmap.png")
    plot_spectrum(s, a, out / "spectrum.png")
    plot_occupancy(s, a, out / "occupancy.png")
    plot_combined(s, a, out / "combined.png")
    save_tables(s, a, out)
    write_report(s, a, out, mask, sc)
    print(f"{s.name}: {len(s.times)} sweeps, {len(s.freqs_hz):,} bins, {len(a.peaks)} signals"
          f"{'' if sc else ' (no sidecar)'} -> {out}")
    return s, a, sc


def main(argv=None, station: Station | None = None, prog: str = "marsad"):
    station = station or Station.load()
    configure(station)
    p = argparse.ArgumentParser(prog=prog, description="Analyze rtl_power survey CSVs.")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("analyze", "compare"):
        sp = sub.add_parser(name)
        if name == "analyze":
            sp.add_argument("csv", nargs="+", help="rtl_power CSV or rtl_power_fftw .dat file(s)")
        else:
            sp.add_argument("before")
            sp.add_argument("after")
        sp.add_argument("--threshold", type=float, default=10.0, help="dB above the noise floor (default 10)")
        sp.add_argument("--floor-window", type=float, default=2.0, help="MHz window for floor estimate (default 2)")
        sp.add_argument("--mask", action="append", help="exclude a range, e.g. 1095-1240 (repeatable)")
        sp.add_argument("--receiver", choices=list(RECEIVER_GAPS_MHZ), help="auto-mask this receiver's tuning gaps")
        sp.add_argument("--max-sweeps", type=int, default=0,
                        help="analyse only the first N sweeps, so a long run can be compared fairly with a short one")
        sp.add_argument("--skip-sweeps", type=int, default=0,
                        help="discard the first N sweeps (receiver warm-up drift)")
        sp.add_argument("--out", default="reports", help="output folder (default ./reports)")
        sp.add_argument("--crop", type=float, default=None, metavar="F",
                        help="rtl_power_fftw files only: fraction of each hop to drop at the edges "
                             "(default: the sidecar's value, else 0). rtl_power files are cropped "
                             "by rtl_power itself (-c) and ignore this.")
    args = p.parse_args(argv)
    base = Path(args.out)
    if args.cmd == "analyze":
        for c in args.csv:
            run_one(c, args, base)
    else:
        sa, aa, sca = run_one(args.before, args, base)
        sb, ab, scb = run_one(args.after, args, base)
        out = base / f"compare_{sa.name}_vs_{sb.name}"
        out.mkdir(parents=True, exist_ok=True)
        compare(sa, aa, sb, ab, out, sca=sca, scb=scb)
        # Say it on the console too; a warning only in the file is easy to miss.
        for line in compare_sidecars(sa.name, sca, sb.name, scb,
                                     sa.step_hz / 1e3, sb.step_hz / 1e3):
            if line.startswith("- ") or line.startswith("_"):
                print("  " + line.replace("**", ""))
        print(f"comparison -> {out}")


