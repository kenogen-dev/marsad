"""The triggered recorder (marsad/capture.py, marsad/watch.py), the recorder design phases 2 and 3.

No dongle is ever opened: rtl_sdr is replaced by a process (tests/fake_rtl_sdr.py)
or a function that replays synthetic uint8 IQ, rtl_eeprom by a canned detection,
and the `marsad run` child by a thread that writes a survey CSV pass by pass.

What is pinned:
- band tiers, frequency correction, gain snapping, capture planning;
- the cutter (shift + filter + decimate) is continuous across blocks;
- fixed watch on synthetic bursts: one event per burst, one SigMF capture per
  cooldown, characterised, its recipe used for the next tuning;
- PRIVATE bands: no IQ file of any kind, not even a temp file; only a 20 ms x
  ~1 kHz spectrogram reaches disk; ANOMALY for a CW carrier and a pulse train;
- a full pool or day stops IQ but not cataloguing, and deletes nothing;
- the trigger rules (2 passes, 12 dB, artefacts, cooldown, daily reference, ambush)
  and the dispatcher's pre-emption;
- the whole survey + recorder loop end to end on a synthetic survey;
"""
from __future__ import annotations

import builtins
import io
import json
import os
import struct
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from marsad import capture as cap
from marsad import watch as W
from marsad.config import Station

HERE = Path(__file__).resolve().parent
FAKE = HERE / "fake_rtl_sdr.py"
R820T = {"role": "r820t", "serial": "00000202", "device": 0, "tuner": "r820t"}


def station(tmp_path: Path, recorder: dict | None = None) -> Station:
    tmp_path.mkdir(parents=True, exist_ok=True)
    rec = {"serials": {"e4000": "00000101", "r820t": "00000202"},
           "roles": {"surveyor": "e4000", "recorder": "r820t"}, "look_s": 0, **(recorder or {})}
    (tmp_path / "station.json").write_text(json.dumps({"recorder": rec}))
    return Station.load(tmp_path)


def cu8(x: np.ndarray, noise: float = 0.02, seed: int = 1) -> bytes:
    rng = np.random.default_rng(seed)
    x = x + noise * (rng.standard_normal(len(x)) + 1j * rng.standard_normal(len(x)))
    iq = np.empty(2 * len(x))
    iq[0::2], iq[1::2] = x.real, x.imag
    return np.clip(np.round(127.5 + 127.5 * iq), 0, 255).astype(np.uint8).tobytes()


def bursts(fs: float, dur: float, f_off: float, on_s: float, every_s: float, amp: float = 0.3,
           first_s: float = 1.0, kind: str = "tone") -> np.ndarray:
    n = int(fs * dur)
    t = np.arange(n) / fs
    if kind == "fsk":
        sym = (np.floor(t * 1200) % 2) * 2 - 1
        ph = 2 * np.pi * np.cumsum(f_off + 2400 * sym) / fs
    else:
        ph = 2 * np.pi * f_off * t
    gate = ((t - first_s) % every_s < on_s) & (t >= first_s)
    return (amp * gate * np.exp(1j * ph)).astype(np.complex64)


class Launches:
    """A launch() for the Recorder: a radio that hears one scene, wherever it is tuned.

    `scene(off_hz, rate_hz)` returns complex baseband for a signal sitting off_hz above
    the tuned frequency; each launch reads -f and -s from the rtl_sdr command line,
    so a retune from a recipe really moves the signal in the stream. Records each command.
    """

    def __init__(self, scene, signal_hz: float):
        self.scene, self.signal_hz, self.cmds = scene, signal_hz, []

    def __call__(self, cmd, log_path):
        self.cmds.append(cmd)
        tune, rate = float(cmd[cmd.index("-f") + 1]), float(cmd[cmd.index("-s") + 1])
        return io.BytesIO(cu8(self.scene(self.signal_hz - tune, rate))), (lambda: None)


def scene(dur, on_s, every_s, kind="tone", first_s=1.0, amp=0.3):
    return lambda off, fs: bursts(fs, dur, off, on_s, every_s, amp=amp, first_s=first_s, kind=kind)


def det(found):
    return lambda st: {"found": found, "error": None}


# ---------------------------------------------------------------- small pieces

def test_band_tiers_default_table(tmp_path):
    cfg = cap.settings(station(tmp_path))
    assert cap.band_tier(880e6, cfg)["tier"] == "private"            # 850 downlink
    assert cap.band_tier(1940e6, cfg)["tier"] == "private"           # PCS downlink
    assert cap.band_tier(1925e6, cfg)["band"].startswith("DECT")
    assert cap.band_tier(930e6, cfg)["band"] == "pagers"
    assert cap.band_tier(390e6, cfg)["tier"] == "private"            # trunked land-mobile
    assert cap.band_tier(162.475e6, cfg)["tier"] == "open"           # weather radio inside VHF land-mobile
    for f in (1300.00e6, 340.31e6, 433.92e6, 299.999e6, 1090e6):
        assert cap.band_tier(f, cfg)["tier"] == "open"               # anything not listed is open


def test_station_json_overrides_one_setting_and_keeps_the_rest(tmp_path):
    cfg = cap.settings(station(tmp_path, {"pool_gb": 5, "pool": "D:/caps"}))
    assert cfg["pool_gb"] == 5 and cfg["daily_gb"] == 20 and cfg["band_tiers"]
    assert cap.pool_dir(station(tmp_path), None) == tmp_path / "recordings" / "captures"


def test_ppm_correction_and_gain_ladder():
    # a radio reading 0.52 ppm high must be asked for less
    assert cap.corrected(385e6, 0.52) == pytest.approx(385e6 - 200.2, abs=0.5)
    assert cap.corrected(385e6, 0.0) == 385e6
    assert cap.snap_gain("r820t", 23.7) == 22.9
    assert cap.snap_gain("e4000", 30) == 29.0


