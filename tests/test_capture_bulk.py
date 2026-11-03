"""Sizes, combinable filters and archiving a selection on the Captures tab.

The operator keeps only some of what the recorder writes, so the list shows what each event
takes in the pool and archives many events at once. Same filled station as
test_capture_archive (an open signal with one IQ capture and events inside it, a private
signal with fingerprints only). Pinned:
- each row is charged what it owns; an event inside a capture is charged its fingerprint
  and names the capture, so the capture's bytes are counted once; groups add up; sort by size;
- every filter combines (time of day wraps past midnight, content, size, archived, watch,
  band, several grades at once);
- a selection plans one zip per signal group or one for all, changes nothing, and picking
  an event inside a capture brings the capture and every event inside it;
- archiving needs confirm, verifies each zip before anything of its own leaves the pool,
  and a zip that fails keeps its files while the others still go;
- a capture a running recorder is writing is skipped and named, and the rest goes on;
- private rows go as fingerprints and rows only;
- the HTTP endpoints, including the confirm requirement.
"""
from __future__ import annotations

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

from test_capture_archive import filled, pool_files, row  # noqa: F401  (fixture)

pytestmark = pytest.mark.xdist_group("filled_station")


def all_rows(s) -> dict:
    return {r["id"]: r for g in s["groups"] for r in g["rows"]}


def on_disk(*paths) -> int:
    return sum(Path(p).stat().st_size for p in paths if p and Path(p).exists())


# ---------------------------------------------------------------- sizes

def test_each_event_is_charged_what_it_owns_and_a_capture_once(filled):
    st, ids = filled
    s = C.summary(st)
    rs = all_rows(s)
    w = rs[ids["written"][0]]
    data = Path(w["iq_path"])
    meta = data.with_name(data.name.replace(".sigmf-data", ".sigmf-meta"))
    md = Path(w["characterised"]) if w.get("characterised") else None
    assert w["bytes"] == on_disk(w["fingerprint"], data, meta, md, md and md.with_suffix(".json"))
    assert w["bytes"] > data.stat().st_size > 0 and w["inside_of"] is None
    for i in ids["inside"]:
        assert rs[i]["inside_of"] == w["id"] and rs[i]["inside_of_bytes"] == w["bytes"]
        assert rs[i]["bytes"] == on_disk(rs[i]["fingerprint"])           # the IQ is counted on its operator
    for i in ids["private"]:
        assert rs[i]["bytes"] == on_disk(rs[i]["fingerprint"])
    assert s["bytes"] == sum(r["bytes"] for r in rs.values()) == sum(g["bytes"] for g in s["groups"])
    big = C.summary(st, sort="size")["groups"]
    assert [g["bytes"] for g in big] == sorted((g["bytes"] for g in big), reverse=True)
    assert big[0]["rows"][0]["id"] == w["id"]                            # the capture leads its group


def test_filters_combine(filled):
    st, ids = filled
    n = C.summary(st)["total"]
    iq = len(ids["written"]) + len(ids["inside"])
    assert C.summary(st, content="iq")["total"] == iq
    assert C.summary(st, content="fp")["total"] == n - iq
    assert C.summary(st, min_mb=1)["total"] == len(ids["written"])       # only the capture is big
    assert C.summary(st, min_mb=1, tier="private")["total"] == 0
    assert C.summary(st, tier="private", content="fp")["total"] == len(ids["private"])
    assert C.summary(st, watch="fixed")["total"] == n and C.summary(st, watch="survey_x")["total"] == 0
    assert C.summary(st, archived="yes")["total"] == 0 and C.summary(st, archived="no")["total"] == n
    assert C.summary(st, family="none", content="iq")["total"] <= iq
    t = row(st, ids["written"][0])["started"][11:16]
    h = int(t[:2])
    assert C.summary(st, tod_from=t, tod_to=t)["total"] >= 1
    wrap = C.summary(st, tod_from=f"{(h + 1) % 24:02d}:00", tod_to=f"{h:02d}:59")   # runs past midnight
    assert wrap["total"] >= 1
    hours = {int(r["started"][11:13]) for r in all_rows(C.summary(st)).values()}
    free = next(x for x in range(24) if x not in hours)     # the rows can straddle an hour
    assert C.summary(st, tod_from=f"{free:02d}:00", tod_to=f"{free:02d}:30")["total"] == 0
    d = row(st, ids["written"][0])["started"][:10]
    assert C.summary(st, date_from=d, date_to=d)["total"] == n
    assert C.summary(st, date_from="2099-01-01")["total"] == 0
    grades = {r["grade"] or "ungraded" for r in all_rows(C.summary(st)).values()}
    assert C.summary(st, grade=",".join(grades))["total"] == n
    s = C.summary(st)
    assert {w["survey"] for w in s["watches"]} == {None} and isinstance(s["families"], list)


# ---------------------------------------------------------------- planning a selection

def test_picking_an_event_inside_a_capture_brings_the_capture(filled):
    st, ids = filled
    before = pool_files(st)
    p = A.plan_many(st, [ids["inside"][0]])
    assert pool_files(st) == before
    assert len(p["zips"]) == 1 and set(p["zips"][0]["rows"]) == set(ids["written"]) | set(ids["inside"])
    assert p["captures"] == 1 and p["bytes_leaving_pool"] > 0 and not p["skipped"]


