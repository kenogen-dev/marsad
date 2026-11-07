"""The web page's Scan tab calculations, ported line for line (relay 009).

The page (marsad/web/index.html) works these out in the browser, so nothing in the
engine gives them to the window. They are copied here as plain Python with no Qt, and
`tests/test_scan_calc.py` holds them to the page's own output on made-up inputs, run
under Node by `tests/data/make_scan_fixtures.mjs`. The page is not changed.

A missing reading is NaN here and null on the page; each function treats the two alike.
JavaScript's Math.round rounds halves up, so `jsround` does the same (Python's round
rounds halves to even, which moves a mask edge by a column now and then).
"""
from __future__ import annotations

import math

import numpy as np

GRID_COLOURS = ["#e8b14f", "#c58bff", "#4fd3e8", "#ff8a65", "#9bd36b"]


def jsround(x: float) -> int:
    return math.floor(x + 0.5)


def _ok(v) -> bool:
    return v is not None and not (isinstance(v, float) and math.isnan(v))


def pct(values, p: float):
    """The page's percentile: sort what is there, take index floor(p * n) (capped)."""
    v = sorted(float(x) for x in values if x is not None and not math.isnan(float(x)))
    return v[min(len(v) - 1, math.floor(p * len(v)))] if v else None


def display_floor(rows) -> tuple:
    """(floor, top) for the spectrum: the 20th and 99.9th percentile of the last 40 sweeps, by the
    page's rule (sorted present values, index floor(p * n)); done in numpy, as it runs every sweep."""
    a = np.asarray([np.asarray(r, dtype=float) for r in list(rows)[-40:]], dtype=float).ravel()
    v = np.sort(a[~np.isnan(a)])
    if not v.size:
        return None, None
    n = v.size
    return float(v[min(n - 1, math.floor(0.2 * n))]), float(v[min(n - 1, math.floor(0.999 * n))])


def build_mask(lo, hi, cols: int, step_k=None, families=None, hop_centres=()) -> np.ndarray:
    """Columns that hold a receiver line: every harmonic of the internal families (14.4 MHz)
    within 1.5 bins (at least 10 kHz), and the hop centres where a column is finer than a
    quarter hop."""
    mask = np.zeros(cols, dtype=np.uint8)
    if lo is None or hi is None or hi <= lo:
        return mask
    span = hi - lo
    col_m = span / (cols - 1)
    tol = max((step_k or 10) * 1.5, 10) / 1e3

    def mark(f):
        a = jsround((f - tol - lo) / col_m)
        b = jsround((f + tol - lo) / col_m)
        for c in range(max(0, a), min(cols - 1, b) + 1):
            mask[c] = 1

    for base in families or [14.4]:
        n = math.ceil(lo / base)
        while n * base <= hi:
            mark(n * base)
            n += 1
    hc = list(hop_centres or [])
    hop_w = (hc[-1] - hc[0]) / (len(hc) - 1) if len(hc) > 1 else 0
    if hop_w and col_m < hop_w / 4:
        for h in hc:
            mark(h)
    return mask


def _upper_median(a: np.ndarray) -> np.ndarray:
    """v[len >> 1] of the sorted present values along the last axis (NaN = missing), as the page
    takes its medians; NaN where nothing is present."""
    s = np.sort(a, axis=-1)                                   # NaN sorts last
    n = np.sum(~np.isnan(a), axis=-1)
    idx = np.minimum(n >> 1, a.shape[-1] - 1)
    out = np.take_along_axis(s, idx[..., None], axis=-1)[..., 0]
    return np.where(n > 0, out, np.nan)


def sweep_floors(rows, lo, hi) -> np.ndarray:
    """Each sweep's own floor (one row per sweep): medians of blocks a quarter of ~2 MHz wide,
    then the median of the five blocks around each column."""
    rows = np.atleast_2d(np.asarray(rows, dtype=float))
    n_rows, cols = rows.shape
    col_m = (hi - lo) / (cols - 1)
    w = max(8, jsround(2 / col_m))
    b = max(1, jsround(w / 4))
    nb = math.ceil(cols / b)
    pad = np.full((n_rows, nb * b), np.nan)
    pad[:, :cols] = rows
    bm = _upper_median(pad.reshape(n_rows, nb, b))
    around = np.full((n_rows, nb + 4), np.nan)
    around[:, 2:-2] = bm
    fl = _upper_median(np.stack([around[:, k:k + nb] for k in range(5)], axis=2))
    return np.repeat(fl, b, axis=1)[:, :cols].astype(np.float32)


