"""Archiving captures out of the pool (marsad/capture_archive.py, 2026-10-30).

A synthetic station is filled by the real recorder with a replayed radio, as in
test_catalogue.py: an open signal with one IQ capture and events inside it, and a
private one with fingerprints only. Pinned:
- the plan changes nothing, and an event that owns a capture brings the events inside it;
- archive needs confirm; it writes one zip (files + manifest + README) whose hashes match,
  takes the IQ out of the pool, keeps fingerprints by default, marks the rows "archived"
  with archive_path, and the pool figure drops while the archive figure rises;
- a zip that fails verification removes nothing from the pool and leaves no zip behind;
- a capture a running recorder is writing is refused;
- private events archive as fingerprints and rows only, and the 403 rules still hold;
- restore puts every file back byte for byte, gives the rows their old status, keeps
  the zip, and refuses rather than overwrite a different file;
- the fingerprint of an archived event is still viewable, from the zip when it left the pool;
- the HTTP endpoints, including the confirm requirement.
"""
from __future__ import annotations

import hashlib
import json
import socket
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pytest

from marsad import capture as cap
from marsad import capture_archive as A
from marsad import catalogue as C
from marsad import watch as W

from test_watch import V5, Launches, scene, station


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.fixture
def filled(tmp_path):
    st = station(tmp_path, {"capture_s": 4})
    cfg = cap.settings(st)
    rec = W.Recorder(st, cfg, V5, ["rtl_sdr"], "test chain", 29.7,
                     Launches(scene(10.0, 0.4, 2.5, kind="fsk"), 433.92e6), quiet=True,
                     inline_characterise=True)
    W.fixed_watch(rec, 433.92e6, 9.0, width_hz=20e3)
    rec2 = W.Recorder(st, cfg, V5, ["rtl_sdr"], "test chain", 29.7,
                      Launches(scene(6.0, 0.6, 2.5), 880.5e6), quiet=True)
    W.fixed_watch(rec2, 880.5e6, 6.0, width_hz=20e3)
    con = cap.connect(st)
    ids = {k: [r["id"] for r in con.execute(f"SELECT id FROM captures WHERE {w} ORDER BY id")]
           for k, w in (("written", "iq_status = 'written'"), ("inside", "iq_status = 'inside an earlier capture'"),
                        ("private", "tier = 'private'"))}
    con.close()
    assert ids["written"] and ids["inside"] and ids["private"]
    return st, ids


def pool_files(st) -> dict:
    pool = cap.pool_dir(st)
    return {p.relative_to(pool).as_posix(): sha(p) for p in pool.rglob("*")
            if p.is_file() and not p.name.startswith("recorder_status_")}


def row(st, i):
    con = cap.connect(st)
    try:
        return dict(con.execute("SELECT * FROM captures WHERE id = ?", (i,)).fetchone())
    finally:
        con.close()


def test_plan_changes_nothing_and_a_capture_brings_its_events(filled):
    st, ids = filled
    before = pool_files(st)
    p = A.plan(st, ids["written"][0], "event")
    assert pool_files(st) == before
    assert set(p["rows"]) == set(ids["written"]) | set(ids["inside"])       # nothing left pointing at nothing
    kinds = {f["kind"] for f in p["files"]}
    assert {"sigmf-data", "sigmf-meta", "characterise", "fingerprint"} <= kinds
    assert all(not f["leaves_pool"] for f in p["files"] if f["kind"] == "fingerprint")    # kept by default
    assert p["bytes_leaving_pool"] == sum(f["bytes"] for f in p["files"] if f["kind"] != "fingerprint")
    # an event inside the capture, scope "capture", gives the same set
    assert set(A.plan(st, ids["inside"][0], "capture")["rows"]) == set(p["rows"])
    # the signal group: every event at 433.92
    g = A.plan(st, ids["inside"][0], "group")
    assert set(p["rows"]) <= set(g["rows"]) and not set(g["rows"]) & set(ids["private"])


