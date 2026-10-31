"""Broadband events in a survey: sustained steps, and one-sweep transients.

    marsad events <tag|report|csv|dat> [--block 25] [--min-db 0.5] [--min-sweeps 6]
                  [--skip-sweeps N] [--out reports/<survey>]

Found by eye first (V1-4n, 2026-10-29): something switched on at 02:19:45 and lifted
225-349 MHz by +1.0 to +1.4 dB until 05:07-05:12. No single bin shows that - every bin
moves by a fraction of its own scatter - but the MEDIAN of a block of bins in one sweep
repeats to a few hundredths of a dB here (see drift.py), so a broadband emitter shows as a
clean step in it while a single line does not move it at all.

How it decides, per series (each block's per-sweep median, and the whole band's):

- sigma: the settled per-sweep scatter, from the median absolute sweep-to-sweep change
  (x 1.4826 / sqrt 2). A step or a warm-up is one or a few large differences among
  hundreds, so it does not inflate this.
- sustained change: S(t) = median of the next N sweeps minus median of the previous N.
  Local maxima of |S| at or over max(min_db, K_STEP x sigma) are cuts (no two within N);
  a cut that no longer moves the level by that much between its segments is dropped;
  each cut is moved to the earliest sweep from which the series stays on the new side.
  A segment above both neighbours, or below both, by the threshold is an event: its
  NEIGHBOURS are the baseline, because a night has no one quiet level (V1-4n: -0.43 dB
  before the emitter, +0.45 during, -0.41 after, ~0 once the morning starts; a 4 h and an
  8 h rolling median were tried and both settled between them). An edge segment counts
  against its one neighbour when it is the shorter and the neighbour is not an event.
- across series: runs overlapping for half the shorter are one event, dated by the whole
  band when it saw it. Broadband or nothing: the whole band moved, or two or more blocks
  moved and all the same way (blocks going opposite ways is a hump changing shape).
  STEP if at most 2 sweeps sit part-way across a change, else RAMP.
- warm-up: a slow settling seldom makes an N-sweep step, so it is looked for directly
  (warm_up): reported as warm-up / trend, never an event, and the search starts after it.
- transients: a sweep (or two) that differs from the median of the 4 sweeps before it AND
  the 4 after it, in the same direction, by max(min_db, K_TRANSIENT x sigma), in the whole
  band or at least two blocks. Taking the smaller of the two differences is what keeps
  the edge of a step from counting: at an edge one side always agrees. Looked for in
  every sweep, the warm-up skip included.

Defaults, and why: N = 6 sweeps (a median of 6 cannot be moved by a one- or two-sweep
lift, and 6 x 30 s = 3 min resolves a switch-on to the sweep at the station's usual -i 30);
min_db 0.5 (the handled detection floor); K_STEP 5, K_TRANSIENT 6 (on the v5's 0.016 dB
settled scatter both sit far under 0.5, so 0.5 decides; on a noisier block the scatter
decides, and a block with 0.2 dB of sweep-to-sweep noise needs a 1 dB step).
"""
from __future__ import annotations

import argparse
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from .config import Station

BLOCK_MHZ = 25.0
MIN_DB = 0.5
MIN_SWEEPS = 6
K_STEP = 5.0
K_TRANSIENT = 6.0
TRANSIENT_SIDE = 4          # sweeps each side a transient is judged against
WARMUP_FRACTION = 0.25      # a ramp as the first change, this early, is warm-up


@dataclass
class Found:
    kind: str                       # "event", "transient", "warm-up"
    shape: str                      # step / ramp / one-sweep lift / one-sweep dip / ...
    start: int                      # first sweep index (analysed sweeps)
    end: int | None                 # first sweep after it; None = still on at the end
    change: dict = field(default_factory=dict)      # series label -> dB
    moved: list = field(default_factory=list)       # block labels at or over threshold
    note: str = ""


# ---------------------------------------------------------------- series

