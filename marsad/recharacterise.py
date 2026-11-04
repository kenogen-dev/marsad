"""Measure older captures again with the current characteriser. It only adds files.

Each capture gets <stem>.characterise.v2.md and .json: beside the capture in the pool, or for
a capture inside an archive zip in recharacterised/<zip name>/ beside that zip. An index in the
pool, recharacterised_v2.json, lets the Captures tab and `marsad watch report` show the new
figures on top of the old row. Catalogue rows, old reports, captures and zips are never written.

An archive zip stores its captures uncompressed, so a capture is read where it sits, through a
read-only map of the zip file, after its CRC and its manifest's sha256 have been checked.

SNR before and after is not comparable: the characteriser's floor changed.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import struct
import sys
import zipfile
from pathlib import Path

from . import capture as cap
from . import catalogue as cat
from . import characterise as ch
from .config import Station

SUFFIX = ".characterise.v2"
CHUNK = 1 << 24


def low_priority() -> None:
    """Let anything else on the PC, a recorder above all, go first."""
    if sys.platform == "win32":
        BELOW_NORMAL = 0x4000
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), BELOW_NORMAL)
    else:
        os.nice(10)


def open_member(zip_path: Path, data_name: str) -> tuple:
    """(source, meta, how) for a capture inside an archive zip, read in place. Refuses one
    that is compressed or does not match its CRC and manifest."""
    with zipfile.ZipFile(zip_path) as z:
        names = {Path(n).name: n for n in z.namelist()}
        arc = names.get(data_name)
        meta_arc = names.get(data_name.replace(".sigmf-data", ".sigmf-meta"))
        if not arc or not meta_arc:
            raise ValueError(f"{data_name} or its meta is not in {zip_path.name}")
        info = z.getinfo(arc)
        if info.compress_type != zipfile.ZIP_STORED:
            raise ValueError(f"{data_name} is compressed in {zip_path.name}, so it cannot be read in place")
        meta = json.loads(z.read(meta_arc))
        want = {f["arcname"]: f["sha256"] for f in json.loads(z.read("manifest.json"))["files"]}.get(arc)
        h = hashlib.sha256()
        with z.open(arc) as fh:                    # zipfile checks the CRC as this reaches the end
            head = fh.read(4096)
            h.update(head)
            for b in iter(lambda: fh.read(CHUNK), b""):
                h.update(b)
    if not want or h.hexdigest() != want:
        raise ValueError(f"{data_name} does not match the hash in {zip_path.name}'s manifest")
    with open(zip_path, "rb") as f:
        f.seek(info.header_offset)
        local = f.read(30)
        if local[:4] != b"PK\x03\x04":
            raise ValueError(f"no local header for {data_name} in {zip_path.name}")
        offset = info.header_offset + 30 + sum(struct.unpack("<HH", local[26:30]))
        f.seek(offset)
        if f.read(len(head)) != head:
            raise ValueError(f"{data_name}: the bytes at its offset are not the member's")
    src = ch.sigmf_source(meta, zip_path, offset=offset, n=info.file_size // 4, name=data_name)
    return src, meta, {"read": "in place from the archive zip, read-only", "zip": zip_path.name, "member": arc,
                       "crc_ok": True, "sha256_ok": True}


def find_work(station: Station, cfg: dict, survey=None, ids=None) -> tuple:
    """(to do, passed over). Reads only."""
    con = cat.open_ro(station)
    if con is None:
        return [], []
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM captures WHERE iq_path IS NOT NULL AND iq_path != '' "
            "AND iq_status IN ('written', 'archived') ORDER BY id")]
    finally:
        con.close()
    from .capture_archive import _writing_now
    busy = _writing_now(station, cfg)
    todo, passed = [], []
    for r in rows:
        if (ids and r["id"] not in ids) or (survey and survey not in (r.get("survey") or "")):
            continue
        data = Path(r["iq_path"])
        if cat.is_private(r, cfg):
            passed.append((r["id"], "private band: never analysed"))
        elif data.name in busy:
            passed.append((r["id"], "still being written"))
        elif r["iq_status"] == "archived":
            zp = Path(r.get("archive_path") or "")
            if not zp.is_file():
                passed.append((r["id"], f"archive zip not found: {zp}"))
            else:
                todo.append((r, zp))
        elif not data.exists() or not data.with_name(data.name.replace(".sigmf-data", ".sigmf-meta")).exists():
            passed.append((r["id"], "capture or its meta is not in the pool"))
        else:
            todo.append((r, None))
    return todo, passed


def report_paths(row: dict, zip_path: Path | None) -> tuple:
    stem = ch.out_stem(Path(row["iq_path"]))
    folder = Path(row["iq_path"]).parent if zip_path is None else zip_path.parent / "recharacterised" / zip_path.stem
    return folder / f"{stem}{SUFFIX}.md", folder / f"{stem}{SUFFIX}.json"


def measure(row: dict, zip_path: Path | None, cfg: dict) -> dict:
    """Measure one capture and write its new report. Returns its index entry."""
    md, js = report_paths(row, zip_path)
    data = Path(row["iq_path"])
    if zip_path is None:
        src, how = ch.open_iq(data), {"read": "in place from the pool, read-only"}
        meta = json.loads(data.with_name(data.name.replace(".sigmf-data", ".sigmf-meta")).read_text())
        label = data
    else:
        src, meta, how = open_member(zip_path, data.name)
        label = zip_path / how["member"]
    if "marsad:cut_cutoff" in meta.get("global", {}):
        return {}                                  # made by the current recorder: nothing older to redo
    only = cap.timing_only(src.f0, src.fs, cfg)
    res = ch.characterise(label, src=src, timing_only=only)
    snr = cap.capture_snr(res)
    entry = {"id": row["id"], "version": 2, "family": res.get("family"),
             "capture_snr_db": None if snr is None else round(snr, 2),
             "grade": cap.grade_for(snr if snr is not None else row.get("snr_peak_db"), res.get("family")),
             "timing": ch.timing_signature(res), "timing_only": only, "report": str(md),
             "old": {"family": row.get("family"), "report": row.get("characterised"),
                     "capture_snr_db": row.get("capture_snr_db") if row.get("capture_snr_db") is not None
                     else cap.characterised_snr(row.get("characterised"))},
             "source": how, "snr_note": "not comparable with the old report's SNR"}
    md.parent.mkdir(parents=True, exist_ok=True)
    text = (f"> Re-measured with the current characteriser. {how['read'].capitalize()}. The old report is kept. "
            f"SNR here is on a new scale, not comparable with it. The old report said: {row.get('family') or 'nothing'}.\n\n"
            + ch.to_markdown(res))
    part = md.with_name(md.name + ".part")
    part.write_text(text, encoding="utf-8")
    os.replace(part, md)
    part = js.with_name(js.name + ".part")          # the .json last: it marks the report as finished
    part.write_text(json.dumps({"remeasured": entry, "result": ch._clean(res)}, indent=2), encoding="utf-8")
    os.replace(part, js)
    return entry


def save_index(cfg: dict, rows: dict) -> None:
    p = Path(cfg["_remeasured_index"])
    part = p.with_name(p.name + ".part")
    part.write_text(json.dumps({"version": 2, "rows": rows}, indent=1), encoding="utf-8")
    os.replace(part, p)


def run(station: Station, survey=None, ids=None, limit=None, say=print) -> dict:
    cfg = cap.settings(station)
    todo, passed = find_work(station, cfg, survey, ids)
    rows = dict(cap.remeasured_index(cfg))
    done, failed, current = 0, [], 0
    for row, zp in todo:
        if limit is not None and done >= limit:
            break
        md, js = report_paths(row, zp)
        if js.exists():                              # already done: a stopped run picks up where it was
            rows.setdefault(str(row["id"]), json.loads(js.read_text(encoding="utf-8"))["remeasured"])
            continue
        try:
            entry = measure(row, zp, cfg)
        except (Exception, SystemExit) as exc:       # one bad capture does not stop the rest
            failed.append((row["id"], str(exc)))
            say(f"  #{row['id']}: not measured, {exc}")
            continue
        if not entry:
            current += 1
            continue
        rows[str(row["id"])] = entry
        save_index(cfg, rows)
        done += 1
        say(f"  #{row['id']} {row['freq_hz'] / 1e6:.3f} MHz: {row.get('family') or 'nothing'} -> {entry['family']}")
    if rows:
        save_index(cfg, rows)
    return {"done": done, "failed": failed, "passed": passed, "current": current, "rows": rows, "todo": len(todo)}


def main(station: Station, survey=None, ids=None, limit=None, dry_run: bool = False) -> int:
    if dry_run:
        cfg = cap.settings(station)
        todo, passed = find_work(station, cfg, survey, ids)
        print(f"  would look at {len(todo)} captures, {sum(1 for _, z in todo if z)} of them inside archive zips; "
              f"{len(passed)} passed over")
        for i, why in passed:
            print(f"    #{i}: {why}")
        return 0
    low_priority()
    out = run(station, survey, ids, limit)
    print(f"  {out['done']} measured, {out['current']} already current, {len(out['failed'])} failed, "
          f"{len(out['passed'])} passed over. Nothing was overwritten.")
    return 1 if out["failed"] else 0
