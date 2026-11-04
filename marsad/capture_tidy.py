"""Clearing the IQ of captures graded empty out of the recorder pool. Nothing is deleted.

    marsad watch tidy                     the plan: every capture it would touch, and a token
    marsad watch tidy --confirm TOKEN     moves exactly that list
    marsad watch tidy --put-back          what could go back; add --confirm to do it

A move takes the `.sigmf-data` and `.sigmf-meta` of a capture to a holding folder outside the
pool (`recorder.set_aside`, default `recordings/set_aside/<date>/`), at the same path it had
in the pool, checked by size and sha256. The row's `iq_status` becomes "set aside" and
`set_aside_path` says where the data went. The row, the fingerprint and the characterise
reports stay. Each holding folder has a `set_aside.json` with every file's hash and the row's
old status, and is written before a file moves, so a stopped run can still be put back.
Deleting a holding folder is the owner's decision and this program never does it.

Chosen: effective grade "empty" (the re-measured grade when there is one, as on the Captures
tab), `iq_status` "written", both files in the pool. Held back, and said why: anything archived,
private, being written by a running recorder, or held by events filed "inside" it; anything
touching `recorder.timing_only_bands`; any survey in `recorder.tidy_exclude_surveys` or named
by --exclude-survey; anything not before --before.

The token is a hash of the row ids in the plan, so a confirm moves what was looked at: if a
watch or a re-measure changed the list since, the confirm is refused.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path

from . import capture as cap
from . import catalogue
from .capture_archive import _exports_of, _inside_capture, _sha, _writing_now
from .config import Station

SET_ASIDE = catalogue.SET_ASIDE
MANIFEST = "set_aside.json"
Refused = catalogue.Refused


def holding_root(station: Station, cfg: dict | None = None) -> Path:
    return catalogue.set_aside_dir(station, cfg or cap.settings(station))


def _meta_of(data: Path) -> Path:
    return data.with_name(data.name.replace(".sigmf-data", ".sigmf-meta"))


def _touches_timing_only(row: dict, meta_path: Path, cfg: dict) -> bool:
    """The capture's band from its meta, or the event's own band from its row."""
    try:
        m = json.loads(meta_path.read_text(encoding="utf-8"))
        if cap.timing_only(m["captures"][0]["core:frequency"], m["global"]["core:sample_rate"], cfg):
            return True
    except (OSError, ValueError, KeyError, IndexError):
        return True                      # cannot tell which band it is in: leave it alone
    f = row.get("target_hz") or row.get("freq_hz")
    return cap.timing_only(f, max(row.get("bandwidth_hz") or 0, 1.0), cfg)


def plan(station: Station, exclude_surveys=(), before: str | None = None) -> dict:
    """What a tidy would move. Reads only."""
    cfg = cap.settings(station)
    out = {"take": [], "held": [], "token": "", "bytes": 0, "excluded_surveys": [], "before": before}
    excluded = [*(cfg.get("tidy_exclude_surveys") or []), *exclude_surveys]
    out["excluded_surveys"] = excluded
    pool = cap.pool_dir(station, cfg).resolve()
    con = catalogue.open_ro(station)
    if con is None:
        return out
    busy = _writing_now(station, cfg)
    try:
        rows = con.execute("SELECT * FROM captures WHERE iq_status = 'written' AND iq_path IS NOT NULL "
                           "ORDER BY started_epoch").fetchall()
        for r in rows:
            raw = dict(r)
            raw.setdefault("target_hz", None)
            shown = catalogue._row(r, cfg)
            if shown.get("grade") != "empty":
                continue
            data = Path(raw["iq_path"])
            meta = _meta_of(data)
            size = catalogue._bytes(data) + catalogue._bytes(meta)
            item = {"id": raw["id"], "started": raw.get("started"), "mhz": round((raw.get("freq_hz") or 0) / 1e6, 4),
                    "survey": raw.get("survey"), "grade": shown["grade"], "family": shown.get("family"),
                    "family_old": shown.get("family_old"), "bytes": size, "data": str(data),
                    "exports": len(_exports_of(data)) if (data.parent / "exports").exists() else 0}
            why = None
            if shown["private"]:
                why = "private band"
            elif raw.get("archive_path"):
                why = "also in an archive zip"
            elif pool not in data.resolve().parents or not data.exists() or not meta.exists():
                why = "capture or its meta is not in the pool"
            elif data.name in busy:
                why = "a running recorder is writing it"
            elif _touches_timing_only(raw, meta, cfg):
                why = "touches recorder.timing_only_bands"
            elif raw.get("survey") and any(s in raw["survey"] for s in excluded):
                why = "survey excluded"
            elif before and (raw.get("started") or "")[:10] >= before:
                why = f"not before {before}"
            else:
                inside = _inside_capture(con, raw)
                if inside:
                    why = f"holds {len(inside)} event(s) filed inside it"
            if why:
                out["held"].append({**item, "why": why})
            else:
                out["take"].append(item)
    finally:
        con.close()
    out["bytes"] = sum(i["bytes"] for i in out["take"])
    out["token"] = token([i["id"] for i in out["take"]])
    out["holding"] = str(holding_root(station, cfg))
    return out


def token(ids: list) -> str:
    return hashlib.sha256(",".join(str(i) for i in sorted(ids)).encode()).hexdigest()[:8]


# ---------------------------------------------------------------- moving one file

def _same_volume(a: Path, b: Path) -> bool:
    return a.stat().st_dev == b.stat().st_dev


def _move_checked(src: Path, dst: Path, size: int, sha: str) -> None:
    """src to dst, checked by size and sha256 before the original is given up. On the same
    drive a rename; across drives a copy, checked, and only then the original removed."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        raise Refused(f"{dst} already exists; nothing overwritten")
    if _same_volume(src, dst.parent):
        os.replace(src, dst)
        if dst.stat().st_size != size or _sha(dst) != sha:
            os.replace(dst, src)
            raise Refused(f"{src.name} did not check out after the move; it was moved back")
        return
    part = dst.with_name(dst.name + ".part")
    shutil.copyfile(src, part)
    if part.stat().st_size != size or _sha(part) != sha:
        part.unlink()
        raise Refused(f"{src.name} did not check out after copying; the original is untouched")
    os.replace(part, dst)
    src.unlink()