def block_edges(f_mhz: np.ndarray, block_mhz: float) -> list:
    """Blocks of block_mhz from the survey's low edge; a last sliver under a fifth of a
    block joins the one before it."""
    lo, hi = float(f_mhz[0]), float(f_mhz[-1])
    edges = [lo]
    while edges[-1] + block_mhz < hi:
        edges.append(edges[-1] + block_mhz)
    edges.append(hi)
    if len(edges) > 2 and edges[-1] - edges[-2] < 0.2 * block_mhz:
        edges.pop(-2)
    return edges


def _label(a: float, b: float) -> str:
    return f"{a:g}-{b:g}"


def block_series(s, block_mhz: float = BLOCK_MHZ):
    """Per-sweep median of each block and of the whole band: (labels, (T, B+1) array).
    The whole band is the last column, labelled "whole"."""
    f = s.freqs_hz / 1e6
    edges = block_edges(f, block_mhz)
    labels, cols = [], []
    P = s.power_db
    with np.errstate(all="ignore"), _quiet():
        for a, b in zip(edges[:-1], edges[1:]):
            m = (f >= a) & ((f < b) if b < edges[-1] else (f <= b))
            labels.append(_label(round(a, 3), round(b, 3)))
            cols.append(np.nanmedian(P[:, m], axis=1))
        labels.append("whole")
        cols.append(np.nanmedian(P, axis=1))
    return labels, np.column_stack(cols)


class _quiet:
    def __enter__(self):
        import warnings
        self._w = warnings.catch_warnings()
        self._w.__enter__()
        warnings.simplefilter("ignore", RuntimeWarning)

    def __exit__(self, *a):
        self._w.__exit__(*a)


def robust_sigma(x: np.ndarray) -> float:
    d = np.diff(x[np.isfinite(x)])
    if len(d) < 3:
        return math.nan
    return float(1.4826 * np.median(np.abs(d - np.median(d))) / math.sqrt(2))


def _window_medians(x: np.ndarray, w: int) -> np.ndarray:
    """Median of x[i:i+w] for every i (length T-w+1)."""
    from numpy.lib.stride_tricks import sliding_window_view
    with _quiet():
        return np.nanmedian(sliding_window_view(x, w), axis=1)


def step_statistic(x: np.ndarray, w: int) -> np.ndarray:
    """S[t] = median(x[t:t+w]) - median(x[t-w:t]); NaN where either side is short."""
    T = len(x)
    S = np.full(T, np.nan)
    if T < 2 * w:
        return S
    fwd = _window_medians(x, w)
    t = np.arange(w, T - w + 1)
    S[t] = fwd[t] - fwd[t - w]
    return S


# ---------------------------------------------------------------- sustained changes

def change_points(x: np.ndarray, thr: float, w: int) -> list:
    """Local maxima of |S| at or over thr, strongest first, no two closer than w; then
    every cut that no longer moves the level by thr between its neighbouring segments is
    dropped, weakest first."""
    S = np.abs(step_statistic(x, w))
    cand = [(float(S[t]), int(t)) for t in np.flatnonzero(np.nan_to_num(S) >= thr)
            if S[t] >= np.nanmax(S[max(0, t - w):t + w + 1])]
    cuts = []
    for _, t in sorted(cand, reverse=True):
        if all(abs(t - c) >= w for c in cuts):
            cuts.append(t)
    cuts.sort()
    while cuts:
        lv = levels(x, cuts)
        d = np.abs(np.diff(lv))
        k = int(np.nanargmin(d))
        if d[k] >= thr:
            break
        cuts.pop(k)
    return cuts


def levels(x: np.ndarray, cuts: list) -> np.ndarray:
    b = [0, *cuts, len(x)]
    with _quiet():
        return np.array([np.nanmedian(x[b[i]:b[i + 1]]) for i in range(len(b) - 1)])


def _shape(x: np.ndarray, c: int, before: float, after: float, w: int) -> str:
    """STEP if at most 2 sweeps around the change sit part-way (25-75 %) between the two
    levels, else RAMP."""
    d = after - before
    if not (np.isfinite(d) and d):
        return "step"
    seg = x[max(0, c - w):min(len(x), c + w)]
    u = (seg - before) / d
    return "step" if int(np.sum((u > 0.25) & (u < 0.75))) <= 2 else "ramp"


