"""Station backup (marsad/backup.py): a round trip on a synthetic station, and every refusal.
No radio. The station comes from conftest's filled template: one open signal with a
capture, one private signal with fingerprints only.
"""
import filecmp
import hashlib
import json
import os
import sqlite3
import sys
import zipfile
from pathlib import Path

import pytest

from conftest import ROOT, copy_station

sys.path.insert(0, str(ROOT))
from marsad import backup as B, capture as cap, capture_archive as A, catalogue as C, host  # noqa: E402
from marsad.config import Station  # noqa: E402


def snapshot(root: Path) -> dict:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            st = p.stat()
            out[p.relative_to(root).as_posix()] = (st.st_size, st.st_mtime_ns,
                                                   hashlib.sha256(p.read_bytes()).hexdigest())
    return out


@pytest.fixture
def st(filled_template, tmp_path):
    s = copy_station(filled_template, tmp_path / "station")
    root = s.root
    (root / "station.json").write_text(json.dumps({"presets": {}, "recorder": {"pool_gb": 7}}), encoding="utf-8")
    (root / "chain.json").write_text('{"schema": 1, "current": null, "chains": {}}', encoding="utf-8")
    (root / "ROOM_RF.md").write_text("# room\n", encoding="utf-8")
    (root / "notes.md").write_text("my notes\n", encoding="utf-8")
    (root / "my_tests").mkdir()
    (root / "my_tests" / "t.py").write_text("x = 1\n", encoding="utf-8")
    (root / "station.local.json").write_text('{"location": {"lat": 1.0, "lon": 2.0}}', encoding="utf-8")
    for junk in ("rtl-sdr", ".venv", "dist", "build", ".git-other", "marsad", "tools"):
        (root / junk).mkdir(exist_ok=True)
        (root / junk / "program.bin").write_bytes(b"program")
    (root / "marsad.exe").write_bytes(b"MZ")
    (root / "surveys").mkdir(exist_ok=True)
    (root / "surveys" / "survey_x_1h_20310115-0000.csv").write_text("2031-01-15, 00:00:00, 1, 2, 1, 1, -40\n")
    (root / "surveys" / "survey_x_1h_20310115-0000.json").write_text('{"receiver": "r820t"}')
    (root / "reports" / "survey_x_1h_20310115-0000").mkdir(parents=True)
    (root / "reports" / "survey_x_1h_20310115-0000" / "report.md").write_text("# report\n")
    (root / "recordings" / "other.txt").write_text("loose file\n")
    return Station.load(root)


def export_all(st, tmp_path, **kw):
    return B.export(st, tmp_path / "ext", **kw)


def members(file) -> list:
    with zipfile.ZipFile(file) as z:
        return z.namelist()


def test_round_trip_is_byte_identical_and_opens_as_a_station(st, tmp_path):
    before = snapshot(st.root)
    r = export_all(st, tmp_path)
    assert r["verified"] and Path(r["file"]).suffix == ".zip" and not Path(r["file"] + ".part").exists()
    assert snapshot(st.root) == before                               # the sources are untouched
    assert B.verify(r["file"])["ok"]

    into = tmp_path / "restored"
    out = B.import_backup(r["file"], into)
    assert out["restored"] == r["files"] and out["paths_rewritten"] > 0
    new = snapshot(into)
    for rel, (_, _, sha) in new.items():
        if rel != "occupancy.db":
            assert before[rel][2] == sha, rel                        # byte for byte
    wanted = {k for k in before if not k.startswith(("rtl-sdr/", ".venv/", "dist/", "build/", ".git-other/", "marsad/", "tools/"))
              and k not in ("station.local.json", "marsad.exe") and "recorder_status_" not in k}
    assert set(new) == wanted

    # it opens: same rows, the fingerprint and the IQ read back from their new home
    s2 = Station.load(into)
    assert cap.settings(s2)["pool_gb"] == 7
    sum1, sum2 = C.summary(st), C.summary(s2)
    assert sum1["total"] == sum2["total"] and sum2["total"] > 0
    con = sqlite3.connect(s2.db)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT id, iq_path, iq_status FROM captures").fetchall()
    con.close()
    written = [r for r in rows if r["iq_status"] == "written"]
    assert written and all(Path(r["iq_path"]).is_relative_to(into) for r in written)
    assert C.fingerprint_view(s2, rows[0]["id"])
    assert C.iq_view(s2, written[0]["id"])


