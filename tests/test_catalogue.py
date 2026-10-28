"""The Captures tab's server side (marsad/catalogue.py), RECORDER_DESIGN.md phase 4.

A synthetic station is filled by the real recorder (`watch.fixed_watch` with a replayed
radio, as in test_watch.py): an open band with an IQ capture and a private band with
fingerprints only. Pinned:
- the list: grouping by signal, badges, filters, the storage strip;
- the event page: own capture, an event inside an earlier capture, a private event
  that gets no meta, report or exports;
- pictures: fingerprints for every event, private included; the IQ view reads the
  file a slice at a time and is refused for private events;
- exports: the WAV is the SigMF data byte for byte behind a 16-bit stereo header and an
  SDR++-style name carrying the centre frequency; the cf32 matches; the master is
  untouched; a private event is refused by the server, through the HTTP endpoint too,
  even when the row was forged to say "open";
- an old catalogue without target_hz still reads.
"""
from __future__ import annotations

import hashlib
import json
import re
import socket
import sqlite3
import struct
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import pytest

from marsad import capture as cap
from marsad import catalogue as C
from marsad import watch as W

from test_watch import V5, Launches, scene, station


@pytest.fixture(scope="module")
def filled(tmp_path_factory):
    """One open signal (433.92 MHz, captured and characterised) and one private one
    (880.5 MHz, 850 cellular downlink, fingerprints only)."""
    root = tmp_path_factory.mktemp("catalogue")
    st = station(root, {"capture_s": 4})
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
    return st, ids


def test_summary_groups_badges_and_storage(filled):
    st, ids = filled
    s = C.summary(st)
    assert len(s["groups"]) == 2 and s["total"] >= 6
    by = {int(g["f_mhz"]): g for g in s["groups"]}
    assert by[433]["captures"] == 1 and not by[433]["private"] and by[433]["family"]
    assert by[880]["private"] and by[880]["captures"] == 0
    assert all("PRIVATE: do not decode" in r["badges"] for r in by[880]["rows"])
    assert all(r["badges"][0] == "OPEN" for r in by[433]["rows"])
    assert s["storage"]["used_gb"] > 0 and s["storage"]["pool_gb"] == 100 and s["storage"]["daily_gb"] == 20
    assert s["status"]["state"] == "idle"                 # the recorder said so when it finished
    assert C.summary(st, badge="private")["total"] == len(ids["private"])
    assert C.summary(st, badge="written")["total"] == 1
    assert C.summary(st, lo_mhz=800, hi_mhz=900)["groups"][0]["private"]
    assert C.summary(st, date="1999-01-01")["total"] == 0


def test_event_pages_open_inside_and_private(filled):
    st, ids = filled
    e = C.event(st, ids["written"][0])
    assert not e["private"] and e["capture"]["own"] and e["meta"]["global"]["core:datatype"] == "ci16_le"
    assert e["meta"]["global"]["marsad:chain"] == "test chain" and e["recipe"] and "Recipe" in e["report"]
    inside = C.event(st, ids["inside"][0])
    assert inside["capture"]["id"] == ids["written"][0] and not inside["capture"]["own"]
    assert inside["capture"]["offset_s"] > 0
    p = C.event(st, ids["private"][0])
    assert p["private"] and p["meta"] is None and p["report"] is None and p["exports"] == [] and p["capture"] is None


def test_fingerprints_for_every_event_private_included(filled):
    st, ids = filled
    for i in (ids["written"][0], ids["private"][0]):
        f = C.fingerprint_view(st, i)
        raw = np.frombuffer(__import__("base64").b64decode(f["img"]), np.uint8)
        assert raw.size == f["rows"] * f["cols"] and raw.max() > 200     # the burst stands out
        assert f["dt_s"] >= 0.019


