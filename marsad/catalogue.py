"""The Captures tab's server side (RECORDER_DESIGN.md phase 4).

Reads the `captures` table in occupancy.db and the files in the recorder pool; never
writes to either, except that an export makes a COPY of an open-band capture in an
`exports` folder beside it. Nothing here deletes anything, and there is no endpoint
that could.

- `summary`  the list: storage strip, events grouped by signal, filters.
- `event`    one event: its row, the SigMF meta, the characterise report, the recipe,
             and which capture holds its IQ (its own, or the one it fell inside).
- `fingerprint_view`  the 20 ms x ~1 kHz spectrogram every event has, private included.
- `iq_view`  waterfall and envelope of an open capture, streamed slice by slice so an
             860 MB file is never in memory at once. Cached per file.
- `export`   WAV for SDR++ (16-bit stereo I/Q, `baseband_<Hz>Hz_...` name: SDR++'s
             file source takes the centre frequency from the first `[0-9]+Hz` in the
             name) or complex float32 `.cf32` with a SigMF meta for Inspectrum / URH /
             GNU Radio. **Refused for anything private**, whatever the page sends.

Images go to the page as uint8 base64 (dB over the floor, -5..45 dB mapped to 0..255),
which the page paints on a canvas. Stdlib + numpy.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import os
import sqlite3
import struct
import threading
import time
from pathlib import Path

import numpy as np

from . import capture as cap
from .config import Station

DB_LO, DB_SPAN = -5.0, 50.0          # image scale: dB over the floor
FP_MAX_ROWS, FP_MAX_BINS = 300, 400
IQ_ROWS, IQ_BINS, IQ_NFFT, ENV_PER_ROW = 300, 512, 1024, 8
EXPORT_CHUNK = 1 << 20               # complex samples per export read
STATUS_FILE = "recorder_status.json"


class Refused(Exception):
    """A request the catalogue will not serve (private band, no such file). Said plainly."""


# ---------------------------------------------------------------- reading the catalogue

def open_ro(station: Station) -> sqlite3.Connection | None:
    """occupancy.db read-only, or None when there is none or no captures table yet."""
    if not station.db.exists():
        return None
    con = sqlite3.connect(f"file:{station.db}?mode=ro", uri=True, check_same_thread=False)
    con.row_factory = sqlite3.Row
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name = 'captures'").fetchone():
        con.close()
        return None
    return con


def is_private(row: dict, cfg: dict) -> bool:
    """Private if the row says so, its flags say so, or the band table says so now."""
    if (row.get("tier") or "") == "private" or cap.PRIVATE in (row.get("flags") or ""):
        return True
    f = row.get("target_hz") or row.get("freq_hz")
    return bool(f) and cap.band_tier(float(f), cfg)["tier"] == "private"


def badges(row: dict, cfg: dict) -> list:
    out = ["PRIVATE: do not decode" if is_private(row, cfg) else "OPEN"]
    if cap.ANOMALY in (row.get("flags") or ""):
        out.append("ANOMALY: review")
    return out


def _row(r: sqlite3.Row, cfg: dict) -> dict:
    d = dict(r)
    d.setdefault("target_hz", None)
    d["private"] = is_private(d, cfg)
    d["badges"] = badges(d, cfg)
    d.pop("recipe", None)          # the list does not need it; the event page has it
    return d


def _f(con) -> str:
    """The frequency a row is filed under: what the recorder was asked for, when known."""
    return "COALESCE(target_hz, freq_hz)" if cap.has_target(con) else "freq_hz"


def rows(con, cfg: dict, date: str | None = None, lo_mhz=None, hi_mhz=None, badge: str | None = None) -> list:
    q, args = "SELECT * FROM captures WHERE 1", []
    if date:
        q += " AND substr(started, 1, 10) = ?"
        args.append(date)
    if lo_mhz is not None:
        q += f" AND {_f(con)} >= ?"
        args.append(float(lo_mhz) * 1e6)
    if hi_mhz is not None:
        q += f" AND {_f(con)} <= ?"
        args.append(float(hi_mhz) * 1e6)
    out = [_row(r, cfg) for r in con.execute(q + " ORDER BY started_epoch DESC", args)]
    if badge == "open":
        out = [r for r in out if not r["private"]]
    elif badge == "private":
        out = [r for r in out if r["private"]]
    elif badge == "anomaly":
        out = [r for r in out if "ANOMALY: review" in r["badges"]]
    elif badge == "written":
        out = [r for r in out if r["iq_status"] == "written"]
    return out


def group(rs: list) -> list:
    """Events grouped by signal: the frequency the recorder was asked for when known,
    else the measured centre; two events belong together when they lie within 25 kHz
    or within half the wider one's occupied bandwidth."""
    groups: list = []
    for r in sorted(rs, key=lambda r: r.get("target_hz") or r.get("freq_hz") or 0):
        f = r.get("target_hz") or r.get("freq_hz") or 0.0
        half = max(25e3, (r.get("bandwidth_hz") or 0) / 2)
        g = groups[-1] if groups else None
        if g and abs(f - g["f_hz"]) <= max(half, g["half"]):
            g["events"].append(r)
            g["half"] = max(g["half"], half)
        else:
            groups.append({"f_hz": f, "half": half, "events": [r]})
    out = []
    for g in groups:
        ev = sorted(g["events"], key=lambda r: -(r.get("started_epoch") or 0))
        fams = [r["family"] for r in ev if r.get("family")]
        fs = sorted((r.get("target_hz") or r.get("freq_hz") or 0.0) for r in ev)
        out.append({
            "f_mhz": round(fs[len(fs) // 2] / 1e6, 4),
            "family": fams[0] if fams else None,
            "band": next((r["band"] for r in ev if r.get("band")), None),
            "private": any(r["private"] for r in ev),
            "anomaly": any("ANOMALY: review" in r["badges"] for r in ev),
            "events": len(ev),
            "captures": sum(1 for r in ev if r["iq_status"] == "written"),
            "last": ev[0].get("started"),
            "peak_db": max((r.get("snr_peak_db") or 0) for r in ev),
            "rows": ev,
        })
    out.sort(key=lambda g: g["last"] or "", reverse=True)
    return out


# ---------------------------------------------------------------- storage and status

_POOL_CACHE = {"t": 0.0, "pool": None, "bytes": 0}


def storage(station: Station, cfg: dict, con) -> dict:
    pool = cap.pool_dir(station, cfg)
    if _POOL_CACHE["pool"] != str(pool) or time.time() - _POOL_CACHE["t"] > 30:
        _POOL_CACHE.update(t=time.time(), pool=str(pool), bytes=cap.folder_bytes(pool))
    today = cap.bytes_since(con, cap.local_midnight(time.time())) if con is not None else 0
    return {"pool": str(pool), "used_gb": _POOL_CACHE["bytes"] / 1e9, "pool_gb": float(cfg.get("pool_gb", 100)),
            "today_gb": today / 1e9, "daily_gb": float(cfg.get("daily_gb", 20))}


def recorder_status(station: Station, cfg: dict, con) -> dict:
    """What `marsad watch` last said it was doing (recorder_status.json in the pool), and
    the frequencies still in their cooldown according to the catalogue."""
    out = {"state": "unknown", "note": "no recorder has reported yet"}
    p = cap.pool_dir(station, cfg) / STATUS_FILE
    try:
        s = json.loads(p.read_text(encoding="utf-8"))
        live = s.get("state") == "listening" and time.time() < float(s.get("until") or 0) + 60
        out = {**s, "state": s.get("state") if live else "idle"}
        if s.get("state") == "listening" and not live:
            out["note"] = "the last session's time is up; the recorder is not running"
    except (OSError, ValueError):
        pass
    cool = []
    if con is not None:
        span = float(cfg.get("cooldown_min", 30)) * 60
        f = _f(con)
        for r in con.execute(f"SELECT {f} AS f, MAX(started_epoch) AS t FROM captures "
                             f"WHERE captured = 1 AND started_epoch >= ? GROUP BY ROUND({f} / 25e3)",
                             (time.time() - span,)):
            cool.append({"f_mhz": round(r["f"] / 1e6, 4), "until": r["t"] + span})
    out["cooldowns"] = sorted(cool, key=lambda c: c["until"])
    return out


def summary(station: Station, date=None, lo_mhz=None, hi_mhz=None, badge=None) -> dict:
    cfg = cap.settings(station)
    con = open_ro(station)
    try:
        if con is None:
            return {"storage": storage(station, cfg, None), "dates": [], "groups": [], "total": 0,
                    "status": recorder_status(station, cfg, None), "db": None}
        dates = [r[0] for r in con.execute("SELECT DISTINCT substr(started, 1, 10) FROM captures "
                                           "WHERE started IS NOT NULL ORDER BY 1 DESC")]
        rs = rows(con, cfg, date, lo_mhz, hi_mhz, badge)
        return {"storage": storage(station, cfg, con), "dates": dates, "groups": group(rs),
                "total": len(rs), "status": recorder_status(station, cfg, con), "db": str(station.db)}
    finally:
        if con is not None:
            con.close()


# ---------------------------------------------------------------- one event

def _inside(path, station: Station, cfg: dict) -> Path:
    """A path from the catalogue, only if it lies inside the recorder pool."""
    if not path:
        raise Refused("no file recorded for this event")
    p = Path(path).resolve()
    pool = cap.pool_dir(station, cfg).resolve()
    if pool not in p.parents:
        raise Refused(f"{p.name} is not inside the recorder pool ({pool})")
    if not p.exists():
        raise Refused(f"{p.name} is no longer on disk")
    return p


def _get(con, event_id: int) -> dict:
    r = con.execute("SELECT * FROM captures WHERE id = ?", (int(event_id),)).fetchone()
    if r is None:
        raise Refused(f"no event {event_id} in the catalogue")
    d = dict(r)
    d.setdefault("target_hz", None)
    return d


def covering_capture(con, row: dict) -> dict | None:
    """The written capture whose IQ holds this event: its own, or an earlier one on the
    same frequency whose span (from its file size) reaches past this event's start."""
    if row.get("iq_status") == "written":
        return row
    if row.get("iq_status") not in ("inside an earlier capture",):
        return None
    f = row.get("target_hz") or row.get("freq_hz")
    for c in cap.near(con, f, 25e3, "iq_status = 'written'"):
        c = dict(c)
        if c["started_epoch"] > row["started_epoch"] or not c.get("store_rate_hz"):
            continue
        span = (c.get("iq_bytes") or 0) / 4 / c["store_rate_hz"]
        if row["started_epoch"] <= c["started_epoch"] + span + 1.5:
            return c
        break
    return None


def _meta(data_path: Path) -> dict:
    m = data_path.with_name(data_path.name.replace(".sigmf-data", ".sigmf-meta"))
    return json.loads(m.read_text(encoding="utf-8"))


def event(station: Station, event_id: int) -> dict:
    cfg = cap.settings(station)
    con = open_ro(station)
    if con is None:
        raise Refused("there is no captures catalogue yet")
    try:
        row = _get(con, event_id)
        private = is_private(row, cfg)
        out = {"row": {k: v for k, v in row.items() if k != "recipe"}, "private": private,
               "badges": badges(row, cfg), "recipe": None, "meta": None, "report": None,
               "capture": None, "exports": []}
        if row.get("recipe"):
            try:
                out["recipe"] = json.loads(row["recipe"])
            except ValueError:
                pass
        if private:
            return out                 # fingerprint only: no IQ, no report, no exports
        c = covering_capture(con, row)
        if c is None:
            return out
        try:
            data = _inside(c["iq_path"], station, cfg)
        except Refused as exc:
            out["capture_note"] = str(exc)
            return out
        meta = _meta(data)
        out["meta"] = meta
        out["capture"] = {"id": c["id"], "file": data.name, "bytes": data.stat().st_size,
                          "own": c["id"] == row["id"],
                          "offset_s": max(0.0, row["started_epoch"] - c["started_epoch"]),
                          "seconds": data.stat().st_size / 4 / float(meta["global"]["core:sample_rate"])}
        if c.get("characterised"):
            try:
                out["report"] = _inside(c["characterised"], station, cfg).read_text(encoding="utf-8")
            except Refused:
                pass
        if out["recipe"] is None and c.get("recipe"):
            try:
                out["recipe"] = json.loads(c["recipe"])
            except ValueError:
                pass
        ex = data.parent / "exports"
        stem = data.name.replace(".sigmf-data", "")
        out["exports"] = sorted(p.name for p in ex.glob("*") if stem in p.name or _wav_name(meta, stem) == p.name) \
            if ex.exists() else []
        return out
    finally:
        con.close()


# ---------------------------------------------------------------- pictures

def _u8(over_db: np.ndarray) -> str:
    q = np.clip((over_db - DB_LO) / DB_SPAN * 255.0, 0, 255)
    q = np.nan_to_num(q, nan=0.0).astype(np.uint8)
    return base64.b64encode(q.tobytes()).decode("ascii")


def _maxpool(a: np.ndarray, rows: int, cols: int) -> np.ndarray:
    r = max(1, -(-a.shape[0] // rows))
    c = max(1, -(-a.shape[1] // cols))
    R, C = -(-a.shape[0] // r), -(-a.shape[1] // c)
    pad = np.full((R * r, C * c), -np.inf, dtype=np.float32)
    pad[:a.shape[0], :a.shape[1]] = a
    return pad.reshape(R, r, C, c).max(axis=(1, 3))


def fingerprint_view(station: Station, event_id: int) -> dict:
    """Every event's spectrogram, private ones included: power at 20 ms x ~1 kHz cannot
    be demodulated, which is why it is the one thing a private event keeps."""
    cfg = cap.settings(station)
    con = open_ro(station)
    if con is None:
        raise Refused("there is no captures catalogue yet")
    try:
        row = _get(con, event_id)
    finally:
        con.close()
    p = _inside(row.get("fingerprint"), station, cfg)
    z = np.load(p)
    spec = z["spec_db"].astype(np.float32)
    over = spec - z["floor_db"].astype(np.float32)[None, :]
    img = _maxpool(over, FP_MAX_ROWS, FP_MAX_BINS)
    f = z["f_rel_hz"]
    dt_s = float(z["dt_s"])
    return {"rows": int(img.shape[0]), "cols": int(img.shape[1]), "img": _u8(img),
            "scale_db": [DB_LO, DB_LO + DB_SPAN], "f_lo_khz": float(f[0]) / 1e3, "f_hi_khz": float(f[-1]) / 1e3,
            "seconds": spec.shape[0] * dt_s, "dt_s": dt_s, "signal_mhz": float(z["signal_hz"]) / 1e6,
            "start": str(z["start"]), "private": is_private(row, cfg)}


_IQ_CACHE: dict = {}
_IQ_LOCK = threading.Lock()


def _reader(data: Path, datatype: str):
    """read(start, count) -> complex64 at full scale 1.0, reading only that slice."""
    kind = {"ci16_le": (np.int16, 32768.0, 0.0), "cu8": (np.uint8, 127.5, 127.5),
            "cf32_le": (np.float32, 1.0, 0.0)}.get(datatype)
    if kind is None:
        raise Refused(f"SigMF datatype {datatype!r} is not one the recorder writes")
    dtype, scale, zero = kind
    item = np.dtype(dtype).itemsize
    n = data.stat().st_size // (2 * item)

    def read(start: int, count: int) -> np.ndarray:
        count = max(0, min(count, n - start))
        r = np.fromfile(data, dtype=dtype, count=2 * count, offset=2 * item * start).astype(np.float32)
        return ((r - zero) / scale).view(np.complex64)
    return read, n


def iq_view(station: Station, event_id: int) -> dict:
    """Waterfall (max-hold within each row, so a 114 us pulse survives) and envelope
    (peak and mean power per slice) of the capture that holds this event's IQ.
    Reads the file one row-slice at a time; the answer is cached per file."""
    cfg = cap.settings(station)
    con = open_ro(station)
    if con is None:
        raise Refused("there is no captures catalogue yet")
    try:
        row = _get(con, event_id)
        if is_private(row, cfg):
            raise Refused("private band: there is no IQ, by design")
        c = covering_capture(con, row)
    finally:
        con.close()
    if c is None:
        raise Refused("no IQ was written for this event (" + (row.get("iq_status") or "not recorded") + ")")
    data = _inside(c["iq_path"], station, cfg)
    st_ = data.stat()
    key = (str(data), st_.st_size, st_.st_mtime)
    with _IQ_LOCK:
        if key in _IQ_CACHE:
            return _IQ_CACHE[key]
    meta = _meta(data)
    g = meta["global"]
    fs = float(g["core:sample_rate"])
    read, n = _reader(data, g["core:datatype"])
    nfft = IQ_NFFT if n >= IQ_NFFT * 4 else 64
    rows_n = max(1, min(IQ_ROWS, n // nfft))
    per = n // rows_n
    win = np.hanning(nfft).astype(np.float32)
    win /= np.sqrt(np.mean(win ** 2))
    wf_max, wf_mean, env_pk, env_mean = [], [], [], []
    for i in range(rows_n):
        x = read(i * per, per)
        m = (len(x) // nfft) * nfft
        if m == 0:
            break
        fr = x[:m].reshape(-1, nfft)
        P = np.abs(np.fft.fftshift(np.fft.fft(fr * win, axis=1), axes=1)) ** 2 / nfft
        wf_max.append(P.max(axis=0))
        wf_mean.append(P.mean(axis=0))
        p = np.abs(x) ** 2
        k = len(p) // ENV_PER_ROW
        if k:
            seg = p[:k * ENV_PER_ROW].reshape(ENV_PER_ROW, k)
            env_pk.extend(seg.max(axis=1))
            env_mean.extend(seg.mean(axis=1))
    wmax = 10 * np.log10(np.maximum(np.array(wf_max, np.float32), 1e-14))
    wmean = 10 * np.log10(np.maximum(np.array(wf_mean, np.float32), 1e-14))
    floor = np.median(wmean, axis=0)                     # per-bin floor: the quiet majority
    img = _maxpool(wmax - floor[None, :], IQ_ROWS, IQ_BINS)
    centre = float(meta["captures"][0]["core:frequency"])
    out = {"rows": int(img.shape[0]), "cols": int(img.shape[1]), "img": _u8(img),
           "scale_db": [DB_LO, DB_LO + DB_SPAN], "f_lo_khz": -fs / 2e3, "f_hi_khz": fs / 2e3,
           "centre_mhz": centre / 1e6, "seconds": n / fs, "rate": fs, "file": data.name,
           "env_dbfs_peak": [round(float(v), 2) for v in 10 * np.log10(np.maximum(env_pk, 1e-12))],
           "env_dbfs_mean": [round(float(v), 2) for v in 10 * np.log10(np.maximum(env_mean, 1e-12))],
           "env_dt_s": per / ENV_PER_ROW / fs, "nfft": nfft}
    with _IQ_LOCK:
        if len(_IQ_CACHE) > 8:
            _IQ_CACHE.pop(next(iter(_IQ_CACHE)))
        _IQ_CACHE[key] = out
    return out


# ---------------------------------------------------------------- exports

def _wav_name(meta: dict, stem: str) -> str:
    """SDR++'s own convention, baseband_<Hz>Hz_<HH-MM-SS>_<DD-MM-YYYY>.wav, so its file
    source finds the centre frequency (it takes the first [0-9]+Hz in the name)."""
    c = meta["captures"][0]
    t = dt.datetime.strptime(c["core:datetime"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc).astimezone()
    return f"baseband_{int(round(c['core:frequency']))}Hz_{t:%H-%M-%S_%d-%m-%Y}_{stem.split('_')[-1]}.wav"


def _wav_header(n: int, rate: int) -> bytes:
    data = n * 4
    return (b"RIFF" + struct.pack("<I", 36 + data) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 2, rate, rate * 4, 4, 16)
            + b"data" + struct.pack("<I", data))


def export(station: Station, event_id: int, fmt: str) -> dict:
    """Copy an open-band capture to `exports/` as WAV (SDR++) or cf32 + SigMF meta.

    Private events are refused here, on the server, whatever the page asks: they have
    no IQ by design, and nothing should ever turn one into a playable file."""
    cfg = cap.settings(station)
    if fmt not in ("wav", "cf32"):
        raise Refused(f"unknown export format {fmt!r} (wav or cf32)")
    con = open_ro(station)
    if con is None:
        raise Refused("there is no captures catalogue yet")
    try:
        row = _get(con, event_id)
        if is_private(row, cfg):
            raise Refused("private band: no export, by design (PRIVATE: do not decode)")
        c = covering_capture(con, row)
    finally:
        con.close()
    if c is None:
        raise Refused("this event has no IQ capture to export")
    data = _inside(c["iq_path"], station, cfg)
    meta = _meta(data)
    g = meta["global"]
    fs = float(g["core:sample_rate"])
    read, n = _reader(data, g["core:datatype"])
    stem = data.name.replace(".sigmf-data", "")
    outdir = data.parent / "exports"
    outdir.mkdir(exist_ok=True)
    notes = []
    if fmt == "wav":
        rate = int(round(fs))
        if abs(rate - fs) > 1e-6:
            notes.append(f"WAV holds whole-number rates: {fs:.3f} S/s written as {rate}")
        if n * 4 + 36 >= 2 ** 32:
            raise Refused("too long for a WAV file (4 GB limit); use cf32")
        target = outdir / _wav_name(meta, stem)
        head, size = _wav_header(n, rate), 44 + n * 4
    else:
        target = outdir / f"{stem}.cf32"
        head, size = b"", n * 8
    if target.exists() and target.stat().st_size == size:
        return {"file": str(target), "bytes": size, "note": "already exported", "notes": notes}
    part = target.with_name(target.name + ".part")
    with open(part, "wb") as fh:
        fh.write(head)
        for s in range(0, n, EXPORT_CHUNK):
            x = read(s, EXPORT_CHUNK)
            if fmt == "wav":
                iq = np.empty(2 * len(x), np.int16)
                iq[0::2] = np.clip(np.round(x.real * 32768), -32768, 32767)
                iq[1::2] = np.clip(np.round(x.imag * 32768), -32768, 32767)
                fh.write(iq.tobytes())
            else:
                fh.write(x.astype(np.complex64).tobytes())
    os.replace(part, target)
    if fmt == "cf32":
        m = json.loads(json.dumps(meta))
        m["global"]["core:datatype"] = "cf32_le"
        m["global"]["core:dataset"] = target.name          # SigMF's non-conforming dataset name
        m["global"]["marsad:exported_from"] = data.name
        (outdir / f"{stem}.sigmf-meta").write_text(json.dumps(m, indent=2), encoding="utf-8")
    return {"file": str(target), "bytes": size, "note": "exported", "notes": notes}