def test_plan_capture_zero_if_offset_and_recipe_gain():
    p = cap.plan_capture(433.92e6, 20e3, "e4000")
    assert p["rtl_rate_hz"] == 240e3 and p["signal_hz"] - p["tune_hz"] == pytest.approx(30e3)
    assert p["store_rate_hz"] >= 2.5 * 20e3 and p["rtl_rate_hz"] % p["store_rate_hz"] == 0
    full = cap.plan_capture(1300.00e6, None, "r820t", gain_db=29.7)
    assert full["tune_hz"] == 1300.00e6 and full["store_rate_hz"] == 2.4e6   # r820t: centred, all kept
    e4000 = cap.plan_capture(1300.00e6, None, "e4000")
    assert e4000["signal_hz"] - e4000["tune_hz"] == pytest.approx(600e3)          # off the DC spike
    r = {"rtl_rate_hz": 240e3, "store_rate_hz": 50e3, "signal_above_tune_hz": 30e3,
         "gain": {"change_db": -6}, "gain_db_used": 29.7, "occupied_hz": 20e3}
    a = cap.plan_capture(433.92e6, None, "r820t", r, gain_db=40.2)
    assert a["gain_db"] == 22.9                     # 29.7 - 6, not 40.2 - 6: no oscillation
    assert a["decim"] == 4 and a["width_hz"] == pytest.approx(24e3)


def test_rtl_sdr_command_uses_the_serial_and_the_correction():
    p = cap.plan_capture(162.55e6, 12e3, "r820t", gain_db=29.7)
    cmd = cap.rtl_sdr_command("rtl_sdr", "00000202", p, 1.0)
    assert cmd[:3] == ["rtl_sdr", "-d", "00000202"] and cmd[-1] == "-"
    assert int(cmd[cmd.index("-f") + 1]) == round(p["tune_hz"] / (1 + 1e-6))


def test_cutter_is_continuous_across_blocks():
    fs, off, d = 240e3, 30e3, 4
    x = bursts(fs, 0.5, off + 1e3, 1.0, 1.0, first_s=0.0)        # a steady tone 1 kHz above the signal
    whole = cap.Cutter(fs, off, d)(x)
    c = cap.Cutter(fs, off, d)
    parts = np.concatenate([c(x[i:i + 4567]) for i in range(0, len(x), 4567)])
    assert np.abs(whole.astype(int) - parts.astype(int)).max() <= 1    # single-precision FFT rounding at most
    z = (parts[0::2] + 1j * parts[1::2])[200:] / 32768
    f = np.fft.fftfreq(len(z), d / fs)
    assert abs(f[np.argmax(np.abs(np.fft.fft(z)))] - 1e3) < 30    # signal moved to 0 Hz, tone at +1 kHz
    assert np.abs(z).mean() == pytest.approx(0.3, rel=0.05)       # passband gain 1


def test_receiver_found_by_serial_never_by_index(tmp_path):
    st = station(tmp_path)
    cfg = cap.settings(st)
    found = [{"device": 0, "product": "Generic E4000", "serial": "00000101", "tuner": "E4000"},
             {"device": 1, "product": "Generic R820T", "serial": "00000202", "tuner": "R820T"}]
    assert W.resolve_receiver(st, "r820t", cfg, det(found))["device"] == 1
    with pytest.raises(SystemExit, match="00000202"):
        W.resolve_receiver(st, "r820t", cfg, det(found[:1]))


def test_no_serials_one_dongle_is_found_whatever_it_is(tmp_path):
    (tmp_path / "station.json").write_text("{}")
    st = Station.load(tmp_path)
    cfg = cap.settings(st)
    assert cfg["serials"] == {}
    one = [{"device": 0, "product": "Generic", "serial": "00000001", "tuner": "Rafael Micro R820T"}]
    got = W.resolve_receiver(st, "r820t", cfg, det(one))
    assert got["serial"] == "00000001" and got["device"] == 0
    # the only dongle plugged in, even if the station's name for it says another tuner
    assert W.resolve_receiver(st, "e4000", cfg, det(one))["device"] == 0
    # a blank serial leaves the index, which rtl_sdr -d also takes
    blank = [{**one[0], "device": 2, "serial": ""}]
    assert W.resolve_receiver(st, "r820t", cfg, det(blank))["serial"] == "2"


def test_no_serials_two_dongles_are_told_apart_by_tuner_or_refused_plainly(tmp_path):
    (tmp_path / "station.json").write_text("{}")
    st = Station.load(tmp_path)
    cfg = cap.settings(st)
    mixed = [{"device": 0, "product": "A", "serial": "1", "tuner": "Elonics E4000"},
             {"device": 1, "product": "B", "serial": "2", "tuner": "Rafael Micro R828D"}]
    assert W.resolve_receiver(st, "r820t", cfg, det(mixed))["device"] == 1
    assert W.resolve_receiver(st, "e4000", cfg, det(mixed))["device"] == 0
    twins = [{"device": 0, "product": "A", "serial": "00000001", "tuner": "R820T"},
             {"device": 1, "product": "B", "serial": "00000001", "tuner": "R820T"}]
    with pytest.raises(SystemExit, match=r"recorder.*serials"):
        W.resolve_receiver(st, "r820t", cfg, det(twins))
    with pytest.raises(SystemExit, match="no RTL-SDR"):
        W.resolve_receiver(st, "r820t", cfg, lambda s: {"found": [], "error": None})


def test_survey_roles_default_to_the_first_two_receivers(tmp_path):
    (tmp_path / "station.json").write_text("{}")
    st = Station.load(tmp_path)
    cfg = cap.settings(st)
    assert W.survey_roles(st, cfg, None, None) == ("r820t", "e4000")
    assert W.survey_roles(st, {**cfg, "roles": {"surveyor": "e4000", "recorder": "r820t"}}, None, None) == ("e4000", "r820t")
    with pytest.raises(SystemExit, match="two receivers"):
        W.survey_roles(st, cfg, "r820t", "r820t")


# ---------------------------------------------------------------- phase 2, synthetic

