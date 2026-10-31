"""Listening to a capture (catalogue.audio_plan / audio_stream, GET /captures/audio, 2026-10-30).

The filled station (conftest.filled_template) holds an open FSK signal on 433.92 MHz with an IQ
capture (bursts 0.4 s long every 2.5 s, the symbol flipping at 1200 baud, +/-2.4 kHz) and a
private one on 880.5 MHz (850 cellular downlink) with fingerprints only. Pinned:

- the WAV is exactly as long as its header says, at the rate the plan names, in every mode;
- narrow FM recovers the FSK's 600 Hz square wave; envelope mode turns the bursts into a 2.5 s
  rhythm with silence between; an offset moves the listening point;
- nothing is opened for writing while audio is made, here or over HTTP;
- a private event is refused (403 over HTTP), and so is a row that claims to be open in a band
  the tier table says is private, exactly like /captures/iq and /captures/export.
"""
from __future__ import annotations

import json
import socket
import struct
import threading
import time
import urllib.error
import urllib.request

import numpy as np
import pytest

from marsad import capture as cap
from marsad import catalogue as C

from conftest import copy_station
from test_watch import WriteSpy

pytestmark = pytest.mark.xdist_group("filled_station")


@pytest.fixture
def filled(filled_template, tmp_path):
    st = copy_station(filled_template, tmp_path / "st")
    con = cap.connect(st)
    ids = {k: [r["id"] for r in con.execute(f"SELECT id FROM captures WHERE {w} ORDER BY id")]
           for k, w in (("written", "iq_status = 'written'"), ("private", "tier = 'private'"))}
    con.close()
    return st, ids


def pcm(st, eid, mode, **kw):
    plan = C.audio_plan(st, eid, mode, **kw)
    b = b"".join(C.audio_stream(plan))
    assert len(b) == plan["bytes"] and b[:4] == b"RIFF"
    rate = struct.unpack("<I", b[24:28])[0]
    n = struct.unpack("<I", b[40:44])[0] // 2
    assert rate == plan["rate"] and n == plan["samples"]
    return plan, np.frombuffer(b[44:], "<i2").astype(float) / 32767


def test_every_mode_is_a_whole_wav_and_nothing_is_written(filled, monkeypatch):
    st, ids = filled
    spy = WriteSpy(monkeypatch)
    for mode in ("am", "nfm", "envelope"):
        plan, x = pcm(st, ids["written"][0], mode)
        assert plan["seconds"] > 4 and len(x) == plan["samples"] and np.abs(x).max() > 0.1
    assert spy.opened == []                                 # streamed, never saved


def test_narrow_fm_recovers_the_fsk_and_envelope_its_rhythm(filled):
    st, ids = filled
    plan, e = pcm(st, ids["written"][0], "envelope")
    blk = plan["rate"] // 20                                # 50 ms blocks
    pk = np.abs(e[: len(e) // blk * blk]).reshape(-1, blk).max(axis=1)
    on = np.flatnonzero(pk > 0.3) * 0.05
    starts = [on[0]] + [b for a, b in zip(on[:-1], on[1:]) if b - a > 0.5]
    assert len(starts) >= 2 and np.diff(starts) == pytest.approx(2.5, abs=0.15)
    burst, quiet = np.percentile(pk, 95), np.median(pk[pk < 0.3])
    assert 20 * np.log10(burst / quiet) > 20                # bursts stand 20 dB over the gaps
    # FM between bursts is noise (random phase), so look inside one: the middle of a burst
    plan, x = pcm(st, ids["written"][0], "nfm")
    t0 = starts[1] + 0.1
    seg = x[int(t0 * plan["rate"]): int((t0 + 0.2) * plan["rate"])]
    f = np.fft.rfftfreq(len(seg), 1 / plan["rate"])
    peak = f[np.argmax(np.abs(np.fft.rfft(seg * np.hanning(len(seg))))[1:]) + 1]
    assert peak == pytest.approx(600, abs=40)               # 1200 baud alternating = 600 Hz


def test_offset_and_start_and_mode_are_checked(filled):
    st, ids = filled
    eid = ids["written"][0]
    p = C.audio_plan(st, eid, "am", start_s=1.0, offset_hz=3e3)
    assert p["start"] == int(p["fs"]) and p["offset_hz"] == 3e3
    with pytest.raises(C.Refused, match="outside the capture's band"):
        C.audio_plan(st, eid, "am", offset_hz=p["fs"])
    with pytest.raises(C.Refused, match="unknown mode"):
        C.audio_plan(st, eid, "usb")
    assert C.audio_plan(st, eid, "am", dur_s=0)["bytes"] == 44


def test_private_is_refused_even_for_a_forged_row(filled):
    st, ids = filled
    with pytest.raises(C.Refused, match="private"):
        C.audio_plan(st, ids["private"][0], "am")
    con = cap.connect(st)
    src = con.execute("SELECT * FROM captures WHERE iq_status = 'written'").fetchone()
    rid = cap.add_row(con, {"started": src["started"], "started_epoch": src["started_epoch"] + 1,
                            "freq_hz": 1940e6, "target_hz": 1940e6, "tier": "open", "iq_status": "written",
                            "iq_path": src["iq_path"], "iq_bytes": src["iq_bytes"],
                            "store_rate_hz": src["store_rate_hz"], "fingerprint": src["fingerprint"]})
    con.close()
    for mode in ("am", "nfm", "envelope"):
        with pytest.raises(C.Refused, match="private"):
            C.audio_plan(st, rid, mode)


def test_http_streams_open_and_refuses_private_with_403(filled, monkeypatch):
    from marsad import live
    st, ids = filled
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    threading.Thread(target=live.main, args=(["--port", str(port)], st), daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            urllib.request.urlopen(base + "/captures/recorders", timeout=2).read()
            break
        except OSError:
            time.sleep(0.1)
    spy = WriteSpy(monkeypatch)
    eid = ids["written"][0]
    r = urllib.request.urlopen(f"{base}/captures/audio?id={eid}&mode=envelope&start=0.5", timeout=30)
    body = r.read()
    assert r.headers["Content-Type"] == "audio/wav" and int(r.headers["Content-Length"]) == len(body)
    assert body[:4] == b"RIFF" and len(body) == C.audio_plan(st, eid, "envelope", start_s=0.5)["bytes"]
    for bad in (ids["private"][0],):
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(f"{base}/captures/audio?id={bad}&mode=am", timeout=10)
        assert ei.value.code == 403 and "private" in json.loads(ei.value.read())["error"]
    assert spy.opened == []
    page = urllib.request.urlopen(base + "/", timeout=10).read().decode("utf-8")
    assert 'id="cs-au-play"' in page and "/captures/audio?" in page