def settle_cut(x: np.ndarray, c: int, before: float, after: float, w: int) -> int:
    """Move a cut to the earliest sweep from which the series stays on the new side of
    the midpoint for the rest of the window. A switch-on preceded by a dip, or a switch-off
    that dips and overshoots before it settles (V1-4n, 05:07-05:12), then dates to the
    sweep where the new level begins rather than to wherever the median window put it."""
    mid, sgn = (before + after) / 2, np.sign(after - before)
    lo, hi = max(1, c - w), min(len(x), c + w)
    ok = np.nan_to_num(sgn * (x[lo:hi] - mid), nan=1.0) > 0
    stays = np.flip(np.cumprod(np.flip(ok)))          # True where every later sweep is on the new side
    i = np.flatnonzero(stays)
    return int(lo + i[0]) if len(i) else c


def series_events(x: np.ndarray, thr: float, w: int) -> tuple:
    """One series cut into segments at its own change points. Returns (runs, warm):
    runs = [(start, end, level change)] for every segment that sits above both
    neighbours or below both by thr (an edge segment against its one neighbour, when
    it is the shorter of the two); warm = end of a warm-up ramp at the start, or None."""
    T = len(x)
    cuts = change_points(x, thr, w)
    if not cuts:
        return [], None
    lv = levels(x, cuts)
    cuts = [settle_cut(x, c, lv[i], lv[i + 1], w) for i, c in enumerate(cuts)]
    lv = levels(x, cuts)
    b = [0, *cuts, T]
    first, warm = 0, None
    c = cuts[0]
    if c <= WARMUP_FRACTION * T and _shape(x, c, lv[0], lv[1], w) == "ramp":
        first, warm = 1, c
    n = len(lv)
    hit = {}
    for i in range(first + 1, n - 1):
        d = (lv[i] - lv[i - 1], lv[i] - lv[i + 1])
        if np.sign(d[0]) == np.sign(d[1]) and min(abs(d[0]), abs(d[1])) >= thr:
            hit[i] = float(np.mean(d))
    # an edge segment against its one neighbour: only when that neighbour is not itself an
    # event (the quiet stretch before an emitter is not "a dip that was already on") and
    # the edge is the shorter of the two, so the longer one is taken as the baseline
    for i, k in ((first, first + 1), (n - 1, n - 2)):
        if first <= k < n and i != k and k not in hit and i not in hit:
            d = lv[i] - lv[k]
            if abs(d) >= thr and b[i + 1] - b[i] < b[k + 1] - b[k]:
                hit[i] = float(d)
    return [(b[i], b[i + 1], hit[i]) for i in sorted(hit)], warm