def test_fixed_watch_events_capture_characterise_and_retune(tmp_path):
    st = station(tmp_path, {"capture_s": 6, "cooldown_min": 30})
    cfg = cap.settings(st)
    f = 433.92e6
    plan0 = cap.plan_capture(f, 20e3, "r820t")
    L = Launches(scene(20.0, 0.4, 2.5, kind="fsk"), f)
    rec = W.Recorder(st, cfg, R820T, ["rtl_sdr"], "test chain", 29.7, L, quiet=True,
                     inline_characterise=True)
    tot = W.fixed_watch(rec, f, 19.0, width_hz=20e3)
    rows = rec.con.execute("SELECT * FROM captures ORDER BY started_epoch").fetchall()
    assert len(rows) >= 7                                   # one event per 2.5 s burst
    for r in rows[:7]:
        assert r["duration_s"] == pytest.approx(0.4, abs=0.1)
        assert r["snr_peak_db"] > 20 and r["tier"] == "open" and r["flags"] is None
        assert Path(r["fingerprint"]).exists()
    written = [r for r in rows if r["iq_status"] == "written"]
    assert len(written) == 1 and tot["captures"] == 1        # one per 30 min cooldown
    w = written[0]
    meta = json.loads(Path(w["iq_path"]).with_suffix(".sigmf-meta").read_text())
    g = meta["global"]
    assert g["core:datatype"] == "ci16_le" and g["core:sample_rate"] == plan0["store_rate_hz"]
    assert meta["captures"][0]["core:frequency"] == f
    assert meta["captures"][0]["core:datetime"].endswith("Z")
    assert g["marsad:serial"] == "00000202" and g["marsad:chain"] == "test chain"
    assert g["marsad:gain_db"] == 29.7 and meta["annotations"]
    n = Path(w["iq_path"]).stat().st_size // 4
    assert n / g["core:sample_rate"] == pytest.approx(6 + cap.PRE_S, abs=0.2)
    assert w["recipe"] and w["family"] and Path(w["characterised"]).exists()
    assert json.loads(w["recipe"])["gain_db_used"] == 29.7
    assert [r["iq_status"] for r in rows[1:]].count("cooldown") >= 3
    assert len(L.cmds) == 2                                  # retuned once from the recipe
    assert tot["sessions"] == 2 and tot["plan"]["source"] == "recipe"


def test_full_pool_keeps_cataloguing_writes_no_iq_and_deletes_nothing(tmp_path, capsys):
    st = station(tmp_path, {"pool_gb": 1e-6})               # 1 kB pool
    cfg = cap.settings(st)
    keep = cap.pool_dir(st, cfg) / "old" / "an_old_capture.sigmf-data"
    keep.parent.mkdir(parents=True)
    keep.write_bytes(b"x" * 5000)
    rec = W.Recorder(st, cfg, R820T, ["rtl_sdr"], "c", 29.7, Launches(scene(8.0, 0.4, 2.0), 433.92e6), quiet=True)
    W.fixed_watch(rec, 433.92e6, 8.0, width_hz=20e3)
    rows = rec.con.execute("SELECT iq_status, fingerprint FROM captures").fetchall()
    assert len(rows) >= 3 and {r["iq_status"] for r in rows} == {"pool full"}
    assert all(Path(r["fingerprint"]).exists() for r in rows)
    assert keep.exists() and keep.stat().st_size == 5000
    assert not list(cap.pool_dir(st, cfg).rglob("cap_*.sigmf-data"))
    assert "IQ writing stopped" in capsys.readouterr().out


# ---------------------------------------------------------------- private bands

class WriteSpy:
    """Every file opened for writing while it is installed, with the bytes written."""

    def __init__(self, monkeypatch):
        self.opened = []
        real = builtins.open

        def spy(file, mode="r", *a, **k):
            if any(c in mode for c in "wax+"):
                self.opened.append(str(file))
            return real(file, mode, *a, **k)
        monkeypatch.setattr(builtins, "open", spy)
        monkeypatch.setattr(io, "open", spy)


def files_in(*dirs) -> dict:
    out = {}
    for d in dirs:
        for p in Path(d).rglob("*"):
            if p.is_file():
                out[str(p)] = p.stat().st_size
    return out


def test_private_band_never_writes_iq_not_even_a_temp_file(tmp_path, monkeypatch):
    st = station(tmp_path / "st")
    cfg = cap.settings(st)
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmpdir))
    f = 880.5e6                                              # 850 cellular downlink
    assert cap.band_tier(f, cfg)["tier"] == "private"
    L = Launches(scene(8.0, 0.6, 2.5, kind="fsk"), f)

    def forbidden(*a, **k):
        raise AssertionError("an IQ writer was created for a private band")
    monkeypatch.setattr(cap, "IQWriter", forbidden)
    monkeypatch.setattr(cap, "Cutter", forbidden)
    before = files_in(tmp_path)
    spy = WriteSpy(monkeypatch)
    rec = W.Recorder(st, cfg, R820T, ["rtl_sdr"], "c", 29.7, L, quiet=True)
    W.fixed_watch(rec, f, 8.0, width_hz=20e3)
    after = files_in(tmp_path)
    new = {p: s for p, s in after.items() if p not in before}
    rows = rec.con.execute("SELECT * FROM captures").fetchall()
    assert len(rows) >= 3
    for r in rows:
        assert r["tier"] == "private" and cap.PRIVATE in r["flags"]
        assert r["iq_status"] == "private: not kept" and r["iq_path"] is None
    # what reached disk: the catalogue, rtl_sdr's log path is never opened (launch is mocked),
    # and fingerprints only
    assert not any(p.endswith((".sigmf-data", ".sigmf-meta", ".cu8", ".cs16", ".wav", ".raw", ".iq"))
                   for p in list(new) + spy.opened)
    assert not files_in(tmpdir)
    fps = [p for p in new if p.endswith(".fp.npz")]
    assert len(fps) == len(rows)
    assert all(p.endswith((".fp.npz", "occupancy.db", "occupancy.db-journal", "recorder_status_00000202.json"))
               for p in new)
    status = [p for p in new if p.endswith("recorder_status_00000202.json")]
    assert all(new[p] < 2000 and json.loads(Path(p).read_text())["state"] for p in status)  # a status line, no samples
    streamed = 8.0 * 240e3
    for p in fps:
        z = np.load(p)
        assert float(z["dt_s"]) >= 0.019                    # 20 ms rows: cannot be demodulated
        assert np.diff(z["f_rel_hz"]).min() >= 900           # ~1 kHz bins
        assert z["spec_db"].dtype == np.float16
        assert sum(z[k].size for k in z.files) < streamed / 20   # nothing like the samples
        assert "iq" not in " ".join(z.files)


def test_private_band_survey_trigger_is_fingerprint_only(tmp_path, monkeypatch):
    st = station(tmp_path)
    cfg = cap.settings(st)
    monkeypatch.setattr(cap, "IQWriter", lambda *a, **k: (_ for _ in ()).throw(AssertionError("IQ")))
    f = 386.5e6
    L = Launches(scene(4.0, 4.0, 10.0, first_s=0.0), f)
    rec = W.Recorder(st, cfg, R820T, ["rtl_sdr"], "c", 29.7, L, quiet=True)
    job = {"kind": "capture", "f_mhz": 386.5, "snr_db": 18.0, "width_khz": 12.5, "why": "2 passes",
           "stop": threading.Event()}
    W.survey_job(rec, st, "survey_x", 0.0)(job)
    r = rec.con.execute("SELECT * FROM captures").fetchone()
    assert r["mode"] == "now" and r["captured"] == 1 and r["iq_path"] is None
    assert r["flags"].startswith(cap.PRIVATE) and cap.ANOMALY in r["flags"]   # a CW carrier there
    assert "CW" in r["notes"]


