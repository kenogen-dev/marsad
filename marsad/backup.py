"""Back a station up to another drive, and restore it.

    marsad backup export <dest> [--include settings,surveys,...] [--include-location] [--dry-run]
    marsad backup list <file>
    marsad backup verify <file>
    marsad backup import <file> [--into DIR] [--dry-run]

An export COPIES: nothing in the station is removed or changed. It writes one zip (zip64,
so tens of GB are fine) holding the chosen groups and a manifest.json with every file's
group, path, size and sha256:

    settings    station.json, chain.json, ROOM_RF.md and any other file of yours in the
                station folder, but not program files, rtl-sdr, a venv, build, dist or git
    surveys     surveys/
    reports     reports/
    db          occupancy.db, through sqlite's backup API, so a recorder writing to it
                cannot leave a torn copy
    captures    the recorder pool
    archive     the capture archive
    recordings  anything else in recordings/

The order, as for capture archives: write `<name>.zip.part`, re-open it and hash every
member against the hash taken while writing, and only then rename it. A failed check
removes the half-written zip and nothing else.

station.local.json (your location) stays out unless asked for, and the manifest says which.
Private bands have no raw IQ by design: a file that looks like IQ and belongs to a private
row or band is left out and named, so an export can never create one.

An import verifies the whole zip first, then restores each file through a `.part` with its
hash checked. A file already there with different content is a refusal, listed, before
anything is written. Importing into another folder rewrites the file paths in the restored
occupancy.db so the Captures tab finds its files there; the backup itself is never changed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import zipfile
from pathlib import Path

from . import __version__, capture as cap, catalogue, host
from .capture_archive import IQ_LIKE, archive_dir
from .config import LOCAL_FILE, Station

FORMAT = 1
MANIFEST = "manifest.json"
CHUNK = 1 << 22
GROUPS = {
    "settings": "settings and notes",
    "surveys": "surveys",
    "reports": "reports",
    "db": "occupancy.db",
    "captures": "captures pool",
    "archive": "capture archive",
    "recordings": "other recordings",
}
STORED = (".sigmf-data", ".wav", ".cf32", ".npz", ".zip", ".png", ".jpg", ".cu8", ".cs16", ".raw", ".iq")
FAT32_LIMIT = 4 * 1024 ** 3 - 1
# Never part of "settings and notes", at any depth or at the top of the station folder.
SKIP_ANYWHERE = {"__pycache__", ".pytest_cache", ".venv", "venv", "env", "node_modules"}
SKIP_TOP = {"surveys", "reports", "recordings", "rtl-sdr", "build", "dist"}
SKIP_FILES = {"occupancy.db", "occupancy.db-wal", "occupancy.db-shm", "occupancy.db-journal", LOCAL_FILE}
# What a program folder holds when there is no git to ask.
PROGRAM_TOP = {"marsad", "tests", "tools", "packaging", ".github", "_internal", "marsad.exe", "Start marsad.cmd",
               "README.md", "LICENSE", "pyproject.toml", "pytest.ini", "requirements.txt", ".gitignore",
               ".gitattributes"}


class Refused(ValueError):
    """Said in a sentence, nothing written."""


def human(n: float) -> str:
    for unit, size in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("kB", 1024)):
        if n >= size:
            return f"{n / size:.1f} {unit}"
    return f"{int(n)} B"


def _sha(path: Path, progress=None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(CHUNK), b""):
            h.update(b)
            if progress:
                progress(len(b))
    return h.hexdigest()


# ---------------------------------------------------------------- what is in a station

def _tracked(root: Path) -> set | None:
    """Files the program's own git tracks here, or None when this is not a git checkout."""
    if not (root / ".git").exists():
        return None
    try:
        r = subprocess.run(["git", "ls-files"], cwd=str(root), capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return set(r.stdout.splitlines()) if r.returncode == 0 else None


def _walk(top: Path):
    for folder, dirs, names in os.walk(top):
        dirs.sort()
        for n in sorted(names):
            yield Path(folder) / n


def _settings_files(station: Station, include_location: bool) -> list:
    root = station.root
    tracked = _tracked(root)
    out = []
    for folder, dirs, names in os.walk(root):
        here = Path(folder)
        top = here == root
        dirs[:] = sorted(d for d in dirs if d not in SKIP_ANYWHERE and not d.startswith(".git")
                         and not d.endswith(".egg-info") and not (top and d in SKIP_TOP)
                         and not (top and tracked is None and d in PROGRAM_TOP))
        for n in sorted(names):
            p = here / n
            rel = p.relative_to(root).as_posix()
            if n.endswith((".part", ".pyc")) or (top and n in SKIP_FILES):
                continue
            if tracked is not None and rel in tracked:
                continue
            if tracked is None and top and n in PROGRAM_TOP:
                continue
            out.append(p)
    local = root / LOCAL_FILE
    if include_location and local.exists():
        out.append(local)
    return out


_NAME_MHZ = re.compile(r"^cap_(\d+(?:\.\d+)?)MHz_")
_NAME_HZ = re.compile(r"baseband_(\d+)Hz")


def _private_names(station: Station, cfg: dict) -> set:
    """File names of IQ-like files that private rows claim, if any row ever did."""
    names = set()
    if not station.db.exists():
        return names
    try:
        con = sqlite3.connect(f"file:{station.db.as_posix()}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        try:
            for r in con.execute("SELECT * FROM captures WHERE iq_path IS NOT NULL AND iq_path != ''"):
                if catalogue.is_private(dict(r), cfg):
                    p = Path(str(r["iq_path"]).replace("\\", "/"))
                    names.update({p.name, p.with_suffix(".sigmf-meta").name, p.with_suffix(".sigmf-data").name})
        finally:
            con.close()
    except sqlite3.Error:
        pass
    return names


def _private_iq(name: str, private_names: set, cfg: dict) -> bool:
    """True for a file that is IQ-like and belongs to a private row or a private band."""
    if not name.lower().endswith(IQ_LIKE):
        return False
    if name in private_names:
        return True
    m = _NAME_MHZ.search(name)
    if m:
        return cap.band_tier(float(m.group(1)) * 1e6, cfg)["tier"] == "private"
    m = _NAME_HZ.search(name)
    return bool(m) and cap.band_tier(float(m.group(1)), cfg)["tier"] == "private"


def collect(station: Station, groups: list, include_location: bool = False) -> dict:
    """{group: [{"src", "rel", "bytes"}]} and what was left out, and why. Reads only."""
    cfg = cap.settings(station)
    pool = cap.pool_dir(station, cfg)
    try:
        arch = archive_dir(station, cfg)
    except catalogue.Refused:
        arch = station.recordings / "archive"
    busy = set()
    try:
        from .capture_archive import _writing_now
        busy = _writing_now(station, cfg)
    except (OSError, ValueError):
        pass
    private = _private_names(station, cfg)
    files: dict = {g: [] for g in groups}
    skipped: list = []

    def add(group, p: Path, base: Path):
        name = p.name
        if name.endswith(".part"):
            return
        if _private_iq(name, private, cfg):
            skipped.append({"path": str(p), "why": "private band: raw IQ is never exported"})
            return
        try:
            size = p.stat().st_size
        except OSError as exc:
            skipped.append({"path": str(p), "why": f"cannot read it: {exc}"})
            return
        files[group].append({"src": p, "rel": p.relative_to(base).as_posix(), "bytes": size})

    for g in groups:
        if g == "settings":
            for p in _settings_files(station, include_location):
                add(g, p, station.root)
        elif g == "surveys" and station.surveys.exists():
            for p in _walk(station.surveys):
                add(g, p, station.surveys)
        elif g == "reports" and station.reports.exists():
            for p in _walk(station.reports):
                add(g, p, station.reports)
        elif g == "db" and station.db.exists():
            add(g, station.db, station.root)
        elif g == "captures" and pool.exists():
            for p in _walk(pool):
                if p.name.startswith("recorder_status_") and p.suffix == ".json":
                    continue                     # a running recorder's status: means nothing later
                if p.name in busy or p.with_suffix(".sigmf-data").name in busy:
                    skipped.append({"path": str(p), "why": "a running recorder is writing this capture now"})
                    continue
                if p.suffix == ".sigmf-data" and not p.with_suffix(".sigmf-meta").exists():
                    skipped.append({"path": str(p), "why": "capture not finished (no .sigmf-meta beside it)"})
                    continue
                add(g, p, pool)
        elif g == "archive" and arch.exists():
            for p in _walk(arch):
                add(g, p, arch)
        elif g == "recordings" and station.recordings.exists():
            keep_out = [pool.resolve(), arch.resolve()]
            for p in _walk(station.recordings):
                r = p.resolve()
                if any(k == r or k in r.parents for k in keep_out):
                    continue
                add(g, p, station.recordings)
    return {"files": files, "skipped": skipped, "pool": pool, "archive": arch}


def _parse_groups(include) -> list:
    if not include:
        return list(GROUPS)
    if isinstance(include, str):
        include = [include]
    out = []
    for item in include:
        for g in str(item).split(","):
            g = g.strip()
            if not g:
                continue
            if g not in GROUPS:
                raise Refused(f"unknown group {g!r}; the groups are {', '.join(GROUPS)}")
            if g not in out:
                out.append(g)
    if not out:
        raise Refused(f"nothing chosen; the groups are {', '.join(GROUPS)}")
    return out


# ---------------------------------------------------------------- planning an export

def _existing_parent(p: Path) -> Path | None:
    p = p.resolve()
    for q in (p, *p.parents):
        if q.exists():
            return q
    return None


def destination(dest, now: dt.datetime | None = None) -> Path:
    """The zip an export would write: `dest` itself if it names a .zip, else a dated name in it."""
    if not str(dest or "").strip():
        raise Refused("say where the backup goes: a folder on another drive")
    d = Path(str(dest).strip().strip('"'))
    if not d.is_absolute():
        d = Path.cwd() / d
    if d.suffix.lower() == ".zip":
        return d
    stamp = (now or dt.datetime.now()).strftime("%Y%m%d-%H%M")
    p = d / f"marsad-backup_{stamp}.zip"
    n = 2
    while p.exists() or Path(str(p) + ".part").exists():
        p = d / f"marsad-backup_{stamp}_{n}.zip"
        n += 1
    return p


def _refuse_inside(station: Station, target: Path, found: dict) -> None:
    t = target.resolve()
    for label, base in (("the station folder", station.root), ("the captures pool", found["pool"]),
                        ("the capture archive", found["archive"])):
        b = Path(base).resolve()
        if b == t or b in t.parents:
            raise Refused(f"{t.parent} is inside {label} ({b}). A backup must go somewhere else, or it would "
                          "copy itself and be lost with the station.")


def _activity_warnings(station: Station) -> list:
    out = []
    try:
        running = [r for r in catalogue.live_recorders(station) if r.get("running")]
    except (OSError, ValueError):
        running = []
    if running:
        out.append(f"{len(running)} recorder(s) running: captures being written are left out, and the newest "
                   "files will be incomplete. Best run a backup with nothing recording.")
    try:
        progs = host.running_sdr_programs() or []
    except OSError:
        progs = []
    if progs and not running:
        out.append(f"{', '.join(progs)} running: a scan in progress makes its newest files incomplete.")
    return out


def plan(station: Station, dest, include=None, include_location: bool = False) -> dict:
    """What an export would write, with sizes and free space. Writes nothing."""
    groups = _parse_groups(include)
    target = destination(dest)
    found = collect(station, groups, include_location)
    _refuse_inside(station, target, found)
    rows = [{"group": g, "label": GROUPS[g], "files": len(found["files"][g]),
             "bytes": sum(f["bytes"] for f in found["files"][g])} for g in groups]
    total = sum(r["bytes"] for r in rows)
    warnings = _activity_warnings(station)
    parent = _existing_parent(target.parent)
    free = None
    if parent is None:
        warnings.append(f"{target.parent}: that drive is not there.")
    else:
        try:
            free = shutil.disk_usage(parent).free
        except OSError as exc:
            warnings.append(f"cannot read the free space on {parent}: {exc}")
        same = host.same_physical_drive(station.root, parent)
        if same is True:
            warnings.append("That destination is on the SAME physical drive as the station. It protects against "
                            "a mistake, not against a drive failure.")
        elif same is None:
            warnings.append("Could not tell whether that destination is on the same physical drive as the station.")
        fs = host.filesystem_name(parent)
        if fs and fs.upper() in ("FAT32", "FAT", "VFAT") and total > FAT32_LIMIT:
            raise Refused(f"{parent} is {fs}, which cannot hold a file over 4 GB, and this backup is up to "
                          f"{human(total)}. Choose fewer groups or an exFAT or NTFS drive.")
    if free is not None and total > free:
        raise Refused(f"This backup is up to {human(total)} and {target.parent} has {human(free)} free. "
                      "Choose fewer groups or a bigger drive.")
    location_file = (station.root / LOCAL_FILE).exists()
    return {"file": str(target), "groups": rows, "total_bytes": total, "free_bytes": free,
            "location_included": bool(include_location and location_file),
            "location_exists": location_file, "skipped": found["skipped"], "warnings": warnings,
            "total_h": human(total), "free_h": human(free) if free is not None else "unknown"}


# ---------------------------------------------------------------- export

def _copy_db(src: Path, tmp: Path) -> None:
    """A consistent copy of a database that a recorder may be writing to."""
    con = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True, timeout=30)
    try:
        out = sqlite3.connect(str(tmp))
        try:
            con.backup(out)
        finally:
            out.close()
    finally:
        con.close()


def _member(z: zipfile.ZipFile, name: str, src: Path, mtime: float, progress) -> tuple:
    """Stream one file into the zip, hashing what was actually read. Returns (bytes, sha256)."""
    when = dt.datetime.fromtimestamp(max(mtime, 315532800)).timetuple()[:6]
    zi = zipfile.ZipInfo(name, date_time=when)
    zi.compress_type = zipfile.ZIP_STORED if src.suffix.lower() in STORED else zipfile.ZIP_DEFLATED
    h = hashlib.sha256()
    n = 0
    with open(src, "rb") as fh, z.open(zi, "w", force_zip64=True) as out:
        for b in iter(lambda: fh.read(CHUNK), b""):
            out.write(b)
            h.update(b)
            n += len(b)
            if progress:
                progress(len(b))
    return n, h.hexdigest()


def _hash_members(z: zipfile.ZipFile, entries: list, progress=None) -> list:
    """Every manifest entry against the zip. Returns [(member, why)] for each that fails."""
    bad = []
    names = set(z.namelist())
    for e in entries:
        m = e["member"]
        if m not in names:
            bad.append((m, "missing from the zip"))
            continue
        h = hashlib.sha256()
        n = 0
        try:
            with z.open(m) as fh:
                for b in iter(lambda: fh.read(CHUNK), b""):
                    h.update(b)
                    n += len(b)
                    if progress:
                        progress(len(b))
        except (zipfile.BadZipFile, OSError, ValueError, EOFError) as exc:   # includes a CRC failure
            bad.append((m, f"cannot be read back: {exc}"))
            continue
        if n != e["bytes"] or h.hexdigest() != e["sha256"]:
            bad.append((m, f"hash does not match ({n} bytes read, {e['bytes']} expected)"))
    return bad


def export(station: Station, dest, include=None, include_location: bool = False, progress=None) -> dict:
    """Write and verify the zip. `progress(done, total, label)` is called as it goes."""
    p = plan(station, dest, include, include_location)
    target = Path(p["file"])
    if target.exists():
        raise Refused(f"{target} already exists; a backup never overwrites one. Choose another name or folder.")
    groups = [r["group"] for r in p["groups"]]
    found = collect(station, groups, include_location)
    total = sum(f["bytes"] for g in groups for f in found["files"][g])
    # writing and re-reading are both counted, so the bar runs once from 0 to 100
    done = [0]

    def tick(label):
        def f(n):
            done[0] += n
            if progress:
                progress(done[0], 2 * total, label)
        return f

    target.parent.mkdir(parents=True, exist_ok=True)
    part = Path(str(target) + ".part")
    tmp_db = target.parent / (target.stem + ".db.tmp")
    entries, skipped, changed = [], list(found["skipped"]), []
    try:
        with zipfile.ZipFile(part, "w", allowZip64=True) as z:
            for g in groups:
                for f in found["files"][g]:
                    src, member = f["src"], f"{g}/{f['rel']}"
                    try:
                        st0 = src.stat()
                        if g == "db":
                            _copy_db(src, tmp_db)
                            n, sha = _member(z, member, tmp_db, st0.st_mtime, tick(member))
                        else:
                            n, sha = _member(z, member, src, st0.st_mtime, tick(member))
                            st1 = src.stat()
                            if (st1.st_size, st1.st_mtime_ns) != (st0.st_size, st0.st_mtime_ns):
                                changed.append(member)
                    except (OSError, sqlite3.Error) as exc:
                        if isinstance(exc, OSError) and exc.errno == 28:     # disk full: not a per-file problem
                            raise
                        skipped.append({"path": str(src), "why": f"could not be read: {exc}"})
                        continue
                    entries.append({"group": g, "path": f["rel"], "member": member, "bytes": n,
                                    "sha256": sha, "mtime": st0.st_mtime})
            manifest = {
                "format": FORMAT, "marsad_version": __version__,
                "created": dt.datetime.now().isoformat(timespec="seconds"),
                "paths": {"root": str(station.root.resolve()), "pool": str(Path(found["pool"]).resolve()),
                          "archive": str(Path(found["archive"]).resolve())},
                "groups": {g: {"label": GROUPS[g],
                               "files": sum(1 for e in entries if e["group"] == g),
                               "bytes": sum(e["bytes"] for e in entries if e["group"] == g)} for g in groups},
                "location_included": p["location_included"],
                "location_note": ("station.local.json is in this backup." if p["location_included"]
                                  else "station.local.json (the location) is NOT in this backup."),
                "skipped": skipped, "changed_while_copying": changed, "warnings": p["warnings"],
                "files": entries,
            }
            z.writestr(MANIFEST, json.dumps(manifest, indent=1))
        with zipfile.ZipFile(part) as z:
            bad = _hash_members(z, entries, tick("checking the zip"))
        if bad:
            raise Refused(f"the written zip failed its check, so it was removed: {bad[0][0]}: {bad[0][1]}")
        os.replace(part, target)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    finally:
        tmp_db.unlink(missing_ok=True)
    return {"file": str(target), "bytes": target.stat().st_size, "bytes_h": human(target.stat().st_size),
            "files": len(entries), "groups": manifest["groups"], "skipped": skipped, "changed": changed,
            "warnings": p["warnings"], "location_included": p["location_included"], "verified": True}


# ---------------------------------------------------------------- reading a backup

def read_manifest(path) -> dict:
    p = Path(str(path).strip().strip('"'))
    if not p.exists():
        raise Refused(f"{p} is not there")
    try:
        with zipfile.ZipFile(p) as z:
            return json.loads(z.read(MANIFEST).decode("utf-8"))
    except KeyError:
        raise Refused(f"{p.name} is a zip but not a marsad backup: it has no {MANIFEST}")
    except (zipfile.BadZipFile, ValueError, OSError) as exc:
        raise Refused(f"{p.name} cannot be read as a backup: {exc}")


def listing(path) -> dict:
    m = read_manifest(path)
    return {k: m.get(k) for k in ("format", "marsad_version", "created", "groups", "location_included",
                                  "location_note", "skipped", "changed_while_copying", "warnings", "paths")}


def verify(path, progress=None) -> dict:
    """Every member's hash against the manifest. Reads only."""
    m = read_manifest(path)
    total = sum(e["bytes"] for e in m["files"])
    done = [0]

    def tick(n):
        done[0] += n
        if progress:
            progress(done[0], total, "checking the zip")

    with zipfile.ZipFile(Path(str(path).strip().strip('"'))) as z:
        bad = _hash_members(z, m["files"], tick)
        known = {e["member"] for e in m["files"]} | {MANIFEST}
        extra = [n for n in z.namelist() if n not in known]
    return {"ok": not bad and not extra, "files": len(m["files"]), "bytes": total, "bytes_h": human(total),
            "bad": [{"member": a, "why": b} for a, b in bad], "extra": extra,
            "marsad_version": m.get("marsad_version"), "created": m.get("created")}


def _version(text: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", str(text))[:3])


# ---------------------------------------------------------------- import

def _safe_rel(rel: str) -> str:
    parts = Path(rel).parts
    if Path(rel).is_absolute() or ".." in parts or re.match(r"^[A-Za-z]:", rel):
        raise Refused(f"the backup names a path outside its station: {rel!r}. Nothing was restored.")
    return rel


def _targets(m: dict, z: zipfile.ZipFile, into: Path) -> tuple:
    """Where each member goes, using the backup's own station.json for the pool and archive."""
    data = {}
    if "settings/station.json" in z.namelist():
        try:
            data = json.loads(z.read("settings/station.json").decode("utf-8"))
        except ValueError:
            data = {}
    elif (into / "station.json").exists():
        data = Station.load(into).data
    st = Station(into, {"recorder": (data.get("recorder") or {}) if isinstance(data, dict) else {}}, True)
    cfg = cap.settings(st)
    pool = cap.pool_dir(st, cfg)
    try:
        arch = archive_dir(st, cfg)
    except catalogue.Refused:
        arch = st.recordings / "archive"
    base = {"settings": into, "surveys": st.surveys, "reports": st.reports, "db": into,
            "captures": pool, "archive": arch, "recordings": st.recordings}
    out = []
    for e in m["files"]:
        if e["group"] not in base:
            raise Refused(f"the backup has a group this version does not know: {e['group']!r}")
        out.append((e, base[e["group"]] / _safe_rel(e["path"])))
    return out, pool, arch


def import_plan(path, into) -> dict:
    """What an import would restore and what stands in its way. Writes nothing."""
    m = read_manifest(path)
    if m.get("format", 0) > FORMAT or _version(m.get("marsad_version", "0")) > _version(__version__):
        raise Refused(f"This backup was made by marsad {m.get('marsad_version')} and this is {__version__}: "
                      "install that version or newer to restore it.")
    into = Path(str(into)).resolve()
    with zipfile.ZipFile(Path(str(path).strip().strip('"'))) as z:
        targets, pool, arch = _targets(m, z, into)
    write, same, conflicts = [], 0, []
    for e, t in targets:
        if t.exists():
            if t.stat().st_size == e["bytes"] and _sha(t) == e["sha256"]:
                same += 1
            else:
                conflicts.append(str(t))
        else:
            write.append((e, t))
    need = sum(e["bytes"] for e, _ in write)
    parent = _existing_parent(into)
    free = shutil.disk_usage(parent).free if parent else None
    return {"into": str(into), "write": len(write), "already_there": same, "conflicts": conflicts,
            "bytes": need, "bytes_h": human(need), "free_bytes": free,
            "pool": str(pool), "archive": str(arch), "location_note": m.get("location_note", ""),
            "created": m.get("created"), "marsad_version": m.get("marsad_version"), "_write": write}


def _rewrite_paths(db: Path, old: dict, new: dict) -> int:
    """The catalogue stores full file paths. Point them at where the files now are."""
    con = sqlite3.connect(str(db))
    n = 0
    try:
        have = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='captures'").fetchone()
        if not have:
            return 0
        cols = [r[1] for r in con.execute("PRAGMA table_info(captures)") if r[2].upper() == "TEXT"]
        pairs = []
        for k in ("pool", "archive", "root"):                    # the longer, more particular paths first
            a, b = str(old.get(k) or ""), str(new[k])
            if a and a != b:
                pairs += [(a, b), (a.replace("\\", "/"), b.replace("\\", "/"))]
        for a, b in pairs:
            for c in cols:
                n += con.execute(f"UPDATE captures SET {c} = replace({c}, ?, ?) WHERE {c} LIKE ?",
                                 (a, b, f"{a}%")).rowcount
        con.commit()
    finally:
        con.close()
    return n


def import_backup(path, into, progress=None) -> dict:
    """Verify, refuse on any conflict, then restore."""
    path = Path(str(path).strip().strip('"'))
    p = import_plan(path, into)
    if p["conflicts"]:
        shown = "; ".join(p["conflicts"][:12]) + (f"; and {len(p['conflicts']) - 12} more" if len(p["conflicts"]) > 12 else "")
        raise Refused(f"{len(p['conflicts'])} file(s) already there differ from the backup, and an import never "
                      f"overwrites. Nothing was restored. {shown}")
    if p["free_bytes"] is not None and p["bytes"] > p["free_bytes"]:
        raise Refused(f"This restore needs {human(p['bytes'])} and {p['into']} has {human(p['free_bytes'])} free.")
    v = verify(path, progress)
    if not v["ok"]:
        first = v["bad"][0]["member"] if v["bad"] else v["extra"][0]
        raise Refused(f"The backup failed verification ({len(v['bad'])} bad, {len(v['extra'])} unexpected, first "
                      f"{first}). Nothing was restored.")
    m = read_manifest(path)
    into_p = Path(p["into"])
    write = sorted(p["_write"], key=lambda et: et[0]["group"] == "db")     # the database last
    db_restored = False
    with zipfile.ZipFile(path) as z:
        for e, t in write:
            t.parent.mkdir(parents=True, exist_ok=True)
            part = Path(str(t) + ".part")
            h = hashlib.sha256()
            try:
                with z.open(e["member"]) as src, open(part, "wb") as out:
                    for b in iter(lambda: src.read(CHUNK), b""):
                        out.write(b)
                        h.update(b)
                if h.hexdigest() != e["sha256"]:
                    raise Refused(f"{e['member']} did not restore with the right hash; stopped there.")
                os.replace(part, t)
            finally:
                part.unlink(missing_ok=True)
            if e.get("mtime"):
                os.utime(t, (e["mtime"], e["mtime"]))
            db_restored = db_restored or e["group"] == "db"
    rewritten = 0
    if db_restored:
        rewritten = _rewrite_paths(into_p / "occupancy.db", m.get("paths") or {},
                                   {"root": str(into_p), "pool": p["pool"], "archive": p["archive"]})
    return {"into": p["into"], "restored": len(write), "already_there": p["already_there"], "bytes_h": p["bytes_h"],
            "paths_rewritten": rewritten, "location_note": p["location_note"]}


# ---------------------------------------------------------------- one job at a time, for the web page

JOB: dict = {"state": "idle"}
_JOB_LOCK = threading.Lock()


def _run_job(kind: str, fn) -> dict:
    with _JOB_LOCK:
        if JOB.get("state") == "running":
            raise Refused(f"a backup {JOB.get('kind')} is already running")
        JOB.clear()
        JOB.update({"state": "running", "kind": kind, "done": 0, "total": 0, "label": ""})

    def progress(done, total, label):
        JOB.update({"done": done, "total": total, "label": label})

    def work():
        try:
            JOB["result"] = fn(progress)
            JOB["state"] = "done"
        except (Refused, OSError, sqlite3.Error, zipfile.BadZipFile) as exc:
            JOB["error"] = str(exc)
            JOB["state"] = "failed"

    threading.Thread(target=work, daemon=True, name="backup").start()
    return dict(JOB)


def start_export(station: Station, form: dict) -> dict:
    if form.get("confirm") is not True:
        raise Refused("an export needs confirm: true")
    dest, groups, loc = form.get("dest"), form.get("groups"), form.get("include_location") is True
    plan(station, dest, groups, loc)                  # refuse now, in the page, not in a thread
    return _run_job("export", lambda pr: export(station, dest, groups, loc, pr))


def start_verify(form: dict) -> dict:
    file = form.get("file")
    read_manifest(file)
    return _run_job("verify", lambda pr: verify(file, pr))


# ---------------------------------------------------------------- command line

def _bar(done, total, label, last=[-1]):
    pct = int(100 * done / total) if total else 100
    if pct != last[0] and pct % 5 == 0:
        print(f"  {pct:3d}%  {label[:70]}", flush=True)
    last[0] = pct


def _print_plan(p: dict) -> None:
    print(f"  backup file   {p['file']}")
    for r in p["groups"]:
        print(f"    {r['label']:<20} {r['files']:>7} files  {human(r['bytes']):>10}")
    print(f"  total         up to {p['total_h']} (surveys and reports compress)   free there: {p['free_h']}")
    print("  location      " + ("station.local.json is included" if p["location_included"] else
                                "station.local.json is NOT included (--include-location adds it)" if p["location_exists"]
                                else "no station.local.json here"))
    for s in p["skipped"]:
        print(f"  left out      {s['path']}: {s['why']}")
    for w in p["warnings"]:
        print(f"  WARNING       {w}")


def main(args: list, station: Station, prog: str = "marsad backup") -> int:
    ap = argparse.ArgumentParser(prog=prog, description="Copy this station to a zip on another drive, and restore one.")
    sub = ap.add_subparsers(dest="what", required=True)
    e = sub.add_parser("export", help="write a verified zip; nothing in the station changes")
    e.add_argument("dest", help="a folder on another drive, or a .zip name")
    e.add_argument("--include", action="append", help=f"groups, comma separated (default all): {', '.join(GROUPS)}")
    e.add_argument("--include-location", action="store_true", help="also take station.local.json")
    e.add_argument("--dry-run", action="store_true", help="show sizes and warnings, write nothing")
    li = sub.add_parser("list", help="what a backup holds")
    li.add_argument("file")
    v = sub.add_parser("verify", help="check every file in a backup against its hash")
    v.add_argument("file")
    i = sub.add_parser("import", help="restore a backup; never overwrites a file that differs")
    i.add_argument("file")
    i.add_argument("--into", help="the folder to restore into (default the current folder)")
    i.add_argument("--dry-run", action="store_true", help="show what would be restored, write nothing")
    a = ap.parse_args(args)
    try:
        if a.what == "export":
            p = plan(station, a.dest, a.include, a.include_location)
            _print_plan(p)
            if a.dry_run:
                print("  --dry-run: nothing written.")
                return 0
            r = export(station, a.dest, a.include, a.include_location, _bar)
            print(f"\n  wrote and verified {r['file']}: {r['files']} files, {r['bytes_h']}")
            for c in r["changed"]:
                print(f"  changed while being copied (the copy may be incomplete): {c}")
            print("  Check it again on the other drive at any time with `marsad backup verify <file>`.")
        elif a.what == "list":
            m = listing(a.file)
            print(f"  made {m['created']} by marsad {m['marsad_version']}")
            for g in (m["groups"] or {}).values():
                print(f"    {g['label']:<20} {g['files']:>7} files  {human(g['bytes']):>10}")
            print(f"  {m['location_note']}")
            for s in m.get("skipped") or []:
                print(f"  left out when made: {s['path']}: {s['why']}")
            for c in m.get("changed_while_copying") or []:
                print(f"  changed while being copied: {c}")
        elif a.what == "verify":
            r = verify(a.file, _bar)
            if r["ok"]:
                print(f"  OK: {r['files']} files, {r['bytes_h']}, every hash matches.")
                return 0
            for b in r["bad"]:
                print(f"  BAD  {b['member']}: {b['why']}")
            for x in r["extra"]:
                print(f"  NOT IN THE MANIFEST  {x}")
            print(f"  FAILED: {len(r['bad'])} of {r['files']} files bad.")
            return 1
        else:
            into = a.into or os.getcwd()
            p = import_plan(a.file, into)
            print(f"  restore into  {p['into']}")
            print(f"  to write      {p['write']} files, {p['bytes_h']}; already there and identical: {p['already_there']}")
            print(f"  captures to   {p['pool']}\n  archive to    {p['archive']}\n  {p['location_note']}")
            if a.dry_run:
                for c in p["conflicts"]:
                    print(f"  DIFFERS, would refuse: {c}")
                print("  --dry-run: nothing written.")
                return 1 if p["conflicts"] else 0
            r = import_backup(a.file, into, _bar)
            print(f"\n  restored {r['restored']} files, {r['bytes_h']}. Start it with: marsad --station \"{r['into']}\"")
    except Refused as exc:
        print(f"  {exc}")
        return 1
    except OSError as exc:
        print(f"  {type(exc).__name__}: {exc}")
        return 1
    return 0