def warm_up(x: np.ndarray, thr: float, w: int) -> int | None:
    """End of a warm-up at the start of x, or None.

    A warm-up is a slow, one-way settling: it seldom makes a w-sweep step (V1-1b fell
    1.93 dB over 65 min, 0.09 dB per 6 sweeps at most), so it is looked for directly.
    The run has one when the smoothed whole band, from its first sweeps to the first
    point in the first half where it then holds within thr/2 for a quarter of the run,
    moves by thr or more and does so one way (at least 80 % of its sweep-to-sweep moves
    share a sign)."""
    T = len(x)
    if T < 8 * w:
        return None
    with _quiet():
        xs = pd.Series(x).rolling(w, center=True, min_periods=1).median().to_numpy()
    hold = max(w, T // 4)
    for t in range(0, T // 2):
        seg = xs[t:t + hold]
        if np.nanmax(seg) - np.nanmin(seg) <= thr / 2:
            break
    else:
        return None
    if t <= w:
        return None
    d = np.diff(xs[:t + 1])
    d = d[np.isfinite(d) & (d != 0)]
    one_way = len(d) and max(np.mean(d > 0), np.mean(d < 0)) >= 0.8
    with _quiet():
        moved = abs(np.nanmedian(x[:w]) - np.nanmedian(x[t:t + hold]))
    return t if one_way and moved >= thr else None


def sustained(M: np.ndarray, labels: list, thr: np.ndarray, w: int) -> list:
    wj = labels.index("whole")
    found = []
    t0 = warm_up(M[:, wj], thr[wj], w)
    if t0 is not None:
        with _quiet():
            ch = np.nanmedian(M[:w], axis=0) - np.nanmedian(M[t0:t0 + 4 * w], axis=0)
        found.append(Found("warm-up", "ramp", 0, t0, _named(ch, labels), _moved(ch, labels, thr),
                           f"warm-up or trend, not an event; settled from sweep {t0 + 1}"))
        rest = sustained(M[t0:], labels, thr, w)
        for f in rest:
            f.start += t0
            f.end = None if f.end is None else f.end + t0
            f.note = f.note.replace("already on when the analysed sweeps begin", "already on when the warm-up ended")
        return found + [f for f in rest if f.kind != "warm-up"]
    T = len(M)
    runs, warm_end = [], None
    for j in range(M.shape[1]):
        r, warm = series_events(M[:, j], thr[j], w)
        runs += [(abs(d) / thr[j], j, a, e) for a, e, d in r]
        if j == wj and warm is not None:
            warm_end = warm
    if warm_end is not None:
        with _quiet():
            ch = np.nanmedian(M[:min(w, warm_end)], axis=0) - np.nanmedian(M[warm_end:warm_end + 4 * w], axis=0)
        found.append(Found("warm-up", "ramp", 0, warm_end, _named(ch, labels), _moved(ch, labels, thr),
                           f"warm-up or trend, not an event; level settled from sweep {warm_end + 1}"))
    # the same event seen in several series: the runs overlap for most of the shorter one.
    # The strongest run (in units of its own threshold) sets the event's start and end.
    events = []
    for score, j, a, e in sorted(runs, reverse=True):
        for ev in events:
            if min(e, ev["b"]) - max(a, ev["a"]) >= 0.5 * min(e - a, ev["b"] - ev["a"]):
                ev["members"].append((j, a, e))
                break
        else:
            events.append({"a": a, "b": e, "j": j, "members": [(j, a, e)]})
    for ev in events:
        # when the whole band saw it, its run dates the event: it is the steadiest series
        for j, a, e in ev["members"]:
            if j == wj:
                ev.update(a=a, b=e, j=j)
    for ev in sorted(events, key=lambda ev: ev["a"]):
        a, e, j = ev["a"], ev["b"], ev["j"]
        with _quiet():
            inside = np.nanmedian(M[a:e], axis=0)
            outside = [np.nanmedian(M[max(0, a - (e - a)):a], axis=0) if a > 0 else None,
                       np.nanmedian(M[e:e + (e - a)], axis=0) if e < T else None]
            outside = [o for o in outside if o is not None]
            ch = inside - np.nanmean(outside, axis=0)
        moved = _moved(ch, labels, thr)
        up = sum(1 for lab in moved if ch[labels.index(lab)] > 0)
        down = len(moved) - up
        # broadband: the whole band moved, or at least two blocks moved and all the same way.
        # Blocks going opposite ways is a hump changing shape (the 270 MHz one does, all
        # morning on V1-4n), not something switching on.
        if not (abs(ch[wj]) >= thr[wj] or (up >= 2 and not down) or (down >= 2 and not up)):
            continue
        x = M[:, j]
        with _quiet():
            pre = np.nanmedian(x[max(0, a - w):a]) if a > 0 else np.nan
            post = np.nanmedian(x[e:e + w]) if e < T else np.nan
            on = _shape(x, a, pre, np.nanmedian(x[a:a + w]), w) if a > 0 else None
            off = _shape(x, e, np.nanmedian(x[max(a, e - w):e]), post, w) if e < T else None
        note = []
        if a == 0:
            note.append("already on when the analysed sweeps begin")
        if e == T:
            note.append("still on at the end of the run")
        shape = on or off or "step"
        if on and off and off != on:
            shape = f"{on} on, {off} off"
        found.append(Found("event", shape, a, None if e == T else e, _named(ch, labels), moved,
                           "; ".join(note)))
    return found


def _named(ch, labels) -> dict:
    return {lab: (round(float(v), 3) if np.isfinite(v) else None) for lab, v in zip(labels, ch)}


def _moved(ch, labels, thr) -> list:
    return [lab for lab, v, t in zip(labels, ch, thr) if lab != "whole" and np.isfinite(v) and abs(v) >= t]


# ---------------------------------------------------------------- transients

def transients(M: np.ndarray, labels: list, thr: np.ndarray, h: int = TRANSIENT_SIDE,
               max_len: int | None = None) -> list:
    T = len(M)
    R = np.zeros_like(M)
    with _quiet():
        for t in range(T):
            b = np.nanmedian(M[max(0, t - h):t], axis=0) if t > 0 else None
            a = np.nanmedian(M[t + 1:t + 1 + h], axis=0) if t < T - 1 else None
            sides = [x for x in (b, a) if x is not None]
            if not sides:
                continue
            d = [M[t] - x for x in sides]
            if len(d) == 2:
                same = np.sign(d[0]) == np.sign(d[1])
                R[t] = np.where(same, np.sign(d[0]) * np.minimum(np.abs(d[0]), np.abs(d[1])), 0.0)
            else:
                R[t] = d[0]
    R = np.nan_to_num(R)
    flag = np.abs(R) >= thr[None, :]
    whole = labels.index("whole")
    broad = flag[:, whole] | (flag[:, :whole].sum(axis=1) >= 2)
    found, t = [], 0
    while t < T:
        if not broad[t]:
            t += 1
            continue
        sign = np.sign(R[t, whole]) or np.sign(np.sum(R[t, :whole][flag[t, :whole]]))
        e = t + 1
        while e < T and broad[e] and (np.sign(R[e, whole]) or sign) == sign:
            e += 1
        n = e - t
        if max_len is None or n <= max_len:
            seg = R[t:e]
            pick = np.argmax(np.abs(seg), axis=0)
            ch = seg[pick, np.arange(seg.shape[1])]
            word = "lift" if sign >= 0 else "dip"
            shape = {1: f"one-sweep {word}", 2: f"two-sweep {word}"}.get(n, f"short {word} ({n} sweeps)")
            found.append(Found("transient", shape, t, e, _named(ch, labels),
                               [lab for lab, f_ in zip(labels[:whole], flag[t:e, :whole].any(axis=0)) if f_]))
        t = e
    return found


# ---------------------------------------------------------------- the finder

@dataclass
class Result:
    labels: list
    sigma: np.ndarray
    thr_step: np.ndarray
    thr_transient: np.ndarray
    found: list
    times: pd.DatetimeIndex
    settings: dict


def find(s, block_mhz: float = BLOCK_MHZ, min_db: float = MIN_DB, min_sweeps: int = MIN_SWEEPS,
         k_step: float = K_STEP, k_transient: float = K_TRANSIENT, skip: int = 0) -> Result:
    """`s` holds every sweep; the first `skip` (warm-up) are left out of the sustained
    search and of the scatter, but transients are looked for in them too: a sweep judged
    against its own neighbours does not care that the level is still settling."""
    labels, M = block_series(s, block_mhz)
    return find_series(labels, M, s.times, block_mhz, min_db, min_sweeps, k_step, k_transient, skip)


def find_series(labels: list, M: np.ndarray, times, block_mhz: float = BLOCK_MHZ,
                min_db: float = MIN_DB, min_sweeps: int = MIN_SWEEPS, k_step: float = K_STEP,
                k_transient: float = K_TRANSIENT, skip: int = 0) -> Result:
    """The finder on per-sweep series already made (block_series' output)."""
    keep = np.isfinite(M).any(axis=0)
    labels = [lab for lab, k in zip(labels, keep) if k]
    M = M[:, keep]
    w = max(2, int(min_sweeps))
    skip = max(0, min(int(skip), len(M) - 2 * w))
    sigma = np.array([robust_sigma(M[skip:, j]) for j in range(M.shape[1])])     # settled scatter
    thr_s = np.maximum(min_db, k_step * np.nan_to_num(sigma))
    thr_t = np.maximum(min_db, k_transient * np.nan_to_num(sigma))
    sus = sustained(M[skip:], labels, thr_s, w) if len(M) - skip >= 2 * w else []
    for f in sus:
        f.start += skip
        f.end = None if f.end is None else f.end + skip
    found = sus + transients(M, labels, thr_t, max_len=w - 1)
    found.sort(key=lambda f: (f.start, f.kind))
    return Result(labels, sigma, thr_s, thr_t, found, pd.DatetimeIndex(times),
                  {"block_mhz": block_mhz, "min_db": min_db, "min_sweeps": w,
                   "k_step": k_step, "k_transient": k_transient, "skip": skip})


def _t(res: Result, i: int | None) -> str:
    if i is None:
        return ""
    return pd.Timestamp(res.times[min(i, len(res.times) - 1)]).strftime("%Y-%m-%d %H:%M:%S")


def _minutes(res: Result, f: Found) -> float | None:
    if f.end is None:
        a, b = res.times[f.start], res.times[-1]
    else:
        a, b = res.times[f.start], res.times[min(f.end, len(res.times) - 1)]
    return round((b - a).total_seconds() / 60.0, 1)


def table(res: Result) -> pd.DataFrame:
    rows = []
    for f in res.found:
        r = {"kind": f.kind, "shape": f.shape, "start": _t(res, f.start),
             "end": _t(res, f.end) if f.end is not None else ("" if f.kind != "event" else "still on"),
             "start_sweep": f.start + 1, "sweeps": (f.end if f.end is not None else len(res.times)) - f.start,
             "duration_min": _minutes(res, f), "whole_db": f.change.get("whole"),
             "blocks_moved": "; ".join(f"{b} {f.change[b]:+.2f}" for b in f.moved if f.change.get(b) is not None),
             "note": f.note}
        for lab in res.labels:
            if lab != "whole":
                r[f"d_{lab}"] = f.change.get(lab)
        rows.append(r)
    cols = ["kind", "shape", "start", "end", "start_sweep", "sweeps", "duration_min", "whole_db",
            "blocks_moved", "note", *[f"d_{lab}" for lab in res.labels if lab != "whole"]]
    return pd.DataFrame(rows, columns=cols)


def section(res: Result) -> list:
    """The "## Events" section of report.md, as lines."""
    st = res.settings
    j = res.labels.index("whole")
    lines = [
        "## Events",
        "",
        f"Sustained changes of at least max({st['min_db']:g} dB, {st['k_step']:g} x the settled "
        f"per-sweep scatter) lasting at least {st['min_sweeps']} sweeps, in the per-sweep median of "
        f"each {st['block_mhz']:g} MHz block and of the whole band; transients are one or two sweeps "
        f"off both neighbours by max({st['min_db']:g} dB, {st['k_transient']:g} x scatter). "
        f"Whole-band scatter {res.sigma[j]:.3f} dB per sweep. `marsad events` reruns it; "
        "every row is in events.csv.",
        "",
    ]
    ev = [f for f in res.found if f.kind == "event"]
    wu = [f for f in res.found if f.kind == "warm-up"]
    tr = [f for f in res.found if f.kind == "transient"]
    for f in wu:
        lines.append(f"- **Warm-up / trend** to {_t(res, f.end)}: whole band "
                     f"{f.change.get('whole', float('nan')):+.2f} dB. {f.note}.")
    for f in ev:
        end = f"until {_t(res, f.end)}" if f.end is not None else "still on at the end"
        moved = ", ".join(f"{b} {f.change[b]:+.2f}" for b in f.moved) or "no single block"
        whole = f.change.get("whole")
        lines.append(f"- **{f.shape.capitalize()}** from {_t(res, f.start)} {end} "
                     f"({_minutes(res, f):g} min): {moved} dB"
                     + (f"; whole band {whole:+.2f} dB" if whole is not None else "")
                     + (f" ({f.note})" if f.note else "") + ".")
    if not ev and not wu:
        lines.append("- No sustained change.")
    if tr:
        lines += ["", "Transients:", "", "| when | shape | whole band dB | blocks |", "|---|---|---|---|"]
        for f in tr:
            whole = f.change.get("whole")
            lines.append(f"| {_t(res, f.start)} | {f.shape} | "
                         f"{'' if whole is None else f'{whole:+.2f}'} | "
                         f"{', '.join(f'{b} {f.change[b]:+.2f}' for b in f.moved)} |")
    else:
        lines.append("- No transients.")
    lines.append("")
    return lines


def write(res: Result, out: Path) -> Path:
    p = out / "events.csv"
    table(res).to_csv(p, index=False)
    return p


def splice(report: Path, lines: list) -> None:
    """Put the Events section into an existing report.md, replacing an older one."""
    text = report.read_text(encoding="utf-8")
    block = "\n".join(lines)
    m = re.search(r"^## Events\n.*?(?=^## |\Z)", text, flags=re.S | re.M)
    if m:
        text = text[:m.start()] + block + "\n" + text[m.end():]
    elif "\n## Images" in text:
        i = text.index("\n## Images") + 1
        text = text[:i] + block + "\n" + text[i:]
    else:
        text = text.rstrip("\n") + "\n\n" + block
    report.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------- command line

def resolve(station: Station, token: str) -> Path:
    """A raw survey file, a report folder, or a tag."""
    p = Path(token)
    if p.is_file():
        return p
    name = p.name if (p.is_dir() or p.suffix) else token
    for ext in (".csv", ".dat"):
        q = station.surveys / f"{name}{ext}"
        if q.exists():
            return q
    hits = sorted(f for ext in ("*.csv", "*.dat") for f in station.surveys.glob(ext)
                  if token in f.name) if station.surveys.exists() else []
    if not hits:
        raise SystemExit(f"no survey in surveys/ matches {token!r}")
    if len(hits) > 1:
        print(f"note: {token!r} matched {len(hits)} surveys, using {hits[-1].name}")
    return hits[-1]


def report_skip(report: Path) -> int:
    """The warm-up discard the report was made with ("first N discarded"), else 0."""
    if not report.exists():
        return 0
    m = re.search(r"first (\d+) discarded", report.read_text(encoding="utf-8"))
    return int(m.group(1)) if m else 0


def main(argv=None, station: Station | None = None, prog: str = "marsad events") -> int:
    from . import analyze as A
    station = station or Station.load()
    A.configure(station)
    ap = argparse.ArgumentParser(prog=prog, description="Broadband events in a survey: sustained "
                                 "steps in the per-sweep block median, and one-sweep transients.")
    ap.add_argument("survey", help="tag, raw .csv/.dat path, or report folder")
    ap.add_argument("--block", type=float, default=BLOCK_MHZ, metavar="MHZ",
                    help=f"block width in MHz (default {BLOCK_MHZ:g})")
    ap.add_argument("--min-db", type=float, default=MIN_DB,
                    help=f"smallest change reported, dB (default {MIN_DB:g})")
    ap.add_argument("--min-sweeps", type=int, default=MIN_SWEEPS,
                    help=f"sustained means at least this many sweeps (default {MIN_SWEEPS})")
    ap.add_argument("--skip-sweeps", type=int, default=None,
                    help="discard the first N sweeps (default: what the report used, else 0)")
    ap.add_argument("--out", default=None,
                    help="folder for events.csv (default reports/<survey>); its report.md, "
                         "if any, gets the Events section")
    args = ap.parse_args(argv)

    path = resolve(station, args.survey)
    out = Path(args.out) if args.out else station.reports / path.stem
    skip = args.skip_sweeps if args.skip_sweeps is not None else report_skip(out / "report.md")
    s = A.load_survey(path)
    sc = A.load_sidecar(path) or {}
    mask = A.parse_mask(None, sc.get("receiver") if sc.get("receiver") in A.RECEIVER_GAPS_MHZ else None)
    if mask:
        A.apply_mask(s, mask)
    res = find(s, args.block, args.min_db, args.min_sweeps, skip=skip)
    lines = section(res)
    print(f"{path.name}: {len(s.times)} sweeps"
          + (f", the first {res.settings['skip']} left out of the sustained search" if skip else "") + "\n")
    print("\n".join(lines))
    out.mkdir(parents=True, exist_ok=True)
    print(f"events.csv -> {write(res, out)}")
    if (out / "report.md").exists():
        splice(out / "report.md", lines)
        print(f"Events section -> {out / 'report.md'}")
    return 0