def test_anomaly_rules():
    band = {"band": "PCS cellular downlink", "min_bw_khz": 150}
    cfg = {"cw_max_khz": 3.0}
    ev = {"bandwidth_hz": 1.2e3, "duty": 1.0, "duration_s": 5.0}
    assert "CW" in cap.anomaly(ev, band, cfg)
    ev = {"bandwidth_hz": 1.1e6, "duty": 0.2, "duration_s": 5.0, "bursts": 8, "burst_period_s": 0.5}
    assert "pulse train" in cap.anomaly(ev, band, cfg)
    ev = {"bandwidth_hz": 10e3, "duty": 0.5, "duration_s": 5.0}
    assert "150 kHz" in cap.anomaly(ev, band, cfg)
    ev = {"bandwidth_hz": 1.4e6, "duty": 0.6, "duration_s": 5.0, "burst_period_s": None}
    assert cap.anomaly(ev, band, cfg) is None                 # LTE-shaped: what the band is for


def test_pulse_train_fingerprint_is_flagged(tmp_path):
    st = station(tmp_path)
    cfg = cap.settings(st)
    f = 1940e6                                                 # PCS downlink (r820t cannot tune it; plan only)
    plan = cap.plan_capture(f, 20e3, "r820t")
    plan["signal_hz"] = f
    sig = bursts(plan["rtl_rate_hz"], 6.0, plan["signal_hz"] - plan["tune_hz"], 0.1, 0.5, first_s=0.6)
    s = cap.Session(plan, "private", mode="now", duration_s=6.0, capture_s=6.0)
    for i in range(0, len(sig) - s.block, s.block):
        s.feed(sig[i:i + s.block] + 0.01 * (np.random.default_rng(i).standard_normal(s.block) * (1 + 1j)))
    s.finish()
    ev = s.events[0]
    why = cap.anomaly(ev, cap.band_tier(f, cfg), cfg)
    assert ev["burst_period_s"] == pytest.approx(0.5, abs=0.03) and "pulse train" in why


# ---------------------------------------------------------------- phase 3: trigger rules

def d(f, snr, tags=(), w=12.5):
    return {"f_mhz": f, "snr_db": snr, "width_khz": w, "tags": list(tags), "tol_khz": 18.75}


def test_triggers_two_passes_threshold_artefacts_cooldown():
    t = W.Triggers({"threshold_db": 12, "passes": 2, "ambush_db": 20, "cooldown_min": 30,
                    "reference_carriers_mhz": [162.55, 299.999]})
    now = 1e9
    assert t.feed([d(340.31, 14), d(305.6, 30, ["14.4 MHz SDR clock harmonic (x24)"]), d(350.0, 11)], now) == []
    a = t.feed([d(340.3125, 15), d(305.6, 30, ["14.4 MHz x24"]), d(350.0, 11.5)], now + 60)
    assert [(x["kind"], x["f_mhz"]) for x in a] == [("capture", 340.3125)]
    assert t.feed([d(340.31, 15)], now + 120) == []            # cooldown
    b = t.feed([d(340.31, 15)], now + 120 + 32 * 60)
    assert b and b[0]["kind"] == "capture"                      # the 30 min have passed
    assert t.feed([d(500.0, 14)], now) == []                    # a gap in passes resets the count
    assert t.feed([d(600.0, 14)], now) == [] and t.feed([], now) == [] and t.feed([d(600.0, 14)], now) == []


def test_triggers_hop_centre_and_hop_wide_never_fire():
    t = W.Triggers({"threshold_db": 12, "passes": 2})
    for _ in range(3):
        assert t.feed([d(336.0, 25, ["hop centre: zero-IF DC artefact"]),
                       d(340.0, 25, ["hop-wide (1.0 hops): impulse"])], 1e9) == []


def test_reference_carrier_once_a_day_and_never_an_artefact():
    caught = []
    t = W.Triggers({"threshold_db": 12, "passes": 2, "reference_carriers_mhz": [299.999]},
                   last_handled=lambda f, tol, w=0.0: caught[-1] if caught else None)
    tag = ["100 MHz PCIe clock harmonic (x3)"]
    day = time.mktime((2031, 10, 28, 9, 0, 0, 0, 0, -1))
    t.feed([d(299.999, 15, tag)], day)
    a = t.feed([d(299.999, 15, tag)], day + 60)
    assert a and a[0]["reference"]
    caught.append(day + 60)
    t.fired.clear()
    assert t.feed([d(299.999, 15, tag)], day + 3 * 3600) == []          # same day
    assert t.feed([d(299.999, 15, tag)], day + 16 * 3600)                 # 01:00 next day


def test_a_broad_hump_whose_peak_wanders_is_one_signal(tmp_path):
    """A ~650 kHz pulsed hump whose peak bin landed somewhere
    else each pass must not fire a capture every pass. Same signal = overlapping bands."""
    rng = np.random.default_rng(3)
    t = W.Triggers({"threshold_db": 12, "passes": 2, "ambush_db": 40, "cooldown_min": 30})
    t0, fired = 1e9, []
    for n in range(30):                                          # one pass a minute, 30 min
        hump = 305.5 + rng.uniform(-0.3, 0.3)
        fired += t.feed([d(hump, 18, w=651.0), d(347.5, 15)], t0 + 60 * n)
    assert sum(1 for a in fired if abs(a["f_mhz"] - 305.5) < 0.4) == 1
    assert sum(1 for a in fired if a["f_mhz"] == 347.5) == 1     # a narrow signal 2 MHz away still gets its own
    # and after a restart, the catalogue row alone holds the cooldown across the hump
    con = cap.connect(station(tmp_path))
    cap.add_row(con, {"started": "2031-10-30T16:57:16", "started_epoch": t0, "freq_hz": 305.5414e6,
                      "target_hz": 305.5414e6, "bandwidth_hz": 651e3, "captured": 1})
    assert cap.last_captured(con, 305.1865e6, 25e3, 651e3) == t0           # 355 kHz away, bands overlap
    assert cap.last_captured(con, 305.8054e6, 25e3, 600e3) == t0
    assert cap.last_captured(con, 347.5e6, 25e3, 12.5e3) is None
    assert cap.last_captured(con, 305.60e6, 25e3, 0) == t0                 # inside the row's own band
    assert cap.last_captured(con, 305.90e6, 25e3, 0) is None               # outside it
    con.close()


