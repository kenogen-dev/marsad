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
    whole hop, or one unparseable value discards every reading on the line.
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
        self.medians: list = []     # whole-sweep median, live
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
        self.loaded = False             # false until the file has been read to its end once

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
        # Relay 010: every rtl_power run now has a stop file; Stop needs its name, and needs to
        # know the run is over (kept apart, so the page's meta is as it was).
        self._run_over = bool(d.get("finished_local"))
        return {"duration_s": d.get("duration_requested_s"),
                "preset": d.get("preset"),
                "integration_s": rp.get("integration_s"),
                "gain_db": rp.get("gain_db"),
                "receiver": d.get("receiver"),
                "stop_file": d.get("stop_file"),
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
            self.loaded = True

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
        width measured 6 dB down. Tags: the spur families and the hop
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
                # Stop is offered while the run is alive: a stop file, not over, and the CSV grew
                # within two intervals (rtl_power writes once per -i, so 30 s is too short).
                "can_stop": bool(self.sidecar.get("stop_file")) and not getattr(self, "_run_over", False)
                            and (time.time() - self.last_growth) < max(30.0, 2.0 * float(integ or 0) + 10.0),
            }



def _utc_to_local(text: str) -> str:
    """'2030-01-15 05:44:17 UTC' (what rtl_power_fftw writes) -> local 'YYYY-MM-DD HH:MM:SS'."""
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
                         "below rtl_power's 50 kHz scale at 2.4 MS/s"}
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
        # (a warming E4000 loses them one at a time), so only the frequency wrap is
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


_FACTS: dict = {}


def run_facts(station: Station, path: Path) -> dict:
    """The run's own facts for the desktop window's header (relay 009): tag, when it began, the
    fingerprint and the engine, from its sidecar. Cached per sidecar and age; reads, never writes."""
    side = path.with_suffix(".json")
    try:
        key = (str(side), side.stat().st_mtime)
    except OSError:
        key = (str(side), None)
    if key not in _FACTS:
        from . import hunt
        info = hunt.report_info(station, path.stem)
        try:
            d = json.loads(side.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = {}
        _FACTS[key] = {"tag": info.get("tag"), "started_local": d.get("started_local") or info.get("started"),
                       "finished_local": d.get("finished_local"), "preset": d.get("preset"),
                       "receiver": d.get("receiver"),
                       "engine": d.get("engine") or ("rtl_power_fftw" if path.suffix.lower() == ".dat" else "rtl_power"),
                       "fingerprint": info.get("fingerprint")}
    return dict(_FACTS[key])


def progress_payload(station: Station, tailer, live_name: str | None) -> dict:
    """What /live/progress answers (relay 009): where the run is - sweeps done and expected, the
    sweep in progress, time left, refused hops, whether Stop is offered - and the run's facts, with
    no sweep rows at all, so the desktop window can ask several times a second for almost nothing.
    The same numbers /data carries, read from the same tailer; nothing is computed afresh."""
    d = tailer.payload(10 ** 12)                 # past the end: no rows, no stamps, no medians
    for k in ("rows", "stamps", "medians", "partial", "hop_centres_mhz", "from"):
        d.pop(k, None)
    d.setdefault("engine", "rtl_power")
    meta = d.get("meta") or {}
    facts = run_facts(station, tailer.path)
    if meta.get("fingerprint") and not facts.get("fingerprint"):
        facts["fingerprint"] = meta["fingerprint"]
    d["facts"] = facts
    d["integration_s"] = meta.get("integration_s")
    d["last_finish_age_s"] = (round(time.time() - tailer.last_finish, 2) if tailer.last_finish else None)
    # rtl_power's reader keeps no finished_local in its meta (only the fftw one does), so the
    # sidecar's own, read for the facts, is what says an rtl_power run is over.
    d["finished"] = bool(meta.get("finished_local") or meta.get("ended") or facts.get("finished_local"))
    d["now"] = time.time()
    d["live"] = live_name
    return d


def stop_recording(station: Station, tailer) -> dict:
    """Stop for a run of either engine: touch the stop file its sidecar names, which
    `marsad run` polls twice a second; it then sends Ctrl-Break, the program finishes
    (rtl_power_fftw its pass, rtl_power its interval), writes and exits, and the
    recorder analyses and archives as usual. Refused in plain words otherwise."""
    sf = (tailer.sidecar or {}).get("stop_file") if tailer is not None else None
    if not sf:
        raise ValueError("this run cannot be stopped from here: it was started without a "
                         "stop file (a run started by an older marsad)")
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
        # make the whole page refuse to start.
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
        # rtl_power_fftw: the overhead model and its defaults
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
        "enclosures": station.data.get("enclosures") or ["open bench", "in shielded box", "in shielded box, seam taped"],
        "notes": station.data.get("builder_notes", []),
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
    rx = q.get("rx") or station.default_receiver
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


_STARTED: dict = {}


def _sidecar_start(path: Path) -> float | None:
    """When the sidecar says the recording began. Cached per sidecar and age, because the
    follow loop asks about every recording on every poll."""
    side = path.with_suffix(".json")
    try:
        key = (str(side), side.stat().st_mtime)
    except OSError:
        return None
    if key not in _STARTED:
        try:
            _STARTED[key] = dt.datetime.fromisoformat(
                json.loads(side.read_text(encoding="utf-8"))["started_local"]).timestamp()
        except (OSError, ValueError, KeyError, TypeError):
            _STARTED[key] = None
    return _STARTED[key]


def _created(path: Path) -> float:
    s = path.stat()
    return getattr(s, "st_birthtime", s.st_ctime)


def started_at(path: Path) -> float:
    """When a recording began. Two radios write two files at once, and the one modified
    last flips back and forth with every flush, so "newest" for the Scan tab means newest
    STARTED. The sidecar's start time comes first, because a restored backup gives every
    file a new creation time, in whatever order it was unpacked. Without a sidecar: the
    file's creation time (on Windows that is st_ctime)."""
    t = _sidecar_start(path)
    return t if t is not None else _created(path)


def newest_recording(station: Station):
    """The newest-started recording of either engine: rtl_power's .csv or rtl_power_fftw's .dat."""
    if not station.surveys.exists():
        return None
    files = list(station.surveys.glob("*.csv")) + list(station.surveys.glob("*.dat"))
    # sidecars say the second; two runs started in one second go by creation time
    return max(files, key=lambda p: (started_at(p), _created(p), p.stat().st_mtime)) if files else None


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
    for f in sorted(files, key=lambda p: (started_at(p), _created(p)), reverse=True):
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
# confirmed internal families (the 14.4 MHz clock family; 28.8 is half of it) are tagged on the page; the floor comes from a terminated run with the
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

# ---------------------------------------------------------------- the desktop window's chain drawings
# chain_layout.json sits beside chain.json and holds only drawings: where parts sit, the
# wires, splitters. chain.json stays the old format, so the web page keeps reading it;
# the desktop window writes each path of a drawn rig there as an ordinary chain. This
# service stores the drawing as it is given (checked for shape and size only) and saves
# chain.json through chain.save, exactly as the web page's Save does.
CHAIN_LAYOUT_FILE = "chain_layout.json"
CHAIN_LAYOUT_MAX = 2 * 1024 * 1024


def chain_layout_summary(station: Station) -> dict:
    p = station.root / CHAIN_LAYOUT_FILE
    layout = {"schema": 1, "rigs": {}}
    if p.exists():
        try:
            layout = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"{p.name} is unreadable: {exc}")
    rec = station.data.get("recorder") or {}
    serials = rec.get("serials") or {}
    roles = rec.get("roles") or {}
    return {"chain": chain.summary(station), "layout": layout,
            "serials": {str(k): str(v) for k, v in serials.items()},
            "roles": {str(k): str(v) for k, v in roles.items()}}


def chain_layout_save(station: Station, form: dict) -> dict:
    """{"chains": a chain.json document, "layout": the drawings}. chain.json is
    saved first, through the same check the web page's Save uses; if it is refused,
    nothing is written."""
    layout = form.get("layout")
    if not isinstance(layout, dict) or not isinstance(layout.get("rigs"), dict):
        raise ValueError("the layout must have a 'rigs' object")
    raw = json.dumps(layout, indent=1)
    if len(raw) > CHAIN_LAYOUT_MAX:
        raise ValueError("the layout is too large")
    chain.save(station, form.get("chains"))
    p = station.root / CHAIN_LAYOUT_FILE
    tmp = p.with_name(p.name + ".part")
    tmp.write_text(raw + "\n", encoding="utf-8")
    tmp.replace(p)
    return chain_layout_summary(station)
# ---------------------------------------------------------------- starting scans (relay 010)
#
# The window's "Start a scan" and "Start recorder" panels. Everything here composes, checks
# and launches; nothing here records, analyses or changes how a run is recorded. What is
# launched is the ordinary `marsad run` / `marsad watch` command line, with full --chain and
# --conditions, composed exactly as the old Scan Builder printed it (ported from
# web/builder.html's command(), conditionsText() and flags(), and tested against them), with
# every figure taken from the engine's own geometry (scan_geometry, rpf.geometry) and
# fingerprint, never a second copy of the arithmetic.

SAVED_SCANS_FILE = "saved_scans.json"     # the owner's saved scans and recorder sessions
LAUNCH_LOG_DIR = "logs"                   # what each launched run prints, one file per launch
SCAN_DUR_DEFAULT = {"LS": (16, "h"), "deep": (2, "h"), "custom": (2, "h")}   # builder.html DUR_DEFAULT
ENCLOSURES_DEFAULT = ["open bench", "in shielded box", "in shielded box, seam taped"]
SCAN_DEFAULTS = {"analyze": True, "useskip": True, "skipmin": 16, "thresh": 10, "floorwin": None,
                 "crop": CROP, "fir": FIR, "tag": "", "target": None,
                 "temp": "", "humid": "", "where": "inside", "sky": "", "notes": ""}
TRADEOFF_INTEG = [5, 10, 15, 20, 30, 45, 60, 90, 120, 180, 240, 300]
TRADEOFF_TIME = [0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 30, 60]
WATCH_FLAGS = (("threshold", "--threshold"), ("passes", "--passes"), ("ambush", "--ambush"),
               ("cooldown", "--cooldown"), ("capture", "--capture"), ("keep_min_snr", "--keep-min-snr"))


def jsnum(v) -> str:
    """A number as JavaScript's String(Number) writes it: 29 not 29.0, 0.25, 1e-7."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    v = float(v)
    if v.is_integer() and abs(v) < 1e21:
        return str(int(v))
    r = repr(v)
    if "e" in r:
        m, e = r.split("e")
        e = int(e)
        return f"{m}e{'+' if e > 0 else '-'}{abs(e)}"
    return r


def _num(v, d: int = 0) -> str:
    """toLocaleString with fixed decimals (en): grouping commas, d places."""
    return f"{float(v):,.{d}f}"


def _secs(s: float) -> str:
    if s < 90:
        return _num(s, 1) + " s"
    if s < 5400:
        return _num(s / 60, 1) + " min"
    return _num(s / 3600, 2) + " h"


def _f(v, default=None):
    if v in (None, ""):
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _clean_num(v):
    """29.0 -> 29, so JSON numbers from the window compare as the page's did."""
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def scan_presets(station: Station) -> dict:
    """The presets as the Builder holds them (builder_config), each with its default duration."""
    cfg = builder_config(station)
    out = {}
    for name in cfg["preset_order"]:
        p = dict(cfg["presets"][name])
        if not p.get("dur"):
            p["dur"] = list(SCAN_DUR_DEFAULT.get(name, (1, "h")))
        out[name] = p
    return out


