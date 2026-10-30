"""The Scan tab's second-radio panel (2026-10-29): two radios run at once, the tab follows
the newest recording, and the panel shows the other one, or the live spectrum of a
`marsad watch` parked on a band.

What is pinned:
- which recording is "the other one": being written (no finished_local, grew recently,
  three write intervals for a slow scan), not the one the main view shows, newest first;
- /other: the other recording's rows and fingerprint, incremental by `since`, restarting
  from row 0 when the page has been drawing a different file; running recorders only;
- the recorder's status file carries a power spectrum of its tuning (mean and max-hold
  over ~2 s, floor, trigger = floor + threshold, recent events), and on a PRIVATE band
  it still writes that spectrum and no IQ of any kind.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from marsad import capture as cap
from marsad import live
from marsad import watch as W

from test_watch import V5, Launches, WriteSpy, files_in, scene, snapshots, station


def write_run(st, name: str, receiver: str, sweeps: int, finished: bool = False,
              integ: float = 10.0, age_s: float = 0.0, lo_mhz: float = 300.0) -> Path:
    """A small rtl_power CSV (2 hops of 8 bins) and its sidecar."""
    st.surveys.mkdir(parents=True, exist_ok=True)
    path = st.surveys / name
    lines = []
    for s in range(sweeps):
        for h in range(2):
            lo = (lo_mhz + h) * 1e6
            vals = [-40.0 + (8 if (s == sweeps - 1 and h == 1 and b == 3) else 0) for b in range(8)]
            lines.append(f"2026-10-29, 20:00:{s:02d}, {lo:.0f}, {lo + 1e6:.0f}, 125000.00, 10, "
                         + ", ".join(f"{v:.2f}" for v in vals))
    path.write_text("\n".join(lines) + "\n")
    side = {"receiver": receiver, "csv": name, "preset": "milair", "started_local": "2026-10-29T20:00:00",
            "rtl_power": {"freq_low_mhz": lo_mhz, "freq_high_mhz": lo_mhz + 2,
                          "freq_high_mhz_effective": lo_mhz + 2, "bin_khz": 125, "bin_khz_actual": 125.0,
                          "gain_db": 29.7, "integration_s": integ, "crop": 0.25, "fir": 9},
            "geometry": {"hops": 2}}
    if finished:
        side["finished_local"] = "2026-10-29T20:10:00"
    path.with_suffix(".json").write_text(json.dumps(side))
    t = time.time() - age_s
    os.utime(path, (t, t))
    return path


def test_other_is_the_newest_recording_still_being_written_that_is_not_main(tmp_path):
    st = station(tmp_path)
    write_run(st, "survey_milair_1h_old_20261029-1500.csv", "xtr", 3, finished=True, age_s=50)
    write_run(st, "survey_milair_1h_dead_20261029-1600.csv", "v5", 3, age_s=900)   # killed: never finished
    v5 = write_run(st, "survey_milair_1h_vfive_20261029-2000.csv", "v5", 3, age_s=20)
    xtr = write_run(st, "survey_milair_1h_xtr_20261029-2001.csv", "xtr", 3, age_s=5)
    assert live.recording_active(xtr) and live.recording_active(v5)
    assert not live.recording_active(st.surveys / "survey_milair_1h_old_20261029-1500.csv")
    assert not live.recording_active(st.surveys / "survey_milair_1h_dead_20261029-1600.csv")
    # the main view follows the newest, so the panel shows the v5's run
    assert live.other_recording(st, xtr.name) == v5
    # the owner opened the v5's run in the main view: the panel shows the XTR's
    assert live.other_recording(st, v5.name) == xtr
    # one radio alone: nothing for the panel
    os.utime(v5, (time.time() - 900,) * 2)
    assert live.other_recording(st, xtr.name) is None


def test_main_view_follows_the_newest_started_run_not_the_last_flushed(tmp_path):
    """Two radios flush their files in turn, so the one modified last changes every few
    seconds; following it would flip the main view between the runs."""
    st = station(tmp_path)
    first = write_run(st, "survey_milair_1h_a_20261029-2000.csv", "v5", 2)
    time.sleep(0.05)
    second = write_run(st, "survey_LS_1h_b_20261029-2001.csv", "xtr", 2, age_s=30)
    os.utime(first, (time.time(),) * 2)                   # the older run flushed just now
    assert live.newest_recording(st) == second
    assert live.other_recording(st, second.name) == first


def test_a_slow_scan_stays_active_for_three_write_intervals(tmp_path):
    st = station(tmp_path)
    slow = write_run(st, "survey_LS_6h_slow_20261029-1200.csv", "xtr", 2, integ=600, age_s=1500)
    assert live.recording_active(slow)                     # 25 min < 3 x 10 min
    os.utime(slow, (time.time() - 1900,) * 2)
    assert not live.recording_active(slow)


def test_other_payload_is_incremental_and_restarts_on_a_new_file(tmp_path):
    st = station(tmp_path)
    live.OTHER_TAILERS.clear()
    v5 = write_run(st, "survey_milair_1h_vfive_20261029-2000.csv", "v5", 4, age_s=10)
    xtr = write_run(st, "survey_milair_1h_xtr_20261029-2001.csv", "xtr", 2, age_s=1, lo_mhz=1300)
    d = live.other_payload(st, xtr.name)
    o = d["other"]
    assert o["file"] == v5.name and o["meta"]["receiver"] == "v5" and o["cols"] == live.OTHER_COLS
    assert o["fingerprint"].startswith("300.000-302.000 MHz | 125.0000 kHz | g29.7")
    assert o["from"] == 0 and len(o["rows"]) >= 3 and o["lo"] == pytest.approx(300.0)
    have = o["total"]
    again = live.other_payload(st, xtr.name, since=have, file=v5.name)["other"]
    assert again["rows"] == [] and again["from"] == have        # nothing new
    # the page was drawing some other file: start again from row 0
    fresh = live.other_payload(st, xtr.name, since=have, file="survey_x.csv")["other"]
    assert fresh["from"] == 0 and len(fresh["rows"]) == len(o["rows"])
    assert d["recorders"] == []


def test_other_endpoint_over_http_with_a_running_recorder(tmp_path):
    st = station(tmp_path)
    live.OTHER_TAILERS.clear()
    write_run(st, "survey_milair_1h_vfive_20261029-2000.csv", "v5", 3, age_s=10)
    write_run(st, "survey_milair_1h_xtr_20261029-2001.csv", "xtr", 3, age_s=1)
    pool = cap.pool_dir(st, cap.settings(st))
    pool.mkdir(parents=True, exist_ok=True)
    spec = {"tuned_mhz": 433.9, "lo_mhz": 433.78, "hi_mhz": 434.02, "mean_db": [-50.0] * 256,
            "max_db": [-45.0] * 256, "floor_db": -50.0, "trigger_db": -38.0, "threshold_db": 12.0,
            "watch_mhz": [433.91, 433.93], "hold_s": 2.0}
    (pool / "recorder_status_27825674.json").write_text(json.dumps(
        {"state": "running", "updated": time.time(), "receiver": "v5", "serial": "27825674",
         "mode": "fixed watch", "events": 1, "spectrum": spec, "recent_events": []}))
    (pool / "recorder_status_08701076.json").write_text(json.dumps(
        {"state": "running", "updated": time.time() - 60, "receiver": "xtr", "serial": "08701076"}))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    threading.Thread(target=live.main, args=(["--port", str(port)], st), daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            urllib.request.urlopen(base + "/other", timeout=2).read()
            break
        except OSError:
            time.sleep(0.1)
    d = json.loads(urllib.request.urlopen(base + "/other").read())      # main: the newest
    assert d["other"]["file"] == "survey_milair_1h_vfive_20261029-2000.csv"
    assert [r["serial"] for r in d["recorders"]] == ["27825674"]        # the silent one is gone
    assert d["recorders"][0]["spectrum"]["trigger_db"] == -38.0
    d = json.loads(urllib.request.urlopen(
        base + "/other?main=survey_milair_1h_vfive_20261029-2000.csv").read())
    assert d["other"]["file"] == "survey_milair_1h_xtr_20261029-2001.csv"
    page = urllib.request.urlopen(base + "/").read().decode("utf-8")
    assert 'id="ot"' in page and "setInterval(otPoll" in page


def test_fixed_watch_status_carries_a_power_spectrum_and_recent_events(tmp_path, monkeypatch):
    st = station(tmp_path, {"capture_s": 4})
    cfg = cap.settings(st)
    got = snapshots(monkeypatch)
    L = Launches(scene(12.0, 0.4, 2.5, kind="fsk"), 433.92e6)
    rec = W.Recorder(st, cfg, V5, ["rtl_sdr"], "c", 29.7, L, quiet=True, inline_characterise=True)
    W.fixed_watch(rec, 433.92e6, 11.0, width_hz=20e3)
    specs = [g["spectrum"] for g in got if g.get("spectrum")]
    assert specs, "no status carried a spectrum"
    sp = specs[-1]
    assert len(sp["mean_db"]) == len(sp["max_db"]) <= cap.LIVE_COLS
    assert all(m >= a - 1e-6 for m, a in zip(sp["max_db"], sp["mean_db"]))     # max-hold over the mean
    assert sp["trigger_db"] == pytest.approx(sp["floor_db"] + 12.0, abs=0.11)
    assert sp["lo_mhz"] < sp["watch_mhz"][0] < 433.92 < sp["watch_mhz"][1] < sp["hi_mhz"]
    assert sp["hold_s"] == pytest.approx(cap.LIVE_HOLD_S, abs=0.1)
    # the events show where they happened, up to RECENT_EVENTS of them
    ev = got[-1]["recent_events"]
    assert ev and len(ev) <= W.RECENT_EVENTS and all(abs(e["mhz"] - 433.92) < 0.05 for e in ev)
    assert got[-1]["spectrum"] is None                  # stopped: nothing is listening


def test_private_band_status_has_the_spectrum_and_still_no_iq(tmp_path, monkeypatch):
    st = station(tmp_path / "st")
    cfg = cap.settings(st)
    f = 880.5e6                                              # 850 cellular downlink
    assert cap.band_tier(f, cfg)["tier"] == "private"

    def forbidden(*a, **k):
        raise AssertionError("an IQ writer was created for a private band")
    monkeypatch.setattr(cap, "IQWriter", forbidden)
    monkeypatch.setattr(cap, "Cutter", forbidden)
    got = snapshots(monkeypatch)
    before = files_in(tmp_path)
    spy = WriteSpy(monkeypatch)
    rec = W.Recorder(st, cfg, V5, ["rtl_sdr"], "c", 29.7, Launches(scene(8.0, 0.6, 2.5, kind="fsk"), f), quiet=True)
    W.fixed_watch(rec, f, 8.0, width_hz=20e3)
    new = [p for p in files_in(tmp_path) if p not in before]
    assert not any(p.endswith((".sigmf-data", ".sigmf-meta", ".cu8", ".cs16", ".wav", ".raw", ".iq"))
                   for p in new + spy.opened)
    sp = [g["spectrum"] for g in got if g.get("spectrum")]
    assert sp and got[0]["serial"] == "27825674"
    # power per column and a few scalars: nothing else, and nothing like the samples
    assert set(sp[-1]) == {"at", "tuned_mhz", "lo_mhz", "hi_mhz", "mean_db", "max_db", "hold_s",
                           "watch_mhz", "floor_db", "trigger_db", "threshold_db"}
    assert all(isinstance(v, float) for v in sp[-1]["mean_db"] + sp[-1]["max_db"])
    assert len(json.dumps(sp[-1])) < 8.0 * 240e3 / 100