def test_archive_verifies_then_moves_and_restore_puts_it_back(filled):
    st, ids = filled
    before = pool_files(st)
    used0 = C.summary(st)["storage"]["used_gb"]
    with pytest.raises(A.Refused, match="not confirmed"):
        A.archive(st, ids["written"][0])
    assert pool_files(st) == before
    out = A.archive(st, ids["written"][0], "capture", note="good radar", label="keep me", confirm=True)
    z = Path(out["zip"])
    assert z.parent == st.root / "recordings" / "archive" and z.name.startswith("arch_433.92") and "keep-me" in z.name
    assert out["verified"] and out["bytes_freed"] > 0 and A.verify(z) == []
    with zipfile.ZipFile(z) as zf:
        m = json.loads(zf.read("manifest.json"))
        assert "README.txt" in zf.namelist()
    assert m["note"] == "good radar" and {r["id"] for r in m["rows"]} == set(ids["written"]) | set(ids["inside"])
    assert any(r["iq_status"] == "written" for r in m["rows"])          # the old status, for restore
    for f in m["files"]:
        assert before[f["rel"]] == f["sha256"]                          # what went in is what was there
    after = pool_files(st)
    gone = {f["rel"] for f in m["files"] if f["removed_from_pool"]}
    assert gone and not gone & set(after) and all(r.endswith((".sigmf-data", ".sigmf-meta", ".md", ".json")) for r in gone)
    assert all(after[k] == v for k, v in before.items() if k not in gone)           # nothing else touched
    for i in m["rows"]:
        r = row(st, i["id"])
        assert r["iq_status"] == "archived" and r["archive_path"] == str(z)
    s = C.summary(st)
    assert s["storage"]["used_gb"] == pytest.approx(used0 - out["bytes_freed"] / 1e9, abs=1e-6)
    assert s["storage"]["archive_gb"] == pytest.approx(z.stat().st_size / 1e9)
    assert C.summary(st, badge="archived")["total"] == len(m["rows"])
    ev = C.event(st, ids["written"][0])
    assert "ARCHIVED" in ev["badges"] and ev["archive"]["note"] == "good radar" and ev["capture"] is None
    assert C.fingerprint_view(st, ids["inside"][0])["rows"] > 0         # fingerprint kept in the pool
    with pytest.raises(A.Refused, match="already archived"):
        A.plan(st, ids["written"][0], "event")
    # restore: byte for byte, old statuses, zip kept
    res = A.restore(st, ids["inside"][0])
    assert res["bytes_restored"] == out["bytes_freed"] and z.exists()
    assert pool_files(st) == before
    assert row(st, ids["written"][0])["iq_status"] == "written"
    assert all(row(st, i)["iq_status"] == "inside an earlier capture" for i in ids["inside"])
    assert "ARCHIVED" not in C.event(st, ids["written"][0])["badges"]
    assert C.event(st, ids["written"][0])["capture"] is not None


def test_a_zip_that_fails_verification_takes_nothing_out(filled):
    st, ids = filled
    before = pool_files(st)
    with pytest.raises(A.Refused, match="did not verify"):
        A.archive(st, ids["written"][0], confirm=True, _verify=lambda z, m: ["x.sigmf-data does not match its hash"])
    assert pool_files(st) == before
    assert not list((st.root / "recordings" / "archive").glob("*"))      # no half zip left behind
    assert row(st, ids["written"][0])["iq_status"] == "written"
    # and the real verifier catches a corrupted member
    out = A.archive(st, ids["written"][0], confirm=True)
    z = Path(out["zip"])
    with zipfile.ZipFile(z) as zf:
        m = json.loads(zf.read("manifest.json"))
    m["files"][0]["sha256"] = "0" * 64
    assert A.verify(z, m)