def test_the_database_in_the_zip_is_identical_row_for_row(st, tmp_path):
    r = export_all(st, tmp_path)
    keep = tmp_path / "keep.db"
    with zipfile.ZipFile(r["file"]) as z:
        keep.write_bytes(z.read("db/occupancy.db"))
    dump = lambda p: list(sqlite3.connect(p).iterdump())
    assert dump(keep) == dump(st.db)


def test_a_corrupted_member_fails_verify_and_import(st, tmp_path):
    r = export_all(st, tmp_path)
    raw = bytearray(Path(r["file"]).read_bytes())
    with zipfile.ZipFile(r["file"]) as z:
        info = next(i for i in z.infolist() if i.filename.endswith(".sigmf-data"))
        start = info.header_offset + 30 + len(info.filename) + len(info.extra)
    raw[start + info.file_size // 2] ^= 0xFF
    Path(r["file"]).write_bytes(raw)
    v = B.verify(r["file"])
    assert not v["ok"] and v["bad"][0]["member"].endswith(".sigmf-data")
    into = tmp_path / "x"
    with pytest.raises(B.Refused, match="failed verification"):
        B.import_backup(r["file"], into)
    assert not into.exists()


def test_location_is_left_out_unless_asked(st, tmp_path):
    r = export_all(st, tmp_path)
    assert "settings/station.local.json" not in members(r["file"])
    assert B.listing(r["file"])["location_included"] is False
    assert "NOT in this backup" in B.listing(r["file"])["location_note"]
    r2 = B.export(st, tmp_path / "ext2", include_location=True)
    assert "settings/station.local.json" in members(r2["file"])
    assert B.listing(r2["file"])["location_included"] is True


def test_overwrite_is_refused_and_lists_the_files(st, tmp_path):
    r = export_all(st, tmp_path)
    into = tmp_path / "restored"
    (into).mkdir()
    (into / "notes.md").write_text("different\n", encoding="utf-8")
    with pytest.raises(B.Refused, match=r"notes\.md"):
        B.import_backup(r["file"], into)
    assert sorted(p.name for p in into.rglob("*")) == ["notes.md"]      # nothing else was restored
    assert (into / "notes.md").read_text(encoding="utf-8") == "different\n"


def test_importing_twice_skips_what_is_identical(st, tmp_path):
    r = B.export(st, tmp_path / "ext", include=["settings", "surveys"])
    into = tmp_path / "restored"
    B.import_backup(r["file"], into)
    again = B.import_backup(r["file"], into)
    assert again["restored"] == 0 and again["already_there"] == r["files"]


def test_destination_inside_the_station_is_refused(st):
    with pytest.raises(B.Refused, match="inside the station folder"):
        B.plan(st, st.root / "backups")
    with pytest.raises(B.Refused, match="inside"):
        B.export(st, st.root / "recordings" / "b.zip")
    assert not (st.root / "backups").exists()


def test_same_drive_warns_and_another_does_not(st, tmp_path, monkeypatch):
    monkeypatch.setattr(host, "same_physical_drive", lambda a, b: True)
    assert any("SAME physical drive" in w for w in B.plan(st, tmp_path / "ext")["warnings"])
    monkeypatch.setattr(host, "same_physical_drive", lambda a, b: False)
    assert not any("physical drive" in w for w in B.plan(st, tmp_path / "ext")["warnings"])


def test_not_enough_space_and_fat32_are_refused(st, tmp_path, monkeypatch):
    import shutil
    from collections import namedtuple
    monkeypatch.setattr(shutil, "disk_usage", lambda p: namedtuple("u", "total used free")(10, 9, 1))
    with pytest.raises(B.Refused, match="free"):
        B.plan(st, tmp_path / "ext")
    monkeypatch.undo()
    monkeypatch.setattr(host, "filesystem_name", lambda p: "FAT32")
    monkeypatch.setattr(B, "FAT32_LIMIT", 10)
    with pytest.raises(B.Refused, match="4 GB"):
        B.plan(st, tmp_path / "ext")


def test_a_private_row_produces_no_iq_like_file(st, tmp_path):
    cfg = cap.settings(st)
    con = sqlite3.connect(st.db)
    con.row_factory = sqlite3.Row
    private = [dict(r) for r in con.execute("SELECT * FROM captures") if C.is_private(dict(r), cfg)]
    assert private
    # even if something had left IQ for a private row or band in the pool, it stays out
    day = next(p for p in cap.pool_dir(st, cfg).iterdir() if p.is_dir())
    planted = day / "cap_880.5000MHz_20310115-000000_r820t.sigmf-data"
    planted.write_bytes(b"\0" * 64)
    planted.with_suffix(".sigmf-meta").write_text("{}")
    claimed = day / "claimed.sigmf-data"
    claimed.write_bytes(b"\0" * 64)
    claimed.with_suffix(".sigmf-meta").write_text("{}")
    con.execute("UPDATE captures SET iq_path = ? WHERE id = ?", (str(claimed), private[0]["id"]))
    con.commit()
    con.close()
    r = export_all(st, tmp_path)
    names = members(r["file"])
    assert not [n for n in names if "880.5" in n and n.endswith(A.IQ_LIKE)]
    assert not [n for n in names if "claimed" in n]
    assert any("private band" in s["why"] for s in r["skipped"])
    assert [n for n in names if n.endswith(".fp.npz")]                  # its fingerprints do go
    # and the same for a loose recording in a private band
    (st.root / "recordings" / "baseband_880500000Hz_12-00-00_01-01-2031.wav").write_bytes(b"RIFF")
    r2 = B.export(st, tmp_path / "ext2", include=["recordings"])
    assert not [n for n in members(r2["file"]) if n.endswith(".wav")]


def test_a_capture_being_written_is_skipped_and_named(st, tmp_path, monkeypatch):
    monkeypatch.setattr(C, "STALE_S", 3600)         # a slow machine must not age the status file out
    cfg = cap.settings(st)
    pool = cap.pool_dir(st, cfg)
    day = next(p for p in pool.iterdir() if p.is_dir())
    (day / "cap_433.9000MHz_20310115-000000_r820t.sigmf-data").write_bytes(b"\0" * 64)      # no meta yet
    live = day / "cap_433.9100MHz_20310115-000001_r820t.sigmf-data"
    live.write_bytes(b"\0" * 64)
    live.with_suffix(".sigmf-meta").write_text("{}")
    import time
    (pool / "recorder_status_1.json").write_text(json.dumps(
        {"state": "running", "updated": time.time(), "capture": {"file": live.name}}))
    p = B.plan(st, tmp_path / "ext")
    why = {Path(s["path"]).name: s["why"] for s in p["skipped"]}
    assert "not finished" in why["cap_433.9000MHz_20310115-000000_r820t.sigmf-data"]
    assert "writing" in why[live.name]
    assert any("recorder(s) running" in w for w in p["warnings"])
    names = members(B.export(st, tmp_path / "ext")["file"])
    assert not [n for n in names if "433.9000" in n or "433.9100" in n or "recorder_status" in n]


def test_a_backup_from_a_newer_version_is_refused(st, tmp_path, monkeypatch):
    r = export_all(st, tmp_path)
    monkeypatch.setattr(B, "__version__", "0.0.1")
    with pytest.raises(B.Refused, match="install that version"):
        B.import_backup(r["file"], tmp_path / "restored")


def test_a_path_out_of_the_station_is_refused(st, tmp_path):
    bad = tmp_path / "evil.zip"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("settings/../../x.txt", "x")
        z.writestr("manifest.json", json.dumps({"format": 1, "marsad_version": "0.0.1", "files": [
            {"group": "settings", "path": "../../x.txt", "member": "settings/../../x.txt", "bytes": 1,
             "sha256": hashlib.sha256(b"x").hexdigest()}]}))
    with pytest.raises(B.Refused, match="outside its station"):
        B.import_backup(bad, tmp_path / "restored")


def test_group_choice_and_unknown_groups(st, tmp_path):
    r = B.export(st, tmp_path / "ext", include="settings,db")
    assert {n.split("/")[0] for n in members(r["file"])} == {"settings", "db", "manifest.json"}
    with pytest.raises(B.Refused, match="unknown group"):
        B.plan(st, tmp_path / "ext", include="everything")


def test_a_file_that_is_not_a_backup_says_so(tmp_path):
    p = tmp_path / "a.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("x", "y")
    with pytest.raises(B.Refused, match="not a marsad backup"):
        B.verify(p)


def test_a_failed_export_leaves_no_part_file(st, tmp_path, monkeypatch):
    real = B._hash_members
    monkeypatch.setattr(B, "_hash_members", lambda z, e, p=None: [("x", "bad")])
    with pytest.raises(B.Refused, match="failed its check"):
        export_all(st, tmp_path)
    assert list((tmp_path / "ext").glob("*")) == []
    monkeypatch.setattr(B, "_hash_members", real)


def test_command_line(st, tmp_path, capsys):
    dest = tmp_path / "ext"
    assert B.main(["export", str(dest), "--dry-run"], st) == 0
    assert "NOT included" in capsys.readouterr().out and not dest.exists()
    assert B.main(["export", str(dest), "--include", "settings"], st) == 0
    file = next(dest.glob("*.zip"))
    assert B.main(["list", str(file)], st) == 0
    assert B.main(["verify", str(file)], st) == 0
    assert "every hash matches" in capsys.readouterr().out
    assert B.main(["import", str(file), "--into", str(tmp_path / "r"), "--dry-run"], st) == 0
    assert not (tmp_path / "r").exists()
    assert B.main(["import", str(file), "--into", str(tmp_path / "r")], st) == 0
    assert (tmp_path / "r" / "notes.md").exists()
    assert B.main(["export", str(st.root / "inside")], st) == 1


def test_web_panel_and_job(st, tmp_path):
    page = (ROOT / "marsad" / "web" / "index.html").read_text(encoding="utf-8")
    assert 'id="backup-panel"' in page and "/backup/plan" in page
    B.JOB.clear()
    B.JOB["state"] = "idle"
    with pytest.raises(B.Refused, match="confirm"):
        B.start_export(st, {"dest": str(tmp_path / "ext")})
    B.start_export(st, {"dest": str(tmp_path / "ext"), "groups": ["settings"], "confirm": True})
    import time
    for _ in range(200):
        if B.JOB["state"] != "running":
            break
        time.sleep(0.05)
    assert B.JOB["state"] == "done" and B.JOB["result"]["verified"]
    B.start_verify({"file": B.JOB["result"]["file"]})
    for _ in range(200):
        if B.JOB["state"] != "running":
            break
        time.sleep(0.05)
    assert B.JOB["result"]["ok"]


def test_the_endpoints_answer_over_http(st, tmp_path):
    import socket
    import threading
    import time
    import urllib.error
    import urllib.request
    from marsad import live
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    threading.Thread(target=live.main, args=(["--port", str(port)], st), daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    def post(path, body):
        req = urllib.request.Request(base + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as exc:
            return json.loads(exc.read())

    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/backup/status", timeout=2)
            break
        except OSError:
            time.sleep(0.1)
    dest = str(tmp_path / "ext")
    assert "free_h" in post("/backup/plan", {"dest": dest, "groups": ["settings"]})
    assert "inside" in post("/backup/plan", {"dest": str(st.root / "b")})["error"]
    assert "confirm" in post("/backup/export", {"dest": dest, "groups": ["settings"]})["error"]
    B.JOB.clear()
    B.JOB["state"] = "idle"
    post("/backup/export", {"dest": dest, "groups": ["settings"], "confirm": True})
    for _ in range(200):
        j = json.loads(urllib.request.urlopen(base + "/backup/status", timeout=5).read())
        if j["state"] != "running":
            break
        time.sleep(0.05)
    assert j["state"] == "done" and post("/backup/list", {"file": j["result"]["file"]})["location_included"] is False