def scan_from_preset(station: Station, name: str, rx: str | None = None) -> dict:
    """A full scan setting (the Builder's read() shape) from a preset, as applyPreset() fills
    the page: range, bin, gain, integration, engine settings, receiver and duration."""
    presets = scan_presets(station)
    if name not in presets:
        raise ValueError(f"no preset called {name!r}")
    p = presets[name]
    fftw = p.get("engine") == rpf.ENGINE
    rx = p.get("rx") or rx or station.default_receiver or next(iter(station.receivers), "")
    s = {**SCAN_DEFAULTS, "preset": name, "engine": rpf.ENGINE if fftw else "rtl_power",
         "lo": p["lo"], "hiReq": p["hi"], "bin": p.get("bin"), "gain": p["gain"], "integ": p.get("integ"),
         "fbins": p.get("bins", rpf.DEFAULT_BINS), "ftime": p.get("time", rpf.DEFAULT_TIME_S),
         "frate": p.get("rate", rpf.DEFAULT_RATE_HZ), "fover": p.get("overlap") if fftw else None,
         "crop": p.get("crop", CROP) if fftw else CROP, "fir": FIR, "rx": rx,
         "durVal": p["dur"][0], "unit": p["dur"][1], "floorwin": p.get("fw") or None}
    return s


def scan_settings(station: Station, form: dict) -> dict:
    """The Builder's read(): every setting of one scan, numbers as numbers. Missing keys
    come from the named preset, so a saved scan only has to hold what it set."""
    base = scan_from_preset(station, form["preset"]) if form.get("preset") in scan_presets(station) else \
        {**SCAN_DEFAULTS, "preset": "custom", "engine": "rtl_power", "lo": 100.0, "hiReq": 200.0,
         "bin": 50, "gain": 29, "integ": 120, "fbins": rpf.DEFAULT_BINS, "ftime": rpf.DEFAULT_TIME_S,
         "frate": rpf.DEFAULT_RATE_HZ, "fover": None, "rx": station.default_receiver or "",
         "durVal": 2, "unit": "h"}
    s = {**base, **{k: v for k, v in form.items() if v is not None or k in ("fover", "target", "floorwin")}}
    s["preset"] = s["preset"] if s.get("preset") in scan_presets(station) else "custom"
    for k in ("lo", "hiReq", "bin", "gain", "crop", "ftime", "durVal", "skipmin", "thresh"):
        s[k] = _clean_num(_f(s.get(k), _f(base.get(k), 0.0)))
    s["integ"] = _clean_num(_f(s.get("integ"), 1) or 1)
    s["fir"] = _clean_num(_f(s.get("fir"), FIR))
    s["fbins"] = int(_f(s.get("fbins"), rpf.DEFAULT_BINS))
    s["frate"] = int(round(_f(s.get("frate"), rpf.DEFAULT_RATE_HZ)))
    s["fover"] = None if s.get("fover") in (None, "") else _clean_num(_f(s["fover"]))
    s["target"] = None if s.get("target") in (None, "") else _clean_num(_f(s["target"]))
    s["floorwin"] = None if s.get("floorwin") in (None, "", 0) else _clean_num(_f(s["floorwin"]))
    s["unit"] = s.get("unit") if s.get("unit") in ("m", "h") else "h"
    s["engine"] = rpf.ENGINE if s.get("engine") == rpf.ENGINE else "rtl_power"
    for k in ("analyze", "useskip", "dryfirst", "nonint"):
        s[k] = bool(s.get(k))
    for k in ("tag", "temp", "humid", "where", "sky", "notes", "device"):
        s[k] = "" if s.get(k) is None else str(s[k]).strip() if k != "device" else str(s[k])
    s["where"] = s["where"] or "inside"
    s["rx"] = s.get("rx") if s.get("rx") in station.receivers else (station.default_receiver or "")
    s["durS"] = s["durVal"] * (3600 if s["unit"] == "h" else 60)
    s["hi"] = s["hiReq"] if s["engine"] == rpf.ENGINE else min(s["hiReq"], RTL_POWER_MAX_MHZ)
    s["clamped"] = s["engine"] != rpf.ENGINE and s["hiReq"] > RTL_POWER_MAX_MHZ
    return s


def conditions_text(s: dict) -> str:
    """builder.html conditionsText(): what --conditions records."""
    bits = []
    if s.get("sky"):
        bits.append(s["sky"])
    if s.get("temp", "") != "":
        bits.append(f"{s['temp']}C {s.get('where') or 'inside'}")
    if s.get("humid", "") != "":
        bits.append(f"{s['humid']}% RH")
    if s.get("notes"):
        bits.append(s["notes"])
    return ", ".join(bits)


def _fftw_centre(station: Station, s: dict):
    p = scan_presets(station).get(s["preset"]) or {}
    if p.get("engine") == rpf.ENGINE and p.get("centre") is not None and s["lo"] == p["lo"] and s["hiReq"] == p["hi"]:
        return p["centre"]
    return None


def scan_geometry_for(station: Station, s: dict, integ=None, ftime=None) -> dict:
    """The engine's own geometry for these settings (what `marsad run --dry-run` prints),
    with the keys the page's flags() reads. `integ` / `ftime` override, for the trade-off table."""
    if s["engine"] == rpf.ENGINE:
        t = s["ftime"] if ftime is None else ftime
        over = s["fover"] if s["fover"] is not None else 100.0 * s["crop"]
        retune = float(station.data.get("retune_ms_per_hop_fftw", rpf.RETUNE_MS_DEFAULT))
        read = float(station.data.get("read_ms_per_buffer_fftw", rpf.READ_MS_DEFAULT))
        g = rpf.geometry(s["lo"], s["hiReq"], s["frate"], s["fbins"], t, over, s["crop"], s["durS"],
                         retune, _fftw_centre(station, s), read)
        g.update(engine=rpf.ENGINE, listen=g["listen_per_hop_s"], overhead=g["overhead_per_hop_s"],
                 passS=g["sweep_s"], hopMhz=g["hop_spacing_mhz"], hopWidthMhz=g["hop_mhz"])
    else:
        from .rtlpower import scan_geometry
        i = s["integ"] if integ is None else integ
        g = scan_geometry(s["lo"], s["hi"], s["bin"], i, s["durS"], s["crop"],
                          retune_ms_per_hop=station.retune_ms_per_hop)
        g.update(engine=None, hopMhz=g["hop_mhz"])
    g.update(retune=g["retune_s_per_sweep"], listening=g["listening_frac"], binKhz=g["bin_khz_predicted"],
             kept=g["kept_bins_per_hop"], mb=g["csv_mb"], perFreq=g["listen_per_freq_s"],
             occStep=g["occupancy_step_pct"], ceilPerFreq=s["durS"] / max(1, g["hops"]),
             centres=g["hop_centres_mhz"])
    return g


def time_split(g: dict, integ) -> dict | None:
    """builder.html timeSplit(): kept / cropped / retuning shares; None when a sweep cannot
    finish inside -i."""
    if not g["engine"] and integ <= g["retune"]:
        return None
    kept = g["kept"] / g["fft"] if g["fft"] else 1
    return {"kept": g["listening"] * kept, "cropped": g["listening"] * (1 - kept), "retune": 1 - g["listening"]}


def scan_timeline(g: dict, s: dict) -> dict:
    """What happens to the run's time, in the engine's own terms: the recording lasts the
    duration; `--skip-warmup` is applied at analysis, as whole sweeps (record.py rounds up and
    always keeps at least the last sweep), so warm-up is inside the duration, never added to it."""
    total = float(s["durS"])
    per = float(g["passS"] if g["engine"] else s["integ"]) or 1.0
    sweeps = int(g["sweeps"] or 0)
    word = "passes" if g["engine"] else "sweeps"
    out = {"total_s": total, "per_s": per, "sweeps": sweeps, "unit": word, "analysed": bool(s["analyze"])}
    if not s["analyze"]:
        out.update(skipped=0, warm_s=0.0, analysed_s=0.0, level="info",
                   text=f"Records {_secs(total)}. Analysis is off, so nothing is discarded or analysed when it "
                        "ends (run `marsad analyze` on it later).")
        return out
    want = s["skipmin"] * 60 if s["useskip"] else 0.0
    skipped = min(max(0, sweeps - 1), math.ceil(want / per)) if want > 0 else 0
    warm = min(total, skipped * per)
    out.update(skipped=skipped, warm_s=warm, analysed_s=max(0.0, total - warm))
    if want > 0 and want >= total:
        out.update(level="bad", text=f"Records {_secs(total)}, but the warm-up to discard ({_secs(want)}) is as long "
                                     "as the run: the whole run would be warm-up, and nothing would be analysed "
                                     f"(the analyser keeps only the last of the {sweeps} {word}).")
    elif skipped:
        out.update(level="ok", text=f"Records {_secs(total)}: the first {_secs(warm)} ({skipped} {word}) are warm-up "
                                    f"and left out of the analysis, then {_secs(total - warm)} "
                                    f"({max(0, sweeps - skipped)} {word}) are analysed. Warm-up is part of the "
                                    "duration, not added to it.")
    else:
        out.update(level="ok", text=f"Records {_secs(total)}, all of it analysed ({sweeps} {word}); no warm-up is "
                                    "discarded.")
    return out


def default_sources(station: Station, rx: str | None = None) -> dict:
    """Where each radio-dependent default comes from, in plain words (relay 011): station.json
    (read here, raw, with its own `_key` note when it has one), or marsad's built-in starting value,
    or the window's own. Never station.local.json. Nothing is guessed: a value with no note says
    only where it is set, and names the command that would measure it."""
    from . import capture as cap
    try:
        raw = json.loads(station.file.read_text(encoding="utf-8")) if station.file.exists() else {}
    except (OSError, ValueError):
        raw = {}
    raw = raw if isinstance(raw, dict) else {}
    rx = rx if rx in station.receivers else station.default_receiver
    rraw = ((raw.get("receivers") or {}).get(rx) or {}) if rx else {}
    rec_raw = raw.get("recorder") or {}
    cfg = cap.settings(station)

    def item(value, where, note="", measure="", generic=False):
        text = {"station": "from station.json, entered by hand",
                "builtin": "not measured here: marsad's built-in starting value",
                "window": "not measured: a generic starting value"}[where]
        return {"value": value, "where": where, "text": text, "note": str(note or ""), "measure": measure,
                "generic": generic or where != "station", "scope": "radio"}

    out = {}
    pre_skip = {k: v.get("skip_warmup") for k, v in (raw.get("presets") or {}).items()
                if isinstance(v, dict) and v.get("skip_warmup") is not None}
    out["warmup"] = item(f"{jsnum(SCAN_DEFAULTS['skipmin'])} min", "window",
                         measure="`marsad drift <tag>` on a run started cold (ideally with a 50 ohm load): it "
                                 "reports the sweep from which the floor stays settled")
    if pre_skip:
        out["warmup"]["note"] = "station.json presets with their own warm-up: " + ", ".join(
            f"{k} {jsnum(v)} min" for k, v in pre_skip.items())
    out["retune"] = item(f"{jsnum(station.retune_ms_per_hop)} ms per hop",
                         "station" if "retune_ms_per_hop" in raw else "builtin", raw.get("_retune_ms_per_hop"),
                         "`marsad calibrate-retune`")
    out["retune_fftw"] = item("59 ms per hop + 16 ms per extra buffer read",
                              "station" if "retune_ms_per_hop_fftw" in raw else "builtin",
                              raw.get("_retune_ms_per_hop_fftw"),
                              "a short rtl_power_fftw run: its log gives per-hop timings")
    gaps = (station.receivers.get(rx) or {}).get("gaps_mhz") or []
    out["gap"] = item(", ".join(f"{jsnum(a)}–{jsnum(b)} MHz" for a, b in gaps) or "none",
                      "station" if "gaps_mhz" in rraw else "builtin", rraw.get("_gaps_mhz"),
                      "`rtl_test -t` (E4000 tuners report where the PLL will not lock); it moves as the radio warms")
    rng = (station.receivers.get(rx) or {}).get("range_mhz")
    out["range"] = item(f"{jsnum(rng[0])}–{jsnum(rng[1])} MHz" if rng else "not set",
                        "station" if "range_mhz" in rraw else "builtin", rraw.get("_range_mhz"),
                        "`rtl_test -t` for E4000 tuners; otherwise the tuner's datasheet")
    df = station.detection_floors
    out["detection_floor"] = item(", ".join(f"{k} {jsnum(v)} dB" for k, v in df.items()) or "not set",
                                  "station" if "detection_floor_db" in raw else "builtin",
                                  raw.get("_detection_floor_db"),
                                  "two terminated runs back to back, compared with `marsad compare-runs`")
    out["threshold"] = item(f"{jsnum(SCAN_DEFAULTS['thresh'])} dB", "window",
                            measure="a choice, not a measurement: raise it if the noise produces detections")
    out["trigger"] = item(f"{jsnum(cfg.get('threshold_db', 12))} dB",
                          "station" if "threshold_db" in rec_raw else "builtin", rec_raw.get("_threshold_db"),
                          "a choice: look at what a watch caught (`marsad watch report`) and adjust")
    keep = cfg.get("keep_min_snr_db") or 0
    out["keep"] = item("off" if not keep else f"{jsnum(keep)} dB",
                       "station" if "keep_min_snr_db" in rec_raw else "builtin", rec_raw.get("_keep_min_snr_db"),
                       "a choice: compare kept and refused events in `marsad watch report`")
    for k in ("retune", "retune_fftw", "detection_floor", "threshold", "trigger", "keep", "warmup"):
        out[k]["scope"] = "station"             # one figure for every radio on this station, not per radio
    return {"receiver": rx, "items": out}


