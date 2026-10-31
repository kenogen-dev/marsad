"""The station's local web app: Scan, Builder, Archive, Roadmap.

    marsad live                 # serve on http://127.0.0.1:8731/
    marsad                      # the same, and open it in the browser

Read-only on the radio: the Scan tab tails whichever CSV rtl_power is writing
and never touches the SDR, so it is safe alongside a recording. rtl_power
block-buffers its output, so rows appear in bursts of roughly a dozen rather
than one at a time - the view lags the radio by a few seconds, never more.

One viewer on port 8731 is enough: it switches to a newer CSV as soon as one
appears, so a tab left open from the last run shows the next run too.
`--pin` stays on one file.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import statistics as st
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import chain, host, hunt, probe, room, rpf
from .config import Station
from .rtlpower import (CROP, FIR, RTL_MAX_SAMPLE_HZ, RTL_POWER_MAX_MHZ, TUNER_GAINS,
                       build_label, csv_bin_khz, fingerprint, parse_freq_spec)

WEB = Path(__file__).resolve().parent / "web"

# What /reports/<survey>/<file> will serve, and as what. Anything else is refused.
REPORT_TYPES = {".md": "text/plain; charset=utf-8", ".csv": "text/csv; charset=utf-8",
                ".png": "image/png", ".txt": "text/plain; charset=utf-8"}


def report_file(station: Station, url_path: str):
    """(path, content-type) for a file inside reports/, or None.

    Only plain names are accepted (letters, digits, dot, dash, underscore), and the
    resolved path must still be inside reports/, so a crafted URL cannot read
    anything else on the disk.
    """
    parts = url_path.strip("/").split("/")
    if len(parts) != 3 or parts[0] != "reports":
        return None
    for p in parts[1:]:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", p) or p.startswith("."):
            return None
    path = (station.reports / parts[1] / parts[2]).resolve()
    try:
        path.relative_to(station.reports.resolve())
    except ValueError:
        return None
    ctype = REPORT_TYPES.get(path.suffix.lower())
    if ctype is None or not path.is_file():
        return None
    return path, ctype


def _reading(x: str) -> float:
    """One dB reading, or NaN if it is not a finite number.

    rtl_power emits non-finite values on its strongest bins, and the MSVC build
    spells them `-nan(ind)`, which float() rejects. A bad bin must not cost the
    whole hop: the analyser learned this on 2026-10-18, when one unparseable
    value was discarding all 240 readings on the line and 40% of an FM survey.
    """
    try:
        v = float(x)
    except (ValueError, TypeError):
        return math.nan
    return v if math.isfinite(v) else math.nan


class Tailer:
    """Accumulates completed sweeps from a growing rtl_power CSV.

    A sweep is detected by the start frequency wrapping back down: rtl_power
    walks hops upward through the span, so the first row whose Hz_low is not
    above the previous one begins a new sweep.
    """

    def __init__(self, path: Path, cols: int, keep: int):
        self.path = path
        self.cols = cols
        self.keep = keep
        self.offset = 0
        self.partial = ""
        self.cur: list = []   # (freq_mhz, dB) this sweep
        self.last_lo = None
        self.lo = self.hi = None
        self.sweeps: list = []
        self.stamps: list = []
        self.medians: list = []     # whole-sweep median: the cage statistic, live
        self.t0 = None
        self.last_growth = time.time()    # for --exit-after-idle
        self.hops = None      # rows per sweep; the sidecar fills this in
        self.cur_rows = 0
        self.cur_stamp = None   # the first row's time: when this sweep started
        self.step_hz = None
        self.hop_centres: set = set()   # Hz, from each row's low/high: where zero-IF DC spikes sit
        self.maxhold: dict = {}         # kHz -> highest dB ever seen, at full resolution
        self.latest: dict = {}          # kHz -> dB in the latest complete sweep
        self._peaks = (None, [])        # (sweep count it was computed at, detections)
        self.sidecar = self._sidecar()    # must come AFTER hops, which it sets
        self.last_finish = None
        self.dropped = 0        # sweeps rolled out of the ring buffer
        self.rows = 0
        self.readings = 0
        self.nonfinite = 0    # saturation diagnostic: NaNs land on the strongest bins
        self.lock = threading.Lock()
        # The background loop and a request handler may both read the file (the live
        # hunt hop reads it on every request); one reader at a time keeps the offset sane.
        self.poll_lock = threading.Lock()
        self.finish_times: list = []    # wall clock of the last few completed sweeps

    def _sidecar(self) -> dict:
        """The .json beside the CSV carries the range and the target duration,
        which the CSV itself never states. Absent for a hand-run rtl_power."""
        try:
            d = json.loads(self.path.with_suffix(".json").read_text())
        except (OSError, ValueError):
            return {}
        rp = d.get("rtl_power", {})
        # Hop count from the sidecar, so the FIRST sweep can close itself.
        g = d.get("geometry") or {}
        if g.get("hops"):
            self.hops = int(g["hops"])
        if rp.get("freq_low_mhz") is not None:
            self.lo = float(rp["freq_low_mhz"])
            self.hi = float(rp.get("freq_high_mhz_effective") or rp.get("freq_high_mhz"))
        return {"duration_s": d.get("duration_requested_s"),
                "preset": d.get("preset"),
                "integration_s": rp.get("integration_s"),
                "gain_db": rp.get("gain_db"),
                "receiver": d.get("receiver"),
                "tag": (d.get("csv") or "").split("_")[3] if d.get("csv") else None}

    def _downsample(self, pairs) -> list:
        span = max(1e-9, self.hi - self.lo)
        row = [None] * self.cols
        for f, v in pairs:
            if v != v:            # NaN: a missing reading, never a maximum
                continue
            c = int((f - self.lo) / span * (self.cols - 1))
            if 0 <= c < self.cols and (row[c] is None or v > row[c]):
                row[c] = v
        return row

    def _row(self, line: str) -> None:
        parts = line.split(",")
        if len(parts) < 7:
            return
        try:
            lo = float(parts[2]); hi_hz = float(parts[3]); step = float(parts[4])
            vals = [_reading(v) for v in parts[6:] if v.strip()]
        except ValueError:
            return             # only a broken timestamp or frequency field drops a line
        self.rows += 1
        self.readings += len(vals)
        self.nonfinite += sum(1 for v in vals if v != v)
        stamp = parts[0].strip() + " " + parts[1].strip()
        if self.last_lo is not None and lo <= self.last_lo:
            self._finish(self.cur_stamp or stamp)
        if self.cur_rows == 0:
            self.cur_stamp = stamp
        self.last_lo = lo
        self.cur_rows += 1
        self.step_hz = step
        if self.hops is None or len(self.hop_centres) < self.hops:
            self.hop_centres.add(round((lo + hi_hz) / 2.0))   # the analyser's definition
        base = lo / 1e6
        step_mhz = step / 1e6
        for i, v in enumerate(vals):
            self.cur.append((base + i * step_mhz, v))
        # Close the sweep the moment it is full rather than waiting for the next
        # one to start: rtl_power writes a whole interval's rows at once.
        if self.hops and self.cur_rows >= self.hops:
            self._finish(self.cur_stamp or stamp)

    def _finish(self, stamp: str) -> None:
        if not self.cur:
            return
        if self.lo is None:
            self.lo = min(f for f, _ in self.cur)
            self.hi = max(f for f, _ in self.cur)
        # Max within each display column, not mean: a single narrow spur must
        # survive 22,896 bins being squeezed into ~1,200 pixels.
        self.sweeps.append(self._downsample(self.cur))
        latest = {}
        mh = self.maxhold
        for f, v in self.cur:
            k = int(round(f * 1e3))
            latest[k] = v
            if v > mh.get(k, -1e9):      # false for NaN, which is what we want
                mh[k] = v
        self.latest = latest
        self.stamps.append(stamp)
        # Median over the FULL resolution sweep, not the downsampled row.
        finite = [v for _, v in self.cur if v == v]
        self.medians.append(round(st.median(finite), 3) if finite else None)
        if self.t0 is None:
            self.t0 = time.time()
        if self.hops is None and self.cur_rows:
            self.hops = self.cur_rows
        self.cur = []
        self.cur_rows = 0
        self.last_finish = time.time()
        self.finish_times.append(self.last_finish)
        del self.finish_times[:-40]
        while len(self.sweeps) > self.keep:
            self.sweeps.pop(0); self.stamps.pop(0); self.medians.pop(0); self.dropped += 1

    def poll(self) -> None:
        with self.poll_lock:
            self._poll()

    def updates_per_s(self, window_s: float = 5.0):
        """Sweeps completed per second over the last few seconds, as they reached
        this server: the update rate the page can actually have."""
        with self.lock:
            t = list(self.finish_times)
        now = time.time()
        recent = [x for x in t if now - x <= window_s]
        if len(recent) < 2:
            return None
        span = max(recent[-1] - recent[0], 1e-6)
        return round((len(recent) - 1) / span, 2)

    def _poll(self) -> None:
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if size < self.offset:        # file replaced under us
            self.offset = 0; self.partial = ""
        if size == self.offset:
            return
        self.last_growth = time.time()
        with open(self.path, "r", errors="replace") as fh:
            fh.seek(self.offset)
            chunk = fh.read()
            self.offset = fh.tell()
        with self.lock:
            data = self.partial + chunk
            lines = data.split("\n")
            self.partial = lines.pop()     # last piece may be mid-line
            for ln in lines:
                if ln.strip():
                    self._row(ln)

    def peaks(self, threshold_db: float, floor_window_mhz: float, families, readout) -> list:
        """Detections on the full-resolution max-hold: the analyser's method in miniature.

        Floor: the latest complete sweep, as block medians a quarter of the floor
        window wide, then the median of the five blocks around each point, so a
        narrow line cannot lift the floor under itself. Detection: max-hold at
        least threshold_db above that floor, contiguous bins grouped into one peak,
        width measured 6 dB down. Tags: the station's spur families and the hop
        centres at the analyser's tolerance (1.5 bins, at least 10 kHz), plus the
        hover readout's families as a weaker "near" tag. Recomputed once per sweep.
        The report is the authority; this is its live preview.
        """
        n = self.dropped + len(self.sweeps)
        if self._peaks[0] == n:
            return self._peaks[1]
        with self.lock:
            latest = self.latest                 # replaced whole, never mutated
            mh = dict(self.maxhold)
            step = self.step_hz
            hc = sorted(self.hop_centres)
        if not latest or not step:
            return []
        keys = sorted(latest)
        lo_k = keys[0]
        bw = max(1, int(round(floor_window_mhz * 1e3 / 4)))     # block width, kHz
        blocks: dict = {}
        for k in keys:
            v = latest[k]
            if v == v:                                           # skip NaN
                blocks.setdefault((k - lo_k) // bw, []).append(v)
        bmed = {b: st.median(vs) for b, vs in blocks.items()}
        floor = {}
        for b in bmed:
            vs = [bmed[j] for j in range(b - 2, b + 3) if j in bmed]
            floor[b] = st.median(vs)
        step_k = step / 1e3
        tol_k = max(step * 1.5, 10e3) / 1e3
        hc_k = [h / 1e3 for h in hc]
        hop_k = st.median([b - a for a, b in zip(hc_k, hc_k[1:])]) if len(hc_k) > 1 else 0.0
        snr = []
        for k in keys:
            f = floor.get((k - lo_k) // bw)
            v = mh.get(k)
            snr.append(None if (f is None or v is None or v != v) else v - f)
        out = []
        N = len(keys)
        i = 0
        while i < N:
            if snr[i] is None or snr[i] < threshold_db:
                i += 1
                continue
            j = i
            while (j + 1 < N and snr[j + 1] is not None and snr[j + 1] >= threshold_db
                   and keys[j + 1] - keys[j] <= 2 * step_k):
                j += 1
            p = max(range(i, j + 1), key=lambda t: snr[t])
            thr = mh[keys[p]] - 6
            a = p
            while a > 0 and mh.get(keys[a - 1], -1e9) >= thr and keys[a] - keys[a - 1] <= 2 * step_k:
                a -= 1
            b = p
            while b < N - 1 and mh.get(keys[b + 1], -1e9) >= thr and keys[b + 1] - keys[b] <= 2 * step_k:
                b += 1
            width_k = (b - a + 1) * step_k
            f_mhz = keys[p] / 1e3
            tags = []
            for base, label in families:
                m = round(f_mhz / base)
                if m >= 1 and abs(f_mhz - m * base) * 1e3 <= tol_k:
                    tags.append(f"{label} (x{m})")
            # A lone sub-MHz comb match is weak evidence (7% of arbitrary lines land on
            # the 600 kHz comb by chance), so it keeps its tag but not the spur colour.
            kind = "spur" if any(base >= 1.0 and round(f_mhz / base) >= 1
                                 and abs(f_mhz - round(f_mhz / base) * base) * 1e3 <= tol_k
                                 for base, _ in families) else "signal"
            if not tags:
                for base in readout:
                    m = round(f_mhz / base)
                    if m >= 1 and abs(f_mhz - m * base) * 1e3 <= tol_k:
                        tags.append(f"near {base} MHz x {m} (readout family, weak evidence)")
                        kind = "spur"
                        break
            if hc_k and min(abs(h - keys[p]) for h in hc_k) <= tol_k:
                tags.append("hop centre: zero-IF DC artefact, likely not real")
                if kind == "signal":
                    kind = "artefact"
            if hop_k and width_k >= 0.8 * hop_k:
                tags.insert(0, f"hop-wide ({width_k / hop_k:.1f} hops): impulse, not a signal")
                kind = "artefact"
            out.append({"f": round(f_mhz, 4), "db": round(mh[keys[p]], 1),
                        "snr": round(snr[p], 1), "w": round(width_k, 1),
                        "kind": kind, "tags": "; ".join(tags)})
            i = j + 1
        self._peaks = (n, out)
        return out

    def payload(self, since: int) -> dict:
        # The recorder writes finished_local into the sidecar when rtl_power exits.
        # Re-read it, at most every 10 s, until it appears, so the page can say the
        # scan is over.
        if "finished_local" not in self.sidecar and time.time() - getattr(self, "_sc_at", 0) > 10:
            self._sc_at = time.time()
            self.sidecar = self._sidecar()
        with self.lock:
            first = self.dropped
            start = max(0, since - first)
            rows = self.sweeps[start:]
            done = self.dropped + len(self.sweeps)
            # Where rtl_power is right now, estimated from the clock: it walks
            # the span linearly across one interval and dumps the sweep at the end.
            pct = None
            integ = self.sidecar.get("integration_s")
            if integ and self.last_finish:
                pct = max(0.0, min(1.0, (time.time() - self.last_finish) / float(integ)))
            elif self.cur and self.lo is not None and self.hi:
                pct = max(0.0, min(1.0, (max(f for f, _ in self.cur) - self.lo)
                                   / max(1e-9, self.hi - self.lo)))
            cur_hi = None
            if pct is not None and self.lo is not None and self.hi:
                cur_hi = self.lo + pct * (self.hi - self.lo)
            eta = None
            dur = self.sidecar.get("duration_s")
            if dur and integ and done:
                eta = max(0, int(dur - (done + (pct or 0)) * integ))
            return {
                "file": self.path.name,
                "lo": self.lo, "hi": self.hi, "cols": self.cols,
                "total": done,
                "from": first + start,
                "rows": rows,
                "stamps": self.stamps[start:],
                "medians": self.medians[start:],
                "partial": self._downsample(self.cur) if (self.cur and self.lo is not None) else None,
                "cursor": cur_hi, "cursor_pct": pct, "cursor_est": True,
                "hops": self.hops,
                "hop_centres_mhz": [h / 1e6 for h in sorted(self.hop_centres)] if since == 0 else None,
                "step_khz": self.step_hz / 1e3 if self.step_hz else None,
                "eta_s": eta,
                "expected": int(dur / integ) if (dur and integ) else None,
                "meta": self.sidecar,
                "parsed_rows": self.rows,
                "nonfinite_pct": round(100.0 * self.nonfinite / self.readings, 2) if self.readings else 0.0,
                "bytes": self.offset,
            }



def _utc_to_local(text: str) -> str:
    """'2026-10-22 05:44:17 UTC' (what rtl_power_fftw writes) -> local 'YYYY-MM-DD HH:MM:SS'."""
    try:
        t = dt.datetime.strptime(text.replace(" UTC", "").strip(), "%Y-%m-%d %H:%M:%S")
        return t.replace(tzinfo=dt.timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return text


class DatTailer(Tailer):
    """Accumulates completed passes from a growing rtl_power_fftw .dat file.

    The format is one block per hop: five `#` header lines (acquisition start and
    end in UTC), N lines of `frequency_Hz power_dB`, then a blank line. With -e
    the program writes the extra blank line that ends a pass only once, at the
    very end, so passes are found the way sweeps are for rtl_power: the first
    block whose lowest frequency is not above the previous block's begins a new
    pass. That is the only exact rule: hops drop out as the tuner refuses them,
    so a count cannot close a pass early; the pass in progress is drawn live anyway.

    The sidecar's crop is applied as each block arrives - int(N*crop/2) bins off
    each edge, rtl_power's own arithmetic, the same cut `marsad analyze` makes -
    so the live picture is the report's picture. The hop centre is the middle
    bin of the uncropped block. Levels are the engine's own scale (a power
    spectral density), not rtl_power's; the payload says so.
    """

    def __init__(self, path: Path, cols: int, keep: int):
        self.crop = 0.0
        self.planned = None             # hops the plan asked for
        self.refused: dict = {}         # Hz -> True: "Unable to tune to" in the .log
        self.rate_hz = None
        self.ended = False              # "Session duration elapsed." seen in the .log
        self.log_path = path.with_suffix(".log")
        self.log_offset = 0
        self.start_epoch = None
        self._blk_f: list = []
        self._blk_v: list = []
        self._blk_stamp = None
        self._pass_stamp = None
        super().__init__(path, cols, keep)

    def _sidecar(self) -> dict:
        try:
            d = json.loads(self.path.with_suffix(".json").read_text())
        except (OSError, ValueError):
            return {"engine": "rtl_power_fftw", "integration_s": None}
        rp = d.get("rtl_power_fftw", {}) or {}
        g = d.get("geometry") or {}
        if g.get("hops"):
            self.planned = int(g["hops"])
            self.hops = self.planned
        self.crop = float(rp.get("crop") or 0.0)
        self.rate_hz = rp.get("rate_hz_actual") or rp.get("rate_hz")
        cov = g.get("covered_mhz")
        if cov and len(cov) == 2:
            self.lo, self.hi = float(cov[0]), float(cov[1])
        elif rp.get("freq_low_mhz") is not None:
            self.lo, self.hi = float(rp["freq_low_mhz"]), float(rp["freq_high_mhz"])
        try:
            self.start_epoch = dt.datetime.fromisoformat(d["started_local"]).timestamp()
        except (KeyError, ValueError, TypeError):
            pass
        try:
            fp = rpf.fingerprint(d, rp.get("bin_khz_actual"))
        except (KeyError, TypeError, ValueError):
            fp = None
        meta = {"engine": "rtl_power_fftw",
                "duration_s": d.get("duration_requested_s"),
                "preset": d.get("preset"),
                "integration_s": None,          # per-hop, not per-sweep: the cursor comes from the data
                "time_per_hop_s": rp.get("time_per_hop_s"),
                "bins": rp.get("bins"), "rate_hz": self.rate_hz, "crop": self.crop,
                "gain_db": rp.get("gain_db"),
                "receiver": d.get("receiver"),
                "tag": ((d.get("csv") or "").split("_") + [None] * 4)[3],
                "fingerprint": fp,
                "pass_s": g.get("sweep_s"),
                "expected": g.get("sweeps"),
                "stop_file": d.get("stop_file"),
                "scale": "rtl_power_fftw: power spectral density on its own scale, about 26 dB "
                         "below rtl_power's 50 kHz scale at 2.4 MS/s (measured 2026-10-22)"}
        # Only present once the recorder has written it, so the base class keeps
        # re-reading the sidecar until the run is over.
        if d.get("finished_local"):
            meta["finished_local"] = d["finished_local"]
        return meta

    def _poll_log(self) -> None:
        """Read what the program said since last time: refused hops, the actual
        sample rate, and whether the session has ended."""
        try:
            size = self.log_path.stat().st_size
        except OSError:
            return
        if size <= self.log_offset:
            return
        with open(self.log_path, "r", errors="replace") as fh:
            fh.seek(self.log_offset)
            text = fh.read()
            self.log_offset = fh.tell()
        for m in re.finditer(r"Unable to tune to\s*(\d+)", text):
            self.refused[int(m.group(1))] = True
        m = re.search(r"Actual sample rate:\s*(\d+)", text)
        if m:
            self.rate_hz = int(m.group(1))
        if "Session duration elapsed" in text:
            self.ended = True

    def _poll(self) -> None:
        self._poll_log()
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if size < self.offset:
            self.offset = 0; self.partial = ""
        if size == self.offset:
            with self.lock:
                self._close_if_over()
            return
        self.last_growth = time.time()
        with open(self.path, "r", errors="replace") as fh:
            fh.seek(self.offset)
            chunk = fh.read()
            self.offset = fh.tell()
        with self.lock:
            data = self.partial + chunk
            lines = data.split("\n")
            self.partial = lines.pop()
            for ln in lines:
                self._line(ln)       # blank lines matter here: they close a block
            self._close_if_over()

    def _close_if_over(self) -> None:
        """Once the program has said the session is over (or the recorder has
        written finished_local), the last pass has no successor to close it by the
        frequency wrap, so close it here. A run that died mid-pass keeps its last
        blocks as the in-progress trace, which is what they are."""
        if self.cur and (self.ended or self.sidecar.get("finished_local")) and not self._blk_f:
            self._finish(self._pass_stamp or "")

    def _line(self, ln: str) -> None:
        s = ln.strip()
        if not s:
            self._block_done()
            return
        if s.startswith("#"):
            m = s.partition("Acquisition start:")[2].strip()
            if m:
                self._blk_stamp = _utc_to_local(m)
            return
        parts = s.split()
        if len(parts) < 2:
            return
        try:
            f = float(parts[0])
        except ValueError:
            return
        self._blk_f.append(f)
        self._blk_v.append(_reading(parts[1]))

    def _expected(self) -> int:
        return max(0, (self.hops or 0) - len(self.refused))

    def _block_done(self) -> None:
        n = len(self._blk_f)
        if n == 0:
            return
        f, v = self._blk_f, self._blk_v
        self._blk_f, self._blk_v = [], []
        self.rows += 1
        self.readings += n
        self.nonfinite += sum(1 for x in v if x != x)
        centre = f[n // 2]
        k = int(n * self.crop * 0.5) if self.crop > 0 else 0
        if k and n - 2 * k >= 2:
            f, v = f[k:n - k], v[k:n - k]
        lo = f[0]
        if self.last_lo is not None and lo <= self.last_lo:
            self._finish(self._pass_stamp or "")
        if self.cur_rows == 0:
            self._pass_stamp = self._blk_stamp
        self.last_lo = lo
        self.cur_rows += 1
        if len(f) > 1:
            self.step_hz = (f[-1] - f[0]) / (len(f) - 1)
        if self.hops is None or len(self.hop_centres) < self.hops:
            self.hop_centres.add(round(centre))
        self.cur.extend((fi / 1e6, vi) for fi, vi in zip(f, v))
        # A one-hop plan (the live hunt hop) is exact: every block is a whole pass,
        # so close it now instead of waiting for the next block to wrap it.
        if self.planned == 1:
            self._finish(self._pass_stamp or "")
        # No early close on a hop count: hops drop out as the tuner refuses them
        # (tonight's run lost eight, one at a time), so only the frequency wrap is
        # exact. The pass in progress is on screen anyway, as the partial trace.

    def payload(self, since: int) -> dict:
        d = super().payload(since)
        with self.lock:
            cur_top = max((f for f, _ in self.cur), default=None)
            refused = sorted(self.refused)
            tuned = len(self.hop_centres)
        half = float(self.rate_hz or 2.4e6) * (1.0 - self.crop) / 2.0 / 1e6
        finished = bool(self.sidecar.get("finished_local")) or self.ended
        d["engine"] = "rtl_power_fftw"
        d["planned_hops"] = self.planned
        d["tuned_hops"] = tuned
        d["refused_mhz"] = [[round(h / 1e6 - half, 4), round(h / 1e6 + half, 4)] for h in refused]
        d["expected"] = self.sidecar.get("expected")
        # The cursor is where the last block landed, which is a fact, not a clock estimate.
        d["cursor_est"] = False
        if cur_top is not None and self.lo is not None and self.hi:
            pct = max(0.0, min(1.0, (cur_top - self.lo) / max(1e-9, self.hi - self.lo)))
            d["cursor_pct"], d["cursor"] = pct, cur_top
        else:
            d["cursor_pct"], d["cursor"] = None, None
        dur = self.sidecar.get("duration_s")
        if dur and self.start_epoch and not finished:
            d["eta_s"] = max(0, int(dur - (time.time() - self.start_epoch)))
        elif finished:
            d["eta_s"] = 0
        # Stop is offered while the recorder is alive: it has a stop file, the run is
        # not over, and the file grew in the last half minute.
        d["can_stop"] = bool(self.sidecar.get("stop_file")) and not finished \
            and (time.time() - self.last_growth) < 30
        d["meta"] = {**self.sidecar, "ended": self.ended}
        return d


def stop_recording(station: Station, tailer) -> dict:
    """The Scan tab's Stop for an rtl_power_fftw run: touch the stop file its
    sidecar names, which `marsad run` polls twice a second; it then sends
    Ctrl-Break and the program finishes its pass, writes it and exits, and the
    recorder analyses and archives as usual. Refused in plain words otherwise."""
    sf = (tailer.sidecar or {}).get("stop_file") if tailer is not None else None
    if not sf:
        raise ValueError("this run cannot be stopped from here: it was started without a "
                         "stop file (an older marsad run, or not an rtl_power_fftw run)")
    path = Path(sf)
    if not path.is_absolute():
        path = station.root / path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("stop\n", encoding="utf-8")
    return {"ok": True, "note": "asked the recorder to finish the pass in progress and stop"}


# ---------------------------------------------------------------- config for the builder

def builder_config(station: Station) -> dict:
    """Everything the scan builder page needs, derived from station.json."""
    presets = {}
    for name, p in station.presets.items():
        # The builder writes rtl_power commands. An rtl_power_fftw preset has a
        # range with no bin width ("2147M:2187M", or a single centre) and would
        # make the whole page refuse to start, as it did on 2026-10-22.
        if p.get("engine") == "rtl_power_fftw":
            try:
                rate = int(p.get("rate", rpf.DEFAULT_RATE_HZ))
                lo, hi, c = rpf.parse_freq(p["freq"], rate)
            except (SystemExit, ValueError, KeyError):
                continue
            bins = int(p.get("bins", rpf.DEFAULT_BINS))
            crop = float(p.get("crop", rpf.DEFAULT_CROP))
            presets[name] = {"engine": "rtl_power_fftw", "lo": lo, "hi": hi, "centre": c,
                             "bins": bins, "rate": rate, "time": float(p.get("time", rpf.DEFAULT_TIME_S)),
                             "overlap": float(p.get("overlap", 100.0 * crop)), "crop": crop,
                             "bin": rate / bins / 1e3, "integ": float(p.get("time", rpf.DEFAULT_TIME_S)),
                             "gain": p["gain"], "rx": p.get("receiver"),
                             "fw": p.get("floor_window", 0), "note": p.get("note", "")}
            continue
        if p.get("engine") not in (None, "rtl_power"):
            continue
        try:
            lo, hi, b = parse_freq_spec(p["freq"])
        except (SystemExit, ValueError, KeyError):
            continue
        presets[name] = {"lo": lo, "hi": hi, "bin": b, "gain": p["gain"],
                         "integ": p["integration"], "rx": p.get("receiver"),
                         "fw": p.get("floor_window", 0), "note": p.get("note", "")}
    for name, p in station.data.get("builder_profiles", {}).items():
        presets[name] = dict(p)
    receivers = {}
    for name, r in station.receivers.items():
        tuner = (r.get("tuner") or "e4000").lower()
        receivers[name] = {"label": r.get("label", name), "tuner": tuner,
                           "range": r.get("range_mhz", [24, 1766]),
                           "gaps": r.get("gaps_mhz", []), "device": r.get("device", 0),
                           "gains": TUNER_GAINS.get(tuner, TUNER_GAINS["e4000"])}
    return {
        "ceiling_mhz": RTL_POWER_MAX_MHZ,
        "rtl_max_sample_hz": RTL_MAX_SAMPLE_HZ,
        "retune_ms": station.retune_ms_per_hop,
        "crop": CROP, "fir": FIR,
        # rtl_power_fftw: the measured overhead model and its defaults (CLAUDE.md 2026-10-22)
        "fftw": {"retune_ms": float(station.data.get("retune_ms_per_hop_fftw", rpf.RETUNE_MS_DEFAULT)),
                 "read_ms": float(station.data.get("read_ms_per_buffer_fftw", rpf.READ_MS_DEFAULT)),
                 "base_buf": rpf.BASE_BUF, "buf_bytes": rpf.DEFAULT_BUF_BYTES,
                 "default_rate": rpf.DEFAULT_RATE_HZ, "default_bins": rpf.DEFAULT_BINS,
                 "default_time": rpf.DEFAULT_TIME_S, "default_crop": rpf.DEFAULT_CROP,
                 "scale_offset_db": -26.2},
        "receivers": receivers,
        "presets": presets,
        # JSON objects lose their order in a browser when keys look like numbers
        # ("900" jumps to the front), so the order travels separately.
        "preset_order": list(presets),
        "known": station.data.get("known_mhz", []),
        "chain_modules": station.data.get("chain_modules", []),
        "chain_off": station.data.get("chain_off_by_receiver", {}),
        "enc_default": station.data.get("enclosure_default", {}),
        "readout_families": station.data.get("readout_families_mhz", [14.4, 28.8, 24, 25, 100]),
        "spur_families": [list(f) for f in station.spur_families],
        "peak_threshold_db": float(station.data.get("peak_threshold_db", 10.0)),
        "floor_window_mhz": float(station.data.get("floor_window_mhz", 2.0)),
        "cmd_prefix": "marsad run",
        "station_root": str(station.root),
    }


def builder_fingerprint(station: Station, q: dict) -> str:
    """The Builder's fingerprint preview, made by marsad's own fingerprint function on
    the sidecar `marsad run` would write for these settings, so the two cannot drift
    apart (the page's own copy had no `| gainfix` and grouped thousands as "1,241.000").
    q holds one value per key: engine, rx, lo, hi, gain, then bin, integ, crop, fir for
    rtl_power or bins, rate, time, overlap, crop for rtl_power_fftw."""
    def f(k, d=None):
        v = q.get(k, d)
        return float(v) if v not in (None, "") else d
    rx = q.get("rx") or "xtr"
    lo, hi, gain = f("lo"), f("hi"), f("gain")
    gain = int(gain) if float(gain).is_integer() else gain
    crop = f("crop", CROP)
    if q.get("engine") == "rtl_power_fftw":
        bins, rate = int(f("bins", rpf.DEFAULT_BINS)), int(f("rate", rpf.DEFAULT_RATE_HZ))
        time_s = f("time", rpf.DEFAULT_TIME_S)
        sc = {"engine": rpf.ENGINE, "receiver": rx, rpf.ENGINE: {
            "freq_low_mhz": round(lo, 6), "freq_high_mhz": round(hi, 6), "bins": bins,
            "rate_hz": rate, "time_per_hop_s": int(time_s) if time_s.is_integer() else time_s,
            "overlap_pct": f("overlap", 100.0 * crop), "crop": crop, "gain_db": gain}}
        return fingerprint(sc, rate / bins / 1e3)
    exe, _ = station.rtl_power()
    tuner = station.tuner_of(rx)
    sc = {"receiver": rx, "rtl_power": {
        "freq_low_mhz": lo, "freq_high_mhz": hi,
        "freq_high_mhz_effective": min(hi, RTL_POWER_MAX_MHZ), "bin_khz": f("bin"),
        "gain_db": gain, "integration_s": int(f("integ")), "crop": crop,
        "fir": int(f("fir", FIR)), "tuner": tuner, "build": build_label(exe) if exe else None}}
    return fingerprint(sc, csv_bin_khz(lo, hi, f("bin"), crop))


def archive_summary(station: Station) -> dict:
    """What occupancy.db holds, for the Archive tab. Empty when there is no db."""
    if not station.db.exists():
        return {"surveys": [], "seen": [], "db": None}
    from . import archive
    con = archive.connect(station)
    surveys = [dict(r) for r in archive.list_rows(con)]
    seen = [dict(r) for r in archive.seen_rows(con, 2, 60)]
    con.close()
    return {"surveys": surveys, "seen": seen, "db": str(station.db)}


def survey_list(station: Station) -> list:
    """Every recording in surveys/ (rtl_power .csv and rtl_power_fftw .dat), newest
    first, with whatever the sidecar knows."""
    out = []
    if not station.surveys.exists():
        return out
    files = list(station.surveys.glob("*.csv")) + list(station.surveys.glob("*.dat"))
    for f in sorted(files, key=lambda p: p.stat().st_mtime, reverse=True):
        meta = {"engine": "rtl_power_fftw" if f.suffix.lower() == ".dat" else "rtl_power"}
        try:
            d = json.loads(f.with_suffix(".json").read_text())
            eng = d.get("engine") or meta["engine"]
            rp = d.get(eng, {}) or {}
            g = d.get("geometry", {})
            meta = {"engine": eng, "receiver": d.get("receiver"), "gain": rp.get("gain_db"),
                    "integ": rp.get("integration_s"), "tph": rp.get("time_per_hop_s"),
                    "sweeps": g.get("sweeps"),
                    "lo": rp.get("freq_low_mhz"), "hi": rp.get("freq_high_mhz_effective"),
                    "started": d.get("started_local"), "tag": d.get("preset")}
        except (OSError, ValueError):
            pass
        out.append({"name": f.name, "mb": round(f.stat().st_size / 1e6, 1),
                    "mtime": f.stat().st_mtime, **meta})
    return out


def started_at(path: Path) -> float:
    """When a recording was created. Two radios write two files at once, and the one
    modified last flips back and forth with every flush, so "newest" for the Scan tab
    means newest STARTED. On Windows st_ctime is the creation time."""
    s = path.stat()
    return getattr(s, "st_birthtime", s.st_ctime)


def newest_recording(station: Station):
    """The newest-started recording of either engine: rtl_power's .csv or rtl_power_fftw's .dat."""
    if not station.surveys.exists():
        return None
    files = list(station.surveys.glob("*.csv")) + list(station.surveys.glob("*.dat"))
    return max(files, key=lambda p: (started_at(p), p.stat().st_mtime)) if files else None


STILL_WRITING_S = 300.0      # a recording untouched this long is not being written any more


def recording_active(path: Path, now: float | None = None) -> bool:
    """Is this recording still being written? Its sidecar has no `finished_local` (the
    recorder stamps that when the program exits), and the file grew recently: within
    STILL_WRITING_S, or three of its write intervals for a slow scan (rtl_power writes
    once per -i, rtl_power_fftw once per pass). A run killed without warning never gets
    `finished_local`, so the clock is what retires it."""
    now = time.time() if now is None else now
    try:
        age = now - path.stat().st_mtime
    except OSError:
        return False
    every = 0.0
    try:
        d = json.loads(path.with_suffix(".json").read_text())
        if d.get("finished_local"):
            return False
        eng = d.get(d.get("engine") or "rtl_power") or {}
        every = float(eng.get("integration_s") or (d.get("geometry") or {}).get("sweep_s") or 0)
    except (OSError, ValueError, TypeError):
        pass
    return age <= max(STILL_WRITING_S, 3 * every)


def other_recording(station: Station, main: str | None, now: float | None = None):
    """The newest recording being written that is not the one the Scan tab shows: the
    second radio's `marsad run`, when two run at once. None when there is no such run."""
    if not station.surveys.exists():
        return None
    files = list(station.surveys.glob("*.csv")) + list(station.surveys.glob("*.dat"))
    for f in sorted(files, key=started_at, reverse=True):
        if f.name != main and recording_active(f, now):
            return f
    return None


OTHER_COLS, OTHER_KEEP = 600, 120     # the second-radio panel is small: half the columns, 120 rows
OTHER_TAILERS: dict = {}              # its own tailers, so the main view's keep their full width


def other_payload(station: Station, main: str | None, since: int = 0, file: str | None = None) -> dict:
    """What the Scan tab's second-radio panel draws: the other recording being written
    (its latest rows from `since`, as /data gives them), and every running recorder's
    status, which carries the live spectrum of the band a `marsad watch` is parked on.
    `file` is the recording the page has been drawing; a different answer tells it to
    start again from row 0."""
    from . import catalogue        # needs numpy; the rest of the app does not
    out = {"other": None, "recorders": []}
    f = other_recording(station, main)
    if f is not None:
        with TAIL_LOCK:
            t = OTHER_TAILERS.get(f.name)
            if t is None:
                t = (DatTailer if f.suffix.lower() == ".dat" else Tailer)(f, OTHER_COLS, OTHER_KEEP)
                OTHER_TAILERS[f.name] = t
            for k in [k for k in OTHER_TAILERS if k != f.name]:
                del OTHER_TAILERS[k]          # one at a time is all the panel shows
        t.poll()
        p = t.payload(since if file == f.name else 0)
        fp = p["meta"].get("fingerprint") or hunt.report_info(station, f.stem).get("fingerprint")
        p["fingerprint"] = fp
        p.pop("hop_centres_mhz", None)
        out["other"] = p
    try:
        out["recorders"] = [r for r in catalogue.live_recorders(station) if r.get("running")]
    except Exception:  # noqa: BLE001 - a bad status file must not cost the panel the recording
        out["recorders"] = []
    return out


# The waterfall's "internal" layer: what the receiver makes on its own. The
# confirmed internal families (CLAUDE.md: the 14.4 MHz clock family; 28.8 is half
# of it) are tagged on the page; the floor comes from a terminated run with the
# same fingerprint, the only honest measurement of it. Sub-MHz combs are left out:
# at display resolution they would tag every column.
INTERNAL_FAMILIES_MHZ = [14.4]


def internal_floor(station: Station, name: str, lo_mhz, hi_mhz, cols: int) -> dict:
    """The newest terminated run with the same fingerprint as `name`, its bins.csv
    `median_db` put on the display grid (median per column), or why there is none.

    Terminated is taken from occupancy.db when the survey is archived there (`mark`
    is the record of fact), otherwise guessed from the sidecar text."""
    from . import hunt
    from .archive import looks_terminated, median
    out = {"families_mhz": INTERNAL_FAMILIES_MHZ, "found": False, "floor": None}
    stem = Path(name).stem
    info = hunt.report_info(station, stem)
    fp = info.get("fingerprint")
    if not fp:
        out["reason"] = "this recording has no sidecar fingerprint, so no terminated run can be matched to it"
        return out
    out["fingerprint"] = fp
    flags = {}
    if station.db.exists():
        try:
            from . import archive
            con = archive.connect(station)
            flags = {r["name"]: r["terminated"] for r in
                     con.execute("SELECT name, terminated FROM surveys WHERE fingerprint = ?", (fp,))}
            con.close()
        except Exception:  # noqa: BLE001 - a broken db only loses the archive's flags
            flags = {}
    best = None
    for rep_dir in station.reports.iterdir() if station.reports.exists() else []:
        if rep_dir.name == stem or not (rep_dir / "bins.csv").exists():
            continue
        i = hunt.report_info(station, rep_dir.name)
        if i.get("fingerprint") != fp:
            continue
        term = flags.get(rep_dir.name)
        if term is None:
            try:
                sc = json.loads((station.surveys / f"{rep_dir.name}.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            term = looks_terminated(sc)
        if term and (best is None or (i.get("started") or "") > (best.get("started") or "")):
            best = i
    if best is None:
        out["reason"] = ("no terminated run with this fingerprint, so the receiver's own floor "
                         "is not known here; only the tagged clock lines are shown as internal")
        return out
    if lo_mhz is None or hi_mhz is None or hi_mhz <= lo_mhz:
        out["reason"] = "the recording's frequency range is not known yet"
        return out
    span = hi_mhz - lo_mhz
    per: list = [[] for _ in range(cols)]
    for k, (_, med, _) in hunt.load_bins(station.reports / best["name"]).items():
        c = int((k / 1e3 - lo_mhz) / span * (cols - 1))
        if 0 <= c < cols:
            per[c].append(med)
    out.update({"found": True, "name": best["name"], "started": best.get("started"),
                "floor": [round(median(v), 2) if v else None for v in per]})
    return out


def page_html() -> str:
    return (WEB / "index.html").read_text(encoding="utf-8")


def roadmap_text(station: Station) -> str:
    md = station.root / "TOOL_ROADMAP.md"
    if md.exists():
        return md.read_text(encoding="utf-8")
    packaged = WEB / "ROADMAP.md"
    return packaged.read_text(encoding="utf-8") if packaged.exists() else "No roadmap found."


TAILERS: dict = {}
TAIL_LOCK = threading.Lock()
LAST_REQUEST = [time.time()]      # any HTTP hit defers the idle shutdown
FOLLOWERS: dict = {}              # recording name -> the Hunt panel's line follower


def hunt_reading(t, name: str, mode: str = "strongest", centre_mhz=None, lock_mhz=None,
                 reset: bool = False, spectrum: bool = True) -> dict:
    """One Hunt-panel reading from a tailer: the followed line, the floor, and the
    rate sweeps are arriving at. The follower steps once per NEW sweep, however
    often the page asks, so its hysteresis counts sweeps and not requests."""
    if mode not in hunt.FOLLOW_MODES:
        raise ValueError(f"follow mode must be one of {', '.join(hunt.FOLLOW_MODES)}")
    with TAIL_LOCK:
        slot = FOLLOWERS.get(name)
        if slot is None or reset or slot["mode"] != mode or slot["centre"] != centre_mhz:
            slot = {"f": hunt.Follower(), "seen": -1, "last": None, "mode": mode,
                    "centre": centre_mhz, "lock": lock_mhz}
            FOLLOWERS[name] = slot
    with t.lock:
        total = t.dropped + len(t.sweeps)
        latest = dict(t.latest)
        centres = sorted(t.hop_centres)
        step = t.step_hz / 1e3 if t.step_hz else None
    if total != slot["seen"] or lock_mhz != slot["lock"]:
        slot["lock"] = lock_mhz
        r = slot["f"].update(latest, centres, step, mode, centre_mhz, lock_mhz) if latest else None
        if total != slot["seen"]:
            slot["seen"] = total
        if r is not None:
            slot["last"] = r
            slot["spectrum"] = hunt.spectrum_strip(latest, centres, step)
    meta = t.sidecar or {}
    return {"file": name, "sweeps": total, "reading": slot["last"],
            "spectrum": slot.get("spectrum") if spectrum else None,
            "updates_per_s": t.updates_per_s(),
            "age_s": round(time.time() - t.last_finish, 2) if t.last_finish else None,
            "lo": t.lo, "hi": t.hi, "engine": meta.get("engine", "rtl_power"),
            "finished": bool(meta.get("finished_local")) or bool(getattr(t, "ended", False))}


def get_tailer(station: Station, name: str, cols: int, keep: int):
    """One tailer per recording, built the first time it is asked for; a .dat gets
    the rtl_power_fftw reader."""
    with TAIL_LOCK:
        t = TAILERS.get(name)
        if t is None:
            path = station.surveys / name
            if not path.exists():
                return None
            t = (DatTailer if path.suffix.lower() == ".dat" else Tailer)(path, cols, keep)
            TAILERS[name] = t
        return t


def main(argv=None, station: Station | None = None, prog: str = "marsad live",
         banner: str | None = None) -> int:
    """`banner` replaces the usual "(Ctrl-C to stop)" line, for the double-click launch."""
    station = station or Station.load()
    ap = argparse.ArgumentParser(prog=prog, description="The station's local web app")
    ap.add_argument("--csv", help="path to the recording, .csv or .dat (default: newest in surveys/)")
    ap.add_argument("--port", type=int, default=8731)
    ap.add_argument("--cols", type=int, default=1200, help="display columns")
    ap.add_argument("--keep", type=int, default=400, help="sweeps held in memory")
    ap.add_argument("--pin", action="store_true",
                    help="stay on the starting CSV instead of switching to a newer "
                         "one when it appears in surveys/")
    ap.add_argument("--open", action="store_true", help="open the page in your browser")
    ap.add_argument("--exit-after-idle", type=float, default=0, metavar="MIN",
                    help="shut down this many minutes after the CSV stops growing "
                         "(0 = run until Ctrl-C). Lets `marsad run` start a viewer "
                         "that cleans itself up once the scan is over.")
    args = ap.parse_args(argv)

    def newest_csv():
        return newest_recording(station)

    if args.csv:
        path = Path(args.csv)
        if not path.is_absolute():
            path = station.root / args.csv
    else:
        path = newest_csv()
    follow = not args.pin
    if path is not None:
        print(f"  watching {path.name}" + ("" if args.pin else "  (will follow a newer CSV)"))
    else:
        print("  no surveys yet; the Scan tab fills in when the first one starts")

    live = {"path": path, "tail": None}
    if path is not None:
        tail = (DatTailer if path.suffix.lower() == ".dat" else Tailer)(path, args.cols, args.keep)
        with TAIL_LOCK:
            TAILERS[path.name] = tail
        live["tail"] = tail

    cfg = builder_config(station)
    runner = probe.ProbeRunner(station)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):          # keep the console for the scan
            pass

        def _send(self, body: bytes, ctype: str = "application/json", code: int = 200):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json_body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            try:
                d = json.loads(raw.decode("utf-8") or "{}")
            except ValueError:
                d = {}
            return d if isinstance(d, dict) else {}

        def do_POST(self):
            """The Probe tab's actions. Each answers with JSON; a refusal is a
            plain-language `error` with status 400, never a hang."""
            u = urlparse(self.path)
            LAST_REQUEST[0] = time.time()
            form = self._json_body()
            try:
                if u.path == "/probe/preview":
                    out = probe.compose(station, form)
                elif u.path == "/probe/start":
                    out = runner.start(form)
                elif u.path == "/probe/stop":
                    out = runner.stop()
                elif u.path == "/chain/save":
                    chain.save(station, form)
                    out = chain.summary(station)
                elif u.path == "/chain/use":
                    chain.use(station, str(form.get("name") or ""))
                    out = chain.summary(station)
                elif u.path == "/captures/export":
                    # Refused on the server for anything private (catalogue.export says why).
                    from . import catalogue
                    try:
                        out = catalogue.export(station, int(form.get("id") or 0), str(form.get("format") or ""))
                    except catalogue.Refused as exc:
                        self._send(json.dumps({"error": str(exc)}).encode(), code=403)
                        return
                elif u.path in ("/captures/archive/plan", "/captures/archive", "/captures/restore"):
                    # The only way anything leaves the pool: zipped, verified, then moved,
                    # and only with confirm=true (capture_archive.py says the order).
                    from . import capture_archive as ca
                    try:
                        eid = int(form.get("id") or 0)
                        opts = dict(scope=str(form.get("scope") or "event"),
                                    include_exports=bool(form.get("exports")),
                                    keep_fingerprints=form.get("keep_fp", True) is not False)
                        if u.path == "/captures/archive/plan":
                            out = ca.plan(station, eid, **opts)
                        elif u.path == "/captures/archive":
                            out = ca.archive(station, eid, note=str(form.get("note") or ""),
                                             label=str(form.get("label") or ""),
                                             confirm=form.get("confirm") is True, **opts)
                        else:
                            out = ca.restore(station, eid)
                    except ca.Refused as exc:
                        self._send(json.dumps({"error": str(exc)}).encode(), code=403)
                        return
                    except OSError as exc:          # a full disk, a file held open: said, not crashed
                        self._send(json.dumps({"error": f"{type(exc).__name__}: {exc}"}).encode(), code=500)
                        return
                elif u.path == "/scan/stop":
                    name = str(form.get("file") or "")
                    t = live["tail"] if (live["path"] and name == live["path"].name) else TAILERS.get(name)
                    out = stop_recording(station, t)
                else:
                    self.send_error(404); return
            except (ValueError, RuntimeError) as exc:
                self._send(json.dumps({"error": str(exc)}).encode(), code=400)
                return
            self._send(json.dumps(out).encode())

        def do_GET(self):
            u = urlparse(self.path)
            LAST_REQUEST[0] = time.time()
            q = parse_qs(u.query)
            ctype = "application/json"
            if u.path == "/data":
                since = int(q.get("since", ["0"])[0])
                name = (q.get("file") or [None])[0]
                cur = live["tail"]
                live_name = live["path"].name if live["path"] else None
                t = cur if (not name or name == live_name) else get_tailer(station, name, args.cols, args.keep)
                if t is None:
                    self.send_error(404); return
                if t is not cur:
                    t.poll()        # archived files are not polled in the background
                pr = station.presets.get(t.sidecar.get("preset") or "", {})
                fw = float(pr.get("floor_window") or cfg["floor_window_mhz"])
                pk = t.peaks(cfg["peak_threshold_db"], fw, cfg["spur_families"], cfg["readout_families"])
                body = json.dumps({**t.payload(since), "peaks": pk, "live": live_name}).encode()
            elif u.path == "/other":
                # The second-radio panel: whatever is being recorded besides `main`.
                main_name = (q.get("main") or [""])[0] or (live["path"].name if live["path"] else None)
                body = json.dumps(other_payload(station, main_name, int((q.get("since") or ["0"])[0]),
                                                (q.get("file") or [""])[0] or None)).encode()
            elif u.path == "/internal":
                name = (q.get("file") or [None])[0] or (live["path"].name if live["path"] else None)
                t = get_tailer(station, name, args.cols, args.keep) if name else None
                if t is None:
                    self.send_error(404); return
                if t.lo is None:
                    t.poll()
                body = json.dumps(internal_floor(station, name, t.lo, t.hi, args.cols)).encode()
            elif u.path == "/surveys":
                body = json.dumps({"live": live["path"].name if live["path"] else None,
                                   "follow": follow,
                                   "surveys": survey_list(station)}).encode()
            elif u.path == "/config":
                body = json.dumps(cfg).encode()
            elif u.path == "/builder/fingerprint":
                try:
                    body = json.dumps({"fingerprint": builder_fingerprint(
                        station, {k: v[0] for k, v in q.items()})}).encode()
                except (TypeError, ValueError, KeyError, ZeroDivisionError) as exc:
                    body = json.dumps({"error": str(exc)}).encode()
            elif u.path == "/archive":
                try:
                    body = json.dumps(archive_summary(station)).encode()
                except Exception as exc:  # noqa: BLE001 - a broken db must not kill the page
                    body = json.dumps({"surveys": [], "seen": [], "db": None,
                                       "error": str(exc)}).encode()
            elif u.path == "/roadmap":
                body = roadmap_text(station).encode()
                ctype = "text/plain; charset=utf-8"
            elif u.path == "/probe/config":
                body = json.dumps(probe.catalog(station)).encode()
            elif u.path == "/chain":
                try:
                    body = json.dumps(chain.summary(station)).encode()
                except ValueError as exc:
                    body = json.dumps({"error": str(exc), "chains": {}, "current": None,
                                       "parts": chain.PART_TYPES}).encode()
            elif u.path == "/probe/receivers":
                # Opens each dongle briefly, so never while a recording holds one.
                if runner.running() or host.running_sdr_programs():
                    body = json.dumps({"found": [], "error": "the SDR is in use; cannot look "
                                       "for receivers until the recording ends"}).encode()
                else:
                    body = json.dumps(probe.detect_receivers(station)).encode()
            elif u.path == "/probe/status":
                body = json.dumps(runner.status()).encode()
            elif u.path == "/hunt/reading":
                # The Hunt panel's meter: polled several times a second while a hunt
                # run records, so it reads the file itself instead of waiting for the loop.
                name = (q.get("file") or [""])[0]
                t = get_tailer(station, name, args.cols, args.keep) if name else None
                if t is None:
                    self._send(json.dumps({"error": "no such recording yet", "reading": None}).encode())
                    return
                t.poll()
                num = lambda k: float(q[k][0]) if (q.get(k) and q[k][0] not in ("", "null")) else None
                try:
                    out = hunt_reading(t, name, (q.get("mode") or ["strongest"])[0], num("centre"),
                                       num("lock"), (q.get("reset") or ["0"])[0] == "1")
                except ValueError as exc:
                    self._send(json.dumps({"error": str(exc)}).encode(), code=400)
                    return
                body = json.dumps(out).encode()
            elif u.path == "/hunt/reports":
                body = json.dumps({"reports": hunt.report_list(station),
                                   "block_choices_mhz": hunt.BLOCK_CHOICES_MHZ,
                                   "block_mhz": hunt.BLOCK_MHZ}).encode()
            elif u.path == "/hunt/candidates":
                # Which blocks the device run raised over the background run. A refusal
                # (different fingerprints, same report twice) is a plain-language `error`.
                try:
                    out = hunt.candidates(station, (q.get("bg") or [""])[0], (q.get("dev") or [""])[0],
                                          float((q.get("block") or [hunt.BLOCK_MHZ])[0]),
                                          int((q.get("top") or [hunt.TOP])[0]))
                except ValueError as exc:
                    self._send(json.dumps({"error": str(exc)}).encode(), code=400)
                    return
                body = json.dumps(out).encode()
            elif u.path == "/room/targets":
                body = json.dumps({"targets": room.targets(station),
                                   "block_choices_mhz": hunt.BLOCK_CHOICES_MHZ,
                                   "block_mhz": hunt.BLOCK_MHZ}).encode()
            elif u.path in ("/room/compare", "/room/loudest"):
                # A device's states against its background, or the devices against each
                # other. A refusal is a plain-language `error`, as for /hunt/candidates.
                try:
                    block = float((q.get("block") or [hunt.BLOCK_MHZ])[0])
                    if u.path == "/room/compare":
                        out = room.compare(station, (q.get("target") or [""])[0],
                                           (q.get("bg") or [""])[0] or None, block)
                    else:
                        out = room.loudest(station, block)
                except ValueError as exc:
                    self._send(json.dumps({"error": str(exc)}).encode(), code=400)
                    return
                body = json.dumps(out).encode()
            elif u.path.startswith("/captures"):
                # The Captures tab. Read-only on occupancy.db and the pool; a refusal
                # (private band, missing file) is a plain-language `error`. Imported here:
                # it needs numpy, and the rest of the app does not.
                from . import catalogue
                one = lambda k: (q.get(k) or [""])[0] or None
                try:
                    if u.path == "/captures/audio":
                        # Listening to an open-band capture: demodulated slice by slice and
                        # streamed as WAV. Never written to disk; private refused (403) by
                        # audio_plan before a single byte is sent.
                        num = lambda k, d=0.0: float(one(k)) if one(k) else d
                        plan = catalogue.audio_plan(station, int(one("id") or 0), one("mode") or "am",
                                                    num("start"), num("offset_khz") * 1e3,
                                                    num("dur", None))
                        self.send_response(200)
                        self.send_header("Content-Type", "audio/wav")
                        self.send_header("Content-Length", str(plan["bytes"]))
                        self.send_header("Cache-Control", "no-store")
                        self.end_headers()
                        try:
                            for part in catalogue.audio_stream(plan):
                                self.wfile.write(part)
                        except (ConnectionError, OSError):
                            pass                            # the page stopped or moved the slider
                        return
                    if u.path == "/captures":
                        num = lambda k: float(one(k)) if one(k) else None
                        out = catalogue.summary(station, one("date"), num("lo"), num("hi"), one("badge"))
                    elif u.path == "/captures/event":
                        out = catalogue.event(station, int(one("id") or 0))
                    elif u.path == "/captures/fp":
                        out = catalogue.fingerprint_view(station, int(one("id") or 0))
                    elif u.path == "/captures/iq":
                        out = catalogue.iq_view(station, int(one("id") or 0))
                    elif u.path == "/captures/recorders":
                        # polled by every tab: small status files only, never the radio or the db
                        out = {"recorders": catalogue.live_recorders(station)}
                    else:
                        self.send_error(404); return
                except catalogue.Refused as exc:
                    self._send(json.dumps({"error": str(exc)}).encode(), code=403)
                    return
                except (ValueError, OSError) as exc:
                    self._send(json.dumps({"error": str(exc)}).encode(), code=400)
                    return
                body = json.dumps(out).encode()
            elif u.path == "/probe/log":
                body = probe.read_log(station).encode("utf-8")
                ctype = "text/plain; charset=utf-8"
            elif u.path.startswith("/reports/"):
                hit = report_file(station, u.path)
                if hit is None:
                    self.send_error(404); return
                body, ctype = hit[0].read_bytes(), hit[1]
            elif u.path == "/builder":
                body = (WEB / "builder.html").read_text(encoding="utf-8").encode()
                ctype = "text/html; charset=utf-8"
            elif u.path in ("/", "/index.html"):
                body = page_html().encode()
                ctype = "text/html; charset=utf-8"
            else:
                self.send_error(404); return
            self._send(body, ctype)

    # Python's HTTP server sets SO_REUSEADDR, which on Windows lets a second copy bind
    # a port another copy is already listening on. Then two viewers answer the same
    # address and the browser may get the stale one. Refuse instead: one viewer per port.
    class OnePerPort(ThreadingHTTPServer):
        allow_reuse_address = False

    try:
        srv = OnePerPort(("127.0.0.1", args.port), H)
    except OSError:
        print(f"  Port {args.port} is already in use - a viewer is probably running there.")
        print(f"  Open http://127.0.0.1:{args.port}/ or start this one with --port {args.port + 1}.")
        return 2

    def loop():
        idle = args.exit_after_idle * 60
        while True:
            if follow:
                n = newest_csv()
                if n is not None and (live["path"] is None or n.name != live["path"].name):
                    t = get_tailer(station, n.name, args.cols, args.keep)
                    if t is not None:
                        live["path"], live["tail"] = n, t
                        print(f"  now watching {n.name}")
            tail = live["tail"]
            if tail is not None:
                tail.poll()
                quiet = min(time.time() - tail.last_growth, time.time() - LAST_REQUEST[0])
                if idle and tail.rows and quiet > idle and not runner.running():
                    print(f"  no new data for {args.exit_after_idle:g} min - shutting down")
                    srv.shutdown()
                    return
            # A hunt tile writes a sweep a second and the page turns it into sound, so
            # while a run started from the page is recording the file is read twice a
            # second. Otherwise every 2 s is plenty: rtl_power flushes in 4 KB bursts.
            time.sleep(0.5 if runner.running() else 2.0)

    threading.Thread(target=loop, daemon=True).start()

    url = f"http://127.0.0.1:{args.port}/"
    note = f", exits {args.exit_after_idle:g} min after the scan ends" if args.exit_after_idle else ""
    print(banner if banner else f"  {url}   (Ctrl-C to stop{note})", flush=True)
    if args.open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        runner.shutdown()      # a probe run started from the page must not outlive the Stop button
    return 0