def test_dispatcher_treats_overlapping_bands_as_one_job():
    started = []
    disp = W.Dispatcher(lambda job: (started.append(job["f_mhz"]), job["stop"].wait(0.3)))
    disp.submit({"kind": "ambush", "f_mhz": 305.54, "snr_db": 25, "width_khz": 651})
    time.sleep(0.1)
    disp.submit({"kind": "ambush", "f_mhz": 305.19, "snr_db": 26, "width_khz": 640})   # same hump: already parked
    disp.submit({"kind": "capture", "f_mhz": 347.5, "snr_db": 15, "width_khz": 12.5})
    disp.close(5)
    assert started == [305.54, 347.5]


def test_ambush_on_one_strong_pass():
    t = W.Triggers({"threshold_db": 12, "passes": 2, "ambush_db": 20})
    a = t.feed([d(360.123, 25), d(361.0, 15)], 1e9)
    assert [(x["kind"], x["f_mhz"]) for x in a] == [("ambush", 360.123)]


def test_dispatcher_capture_preempts_ambush():
    log = []

    def run(job):
        log.append(("start", job["kind"]))
        job["stop"].wait(3 if job["kind"] == "ambush" else 0.05)
        log.append(("end", job["kind"], job["stop"].is_set()))
    disp = W.Dispatcher(run)
    disp.submit({"kind": "ambush", "f_mhz": 360.0, "snr_db": 25})
    time.sleep(0.3)
    t0 = time.time()
    disp.submit({"kind": "capture", "f_mhz": 340.31, "snr_db": 15})
    disp.close(5)
    assert time.time() - t0 < 2
    assert log == [("start", "ambush"), ("end", "ambush", True), ("start", "capture"), ("end", "capture", False)]


def test_pass_detections_floor_and_tags():
    step = 12.5e3
    keys = np.arange(335000, 345000, 12.5)                # kHz
    rng = np.random.default_rng(0)
    vals = -40 + rng.normal(0, 0.5, len(keys))
    latest = {float(k): float(v) for k, v in zip(keys, vals)}
    latest[340312.5] = -25.0                               # a signal
    latest[336000.0] = -20.0                               # a hop centre
    hc = [336e6, 338e6, 340e6, 342e6, 344e6]
    out = W.pass_detections(latest, step, hc, 12, 2.0, W.trigger_families(Station.load(HERE)))
    by = {round(x["f_mhz"], 4): x for x in out}
    assert by[340.3125]["snr_db"] == pytest.approx(15, abs=1.5) and not by[340.3125]["tags"]
    assert any("hop centre" in t for t in by[336.0]["tags"])


# ---------------------------------------------------------------- phase 3 end to end