def scan_fingerprint(station: Station, s: dict) -> str:
    """The fingerprint `marsad run` would write, by marsad's own function."""
    if s["engine"] == rpf.ENGINE:
        q = {"engine": rpf.ENGINE, "rx": s["rx"], "lo": s["lo"], "hi": s["hiReq"], "gain": s["gain"],
             "bins": s["fbins"], "rate": s["frate"], "time": s["ftime"],
             "overlap": s["fover"] if s["fover"] is not None else 100 * s["crop"], "crop": s["crop"]}
    else:
        q = {"engine": "rtl_power", "rx": s["rx"], "lo": s["lo"], "hi": s["hiReq"], "gain": s["gain"],
             "bin": s["bin"], "integ": s["integ"], "crop": s["crop"], "fir": s["fir"]}
    return builder_fingerprint(station, q)


def _is_load(chain_text: str) -> bool:
    """The page asked whether the chain starts with its "50 ohm load" module; here the
    engine's own reading of a chain's source (record.source_is_load) decides."""
    from .record import source_is_load
    return source_is_load(chain_text) is True


def _enclosure(chain_text: str, enclosures: list) -> str | None:
    """The enclosure the chain text names, as the Builder's SDR module carried it."""
    for e in sorted(enclosures, key=len, reverse=True):
        if f"({e})" in chain_text:
            return e
    return None


def scan_command(station: Station, g: dict, s: dict, chain_text: str) -> dict:
    """builder.html command(): the `marsad run` line, the --dry-run line first when asked,
    and the notes after it. Also `argv`, the same line as arguments for launching it."""
    presets = scan_presets(station)
    p = presets.get(s["preset"]) or {}
    dur = jsnum(s["durVal"]) + s["unit"]
    q = lambda t: '"' + t.replace('"', "'") + '"'
    cli_preset = p.get("as") or s["preset"]
    parts = [("marsad run",), (cli_preset,), (dur,)]       # display tokens; argv follows the same order
    argv = ["run", cli_preset, dur]

    def opt(flag, value=None, raw=None):
        parts.append((flag + (" " + (raw if raw is not None else str(value)) if value is not None else ""),))
        argv.append(flag)
        if value is not None:
            argv.append(str(value))

    def mhz(v):
        return jsnum(v if float(v).is_integer() else round(float(v), 6)) + "M"
    if g["engine"]:
        pf = p.get("engine") == rpf.ENGINE
        if not pf:
            opt("--engine", rpf.ENGINE)
        single = pf and p.get("centre") is not None and s["lo"] == p.get("lo") and s["hiReq"] == p.get("hi")
        if not single and (not pf or s["lo"] != p.get("lo") or s["hiReq"] != p.get("hi") or cli_preset == "custom"):
            opt("--freq", mhz(s["lo"]) + ":" + mhz(s["hiReq"]))
        if not pf or s["fbins"] != p.get("bins"):
            opt("--bins", jsnum(s["fbins"]))
        if not pf or s["ftime"] != p.get("time"):
            opt("--time", jsnum(s["ftime"]))
        if not pf or s["frate"] != p.get("rate"):
            opt("--rate", jsnum(s["frate"]))
        over = s["fover"] if s["fover"] is not None else 100 * s["crop"]
        if not pf or over != p.get("overlap"):
            opt("--overlap", jsnum(over))
        if s["gain"] != p.get("gain"):
            opt("--gain", jsnum(s["gain"]))
    else:
        if s["lo"] != p.get("lo") or s["hiReq"] != p.get("hi") or s["bin"] != p.get("bin") or cli_preset == "custom":
            opt("--freq", mhz(s["lo"]) + ":" + mhz(s["hiReq"]) + ":" + jsnum(s["bin"]) + "k")
        if s["gain"] != p.get("gain"):
            opt("--gain", jsnum(s["gain"]))
        if s["integ"] != p.get("integ"):
            opt("--integration", jsnum(s["integ"]))
    if not p.get("rx") or s["rx"] != p.get("rx"):
        opt("--receiver", s["rx"])
    if s["device"] != "":
        opt("--device", s["device"])
    if s["tag"]:
        opt("--tag", s["tag"])
    if chain_text:
        opt("--chain", chain_text.replace('"', "'"), raw=q(chain_text))
    cond = conditions_text(s)
    if cond:
        opt("--conditions", cond.replace('"', "'"), raw=q(cond))
    if s["thresh"] != 10:
        opt("--threshold", jsnum(s["thresh"]))
    if s["floorwin"]:
        opt("--floor-window", jsnum(s["floorwin"]))
    if s["useskip"] and s["skipmin"] > 0:
        opt("--skip-warmup", jsnum(s["skipmin"]))
    if s["crop"] != 0.25:
        opt("--crop", jsnum(s["crop"]))
    if not g["engine"] and s["fir"] != 9:
        opt("--fir", jsnum(s["fir"]))
    if not s["analyze"]:
        opt("--no-analyze")
    if s["nonint"]:
        opt("--non-interactive")
    line = " ".join(t[0] for t in parts)
    tuner = station.tuner_of(s["rx"])
    load = _is_load(chain_text)
    after = []
    if not load:
        after.append("marsad adsb-check --seconds 600 --gain " + ("29" if tuner == "e4000" else "40.2")
                     + " --device " + (s["device"] or "0") + " --label " + q(s["tag"] or "run")
                     + ("   # cold E4000: warm, its PLL gap can reach 1090 MHz" if tuner == "e4000" else ""))
    after.append("# archived and flagged " + ("--terminated" if load else "--antenna")
                 + " automatically from the chain above")
    after.append("# compare: marsad compare-runs <before-tag> <after-tag>")
    after.append("# events (broadband steps, one-sweep lifts): in report.md; rerun with marsad events "
                 + (s["tag"] or "<tag>"))
    if chain_text and cond and not s["nonint"]:
        after.append("# --chain and --conditions together skip the station-notes questions; "
                     "the 'Start anyway?' device prompt still asks")
    text = (line + " --dry-run\n" if s["dryfirst"] else "") + line + "\n" + "\n".join(after)
    return {"line": line, "text": text, "argv": argv, "after": after}


def _station_notes(station: Station, s: dict, g: dict, chain_text: str, out: list) -> None:
    """builder.html stationNotes(): station.json's builder_notes, matched as the page did.
    A note keyed on a Builder chain module (`module_on`) matches when that module's name is
    in the chain text, since the chain is now drawn, not ticked."""
    modules = {m.get("id"): m.get("name", "") for m in station.data.get("chain_modules", [])}
    tuner = station.tuner_of(s["rx"])
    for n in station.data.get("builder_notes", []):
        w = n.get("when") or {}
        if w.get("rx") and s["rx"] != w["rx"]:
            continue
        if w.get("rx_not") and s["rx"] == w["rx_not"]:
            continue
        if w.get("tuner") and tuner != w["tuner"]:
            continue
        if w.get("preset") and s["preset"] != w["preset"]:
            continue
        if "engine" in w and bool(g["engine"]) != w["engine"]:
            continue
        if w.get("span") and not (s["lo"] < w["span"][1] and s["hiReq"] > w["span"][0]):
            continue
        if "includes" in w and not (s["lo"] <= w["includes"] <= s["hiReq"]):
            continue
        if w.get("exact") and not (s["lo"] == w["exact"][0] and s["hiReq"] == w["exact"][1]):
            continue
        if "hi_above" in w and not s["hiReq"] > w["hi_above"]:
            continue
        if "hi_at_most" in w and not s["hiReq"] <= w["hi_at_most"]:
            continue
        if "gain_above" in w and not s["gain"] > w["gain_above"]:
            continue
        if "gain_not" in w and s["gain"] == w["gain_not"]:
            continue
        if w.get("not_load") and _is_load(chain_text):
            continue
        if "skip_below" in w and s["useskip"] and s["skipmin"] >= w["skip_below"]:
            continue
        if w.get("chain_has") and not re.search(w["chain_has"], chain_text, re.I):
            continue
        if w.get("chain_lacks") and re.search(w["chain_lacks"], chain_text, re.I):
            continue
        if w.get("module_on") and not (modules.get(w["module_on"]) and modules[w["module_on"]] in chain_text):
            continue
        out.append([n.get("level") or "warn", n.get("title") or "Note", n.get("text") or ""])