def _load_manifest(folder: Path) -> dict:
    p = folder / MANIFEST
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"version": 1, "entries": []}


def _save_manifest(folder: Path, m: dict) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    part = folder / (MANIFEST + ".part")
    part.write_text(json.dumps(m, indent=1), encoding="utf-8")
    os.replace(part, folder / MANIFEST)


# ---------------------------------------------------------------- the move

def tidy(station: Station, confirm: str, exclude_surveys=(), before: str | None = None, say=print,
         today: str | None = None) -> dict:
    """Move what the plan lists, if `confirm` is that plan's token."""
    p = plan(station, exclude_surveys, before)
    if not p["take"]:
        raise Refused("nothing to set aside")
    if confirm != p["token"]:
        raise Refused(f"the token does not match the plan as it is now ({p['token']}): the list has changed "
                      "or the filters differ. Nothing was moved. Look at the plan again.")
    cfg = cap.settings(station)
    pool = cap.pool_dir(station, cfg).resolve()
    folder = holding_root(station, cfg) / (today or dt.date.today().isoformat())
    manifest = _load_manifest(folder)
    con = cap.connect(station)
    ensure_columns(con)
    done, failed, moved_bytes = [], [], 0
    try:
        for item in p["take"]:
            data = Path(item["data"]).resolve()
            files = []
            for src in (data, _meta_of(data)):
                rel = src.relative_to(pool).as_posix()
                files.append({"rel": rel, "bytes": src.stat().st_size, "sha256": _sha(src)})
            entry = {"id": item["id"], "old_status": "written", "state": "moving", "files": files,
                     "when": dt.datetime.now().isoformat(timespec="seconds")}
            manifest["entries"].append(entry)
            _save_manifest(folder, manifest)     # before anything moves: a stopped run can be put back
            moved = []
            try:
                for f in files:
                    _move_checked(pool / f["rel"], folder / f["rel"], f["bytes"], f["sha256"])
                    moved.append(f)
                con.execute("UPDATE captures SET iq_status = ?, set_aside_path = ? WHERE id = ? AND iq_status = 'written'",
                            (SET_ASIDE, str(folder / files[0]["rel"]), item["id"]))
                con.commit()
            except (OSError, Refused, sqlite3.Error) as exc:
                for f in moved:
                    os.replace(folder / f["rel"], pool / f["rel"])
                manifest["entries"].remove(entry)
                _save_manifest(folder, manifest)
                failed.append({"id": item["id"], "why": str(exc)})
                say(f"  #{item['id']}: left in the pool, {exc}")
                continue
            entry["state"] = "moved"
            _save_manifest(folder, manifest)
            done.append(item["id"])
            moved_bytes += sum(f["bytes"] for f in files)
    finally:
        con.close()
    return {"moved": done, "failed": failed, "bytes": moved_bytes, "folder": str(folder)}


def ensure_columns(con) -> None:
    have = {r[1] for r in con.execute("PRAGMA table_info(captures)")}
    if "set_aside_path" not in have:
        con.execute("ALTER TABLE captures ADD COLUMN set_aside_path TEXT")
        con.commit()


# ---------------------------------------------------------------- putting back

def _entries(station: Station, cfg: dict) -> list:
    """(holding folder, manifest, entry) for everything not yet put back, oldest folder first."""
    root = holding_root(station, cfg)
    out = []
    if root.exists():
        for folder in sorted(d for d in root.iterdir() if d.is_dir()):
            if (folder / MANIFEST).exists():
                m = _load_manifest(folder)
                out += [(folder, m, e) for e in m["entries"] if e.get("state") != "returned"]
    return out


def _back_state(folder: Path, pool: Path, e: dict) -> str | None:
    """Why this entry cannot go back as it stands, or None. Hashes every held file."""
    for f in e["files"]:
        held, home = folder / f["rel"], pool / f["rel"]
        if held.exists():
            if home.exists():
                return f"{home.name} is in the pool as well as the holding folder; nothing overwritten"
            if held.stat().st_size != f["bytes"] or _sha(held) != f["sha256"]:
                return f"{held.name} no longer matches its hash"
        elif not home.exists():
            return f"{held.name} is gone from {folder}"
    return None


