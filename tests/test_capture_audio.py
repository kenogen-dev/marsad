"""Listening to a capture (catalogue.audio_plan / audio_stream, GET /captures/audio).

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

import base64
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


# ---------------------------------------------------------------- choosing what plays

FS = 2.4e6
A_KHZ, B_KHZ = 300.0, -500.0      # above and below the capture's centre


def tone(t, f_hz, am_hz=0.0):
    x = np.exp(2j * np.pi * f_hz * t)
    return x * (1 + 0.8 * np.sin(2 * np.pi * am_hz * t)) / 1.8 if am_hz else x


def synth(st, x, name, freq_hz=433.92e6, fs=FS):
    """x (complex, about +/-1) as a written capture in the pool, with its catalogue row."""
    day = cap.pool_dir(st) / "synth"
    day.mkdir(parents=True, exist_ok=True)
    data = day / f"{name}.sigmf-data"
    rng = np.random.default_rng(7)
    x = x + (rng.normal(size=len(x)) + 1j * rng.normal(size=len(x))) * 0.003
    iq = np.empty(2 * len(x), np.int16)
    iq[0::2], iq[1::2] = np.clip(x.real * 12000, -32000, 32000), np.clip(x.imag * 12000, -32000, 32000)
    iq.astype("<i2").tofile(data)
    data.with_name(f"{name}.sigmf-meta").write_text(json.dumps({
        "global": {"core:datatype": "ci16_le", "core:sample_rate": fs},
        "captures": [{"core:frequency": freq_hz, "core:datetime": "2031-11-02T10:00:00Z", "core:sample_start": 0}],
        "annotations": []}))
    con = cap.connect(st)
    rid = cap.add_row(con, {"started": "2031-11-02T10:00:00", "started_epoch": 1793000000.0 + len(name),
                            "freq_hz": freq_hz, "target_hz": freq_hz, "tier": "open", "iq_status": "written",
                            "iq_path": str(data), "iq_bytes": data.stat().st_size, "store_rate_hz": fs})
    con.close()
    return rid


def strongest_hz(seg, rate):
    f = np.fft.rfftfreq(len(seg), 1 / rate)
    return f[np.argmax(np.abs(np.fft.rfft((seg - seg.mean()) * np.hanning(len(seg))))[1:]) + 1]


@pytest.fixture
def two_tones(filled):
    """A at +300 kHz, amplitude-modulated at 400 Hz; B at -500 kHz, at 1500 Hz. Both always on."""
    st, ids = filled
    t = np.arange(int(1.5 * FS)) / FS
    return st, ids, synth(st, 0.45 * tone(t, A_KHZ * 1e3, 400) + 0.45 * tone(t, B_KHZ * 1e3, 1500), "two_tones")


def test_the_waterfall_axis_and_the_offset_agree_on_which_side_is_which(filled):
    """A wrong sign would play the mirror frequency and still sound plausible, so this reads a
    column off the waterfall by the page's own formula and plays what that column names. The
    signals are gated: the waterfall is dB over each bin's median, so a steady one does not show."""
    st, ids = filled
    t = np.arange(int(1.5 * FS)) / FS
    eid = synth(st, 0.8 * tone(t, A_KHZ * 1e3, 400) * ((t > 0.1) & (t < 0.5))
                + 0.8 * tone(t, B_KHZ * 1e3, 1500) * ((t > 0.9) & (t < 1.3)), "gated")
    v = C.iq_view(st, eid)
    img = np.frombuffer(base64.b64decode(v["img"]), np.uint8).reshape(v["rows"], v["cols"]).astype(float)
    span = v["f_hi_khz"] - v["f_lo_khz"]
    khz = lambda c: v["f_lo_khz"] + (c + 0.5) / v["cols"] * span - v["rate"] / v["nfft"] / 2e3   # csAuXtoF
    row = lambda sec: int(sec / v["seconds"] * v["rows"])                                        # down the page
    # the middle of each peak: a strong line saturates the image scale across a few columns
    peak = lambda a: float(np.flatnonzero(a >= a.max() - 1).mean())
    upper = peak(img[row(0.2):row(0.4)].mean(axis=0))
    lower = peak(img[row(1.0):row(1.2)].mean(axis=0))
    assert khz(upper) == pytest.approx(A_KHZ, abs=span / v["cols"])
    assert khz(lower) == pytest.approx(B_KHZ, abs=span / v["cols"])
    plan, x = pcm(st, eid, "am", start_s=0.15, dur_s=0.3, offset_hz=khz(upper) * 1e3, bw_hz=40e3)
    assert strongest_hz(x, plan["rate"]) == pytest.approx(400, abs=15)
    plan, x = pcm(st, eid, "am", start_s=0.95, dur_s=0.3, offset_hz=khz(lower) * 1e3, bw_hz=40e3)
    assert strongest_hz(x, plan["rate"]) == pytest.approx(1500, abs=15)
    # the mirror of A is empty: nothing there stands above the noise on the plot's own terms
    plan, x = pcm(st, eid, "envelope", start_s=0.15, dur_s=0.3, offset_hz=-khz(upper) * 1e3, bw_hz=40e3)
    assert np.sqrt(np.mean(x ** 2)) < 0.01