def scan_warnings(station: Station, g: dict, s: dict, chain_text: str) -> list:
    """builder.html flags(): [level, title, text], level bad / warn / ok, on the engine's
    own geometry. Same cases, same words."""
    out = []
    rcv = station.receivers.get(s["rx"]) or {}
    min_f, max_f = rcv.get("range_mhz", [24, 1766])
    ceil = RTL_POWER_MAX_MHZ
    if s["hiReq"] <= s["lo"]:
        out.append(["bad", "Range", "Stop must be above start."])
    if s["lo"] < min_f or s["hiReq"] > max_f:
        out.append(["bad", "Tuner", f"The {s['rx'].upper()} covers {jsnum(min_f)}–{jsnum(max_f)} MHz. "
                                    "This range goes outside it."])
    if s["clamped"] and not g["engine"]:
        out.append(["warn", "Ceiling", f"rtl_power stops at {jsnum(ceil)} MHz, a signed 32-bit limit. The top is "
                                       f"silently clamped from {_num(s['hiReq'], 3)}. A range entirely above it "
                                       "writes no file and still exits 0."])
    if g["engine"] and s["hiReq"] > ceil:
        out.append(["ok", "Ceiling", f"Above rtl_power's {jsnum(ceil)} MHz limit: fine for rtl_power_fftw (64-bit "
                                     "frequencies). Hops it refuses are dropped and named in the .log, and the Scan "
                                     "page hatches them."])
    if g["engine"] and s["fbins"] % 2:
        out.append(["bad", "Bins", "rtl_power_fftw wants an even number of FFT bins."])
    over = s["fover"] if s["fover"] is not None else 100 * s["crop"]
    if g["engine"] and g["hops"] > 1 and s["crop"] > 0 and over < 100 * s["crop"] - 1e-9:
        out.append(["bad", "Overlap", f"With crop {jsnum(s['crop'])} the hops must overlap by at least "
                                      f"{_num(100 * s['crop'], 0)}% or the cropped edges leave gaps between hops."])
    if g["engine"] and g["listening"] < 0.5:
        out.append(["warn", "Dwell", f"Only {_num(g['listening'] * 100, 0)}% of the time is spent listening: "
                                     f"{_num(g['overhead'] * 1000, 0)} ms of overhead per hop against "
                                     f"{_num(g['listen'], 2)} s of listening. More seconds per hop fixes it."])
    for a, b in rcv.get("gaps_mhz", []):
        if s["lo"] < b and s["hiReq"] > a:
            out.append(["warn", "PLL gap", f"{jsnum(a)}–{jsnum(b)} MHz is this receiver's lock gap (rtl_test -t "
                                           "measures yours). Hops there record nothing useful; the analyser masks it."])
    _station_notes(station, s, g, chain_text, out)
    if g["engine"]:
        pass                                   # no retune floor: the program waits for each hop
    elif s["integ"] < g["retune"] * 1.2:
        out.append(["bad", "Dwell", f"-i {jsnum(s['integ'])} is under the {_num(g['retune'], 1)} s retune floor for "
                                    f"{g['hops']} hops. rtl_power misbehaves when a sweep outlasts the interval."])
    elif g["listening"] < 0.5:
        out.append(["bad", "Dwell", f"Only {_num(g['listening'] * 100, 0)}% of the time is spent listening — most "
                                    "of the run is retuning. Fewer hops or a longer -i both fix it."])
    elif g["listening"] < 0.75:
        out.append(["warn", "Dwell", f"{_num(g['listening'] * 100, 0)}% listening: {_num(g['retune'], 1)} s of every "
                                     f"{jsnum(s['integ'])} s sweep is retuning. Doubling -i would reach "
                                     f"{_num(100 * (1 - g['retune'] / (s['integ'] * 2)), 0)}%. Over 80% is the "
                                     "station norm for long runs."])
    if not g["engine"] and g["fft"] and g["fft"] <= 32:
        out.append(["warn", "FFT", f"A {g['fft']}-point FFT gives only {g['kept']} bins per hop after crop. Narrow "
                                   "spur lines get averaged into the floor. Halving the bin request would give FFT "
                                   f"{g['fft'] * 2} at the same hop count and dwell — finer bins cost disk, not time."])
    if g["sweeps"] < 10:
        out.append(["warn", "Sweeps", f"{g['sweeps']} sweeps is thin for a 20th-percentile floor."])
    if s["useskip"] and s["skipmin"] * 60 >= s["durS"]:
        out.append(["bad", "Warm-up", "The warm-up discard is as long as the run."])
    if not g["engine"] and (s["crop"] != 0.25 or s["fir"] != 9):
        out.append(["warn", "Convention", "Crop and FIR differ from 0.25 / 9. Nothing already archived uses these, so "
                                          "this run will have no comparison set."])
    if g["engine"] and s["crop"] != rpf.DEFAULT_CROP:
        out.append(["warn", "Convention", f"Crop differs from {jsnum(rpf.DEFAULT_CROP)}, the station convention for "
                                          "this engine too."])
    if s["target"] is not None:
        best = None
        for c in g["centres"]:
            d = abs(c - s["target"])
            if best is None or d < best[0]:
                best = (d, c)
        if best and best[0] * 1000 < g["binKhz"] * 3:
            out.append(["bad", "Hop centre", f"A hop centre sits at {_num(best[1], 4)} MHz, {_num(best[0] * 1000, 1)} "
                                             "kHz from your target. The DC spike there will mask it — shift the start by "
                                             f"about half a hop ({_num(g['hopMhz'] / 2, 3)} MHz)."])
        elif best:
            out.append(["ok", "Hop centre", f"Nearest hop centre is {_num(best[1], 4)} MHz, {_num(best[0], 3)} MHz clear."])
    span = s["hiReq"] - s["lo"]
    if s["analyze"] and span < 8 and not s["floorwin"]:
        out.append(["warn", "Floor window", f"The analyser's 2 MHz rolling floor is a large share of a {_num(span, 1)} "
                                            "MHz span. Set Floor MHz to something well under it."])
    encl = station.data.get("enclosures") or ENCLOSURES_DEFAULT
    enc = _enclosure(chain_text, encl)
    if enc and re.search(r"(cage|box)-?off|open-?bench", s["tag"], re.I) and enc != encl[0]:
        out.append(["warn", "Enclosure", f'The tag says the enclosure is off but the chain records "{enc}".'])
    if enc == encl[0] and re.search(r"(cage|box)-?on", s["tag"], re.I):
        out.append(["warn", "Enclosure", "The tag says the enclosure is on but the chain records the SDR on the open bench."])
    if not chain_text:
        out.append(["bad", "Chain", "Nothing is connected, so no --chain is emitted and marsad run falls back to "
                                    "whatever station.json last remembered. That is exactly how two runs were "
                                    "mislabelled. Check at least the source, the SDR and the PC."])
    dev_cfg = rcv.get("device", 0)
    if s["device"] != "" and _f(s["device"]) != dev_cfg:
        out.append(["warn", "Device", f"--device {s['device']}, but station.json says the {s['rx'].upper()} "
                                      f"enumerates as index {dev_cfg}. Fine if USB order has since changed — but "
                                      "confirm with rtl_test first, because a wrong index silently records one "
                                      "dongle's data under the other's name."])
    load = _is_load(chain_text)
    if load:
        out.append(["ok", "Terminated", "A terminated run measures what each stage ADDS to the floor — never what it "
                                        "lets through. Pair it with marsad adsb-check for the sensitivity half, or the "
                                        "characterisation is incomplete."])
    if load and not re.search(r"term", s["tag"], re.I):
        out.append(["warn", "Tag", "The source is a 50 ohm load but the tag does not say so. archive.py reads the tag "
                                   "to flag terminated runs, and a mislabelled one pollutes every later comparison."])
    if not load and re.search(r"term", s["tag"], re.I):
        out.append(["warn", "Tag", "The tag says terminated but the antenna is connected."])
    return out


def scan_facts(station: Station, s: dict) -> list:
    """builder.html drawFacts(): the receiver's range, then station.json's known_mhz entries that
    fall in the span, [kind, label, text]."""
    rcv = station.receivers.get(s["rx"]) or {}
    lo_t, hi_t = rcv.get("range_mhz", [24, 1766])
    out = [["", f"{jsnum(lo_t)}–{jsnum(hi_t)} MHz", rcv.get("label", s["rx"])]]
    for f in station.data.get("known_mhz", []):
        a, b = f.get("a"), f.get("b")
        if a is None or b is None or b < s["lo"] or a > s["hiReq"]:
            continue
        text = _num(a, 2 if a % 1 else 0) if a == b else f"{_num(a, 0)}–{_num(b, 0)}"
        out.append([f.get("k") or "", text, f.get("t", "")])
    return out


def scan_cells(g: dict, s: dict) -> list:
    """builder.html cells(): what the program will really do, [label, value, unit, key]."""
    finish = dt.datetime.now() + dt.timedelta(seconds=s["durS"])
    per = g["passS"] if g["engine"] else s["integ"]
    skipped = min(g["sweeps"] - 1, math.ceil(s["skipmin"] * 60 / per)) if s["useskip"] else 0
    when = ["Finishes", finish.strftime("%H:%M"), "today" if finish.date() == dt.date.today() else "tomorrow", False]
    occ = _num(100 / max(1, g["sweeps"] - skipped), 2) + "%"
    if g["engine"]:
        spectra = math.ceil(g["sample_rate_hz"] * g["listen"] / g["fft"]) if g["fft"] else 0
        return [
            ["Hops", _num(g["hops"]), f"× {_num(g['hopWidthMhz'], 4)} MHz, spaced {_num(g['hopMhz'], 4)}", False],
            ["Sample rate", _num(g["sample_rate_hz"] / 1e6, 6), "MS/s", False],
            ["FFT bins", _num(g["fft"]), f"{g['kept']} kept per hop after crop", False],
            ["Bin width", _num(g["binKhz"], 4), "kHz", False],
            ["Bins logged", _num(g["bins"]), "", False],
            ["Spectra per hop", _num(spectra), f"{_num(g['listen'], 3)} s listening, {g['reads_per_hop']} "
                                               + ("read" if g["reads_per_hop"] == 1 else "reads"), False],
            ["One pass", _secs(g["passS"]), f"{_num(g['overhead'] * 1000, 0)} ms overhead per hop", False],
            ["Passes", _num(g["sweeps"]), f"{_num(g['sweeps'] - skipped)} after warm-up" if skipped
             else "ends after the pass in which the duration elapses", False],
            ["Listening", _num(g["listening"] * 100, 1) + "%", "of each pass", False],
            ["Listen per frequency", _secs(g["perFreq"]), "ceiling " + _secs(g["ceilPerFreq"]), True],
            ["Occupancy step", occ, "one pass", False],
            ["File size", _num(g["mb"], 1), "MB (.dat)", False],
            when]
    return [
        ["Hops", _num(g["hops"]), f"× {_num(g['hopMhz'], 4)} MHz", False],
        ["Sample rate", _num(g["sample_rate_hz"] / 1e6, 6), "MS/s", False],
        ["FFT size", _num(g["fft"]), f"{g['kept']} kept per hop", False],
        ["Actual bin", _num(g["binKhz"], 4), "kHz", False],
        ["Bins logged", _num(g["bins"]), "", False],
        ["Sweeps", _num(g["sweeps"]), f"{_num(g['sweeps'] - skipped)} after warm-up" if skipped else "", False],
        ["Listening", _num(g["listening"] * 100, 1) + "%", "of each sweep", False],
        ["Listen per frequency", _secs(g["perFreq"]), "ceiling " + _secs(g["ceilPerFreq"]), True],
        ["Occupancy step", occ, "one sweep", False],
        ["CSV size", _num(g["mb"], 1), "MB", False],
        ["Retune per sweep", _num(g["retune"], 2), "s", False],
        when]


def scan_tradeoff(station: Station, s: dict) -> list:
    """builder.html tradeoff(): one row per integration (or seconds per hop), engine geometry."""
    rows = []
    if s["engine"] == rpf.ENGINE:
        opts = sorted(set(TRADEOFF_TIME) | {s["ftime"]})
        for t in opts:
            g = scan_geometry_for(station, s, ftime=t)
            rows.append({"value": t, "label": f"-t {jsnum(t)}", "ok": g["listening"] >= 0.5, "now": t == s["ftime"],
                         "listening": g["listening"], "split": time_split(g, t), "sweeps": g["sweeps"],
                         "per_freq": _secs(g["perFreq"]), "occ_step": g["occStep"], "mb": g["mb"]})
        return rows
    for i in sorted(set(TRADEOFF_INTEG) | {s["integ"]}):
        g = scan_geometry_for(station, s, integ=i)
        rows.append({"value": i, "label": f"-i {jsnum(i)}", "ok": i >= g["retune"] * 1.2, "now": i == s["integ"],
                     "listening": g["listening"], "split": time_split(g, i), "sweeps": g["sweeps"],
                     "per_freq": _secs(g["perFreq"]), "occ_step": g["occStep"], "mb": g["mb"]})
    return rows


