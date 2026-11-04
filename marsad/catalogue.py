"""The Captures tab's server side.

Reads the `captures` table in occupancy.db and the files in the recorder pool; never
writes to either, except that an export makes a COPY of an open-band capture in an
`exports` folder beside it. Nothing here deletes anything. The one way a file leaves
the pool is capture_archive.py: zipped, verified, then moved, on an explicit confirm.

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
import io
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
    if row.get("iq_status") == "archived":
        out.append("ARCHIVED")
    return out


def _row(r: sqlite3.Row, cfg: dict) -> dict:
    d = dict(r)
    d.setdefault("target_hz", None)
    d["private"] = is_private(d, cfg)
    d["badges"] = badges(d, cfg)
    d["grade"], d["grade_snr_db"], d["grade_source"] = cap.grade_row(d)
    # Worked out on read, so old rows get them too and nothing in the catalogue is rewritten.
    d["filter_shaped"] = cap.filter_shaped(d)
    d["harmonic"] = cap.harmonic_note(d.get("freq_hz"), cfg)
    d.pop("recipe", None)          # the list does not need it; the event page has it
    return d


def _f(con) -> str:
    """The frequency a row is filed under: what the recorder was asked for, when known."""
    return "COALESCE(target_hz, freq_hz)" if cap.has_target(con) else "freq_hz"


GRADE_ORDER = {"strong": 3, "medium": 2, "weak": 1, "empty": 0}


INSIDE = "inside an earlier capture"
HAS_IQ = ("written", INSIDE)


def _tod(t: str | None) -> str | None:
    """'7:5' or '07:05' -> '07:05'; anything else None."""
    try:
        h, m = (t or "").strip().split(":")[:2]
        return f"{int(h):02d}:{int(m):02d}"
    except ValueError:
        return None


def rows(con, cfg: dict, date: str | None = None, lo_mhz=None, hi_mhz=None, badge: str | None = None,
         grade: str | None = None, date_from: str | None = None, date_to: str | None = None,
         tod_from: str | None = None, tod_to: str | None = None, family: str | None = None,
         content: str | None = None, tier: str | None = None, anomaly=None, archived: str | None = None,
         watch: str | None = None) -> list:
    """Catalogue rows, every filter combinable. `badge` is the old single-choice filter,
    kept for callers that still send it. Time of day is local; a range that runs past
    midnight (22:00 to 05:00) wraps. `watch` is the survey a row came from, or "fixed"
    for the fixed watches (no survey). `content` "iq" means the row has IQ in the pool
    (its own capture, or one it fell inside); "fp" means a fingerprint and nothing more.
    `grade` takes one grade or several joined by commas ("strong,medium")."""
    q, args = "SELECT * FROM captures WHERE 1", []
    if date:
        q += " AND substr(started, 1, 10) = ?"
        args.append(date)
    if date_from:
        q += " AND substr(started, 1, 10) >= ?"
        args.append(date_from)
    if date_to:
        q += " AND substr(started, 1, 10) <= ?"
        args.append(date_to)
    a, b = _tod(tod_from), _tod(tod_to)
    if a or b:
        a, b = a or "00:00", b or "23:59"
        t = "substr(started, 12, 5)"
        q += f" AND ({t} >= ? AND {t} <= ?)" if a <= b else f" AND ({t} >= ? OR {t} <= ?)"
        args += [a, b]
    if lo_mhz is not None:
        q += f" AND {_f(con)} >= ?"
        args.append(float(lo_mhz) * 1e6)
    if hi_mhz is not None:
        q += f" AND {_f(con)} <= ?"
        args.append(float(hi_mhz) * 1e6)
    if family == "none":
        q += " AND family IS NULL"
    elif family:
        q += " AND family = ?"
        args.append(family)
    if watch == "fixed":
        q += " AND survey IS NULL"
    elif watch:
        q += " AND survey = ?"
        args.append(watch)
    if content == "iq":
        q += " AND iq_status IN (?, ?)"
        args += list(HAS_IQ)
    elif content == "fp":
        q += " AND COALESCE(iq_status, '') NOT IN (?, ?, 'archived')"
        args += list(HAS_IQ)
    if archived == "yes":
        q += " AND iq_status = 'archived'"
    elif archived == "no":
        q += " AND COALESCE(iq_status, '') != 'archived'"
    out = [_row(r, cfg) for r in con.execute(q + " ORDER BY started_epoch DESC", args)]
    if badge == "open" or tier == "open":
        out = [r for r in out if not r["private"]]
    if badge == "private" or tier == "private":
        out = [r for r in out if r["private"]]
    if badge == "anomaly" or anomaly:
        out = [r for r in out if "ANOMALY: review" in r["badges"]]
    if badge == "written":
        out = [r for r in out if r["iq_status"] == "written"]
    elif badge == "archived":
        out = [r for r in out if r["iq_status"] == "archived"]
    if grade:
        want = set(grade.split(","))
        out = [r for r in out if (r["grade"] or "ungraded") in want]
    return out


def _bytes(p) -> int:
    try:
        return Path(p).stat().st_size if p else 0
    except OSError:
        return 0


def sizes(con, station: Station, cfg: dict, rs: list) -> None:
    """On-disk bytes in the pool for each row, set in place. A row is charged for what
    it owns: its fingerprint and, when it owns a capture, the SigMF pair, the
    characterise report and any exports. An event that fell inside a capture is charged
    only its fingerprint and names the capture (`inside_of`, with that capture's own
    bytes in `inside_of_bytes`), so a shared file is counted once. An archived row is
    charged what is still in the pool (a fingerprint kept there, by default)."""
    from .capture_archive import _exports_of
    owned: dict = {}

    def own(r) -> int:
        if r["id"] in owned:
            return owned[r["id"]]
        n = _bytes(r.get("fingerprint"))
        if r.get("iq_status") == "written" and r.get("iq_path"):
            data = Path(r["iq_path"])
            n += _bytes(data) + _bytes(data.with_name(data.name.replace(".sigmf-data", ".sigmf-meta")))
            if r.get("characterised"):
                md = Path(r["characterised"])
                n += _bytes(md) + _bytes(md.with_suffix(".json"))
            if (data.parent / "exports").exists():
                n += sum(_bytes(x) for x in _exports_of(data))
        owned[r["id"]] = n
        return n

    for r in rs:
        r["bytes"] = own(r)
        r["inside_of"] = r["inside_of_bytes"] = None
        if r.get("iq_status") == INSIDE:
            c = covering_capture(con, r)
            if c is not None:
                r["inside_of"] = c["id"]
                r["inside_of_bytes"] = own(dict(c))


def group(rs: list, sort: str = "time") -> list:
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
        ev = sorted(g["events"], key=(lambda r: (-(r.get("bytes") or 0), -(r.get("started_epoch") or 0)))
                    if sort == "size" else (lambda r: -(r.get("started_epoch") or 0)))
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
            "archived": sum(1 for r in ev if r["iq_status"] == "archived"),
            "last": ev[0].get("started"),
            "peak_db": max((r.get("snr_peak_db") or 0) for r in ev),
            "bytes": sum(r.get("bytes") or 0 for r in ev),
            # the best grade any of its events reached, and how many of each
            "grade": max((r["grade"] for r in ev if r.get("grade")), key=GRADE_ORDER.get, default=None),
            "grades": {g: sum(1 for r in ev if r.get("grade") == g) for g in GRADE_ORDER
                       if any(r.get("grade") == g for r in ev)},
            "rows": ev,
        })
    if sort == "size":
        out.sort(key=lambda g: (g["bytes"], g["last"] or ""), reverse=True)
    else:
        out.sort(key=lambda g: g["last"] or "", reverse=True)
    return out


EPISODE_GAP_S = 600.0        # events further apart than this are not one episode
EPISODE_SPAN_HZ = 15e6       # ...nor further apart in frequency than this
EPISODE_MIN_DB = 20.0        # weak events chain together by chance across a busy band


def episodes(rs: list, gap_s: float = EPISODE_GAP_S, span_hz: float = EPISODE_SPAN_HZ,
             min_db: float = EPISODE_MIN_DB) -> list:
    """Strong events close in time and in frequency, read as one happening: at least three
    events on at least two signals, so a lone signal repeating is not an episode. A view over
    the events and never a replacement for them. Open bands only."""
    open_eps: list = []
    done: list = []
    for r in sorted(rs, key=lambda r: r.get("started_epoch") or 0):
        if r.get("private") or not r.get("started_epoch") or not r.get("freq_hz") or (r.get("snr_peak_db") or 0) < min_db:
            continue
        t, f = r["started_epoch"], r.get("target_hz") or r["freq_hz"]
        for e in [e for e in open_eps if t - e["t1"] > gap_s]:
            open_eps.remove(e)
            done.append(e)
        hit = next((e for e in open_eps if max(e["hi"], f) - min(e["lo"], f) <= span_hz), None)
        if hit is None:
            hit = {"lo": f, "hi": f, "t0": t, "t1": t, "rows": []}
            open_eps.append(hit)
        hit.update(lo=min(hit["lo"], f), hi=max(hit["hi"], f), t1=t)
        hit["rows"].append(r)
    out = []
    for e in done + open_eps:
        n = len(group(e["rows"]))
        if len(e["rows"]) >= 3 and n >= 2:
            out.append({"first": e["rows"][0].get("started"), "last": e["rows"][-1].get("started"),
                        "lo_mhz": round(e["lo"] / 1e6, 3), "hi_mhz": round(e["hi"] / 1e6, 3),
                        "events": len(e["rows"]), "signals": n, "ids": [r["id"] for r in e["rows"]]})
    return sorted(out, key=lambda e: e["first"] or "", reverse=True)


# ---------------------------------------------------------------- storage and status

_POOL_CACHE = {"t": 0.0, "pool": None, "bytes": 0, "archive_gb": 0.0}


def storage(station: Station, cfg: dict, con) -> dict:
    from . import capture_archive
    pool = cap.pool_dir(station, cfg)
    if _POOL_CACHE["pool"] != str(pool) or time.time() - _POOL_CACHE["t"] > 30:
        _POOL_CACHE.update(t=time.time(), pool=str(pool), bytes=cap.folder_bytes(pool),
                           archive_gb=capture_archive.size_gb(station, cfg))
    today = cap.bytes_since(con, cap.local_midnight(time.time())) if con is not None else 0
    try:
        adir = str(capture_archive.archive_dir(station, cfg))
    except Refused as exc:
        adir = str(exc)
    return {"pool": str(pool), "used_gb": _POOL_CACHE["bytes"] / 1e9, "pool_gb": float(cfg.get("pool_gb", 100)),
            "today_gb": today / 1e9, "daily_gb": float(cfg.get("daily_gb", 20)),
            "archive": adir, "archive_gb": _POOL_CACHE["archive_gb"]}


STALE_S = 15.0        # a running watch rewrites its file every 2 s; silence this long means it is gone


def live_recorders(station: Station, cfg: dict | None = None, now: float | None = None) -> list:
    """Every recorder that has reported (one recorder_status_<serial>.json per radio in the
    pool), with `running` true only while its file is fresh. A watch killed without warning
    (Ctrl-C in its window, a power cut) leaves state "running" behind, so freshness decides,
    not the state field. Reads small files only: no radio, no database."""
    cfg = cfg or cap.settings(station)
    now = time.time() if now is None else now
    out = []
    for p in sorted(cap.pool_dir(station, cfg).glob("recorder_status_*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        age = now - float(d.get("updated") or 0)
        d["age_s"] = round(age, 1)
        d["running"] = d.get("state") == "running" and age <= STALE_S
        if d.get("state") == "running" and not d["running"]:
            ago = f"{age:.0f} s" if age < 120 else f"{age / 60:.0f} min"
            d["note"] = f"stopped reporting {ago} ago (closed or killed)"
        out.append(d)
    out.sort(key=lambda d: (not d["running"], -float(d.get("updated") or 0)))
    return out


def recorder_status(station: Station, cfg: dict, con) -> dict:
    """The live recorders, plus the cooldowns the catalogue knows about (which cover a
    recorder that has stopped, or one from before the status file existed)."""
    out = {"recorders": live_recorders(station, cfg)}
    cool = []
    if con is not None:
        span = float(cfg.get("cooldown_min", 30)) * 60
        f = _f(con)
        for r in con.execute(f"SELECT {f} AS f, MAX(started_epoch) AS t FROM captures "
                             f"WHERE iq_status = 'written' AND started_epoch >= ? GROUP BY ROUND({f} / 25e3)",
                             (time.time() - span,)):
            cool.append({"mhz": round(r["f"] / 1e6, 4), "until": r["t"] + span})
    out["cooldowns"] = sorted(cool, key=lambda c: c["until"])
    return out


FILTERS = ("date_from", "date_to", "tod_from", "tod_to", "family", "content", "tier", "anomaly", "archived",
           "watch")


def summary(station: Station, date=None, lo_mhz=None, hi_mhz=None, badge=None, grade=None,
            sort: str = "time", min_mb=None, **filters) -> dict:
    """The list for the Captures tab: rows filtered, sized, grouped by signal. `min_mb`
    keeps rows charged at least that many MB (an event inside a capture is charged its
    fingerprint only, so it drops out unless its capture is big enough to stay)."""
    cfg = cap.settings(station)
    con = open_ro(station)
    try:
        if con is None:
            return {"storage": storage(station, cfg, None), "dates": [], "groups": [], "total": 0, "bytes": 0,
                    "families": [], "watches": [], "status": recorder_status(station, cfg, None), "db": None}
        dates = [r[0] for r in con.execute("SELECT DISTINCT substr(started, 1, 10) FROM captures "
                                           "WHERE started IS NOT NULL ORDER BY 1 DESC")]
        fams = [r[0] for r in con.execute("SELECT DISTINCT family FROM captures WHERE family IS NOT NULL ORDER BY 1")]
        watches = [{"survey": r[0], "events": r[1], "first": r[2]} for r in con.execute(
            "SELECT survey, COUNT(*), MIN(started) FROM captures GROUP BY survey ORDER BY 3 DESC")]
        rs = rows(con, cfg, date, lo_mhz, hi_mhz, badge, grade, **{k: v for k, v in filters.items() if k in FILTERS})
        sizes(con, station, cfg, rs)
        if min_mb:
            rs = [r for r in rs if r["bytes"] >= float(min_mb) * 1e6]
        return {"storage": storage(station, cfg, con), "dates": dates, "groups": group(rs, sort),
                "total": len(rs), "bytes": sum(r["bytes"] for r in rs), "families": fams, "watches": watches,
                "episodes": episodes(rs),
                "status": recorder_status(station, cfg, con), "db": str(station.db)}
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
        g = cap.grade_row(row)
        shown = {k: v for k, v in row.items() if k != "recipe"}
        shown.update(grade=g[0], grade_snr_db=g[1], grade_source=g[2], filter_shaped=cap.filter_shaped(row),
                     harmonic=cap.harmonic_note(row.get("freq_hz"), cfg))
        if shown.get("settings"):
            try:
                shown["settings"] = json.loads(shown["settings"])
            except ValueError:
                pass
        out = {"row": shown, "private": private,
               "badges": badges(row, cfg), "recipe": None, "meta": None, "report": None,
               "capture": None, "exports": [], "archive": None}
        if row.get("archive_path"):
            from . import capture_archive
            out["archive"] = capture_archive.describe(station, row["archive_path"])
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
    try:
        z = np.load(_inside(row.get("fingerprint"), station, cfg))
    except Refused:
        if not row.get("archive_path"):
            raise
        from . import capture_archive          # taken out of the pool: read it from the zip
        z = np.load(io.BytesIO(capture_archive.member(station, row["archive_path"], row["fingerprint"])))
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
    """Waterfall (max-hold within each row, so a 100 us pulse survives) and envelope
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


