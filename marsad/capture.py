"""The recorder's engine: one radio parked on one frequency, events catalogued, IQ cut and kept.

RECORDER_DESIGN.md phases 2 and 3. `marsad watch` (watch.py) decides WHERE to listen;
this module does the listening:

- `Session` reads a raw rtl_sdr stream (uint8 IQ) in ~20 ms blocks, keeps a power
  spectrogram of the watched band at 20 ms x ~1 kHz, and opens an EVENT whenever
  the band rises `threshold_db` over its own floor. An event closes after `hold_s`
  of quiet. Every event becomes a catalogue row and a fingerprint file.
- For an OPEN band it can also write IQ: the stream from one second before the
  event onward, shifted so the signal sits at 0 Hz, low-pass filtered and
  decimated to the recipe's rate, as SigMF (.sigmf-data complex int16 +
  .sigmf-meta). The characteriser then runs on it and its recipe is stored with
  the row, so the next capture of that frequency is set from it.
- For a PRIVATE band (cellular, pagers, cordless/DECT, private land-mobile: the
  band-tier table in station.json) there is no IQ writer at all. The raw stream
  lives in memory only; what reaches disk is the 20 ms power spectrogram, which
  shows shape and timing but cannot be demodulated (pager symbols are 0.16-0.8 ms,
  voice needs phase). The row is flagged `PRIVATE: do not decode`, and
  `ANOMALY: review` when the fingerprint does not look like the band's service.

The catalogue is a `captures` table in occupancy.db beside surveys / signals /
bands. Nothing is ever deleted: when the pool (100 GB) or the day (20 GB) is
full, events are still catalogued and fingerprinted, IQ writing stops, and the
row and the console say why.

Stdlib + numpy/scipy. Never opens a dongle itself: `rtl_sdr_command` builds the
command line and watch.py starts it.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import queue
import sqlite3
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np

from . import archive, characterise
from .config import Station, builtin_defaults
from .rtlpower import TUNER_GAINS

BLOCK_S = 0.02            # spectrogram row: ~20 ms, the fingerprint's time resolution
FP_RES_HZ = 1000.0        # spectrogram bin: ~1 kHz
MIN_GROUP_BINS = 4        # detector sub-bands are at least this many bins, so noise cannot fire it
MAX_GROUPS = 32
CALIBRATE_BLOCKS = 25     # 0.5 s of floor before anything can trigger
FLOOR_ROWS = 250          # quiet rows the floor is the median of (~5 s)
FLOOR_CAP = 2.0           # no bin's floor above 2x (3 dB) the band's typical bin: a steady carrier cannot hide itself
PRE_S = 1.0               # IQ kept from before the trigger

PRIVATE = "PRIVATE: do not decode"
ANOMALY = "ANOMALY: review"


# ---------------------------------------------------------------- settings

def settings(station: Station) -> dict:
    """The `recorder` section: built-in defaults, then whatever station.json sets."""
    cfg = dict(builtin_defaults().get("recorder", {}))
    cfg.update(station.data.get("recorder") or {})
    return cfg


def pool_dir(station: Station, cfg: dict | None = None) -> Path:
    cfg = cfg or settings(station)
    p = Path(cfg.get("pool") or "recordings/captures")
    return p if p.is_absolute() else station.root / p


def band_tier(freq_hz: float, cfg: dict) -> dict:
    """{"tier": open|private, "band": label, "min_bw_khz": ...} for one frequency.

    First matching row wins, so an open exception can sit before the private band
    that contains it. Anything not in the table is open: unidentified signals are
    exactly what the recorder is for.
    """
    f = freq_hz / 1e6
    for row in cfg.get("band_tiers", []):
        if row["lo"] <= f <= row["hi"]:
            return {"tier": row.get("tier", "open"), "band": row.get("band", ""),
                    "min_bw_khz": row.get("min_bw_khz")}
    return {"tier": "open", "band": "", "min_bw_khz": None}


def serial_of(cfg: dict, receiver: str) -> str | None:
    return (cfg.get("serials") or {}).get(receiver)


def ppm_of(cfg: dict, serial: str | None) -> float:
    return float((cfg.get("ppm") or {}).get(serial or "", 0.0))


def corrected(freq_hz: float, ppm: float) -> float:
    """What to ask the radio for so it really tunes to freq_hz.

    ppm is the radio's own error, measured as (frequency it reports - true frequency)
    / true * 1e6: a radio that reads a 385.000 MHz carrier at 385.0002 is +0.52 ppm,
    and has to be asked for less to land on the true frequency.
    """
    return freq_hz / (1.0 + ppm * 1e-6)


def snap_gain(tuner: str | None, gain_db: float) -> float:
    ladder = TUNER_GAINS.get((tuner or "").lower())
    if not ladder:
        return gain_db
    return min(ladder, key=lambda g: abs(g - gain_db))


# ---------------------------------------------------------------- planning a capture

def plan_capture(signal_hz: float, width_hz: float | None, tuner: str | None,
                 recipe: dict | None = None, gain_db: float | None = None) -> dict:
    """Radio rate, tuning, cut rate and watched band for one signal.

    With a recipe from an earlier capture, follow it. Without one, the bandwidth is
    a guess: a known width gets the characteriser's own arithmetic (rate ~2.5x the
    width, signal one side of DC); no width at all means "unknown": the whole
    2.4 MS/s is kept, on the v5 centred (its R860 is low-IF, no DC spike), on a
    zero-IF tuner a quarter of the band above DC.
    """
    zero_if = (tuner or "").lower() == "e4000"
    if recipe and recipe.get("rtl_rate_hz"):
        rtl = float(recipe["rtl_rate_hz"])
        offset = float(recipe.get("signal_above_tune_hz") or 0.0)
        store = float(recipe.get("store_rate_hz") or rtl)
        occ = recipe.get("occupied_hz") or store / 2.5
        width = min(max(1.2 * occ, 10e3), 0.8 * rtl)
        src = "recipe"
    elif width_hz:
        w = max(width_hz, 4e3)
        guard = max(20e3, 0.1 * w)
        need = (w + guard) / 0.4
        rtl = next((r for r in characterise.RTL_RATES if r >= need), 2.4e6)
        offset = (w / 2 + guard) if need <= 2.4e6 else (0.25 * rtl if zero_if else 0.0)
        store = max(2.5 * w, 10e3)
        width = min(1.5 * w + 2 * guard, 0.8 * rtl)
        src = "survey width"
    else:
        rtl = 2.4e6
        offset = 0.25 * rtl if zero_if else 0.0
        store = rtl
        width = 0.4 * rtl if zero_if else 0.8 * rtl
        src = "no recipe yet: full band"
    # Keep the watched band inside the usable 80 % of the radio's band.
    room = 0.4 * rtl - abs(offset)
    width = max(min(width, 2 * room), 4e3)
    decim = max(1, int(rtl // max(store, 1.0)))
    plan = {"signal_hz": signal_hz, "tune_hz": signal_hz - offset, "rtl_rate_hz": rtl,
            "decim": decim, "store_rate_hz": rtl / decim, "width_hz": width,
            "source": src, "duration_s": (recipe or {}).get("duration_s")}
    if gain_db is not None:
        # the recipe's nudge is relative to the gain its capture was made at
        base = (recipe or {}).get("gain_db_used")
        change = ((recipe or {}).get("gain") or {}).get("change_db", 0) or 0
        plan["gain_db"] = snap_gain(tuner, (gain_db if base is None else base) + change)
    return plan


def rtl_sdr_command(exe: str, device: str, plan: dict, ppm: float) -> list:
    """rtl_sdr streaming uint8 IQ to stdout; nothing is written by rtl_sdr itself.

    -d takes the serial number: librtlsdr matches it to whichever USB index the
    dongle has today, so a remembered index can never pick the wrong radio.
    """
    return [exe, "-d", str(device), "-f", str(int(round(corrected(plan["tune_hz"], ppm)))),
            "-s", str(int(plan["rtl_rate_hz"])), "-g", f"{plan['gain_db']:g}", "-"]


# ---------------------------------------------------------------- the catalogue

SCHEMA = """
CREATE TABLE IF NOT EXISTS captures (
  id              INTEGER PRIMARY KEY,
  started         TEXT,
  started_epoch   REAL,
  duration_s      REAL,
  freq_hz         REAL,
  tuned_hz        REAL,
  receiver        TEXT,
  serial          TEXT,
  mode            TEXT,
  trigger         TEXT,
  survey          TEXT,
  tier            TEXT,
  band            TEXT,
  flags           TEXT,
  snr_peak_db     REAL,
  snr_median_db   REAL,
  bandwidth_hz    REAL,
  peak_hz         REAL,
  duty            REAL,
  repeat_s        REAL,
  captured        INTEGER,
  iq_status       TEXT,
  iq_path         TEXT,
  iq_bytes        INTEGER,
  fingerprint     TEXT,
  fp_bytes        INTEGER,
  rtl_rate_hz     REAL,
  store_rate_hz   REAL,
  gain_db         REAL,
  ppm             REAL,
  chain           TEXT,
  family          TEXT,
  characterised   TEXT,
  recipe          TEXT,
  notes           TEXT
);
CREATE INDEX IF NOT EXISTS ix_cap_freq ON captures(freq_hz);
CREATE INDEX IF NOT EXISTS ix_cap_time ON captures(started_epoch);
"""
COLUMNS = ("started", "started_epoch", "duration_s", "freq_hz", "tuned_hz", "receiver", "serial",
           "mode", "trigger", "survey", "tier", "band", "flags", "snr_peak_db", "snr_median_db",
           "bandwidth_hz", "peak_hz", "duty", "repeat_s", "captured", "iq_status", "iq_path",
           "iq_bytes", "fingerprint", "fp_bytes", "rtl_rate_hz", "store_rate_hz", "gain_db",
           "ppm", "chain", "family", "characterised", "recipe", "notes")


def connect(station: Station) -> sqlite3.Connection:
    """occupancy.db with the captures table. One connection may be shared by the watcher
    and the recorder thread (sqlite serialises them), hence check_same_thread=False."""
    con = sqlite3.connect(station.db, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.executescript(archive.SCHEMA)
    con.executescript(SCHEMA)
    return con


def add_row(con: sqlite3.Connection, row: dict) -> int:
    keys = [k for k in COLUMNS if k in row]
    vals = [json.dumps(row[k]) if isinstance(row[k], (dict, list)) else row[k] for k in keys]
    cur = con.execute(f"INSERT INTO captures ({', '.join(keys)}) VALUES ({', '.join('?' * len(keys))})", vals)
    con.commit()
    return int(cur.lastrowid)


def update_row(con: sqlite3.Connection, row_id: int, **fields) -> None:
    keys = [k for k in fields if k in COLUMNS]
    if not keys:
        return
    vals = [json.dumps(fields[k]) if isinstance(fields[k], (dict, list)) else fields[k] for k in keys]
    con.execute(f"UPDATE captures SET {', '.join(k + ' = ?' for k in keys)} WHERE id = ?", [*vals, row_id])
    con.commit()


def near(con, freq_hz: float, tol_hz: float, where: str = "1") -> list:
    return con.execute(f"SELECT * FROM captures WHERE ABS(freq_hz - ?) <= ? AND ({where}) "
                       "ORDER BY started_epoch DESC", (freq_hz, tol_hz)).fetchall()


def recipe_for(con, freq_hz: float, tol_hz: float = 20e3) -> dict | None:
    """The newest recipe the characteriser wrote for this frequency."""
    for r in near(con, freq_hz, tol_hz, "recipe IS NOT NULL"):
        try:
            return json.loads(r["recipe"])
        except ValueError:
            continue
    return None


def last_captured(con, freq_hz: float, tol_hz: float) -> float | None:
    """When the recorder last spent a capture on this frequency (epoch), IQ or fingerprint."""
    rows = near(con, freq_hz, tol_hz, "captured = 1")
    return rows[0]["started_epoch"] if rows else None


def bytes_since(con, epoch: float) -> int:
    r = con.execute("SELECT COALESCE(SUM(COALESCE(iq_bytes, 0) + COALESCE(fp_bytes, 0)), 0) "
                    "FROM captures WHERE started_epoch >= ?", (epoch,)).fetchone()
    return int(r[0])


def local_midnight(epoch: float) -> float:
    d = dt.datetime.fromtimestamp(epoch)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def folder_bytes(path: Path) -> int:
    total = 0
    if path.exists():
        for root, _, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    return total


class Budget:
    """The pool and the day, in bytes. Never deletes: when full it says no, and why."""

    def __init__(self, station: Station, con, cfg: dict):
        self.pool = pool_dir(station, cfg)
        self.pool_cap = float(cfg.get("pool_gb", 100)) * 1e9
        self.day_cap = float(cfg.get("daily_gb", 20)) * 1e9
        self.used = folder_bytes(self.pool)
        self.con = con
        self.warned = set()

    def add(self, n: int) -> None:
        self.used += int(n)

    def iq_refusal(self, expect_bytes: float = 0.0) -> str | None:
        """None when IQ may be written; otherwise the reason it may not."""
        if self.used + expect_bytes > self.pool_cap:
            return "pool full"
        today = bytes_since(self.con, local_midnight(time.time()))
        if today + expect_bytes > self.day_cap:
            return "daily cap"
        return None

    def say_once(self, reason: str) -> None:
        if reason in self.warned:
            return
        self.warned.add(reason)
        what = (f"the recorder pool ({self.pool}) holds {self.used / 1e9:.1f} of {self.pool_cap / 1e9:.0f} GB"
                if reason == "pool full" else f"today's IQ has reached the {self.day_cap / 1e9:.0f} GB daily cap")
        print(f"\n  *** IQ writing stopped: {what}.\n"
              "      Events are still catalogued and fingerprinted. Nothing is deleted;\n"
              "      free space by hand (or raise pool_gb / daily_gb in station.json).")


# ---------------------------------------------------------------- files

def stamp(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch).strftime("%Y%m%d-%H%M%S")


def stem_for(freq_hz: float, epoch: float, receiver: str) -> str:
    return f"cap_{freq_hz / 1e6:.4f}MHz_{stamp(epoch)}_{receiver}"


def utc_iso(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class Cutter:
    """Streamed shift + low-pass + decimate, continuous across blocks, to complex int16.

    Decimation is by a whole number so the filter state carries over exactly; the
    cut rate is therefore the radio rate / D, never below what the plan asked for.
    """

    def __init__(self, rate: float, shift_hz: float, decim: int):
        from scipy.signal import firwin
        self.rate, self.decim = rate, int(decim)
        self.shift = shift_hz if decim > 1 else 0.0
        self.phase = 0.0
        self.taps = None
        if self.decim > 1:
            n = min(1023, 16 * self.decim + 1)
            self.taps = firwin(n, 0.4 / self.decim).astype(np.float32)   # pass +/-0.4 x the cut rate
            self.hist = np.zeros(n - 1, np.complex64)
        self.skip = 0          # samples to drop before the next kept one

    def __call__(self, x: np.ndarray) -> np.ndarray:
        if self.shift:
            n = np.arange(len(x))
            w = -2 * np.pi * self.shift / self.rate
            x = (x * np.exp(1j * (self.phase + w * n))).astype(np.complex64)
            self.phase = float((self.phase + w * len(x)) % (2 * np.pi))
        if self.decim > 1:
            from scipy.signal import fftconvolve
            buf = np.concatenate([self.hist, x])
            y = fftconvolve(buf, self.taps, mode="valid").astype(np.complex64)
            self.hist = buf[-(len(self.taps) - 1):]
            y = y[self.skip::self.decim]
            self.skip = (self.skip - len(x)) % self.decim
            x = y
        iq = np.empty(2 * len(x), np.int16)
        iq[0::2] = np.clip(np.round(x.real * 32768), -32768, 32767)
        iq[1::2] = np.clip(np.round(x.imag * 32768), -32768, 32767)
        return iq


class IQWriter:
    """One open-band capture on disk: .sigmf-data grows block by block, .sigmf-meta at the end."""

    def __init__(self, folder: Path, stem: str, plan: dict, meta: dict, start_epoch: float):
        folder.mkdir(parents=True, exist_ok=True)
        self.data = folder / f"{stem}.sigmf-data"
        self.meta_path = folder / f"{stem}.sigmf-meta"
        self.plan, self.meta, self.start_epoch = plan, meta, start_epoch
        shift = plan["signal_hz"] - plan["tune_hz"]
        self.cut = Cutter(plan["rtl_rate_hz"], shift, plan["decim"])
        self.centre = plan["signal_hz"] if plan["decim"] > 1 else plan["tune_hz"]
        self.fh = open(self.data, "wb")
        self.bytes = 0
        self.raw_samples = 0
        self.annotations = []

    def write(self, x: np.ndarray) -> None:
        iq = self.cut(x)
        self.fh.write(iq.tobytes())
        self.bytes += iq.nbytes
        self.raw_samples += len(x)

    def annotate(self, t0_s: float, dur_s: float, label: str) -> None:
        fs = self.plan["store_rate_hz"]
        self.annotations.append({"core:sample_start": max(0, int(t0_s * fs)),
                                 "core:sample_count": max(1, int(dur_s * fs)), "core:label": label})

    def close(self) -> Path:
        self.fh.close()
        g = {"core:datatype": "ci16_le", "core:sample_rate": self.plan["store_rate_hz"],
             "core:version": "1.0.0", "core:recorder": "marsad watch",
             "core:description": self.meta.get("description", ""),
             "core:hw": self.meta.get("hw", "")}
        g.update({f"marsad:{k}": v for k, v in self.meta.items() if k not in ("description", "hw")})
        g.update({"marsad:tuned_hz": self.plan["tune_hz"], "marsad:radio_rate_hz": self.plan["rtl_rate_hz"],
                  "marsad:decimation": self.plan["decim"], "marsad:gain_db": self.plan.get("gain_db")})
        m = {"global": g,
             "captures": [{"core:sample_start": 0, "core:frequency": self.centre,
                           "core:datetime": utc_iso(self.start_epoch)}],
             "annotations": self.annotations}
        self.meta_path.write_text(json.dumps(m, indent=2), encoding="utf-8")
        self.bytes += self.meta_path.stat().st_size
        return self.meta_path


# ---------------------------------------------------------------- reading the stream

def cu8_to_complex(b: bytes) -> np.ndarray:
    r = np.frombuffer(b, np.uint8).astype(np.float32)
    return ((r - 127.5) / 127.5).view(np.complex64)


class StreamReader:
    """A thread that keeps rtl_sdr's pipe drained into memory.

    Processing is bursty (a capture closing, the characteriser starting); if the pipe
    ever filled, rtl_sdr would block in its USB callback and librtlsdr would drop
    samples. The queue holds up to ~20 s of raw stream. Memory only, never a file.
    """

    def __init__(self, stream, block_bytes: int, max_blocks: int = 1000):
        self.stream, self.block_bytes = stream, int(block_bytes)
        self.q: queue.Queue = queue.Queue(max_blocks)
        self.eof = False
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def _run(self):
        buf = bytearray()
        try:
            while True:
                chunk = self.stream.read(self.block_bytes - len(buf))
                if not chunk:
                    break
                buf += chunk
                if len(buf) >= self.block_bytes:
                    self.q.put(bytes(buf))
                    buf = bytearray()
        except (OSError, ValueError):
            pass
        self.eof = True
        self.q.put(None)

    def get(self, timeout: float = 5.0):
        try:
            return self.q.get(timeout=timeout)
        except queue.Empty:
            return b""          # nothing yet: not the end


# ---------------------------------------------------------------- the session

def fft_size(rate: float) -> int:
    return int(2 ** round(math.log2(max(rate / FP_RES_HZ, 16))))


class Session:
    """Listen to one tuning for a while; find events; write IQ where allowed.

    mode "watch": events open when the band rises threshold_db over its floor, and
      an IQ capture (open bands, when `allow_iq` says yes) starts PRE_S before the
      first event and runs capture_s, covering whatever repeats inside it.
    mode "now": a survey trigger. The whole session is one event and, open band
      permitting, one capture from the first block.

    Times are counted in samples from the start of the stream, so a replayed file
    gives the same answer every time; `t0_epoch` turns them into clock time.
    """

    def __init__(self, plan: dict, tier: str, mode: str = "watch", duration_s: float = 60.0,
                 capture_s: float = 30.0, threshold_db: float = 12.0, hold_s: float = 1.0,
                 event_max_s: float = 120.0, t0_epoch: float | None = None,
                 on_event=None, allow_iq=None, open_writer=None, on_capture_closed=None,
                 stop_after_capture: bool = False, stop=None, restart=None):
        self.plan, self.tier, self.mode = plan, tier, mode
        self.private = tier == "private"
        self.rate = float(plan["rtl_rate_hz"])
        self.nfft = fft_size(self.rate)
        self.k = max(1, int(round(BLOCK_S * self.rate / self.nfft)))
        self.block = self.nfft * self.k
        self.block_s = self.block / self.rate
        self.duration_s, self.capture_s = duration_s, capture_s
        self.threshold, self.hold = threshold_db, max(1, int(round(hold_s / self.block_s)))
        self.event_max = int(event_max_s / self.block_s)
        self.t0 = time.time() if t0_epoch is None else t0_epoch
        self.on_event, self.allow_iq = on_event, allow_iq
        self.open_writer, self.on_capture_closed = open_writer, on_capture_closed
        self.stop_after_capture = stop_after_capture
        self.stop = stop or threading.Event()
        self.restart = restart or threading.Event()     # leave between captures (a new recipe)
        f = (np.arange(self.nfft) - self.nfft // 2) * self.rate / self.nfft
        sig_off = plan["signal_hz"] - plan["tune_hz"]
        self.wb = np.where(np.abs(f - sig_off) <= plan["width_hz"] / 2)[0]
        if len(self.wb) < MIN_GROUP_BINS:
            c = int(np.argmin(np.abs(f - sig_off)))
            self.wb = np.arange(max(0, c - 2), min(self.nfft, c + 2))
        self.f_rel = f[self.wb] - sig_off          # Hz relative to the signal
        groups = max(1, min(MAX_GROUPS, len(self.wb) // MIN_GROUP_BINS))
        self.edges = np.linspace(0, len(self.wb), groups + 1).astype(int)
        self.win = np.hanning(self.nfft).astype(np.float32)
        self.win /= np.sqrt(np.mean(self.win ** 2))
        self.quiet = deque(maxlen=FLOOR_ROWS)
        self.floor = None
        self.n_blocks = 0
        self.ev = None
        self.events = []
        self.pre = deque(maxlen=max(1, int(round(PRE_S / self.block_s))))
        self.writer = None
        self.writer_left = 0
        self.capture = None          # what the last capture was, for the caller
        self.captures = 0
        self.stat_max = -99.0

    # -- per block
    def _rows(self, x: np.ndarray):
        fr = x.reshape(self.k, self.nfft) * self.win
        P = np.abs(np.fft.fftshift(np.fft.fft(fr, axis=1), axes=1)) ** 2 / self.nfft
        Pw = P[:, self.wb]
        return Pw.mean(axis=0), Pw

    def _update_floor(self, row: np.ndarray) -> None:
        self.quiet.append(row)
        if self.floor is None or self.n_blocks % 10 == 0:
            fl = np.median(np.array(self.quiet), axis=0)
            fl = np.minimum(fl, FLOOR_CAP * np.median(fl))
            self.floor = np.maximum(fl, 1e-12)

    def _stat(self, Pw: np.ndarray) -> float:
        cs = np.cumsum(np.concatenate([np.zeros((Pw.shape[0], 1)), Pw], axis=1), axis=1)
        sub = cs[:, self.edges[1:]] - cs[:, self.edges[:-1]]
        fc = np.concatenate([[0.0], np.cumsum(self.floor)])
        fl = fc[self.edges[1:]] - fc[self.edges[:-1]]
        return float(10 * np.log10(np.max(sub / fl)))

    def _open_event(self, t_s: float):
        self.ev = {"start_s": t_s, "rows": [], "stats": [], "active": 0, "quiet_run": 0,
                   "floor": self.floor.copy()}

    def _close_event(self):
        ev, self.ev = self.ev, None
        n_keep = len(ev["rows"]) - ev["quiet_run"] if self.mode == "watch" else len(ev["rows"])
        n_keep = max(1, n_keep)
        ev["rows"] = np.array(ev["rows"][:n_keep], np.float32)
        ev["stats"] = np.array(ev["stats"][:n_keep])
        ev["duration_s"] = n_keep * self.block_s
        ev["block_s"] = self.block_s
        ev["f_rel_hz"] = self.f_rel
        fingerprint_stats(ev, self.threshold)
        if self.writer is not None:
            self.writer.annotate(ev["start_s"] - self.writer_t0, ev["duration_s"],
                                 f"event, peak {ev['snr_peak_db']:.1f} dB")
            ev["in_capture"] = self.capture
        self.events.append(ev)
        if self.on_event:
            self.on_event(ev, self)

    def _start_capture(self, t_s: float, ev: dict | None):
        if self.private or self.open_writer is None:
            return
        reason = self.allow_iq(t_s) if self.allow_iq else None
        if reason:
            if ev is not None:
                ev["iq_refused"] = reason
            return
        pre = list(self.pre) if self.mode == "watch" else []
        self.writer_t0 = t_s - len(pre) * self.block_s
        self.writer = self.open_writer(self, self.t0 + self.writer_t0)
        for x in pre:
            self.writer.write(x)
        self.writer_left = int(math.ceil(self.capture_s / self.block_s))
        self.capture = {"path": None, "start_s": self.writer_t0, "writer": self.writer}
        if ev is not None:
            ev["capture_started"] = self.capture

    def _close_capture(self):
        w, self.writer = self.writer, None
        if self.ev is not None:            # the capture ends inside an event: note it
            w.annotate(self.ev["start_s"] - self.writer_t0,
                       len(self.ev["rows"]) * self.block_s, "event (continues past the capture)")
        meta = w.close()
        self.capture.update({"path": str(meta), "bytes": w.bytes,
                             "duration_s": w.raw_samples / self.rate})
        self.captures += 1
        if self.on_capture_closed:
            self.on_capture_closed(self.capture, self)

    def feed(self, x: np.ndarray) -> None:
        t_s = self.n_blocks * self.block_s
        row, Pw = self._rows(x)
        calibrating = self.n_blocks < CALIBRATE_BLOCKS
        if self.floor is None or (calibrating and self.mode == "watch"):
            self._update_floor(row)
        stat = self._stat(Pw)
        self.stat_max = max(self.stat_max, stat)
        active = stat >= self.threshold and not (calibrating and self.mode == "watch")
        if self.mode == "now" and self.ev is None and self.n_blocks == 0:
            self._open_event(t_s)
            self._start_capture(t_s, self.ev)
        if self.ev is None and active:
            self._open_event(t_s)
            if self.writer is not None:
                self.ev["in_capture"] = self.capture
            elif self.captures == 0 or not self.stop_after_capture:
                self._start_capture(t_s, self.ev)
        if self.ev is not None:
            self.ev["rows"].append(row)
            self.ev["stats"].append(stat)
            if active:
                self.ev["active"] += 1
                self.ev["quiet_run"] = 0
            else:
                self.ev["quiet_run"] += 1
            if self.mode == "watch" and (self.ev["quiet_run"] >= self.hold or len(self.ev["rows"]) >= self.event_max):
                self._close_event()
        if not active and not calibrating and self.mode == "watch":
            self._update_floor(row)
        elif self.mode == "now" and not calibrating and stat < self.threshold:
            self._update_floor(row)
        if self.writer is not None:
            self.writer.write(x)
            self.writer_left -= 1
            if self.writer_left <= 0:
                self._close_capture()
                if self.stop_after_capture:
                    self.stop.set()
        else:
            self.pre.append(x)
        self.n_blocks += 1

    def run(self, reader) -> dict:
        """Feed blocks until the duration is up, the stream ends, or stop is set."""
        need = int(self.duration_s / self.block_s)
        stalls = 0
        ended = "duration"
        while self.n_blocks < need:
            if self.stop.is_set():
                ended = "stop"
                break
            if self.restart.is_set() and self.writer is None:
                ended = "restart"
                break
            b = reader.get()
            if b is None:
                ended = "eof"
                break
            if not b:
                stalls += 1
                if stalls >= 6:          # 30 s with no samples: the radio has gone
                    ended = "eof"
                    break
                continue
            stalls = 0
            self.feed(cu8_to_complex(b))
        out = self.finish()
        out["ended"] = ended
        return out

    def finish(self) -> dict:
        if self.ev is not None:
            self._close_event()
        if self.writer is not None:
            self._close_capture()
        return {"blocks": self.n_blocks, "seconds": self.n_blocks * self.block_s,
                "events": len(self.events), "captures": self.captures}


# ---------------------------------------------------------------- the fingerprint

def fingerprint_stats(ev: dict, threshold_db: float) -> None:
    """Bandwidth, SNR, duty, peak frequency and burst timing from the 20 ms spectrogram."""
    rows, stats, floor = ev["rows"], ev["stats"], ev["floor"]
    f = ev["f_rel_hz"]
    act = stats >= threshold_db
    use = rows[act] if act.any() else rows
    sig = use.mean(axis=0) - floor
    edges = characterise.obw99(f, np.maximum(sig, 0.0), floor)
    ev["bandwidth_hz"] = (edges[1] - edges[0]) if edges else None
    ev["centre_rel_hz"] = 0.5 * (edges[0] + edges[1]) if edges else 0.0
    i = int(np.argmax(sig))
    pk = float(f[i])
    if 0 < i < len(sig) - 1:                 # parabolic interpolation on the dB peak
        a, b, c = (10 * np.log10(np.maximum(sig[i - 1:i + 2], 1e-12)))
        den = a - 2 * b + c
        if den < 0:
            pk += 0.5 * (a - c) / den * (f[1] - f[0])
    ev["peak_rel_hz"] = pk
    ev["snr_peak_db"] = float(stats.max())
    ev["snr_median_db"] = float(np.median(stats[act])) if act.any() else float(np.median(stats))
    ev["duty"] = float(act.mean())
    # bursts inside the event: runs of active rows
    starts = np.flatnonzero(act & ~np.concatenate([[False], act[:-1]]))
    ev["bursts"] = int(len(starts))
    ev["burst_period_s"] = None
    if len(starts) >= 6:
        d = np.diff(starts) * ev["block_s"]
        if np.median(d) > 0 and np.std(d) / np.median(d) < 0.1:
            ev["burst_period_s"] = float(np.median(d))


def anomaly(ev: dict, band: dict, cfg: dict) -> str | None:
    """Why a private-band fingerprint does not look like the band's service, or None."""
    bw = ev.get("bandwidth_hz")
    cw_hz = float(cfg.get("cw_max_khz", 3.0)) * 1e3
    if bw is not None and bw <= cw_hz and ev.get("duty", 0) >= 0.9 and ev["duration_s"] >= 0.2:
        return f"steady CW-like carrier ({bw / 1e3:.1f} kHz) in a {band.get('band') or 'private'} band"
    if ev.get("burst_period_s"):
        return (f"regular pulse train ({ev['bursts']} bursts every {ev['burst_period_s'] * 1e3:.0f} ms) "
                f"in a {band.get('band') or 'private'} band")
    want = band.get("min_bw_khz")
    if want and bw is not None and bw < 0.2 * want * 1e3:
        return f"{bw / 1e3:.1f} kHz wide where the band's service is {want:g} kHz or more"
    return None


def save_fingerprint(folder: Path, stem: str, ev: dict, t0_epoch: float, signal_hz: float) -> Path:
    """The power spectrogram, 20 ms x ~1 kHz, in dB as float16. Shape, not content."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{stem}.fp.npz"
    db = 10 * np.log10(np.maximum(ev["rows"], 1e-12)).astype(np.float16)
    np.savez_compressed(path, spec_db=db, floor_db=(10 * np.log10(ev["floor"])).astype(np.float16),
                        f_rel_hz=ev["f_rel_hz"].astype(np.float32), dt_s=np.float32(ev["block_s"]),
                        signal_hz=np.float64(signal_hz), start=np.array(utc_iso(t0_epoch)))
    return path