def scan_plan(station: Station, form: dict) -> dict:
    """POST /scan/plan: everything the setup panel shows for one scan: the engine's geometry
    and fingerprint, the Builder's warnings, the trade-off table and the full command.
    `base` (optional) is the saved scan or preset it came from: if a locked setting differs,
    the fingerprint changes and `new_set` says so."""
    s = scan_settings(station, form.get("scan") or {})
    chain_text = str(form.get("chain") if form.get("chain") is not None else "").strip()
    g = scan_geometry_for(station, s)
    fp = scan_fingerprint(station, s)
    base_fp = None
    if isinstance(form.get("base"), dict):
        try:
            base_fp = scan_fingerprint(station, scan_settings(station, form["base"]))
        except (ValueError, KeyError, TypeError, ZeroDivisionError, SystemExit):
            base_fp = None
    cmd = scan_command(station, g, s, chain_text)
    geo = {k: g[k] for k in ("hops", "hop_mhz", "sample_rate_hz", "fft", "bin_khz_predicted", "kept_bins_per_hop",
                             "bins", "sweeps", "retune_s_per_sweep", "listening_frac", "listen_per_freq_s",
                             "occupancy_step_pct", "csv_mb", "hop_centres_mhz")}
    if g["engine"]:
        geo["covered_mhz"] = g.get("covered_mhz")
        geo["hop_spacing_mhz"] = g["hop_spacing_mhz"]
    return {"settings": s, "engine": s["engine"], "geometry": geo, "fingerprint": fp, "base_fingerprint": base_fp,
            "new_set": bool(base_fp and base_fp != fp),
            "warnings": scan_warnings(station, g, s, chain_text), "cells": scan_cells(g, s),
            "facts": scan_facts(station, s),
            "split": time_split(g, s["integ"]), "tradeoff": scan_tradeoff(station, s),
            "timeline": scan_timeline(g, s),
            "conditions": conditions_text(s), "command": cmd["text"], "line": cmd["line"],
            # relay 001: rtl_power's real listening share is about 6 % whatever -i is; the
            # engine still reports its dwell model, so the window marks the figure under review.
            "listening_under_review": s["engine"] == "rtl_power"}


# ---- checks before starting

def detect_receivers(station: Station) -> dict:
    """The dongles plugged in, by rtl_eeprom (probe.detect_receivers). A module-level name so
    the tests replace it: no test ever opens a radio."""
    return probe.detect_receivers(station)


def radio_in_use(station: Station) -> dict:
    """Rule 7: one program per radio. `marsad run` refuses to start while any radio program
    is running (record.check_device_free), so the window does too, naming it, and naming
    which radio it holds where a recording or a recorder says so."""
    busy = host.running_sdr_programs()
    held = []
    if station.surveys.exists():
        for f in list(station.surveys.glob("*.csv")) + list(station.surveys.glob("*.dat")):
            if recording_active(f):
                try:
                    rx = json.loads(f.with_suffix(".json").read_text()).get("receiver")
                except (OSError, ValueError):
                    rx = None
                held.append(f"{radio_word(rx) if rx else 'a radio'} is recording {f.name}")
    try:
        from . import catalogue
        for d in catalogue.live_recorders(station):
            if d.get("running"):
                held.append(f"{radio_word(d.get('receiver') or d.get('serial') or 'a radio')} is held by "
                            f"marsad watch ({d.get('mode') or 'recorder'})")
    except Exception:  # noqa: BLE001 - a broken status file must not hide the process check
        pass
    if busy is None:
        return {"blocked": False, "programs": None, "held": held,
                "text": "Could not check for running radio programs. Make sure SDR++ and other radio programs are closed."}
    if busy:
        return {"blocked": True, "programs": busy, "held": held,
                "text": "In use: " + ", ".join(busy) + " is running" + (f" ({'; '.join(held)})" if held else "")
                        + ". Only one program can use a radio at a time, and marsad run refuses to start while one "
                          "is running. Close it, or wait for the run to finish."}
    return {"blocked": False, "programs": [], "held": held, "text": "No other radio program is running."}


def find_radio(station: Station, rx: str) -> dict:
    """Rule 8: the radio by its serial (recorder.serials), today's USB index from that."""
    from . import capture as cap, watch
    cfg = cap.settings(station)
    try:
        r = watch.resolve_receiver(station, rx, cfg, detect=lambda st: detect_receivers(st))
    except SystemExit as exc:
        return {"found": False, "rx": rx, "serial": cap.serial_of(cfg, rx), "text": str(exc).strip().split("\n  Nothing")[0]}
    return {"found": True, "rx": rx, "serial": r["serial"], "device": r["device"],
            "text": f"{radio_word(rx)} found by serial {r['serial']} at USB index {r['device']}."}


def chain_for(station: Station, rx: str) -> dict:
    """Rule 6: the chain this radio's run records, and whether "still built as drawn?" is due:
    the same tests record.confirm_default_chain makes before it would ask."""
    d = station.defaults
    try:
        rig = chain.rig_text_for(station, rx)
    except SystemExit as exc:
        return {"ok": False, "text": "", "source": "none", "due": False, "used_at": None,
                "problem": str(exc).strip()}
    text = rig if rig is not None else (d.get("chain_order") or "")
    used = d.get("chain_used_at")
    if rig is not None:
        static = chain.texts_are_static(station, (d.get("chain_by_serial") or {}).values())
        source = f"the rig used for runs, path to the {radio_word(rx)}"
    else:
        static = chain.default_is_static(station, text) if text else False
        source = "the saved default chain"
    return {"ok": bool(text), "text": text, "source": source, "used_at": used, "static": bool(static),
            "due": bool(text and used and not static),
            "problem": None if text else "No chain is saved for this radio. Draw it on the Chain screen and use it for runs."}


def scan_checks(station: Station, rx: str, detect: bool = True) -> dict:
    """Everything checked before a start, in plain words. Detection asks the dongles their
    serials (rtl_eeprom), so it is skipped while another radio program runs."""
    use = radio_in_use(station)
    if use["blocked"]:
        radio = {"found": None, "rx": rx, "text": "Not checked while another radio program is running."}
    elif not detect:
        radio = {"found": None, "rx": rx, "text": "Not checked yet: Start asks it its serial, or Check again does now."}
    else:
        radio = find_radio(station, rx)
    if rx in disabled_radios(station):              # the window's own switch: never offered, never started
        radio = {"found": False, "rx": rx, "disabled": True,
                 "text": f"The {radio_word(rx)} is disabled in this window (the switch on its card). Enable it "
                         "there to use it; its role and settings are kept."}
    return {"in_use": use, "radio": radio, "chain": chain_for(station, rx)}


# ---- launching outside every process tree (rule 1)

def marsad_argv(station: Station, args: list) -> list:
    """`marsad --station ROOT <args>` as an argument list for this install. A module-level
    name so a test can launch a harmless stand-in through the very same path."""
    return host.self_command("--station", str(station.root), *args, prefer_venv=station.venv_python)


_LAUNCHER = ("import json,os,subprocess,sys;a=json.load(open(sys.argv[1],encoding='utf-8'));"
             "e=dict(os.environ,PYTHONUNBUFFERED='1');f=open(a['log'],'ab');"
             "sys.exit(subprocess.call(a['argv'],cwd=a['cwd'],env=e,stdin=subprocess.DEVNULL,stdout=f,"
             "stderr=subprocess.STDOUT))")


def launch_outside(station: Station, argv: list, label: str) -> dict:
    """Start `argv` so that nothing this service or the window does can end it.

    On Windows the run is created by WMI (Win32_Process.Create, the method a session used to
    keep runs alive across desktop-app restarts): its parent is the WMI host, not this
    service, and it is in none of this service's job objects, so killing or restarting the
    service, or closing the window, leaves it running. It gets its own hidden console, which
    is what lets Stop send Ctrl-Break to rtl_power. A small Python wrapper sends what the run
    prints to logs/<stamp>_<label>.log. Elsewhere: a new session, detached."""
    import subprocess
    import sys as _sys
    from .config import source_root
    logs = station.root / LAUNCH_LOG_DIR
    logs.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", label)[:60] or "run"
    log = logs / f"{stamp}_{safe}.log"
    spec = logs / f"{stamp}_{safe}.launch.json"
    cwd = str(source_root() or station.root)
    spec.write_text(json.dumps({"argv": [str(a) for a in argv], "cwd": cwd, "log": str(log)}, indent=1),
                    encoding="utf-8")
    py = argv[0] if str(argv[0]).lower().endswith(("python.exe", "pythonw.exe", "python", "python3")) else _sys.executable
    wrapper = [str(py), "-c", _LAUNCHER, str(spec)]
    if not host.IS_WINDOWS:
        p = subprocess.Popen(wrapper, cwd=cwd, start_new_session=True, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"pid": p.pid, "log": str(log), "spec": str(spec)}
    ps = (r"$ErrorActionPreference='Stop';"
          r"$si=New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ShowWindow=[UInt16]0};"
          r"$r=Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments "
          r"@{CommandLine=$env:MARSAD_LAUNCH_CMD;CurrentDirectory=$env:MARSAD_LAUNCH_CWD;ProcessStartupInformation=$si};"
          r"Write-Output ('{0} {1}' -f $r.ReturnValue,$r.ProcessId)")
    import os as _os
    import shutil as _sh
    exe = _sh.which("powershell.exe") or r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    env = dict(_os.environ, MARSAD_LAUNCH_CMD=subprocess.list2cmdline(wrapper), MARSAD_LAUNCH_CWD=cwd)
    r = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, text=True,
                       env=env, timeout=60, stdin=subprocess.DEVNULL,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    m = re.search(r"(\d+)\s+(\d+)", r.stdout or "")
    if r.returncode != 0 or not m or m.group(1) != "0":
        raise RuntimeError("Windows would not start the run outside the service "
                           f"(WMI said: {(r.stdout or '').strip() or (r.stderr or '').strip()[:300]}). Nothing was started.")
    return {"pid": int(m.group(2)), "log": str(log), "spec": str(spec)}


def _dry_run(station: Station, args: list) -> dict:
    """The engine's own dry run of exactly this command: parses for real, prints the geometry
    and the filename, opens no radio."""
    import subprocess
    from .config import source_root
    argv = marsad_argv(station, [*args, "--dry-run"])
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=120, cwd=source_root() or None,
                           stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace",
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"the dry run could not be started: {exc}")
    return {"exit_code": r.returncode, "output": ((r.stdout or "") + (r.stderr or "")).rstrip()}


def _remember_conditions(station: Station, s: dict) -> None:
    doc = saved_doc(station)
    doc["conditions_last"] = {k: s.get(k, "") for k in ("temp", "humid", "where", "sky", "notes")}
    _saved_write(station, doc)