def test_refused_while_a_recorder_is_writing_that_capture(filled):
    st, ids = filled
    data = Path(row(st, ids["written"][0])["iq_path"])
    pool = cap.pool_dir(st)
    (pool / "recorder_status_27825674.json").write_text(json.dumps(
        {"state": "running", "updated": time.time(), "serial": "27825674",
         "capture": {"file": data.name, "started": time.time(), "ends_at": time.time() + 30}}))
    before = pool_files(st)
    with pytest.raises(A.Refused, match="being written"):
        A.archive(st, ids["written"][0], confirm=True)
    assert pool_files(st) == before
    # once that recorder has gone silent, it may go
    (pool / "recorder_status_27825674.json").write_text(json.dumps(
        {"state": "running", "updated": time.time() - 60, "serial": "27825674", "capture": {"file": data.name}}))
    assert A.plan(st, ids["written"][0])["captures"] == 1


def test_private_events_archive_fingerprints_and_rows_only(filled):
    st, ids = filled
    p = A.plan(st, ids["private"][0], "group", include_exports=True, keep_fingerprints=False)
    assert p["private"] == len(p["rows"]) == len(ids["private"])
    assert {f["kind"] for f in p["files"]} == {"fingerprint"}
    out = A.archive(st, ids["private"][0], "group", keep_fingerprints=False, confirm=True)
    with zipfile.ZipFile(out["zip"]) as zf:
        assert all(n.endswith((".fp.npz", "manifest.json", "README.txt")) for n in zf.namelist())
    # its fingerprint left the pool and is read back out of the zip
    assert not Path(row(st, ids["private"][0])["fingerprint"]).exists()
    assert C.fingerprint_view(st, ids["private"][0])["private"]
    with pytest.raises(C.Refused):
        C.export(st, ids["private"][0], "wav")
    with pytest.raises(C.Refused):
        C.iq_view(st, ids["private"][0])


def test_restore_never_overwrites_a_different_file(filled):
    st, ids = filled
    out = A.archive(st, ids["written"][0], confirm=True)
    with zipfile.ZipFile(out["zip"]) as zf:
        m = json.loads(zf.read("manifest.json"))
    rel = next(f["rel"] for f in m["files"] if f["kind"] == "sigmf-meta")
    (cap.pool_dir(st) / rel).write_text("someone else's file")
    with pytest.raises(A.Refused, match="different content"):
        A.restore(st, ids["written"][0])
    assert row(st, ids["written"][0])["iq_status"] == "archived"
    assert (cap.pool_dir(st) / rel).read_text() == "someone else's file"


def test_archive_folder_inside_the_pool_is_refused(tmp_path):
    st = station(tmp_path, {"archive": "recordings/captures/keep"})
    with pytest.raises(A.Refused, match="inside the pool"):
        A.archive_dir(st)


def test_http_archive_needs_confirm_and_restores(filled):
    from marsad import live
    st, ids = filled
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    threading.Thread(target=live.main, args=(["--port", str(port)], st), daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            urllib.request.urlopen(base + "/captures", timeout=2).read()
            break
        except OSError:
            time.sleep(0.1)

    def post(path, body):
        req = urllib.request.Request(base + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req, timeout=60).read())

    eid = ids["written"][0]
    p = post("/captures/archive/plan", {"id": eid, "scope": "event"})
    assert p["captures"] == 1 and p["bytes_leaving_pool"] > 0
    with pytest.raises(urllib.error.HTTPError) as ei:                 # "confirm": "yes" is not true
        post("/captures/archive", {"id": eid, "confirm": "yes"})
    assert ei.value.code == 403 and "not confirmed" in json.loads(ei.value.read())["error"]
    d = post("/captures/archive", {"id": eid, "note": "n", "confirm": True})
    assert d["bytes_freed"] == p["bytes_leaving_pool"]
    s = json.loads(urllib.request.urlopen(base + "/captures").read())
    assert s["storage"]["archive_gb"] > 0
    assert post("/captures/restore", {"id": eid})["bytes_restored"] == d["bytes_freed"]
    page = urllib.request.urlopen(base + "/").read().decode("utf-8")
    assert 'id="cs-ar"' in page and "/captures/archive/plan" in page