def test_iq_view_streams_and_refuses_private(filled, monkeypatch):
    st, ids = filled
    real = np.fromfile
    biggest = []

    def spy(*a, **k):
        biggest.append(k.get("count", -1))
        return real(*a, **k)
    monkeypatch.setattr(C.np, "fromfile", spy)
    C._IQ_CACHE.clear()
    v = C.iq_view(st, ids["inside"][0])                 # through the covering capture
    data = Path(C.event(st, ids["written"][0])["capture"]["file"])
    n = C.event(st, ids["written"][0])["capture"]["bytes"] // 2
    assert v["rows"] > 10 and len(v["env_dbfs_peak"]) > 50 and max(v["env_dbfs_peak"]) > -20
    assert biggest and max(biggest) <= n // 10              # a slice at a time, never the whole file
    with pytest.raises(C.Refused, match="private"):
        C.iq_view(st, ids["private"][0])
    assert data.name.endswith(".sigmf-data")


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_exports_wav_and_cf32_copy_the_master(filled):
    st, ids = filled
    e = C.event(st, ids["written"][0])
    con = cap.connect(st)
    data = Path(con.execute("SELECT iq_path FROM captures WHERE id = ?", (ids["written"][0],)).fetchone()[0])
    before = sha(data)
    w = C.export(st, ids["inside"][0], "wav")               # from an event inside the capture: same file
    wav = Path(w["file"])
    assert wav.parent == data.parent / "exports"
    m = re.search(r"([0-9]+)Hz", wav.name)                  # what SDR++'s file source looks for
    assert m and int(m.group(1)) == int(round(e["meta"]["captures"][0]["core:frequency"]))
    raw = wav.read_bytes()
    assert raw[:4] == b"RIFF" and raw[8:12] == b"WAVE"
    tag, ch, rate, _, align, bits = struct.unpack("<HHIIHH", raw[20:36])
    assert (tag, ch, bits, align) == (1, 2, 16, 4)
    assert rate == int(round(e["meta"]["global"]["core:sample_rate"]))
    assert raw[44:] == data.read_bytes()                    # ci16 I/Q is 16-bit stereo already
    assert C.export(st, ids["written"][0], "wav")["note"] == "already exported"
    c = C.export(st, ids["written"][0], "cf32")
    z = np.fromfile(c["file"], np.complex64)
    i16 = np.fromfile(data, np.int16).astype(np.float32) / 32768
    assert np.allclose(z.view(np.float32), i16)
    meta = json.loads((data.parent / "exports" / data.name.replace(".sigmf-data", ".sigmf-meta")).read_text())
    assert meta["global"]["core:datatype"] == "cf32_le" and meta["global"]["core:dataset"] == Path(c["file"]).name
    assert sha(data) == before                              # the master is never touched
    assert set(C.event(st, ids["written"][0])["exports"]) >= {wav.name, Path(c["file"]).name}
    with pytest.raises(C.Refused):
        C.export(st, ids["written"][0], "mp3")


def test_private_export_refused_even_for_a_forged_row(filled, tmp_path):
    st, ids = filled
    with pytest.raises(C.Refused, match="private"):
        C.export(st, ids["private"][0], "wav")
    # a row that claims to be open, with a file, in a band the table says is private
    con = cap.connect(st)
    src = con.execute("SELECT * FROM captures WHERE iq_status = 'written'").fetchone()
    rid = cap.add_row(con, {"started": src["started"], "started_epoch": src["started_epoch"] + 1,
                            "freq_hz": 1940e6, "target_hz": 1940e6, "tier": "open", "iq_status": "written",
                            "iq_path": src["iq_path"], "iq_bytes": src["iq_bytes"],
                            "store_rate_hz": src["store_rate_hz"], "fingerprint": src["fingerprint"]})
    try:
        for fmt in ("wav", "cf32"):
            with pytest.raises(C.Refused, match="private"):
                C.export(st, rid, fmt)
        with pytest.raises(C.Refused):
            C.iq_view(st, rid)
        assert C.event(st, rid)["private"]
    finally:
        con.execute("DELETE FROM captures WHERE id = ?", (rid,))   # the test's own forged row
        con.commit()


def test_http_endpoints_refuse_private_export(filled):
    st, ids = filled
    from marsad import live
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
    summ = json.loads(urllib.request.urlopen(base + "/captures?badge=private").read())
    assert summ["total"] == len(ids["private"])

    def post(i, fmt):
        req = urllib.request.Request(base + "/captures/export", method="POST",
                                     data=json.dumps({"id": i, "format": fmt}).encode(),
                                     headers={"Content-Type": "application/json"})
        return urllib.request.urlopen(req, timeout=60)
    for fmt in ("wav", "cf32"):
        with pytest.raises(urllib.error.HTTPError) as ei:
            post(ids["private"][0], fmt)
        assert ei.value.code == 403 and "private" in json.loads(ei.value.read())["error"]
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(base + f"/captures/iq?id={ids['private'][0]}")
    assert ei.value.code == 403
    assert json.loads(post(ids["written"][0], "wav").read())["bytes"] > 44
    page = urllib.request.urlopen(base + "/").read().decode("utf-8")
    assert 'data-pane="captures"' in page
    pane = page[page.index('id="pane-captures"'):page.index('id="pane-probe"')]
    # no delete control anywhere on the tab, and no route that could delete
    assert not re.search(r"<button[^>]*>[^<]*(delete|remove)", pane, re.I)
    assert "method:'DELETE'" not in page and "/delete" not in page


def test_old_catalogue_without_target_column_reads(tmp_path):
    st = station(tmp_path)
    c = sqlite3.connect(st.db)
    c.executescript(cap.SCHEMA.replace("  target_hz       REAL,\n", ""))
    c.execute("INSERT INTO captures (started, started_epoch, freq_hz, bandwidth_hz, tier, iq_status, captured) "
              "VALUES ('2026-10-28T09:18:28', 1, 1265.497e6, 1.6e6, 'open', 'written', 1)")
    c.commit(); c.close()
    s = C.summary(st)                     # read-only: must not need (or add) the new column
    assert s["total"] == 1 and s["groups"][0]["f_mhz"] == pytest.approx(1265.497)
    assert "target_hz" not in {r[1] for r in sqlite3.connect(st.db).execute("PRAGMA table_info(captures)")}