def test_a_box_plays_that_signal_and_not_the_other(two_tones):
    st, _, eid = two_tones
    plan, x = pcm(st, eid, "am", offset_hz=A_KHZ * 1e3, bw_hz=40e3)
    S = np.abs(np.fft.rfft((x - x.mean()) * np.hanning(len(x))))
    f = np.fft.rfftfreq(len(x), 1 / plan["rate"])
    at = lambda hz: S[np.abs(f - hz) < 30].max()
    assert plan["bw_hz"] == 40e3 and not plan["wide"]
    assert 20 * np.log10(at(400) / at(1500)) > 40           # B is 800 kHz outside the box


def test_envelope_mode_hears_only_the_box(filled):
    """Envelope mode used to ignore any channel: a box would still have played everything."""
    st, ids = filled
    t = np.arange(int(2.0 * FS)) / FS
    a = 0.8 * tone(t, A_KHZ * 1e3) * ((t > 0.2) & (t < 0.6))
    b = 0.8 * tone(t, B_KHZ * 1e3) * ((t > 1.2) & (t < 1.6))
    eid = synth(st, a + b, "bursts")
    rms = lambda x, r, t0, t1: float(np.sqrt(np.mean(x[int(t0 * r):int(t1 * r)] ** 2)))
    plan, x = pcm(st, eid, "envelope")
    assert rms(x, plan["rate"], 0.3, 0.5) > 0.05 and rms(x, plan["rate"], 1.3, 1.5) > 0.05
    plan, x = pcm(st, eid, "envelope", offset_hz=A_KHZ * 1e3, bw_hz=100e3)
    assert rms(x, plan["rate"], 0.3, 0.5) > 50 * rms(x, plan["rate"], 1.3, 1.5)
    assert "100.0 kHz box" in plan["demod"]
    plan, x = pcm(st, eid, "envelope", offset_hz=B_KHZ * 1e3, bw_hz=100e3)
    assert rms(x, plan["rate"], 1.3, 1.5) > 50 * rms(x, plan["rate"], 0.3, 0.5)
    # a selection that is all signal: its own median would have gated the burst away
    plan, x = pcm(st, eid, "envelope", start_s=0.3, dur_s=0.2, offset_hz=A_KHZ * 1e3, bw_hz=100e3)
    assert np.sqrt(np.mean(x ** 2)) > 0.05


def radar_scene(width_s):
    """Chirps of this width, +/-700 kHz, every 3 ms, for 60 ms in the middle of 1 s."""
    fs, t = FS, np.arange(int(1.0 * FS)) / FS
    x = np.zeros(len(t), complex)
    n = int(width_s * fs)
    tp = np.arange(n) / fs
    pulse = 0.8 * np.exp(2j * np.pi * (-700e3 * tp + 0.5 * (1400e3 / width_s) * tp ** 2))
    starts = [int((0.5 + k * 3e-3) * fs) for k in range(20)]
    for s in starts:
        x[s:s + n] = pulse
    return x, [s / fs for s in starts]