def write_survey(path: Path, passes: list, hops, bins: int, step: float, delay: float):
    """A fake `marsad run`: sidecar first, then one rtl_power CSV pass at a time."""
    path.with_suffix(".json").write_text(json.dumps({
        "csv": path.name, "geometry": {"hops": len(hops)},
        "rtl_power": {"freq_low_mhz": hops[0][0] / 1e6, "freq_high_mhz": (hops[-1][0] + bins * step) / 1e6}}))
    rng = np.random.default_rng(3)
    with open(path, "w") as fh:
        for p, lines in enumerate(passes):
            for lo, _ in hops:
                f = lo + step * np.arange(bins)
                v = -40 + rng.normal(0, 0.4, bins)
                v[bins // 2] = -28                         # rtl_power's hop-centre DC spike, 12 dB
                for fl, snr in lines:
                    k = int(round((fl - lo) / step))
                    if 0 <= k < bins:
                        v[k] = -40 + snr
                fh.write(f"2031-10-28, 09:00:{p:02d}, {lo:.0f}, {lo + bins * step:.0f}, {step:.2f}, 100, "
                         + ", ".join(f"{x:.2f}" for x in v) + "\n")
            fh.flush()
            time.sleep(delay)


class FakeRun:
    """Stands in for the `marsad run` child process."""

    def __init__(self, st: Station, passes):
        self.cmd = None
        self.returncode = None
        st.surveys.mkdir(parents=True, exist_ok=True)
        self.path = st.surveys / "survey_milair_1m_20311028-0900_test.csv"
        step, bins = 12.5e3, 160
        hops = [(335e6 + i * bins * step, None) for i in range(28)]
        self.t = threading.Thread(target=self._go, args=(passes, hops, bins, step), daemon=True)

    def __call__(self, cmd, cwd=None):
        self.cmd = cmd
        self.t.start()
        return self

    def _go(self, *a):
        write_survey(self.path, *a, delay=0.4)
        time.sleep(1.0)
        self.returncode = 0

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.t.join(timeout)
        return self.returncode


class MultiLaunch(Launches):
    """Several steady carriers on air; each launch hears whichever falls in its band."""

    def __init__(self, carriers):
        super().__init__(None, 0.0)
        self.carriers = carriers

    def __call__(self, cmd, log_path):
        self.cmds.append(cmd)
        tune, rate = float(cmd[cmd.index("-f") + 1]), float(cmd[cmd.index("-s") + 1])
        x = sum(bursts(rate, 3.0, f - tune, 3.0, 10.0, first_s=0.0, amp=0.1) for f in self.carriers
                if abs(f - tune) < 0.45 * rate)
        x = x if not np.isscalar(x) else np.zeros(int(3.0 * rate), np.complex64)
        return io.BytesIO(cu8(x)), (lambda: None)


def test_survey_and_recorder_end_to_end(tmp_path, monkeypatch):
    st = station(tmp_path, {"capture_s": 2, "ambush_min": 10})
    cfg = cap.settings(st)
    found = [{"device": 0, "product": "E4000", "serial": "00000101", "tuner": "E4000"},
             {"device": 1, "product": "r820t", "serial": "00000202", "tuner": "R820T"}]
    passes = [[(340.31e6, 16), (305.6e6, 30), (386.5e6, 18), (360.125e6, 26)],
              [(340.31e6, 17), (305.6e6, 30), (386.5e6, 18)],
              [(340.31e6, 16), (305.6e6, 30), (386.5e6, 18)],
              [(340.31e6, 16), (305.6e6, 30), (386.5e6, 18)]]
    run = FakeRun(st, passes)
    L = MultiLaunch([340.31e6, 386.5e6, 360.125e6])
    rc = W.main(["survey", "milair", "1m", "--chain", "x > PC", "--recorder-chain", "r820t chain"],
                st, detect=det(found), launch=L, exe=["rtl_sdr"], spawn=run, poll_s=0.1)
    assert rc == 0
    assert run.cmd[run.cmd.index("--device") + 1] == "0"            # the E4000, found by serial
    assert "--chain" in run.cmd and "--non-interactive" in run.cmd
    sur, _ = W.survey_roles(st, cfg, None, None)
    assert run.cmd[run.cmd.index("--receiver") + 1] == sur          # the child names the radio that surveys
    rows = cap.connect(st).execute("SELECT * FROM captures ORDER BY id").fetchall()
    freqs = {round(r["freq_hz"] / 1e6, 2): r for r in rows if r["mode"] in ("now", "survey pass")}
    assert 305.6 not in freqs                                         # 14.4 MHz x24 never triggers
    assert not any(abs(r["freq_hz"] / 1e6 - round(r["freq_hz"] / 2e6) * 2) < 0.02 for r in rows)  # no hop centre
    open_ = [r for r in rows if r["mode"] == "now" and abs(r["freq_hz"] - 340.31e6) < 30e3]
    assert len(open_) == 1 and open_[0]["iq_status"] == "written" and open_[0]["tier"] == "open"
    assert Path(open_[0]["iq_path"]).exists() and open_[0]["chain"] == "r820t chain"
    priv = [r for r in rows if r["mode"] == "now" and abs(r["freq_hz"] - 386.5e6) < 30e3]
    assert len(priv) == 1 and priv[0]["iq_path"] is None and cap.PRIVATE in priv[0]["flags"]
    amb = [r for r in rows if r["mode"] == "survey pass"]
    assert len(amb) == 1 and abs(amb[0]["freq_hz"] - 360.125e6) < 30e3
    assert all(c[c.index("-d") + 1] == "00000202" for c in L.cmds)    # recorder by serial
    assert not list(cap.pool_dir(st, cfg).rglob("cap_386.*.sigmf-data"))


# ---------------------------------------------------------------- the real radar

def wav_data_offset(path: Path) -> int:
    with open(path, "rb") as f:
        f.read(12)
        while True:
            h = f.read(8)
            cid, size = h[:4], struct.unpack("<I", h[4:])[0]
            if cid == b"data":
                return f.tell()
            f.seek(size + (size & 1), 1)


# ---------------------------------------------------------------- recipes carry over between runs

RADAR_RECIPE = {"store_rate_hz": 2.4e6, "rtl_rate_hz": 2.4e6, "signal_hz": 1300.057e6,
                "tune_hz": 1299.774e6, "signal_above_tune_hz": 283e3, "duration_s": 90.0,
                "gain": {"change_db": 0}, "gain_db_used": 29.7, "occupied_hz": 1.6e6}


@pytest.mark.parametrize("target", [None, 1300.00e6])
def test_a_fresh_watch_uses_the_stored_recipe(tmp_path, target, monkeypatch):
    """A capture stores its recipe on a row whose measured centre can sit tens of kHz from the
    frequency asked for; a later watch must still find it. A row from before target_hz existed (None) must match through its own
    occupied band; a new row through the frequency the recorder was asked for."""
    st = station(tmp_path)
    cfg = cap.settings(st)
    con = cap.connect(st)
    row = {"started": "2031-10-28T09:18:28", "started_epoch": time.time() - 3 * 3600,
           "freq_hz": 1300.057e6, "bandwidth_hz": 1.6e6, "mode": "watch", "captured": 1,
           "iq_status": "written", "recipe": RADAR_RECIPE, "family": "pulsed"}
    if target:
        row["target_hz"] = target
    cap.add_row(con, row)
    assert cap.recipe_for(con, 1300.00e6)["duration_s"] == 90.0
    assert cap.recipe_for(con, 1297.0e6) is None                 # a different signal
    assert W.capture_len(cfg, cap.recipe_for(con, 1300.00e6)) == 90.0
    # what the capture characterises to is not the point here, and it is most of the run time
    monkeypatch.setattr(W.characterise, "characterise", lambda meta: {"family": "test"})
    L = Launches(scene(1.6, 0.4, 2.5), 1300.00e6)
    rec = W.Recorder(st, cfg, R820T, ["rtl_sdr"], "c", 29.7, L, quiet=True)
    tot = W.fixed_watch(rec, 1300.00e6, 1.5)
    assert tot["plan"]["source"] == "recipe"
    assert float(L.cmds[0][L.cmds[0].index("-f") + 1]) == pytest.approx(1300.057e6 - 283e3, abs=1)
    new = rec.con.execute("SELECT target_hz FROM captures WHERE id > 1").fetchall()
    assert new and all(r["target_hz"] == 1300.00e6 for r in new)  # rows say what was asked for


def test_old_catalogue_gains_target_column(tmp_path):
    import sqlite3
    st = station(tmp_path)
    c = sqlite3.connect(st.db)
    c.executescript(cap.SCHEMA.replace("  target_hz       REAL,\n", ""))
    c.execute("INSERT INTO captures (freq_hz, recipe) VALUES (1300.057e6, '{}')")
    c.commit(); c.close()
    con = cap.connect(st)
    assert "target_hz" in {r[1] for r in con.execute("PRAGMA table_info(captures)")}
    assert con.execute("SELECT freq_hz FROM captures").fetchone()[0] == 1300.057e6



# ---------------------------------------------------------------- the live status the web app shows

def snapshots(monkeypatch) -> list:
    """Every status the recorder writes, as written."""
    got = []
    real = W.Recorder.write_status

    def spy(self):
        if self.st is not None:
            got.append(self.snapshot())
        real(self)
    monkeypatch.setattr(W.Recorder, "write_status", spy)
    return got


def test_fixed_watch_reports_its_status(tmp_path, monkeypatch):
    """A fixed watch writes no survey file, so without this the web app would suggest nothing
    was running. It reports itself."""
    st = station(tmp_path, {"capture_s": 4})
    cfg = cap.settings(st)
    got = snapshots(monkeypatch)
    L = Launches(scene(12.0, 0.4, 2.5, kind="fsk"), 433.92e6)
    rec = W.Recorder(st, cfg, R820T, ["rtl_sdr"], "c", 29.7, L, quiet=True, inline_characterise=True)
    W.fixed_watch(rec, 433.92e6, 11.0, width_hz=20e3)
    first, last = got[0], got[-1]
    assert first["state"] == "running" and first["mode"] == "fixed watch" and first["serial"] == "00000202"
    jobs = [g["job"] for g in got if g["job"]]
    assert jobs and jobs[0]["kind"] == "fixed watch" and jobs[0]["target_mhz"] == pytest.approx(433.92)
    assert jobs[0]["tuned_mhz"] < 433.92 and jobs[0]["rate"] == 240e3
    caps = [g["capture"] for g in got if g["capture"]]
    assert caps and caps[0]["file"].endswith(".sigmf-data") and caps[0]["ends_at"] > caps[0]["started"]
    counts = [g["events"] for g in got]
    assert counts == sorted(counts) and counts[-1] >= 4 and last["last_event"]["snr_db"] > 20
    after = [g for g in got if g["job"] and g["captures"] >= 1]
    assert after and after[-1]["job"]["next_capture_at"] == pytest.approx(caps[0]["started"] + 1800, abs=1)
    assert last["state"] == "stopped" and last["job"] is None and last["captures"] == 1
    on_disk = json.loads((cap.pool_dir(st, cfg) / "recorder_status_00000202.json").read_text())
    assert on_disk["state"] == "stopped" and on_disk["events"] == last["events"]


def test_status_heartbeat_keeps_the_file_fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(W, "STATUS_EVERY_S", 0.05)
    st = station(tmp_path)
    rec = W.Recorder(st, cap.settings(st), R820T, ["rtl_sdr"], "c", 29.7, Launches(scene(1, 1, 1), 1e8), quiet=True)
    rec.start_status("fixed watch", ends_at=time.time() + 60)
    path = rec.status_path()
    t1 = json.loads(path.read_text())["updated"]
    time.sleep(0.4)
    t2 = json.loads(path.read_text())["updated"]
    rec.stop_status()
    assert t2 > t1                                   # rewritten with nothing else happening
    time.sleep(0.2)
    assert json.loads(path.read_text())["state"] == "stopped"


def test_survey_reports_passes_jobs_and_ambush(tmp_path, monkeypatch):
    st = station(tmp_path, {"capture_s": 2, "ambush_min": 10})
    got = snapshots(monkeypatch)
    found = [{"device": 0, "product": "E4000", "serial": "00000101", "tuner": "E4000"},
             {"device": 1, "product": "r820t", "serial": "00000202", "tuner": "R820T"}]
    passes = [[(340.31e6, 16), (360.125e6, 26)], [(340.31e6, 17)], [(340.31e6, 16)], [(340.31e6, 16)]]
    run = FakeRun(st, passes)
    rc = W.main(["survey", "milair", "1m", "--chain", "x > PC"], st, detect=det(found),
                launch=MultiLaunch([340.31e6, 360.125e6]), exe=["rtl_sdr"], spawn=run, poll_s=0.1)
    assert rc == 0
    assert got[0]["mode"] == "survey + recorder" and got[0]["surveyor_serial"] == "00000101"
    assert max(g.get("passes", 0) for g in got) == 4
    assert any(g["ambush"] and g["ambush"]["mhz"] == pytest.approx(360.125) for g in got)
    assert any(g["job"] and g["job"]["kind"] == "capture" for g in got)
    cool = [c["mhz"] for g in got for c in g["cooldowns"]]
    assert any(abs(m - 340.31) < 0.01 for m in cool)
    assert got[-1]["state"] == "stopped"


def test_second_capture_of_a_run_uses_the_first_ones_recipe(tmp_path):
    """Within one watch, a later capture must follow the recipe of the one before. A new
    recipe restarts the radio and re-plans through the same lookup as a fresh run, which
    missed because the signal's measured centre sits off the asked-for frequency. Here the
    signal sits 40 kHz above the watched 433.92 MHz , the cooldown is
    a few seconds, and the run must make two captures, the second planned from the first."""
    st = station(tmp_path, {"capture_s": 4, "cooldown_min": 0.1})
    cfg = cap.settings(st)
    f_sig = 433.96e6
    L = Launches(scene(44.0, 0.4, 2.5, kind="fsk"), f_sig)
    rec = W.Recorder(st, cfg, R820T, ["rtl_sdr"], "c", 29.7, L, quiet=True, inline_characterise=True)
    W.fixed_watch(rec, 433.92e6, 42.0, width_hz=100e3)
    caps = rec.con.execute("SELECT * FROM captures WHERE iq_status = 'written' ORDER BY started_epoch").fetchall()
    assert len(caps) >= 2
    first = json.loads(caps[0]["recipe"])
    assert abs(first["signal_hz"] - 433.92e6) > 20e3          # the case the old 20 kHz match missed
    assert first["duration_s"] >= 10                           # a sane repeat, not -2.8e12 s
    m2 = json.loads(Path(caps[1]["iq_path"]).with_suffix(".sigmf-meta").read_text())["global"]
    assert m2["marsad:tuned_hz"] == pytest.approx(first["tune_hz"], abs=1)
    assert m2["marsad:radio_rate_hz"] == first["rtl_rate_hz"]
    secs = Path(caps[1]["iq_path"]).stat().st_size / 4 / m2["core:sample_rate"]
    assert secs == pytest.approx(first["duration_s"] + cap.PRE_S, abs=0.3)
    first_s = Path(caps[0]["iq_path"]).stat().st_size / 4 / caps[0]["store_rate_hz"]
    assert first_s == pytest.approx(4 + cap.PRE_S, abs=0.3)   # the first had no recipe yet
    assert float(L.cmds[1][L.cmds[1].index("-f") + 1]) == pytest.approx(first["tune_hz"], abs=1)


# ---------------------------------------------------------------- one entry per survey sweep

def write_dat_block(fh, lo: float, bins: int, step: float, level: float, stamp: str) -> None:
    fh.write(f"# rtl-power-fftw output\n# Acquisition start: {stamp} UTC\n"
             f"# Acquisition end: {stamp} UTC\n#\n# frequency [Hz] power spectral density [dB/Hz]\n")
    for i in range(bins):
        fh.write(f"{lo + i * step:.0f} {level:.2f}\n")
    fh.write("\n")


def poll_after_each_line(src: Path, dst: Path, tailer) -> None:
    """The file growing line by line under the tailer, the worst split a poll can see."""
    with open(dst, "a") as out:
        for line in src.read_text().splitlines(True):
            out.write(line)
            out.flush()
            tailer.poll()


@pytest.mark.parametrize("sidecar", [True, False])
@pytest.mark.parametrize("per_line", [False, True])
def test_csv_tailer_hands_on_each_sweep_once_well_past_its_ring_buffer(tmp_path, sidecar, per_line):
    src = tmp_path / "src" / "survey_milair_1m_20311028-0900.csv"
    src.parent.mkdir()
    step, bins = 12.5e3, 40
    hops = [(335e6 + i * bins * step, None) for i in range(3)]
    n = 12                                          # the tailer's ring buffer keeps 4
    write_survey(src, [[] for _ in range(n)], hops, bins, step, delay=0)
    dst = tmp_path / src.name
    if sidecar:
        dst.with_suffix(".json").write_text(src.with_suffix(".json").read_text())
    if per_line:
        dst.write_text("")
        t = W.watch_tailer(dst)
        poll_after_each_line(src, dst, t)
    else:
        dst.write_text(src.read_text())
        t = W.watch_tailer(dst)
        t.poll()
    # without a sidecar the hop count is unknown until the first wrap, and the last sweep
    # still closes itself, because the first sweep taught the tailer the count
    assert len(t.done) == n
    assert len({id(p[0]) for p in t.done}) == n     # n distinct sweeps, none handed on twice


@pytest.mark.parametrize("hop_count", [3, 1])
def test_dat_tailer_hands_on_each_pass_once_well_past_its_ring_buffer(tmp_path, hop_count):
    """A multi-hop .dat closes a pass only on the wrap. A one-hop plan closes every block
    itself, and the next block's wrap then finds nothing to close, the same trap as a CSV."""
    dat = tmp_path / "survey.dat"
    dat.with_suffix(".json").write_text(json.dumps(
        {"geometry": {"hops": hop_count}, "rtl_power_fftw": {"crop": 0.0}}))
    dat.write_text("")
    t = W.watch_tailer(dat)
    n, step, bins = 12, 18750.0, 64
    with open(dat, "a") as fh:
        for p in range(n):
            for h in range(hop_count):
                write_dat_block(fh, 2147e6 + h * bins * step, bins, step, -60.0 + p, f"2031-10-22 11:44:{p:02d}")
            fh.flush()
            t.poll()
    dat.with_suffix(".log").write_text("Session duration elapsed.\n")
    t.poll()
    assert len(t.done) == n
    assert len({id(p[0]) for p in t.done}) == n
    tops = [max(p[0].values()) for p in t.done]
    assert tops == [pytest.approx(-60.0 + p) for p in range(n)]     # in order, each pass its own


def follow_lines(tmp_path: Path, lit: dict, n: int = 12, cfg: dict | None = None) -> tuple:
    """A survey of n sweeps through the real tailer and trigger rules. `lit` maps a sweep
    number to the lines on air in it. Returns (sweeps the follower saw, the actions fired)."""
    path = tmp_path / "survey_milair_1m_20311028-0900.csv"
    step, bins = 12.5e3, 160
    hops = [(335e6 + i * bins * step, None) for i in range(4)]
    write_survey(path, [lit.get(p, []) for p in range(n)], hops, bins, step, delay=0)
    st = station(tmp_path / "st")
    fired = []
    disp = type("Disp", (), {"submit": lambda self, a: fired.append(a)})()
    trig = W.Triggers(cfg or {"threshold_db": 12, "passes": 2, "ambush_db": 20, "cooldown_min": 30})
    seen = W.follow_survey(W.watch_tailer(path), trig, disp, W.trigger_families(st), 2.0,
                           alive=lambda: False, say=lambda *_: None, poll_s=0)
    return seen, fired


def test_a_line_in_one_sweep_past_the_ring_buffer_does_not_capture(tmp_path):
    seen, fired = follow_lines(tmp_path, {8: [(340.3125e6, 15)]})
    assert seen == 12 and fired == []


def test_the_same_line_in_two_consecutive_sweeps_captures(tmp_path):
    seen, fired = follow_lines(tmp_path, {8: [(340.3125e6, 15)], 9: [(340.3125e6, 15)]})
    assert seen == 12
    assert [a["kind"] for a in fired] == ["capture"] and fired[0]["why"].startswith("2 passes")
    assert fired[0]["f_mhz"] == pytest.approx(340.3125, abs=0.02)


def test_two_sweeps_with_a_gap_between_them_do_not_capture(tmp_path):
    _, fired = follow_lines(tmp_path, {7: [(340.3125e6, 15)], 9: [(340.3125e6, 15)]})
    assert fired == []


def test_one_strong_sweep_past_the_ring_buffer_is_one_ambush(tmp_path):
    _, fired = follow_lines(tmp_path, {8: [(340.3125e6, 25)]})
    assert [a["kind"] for a in fired] == ["ambush"]


def test_survey_dry_run_checks_the_childs_flags_with_its_own_parser(tmp_path, capsys):
    st = station(tmp_path)
    st.presets["custom"] = {"freq": "1249M:1749M:50k", "gain": 29, "integration": 30}
    base = ["survey", "custom", "1h", "--dry-run"]
    with pytest.raises(SystemExit) as ei:
        W.main(base + ["-g", "29", "-i", "30"], st)
    assert ei.value.code == 2
    err = capsys.readouterr().err
    assert "-g 29 -i 30" in err and "marsad run" in err
    assert W.main(base + ["--gain", "29", "--integration", "30", "--chain", "a > b", "--conditions", "c",
                          "--tag", "x", "--skip-warmup", "5"], st) == 0
    assert "dry run: no radio opened" in capsys.readouterr().out


def test_survey_child_is_told_the_surveying_radio(tmp_path, capsys):
    """Owner's decision 2026-11-07 (relay 011): with the roles swapped, the survey run records the
    radio that actually surveys, not the preset's, so its fingerprint names the right radio."""
    st = station(tmp_path)
    names = list(st.receivers)
    base = ["survey", "milair", "1h", "--dry-run", "--chain", "a > b", "--conditions", "c"]
    assert W.main(base + ["--surveyor", names[1], "--recorder", names[0]], st) == 0
    assert f"--receiver {names[1]}" in capsys.readouterr().out
    assert W.main(base + ["--surveyor", names[1], "--recorder", names[0], "--receiver", names[1]], st) == 0
    assert capsys.readouterr().out.count("--receiver") == 1          # given once, never added twice