def scan_start(station: Station, form: dict) -> dict:
    """POST /scan/start. `dry_run`: the engine's dry run of the complete command. Otherwise:
    every check again on the server (a radio in use refuses; the radio must be found by
    serial; a chain due for "still built as drawn?" needs `chain_confirmed`; a "bad" warning
    needs `problems_confirmed`), then the run is launched outside every process tree."""
    s = scan_settings(station, form.get("scan") or {})
    s["nonint"], s["dryfirst"] = True, False          # nobody answers prompts in a launched run
    dry = bool(form.get("dry_run"))
    chk = scan_checks(station, s["rx"], detect=not dry or bool(form.get("detect")))
    if form.get("chain") not in (None, ""):
        chain_text = str(form["chain"]).strip()
    else:
        chain_text = chk["chain"]["text"]
    if chk["radio"].get("found"):
        s["device"] = str(chk["radio"]["device"])
    g = scan_geometry_for(station, s)
    warnings = scan_warnings(station, g, s, chain_text)
    cmd = scan_command(station, g, s, chain_text)
    if dry:
        out = _dry_run(station, cmd["argv"])
        return {"dry_run": True, "line": cmd["line"], "argv": cmd["argv"], **out, "checks": chk}
    if chk["in_use"]["blocked"]:
        raise ValueError(chk["in_use"]["text"] + " Nothing was started.")
    if not chk["radio"].get("found"):
        raise ValueError(chk["radio"]["text"] + " Nothing was started.")
    if not chain_text:
        raise ValueError((chk["chain"].get("problem") or "No chain to record.") + " Nothing was started.")
    if chk["chain"]["due"] and form.get("chain") in (None, "") and not form.get("chain_confirmed"):
        raise ValueError("Is the rig still built as drawn? Confirm the chain first. Nothing was started.")
    bad = [w for w in warnings if w[0] == "bad"]
    if bad and not form.get("problems_confirmed"):
        raise ValueError("Problems to confirm first: " + "; ".join(w[1] for w in bad) + ". Nothing was started.")
    _remember_conditions(station, s)
    got = launch_outside(station, marsad_argv(station, cmd["argv"]), f"run_{s['preset']}_{s['tag'] or 'scan'}")
    return {"started": True, "line": cmd["line"], "argv": cmd["argv"], **got, "checks": chk}


# ---- saved scans and recorder sessions (rule 4: archived, never deleted)

def saved_doc(station: Station) -> dict:
    p = station.root / SAVED_SCANS_FILE
    doc = {"schema": 1, "scans": {}, "watches": {}, "conditions_last": {}, "roles": {}, "disabled": []}
    if p.exists():
        try:
            got = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(got, dict):
                for k in ("scans", "watches", "conditions_last", "roles"):
                    if isinstance(got.get(k), dict):
                        doc[k] = got[k]
                if isinstance(got.get("disabled"), list):
                    doc["disabled"] = [str(x) for x in got["disabled"]]
        except (OSError, ValueError) as exc:
            raise ValueError(f"{SAVED_SCANS_FILE} is unreadable: {exc}")
    return doc


def _saved_write(station: Station, doc: dict) -> None:
    p = station.root / SAVED_SCANS_FILE
    tmp = p.with_name(p.name + ".part")
    tmp.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    tmp.replace(p)


_LAST_RAN: dict = {}


def last_runs(station: Station) -> dict:
    """{fingerprint: (started, survey name)} for the newest recording of each fingerprint,
    from the sidecars in surveys/ (cached per sidecar and age). "Last ran" for a saved scan
    is the newest recording in its comparison set."""
    out: dict = {}
    if not station.surveys.exists():
        return out
    for side in station.surveys.glob("*.json"):
        try:
            key = (str(side), side.stat().st_mtime)
        except OSError:
            continue
        if key not in _LAST_RAN:
            fp = None
            try:
                d = json.loads(side.read_text(encoding="utf-8"))
                eng = d.get("engine") or "rtl_power"
                sec = d.get(eng) or {}
                if eng == rpf.ENGINE:
                    actual = (sec.get("rate_hz") or 0) / max(1, sec.get("bins") or 1) / 1e3
                else:
                    actual = csv_bin_khz(sec["freq_low_mhz"], sec["freq_high_mhz"], sec["bin_khz"], sec["crop"])
                fp = fingerprint(d, actual)
                _LAST_RAN[key] = (fp, d.get("started_local"), side.stem)
            except (OSError, ValueError, KeyError, TypeError, ZeroDivisionError):
                _LAST_RAN[key] = (None, None, side.stem)
        fp, started, stem = _LAST_RAN[key]
        if fp and started and (fp not in out or started > out[fp][0]):
            out[fp] = (started, stem)
    return out


def _scan_entry(station: Station, name: str, settings: dict, kind: str, runs: dict, extra: dict) -> dict:
    s = scan_settings(station, settings)
    try:
        fp = scan_fingerprint(station, s)
    except (ValueError, KeyError, TypeError, ZeroDivisionError):
        fp = None
    last = runs.get(fp) if fp else None
    return {"name": name, "kind": kind, "settings": {k: v for k, v in s.items() if k not in ("durS", "hi", "clamped")},
            "range": f"{jsnum(s['lo'])}–{jsnum(s['hiReq'])} MHz", "engine": s["engine"], "rx": s["rx"],
            "duration": jsnum(s["durVal"]) + s["unit"], "fingerprint": fp,
            "last_ran": last[0] if last else None, "last_survey": last[1] if last else None, **extra}


def saved_list(station: Station) -> dict:
    """GET /scan/saved: the owner's saved scans (archived ones apart), the station presets as
    starting points, the recorder sessions, and the last conditions answers."""
    doc = saved_doc(station)
    runs = last_runs(station)
    scans = [_scan_entry(station, n, v.get("settings") or {}, "saved", runs,
                         {"archived": bool(v.get("archived")), "updated": v.get("updated")})
             for n, v in doc["scans"].items()]
    presets = [_scan_entry(station, n, {"preset": n}, "preset", runs, {"note": p.get("note", ""), "archived": False})
               for n, p in scan_presets(station).items()]
    watches = [{"name": n, "settings": v.get("settings") or {}, "archived": bool(v.get("archived")),
                "updated": v.get("updated")} for n, v in doc["watches"].items()]
    cond = {"temp": "", "humid": "", "where": "inside", "sky": "", "notes": "", **doc["conditions_last"]}
    return {"scans": sorted(scans, key=lambda e: e["name"].lower()), "presets": presets,
            "watches": sorted(watches, key=lambda e: e["name"].lower()), "conditions_last": cond,
            "receivers": scan_receivers(station)}


def scan_receivers(station: Station) -> dict:
    from . import capture as cap
    cfg = cap.settings(station)
    out = {}
    for name, r in station.receivers.items():
        tuner = (r.get("tuner") or "e4000").lower()
        out[name] = {"label": r.get("label", name), "tuner": tuner, "range": r.get("range_mhz", [24, 1766]),
                     "gaps": r.get("gaps_mhz", []), "gains": TUNER_GAINS.get(tuner, TUNER_GAINS["e4000"]),
                     "serial": cap.serial_of(cfg, name)}
    return out


def saved_change(station: Station, form: dict) -> dict:
    """POST /scan/saved: save (create or update), rename, duplicate, archive or restore one
    saved scan (`kind` "scan") or recorder session (`kind` "watch"). Nothing is ever deleted:
    archiving hides an entry from the list and restore brings it back."""
    kind = "watches" if form.get("kind") == "watch" else "scans"
    action = str(form.get("action") or "save")
    name = str(form.get("name") or "").strip()
    if not name or len(name) > 80:
        raise ValueError("a saved scan needs a name of 1 to 80 characters")
    doc = saved_doc(station)
    items = doc[kind]
    now = dt.datetime.now().isoformat(timespec="seconds")
    if action == "save":
        settings = form.get("settings")
        if not isinstance(settings, dict):
            raise ValueError("nothing to save")
        if kind == "scans":
            s = scan_settings(station, settings)
            settings = {k: v for k, v in s.items() if k not in ("durS", "hi", "clamped", "device", "dryfirst", "nonint")}
        prev = items.get(name) or {}
        items[name] = {**prev, "settings": settings, "updated": now, "archived": False,
                       "created": prev.get("created") or now}
    elif action in ("rename", "duplicate"):
        new = str(form.get("new_name") or "").strip()
        if name not in items:
            raise ValueError(f"no saved entry called {name!r}")
        if not new or len(new) > 80:
            raise ValueError("the new name needs 1 to 80 characters")
        if new in items:
            raise ValueError(f"there is already one called {new!r}")
        items[new] = {**json.loads(json.dumps(items[name])), "updated": now}
        if action == "duplicate":
            items[new]["created"] = now
        else:
            del items[name]
    elif action in ("archive", "restore"):
        if name not in items:
            raise ValueError(f"no saved entry called {name!r}")
        items[name]["archived"] = action == "archive"
        items[name]["updated"] = now
    else:
        raise ValueError(f"unknown action {action!r}")
    _saved_write(station, doc)
    return saved_list(station)


# ---- recorder sessions (marsad watch), Part 4

def watch_settings(station: Station, form: dict) -> dict:
    from . import capture as cap, watch
    cfg = cap.settings(station)
    w = {"mode": "fixed" if form.get("mode") == "fixed" else "survey"}
    for k, _ in WATCH_FLAGS:
        v = form.get(k)
        w[k] = None if v in (None, "") else _clean_num(_f(v))
    if w["mode"] == "survey":
        sur, rec = watch.survey_roles(station, cfg, form.get("surveyor") or None, form.get("recorder") or None)
        scan = dict(form.get("scan") or {})
        scan["rx"] = sur
        w.update(scan=scan_settings(station, scan), surveyor=sur, recorder=rec,
                 recorder_gain=None if form.get("recorder_gain") in (None, "") else _clean_num(_f(form["recorder_gain"])))
    else:
        for k in ("passes", "ambush"):
            w[k] = None                       # survey-only triggers
        rx = form.get("receiver") if form.get("receiver") in station.receivers else station.default_receiver
        w.update(receiver=rx, freq=str(form.get("freq") or "").strip(), width=str(form.get("width") or "").strip(),
                 gain=None if form.get("gain") in (None, "") else _clean_num(_f(form["gain"])),
                 durVal=_clean_num(_f(form.get("durVal"), 1)), unit=form.get("unit") if form.get("unit") in ("m", "h") else "h")
    return w


def watch_command(station: Station, w: dict, chains: dict) -> dict:
    """`marsad watch survey|fixed ...` with each radio's own chain. The survey half is the
    same `marsad run` options the Builder writes (watch passes them to its survey child),
    less --device (watch finds both radios by serial) and --non-interactive (watch adds it),
    and less --threshold, which `marsad watch` takes as its own trigger level."""
    q = lambda t: '"' + t.replace('"', "'") + '"'
    toks = []

    def opt(flag, value=None, quote=False):
        toks.append((flag, None if value is None else str(value), quote))
    if w["mode"] == "survey":
        s = dict(w["scan"], device="", nonint=False, dryfirst=False, thresh=10)
        g = scan_geometry_for(station, s)
        run = scan_command(station, g, s, chains.get(w["surveyor"], ""))
        head = ["watch", "survey", run["argv"][1], run["argv"][2]]
        rest = run["argv"][3:]
        if w["surveyor"] != (station.data.get("recorder", {}).get("roles", {}).get("surveyor")):
            opt("--surveyor", w["surveyor"])
        if w["recorder"] != (station.data.get("recorder", {}).get("roles", {}).get("recorder")):
            opt("--recorder", w["recorder"])
        if w.get("recorder_gain") is not None:
            opt("--recorder-gain", jsnum(w["recorder_gain"]))
        if chains.get(w["recorder"]):
            opt("--recorder-chain", chains[w["recorder"]].replace('"', "'"), quote=True)
    else:
        head = ["watch", "fixed", "--freq", w["freq"], "--receiver", w["receiver"],
                "--duration", jsnum(w["durVal"]) + w["unit"]]
        rest = []
        if w.get("width"):
            opt("--width", w["width"])
        if w.get("gain") is not None:
            opt("--gain", jsnum(w["gain"]))
        if chains.get(w["receiver"]):
            opt("--chain", chains[w["receiver"]].replace('"', "'"), quote=True)
    for k, flag in WATCH_FLAGS:
        if w.get(k) is not None:
            opt(flag, jsnum(w[k]))
    argv = list(head) + list(rest)
    for flag, value, _ in toks:
        argv += [flag] + ([value] if value is not None else [])
    shown, i = [], 0
    while i < len(argv):                         # display: quote the values that carry spaces, as the Builder does
        a = argv[i]
        if a.startswith("--") and i + 1 < len(argv) and not argv[i + 1].startswith("--") and \
                a in ("--chain", "--conditions", "--recorder-chain"):
            shown.append(f"{a} {q(argv[i + 1])}")
            i += 2
            continue
        shown.append(a)
        i += 1
    return {"argv": argv, "line": "marsad " + " ".join(shown)}