@pytest.mark.parametrize("width_s", [100e-6, 5e-6])
def test_a_wide_box_keeps_every_short_pulse(filled, width_s):
    """AM takes x[::decim], one sample in 21 us at 48 kHz audio: a 100 us pulse
    spans about five of those, a 5 us pulse falls between them three times in four. Wide boxes take
    the max-held path, and the page is told so."""
    st, ids = filled
    x, starts = radar_scene(width_s)
    eid = synth(st, x, "radar")
    plan, y = pcm(st, eid, "am", start_s=0.45, dur_s=0.2, bw_hz=1400e3)
    assert plan["wide"] and "pulse envelope" in plan["demod"] and "too wide for AM" in plan["demod"]
    hits = np.flatnonzero(np.abs(y) > 0.3)
    groups = 1 + int(np.sum(np.diff(hits) > 10))
    assert groups == len(starts)                            # all 20 pulses, each once
    assert hits[0] / plan["rate"] + 0.45 == pytest.approx(starts[0], abs=2e-3)   # where the plot says
    assert not C.audio_plan(st, eid, "am", bw_hz=30e3)["wide"]
    assert C.audio_plan(st, eid, "envelope", bw_hz=1400e3)["demod"].startswith("pulse envelope")


def test_wide_fm_runs_its_discriminator_at_the_capture_rate(filled):
    """+/-100 kHz of FM at 1 kHz: at the 48 kHz audio rate its phase steps would wrap."""
    st, ids = filled
    t = np.arange(int(1.0 * FS)) / FS
    phase = 2 * np.pi * 200e3 * t + (100e3 / 1e3) * np.sin(2 * np.pi * 1e3 * t)
    eid = synth(st, 0.8 * np.exp(1j * phase), "wide_fm")
    plan, y = pcm(st, eid, "nfm", offset_hz=200e3, bw_hz=400e3)
    assert plan["wide"] and "capture rate" in plan["demod"]
    assert strongest_hz(y, plan["rate"]) == pytest.approx(1000, abs=15)
    assert not C.audio_plan(st, eid, "nfm")["wide"]         # the plain channel is what it always was