def sweep_floor(row, lo, hi) -> np.ndarray:
    """One sweep's floor (the page's sweepFloor)."""
    return sweep_floors(row, lo, hi)[0]


def layer_rows(rows, floors, mask, term=None, noise=True, signal=True, internal=True, thr=6.0) -> np.ndarray:
    """The sweeps as drawn under a layer choice, in dB (NaN = nothing there), composed in linear
    power so the three together give back the measurement:
        base = min(P, N), E = P - base
        internal = min(terminated floor, base) when one matches, plus E on a masked column
        signal   = E where P is at least thr over N (and not masked)
        noise    = the rest of base, plus E below thr
    The page keeps the result in a Float32Array, so this returns float32 too."""
    v = np.atleast_2d(np.asarray(rows, dtype=float))
    fl = np.atleast_2d(np.asarray(floors, dtype=float))
    m = np.asarray(mask, dtype=bool)[None, :]
    with np.errstate(invalid="ignore", divide="ignore", over="ignore"):
        P = 10 ** (v / 10)
        N = 10 ** (fl / 10)
        base = np.minimum(P, N)
        E = P - base
        if term is not None:
            t = np.asarray(term, dtype=float)[None, :]
            T = np.where(np.isnan(t), 0.0, np.minimum(10 ** (t / 10), base))
        else:
            T = np.zeros_like(base)
        sig = ~m & (v - fl >= thr)
        n_ = base - T + np.where(~m & ~sig, E, 0.0)
        i_ = T + np.where(m, E, 0.0)
        s_ = np.where(sig, E, 0.0)
        total = (n_ if noise else 0) + (s_ if signal else 0) + (i_ if internal else 0)
        out = np.where(total > 0, 10 * np.log10(np.where(total > 0, total, 1.0)), np.nan)
    out[np.isnan(v) | np.isnan(fl)] = np.nan
    return out.astype(np.float32)


def layer_row(row, floor, mask, term=None, noise=True, signal=True, internal=True, thr=6.0) -> np.ndarray:
    """One sweep under a layer choice (the page's layerRow)."""
    return layer_rows(row, floor, mask, term, noise, signal, internal, thr)[0]


def family_ticks(families, f_lo: float, f_hi: float, px_per_mhz: float) -> list:
    """The spur family grid: one tick per harmonic in view of each readout family. A family
    whose ticks would sit closer than 12 px is left out until the view is zoomed in, and 28.8
    is left out when 14.4 is listed (its lines are every second 14.4). Labels from 70 px.
    Returns dicts {base, n, mhz, labelled, colour} in the page's drawing order."""
    out = []
    ci = 0
    fams = list(families or [])
    for base in fams:
        if base == 28.8 and 14.4 in fams:
            continue
        col = GRID_COLOURS[ci % len(GRID_COLOURS)]
        ci += 1
        if base * px_per_mhz < 12:
            continue
        lab = base * px_per_mhz > 70
        n = math.ceil(f_lo / base)
        while n * base <= f_hi:
            out.append({"base": base, "n": n, "mhz": n * base, "labelled": lab, "colour": col})
            n += 1
    return out


def nearest_family(f: float, families):
    """The readout's "≈ 14.4 MHz × 31": the family harmonic within 0.12 MHz, nearest first,
    or None. Returns (base, n)."""
    best = None
    for k in families or []:
        n = jsround(f / k)
        d = abs(f - n * k)
        if d < 0.12 and n > 0 and (best is None or d < best[0]):
            best = (d, k, n)
    return None if best is None else (best[1], best[2])


def detection_line(peaks, thr) -> str:
    """'N possible signals · N known spurs · N artefacts (max-hold ≥ T dB over the latest
    sweep's floor)', or a no-break space when there are none, word for word as the page writes it
    (the space keeps the line's height)."""
    if not peaks:
        return " "
    nk = {"signal": 0, "spur": 0, "artefact": 0}
    for p in peaks:
        nk[p.get("kind")] = nk.get(p.get("kind"), 0) + 1

    def s(n, word):
        return f"{n} {word}{'' if n == 1 else 's'}"
    t = thr if not (isinstance(thr, float) and thr.is_integer()) else int(thr)
    return (f"{s(nk['signal'], 'possible signal')} · {s(nk['spur'], 'known spur')} · "
            f"{s(nk['artefact'], 'artefact')}  (max-hold ≥ {t} dB over the latest sweep's floor)")