def watch_plan(station: Station, form: dict, detect: bool = False) -> dict:
    """POST /watch/plan: the command, the survey's plan (survey mode), and the checks for
    every radio involved, each chain from its own path."""
    w = watch_settings(station, form.get("watch") or {})
    radios = [w["surveyor"], w["recorder"]] if w["mode"] == "survey" else [w["receiver"]]
    chains = {r: (form.get("chains") or {}).get(r) or chain_for(station, r)["text"] for r in radios}
    use = radio_in_use(station)
    checks = {"in_use": use, "radios": {}}
    for r in radios:
        checks["radios"][r] = {"chain": chain_for(station, r),
                               "radio": ({"found": None, "rx": r, "text": "Not checked yet."} if not detect or use["blocked"]
                                         else find_radio(station, r))}
        if r in disabled_radios(station):
            checks["radios"][r]["radio"] = scan_checks(station, r, detect=False)["radio"]
    cmd = watch_command(station, w, chains)
    out = {"settings": w, "line": cmd["line"], "argv": cmd["argv"], "checks": checks, "chains": chains}
    if w["mode"] == "survey":
        sp = scan_plan(station, {"scan": w["scan"], "chain": chains[w["surveyor"]]})
        out["survey"] = {k: sp[k] for k in ("fingerprint", "warnings", "cells", "geometry")}
    return out


def watch_start(station: Station, form: dict) -> dict:
    """POST /watch/start: dry run (the engine's own `--dry-run`, which opens no radio), or the
    checks again for both radios and a launch outside every process tree."""
    dry = bool(form.get("dry_run"))
    plan = watch_plan(station, form, detect=not dry)
    if dry:
        return {"dry_run": True, "line": plan["line"], "argv": plan["argv"], **_dry_run(station, plan["argv"]),
                "checks": plan["checks"]}
    ch = plan["checks"]
    if ch["in_use"]["blocked"]:
        raise ValueError(ch["in_use"]["text"] + " Nothing was started.")
    for r, c in ch["radios"].items():
        if not c["radio"].get("found"):
            raise ValueError(c["radio"]["text"] + " Nothing was started.")
        if not plan["chains"].get(r):
            raise ValueError(f"No chain for the {r.upper()}. Nothing was started.")
        if c["chain"]["due"] and not (form.get("chains") or {}).get(r) and not form.get("chain_confirmed"):
            raise ValueError(f"Is the rig still built as drawn ({r.upper()})? Confirm the chains first. Nothing was started.")
    w = plan["settings"]
    if w["mode"] == "survey":
        _remember_conditions(station, w["scan"])
        if any(x[0] == "bad" for x in plan["survey"]["warnings"]) and not form.get("problems_confirmed"):
            raise ValueError("The survey has problems to confirm first. Nothing was started.")
    got = launch_outside(station, marsad_argv(station, plan["argv"]), f"watch_{w['mode']}")
    return {"started": True, "line": plan["line"], "argv": plan["argv"], **got, "checks": ch}


# ---------------------------------------------------------------- the radios (owner, 2026-11-07)
#
# Which radios are plugged in, what each is doing now, which surveys and which records, and what
# each can and cannot do. Presence comes from Windows' own list of USB devices, which names each
# dongle by its serial without opening it, so it is safe to ask every few seconds even while a
# radio is recording. Nothing here opens a radio; the start path's rtl_eeprom check still decides
# the USB index at start time (rule 8).

RTL_USB_IDS = ("VID_0BDA&PID_2838", "VID_0BDA&PID_2832")


def radio_word(name) -> str:
    """How a radio is named in words: an all-letter name in capitals ("abc" -> "ABC"), any other as written."""
    name = str(name or "")
    return name.upper() if name.isalpha() else name
_USB_CACHE: list = [0.0, None]


def usb_radios() -> dict | None:
    """{serial: device name} for every RTL-SDR plugged in now, from Windows' device list
    (SetupAPI, the list Device Manager shows). Opens nothing. None where it cannot be told
    (not Windows, or the call failed). Cached for 2 s."""
    if time.time() - _USB_CACHE[0] < 2.0:
        return _USB_CACHE[1]
    out = None
    if host.IS_WINDOWS:
        try:
            out = _setupapi_rtl()
        except (OSError, AttributeError, ValueError):
            out = None
    _USB_CACHE[:] = [time.time(), out]
    return out