@pytest.mark.parametrize("mode", ["am", "nfm", "envelope"])
def test_a_selection_shorter_than_a_chunk_stops_on_time_and_opens_without_a_click(two_tones, mode):
    st, _, eid = two_tones
    plan, x = pcm(st, eid, mode, start_s=0.5, dur_s=0.1, offset_hz=A_KHZ * 1e3, bw_hz=40e3)
    assert plan["start"] == int(0.5 * FS) and plan["count"] == int(0.1 * FS)
    assert plan["samples"] == -(-plan["count"] // plan["decim"]) == 4800
    head = np.abs(x[:int(0.005 * plan["rate"])]).max()
    rest = np.percentile(np.abs(x[int(0.01 * plan["rate"]):]), 99.5)
    if mode == "am":                                        # envelope gates a steady tone, FM reads noise
        assert rest > 0.05
    assert head <= 1.5 * rest + 0.02                        # the opening is no louder than what follows
    assert abs(x[0]) < 0.05 and abs(x[-1]) < 0.05           # eased in and out: the cut edges do not click


@pytest.mark.parametrize("off_centre_hz", [0.0, 10e3])
def test_a_steady_carrier_opens_without_a_thump(filled, off_centre_hz):
    """From rest, the channel filter and the DC filter both see a step. The AGC would then set
    its level from that thump and leave everything after it near silence."""
    st, ids = filled
    t = np.arange(int(1.0 * FS)) / FS
    eid = synth(st, 0.8 * tone(t, A_KHZ * 1e3), "carrier")
    plan, x = pcm(st, eid, "am", start_s=0.3, dur_s=0.1, offset_hz=A_KHZ * 1e3 + off_centre_hz, bw_hz=40e3)
    r = plan["rate"]
    assert np.abs(x[: int(0.01 * r)]).max() < 2.5 * np.abs(x[int(0.02 * r):]).max()


def test_a_box_is_checked_in_a_sentence(filled):
    st, ids = filled
    eid = ids["written"][0]
    fs = C.audio_plan(st, eid)["fs"]
    for bw in (0.0, -5e3, float("nan"), 50.0):
        with pytest.raises(C.Refused, match="too narrow"):
            C.audio_plan(st, eid, "am", bw_hz=bw)
    with pytest.raises(C.Refused, match="reaches outside the capture's band"):
        C.audio_plan(st, eid, "am", offset_hz=fs / 4, bw_hz=fs * 0.75)
    with pytest.raises(C.Refused, match="outside the capture's band"):
        C.audio_plan(st, eid, "am", offset_hz=float("nan"))
    p = C.audio_plan(st, eid, "am", offset_hz=fs / 4, bw_hz=fs / 2)     # exactly to the edge
    assert p["bw_hz"] == fs / 2
    assert C.audio_plan(st, eid, "am")["bw_hz"] == C.AUDIO_MODES["am"]  # no box: the mode's channel
    assert C.audio_plan(st, eid, "envelope")["bw_hz"] is None


def test_private_is_refused_with_every_new_parameter_too(filled):
    st, ids = filled
    kw = dict(start_s=0.1, dur_s=0.5, offset_hz=1e3, bw_hz=5e3)
    with pytest.raises(C.Refused, match="private"):
        C.audio_plan(st, ids["private"][0], "am", **kw)
    with pytest.raises(C.Refused, match="private"):
        C.audio_plan(st, ids["private"][0], "am", bw_hz=-1.0)   # refused as private before anything else
    con = cap.connect(st)
    src = con.execute("SELECT * FROM captures WHERE iq_status = 'written'").fetchone()
    rid = cap.add_row(con, {"started": src["started"], "started_epoch": src["started_epoch"] + 2,
                            "freq_hz": 1940e6, "target_hz": 1940e6, "tier": "open", "iq_status": "written",
                            "iq_path": src["iq_path"], "iq_bytes": src["iq_bytes"],
                            "store_rate_hz": src["store_rate_hz"], "fingerprint": src["fingerprint"]})
    con.close()
    for mode in ("am", "nfm", "envelope"):
        with pytest.raises(C.Refused, match="private"):
            C.audio_plan(st, rid, mode, **kw)


def test_http_passes_a_selection_through_and_keeps_the_refusals(two_tones, monkeypatch):
    from marsad import live
    st, ids, eid = two_tones
    con = cap.connect(st)
    src = con.execute("SELECT * FROM captures WHERE id = ?", (eid,)).fetchone()
    forged = cap.add_row(con, {"started": src["started"], "started_epoch": src["started_epoch"] + 1,
                               "freq_hz": 1940e6, "target_hz": 1940e6, "tier": "open", "iq_status": "written",
                               "iq_path": src["iq_path"], "iq_bytes": src["iq_bytes"],
                               "store_rate_hz": src["store_rate_hz"]})
    con.close()
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
    sel = "start=0.5&offset_khz=300&bw_khz=40"
    r = urllib.request.urlopen(f"{base}/captures/audio?id={eid}&mode=am&{sel}&dur=0.25", timeout=30)
    body = r.read()
    plan = C.audio_plan(st, eid, "am", 0.5, 300e3, 0.25, 40e3)
    assert len(body) == plan["bytes"] == 44 + 2 * 12000
    assert r.headers["X-Marsad-Demod"] == plan["demod"] == "AM over the 40.0 kHz box"
    # the page's preflight: only the header, whatever the selection
    r = urllib.request.urlopen(f"{base}/captures/audio?id={eid}&mode=am&{sel}&dur=0", timeout=30)
    assert len(r.read()) == 44 and r.status == 200
    for q, code, word in ((f"id={ids['private'][0]}&mode=am&{sel}&dur=0.25", 403, "private"),
                          (f"id={forged}&mode=envelope&{sel}&dur=0", 403, "private"),
                          (f"id={eid}&mode=am&offset_khz=1100&bw_khz=400", 403, "reaches outside"),
                          (f"id={eid}&mode=am&bw_khz=0", 403, "too narrow"),
                          (f"id={eid}&mode=am&bw_khz=wide", 400, "")):
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(f"{base}/captures/audio?{q}", timeout=10)
        assert ei.value.code == code and word in json.loads(ei.value.read())["error"]
    assert spy.opened == []
    page = urllib.request.urlopen(base + "/", timeout=10).read().decode("utf-8")
    for needle in ('id="cs-wf-o"', 'id="cs-env-o"', 'id="cs-au-clr"', "pre.set('dur','0')", "X-Marsad-Demod"):
        assert needle in page