def test_one_zip_per_group_or_one_for_all(filled):
    st, ids = filled
    pick = [ids["written"][0], ids["private"][0]]
    per = A.plan_many(st, pick, per_group=True)
    one = A.plan_many(st, pick, per_group=False)
    assert len(per["zips"]) == 2 and len(one["zips"]) == 1
    assert per["events"] == one["events"] == len(ids["written"]) + len(ids["inside"]) + 1
    assert per["bytes_leaving_pool"] == one["bytes_leaving_pool"]
    priv = next(z for z in per["zips"] if z["private"])
    assert {f["kind"] for f in priv["files"]} == {"fingerprint"}
    with pytest.raises(A.Refused, match="nothing is selected"):
        A.plan_many(st, [])


# ---------------------------------------------------------------- archiving a selection

def test_archive_many_needs_confirm_then_moves_each_verified_zip(filled):
    st, ids = filled
    before = pool_files(st)
    pick = [ids["written"][0]] + ids["private"]
    with pytest.raises(A.Refused, match="not confirmed"):
        A.archive_many(st, pick)
    assert pool_files(st) == before
    out = A.archive_many(st, pick, note="keep", label="bulk", confirm=True)
    assert out["verified"] and len(out["zips"]) == 2 and not out["failed"]
    for z in out["zips"]:
        assert A.verify(Path(z["zip"])) == [] and "bulk" in Path(z["zip"]).name
    with zipfile.ZipFile(next(z["zip"] for z in out["zips"] if "880" in Path(z["zip"]).name)) as zf:
        assert all(n.endswith((".fp.npz", "manifest.json", "README.txt")) for n in zf.namelist())
    after = pool_files(st)
    assert not [k for k in after if k.endswith(".sigmf-data")]
    assert all(row(st, i)["iq_status"] == "archived" for i in ids["written"] + ids["inside"] + ids["private"])
    assert out["bytes_freed"] == sum(z["bytes_freed"] for z in out["zips"]) > 0
    with pytest.raises(A.Refused, match="already archived"):
        A.plan_many(st, pick)


def test_a_zip_that_fails_keeps_its_files_and_the_others_still_go(filled):
    st, ids = filled
    before = pool_files(st)
    bad = lambda z, m: (["x does not match its hash"] if any(f["kind"] == "sigmf-data" for f in m["files"]) else [])
    out = A.archive_many(st, [ids["written"][0], ids["private"][0]], confirm=True, _verify=bad)
    assert len(out["failed"]) == 1 and len(out["zips"]) == 1 and not out["verified"]
    after = pool_files(st)
    assert {k: v for k, v in before.items() if "sigmf" in k} == {k: v for k, v in after.items() if "sigmf" in k}
    assert row(st, ids["written"][0])["iq_status"] == "written"
    assert row(st, ids["private"][0])["iq_status"] == "archived"
    assert len(list((st.root / "recordings" / "archive").glob("*.zip"))) == 1    # no half zip left


def test_a_capture_being_written_is_skipped_and_the_rest_goes(filled):
    st, ids = filled
    data = Path(row(st, ids["written"][0])["iq_path"])
    (cap.pool_dir(st) / "recorder_status_00000202.json").write_text(json.dumps(
        {"state": "running", "updated": time.time(), "serial": "00000202",
         "capture": {"file": data.name, "started": time.time(), "ends_at": time.time() + 30}}))
    p = A.plan_many(st, [ids["inside"][0], ids["private"][0]])
    assert len(p["skipped"]) == 1 and "being written" in p["skipped"][0]["why"]
    assert set(p["skipped"][0]["rows"]) == set(ids["written"]) | set(ids["inside"])
    out = A.archive_many(st, [ids["inside"][0], ids["private"][0]], confirm=True)
    assert out["skipped"] and data.exists() and row(st, ids["written"][0])["iq_status"] == "written"
    assert row(st, ids["private"][0])["iq_status"] == "archived"
    with pytest.raises(A.Refused, match="being written"):
        A.plan_many(st, [ids["written"][0]])                               # nothing else left to do


# ---------------------------------------------------------------- over HTTP

def test_http_sizes_filters_and_bulk_archive(filled):
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

    s = json.loads(urllib.request.urlopen(base + "/captures?sort=size&content=iq&min_mb=1").read())
    assert s["total"] == len(ids["written"]) and s["groups"][0]["bytes"] > 0
    s = json.loads(urllib.request.urlopen(base + "/captures?tier=private&archived=no").read())
    assert s["total"] == len(ids["private"])
    pick = [ids["inside"][0], ids["private"][0]]
    p = post("/captures/archive/plan", {"ids": pick, "per_group": False})
    assert p["bulk"] and len(p["zips"]) == 1
    with pytest.raises(urllib.error.HTTPError) as ei:
        post("/captures/archive", {"ids": pick, "confirm": "yes"})
    assert ei.value.code == 403 and "not confirmed" in json.loads(ei.value.read())["error"]
    d = post("/captures/archive", {"ids": pick, "per_group": False, "confirm": True})
    assert d["bytes_freed"] == p["bytes_leaving_pool"] and len(d["zips"]) == 1
    page = urllib.request.urlopen(base + "/").read().decode("utf-8")
    assert 'id="cs-bk"' in page and "daily cap counts everything written today" in page
    assert "delete" not in page[page.index('id="cs-bk"'):page.index('id="cs-groups"')].lower()
