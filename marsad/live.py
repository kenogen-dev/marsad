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

from . import probe
from .config import Station
from .rtlpower import (CROP, FIR, RTL_MAX_SAMPLE_HZ, RTL_POWER_MAX_MHZ, TUNER_GAINS,
                       parse_freq_spec)

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
        if self.last_lo is not None and lo <= self.last_lo:
            self._finish(parts[0].strip() + " " + parts[1].strip())
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
            self._finish(parts[0].strip() + " " + parts[1].strip())

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
        while len(self.sweeps) > self.keep:
            self.sweeps.pop(0); self.stamps.pop(0); self.medians.pop(0); self.dropped += 1

    def poll(self) -> None:
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


# ---------------------------------------------------------------- config for the builder

def builder_config(station: Station) -> dict:
    """Everything the scan builder page needs, derived from station.json."""
    presets = {}
    for name, p in station.presets.items():
        lo, hi, b = parse_freq_spec(p["freq"])
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
    """Every CSV in surveys/, newest first, with whatever the sidecar knows."""
    out = []
    if not station.surveys.exists():
        return out
    for f in sorted(station.surveys.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True):
        meta = {}
        try:
            d = json.loads(f.with_suffix(".json").read_text())
            rp = d.get("rtl_power", {})
            g = d.get("geometry", {})
            meta = {"receiver": d.get("receiver"), "gain": rp.get("gain_db"),
                    "integ": rp.get("integration_s"), "sweeps": g.get("sweeps"),
                    "lo": rp.get("freq_low_mhz"), "hi": rp.get("freq_high_mhz_effective"),
                    "started": d.get("started_local"), "tag": d.get("preset")}
        except (OSError, ValueError):
            pass
        out.append({"name": f.name, "mb": round(f.stat().st_size / 1e6, 1),
                    "mtime": f.stat().st_mtime, **meta})
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


def get_tailer(station: Station, name: str, cols: int, keep: int):
    """One Tailer per CSV, built the first time it is asked for."""
    with TAIL_LOCK:
        t = TAILERS.get(name)
        if t is None:
            path = station.surveys / name
            if not path.exists():
                return None
            t = Tailer(path, cols, keep)
            TAILERS[name] = t
        return t


def main(argv=None, station: Station | None = None, prog: str = "marsad live") -> int:
    station = station or Station.load()
    ap = argparse.ArgumentParser(prog=prog, description="The station's local web app")
    ap.add_argument("--csv", help="path to the CSV (default: newest in surveys/)")
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
        if not station.surveys.exists():
            return None
        files = sorted(station.surveys.glob("*.csv"), key=lambda p: p.stat().st_mtime)
        return files[-1] if files else None

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
        tail = Tailer(path, args.cols, args.keep)
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
            elif u.path == "/surveys":
                body = json.dumps({"live": live["path"].name if live["path"] else None,
                                   "follow": follow,
                                   "surveys": survey_list(station)}).encode()
            elif u.path == "/config":
                body = json.dumps(cfg).encode()
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
            elif u.path == "/probe/status":
                body = json.dumps(runner.status()).encode()
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
            time.sleep(2.0)

    threading.Thread(target=loop, daemon=True).start()

    url = f"http://127.0.0.1:{args.port}/"
    note = f", exits {args.exit_after_idle:g} min after the scan ends" if args.exit_after_idle else ""
    print(f"  {url}   (Ctrl-C to stop{note})")
    if args.open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        runner.shutdown()      # a probe run started from the page must not outlive the Stop button
    return 0