def _setupapi_rtl() -> dict:
    import ctypes
    from ctypes import wintypes

    class DEVINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("ClassGuid", ctypes.c_byte * 16),
                    ("DevInst", wintypes.DWORD), ("Reserved", ctypes.c_void_p)]
    sa = ctypes.WinDLL("setupapi", use_last_error=True)
    sa.SetupDiGetClassDevsW.restype = ctypes.c_void_p
    sa.SetupDiGetClassDevsW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD]
    sa.SetupDiEnumDeviceInfo.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DEVINFO)]
    sa.SetupDiGetDeviceInstanceIdW.argtypes = [ctypes.c_void_p, ctypes.POINTER(DEVINFO), wintypes.LPWSTR,
                                               wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    sa.SetupDiGetDeviceRegistryPropertyW.argtypes = [ctypes.c_void_p, ctypes.POINTER(DEVINFO), wintypes.DWORD,
                                                     ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
                                                     wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    sa.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
    h = sa.SetupDiGetClassDevsW(None, "USB", None, 0x2 | 0x4)       # DIGCF_PRESENT | DIGCF_ALLCLASSES
    if not h or h == ctypes.c_void_p(-1).value:
        raise OSError("SetupDiGetClassDevs failed")
    found = {}
    try:
        i = 0
        info = DEVINFO()
        info.cbSize = ctypes.sizeof(DEVINFO)
        buf = ctypes.create_unicode_buffer(512)
        while sa.SetupDiEnumDeviceInfo(h, i, ctypes.byref(info)):
            i += 1
            if not sa.SetupDiGetDeviceInstanceIdW(h, ctypes.byref(info), buf, 512, None):
                continue
            iid = buf.value.upper()
            if not any(k in iid for k in RTL_USB_IDS):
                continue
            serial = buf.value.split("\\")[-1]
            name = ""
            for prop in (0xC, 0x0):                                 # SPDRP_FRIENDLYNAME, SPDRP_DEVICEDESC
                nb = ctypes.create_unicode_buffer(256)
                if sa.SetupDiGetDeviceRegistryPropertyW(h, ctypes.byref(info), prop, None, nb, 512, None):
                    name = nb.value
                    break
            found[serial] = name
    finally:
        sa.SetupDiDestroyDeviceInfoList(h)
    return found


def radio_roles(station: Station) -> dict:
    """Which radio surveys and which records: the window's own choice (saved_scans.json "roles"),
    else station.json's recorder.roles, else the first and second receivers (watch.survey_roles).
    Every recorder command states both with --surveyor / --recorder, so this changes nothing in
    the engine's own defaults."""
    from . import capture as cap, watch
    saved = (saved_doc(station).get("roles") or {})
    names = list(station.receivers)
    sur, rec = saved.get("surveyor"), saved.get("recorder")
    if sur not in names or rec not in names or sur == rec:
        try:
            sur, rec = watch.survey_roles(station, cap.settings(station), None, None)
        except SystemExit:
            sur, rec = (names + [None, None])[:2]
    return {"surveyor": sur, "recorder": rec, "chosen_here": bool(saved)}


def set_radio_roles(station: Station, form: dict) -> dict:
    """POST /radios/roles: {"surveyor", "recorder"} or {"swap": true}."""
    doc = saved_doc(station)
    now = radio_roles(station)
    if form.get("swap"):
        sur, rec = now["recorder"], now["surveyor"]
    else:
        sur, rec = form.get("surveyor"), form.get("recorder")
    if sur not in station.receivers or rec not in station.receivers:
        raise ValueError("both roles need a receiver from station.json")
    if sur == rec:
        raise ValueError("one radio cannot survey and record at once; pick two different radios")
    doc["roles"] = {"surveyor": sur, "recorder": rec}
    _saved_write(station, doc)
    return radios_status(station)


def disabled_radios(station: Station) -> set:
    """Radios switched off in the window (saved_scans.json "disabled"). The engine never reads this:
    it only stops the window offering or starting new work on them (relay 011)."""
    try:
        return set(saved_doc(station).get("disabled") or []) & set(station.receivers)
    except ValueError:
        return set()


def set_radio_enabled(station: Station, form: dict) -> dict:
    """POST /radios/enable {"name", "enabled"}. Disabling stops nothing already running on that
    radio (rule 1); it only blocks new starts. The radio keeps its role."""
    name = form.get("name")
    if name not in station.receivers:
        raise ValueError(f"no receiver {name!r} in station.json")
    doc = saved_doc(station)
    off = [x for x in doc.get("disabled") or [] if x != name]
    if not form.get("enabled", True):
        off.append(name)
    doc["disabled"] = off
    _saved_write(station, doc)
    return radios_status(station)


def _intervals_minus(lo: float, hi: float, gaps) -> list:
    out, cur = [], lo
    for a, b in sorted((float(a), float(b)) for a, b in gaps or []):
        if b <= cur or a >= hi:
            continue
        if a > cur:
            out.append([cur, a])
        cur = max(cur, b)
    if cur < hi:
        out.append([cur, hi])
    return out


def _intersect(xs: list, ys: list) -> list:
    out = []
    for a, b in xs:
        for c, d in ys:
            lo, hi = max(a, c), min(b, d)
            if hi > lo:
                out.append([lo, hi])
    return out


def coverage_of(station: Station, rx: str, lo: float, hi: float) -> float | None:
    """Share of [lo, hi] MHz this receiver can tune (its range less its PLL gaps)."""
    r = station.receivers.get(rx) or {}
    rng = r.get("range_mhz")
    if not rng or hi <= lo:
        return None
    ok = _intersect(_intervals_minus(float(rng[0]), float(rng[1]), r.get("gaps_mhz")), [[lo, hi]])
    return sum(b - a for a, b in ok) / (hi - lo)


def _radio_notes(station: Station, rx: str, tuner: str) -> list:
    """station.json's builder_notes that belong to this radio (keyed on it or its tuner), each with
    where it applies, in words."""
    out = []
    for n in station.data.get("builder_notes", []):
        w = n.get("when") or {}
        if w.get("rx") != rx and w.get("tuner") != tuner:
            continue
        where = []
        if w.get("span"):
            where.append(f"{jsnum(w['span'][0])}–{jsnum(w['span'][1])} MHz")
        if "includes" in w:
            where.append(f"scans including {jsnum(w['includes'])} MHz")
        if "hi_above" in w:
            where.append(f"above {jsnum(w['hi_above'])} MHz")
        if "engine" in w:
            where.append("rtl_power_fftw" if w["engine"] else "rtl_power")
        if "gain_above" in w:
            where.append(f"gain over {jsnum(w['gain_above'])}")
        out.append({"level": n.get("level") or "warn", "title": n.get("title") or "Note",
                    "text": n.get("text") or "", "where": ", ".join(where)})
    return out


def radios_status(station: Station) -> dict:
    """GET /radios: each configured radio, whether it is plugged in (by serial, nothing opened),
    what it is doing now, its role, and what it can do; plus what the station can do now, in
    words. Read-only and cheap: safe to ask every few seconds."""
    from . import capture as cap, catalogue
    cfg = cap.settings(station)
    usb = usb_radios()
    programs = host.running_sdr_programs()
    roles = radio_roles(station)
    off = disabled_radios(station)
    active = {}
    if station.surveys.exists():
        for f in list(station.surveys.glob("*.csv")) + list(station.surveys.glob("*.dat")):
            if recording_active(f):
                try:
                    d = json.loads(f.with_suffix(".json").read_text())
                except (OSError, ValueError):
                    continue
                active.setdefault(d.get("receiver"), f.stem)
    recorders = {}
    try:
        for r in catalogue.live_recorders(station, cfg):
            if r.get("running"):
                recorders[str(r.get("serial") or "")] = r
                recorders.setdefault("@" + str(r.get("receiver") or ""), r)
    except Exception:  # noqa: BLE001 - a broken status file must not hide the radios
        pass
    radios = []
    spans = {}
    for name, r in station.receivers.items():
        tuner = (r.get("tuner") or "").lower()
        serial = cap.serial_of(cfg, name)
        rng = r.get("range_mhz") or [24, 1766]
        tunable = _intervals_minus(float(rng[0]), float(rng[1]), r.get("gaps_mhz"))
        spans[name] = tunable
        if usb is None:
            present = None
        elif serial:
            present = serial in usb
        else:
            present = None                           # no serial in station.json: cannot tell which dongle
        rec = recorders.get(serial or "") or recorders.get("@" + name)
        if rec:
            job = rec.get("job") or {}
            doing = {"state": "recording", "text": f"recording ({rec.get('mode') or 'watch'})"
                     + (f", on {job['target_mhz']:.4f} MHz" if isinstance(job.get("target_mhz"), (int, float)) else "")}
        elif active.get(name):
            doing = {"state": "surveying", "text": f"surveying: {active[name]}"}
        elif present is False:
            doing = {"state": "absent", "text": "not plugged in"}
        elif programs:
            doing = {"state": "blocked", "text": f"{', '.join(programs)} is running: a scan cannot start until it ends"}
        elif present:
            doing = {"state": "ready", "text": "plugged in and idle"}
        else:
            doing = {"state": "unknown", "text": "presence not known (no serial in station.json, or Windows would not say)"}
        role = "survey" if name == roles["surveyor"] else ("record" if name == roles["recorder"] else None)
        enabled = name not in off
        if not enabled and doing["state"] in ("recording", "surveying"):
            doing = dict(doing, text=doing["text"] + " (disabled: this keeps going; only new starts are blocked)")
        elif not enabled:
            doing = {"state": "disabled", "text": "disabled in this window: not offered for scans or recording"
                     + ("" if present is not False else " (also not plugged in)")}
        facts = [f"{jsnum(rng[0])}–{jsnum(rng[1])} MHz"]
        for a, b in r.get("gaps_mhz") or []:
            facts.append(f"blind {jsnum(a)}–{jsnum(b)} MHz (PLL gap)")
        if float(rng[1]) > RTL_POWER_MAX_MHZ:
            facts.append(f"above {RTL_POWER_MAX_MHZ:.2f} MHz only with rtl_power_fftw")
        facts.append({"e4000": "zero-IF: a DC spike at every hop centre",
                      "r820t": "low-IF: a small bump at the tuning frequency",
                      "r828d": "low-IF: a small bump at the tuning frequency"}.get(tuner, f"tuner {tuner or '?'}"))
        gains = TUNER_GAINS.get(tuner) or []
        radios.append({
            "name": name, "label": r.get("label", name), "tuner": tuner, "serial": serial,
            "usb_name": (usb or {}).get(serial or "", ""), "present": present, "enabled": enabled,
            "doing": doing, "role": role,
            "range_mhz": rng, "gaps_mhz": r.get("gaps_mhz") or [], "tunable_mhz": tunable,
            "gains": gains, "gain_text": (f"{len(gains)} steps, {jsnum(gains[0])}–{jsnum(gains[-1])} dB" if gains else ""),
            "facts": facts, "notes": _radio_notes(station, name, tuner),
            "chain": chain_for(station, name),
        })
    names = [r["name"] for r in radios]
    both = _intersect(spans[names[0]], spans[names[1]]) if len(names) >= 2 else []
    here = [r for r in radios if r["present"] and r["enabled"]]
    sur, rec = roles["surveyor"], roles["recorder"]
    off_words = " ".join(f"The {radio_word(r['name'])} is disabled in this window, so it is not offered."
                         for r in radios if not r["enabled"])
    if len(here) >= 2:
        mode = {"radios": 2, "survey_record": True,
                "text": f"Two radios plugged in. A scan uses one of them. Survey + record is possible: "
                        f"the {radio_word(sur)} surveys and the {radio_word(rec)} records what it finds."}
    elif len(here) == 1:
        one = radio_word(here[0]["name"])
        mode = {"radios": 1, "survey_record": False,
                "text": f"Only the {one} is plugged in. A scan with it surveys the band and writes its spectrum file, but "
                        f"no IQ is captured. Fixed-frequency recording works with the {one} alone. Survey + record needs "
                        "the second radio plugged in" + (" and enabled." if off else ".")}
    else:
        mode = {"radios": 0, "survey_record": False,
                "text": ("No radio is plugged in and enabled. Nothing can start until one is."
                         if off else "No radio is plugged in (as far as Windows can tell). Nothing can start until one is.")
                        if usb is not None else "Windows would not say which radios are plugged in."}
    if off_words:
        mode["text"] = off_words + " " + mode["text"]
    return {"radios": radios, "roles": roles, "both_cover_mhz": both, "mode": mode,
            "programs": programs, "presence_source": "Windows device list, by serial (nothing opened)"
                                                    if usb is not None else None, "checked": time.time()}


SCAN_POST_ROUTES = {
    "/scan/plan": scan_plan,
    "/scan/check": lambda st, f: scan_checks(st, str(f.get("rx") or st.default_receiver or ""),
                                             detect=bool(f.get("detect", True))),
    "/scan/start": scan_start,
    "/scan/saved": saved_change,
    "/watch/plan": lambda st, f: watch_plan(st, f, detect=bool(f.get("detect"))),
    "/watch/start": watch_start,
    "/radios/roles": set_radio_roles,
    "/radios/enable": set_radio_enabled,
}


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
                    rig = form.get("rig")      # the desktop window's split rigs only
                    chain.use(station, str(form.get("name") or ""),
                              rig=[str(n) for n in rig] if isinstance(rig, list) else None)
                    out = chain.summary(station)
                elif u.path == "/chain/layout/save":
                    out = chain_layout_save(station, form)
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
                        ids = form.get("ids")
                        opts = dict(scope=str(form.get("scope") or "event"),
                                    include_exports=bool(form.get("exports")),
                                    keep_fingerprints=form.get("keep_fp", True) is not False)
                        if isinstance(ids, list) and u.path != "/captures/restore":
                            # a selection: one zip per signal group, or one for all of it
                            many = dict(per_group=form.get("per_group", True) is not False,
                                        include_exports=opts["include_exports"],
                                        keep_fingerprints=opts["keep_fingerprints"])
                            if u.path == "/captures/archive/plan":
                                out = ca.plan_many(station, ids, **many)
                            else:
                                out = ca.archive_many(station, ids, note=str(form.get("note") or ""),
                                                      label=str(form.get("label") or ""),
                                                      confirm=form.get("confirm") is True, **many)
                        elif u.path == "/captures/archive/plan":
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
                elif u.path in ("/backup/plan", "/backup/export", "/backup/verify", "/backup/list"):
                    from . import backup
                    if u.path == "/backup/plan":
                        out = backup.plan(station, form.get("dest"), form.get("groups"),
                                          form.get("include_location") is True)
                    elif u.path == "/backup/export":
                        out = backup.start_export(station, form)
                    elif u.path == "/backup/verify":
                        out = backup.start_verify(form)
                    else:
                        out = backup.listing(form.get("file"))
                elif u.path in SCAN_POST_ROUTES:
                    # Relay 010: the window's Start panels. The engine's own refusals
                    # (SystemExit with a sentence) come back as that sentence.
                    try:
                        out = SCAN_POST_ROUTES[u.path](station, form)
                    except SystemExit as exc:
                        raise ValueError(str(exc).strip() or "refused")
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
            if u.path == "/ping":
                # Touches no file. Reading a long recording for the first time takes minutes
                # and starves every request that does (each file call waits for the
                # interpreter), so this is what says the app is up while it loads.
                t = live["tail"]
                busy = t is not None and not t.loaded
                body = json.dumps({"app": "marsad", "follow": follow,
                                   "loading": t.path.name if busy else None}).encode()
            elif u.path == "/data":
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
            elif u.path == "/live/progress":
                # The desktop window's light question (relay 009): no rows, asked often.
                name = (q.get("file") or [None])[0]
                cur = live["tail"]
                live_name = live["path"].name if live["path"] else None
                t = cur if (not name or name == live_name) else TAILERS.get(name)
                if t is None:
                    body = json.dumps({"file": None, "live": live_name, "total": 0, "now": time.time()}).encode()
                else:
                    body = json.dumps(progress_payload(station, t, live_name)).encode()
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
            elif u.path == "/backup/status":
                from . import backup
                body = json.dumps(dict(backup.JOB)).encode()
            elif u.path == "/config":
                body = json.dumps(cfg).encode()
            elif u.path == "/radios":
                try:
                    body = json.dumps(radios_status(station)).encode()
                except (ValueError, SystemExit) as exc:
                    body = json.dumps({"error": str(exc)}).encode()
            elif u.path == "/explain/defaults":
                try:
                    body = json.dumps(default_sources(station, (q.get("rx") or [None])[0])).encode()
                except (ValueError, SystemExit) as exc:
                    body = json.dumps({"error": str(exc)}).encode()
            elif u.path == "/scan/saved":
                try:
                    body = json.dumps(saved_list(station)).encode()
                except (ValueError, SystemExit) as exc:
                    body = json.dumps({"error": str(exc)}).encode()
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
                                       "parts": chain.part_types(station)}).encode()
            elif u.path == "/chain/layout":
                try:
                    body = json.dumps(chain_layout_summary(station)).encode()
                except ValueError as exc:
                    body = json.dumps({"error": str(exc)}).encode()
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
                                                    num("dur", None),
                                                    None if one("bw_khz") is None else num("bw_khz") * 1e3)
                        self.send_response(200)
                        self.send_header("Content-Type", "audio/wav")
                        self.send_header("X-Marsad-Demod", plan["demod"])
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
                        out = catalogue.summary(station, one("date"), num("lo"), num("hi"), one("badge"), one("grade"),
                                                sort=one("sort") or "time", min_mb=num("min_mb"),
                                                **{k: one(k) for k in catalogue.FILTERS})
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
