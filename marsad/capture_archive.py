"""Archiving captures out of the recorder pool (2026-10-30), for the Captures tab.

The pool has a cap (`recorder.pool_gb`), and a good capture should be kept for good
without counting against it. An archive action writes ONE zip into the archive folder
(`recorder.archive` in station.json, default `recordings/archive`, absolute paths allowed,
never inside the pool) holding the chosen files, a `manifest.json` (the catalogue rows,
a sha256 per file, chain / gain / serial, the owner's note) and a `README.txt`.

The order is the whole point, because this is the only path by which anything leaves
the pool:

1. `plan`     what would go: rows, files, bytes leaving the pool. Changes nothing.
2. `archive`  only with `confirm=True`: write `<name>.zip.part`, close it, re-open it and
              hash every member against the hash taken while writing, rename it, and only
              then remove the originals that the plan says leave the pool (each one first
              checked to be the same size and age as when it was zipped). If verification
              fails nothing in the pool is touched and the half-written zip is removed.
3. `restore`  unzip back into the pool, verifying the zip first and each file after;
              a file already in the pool with different content is a refusal, never an
              overwrite. The zip is kept: after a restore it is a second copy.

The rows stay in occupancy.db: `iq_status` becomes "archived" and `archive_path` names
the zip (the old status is in the manifest, for restore). The Captures tab still lists
them, with an ARCHIVED badge; a fingerprint left in the pool (the default) or read back
out of the zip stays viewable.

Selection, so that no event is left pointing at IQ that has gone: an event that owns a
capture always brings the events that fell inside that capture.

Private-tier events have no IQ by design. An archive of them carries their fingerprints
and rows only, and this module refuses to put any IQ-like file of a private row into a
zip even if a row claimed one. Exports (WAV, cf32) are regenerated from the SigMF capture,
so they go only when asked; left out, they stay in the pool.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import zipfile
from pathlib import Path

from . import capture as cap
from . import catalogue
from .config import Station

ARCHIVED = "archived"
SCOPES = ("event", "capture", "group")
CHUNK = 1 << 22                       # 4 MB per read while zipping and hashing
STORED = (".sigmf-data", ".wav", ".cf32", ".npz")   # already dense: deflate buys nothing and costs minutes
IQ_LIKE = (".sigmf-data", ".sigmf-meta", ".wav", ".cf32", ".cu8", ".cs16", ".raw", ".iq")

Refused = catalogue.Refused


# ---------------------------------------------------------------- where things live

def archive_dir(station: Station, cfg: dict | None = None) -> Path:
    cfg = cfg or cap.settings(station)
    p = Path(cfg.get("archive") or "recordings/archive")
    p = p if p.is_absolute() else station.root / p
    pool = cap.pool_dir(station, cfg).resolve()
    if p.resolve() == pool or pool in p.resolve().parents:
        raise Refused(f"recorder.archive ({p}) is inside the pool ({pool}): an archive there would still count "
                      "against pool_gb. Point it somewhere else in station.json.")
    return p


def ensure_columns(con: sqlite3.Connection) -> None:
    have = {r[1] for r in con.execute("PRAGMA table_info(captures)")}
    if "archive_path" not in have:
        con.execute("ALTER TABLE captures ADD COLUMN archive_path TEXT")
        con.commit()


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(CHUNK), b""):
            h.update(b)
    return h.hexdigest()


# ---------------------------------------------------------------- what would go

def _inside_capture(con, c: dict) -> list:
    """Events filed as 'inside an earlier capture' whose capture is c."""
    f = c.get("target_hz") or c.get("freq_hz")
    out = []
    for r in cap.near(con, f, 25e3, "iq_status = 'inside an earlier capture'"):
        r = dict(r)
        r.setdefault("target_hz", None)
        cc = catalogue.covering_capture(con, r)
        if cc is not None and cc["id"] == c["id"]:
            out.append(r)
    return out


def _rows_for(con, cfg: dict, event_id: int, scope: str) -> list:
    if scope not in SCOPES:
        raise Refused(f"unknown scope {scope!r} ({', '.join(SCOPES)})")
    row = catalogue._get(con, event_id)
    if scope == "event":
        rows = [row]
    elif scope == "capture":
        c = catalogue.covering_capture(con, row)
        if c is None:
            raise Refused("this event has no capture in the pool: choose 'this event only' or the signal group")
        rows = [dict(c)]
    else:
        everything = catalogue.rows(con, cfg)
        g = next((g for g in catalogue.group(everything) if any(r["id"] == row["id"] for r in g["rows"])), None)
        rows = [catalogue._get(con, r["id"]) for r in (g["rows"] if g else [row])]
    # an event that owns IQ brings the events inside it, so none is left pointing at nothing
    seen = {r["id"] for r in rows}
    for r in list(rows):
        if r.get("iq_status") == "written":
            for e in _inside_capture(con, r):
                if e["id"] not in seen:
                    seen.add(e["id"])
                    rows.append(e)
    return [r for r in rows if r.get("iq_status") != ARCHIVED]


def _exports_of(data: Path) -> list:
    ex = data.parent / "exports"
    if not ex.exists():
        return []
    stem = data.name.replace(".sigmf-data", "")
    try:
        wav = catalogue._wav_name(catalogue._meta(data), stem)
    except (OSError, ValueError, KeyError):
        wav = None
    return sorted(p for p in ex.iterdir() if p.is_file() and not p.name.endswith(".part")
                  and (stem in p.name or p.name == wav))


def _writing_now(station: Station, cfg: dict) -> set:
    """Data files a running recorder is writing at this moment (from its status file)."""
    busy = set()
    for r in catalogue.live_recorders(station, cfg):
        c = r.get("capture") if r.get("running") else None
        if c and c.get("file"):
            busy.add(c["file"])
    return busy


def plan(station: Station, event_id: int, scope: str = "event", include_exports: bool = False,
         keep_fingerprints: bool = True) -> dict:
    """What an archive action would do. Reads only."""
    cfg = cap.settings(station)
    pool = cap.pool_dir(station, cfg).resolve()
    adir = archive_dir(station, cfg)
    con = catalogue.open_ro(station)
    if con is None:
        raise Refused("there is no captures catalogue yet")
    try:
        rows = _rows_for(con, cfg, event_id, scope)
    finally:
        con.close()
    if not rows:
        raise Refused("everything chosen is already archived")
    busy = _writing_now(station, cfg)
    files, notes, seen = [], [], set()

    def add(path, kind, leaves, row):
        if not path:
            return
        try:
            p = catalogue._inside(path, station, cfg)
        except Refused as exc:
            notes.append(f"event {row['id']}: {kind} skipped, {exc}")
            return
        if p in seen:
            return
        if catalogue.is_private(row, cfg) and p.name.endswith(IQ_LIKE):
            notes.append(f"event {row['id']}: private band, {p.name} NOT archived (no IQ for private bands)")
            return
        if p.name in busy:
            raise Refused(f"{p.name} is being written by a running recorder right now; archive it after the capture ends")
        seen.add(p)
        files.append({"path": str(p), "rel": p.relative_to(pool).as_posix(), "bytes": p.stat().st_size,
                      "kind": kind, "row": row["id"], "leaves_pool": leaves})

    for r in rows:
        add(r.get("fingerprint"), "fingerprint", not keep_fingerprints, r)
        if r.get("iq_status") != "written" or catalogue.is_private(r, cfg):
            continue
        try:
            data = catalogue._inside(r.get("iq_path"), station, cfg)
        except Refused as exc:
            notes.append(f"event {r['id']}: IQ not archived, {exc}")
            continue
        meta = data.with_name(data.name.replace(".sigmf-data", ".sigmf-meta"))
        if not meta.exists():
            raise Refused(f"{data.name} has no .sigmf-meta yet: the capture is still being written")
        add(data, "sigmf-data", True, r)
        add(meta, "sigmf-meta", True, r)
        if r.get("characterised"):
            md = Path(r["characterised"])
            add(md, "characterise", True, r)
            add(md.with_suffix(".json"), "characterise", True, r)
        ex = _exports_of(data)
        if include_exports:
            for p in ex:
                add(p, "export", True, r)
        elif ex:
            notes.append(f"{len(ex)} export(s) of {data.name} stay in the pool (not chosen; they can be "
                         "made again from the SigMF capture)")
    first = min(rows, key=lambda r: r.get("started_epoch") or 0)
    leaving = sum(f["bytes"] for f in files if f["leaves_pool"])
    return {"scope": scope, "rows": [r["id"] for r in rows], "events": len(rows),
            "captures": sum(1 for r in rows if r.get("iq_status") == "written"),
            "private": sum(1 for r in rows if catalogue.is_private(r, cfg)),
            "f_mhz": round((first.get("target_hz") or first.get("freq_hz") or 0) / 1e6, 4),
            "files": files, "bytes_in_zip": sum(f["bytes"] for f in files), "bytes_leaving_pool": leaving,
            "include_exports": bool(include_exports), "keep_fingerprints": bool(keep_fingerprints),
            "archive_dir": str(adir), "notes": notes}


# ---------------------------------------------------------------- doing it

def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", (s or "").strip())[:40].strip("-")


def _readme(manifest: dict) -> str:
    lines = [
        "marsad capture archive",
        "",
        f"Made {manifest['created_utc']} (UTC) from the recorder pool {manifest['pool']}.",
        f"{manifest['events']} catalogue event(s), {manifest['captures']} IQ capture(s), {len(manifest['files'])} file(s).",
        "",
        "Note: " + (manifest["note"] or "(none)"),
        "",
        "manifest.json holds the catalogue rows exactly as they were (occupancy.db, table captures),",
        "a sha256 for every file, and where in the pool each file lived ('rel').",
        "",
        "*.sigmf-data + *.sigmf-meta: complex int16 IQ (SigMF), the master copy of each capture.",
        "*.characterise.md / .json: what `marsad characterise` measured in it.",
        "fingerprints/*.fp.npz: 20 ms x ~1 kHz power spectrogram of each event (numpy).",
        "exports/: WAV for SDR++ or cf32, if they were chosen.",
        "",
        "To put it back: the Captures tab's Restore button, which checks every hash.",
        "Private-band events carry fingerprints and rows only; no IQ of a private band is ever kept.",
    ]
    return "\r\n".join(lines) + "\r\n"


def verify(zip_path: Path, manifest: dict | None = None) -> list:
    """Re-read every member and match its sha256 to the manifest (the one inside the zip
    when none is given). Returns a list of problems; empty means sound."""
    problems = []
    try:
        with zipfile.ZipFile(zip_path) as z:
            if manifest is None:
                manifest = json.loads(z.read("manifest.json"))
            names = set(z.namelist())
            for f in manifest["files"]:
                if f["arcname"] not in names:
                    problems.append(f"{f['arcname']} missing from the zip")
                    continue
                h, n = hashlib.sha256(), 0
                with z.open(f["arcname"]) as fh:           # zipfile also checks each member's CRC
                    for b in iter(lambda: fh.read(CHUNK), b""):
                        h.update(b)
                        n += len(b)
                if h.hexdigest() != f["sha256"] or n != f["bytes"]:
                    problems.append(f"{f['arcname']} does not match its hash")
    except (OSError, zipfile.BadZipFile, KeyError, ValueError, RuntimeError) as exc:
        problems.append(f"cannot read the zip: {exc}")
    return problems


def archive(station: Station, event_id: int, scope: str = "event", include_exports: bool = False,
            keep_fingerprints: bool = True, note: str = "", label: str = "", confirm: bool = False,
            _verify=verify) -> dict:
    """Zip, verify, and only then take the chosen files out of the pool."""
    p = plan(station, event_id, scope, include_exports, keep_fingerprints)
    if not confirm:
        raise Refused("not confirmed: nothing was archived")
    cfg = cap.settings(station)
    pool = cap.pool_dir(station, cfg).resolve()
    adir = archive_dir(station, cfg)
    adir.mkdir(parents=True, exist_ok=True)
    con = cap.connect(station)
    ensure_columns(con)
    try:
        rows = [dict(con.execute("SELECT * FROM captures WHERE id = ?", (i,)).fetchone()) for i in p["rows"]]
        now = dt.datetime.now(dt.timezone.utc)
        name = f"arch_{p['f_mhz']:.4f}MHz_{now:%Y%m%dT%H%M%SZ}" + (f"_{_slug(label)}" if _slug(label) else "") + ".zip"
        final = adir / name
        if final.exists():
            raise Refused(f"{final.name} already exists; try again in a second")
        part = final.with_name(final.name + ".part")
        stats, entries = {}, []
        try:
            with zipfile.ZipFile(part, "w", allowZip64=True) as z:
                for f in p["files"]:
                    src = Path(f["path"])
                    st = src.stat()
                    stats[f["path"]] = (st.st_size, st.st_mtime_ns)
                    comp = zipfile.ZIP_STORED if src.name.endswith(STORED) else zipfile.ZIP_DEFLATED
                    zi = zipfile.ZipInfo(f["rel"], date_time=dt.datetime.fromtimestamp(st.st_mtime).timetuple()[:6])
                    zi.compress_type = comp
                    h = hashlib.sha256()
                    with open(src, "rb") as fin, z.open(zi, "w", force_zip64=True) as fout:
                        for b in iter(lambda: fin.read(CHUNK), b""):
                            h.update(b)
                            fout.write(b)
                    entries.append({"arcname": f["rel"], "rel": f["rel"], "bytes": st.st_size, "sha256": h.hexdigest(),
                                    "kind": f["kind"], "row": f["row"], "removed_from_pool": f["leaves_pool"]})
                manifest = {
                    "format": "marsad capture archive 1", "created_utc": now.isoformat(timespec="seconds"),
                    "note": note or "", "label": label or "", "scope": scope, "pool": str(pool),
                    "events": p["events"], "captures": p["captures"], "files": entries,
                    "rows": rows, "receivers": sorted({f"{r.get('receiver')} SN {r.get('serial')}" for r in rows}),
                    "gains_db": sorted({r.get("gain_db") for r in rows if r.get("gain_db") is not None}),
                    "chains": sorted({r.get("chain") for r in rows if r.get("chain")}),
                    "notes": p["notes"]}
                z.writestr("manifest.json", json.dumps(manifest, indent=1, default=str), zipfile.ZIP_DEFLATED)
                z.writestr("README.txt", _readme(manifest), zipfile.ZIP_DEFLATED)
            problems = _verify(part, manifest)
            if problems:
                raise Refused("the archive did not verify, so nothing was taken out of the pool: " + "; ".join(problems))
            os.replace(part, final)
        except BaseException:
            if part.exists():
                part.unlink()        # our own half-written zip, never anything in the pool
            raise
        removed, kept = 0, []
        for e, f in zip(entries, p["files"]):
            if not e["removed_from_pool"]:
                continue
            src = Path(f["path"])
            st = src.stat()
            if (st.st_size, st.st_mtime_ns) != stats[f["path"]]:
                kept.append(f"{src.name} changed while it was being archived; left in the pool")
                continue
            src.unlink()
            removed += e["bytes"]
        con.executemany("UPDATE captures SET iq_status = ?, archive_path = ? WHERE id = ?",
                        [(ARCHIVED, str(final), i) for i in p["rows"]])
        con.commit()
    finally:
        con.close()
    catalogue._POOL_CACHE["t"] = 0.0
    return {"zip": str(final), "zip_bytes": final.stat().st_size, "events": p["events"], "captures": p["captures"],
            "bytes_freed": removed, "verified": True, "notes": p["notes"] + kept}


# ---------------------------------------------------------------- putting it back

def _zip_of(station: Station, cfg: dict, path: str) -> Path:
    adir = archive_dir(station, cfg).resolve()
    z = Path(path).resolve()
    if adir not in z.parents:
        raise Refused(f"{z.name} is not in the archive folder ({adir})")
    if not z.exists():
        raise Refused(f"{z.name} is no longer in the archive folder")
    return z


def restore(station: Station, event_id: int) -> dict:
    """Unzip an archived event's whole archive back into the pool and give the rows their
    old status back. Verifies the zip first; never overwrites a different file."""
    cfg = cap.settings(station)
    pool = cap.pool_dir(station, cfg).resolve()
    con = cap.connect(station)
    ensure_columns(con)
    try:
        r = con.execute("SELECT archive_path FROM captures WHERE id = ?", (int(event_id),)).fetchone()
        if r is None or not r["archive_path"]:
            raise Refused(f"event {event_id} is not archived")
        zpath = _zip_of(station, cfg, r["archive_path"])
        problems = verify(zpath)
        if problems:
            raise Refused("the archive does not verify, so nothing was restored: " + "; ".join(problems))
        with zipfile.ZipFile(zpath) as z:
            manifest = json.loads(z.read("manifest.json"))
            todo = []
            for f in manifest["files"]:
                target = (pool / f["rel"]).resolve()
                if pool not in target.parents:
                    raise Refused(f"{f['rel']} would land outside the pool")
                if target.exists():
                    if target.stat().st_size == f["bytes"] and _sha(target) == f["sha256"]:
                        continue
                    raise Refused(f"{f['rel']} is already in the pool with different content; nothing was restored")
                todo.append((f, target))
            put = 0
            for f, target in todo:
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_name(target.name + ".part")
                h = hashlib.sha256()
                with z.open(f["arcname"]) as fin, open(tmp, "wb") as fout:
                    for b in iter(lambda: fin.read(CHUNK), b""):
                        h.update(b)
                        fout.write(b)
                if h.hexdigest() != f["sha256"]:
                    tmp.unlink()
                    raise Refused(f"{f['rel']} came out of the zip wrong; stopped")
                os.replace(tmp, target)
                put += f["bytes"]
        old = {r["id"]: r.get("iq_status") for r in manifest["rows"]}
        con.executemany("UPDATE captures SET iq_status = ? WHERE id = ? AND iq_status = ?",
                        [(st, i, ARCHIVED) for i, st in old.items()])
        con.commit()
    finally:
        con.close()
    catalogue._POOL_CACHE["t"] = 0.0
    return {"zip": str(zpath), "events": len(old), "bytes_restored": put,
            "note": "restored; the zip stays in the archive folder as a second copy"}


# ---------------------------------------------------------------- reading from a zip

def member(station: Station, zip_path: str, pool_path: str) -> bytes:
    """One small file (a fingerprint) read back out of an archive, by its old pool path."""
    cfg = cap.settings(station)
    z = _zip_of(station, cfg, zip_path)
    rel = Path(pool_path).resolve().relative_to(cap.pool_dir(station, cfg).resolve()).as_posix()
    with zipfile.ZipFile(z) as zf:
        return zf.read(rel)


def describe(station: Station, zip_path: str) -> dict:
    """The archive an archived event lives in: name, size, note, when."""
    cfg = cap.settings(station)
    try:
        z = _zip_of(station, cfg, zip_path)
        with zipfile.ZipFile(z) as zf:
            m = json.loads(zf.read("manifest.json"))
        return {"file": z.name, "path": str(z), "bytes": z.stat().st_size, "note": m.get("note"),
                "created_utc": m.get("created_utc"), "events": m.get("events"), "captures": m.get("captures")}
    except (Refused, OSError, KeyError, ValueError, zipfile.BadZipFile) as exc:
        return {"file": Path(zip_path).name, "path": zip_path, "error": str(exc)}


def size_gb(station: Station, cfg: dict) -> float:
    try:
        return cap.folder_bytes(archive_dir(station, cfg)) / 1e9
    except Refused:
        return 0.0