# ---------------------------------------------------------------- listening

AUDIO_MODES = {"am": 10e3, "nfm": 12.5e3, "envelope": None}   # channel width; envelope: the whole band
AUDIO_RATE = 48000             # aimed at; the real rate is the capture rate over a whole decimation
AUDIO_MAX_S = 180              # one request plays at most this much
AUDIO_CHUNK_S = 0.5            # read and demodulate this much at a time
NFM_DEVIATION_HZ = 5000.0
ENV_GATE = 1.3                 # envelope mode keeps what stands this far over the chunk's median
ENV_FULL = 8.0                 # ... and only something this many times the gate reaches full scale
AUDIO_MIN_BOX_HZ = 100.0       # a box narrower than this is not a channel a filter can hold
AUDIO_FADE_S = 0.002           # eased in and out so a selection's cut edges do not click
ENV_REF_BLOCKS, ENV_REF_S = 8, 0.02   # where the gate's first level comes from: see _env_reference


def audio_plan(station: Station, event_id: int, mode: str = "am", start_s: float = 0.0,
               offset_hz: float = 0.0, dur_s: float | None = None, bw_hz: float | None = None) -> dict:
    """What /captures/audio will play for an event, or Refused.

    Refused for anything private, exactly as iq_view and export are (the row's tier, its
    flags, or the band table now), and for an event with no IQ. The audio itself is made
    by audio_stream, on the fly, and never written anywhere.

    bw_hz is the width of a box picked on the waterfall (offset_hz is its centre): it
    replaces the mode's channel, and envelope mode then hears only that box too. A box too
    wide for the audio rate cannot be sampled at it: `wide` says so, and audio_stream then
    max-holds (am) or runs the discriminator at the capture rate (nfm). `demod` says in
    words what was played, for the page."""
    if mode not in AUDIO_MODES:
        raise Refused(f"unknown mode {mode!r} (am, nfm or envelope)")
    cfg = cap.settings(station)
    con = open_ro(station)
    if con is None:
        raise Refused("there is no captures catalogue yet")
    try:
        row = _get(con, event_id)
        if is_private(row, cfg):
            raise Refused("private band: nothing to listen to, by design (PRIVATE: do not decode)")
        c = covering_capture(con, row)
    finally:
        con.close()
    if c is None:
        raise Refused("no IQ was written for this event (" + (row.get("iq_status") or "not recorded") + ")")
    data = _inside(c["iq_path"], station, cfg)
    g = _meta(data)["global"]
    fs = float(g["core:sample_rate"])
    _, n = _reader(data, g["core:datatype"])
    if not abs(offset_hz) < fs / 2:
        raise Refused(f"offset {offset_hz / 1e3:g} kHz is outside the capture's band (+/-{fs / 2e3:g} kHz)")
    if bw_hz is not None:
        if not bw_hz >= AUDIO_MIN_BOX_HZ:
            raise Refused(f"a box {bw_hz / 1e3:g} kHz wide is too narrow to listen to (at least {AUDIO_MIN_BOX_HZ / 1e3:g} kHz)")
        if abs(offset_hz) + bw_hz / 2 > fs / 2 + 10:        # 10 Hz: the page's rounding
            raise Refused(f"a box {bw_hz / 1e3:g} kHz wide at offset {offset_hz / 1e3:g} kHz reaches outside "
                          f"the capture's band (+/-{fs / 2e3:g} kHz)")
    decim = max(1, int(fs // AUDIO_RATE))
    rate = int(round(fs / decim))
    bw = bw_hz if bw_hz is not None else AUDIO_MODES[mode]
    wide = bool(bw) and decim > 1 and bw > rate      # more band than its samples can carry
    start = int(max(0.0, min(float(start_s), n / fs)) * fs)
    count = n - start
    if dur_s is not None:
        count = min(count, int(max(0.0, float(dur_s)) * fs))
    count = min(count, int(AUDIO_MAX_S * fs))
    out_n = -(-count // decim)
    width = (f"{bw_hz / 1e3:.1f} kHz box" if bw_hz is not None
             else f"{bw / 1e3:g} kHz channel" if bw else f"whole {fs / 1e3:g} kHz band")
    demod = {"envelope": f"pulse envelope of the {width}",
             "am": (f"pulse envelope of the {width}, too wide for AM at {rate / 1e3:g} kHz audio" if wide
                    else f"AM over the {width}"),
             "nfm": (f"FM discriminator over the {width}, run at the capture rate: too wide for {rate / 1e3:g} kHz audio"
                     if wide else f"narrow FM over the {width}")}[mode]
    return {"data": data, "datatype": g["core:datatype"], "fs": fs, "n": n, "start": start,
            "count": count, "decim": decim, "rate": rate, "mode": mode,
            "offset_hz": float(offset_hz), "bw_hz": bw, "wide": wide, "demod": demod, "samples": out_n,
            "bytes": 44 + 2 * out_n, "seconds": n / fs}


def _wav_mono16_header(n: int, rate: int) -> bytes:
    return (b"RIFF" + struct.pack("<I", 36 + 2 * n) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
            + b"data" + struct.pack("<I", 2 * n))


def _hold(m: np.ndarray, decim: int) -> np.ndarray:
    """The largest value in each output sample, so a pulse shorter than one survives."""
    return np.pad(m, (0, (-len(m)) % decim)).reshape(-1, decim).max(axis=1)


def _env_reference(plan: dict, read, sos, signal) -> float | None:
    """The noise level for envelope mode's gate, from short blocks spread over the capture
    (the quietest block's median). A selection can be all signal, and its own median would
    then gate away the very thing picked. Only the level leaves here; nothing is kept."""
    fs, n = plan["fs"], plan["n"]
    k = min(int(ENV_REF_S * fs), n)
    if k < plan["decim"]:
        return None
    w, levels = -2 * np.pi * plan["offset_hz"] / fs, []
    for i0 in np.linspace(0, n - k, ENV_REF_BLOCKS).astype(np.int64):
        x = read(int(i0), k).astype(np.complex128)
        if w:
            x = x * np.exp(1j * np.mod(w * (np.arange(len(x), dtype=np.float64) + int(i0)), 2 * np.pi))
        if sos is not None:
            x, _ = signal.sosfilt(sos, x, zi=signal.sosfilt_zi(sos) * x[0])
        levels.append(float(np.median(_hold(np.abs(x), plan["decim"]))))
    return min(levels) * ENV_GATE


def audio_stream(plan: dict):
    """Yield a mono 16-bit WAV for the plan: the header, then PCM a chunk at a time.

    am        channel low-pass, magnitude, DC removed
    nfm       channel low-pass, phase step between samples, scaled for 5 kHz deviation
              (the AGC undoes the scale, so a wide box needs no other deviation)
    envelope  the magnitude over the whole capture band, or over the box when one was picked,
              max-held over each output sample, so a 100 us radar pulse is a click and a pulse
              train is its rhythm
    A box wider than the audio rate holds more than its samples can carry, so am takes the
    envelope path (x[::decim] would step over short pulses) and nfm takes its phase steps at
    the capture rate and averages them down.
    The offset moves the listening point within the capture's band (mixed with a phase
    that runs on across chunks). An AGC (down at once, up 5 % a chunk) sets the level. Both
    filters start settled, as if the signal had always been there, so a selection opens
    without a click. Nothing is written to disk: the bytes go to the caller and nowhere else."""
    from scipy import signal
    read, _ = _reader(plan["data"], plan["datatype"])
    fs, decim, mode = plan["fs"], plan["decim"], plan["mode"]
    chunk = max(decim, int(AUDIO_CHUNK_S * fs) // decim * decim)
    sos = None
    if plan["bw_hz"] and plan["bw_hz"] / 2 < 0.45 * fs:
        sos = signal.butter(6, plan["bw_hz"] / 2, fs=fs, output="sos")
    zi = None
    envelope = mode == "envelope" or (mode == "am" and plan["wide"])
    fade = max(1, int(AUDIO_FADE_S * plan["rate"]))
    hp_b, hp_a = [1.0, -1.0], [1.0, -0.995]                 # DC removal, ~38 Hz at 48 kHz
    hp_zi = np.zeros(1)
    w = -2 * np.pi * plan["offset_hz"] / fs
    prev = None
    gain = None
    env_floor = _env_reference(plan, read, sos, signal) if envelope and plan["count"] else None
    yield _wav_mono16_header(plan["samples"], plan["rate"])
    done, left = 0, plan["count"]
    while left > 0:
        k = min(chunk, left)
        i0 = plan["start"] + done
        x = read(i0, k).astype(np.complex128)
        if len(x) < k:                                      # a short file: pad with silence
            x = np.concatenate([x, np.zeros(k - len(x), np.complex128)])
        if w:
            x = x * np.exp(1j * np.mod(w * (np.arange(k, dtype=np.float64) + i0), 2 * np.pi))
        if sos is not None:
            if zi is None:
                zi = signal.sosfilt_zi(sos) * x[0]
            x, zi = signal.sosfilt(sos, x, zi=zi)
        if envelope:
            a = _hold(np.abs(x), decim)
            # Gate the noise out: the chunk's median is the hiss level (pulses are a tiny
            # share of the time), so whatever stands ENV_GATE x above it is kept and the rest
            # is silence. A sparse pulse train is then clicks in quiet, not clicks in hiss.
            fl = float(np.median(a)) * ENV_GATE
            if env_floor is None or fl < env_floor:
                env_floor = fl
            else:
                env_floor = 0.95 * env_floor + 0.05 * fl
            a = np.maximum(a - env_floor, 0.0)
        elif mode == "am":
            a = np.abs(x[::decim])
        elif plan["wide"]:
            xx = np.concatenate([[prev if prev is not None else x[0]], x])
            step = np.angle(xx[1:] * np.conj(xx[:-1]))
            prev = x[-1]
            a = np.pad(step, (0, (-len(step)) % decim)).reshape(-1, decim).mean(axis=1) * fs / (2 * np.pi * NFM_DEVIATION_HZ)
        else:
            z = x[::decim]
            zz = np.concatenate([[prev if prev is not None else z[0]], z])
            a = np.angle(zz[1:] * np.conj(zz[:-1])) * (fs / decim) / (2 * np.pi * NFM_DEVIATION_HZ)
            prev = z[-1]
        if not envelope:                                    # the gate already took the floor away;
            if not done:                                    # a DC filter there would turn a steady
                hp_zi = signal.lfilter_zi(hp_b, hp_a) * np.median(a)  # burst into two clicks
            a, hp_zi = signal.lfilter(hp_b, hp_a, a, zi=hp_zi)
        # Level: from the chunk's peak in envelope mode (sparse pulses would make any
        # percentile the noise), its 99.5th percentile otherwise. Down at once, up by at most
        # 5 % per chunk, so a quiet stretch between bursts is not turned up into hiss.
        p = float(np.max(np.abs(a)) if envelope else np.percentile(np.abs(a), 99.5)) + 1e-9
        target = (0.9 if envelope else 0.5) / p
        if envelope and env_floor:
            # never louder than full scale for something ENV_FULL x the noise floor, so a
            # stretch with no signal stays near silence instead of its noise filling the scale
            target = min(target, 0.9 / (ENV_FULL * env_floor))
        gain = target if gain is None else min(target, gain * 1.05)
        out = np.clip(a * gain, -1.0, 1.0)
        if not done:
            out[:fade] *= np.linspace(0.0, 1.0, len(out[:fade]))
        if left == k:
            out[-fade:] *= np.linspace(1.0, 0.0, len(out[-fade:]))
        yield (out * 32767).astype("<i2").tobytes()
        done += k
        left -= k


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