def put_back(station: Station, ids=None, confirm: bool = False, say=print) -> dict:
    """Return set-aside captures to the pool and give the rows their old status. Without
    `confirm` it only checks and reports."""
    cfg = cap.settings(station)
    pool = cap.pool_dir(station, cfg).resolve()
    want = None if not ids else {int(i) for i in ids}
    ready, blocked, back = [], [], []
    for folder, m, e in _entries(station, cfg):
        if want is not None and e["id"] not in want:
            continue
        why = _back_state(folder, pool, e)
        if why:
            blocked.append({"id": e["id"], "why": why})
        else:
            ready.append((folder, m, e))
    out = {"ready": [e["id"] for _, _, e in ready], "blocked": blocked, "returned": back, "bytes": 0,
           "folders": sorted({str(f) for f, _, _ in ready})}
    out["bytes"] = sum(f["bytes"] for _, _, e in ready for f in e["files"])
    if not confirm or not ready:
        return out
    con = cap.connect(station)
    ensure_columns(con)
    try:
        for folder, m, e in ready:
            try:
                for f in e["files"]:
                    if (folder / f["rel"]).exists():
                        _move_checked(folder / f["rel"], pool / f["rel"], f["bytes"], f["sha256"])
                con.execute("UPDATE captures SET iq_status = ?, set_aside_path = NULL WHERE id = ? AND iq_status = ?",
                            (e.get("old_status") or "written", e["id"], SET_ASIDE))
                con.commit()
            except (OSError, Refused, sqlite3.Error) as exc:
                blocked.append({"id": e["id"], "why": str(exc)})
                say(f"  #{e['id']}: not put back, {exc}")
                continue
            e["state"] = "returned"
            e["returned"] = dt.datetime.now().isoformat(timespec="seconds")
            _save_manifest(folder, m)
            back.append(e["id"])
    finally:
        con.close()
    return out


# ---------------------------------------------------------------- the command

def _print_plan(p: dict) -> None:
    print(f"  {'row':>5}  {'started':19}  {'MHz':>10}  {'grade':6} {'MB':>7}  {'survey':52}  family")
    for i in p["take"]:
        fam = i["family"] or "-"
        if i["family_old"] and i["family_old"] != i["family"]:
            fam += f" (was {i['family_old']})"
        print(f"  {i['id']:5d}  {(i['started'] or '')[:19]:19}  {i['mhz']:10.4f}  {i['grade']:6} {i['bytes'] / 1e6:7.1f}  "
              f"{(i['survey'] or 'fixed watch')[:52]:52}  {fam}" + (f"  [{i['exports']} export(s) stay]" if i["exports"] else ""))
    print(f"\n  {len(p['take'])} captures, {p['bytes'] / 1e9:.2f} GB would leave the pool.")
    reasons: dict = {}
    for h in p["held"]:
        reasons.setdefault(h["why"], []).append(h)
    if reasons:
        print("  Also graded empty, and held back:")
        for why, hs in sorted(reasons.items()):
            print(f"    {len(hs):4d}, {sum(h['bytes'] for h in hs) / 1e9:.2f} GB: {why}  (rows {', '.join(str(h['id']) for h in hs)})")
    print(f"  Excluded surveys: {', '.join(p['excluded_surveys']) or 'none'}")


def main(station: Station, args) -> int:
    try:
        if args.put_back:
            out = put_back(station, [x for x in (args.ids or "").split(",") if x], args.confirm is not None)
            for b in out["blocked"]:
                print(f"  #{b['id']}: {b['why']}")
            if args.confirm is None:
                print(f"  {len(out['ready'])} captures, {out['bytes'] / 1e9:.2f} GB could go back to the pool. "
                      "Nothing was moved; add --confirm to do it.")
            else:
                print(f"  {len(out['returned'])} captures put back and checked; their rows have their old status.")
            return 1 if out["blocked"] else 0
        if args.confirm is None:
            p = plan(station, args.exclude_survey or (), args.before)
            if not p["take"]:
                print("  Nothing to set aside.")
            else:
                _print_plan(p)
                print(f"\n  Nothing was moved. To move this list, IQ files only, to {p['holding']}:")
                filt = "".join(f" --exclude-survey {s}" for s in args.exclude_survey or ()) + (f" --before {args.before}" if args.before else "")
                print(f"    marsad watch tidy --confirm {p['token']}{filt}")
            return 0
        out = tidy(station, args.confirm, args.exclude_survey or (), args.before)
    except Refused as exc:
        print(f"  {exc}")
        return 1
    print(f"  {len(out['moved'])} captures set aside and checked, {out['bytes'] / 1e9:.2f} GB left the pool; "
          f"{len(out['failed'])} left where they were.")
    print(f"  Holding folder: {out['folder']}")
    print("  Nothing was deleted. That folder is yours to delete when you are sure; "
          "until then `marsad watch tidy --put-back --confirm` returns them.")
    return 1 if out["failed"] else 0
