"""What is in an IQ recording, and how should the next capture of it be set?

Phase 1 of RECORDER_DESIGN.md (section 5). Reads one baseband recording and
measures, with the methods already used by hand on this station (the 1265.44 MHz
radar, 2026-10-14):

- occupied bandwidth (99 % power) and centre offset from the tuned frequency;
- duty cycle, pulse width, pulse repetition intervals and their stagger;
- chirp start / end and sweep rate, per pulse, from the instantaneous frequency;
- rotation or repeat period from the pulse-train envelope;
- a modulation family guess (CW, AM-like, FM-like, FSK with symbol rate,
  OFDM-like, noise-like) for signals that are not pulsed.

It then writes a RECIPE for the next capture: sample rate ~2.5x the occupied
bandwidth, a tuning offset that keeps the signal clear of the DC spike, a
duration that covers several repeats, and a gain hint from clipping and SNR.

    marsad characterise <recording.wav>                 SDR++ baseband WAV (rate, and frequency from the name)
    marsad characterise capture.cu8 --rate 2.4e6 --freq 1265.44e6     raw rtl_sdr uint8 IQ
    marsad characterise capture.sigmf-meta              SigMF (cu8, ci16_le, cf32_le)
    marsad characterise x.wav --start 30 --duration 36  analyse only a slice

Writes <name>.characterise.md and <name>.characterise.json beside the recording
(or into --out). Nothing here decodes content: it measures shape and timing only.

How it finds a rare signal: the radar is on air ~0.13 % of the time, so a plain
average spectrum would bury it ~29 dB under the noise. Pass 1 splits the file
into 1024-sample frames and marks the ones with something in them; pass 2 takes
the spectrum of active frames minus quiet ones; pass 3 band-filters the whole file
to the occupied band and builds a 5 us envelope; each pulse found there is then
re-read at the full sample rate for its width and chirp.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import struct
from pathlib import Path

import numpy as np

NFFT = 1024               # frame length for activity and spectra
COARSE = 32               # coarse bins per frame for the activity detector
ACTIVE_RATIO = 4.0        # a frame is active if a coarse bin is 6 dB over its median
CHUNK = 1 << 22           # complex samples per streamed read
ENV_BLOCK_S = 5e-6        # envelope resolution for finding pulses
PULSE_THR_DB = 6.0        # envelope threshold over the noise median
GOOD_PULSE_SNR_DB = 15.0  # pulses used for width and chirp statistics
MERGE_GAP_S = 25e-6       # dropouts shorter than this inside a pulse are joined
WIDTH_LEVELS_DB = (3, 6, 10, 20)   # widths reported at these levels below the pulse peak
MAINLOBE_DB = 10.0        # pulses within this of the strongest mark the beam passing
# Stable RTL2832U rates (the 300k-900k band is not available; above 2.56 drops samples)
RTL_RATES = [240e3, 250e3, 288e3, 300e3, 960e3, 1.024e6, 1.2e6, 1.44e6, 1.6e6,
             1.8e6, 1.92e6, 2.048e6, 2.4e6, 2.56e6]


# ---------------------------------------------------------------- reading files

class IQSource:
    """A memory-mapped IQ file. read() returns complex64 scaled to full scale 1.0."""

    def __init__(self, path: Path, fmt: str, fs: float, f0: float | None,
                 offset: int = 0, n: int | None = None, origin: str = ""):
        self.path, self.fmt, self.fs, self.f0, self.origin = Path(path), fmt, float(fs), f0, origin
        dtype = {"cu8": np.uint8, "cs16": np.int16, "cf32": np.float32}[fmt]
        size = self.path.stat().st_size - offset
        total = size // (2 * np.dtype(dtype).itemsize)
        self.n = int(min(total, n) if n else total)
        self._raw = np.memmap(self.path, dtype=dtype, mode="r", offset=offset, shape=(self.n * 2,))

    def raw(self, start: int, count: int) -> np.ndarray:
        end = min(start + count, self.n)
        start = max(0, start)
        count = max(0, end - start)
        return np.asarray(self._raw[2 * start: 2 * (start + count)])

    def read(self, start: int, count: int) -> np.ndarray:
        r = self.raw(start, count).astype(np.float32)
        if self.fmt == "cu8":
            r = (r - 127.5) / 127.5
        elif self.fmt == "cs16":
            r /= 32768.0
        return r.view(np.complex64)

    def rails(self, r: np.ndarray) -> int:
        """Number of I or Q values sitting on the ADC rails."""
        if self.fmt == "cu8":
            return int(np.count_nonzero((r == 0) | (r == 255)))
        if self.fmt == "cs16":
            return int(np.count_nonzero((r <= -32768) | (r >= 32767)))
        return int(np.count_nonzero(np.abs(r) >= 1.0))

    def full_scale(self, r: np.ndarray) -> float:
        """Largest |I| or |Q| as a fraction of full scale."""
        if r.size == 0:
            return 0.0
        if self.fmt == "cu8":
            return float(max(abs(int(r.max()) - 127.5), abs(int(r.min()) - 127.5)) / 127.5)
        if self.fmt == "cs16":
            return float(max(abs(int(r.max())), abs(int(r.min()))) / 32768.0)
        return float(np.max(np.abs(r)))


def freq_from_name(name: str) -> float | None:
    """SDR++ names its files baseband_1265440000Hz_00-32-45_15-10-2026.wav."""
    m = re.search(r"(\d{4,})Hz", name)
    return float(m.group(1)) if m else None


def open_wav(path: Path, freq: float | None) -> IQSource:
    with open(path, "rb") as f:
        head = f.read(12)
        if head[:4] not in (b"RIFF", b"RF64") or head[8:12] != b"WAVE":
            raise SystemExit(f"{path.name}: not a WAV file")
        fmt_tag = bits = ch = rate = None
        while True:
            hdr = f.read(8)
            if len(hdr) < 8:
                raise SystemExit(f"{path.name}: no data chunk")
            cid, size = hdr[:4], struct.unpack("<I", hdr[4:])[0]
            if cid == b"fmt ":
                body = f.read(size)
                fmt_tag, ch, rate, _, _, bits = struct.unpack("<HHIIHH", body[:16])
                if fmt_tag == 0xFFFE and len(body) >= 26:
                    fmt_tag = struct.unpack("<H", body[24:26])[0]
            elif cid == b"data":
                offset = f.tell()
                # an unfinished recording can carry 0 or 0xFFFFFFFF here
                n_bytes = size if 0 < size < 0xFFFFFFFF else path.stat().st_size - offset
                break
            else:
                f.seek(size + (size & 1), 1)
    if ch != 2:
        raise SystemExit(f"{path.name}: {ch} channel(s); a baseband WAV has 2 (I and Q)")
    kind = {(1, 8): "cu8", (1, 16): "cs16", (3, 32): "cf32"}.get((fmt_tag, bits))
    if kind is None:
        raise SystemExit(f"{path.name}: WAV format {fmt_tag} with {bits} bits is not supported")
    f0 = freq if freq is not None else freq_from_name(path.name)
    per = 2 * {"cu8": 1, "cs16": 2, "cf32": 4}[kind]
    return IQSource(path, kind, rate, f0, offset, n_bytes // per, origin=f"WAV {kind}")


def open_sigmf(path: Path, freq: float | None) -> IQSource:
    meta = path if path.name.endswith(".sigmf-meta") else path.with_name(path.name.replace(".sigmf-data", ".sigmf-meta"))
    data = meta.with_name(meta.name.replace(".sigmf-meta", ".sigmf-data"))
    m = json.loads(meta.read_text())
    g = m.get("global", {})
    dt = g.get("core:datatype", "")
    kind = {"cu8": "cu8", "ci16_le": "cs16", "cf32_le": "cf32"}.get(dt)
    if kind is None:
        raise SystemExit(f"{meta.name}: SigMF datatype {dt!r} not supported (cu8, ci16_le, cf32_le)")
    caps = m.get("captures") or [{}]
    f0 = freq if freq is not None else caps[0].get("core:frequency")
    return IQSource(data, kind, g["core:sample_rate"], f0, origin=f"SigMF {dt}")


def open_iq(path, rate=None, freq=None, fmt=None) -> IQSource:
    path = Path(path)
    name = path.name.lower()
    if name.endswith(".wav"):
        return open_wav(path, freq)
    if ".sigmf" in name:
        return open_sigmf(path, freq)
    if rate is None or freq is None:
        raise SystemExit(f"{path.name}: a raw IQ file needs --rate and --freq")
    kind = fmt or {".cs16": "cs16", ".cf32": "cf32", ".fc32": "cf32"}.get(path.suffix.lower(), "cu8")
    return IQSource(path, kind, rate, freq, origin=f"raw {kind}")


# ---------------------------------------------------------------- helpers

def _freqs(fs: float, n: int = NFFT) -> np.ndarray:
    return (np.arange(n) - n // 2) * fs / n


def obw99(freqs: np.ndarray, sig: np.ndarray, noise) -> tuple | None:
    """99 % power bandwidth of `sig` (noise already subtracted), counting only bins
    that stand at least 3 dB below the noise or higher, so leftover noise around
    zero does not widen it. Returns (low edge, high edge) in Hz."""
    p = np.where(sig > 0.5 * noise, sig, 0.0)
    total = p.sum()
    if total <= 0:
        return None
    c = np.cumsum(p) / total
    lo = int(np.searchsorted(c, 0.005))
    hi = int(np.searchsorted(c, 0.995))
    bw = freqs[1] - freqs[0]
    return float(freqs[lo] - bw / 2), float(freqs[min(hi, len(freqs) - 1)] + bw / 2)


def band_taps(fs: float, lo: float, hi: float):
    """Complex FIR passing lo..hi Hz (with a 15 % margin), or None if that is most of the band."""
    from scipy.signal import firwin
    width = (hi - lo) * 1.3 + 4 * fs / NFFT
    if width >= 0.8 * fs:
        return None
    trans = max(0.1 * width, 0.02 * fs)
    ntaps = int(min(511, 4 * fs / trans)) | 1
    h = firwin(ntaps, width / 2, fs=fs)
    n = np.arange(ntaps) - ntaps // 2
    return (h * np.exp(2j * np.pi * (lo + hi) / 2 * n / fs)).astype(np.complex64)


def _filtered(src: IQSource, start: int, count: int, taps) -> np.ndarray:
    """Samples start..start+count, band-filtered, aligned with the input (zero-phase)."""
    if taps is None:
        return src.read(start, count)
    from scipy.signal import fftconvolve
    h = len(taps) // 2
    lo = start - h
    x = src.read(lo, count + 2 * h)
    pre = max(0, -lo)                      # zero-pad before the file starts
    post = count + 2 * h - pre - len(x)    # ...and after it ends
    if pre or post > 0:
        x = np.concatenate([np.zeros(pre, np.complex64), x, np.zeros(max(0, post), np.complex64)])
    return fftconvolve(x, taps, mode="valid").astype(np.complex64)


def _period_autocorr(series: np.ndarray, dt: float, min_s: float) -> dict | None:
    """Strongest repeat in an envelope series, from its autocorrelation."""
    x = series - series.mean()
    n = len(x)
    if n < 8 or not np.any(x):
        return None
    f = np.fft.rfft(x, 2 * n)
    ac = np.fft.irfft(f * np.conj(f))[:n]
    ac /= ac[0]
    lo, hi = max(1, int(min_s / dt)), n // 2
    if hi <= lo + 2:
        return None
    seg = ac[lo:hi]
    k = int(np.argmax(seg)) + lo
    if ac[k] < 0.3 or k == lo:      # at the search edge: a decay, not a repeat
        return None
    d = 0.0
    a, b, c = ac[k - 1], ac[k], ac[k + 1]
    den = a - 2 * b + c
    if den < -1e-9:                 # parabolic refinement, only on a true peak; a flat top
        d = float(np.clip(0.5 * (a - c) / den, -0.5, 0.5))   # once sent it to -2.8e12 s
    return {"period_s": float((k + d) * dt), "correlation": float(ac[k])}


# ---------------------------------------------------------------- the passes

def pass_activity(src: IQSource, start: int, n: int) -> dict:
    """Pass 1: per-frame coarse spectra -> which frames hold a signal; ADC rails."""
    frames = n // NFFT
    coarse = np.empty((frames, COARSE), np.float32)
    rails = 0
    peak = 0.0
    per = CHUNK // NFFT
    for f0 in range(0, frames, per):
        k = min(per, frames - f0)
        r = src.raw(start + f0 * NFFT, k * NFFT)
        rails += src.rails(r)
        peak = max(peak, src.full_scale(r))
        x = src.read(start + f0 * NFFT, k * NFFT).reshape(k, NFFT)
        p = np.abs(np.fft.fftshift(np.fft.fft(x, axis=1), axes=1)) ** 2
        coarse[f0:f0 + k] = p.reshape(k, COARSE, NFFT // COARSE).sum(axis=2)
    # each bin's own median is its noise, unless a signal is on all the time and
    # IS the median: cap it at the receiver's general floor (25th pct across bins)
    med = np.median(coarse, axis=0)
    noise = np.minimum(med, np.percentile(med, 25))
    ratio = (coarse / np.maximum(noise, 1e-30)).max(axis=1)
    return {"frames": frames, "ratio": ratio, "active": ratio > ACTIVE_RATIO,
            "rails": rails, "peak_fs": peak}


def pass_spectrum(src: IQSource, start: int, act: dict) -> dict:
    """Pass 2: spectrum of active frames minus quiet frames -> occupied band."""
    fs = src.fs
    win = np.hanning(NFFT).astype(np.float32)
    active = np.flatnonzero(act["active"])
    quiet = np.flatnonzero(~act["active"])

    def mean_psd(idx, cap):
        if len(idx) > cap:
            idx = idx[np.linspace(0, len(idx) - 1, cap).astype(int)]
        acc = np.zeros(NFFT)
        for i in idx:
            acc += np.abs(np.fft.fftshift(np.fft.fft(src.read(start + int(i) * NFFT, NFFT) * win))) ** 2
        return acc / max(1, len(idx))

    freqs = _freqs(fs)
    psd_a = mean_psd(active, 50000)
    note = ""
    if len(quiet) >= 50:
        noise = mean_psd(quiet, 20000)
    else:
        # always on: no quiet frames to subtract, so take the floor from the
        # spectrum itself and patch the zero-IF DC spike out
        c = NFFT // 2
        psd_a[c - 2:c + 3] = np.median(np.r_[psd_a[c - 8:c - 2], psd_a[c + 3:c + 9]])
        noise = np.full(NFFT, np.percentile(psd_a, 10))
        note = "signal always on: noise floor taken from the spectrum's 10th percentile, DC bin patched"
    sig = psd_a - noise
    edges = obw99(freqs, sig, noise)
    return {"freqs": freqs, "psd_active": psd_a, "noise": noise, "edges": edges, "note": note}


def pass_envelope(src: IQSource, start: int, n: int, taps) -> tuple:
    """Pass 3: band-filtered mean power in ENV_BLOCK_S blocks over the whole slice."""
    B = max(1, int(round(src.fs * ENV_BLOCK_S)))
    nb = n // B
    env = np.empty(nb, np.float32)
    per = (CHUNK // B) * B
    for s in range(0, nb * B, per):
        k = min(per, nb * B - s)
        y = _filtered(src, start + s, k, taps)
        env[s // B: (s + k) // B] = (np.abs(y) ** 2).reshape(-1, B).mean(axis=1)
    return env, B


# ---------------------------------------------------------------- pulses

def find_pulses(src: IQSource, start: int, env: np.ndarray, B: int, taps) -> tuple:
    """Runs of the envelope over threshold, each refined at full sample rate."""
    fs = src.fs
    noise = float(np.median(env))
    above = env > noise * 10 ** (PULSE_THR_DB / 10)
    if not above.any():
        return [], noise
    d = np.diff(np.r_[0, above.astype(np.int8), 0])
    s, e = np.flatnonzero(d == 1), np.flatnonzero(d == -1)
    # merge runs split by short dropouts (a weak pulse breaks into fragments)
    join = max(1, int(round(MERGE_GAP_S * fs / B)))
    keep_s, keep_e = [s[0]], [e[0]]
    for a, b in zip(s[1:], e[1:]):
        if a - keep_e[-1] <= join:
            keep_e[-1] = b
        else:
            keep_s.append(a)
            keep_e.append(b)
    pulses = []
    pad = 3 * B
    for b0, b1 in zip(keep_s, keep_e):
        if len(pulses) >= 50000:
            break
        lo = int(b0) * B - pad
        y = _filtered(src, start + lo, (int(b1) - int(b0)) * B + 2 * pad, taps)
        p = np.abs(y) ** 2
        core = p[pad: len(p) - pad]
        if core.size == 0:
            continue
        peak = float(np.percentile(core, 90))
        if peak <= noise:
            continue
        snr = 10 * math.log10((peak - noise) / noise)
        ps = np.convolve(p, np.ones(3) / 3, "same")
        widths = {}
        for db in WIDTH_LEVELS_DB:
            if snr < db + 6:          # the level would sit in the noise
                continue
            e_ = _crossings(ps, noise + (peak - noise) * 10 ** (-db / 10))
            if e_:
                widths[db] = e_
        if 6 not in widths:
            continue
        lead, trail = widths[6]
        pulses.append({
            "t": (start + lo + lead) / fs,          # leading edge (50 % amplitude), s from file start
            "width": (trail - lead) / fs,           # 50 % amplitude width: IEEE 181 pulse duration
            "widths": {db: (b - a) / fs for db, (a, b) in widths.items()},
            "snr_db": snr,
            "peak": peak,
            "_y": (lo, y, widths),
        })
    # a fragment that survived the merge overlaps its parent: keep the stronger
    pulses.sort(key=lambda q: q["t"])
    out = []
    for q in pulses:
        if out and q["t"] < out[-1]["t"] + out[-1]["width"] + MERGE_GAP_S:
            if q["peak"] > out[-1]["peak"]:
                out[-1] = q
            continue
        out.append(q)
    return out, noise


def _crossings(p: np.ndarray, level: float):
    """First rising and last falling crossing of `level`, interpolated (sample units)."""
    idx = np.flatnonzero(p >= level)
    if idx.size == 0:
        return None
    i, j = int(idx[0]), int(idx[-1])
    lead = i - 1 + (level - p[i - 1]) / (p[i] - p[i - 1]) if i > 0 and p[i] != p[i - 1] else float(i)
    trail = j + (p[j] - level) / (p[j] - p[j + 1]) if j + 1 < len(p) and p[j] != p[j + 1] else float(j)
    return float(lead), float(trail)


def chirp_of(pulse: dict, fs: float) -> dict | None:
    """Instantaneous frequency across the pulse: a straight-line fit over the
    -10 dB extent (or -6 dB on weaker pulses), the actual start and end frequency
    (median over the first and last 3 % of the 50 %-amplitude extent), and how much
    steeper the sweep is at the ends than in the middle (1 for a linear chirp)."""
    lo, y, widths = pulse["_y"]
    lead, trail = widths.get(10, widths[6])
    a, b = int(math.ceil(lead)) + 1, int(math.floor(trail)) - 1
    if b - a < 16:
        return None
    seg = y[a:b + 1]
    z = seg[1:] * np.conj(seg[:-1])
    f = np.angle(z) * fs / (2 * np.pi)
    t = (np.arange(len(f)) + a + 0.5 - lead) / fs        # seconds from the leading edge
    w = trail - lead
    wts = np.sqrt(np.abs(z))
    keep = np.ones(len(f), bool)
    for _ in range(3):
        coef = np.polyfit(t[keep], f[keep], 1, w=wts[keep])
        res = f - np.polyval(coef, t)
        mad = np.median(np.abs(res[keep])) * 1.4826 or 1.0
        keep = np.abs(res) < 3 * mad
        if keep.sum() < 16:
            return None
    slope, icpt = coef
    l6, t6 = widths[6]
    i0, i1 = max(0, int(l6 - a)), max(1, int(t6 - a))   # the 50 % extent, in f's sample units
    # a short window at each edge: an S-shaped chirp moves fastest there, so a
    # wide window would average the true end frequencies away
    edge = max(4, int(0.03 * (i1 - i0)))
    first, last = f[i0: i0 + edge], f[max(0, i1 - edge): i1]

    def slope_of(u0, u1):
        sel = (t >= u0 * w / fs) & (t <= u1 * w / fs)
        return np.polyfit(t[sel], f[sel], 1, w=wts[sel])[0] if sel.sum() > 4 else np.nan
    mid = slope_of(0.25, 0.75)
    ends = np.nanmean([slope_of(0.0, 0.2), slope_of(0.8, 1.0)])
    ok = np.isfinite(mid) and np.isfinite(ends) and mid != 0
    return {"fit_start_hz": float(icpt), "fit_end_hz": float(icpt + slope * w / fs),
            "rate_hz_per_s": float(slope), "rms_hz": float(np.sqrt(np.mean(res[keep] ** 2))),
            "start_hz": float(np.median(first)) if first.size else float(icpt),
            "end_hz": float(np.median(last)) if last.size else float(icpt + slope * w / fs),
            "end_to_mid_slope": float(ends / mid) if ok else None}


def pri_levels(diffs: np.ndarray) -> list:
    """Cluster pulse-to-pulse intervals into levels; drop the sums of two
    (a missed pulse between them). Returns [{'pri_s', 'count'}], ascending."""
    if diffs.size == 0:
        return []
    v = np.sort(diffs)
    groups, cur = [], [v[0]]
    for x in v[1:]:
        if x - cur[-1] <= max(5e-6, 0.005 * x):
            cur.append(x)
        else:
            groups.append(cur)
            cur = [x]
    groups.append(cur)
    need = max(3, int(0.02 * len(v)))
    levels = []
    for g in groups:
        if len(g) < need:
            continue
        m = float(np.median(g))
        if any(abs(a["pri_s"] + b["pri_s"] - m) < 0.01 * m for a in levels for b in levels):
            continue
        levels.append({"pri_s": m, "count": len(g)})
    return levels


def stagger_runs(seq: list, levels: list) -> dict:
    """Typical number of consecutive intervals at each level, from complete runs only."""
    labels = []
    for d in seq:
        k = None
        for i, L in enumerate(levels):
            if abs(d - L["pri_s"]) <= max(5e-6, 0.005 * L["pri_s"]):
                k = i
        labels.append(k)
    runs = {i: [] for i in range(len(levels))}
    i = 0
    while i < len(labels):
        j = i
        while j < len(labels) and labels[j] == labels[i]:
            j += 1
        bounded = i > 0 and j < len(labels) and labels[i - 1] is not None and labels[j] is not None
        if labels[i] is not None and bounded:
            runs[labels[i]].append(j - i)
        i = j
    out = {}
    for k, r in runs.items():
        if r:
            vals, cnt = np.unique(r, return_counts=True)
            out[k] = int(vals[np.argmax(cnt)])
    return out


def analyse_pulses(pulses: list, fs: float, duration_s: float) -> dict:
    good = [p for p in pulses if p["snr_db"] >= GOOD_PULSE_SNR_DB] or pulses
    widths = np.array([p["width"] for p in good])
    t = np.array([p["t"] for p in pulses])
    levels_w = {}
    for db in WIDTH_LEVELS_DB:
        v = [p["widths"][db] for p in good if db in p["widths"]]
        if v:
            levels_w[db] = float(np.median(v))
    out = {"count": len(pulses), "count_good": len(good),
           "width_s": float(np.median(widths)),
           "width_iqr_s": [float(np.percentile(widths, 25)), float(np.percentile(widths, 75))],
           "width_at_db_s": levels_w,
           "snr_db_max": float(max(p["snr_db"] for p in pulses)),
           "snr_db_median": float(np.median([p["snr_db"] for p in pulses])),
           "duty_overall": float(sum(p["width"] for p in pulses) / duration_s)}

    # bursts: groups of pulses separated by long gaps (a scanning beam passing)
    d = np.diff(t)
    med = float(np.median(d)) if d.size else 0.0
    gap = max(20 * med, 0.1)
    cut = np.flatnonzero(d > gap) + 1
    bursts = np.split(np.arange(len(pulses)), cut)
    in_burst = np.concatenate([np.diff(t[b]) for b in bursts if len(b) > 1]) if len(bursts) else np.array([])
    seq = [x for b in bursts if len(b) > 1 for x in list(np.diff(t[b])) + [None]][:-1] if bursts else []

    levels = pri_levels(in_burst)
    if levels:
        runs = stagger_runs([x if x is not None else -1.0 for x in seq], levels)
        main = [L for L in levels if L["count"] >= 0.1 * sum(x["count"] for x in levels)]
        for i, L in enumerate(levels):
            L["run"] = runs.get(i)
        out["pri"] = levels
        out["staggered"] = len(main) >= 2
        mean_pri = float(np.average([L["pri_s"] for L in levels], weights=[L["count"] for L in levels]))
        out["duty_in_burst"] = out["width_s"] / mean_pri

    # rotation: centres of the beam's passes from the strongest pulses only
    # (sidelobes smear the weak ones over seconds), fitted to whole turns
    snr = np.array([p["snr_db"] for p in pulses])
    top = np.flatnonzero(snr >= max(snr.max() - MAINLOBE_DB, GOOD_PULSE_SNR_DB))
    cut2 = np.flatnonzero(np.diff(t[top]) > gap) + 1
    full = [top[b] for b in np.split(np.arange(len(top)), cut2) if len(b) >= 3]
    out["bursts"] = len(full)
    if len(full) >= 3:
        centres = np.array([np.average(t[b], weights=[pulses[i]["peak"] for i in b]) for b in full])
        iv = np.diff(centres)
        p0 = float(np.median(iv))
        turns = np.round((centres - centres[0]) / p0)
        if len(set(turns)) >= 2:
            P, c0 = np.polyfit(turns, centres, 1)
            res = centres - (c0 + P * turns)
            out["rotation"] = {"period_s": float(P), "bursts": len(full),
                               "fit_rms_s": float(np.sqrt(np.mean(res ** 2))),
                               "intervals_s": [float(x) for x in iv],
                               "rpm": float(60 / P) if P > 0 else None,
                               "dwell_s": float(np.median([t[b[-1]] - t[b[0]] for b in full]))}

    chirps = [c for c in (chirp_of(p, fs) for p in sorted(good, key=lambda p: -p["snr_db"])[:200]) if c]
    if chirps:
        st = np.array([c["start_hz"] for c in chirps])
        en = np.array([c["end_hz"] for c in chirps])
        sweep = float(np.median(en - st))
        f_s = float(np.median([c["fit_start_hz"] for c in chirps]))
        f_e = float(np.median([c["fit_end_hz"] for c in chirps]))
        rms = float(np.median([c["rms_hz"] for c in chirps]))
        ratio = [c["end_to_mid_slope"] for c in chirps if c["end_to_mid_slope"] is not None]
        ratio = float(np.median(ratio)) if ratio else None
        w = out["width_s"]
        swept = abs(sweep) > max(50e3, 2 / w)
        out["chirp"] = {"pulses_fitted": len(chirps),
                        "start_hz": float(np.median(st)), "end_hz": float(np.median(en)),
                        "sweep_hz": sweep, "fit_start_hz": f_s, "fit_end_hz": f_e,
                        "fit_sweep_hz": f_e - f_s,
                        "rate_mhz_per_us": (f_e - f_s) / w / 1e12 if w else None,
                        "rms_hz": rms, "end_to_mid_slope": ratio, "swept": swept,
                        "linear": swept and rms < 0.1 * abs(sweep) and (ratio is None or ratio < 1.5),
                        "time_bandwidth": abs(sweep) * w}
    return out


# ---------------------------------------------------------------- continuous signals

def continuous_features(src: IQSource, start: int, act: dict, centre: float, edges) -> dict:
    """Modulation family of a signal that is on for long stretches."""
    fs = src.fs
    n = min(1 << 21, src.n - start)
    nf = n // NFFT
    if nf < 1:
        return {"family": "unclassified", "why": "too short"}
    a = act["active"].astype(float)
    if len(a) > nf:   # slide a window to where the signal is on most
        s = np.convolve(a, np.ones(nf), "valid")
        f0 = int(np.argmax(s))
    else:
        f0 = 0
    x = src.read(start + f0 * NFFT, nf * NFFT)

    # fine spectrum for a fine bandwidth (CW needs Hz, not kHz)
    nfft = int(min(65536, 2 ** int(math.log2(max(len(x) // 8, 1024)))))
    k = len(x) // nfft
    win = np.hanning(nfft)
    psd = np.mean(np.abs(np.fft.fftshift(np.fft.fft(x[:k * nfft].reshape(k, nfft) * win, axis=1), axes=1)) ** 2, axis=0)
    freqs = _freqs(fs, nfft)
    noise = float(np.percentile(psd, 10))
    sig = psd - noise
    e = obw99(freqs, sig, noise) or edges
    obw = e[1] - e[0]
    fc = (e[0] + e[1]) / 2
    inb = (freqs >= e[0]) & (freqs <= e[1])
    snr_db = 10 * math.log10(max(sig[inb].sum(), 1e-30) / (noise * max(inb.sum(), 1)))
    out = {"obw_hz": obw, "centre_hz": fc, "snr_db": snr_db}
    if snr_db < 6:
        return {**out, "family": "too weak to classify", "why": f"in-band SNR {snr_db:.1f} dB"}

    # move to baseband and cut to the band
    t = np.arange(len(x))
    y = x * np.exp(-2j * np.pi * fc * t / fs)
    Y = np.fft.fft(y)
    ff = np.fft.fftfreq(len(y), 1 / fs)
    Y[np.abs(ff) > max(obw * 0.6, 2 * fs / len(y))] = 0
    y = np.fft.ifft(Y)
    env = np.abs(y)
    cv = float(env.std() / env.mean()) if env.mean() > 0 else 0.0
    out["envelope_cv"] = cv

    if obw < max(1e3, 5 * fs / nfft) and cv < 0.3:
        return {**out, "family": "CW", "why": f"{obw:.0f} Hz wide, steady envelope"}

    # instantaneous frequency, lightly smoothed
    z = y[1:] * np.conj(y[:-1])
    fi = np.angle(z) * fs / (2 * np.pi)
    m = max(1, int(fs / obw / 2))
    fi = np.convolve(fi, np.ones(m) / m, "same")
    lo, hi = np.percentile(fi, [1, 99])
    span = hi - lo
    if span > 0:
        h, be = np.histogram(fi, bins=64, range=(lo, hi))
        hs = np.convolve(h, np.ones(3) / 3, "same")
        peaks = [i for i in range(1, 63) if hs[i] >= hs[i - 1] and hs[i] > hs[i + 1] and hs[i] > 0.3 * hs.max()]
        merged = []
        for i in peaks:
            if not merged or i - merged[-1] > 8:
                merged.append(i)
        if len(merged) >= 2:
            centres = (be[merged] + be[np.array(merged) + 1]) / 2
            near = np.min(np.abs(fi[:, None] - centres[None, :]), axis=1) < 0.15 * span
            conc = float(near.mean())
            if conc >= 0.75:
                lvl = np.argmin(np.abs(fi[:, None] - centres[None, :]), axis=1)
                tr = (np.diff(lvl) != 0).astype(float)
                rs = None
                if tr.sum() > 10:
                    T = np.abs(np.fft.rfft(tr - tr.mean()))
                    fr = np.fft.rfftfreq(len(tr), 1 / fs)
                    band = (fr >= 50) & (fr <= obw)
                    if band.any():
                        i = int(np.argmax(np.where(band, T, 0)))
                        rs = float(fr[i])
                shift = float(centres[-1] - centres[0])
                return {**out, "family": f"FSK ({len(merged)}-level)", "symbol_rate_hz": rs,
                        "shift_hz": shift, "why": f"{conc:.0%} of samples sit on {len(merged)} frequencies"}

    # flat broadband: OFDM-like if the edges are steep, else noise-like
    if obw > 50e3:
        band = sig[inb]
        mid = band[int(0.1 * len(band)): int(0.9 * len(band)) or None]
        mid = mid[mid > 0]
        flat = float(np.exp(np.mean(np.log(mid))) / np.mean(mid)) if mid.size else 0.0
        out["flatness"] = flat
        if flat > 0.7:
            plateau = float(np.median(mid))
            binw = fs / nfft
            def edge_width(side):
                idx = np.flatnonzero(inb)
                i = idx[0] if side < 0 else idx[-1]
                steps = 0
                while 0 < i < nfft - 1 and sig[i] > plateau * 0.01:
                    i += side
                    steps += 1
                return steps * binw
            roll = (edge_width(-1) + edge_width(1)) / 2
            if roll < 0.1 * obw:
                return {**out, "family": "OFDM-like", "why": f"flat top ({flat:.2f}), edges fall 20 dB in {roll / 1e3:.1f} kHz"}
            return {**out, "family": "noise-like / broadband", "why": f"flat ({flat:.2f}) with soft edges"}

    if cv > 0.25:
        return {**out, "family": "AM-like", "why": f"envelope varies (cv {cv:.2f})"}
    return {**out, "family": "FM-like", "why": f"constant envelope (cv {cv:.2f}), frequency moves over {obw / 1e3:.1f} kHz"}


# ---------------------------------------------------------------- recipe

def recipe(fs_now: float, f0: float | None, centre: float, obw: float, res: dict) -> dict:
    """Settings for the next capture of this signal."""
    store = math.ceil(max(2.5 * obw, 10e3) / 1e3) * 1e3
    guard = max(20e3, 0.1 * obw)
    need = (obw + guard) / 0.4          # signal one side of DC, inside 80 % of the band
    rtl = next((r for r in RTL_RATES if r >= need), None)
    notes = []
    if rtl is not None:
        offset = obw / 2 + guard        # signal centre sits this far above the tuned frequency
    else:
        rtl = 2.4e6
        offset = max(0.0, 0.45 * rtl - obw / 2)
        inside = obw / 2 - offset
        notes.append(f"{obw / 1e6:.2f} MHz is too wide to keep clear of DC at a stable rate; "
                     f"the radio's full 2.4 MS/s is kept instead of a {store / 1e3:.0f} kS/s cut. "
                     f"At 2.4 MS/s the best offset still leaves DC {inside / 1e3:.0f} kHz inside the signal. "
                     "On the v5 (low-IF R860, no DC spike) this does not matter.")
    store = min(store, rtl)    # a cut cannot be wider than the capture
    rot = (res.get("pulses") or {}).get("rotation") or res.get("repeat")
    fam = res.get("family", "")
    if rot and rot.get("period_s"):
        dur = min(120.0, max(10.0, 7.5 * rot["period_s"]))
        notes.append(f"duration covers ~{dur / rot['period_s']:.1f} repeats of {rot['period_s']:.2f} s")
    elif fam == "CW":
        dur = 10.0
    else:
        dur = 30.0

    rails = res["clipping"]["rail_fraction"]
    peak_db = res["clipping"]["peak_dbfs"]
    snr = (res.get("pulses") or {}).get("snr_db_max") or (res.get("continuous") or {}).get("snr_db") or 0.0
    if rails > 1e-5 or peak_db > -0.5:
        gain = {"change_db": -6, "why": f"{rails:.2e} of I/Q values on the ADC rails (peak {peak_db:.1f} dBFS)"}
    elif snr < 15 and peak_db < -12:
        gain = {"change_db": +6, "why": f"peak SNR only {snr:.0f} dB with {-peak_db:.0f} dB of headroom"}
    elif snr < 15:
        gain = {"change_db": 0, "why": f"SNR {snr:.0f} dB is marginal but headroom is small; antenna or LNA, not gain"}
    else:
        gain = {"change_db": 0, "why": f"peak SNR {snr:.0f} dB, peak {peak_db:.1f} dBFS, no clipping"}
    return {"store_rate_hz": store, "rtl_rate_hz": rtl,
            "signal_hz": (f0 + centre) if f0 else None,
            "tune_hz": (f0 + centre - offset) if f0 else None,
            "signal_above_tune_hz": offset, "duration_s": dur, "gain": gain, "notes": notes}


# ---------------------------------------------------------------- top level

def characterise(path, rate=None, freq=None, fmt=None, start_s: float = 0.0,
                 duration_s: float | None = None) -> dict:
    src = open_iq(path, rate, freq, fmt)
    fs = src.fs
    start = int(start_s * fs)
    n = src.n - start if duration_s is None else min(int(duration_s * fs), src.n - start)
    if n < NFFT * 4:
        raise SystemExit("recording too short to characterise")
    dur = n / fs
    res = {"file": str(Path(path)), "format": src.origin, "sample_rate_hz": fs,
           "tuned_hz": src.f0, "start_s": start_s, "duration_s": dur}

    act = pass_activity(src, start, n)
    res["clipping"] = {"rail_fraction": act["rails"] / (2 * act["frames"] * NFFT),
                       "peak_dbfs": 20 * math.log10(max(act["peak_fs"], 1e-9))}
    frac = float(act["active"].mean())
    res["active_fraction"] = frac
    if not act["active"].any():
        res["family"] = "nothing found"
        res["summary"] = "no frame rose 6 dB above the noise"
        return res

    spec = pass_spectrum(src, start, act)
    if spec["edges"] is None:
        res["family"] = "nothing found"
        res["summary"] = "active frames carry no power above the quiet ones"
        return res
    lo, hi = spec["edges"]
    obw, centre = hi - lo, (lo + hi) / 2
    res["occupied"] = {"bandwidth_hz": obw, "low_hz": lo, "high_hz": hi,
                       "centre_offset_hz": centre, "note": spec["note"]}

    # repeat period of the activity envelope, in 10 ms bins (cross-check for pulses)
    per = max(1, int(round(0.01 * fs / NFFT)))
    m = len(act["ratio"]) // per
    series = act["active"][:m * per].reshape(m, per).mean(axis=1)
    rep = _period_autocorr(series, per * NFFT / fs, 0.1)
    if rep:
        res["repeat"] = rep

    taps = band_taps(fs, lo, hi)
    pulsed = False
    if frac < 0.5:
        env, B = pass_envelope(src, start, n, taps)
        pulses, _ = find_pulses(src, start, env, B, taps)
        if len(pulses) >= 8 and np.median([p["width"] for p in pulses]) < 5e-3:
            pulsed = True
            res["pulses"] = analyse_pulses(pulses, fs, dur)
    if pulsed:
        ch = res["pulses"].get("chirp")
        if ch and ch["linear"]:
            res["family"] = "pulsed, linear FM chirp (radar-like)"
        elif ch and ch["swept"]:
            res["family"] = "pulsed, non-linear FM chirp (radar-like)"
        elif ch:
            res["family"] = "pulsed, unmodulated carrier"
        else:
            res["family"] = "pulsed"
    else:
        cont = continuous_features(src, start, act, centre, spec["edges"])
        res["continuous"] = cont
        res["family"] = cont["family"]
    res["recipe"] = recipe(fs, src.f0, centre, obw, res)
    return res


# ---------------------------------------------------------------- output

def _us(s):
    return f"{s * 1e6:.1f} us"


def to_markdown(r: dict) -> str:
    L = [f"# Characterisation: {Path(r['file']).name}", ""]
    tuned = f"{r['tuned_hz'] / 1e6:.6f} MHz" if r.get("tuned_hz") else "unknown"
    L += [f"- **What it is:** {r['family']}",
          f"- Recording: {r['format']}, {r['sample_rate_hz'] / 1e6:g} MS/s, tuned {tuned}, "
          f"{r['duration_s']:.2f} s analysed from {r['start_s']:g} s",
          f"- On air in {r['active_fraction']:.2%} of 0.43 ms frames",
          f"- ADC: peak {r['clipping']['peak_dbfs']:.1f} dBFS, {r['clipping']['rail_fraction']:.2e} of values on the rails"]
    o = r.get("occupied")
    if o:
        L.append(f"- Occupied bandwidth (99 % power): **{o['bandwidth_hz'] / 1e3:.1f} kHz**, "
                 f"{o['low_hz'] / 1e3:+.1f} to {o['high_hz'] / 1e3:+.1f} kHz, centre offset {o['centre_offset_hz'] / 1e3:+.1f} kHz")
        if o.get("note"):
            L.append(f"  ({o['note']})")
    p = r.get("pulses")
    if p:
        L += ["", "## Pulses", "",
              f"- {p['count']} pulses ({p['count_good']} at {GOOD_PULSE_SNR_DB:g} dB SNR or more), "
              f"peak SNR {p['snr_db_max']:.1f} dB",
              f"- Width at 50 % amplitude (-6 dB): **{_us(p['width_s'])}** (middle half {_us(p['width_iqr_s'][0])} "
              f"to {_us(p['width_iqr_s'][1])}); " + ", ".join(f"-{db} dB {_us(v)}" for db, v in p.get("width_at_db_s", {}).items()),
              f"- Duty: {p['duty_overall']:.3%} overall" + (f", {p['duty_in_burst']:.2%} while the beam is on" if "duty_in_burst" in p else "")]
        if p.get("pri"):
            kind = "staggered" if p.get("staggered") else "single"
            tot = sum(x["count"] for x in p["pri"])
            parts = [f"{L_['pri_s'] * 1e6:.1f} us ({1 / L_['pri_s']:.0f} Hz) x{L_['count']}"
                     + (f", runs of {L_['run']}" if L_.get("run") else "")
                     + (" (minor)" if L_["count"] < 0.1 * tot else "") for L_ in p["pri"]]
            L.append(f"- PRI, {kind}: " + "; ".join(parts))
        ro = p.get("rotation")
        if ro:
            L.append(f"- Rotation: **{ro['period_s']:.3f} s** ({ro['rpm']:.2f} RPM) from {ro['bursts']} bursts, "
                     f"fit rms {ro['fit_rms_s'] * 1e3:.0f} ms, intervals {min(ro['intervals_s']):.2f} to {max(ro['intervals_s']):.2f} s, "
                     f"dwell {ro['dwell_s'] * 1e3:.0f} ms")
        c = p.get("chirp")
        if c:
            if c["linear"]:
                shape = "linear"
            elif c.get("end_to_mid_slope"):
                shape = f"NOT linear: {c['end_to_mid_slope']:.1f}x steeper at the ends than in the middle"
            else:
                shape = "NOT linear"
            L.append(f"- Chirp: **{c['start_hz'] / 1e3:+.0f} to {c['end_hz'] / 1e3:+.0f} kHz** end to end "
                     f"(sweep {c['sweep_hz'] / 1e6:+.3f} MHz, time-bandwidth {c['time_bandwidth']:.0f}); "
                     f"straight-line fit {c['fit_start_hz'] / 1e3:+.0f} to {c['fit_end_hz'] / 1e3:+.0f} kHz "
                     f"({c['rate_mhz_per_us'] * 1e3:+.2f} kHz/us, residual {c['rms_hz'] / 1e3:.0f} kHz rms); "
                     f"{shape}; from {c['pulses_fitted']} pulses")
    rep = r.get("repeat")
    if rep:
        L.append(f"- Envelope repeats every {rep['period_s']:.2f} s (autocorrelation {rep['correlation']:.2f})")
    c = r.get("continuous")
    if c:
        L += ["", "## Continuous signal", "", f"- {c['family']}: {c.get('why', '')}"]
        if c.get("symbol_rate_hz"):
            L.append(f"- Symbol rate ~{c['symbol_rate_hz']:.0f} Bd, shift {c['shift_hz'] / 1e3:.2f} kHz")
        if "snr_db" in c:
            L.append(f"- In-band SNR {c['snr_db']:.1f} dB, fine bandwidth {c['obw_hz']:.0f} Hz")
    rc = r.get("recipe")
    if rc:
        L += ["", "## Recipe for the next capture", ""]
        if rc.get("signal_hz"):
            L.append(f"- Signal centre {rc['signal_hz'] / 1e6:.6f} MHz; tune to **{rc['tune_hz'] / 1e6:.6f} MHz** "
                     f"(signal sits {rc['signal_above_tune_hz'] / 1e3:.0f} kHz above it)")
        L += [f"- Radio at {rc['rtl_rate_hz'] / 1e6:g} MS/s; keep a cut at **{rc['store_rate_hz'] / 1e3:g} kS/s**",
              f"- Duration **{rc['duration_s']:.0f} s**",
              f"- Gain: {'+' if rc['gain']['change_db'] > 0 else ''}{rc['gain']['change_db']} dB - {rc['gain']['why']}"]
        L += [f"- {x}" for x in rc["notes"]]
    return "\n".join(L) + "\n"


def _clean(o):
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items() if not str(k).startswith("_")}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    if isinstance(o, np.bool_):
        return bool(o)
    return o


def out_stem(path: Path) -> str:
    name = path.name
    for suf in (".sigmf-meta", ".sigmf-data"):
        if name.endswith(suf):
            return name[: -len(suf)]
    return path.stem


def main(argv=None, station=None, prog: str = "marsad characterise") -> int:
    ap = argparse.ArgumentParser(prog=prog, description="Measure what an IQ recording holds and how to capture it next time")
    ap.add_argument("recording", help="SDR++ baseband .wav, raw rtl_sdr uint8 IQ, or .sigmf-meta")
    ap.add_argument("--rate", type=float, help="sample rate in S/s (raw files; overrides nothing for WAV/SigMF)")
    ap.add_argument("--freq", type=float, help="tuned frequency in Hz (raw files; overrides the WAV name)")
    ap.add_argument("--format", choices=["cu8", "cs16", "cf32"], help="raw sample format (default cu8)")
    ap.add_argument("--start", type=float, default=0.0, help="seconds to skip at the start")
    ap.add_argument("--duration", type=float, help="seconds to analyse (default: all)")
    ap.add_argument("--out", help="folder for the .characterise.md / .json (default: beside the recording)")
    args = ap.parse_args(argv)

    path = Path(args.recording)
    if not path.exists():
        raise SystemExit(f"no such file: {path}")
    res = characterise(path, args.rate, args.freq, args.format, args.start, args.duration)
    out = Path(args.out) if args.out else path.parent
    out.mkdir(parents=True, exist_ok=True)
    stem = out_stem(path)
    md = to_markdown(res)
    (out / f"{stem}.characterise.md").write_text(md, encoding="utf-8")
    (out / f"{stem}.characterise.json").write_text(json.dumps(_clean(res), indent=2), encoding="utf-8")
    print(md)
    print(f"  wrote {out / (stem + '.characterise.md')}\n        {out / (stem + '.characterise.json')}")
    return 0
