"""What a long watch taught the recorder and the characteriser: synthetic scenes, no radio.

A width that is only the capture's own filter is not a signal; a burst after the keep window
is still keepable; a short wide look chooses the capture's width; pulses are labelled by what
was measured; gain does not walk down; well-known signals record less often when asked; and
the review of a watch needs nobody to run it.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from marsad import capture as cap
from marsad import catalogue as C
from marsad import characterise as ch
from marsad import live
from marsad import watch as W
from marsad import watch_report as WR
from test_watch import R820T, Launches, cu8, scene, station

RNG = np.random.default_rng(7)


def noise(n: int, sigma: float = 0.02) -> np.ndarray:
    return ((RNG.standard_normal(n) + 1j * RNG.standard_normal(n)) * sigma).astype(np.complex64)


def write_cut(tmp_path: Path, x: np.ndarray, rate: float, decim: int, cutoff: float, name: str = "c",
              tuned_off: float = 0.0, freq: float = 1500e6) -> Path:
    """A capture on disk as the recorder writes it, cut with the given filter edge."""
    from scipy.signal import fftconvolve
    y = fftconvolve(x, ch.cut_taps(decim, cutoff), mode="same")[::decim] if decim > 1 else x
    iq = np.empty(2 * len(y), np.int16)
    iq[0::2], iq[1::2] = np.clip(y.real * 32768, -32768, 32767), np.clip(y.imag * 32768, -32768, 32767)
    data = tmp_path / f"{name}.sigmf-data"
    data.write_bytes(iq.tobytes())
    g = {"core:datatype": "ci16_le", "core:sample_rate": rate / decim, "marsad:decimation": decim,
         "marsad:radio_rate_hz": rate, "marsad:tuned_hz": freq + tuned_off}
    if cutoff != 0.2:
        g["marsad:cut_cutoff"] = cutoff
    meta = tmp_path / f"{name}.sigmf-meta"
    meta.write_text(json.dumps({"global": g, "captures": [{"core:frequency": freq}]}))
    return meta


# ---------------------------------------------------------------- 1. the filter's own shape

@pytest.mark.parametrize("rate,decim", [(240e3, 2), (960e3, 2), (240e3, 24)])
@pytest.mark.parametrize("cutoff", [0.2, 0.4])
def test_receiver_noise_through_the_cut_filter_is_not_a_signal(tmp_path, rate, decim, cutoff):
    """Before the fix this read as a flat signal 40-50 dB strong, 0.44 x the cut rate wide,
    at every rate: the floor came from the filter's stopband."""
    meta = write_cut(tmp_path, noise(int(rate * 8)), rate, decim, cutoff)
    r = ch.characterise(meta)
    assert r["family"] == "nothing found"
    assert "recipe" not in r


def test_a_narrow_line_inside_the_passband_is_found_at_its_true_level(tmp_path):
    rate, decim = 240e3, 2
    n = int(rate * 8)
    t = np.arange(n) / rate
    x = noise(n) + 0.05 * np.exp(2j * np.pi * 5e3 * t)
    r = ch.characterise(write_cut(tmp_path, x, rate, decim, 0.4))
    assert r["family"] == "CW"
    assert abs(r["continuous"]["centre_hz"] - 5e3) < 500
    assert r["continuous"]["obw_hz"] < 2e3
    assert r["recipe"]["store_rate_hz"] < 60e3


def test_a_wide_signal_leaves_noise_beside_it_in_a_new_capture(tmp_path):
    """The recipe's 2.5 x width fits inside the new filter's edge, so there is a floor to read."""
    rate, decim = 960e3, 8                   # 120 kS/s cut: a 48 kHz signal is 2.5 x inside it
    n = int(rate * 8)
    from scipy.signal import firwin, fftconvolve
    band = fftconvolve(noise(n, 1.0), firwin(301, 24e3, fs=rate), mode="same") * 0.15
    r = ch.characterise(write_cut(tmp_path, noise(n) + band, rate, decim, 0.4))
    assert r["family"] != "nothing found"
    assert r["occupied"]["bandwidth_hz"] == pytest.approx(48e3, rel=0.25)


def test_the_tuners_dc_bump_is_not_a_signal(tmp_path):
    """A low-IF tuner's small bump at the tuned frequency lands inside a cut capture."""
    rate, decim = 240e3, 2
    n = int(rate * 8)
    t = np.arange(n) / rate
    x = noise(n) + 0.03 * np.exp(2j * np.pi * -20e3 * t)
    r = ch.characterise(write_cut(tmp_path, x, rate, decim, 0.4, tuned_off=-20e3))
    assert r["family"] == "nothing found"


def old_row(**over) -> dict:
    rec = {"occupied_hz": 53000.0, "gain": {"change_db": 0, "why": "peak SNR 48 dB, peak -43.2 dBFS, no clipping"},
           "signal_hz": 1458e6, "rtl_rate_hz": 240e3, "store_rate_hz": 133e3}
    return {"recipe": json.dumps(rec), "store_rate_hz": 120e3, "rtl_rate_hz": 240e3, "family": "OFDM-like",
            "capture_snr_db": 48.0, "snr_peak_db": 14.0, **over}


def test_old_noise_rows_are_recognised_without_being_rewritten():
    assert cap.filter_shaped(old_row())
    assert cap.grade_row(old_row())[0] == "empty"
    # a real pulsed signal in the same filter stands tens of dB above noise in dBFS
    loud = json.loads(old_row()["recipe"])
    loud["gain"]["why"] = "peak SNR 54 dB, peak -9.0 dBFS, no clipping"
    assert not cap.filter_shaped(old_row(recipe=json.dumps(loud), family="AM-like"))
    assert not cap.filter_shaped(old_row(family="pulsed, unmodulated carrier"))
    assert not cap.filter_shaped(old_row(rtl_rate_hz=120e3))                   # no cut, no filter
    fixed = json.loads(old_row()["recipe"])
    fixed["peak_dbfs"] = -43.2
    assert not cap.filter_shaped(old_row(recipe=json.dumps(fixed)))            # measured by the fixed code


def test_a_fresh_capture_does_not_follow_a_filter_shaped_recipe(tmp_path):
    st = station(tmp_path)
    con = cap.connect(st)
    cap.add_row(con, {"started_epoch": 1.0, "freq_hz": 1458e6, "target_hz": 1458e6, **old_row()})
    assert cap.recipe_for(con, 1458e6) is None
    good = {"signal_hz": 1458e6, "rtl_rate_hz": 960e3, "store_rate_hz": 120e3, "peak_dbfs": -30.0}
    cap.add_row(con, {"started_epoch": 0.5, "freq_hz": 1458e6, "target_hz": 1458e6, "recipe": good})
    assert cap.recipe_for(con, 1458e6)["store_rate_hz"] == 120e3              # the older honest one
    con.close()


def test_a_capture_too_narrow_to_show_a_signal_widens_the_next_one(tmp_path):
    rate, decim = 240e3, 2
    n = int(rate * 8)
    x = noise(n)
    on = np.arange(n) % int(rate * 0.1) < int(rate * 0.001)     # 1 ms pulses, noise-like and wider than the cut
    x = x + on * noise(n, 0.3)
    r = ch.characterise(write_cut(tmp_path, x, rate, decim, 0.4))
    assert r["occupied"]["fills_capture"]
    assert r["recipe"]["rtl_rate_hz"] == 2.4e6 and r["recipe"]["store_rate_hz"] == 2.4e6


# ---------------------------------------------------------------- 2. the keep rule

def session(rate=240e3, capture_s=4.0, duration_s=4.0, keep=12.0, check=1.0, written=None):
    plan = cap.plan_capture(1e9, None, "r820t", {"rtl_rate_hz": rate, "signal_above_tune_hz": 30e3,
                                                  "store_rate_hz": 60e3, "occupied_hz": 20e3, "signal_hz": 1e9}, 29.7)

    class W_:
        def __init__(self):
            self.bytes = self.raw_samples = 0

        def write(self, x):
            self.bytes += len(x)
            self.raw_samples += len(x)

        def annotate(self, *a):
            pass

        def close(self):
            return Path("x.sigmf-meta")

    def opener(sess, epoch):
        w = W_()
        written.append(w)
        return w
    return cap.Session(plan, "open", mode="now", duration_s=duration_s, capture_s=capture_s, t0_epoch=0.0,
                       open_writer=opener, keep_min_db=keep, keep_check_s=check), plan


def feed(sess, x):
    for i in range(0, len(x) - sess.block + 1, sess.block):
        sess.feed(x[i:i + sess.block])


def run_stream(sess, x):
    class R:
        def __init__(self):
            self.i = 0

        def get(self):
            if self.i + sess.block * 2 > len(b):
                return None
            d = b[self.i:self.i + sess.block * 2]
            self.i += sess.block * 2
            return d
    b = cu8(x, noise=0.02, seed=3)
    return sess.run(R())


def test_a_strong_burst_after_the_keep_window_is_kept(tmp_path):
    """The window closed at 1 s; the burst came at 2.5 s, the way a real one did."""
    written: list = []
    s, plan = session(written=written)
    rate = plan["rtl_rate_hz"]
    n = int(rate * 7)
    t = np.arange(n) / rate
    off = plan["signal_hz"] - plan["tune_hz"]
    x = 0.5 * ((t > 2.5) & (t < 2.56)) * np.exp(2j * np.pi * off * t)
    out = run_stream(s, x.astype(np.complex64))
    assert out["captures"] == 1 and out["below_keep"] == 0 and len(written) == 1
    ev = s.events[0]
    assert "capture_started" in ev and "iq_refused" not in ev and ev["keep_snr_db"] >= 12
    kept = written[0].bytes / rate
    assert kept == pytest.approx(cap.PRE_S + 4.0, abs=0.2)     # the second before it, and the full length


def test_noise_alone_is_still_refused(tmp_path):
    written: list = []
    s, plan = session(written=written)
    out = run_stream(s, np.zeros(int(plan["rtl_rate_hz"] * 4), np.complex64))
    assert out["captures"] == 0 and written == []
    assert out["below_keep"] == 1
    assert s.events[0]["iq_refused"] == cap.BELOW_KEEP


def test_a_late_burst_still_obeys_the_budget(tmp_path):
    written: list = []
    s, plan = session(written=written)
    s.allow_iq = lambda t_s: "daily cap" if t_s > 0.5 else None
    rate = plan["rtl_rate_hz"]
    n = int(rate * 4)
    t = np.arange(n) / rate
    x = 0.5 * ((t > 2.5) & (t < 2.6)) * np.exp(2j * np.pi * (plan["signal_hz"] - plan["tune_hz"]) * t)
    run_stream(s, x.astype(np.complex64))
    assert written == [] and s.events[0]["iq_refused"] == "daily cap"


# ---------------------------------------------------------------- 3. the wide look

FS = 2.4e6


def tone(n, f, a=0.1):
    return (a * np.exp(2j * np.pi * f * np.arange(n) / FS)).astype(np.complex64)


def gated(n, every_s, on_s):
    return (np.arange(n) % int(FS * every_s)) < int(FS * on_s)


def test_look_says_continuous_and_measures_it():
    n = int(FS * 2.5)
    r = ch.look(noise(n) + tone(n, 300e3), FS, 300e3)
    assert r["kind"] == "continuous" and r["width_hz"] < 20e3
    assert abs(r["centre_hz"] - 300e3) < 5e3


def test_look_says_pulsed_beside_a_steady_line_and_measures_the_pulses_only():
    """A steady line elsewhere in the band marks every frame active; pulses must still show."""
    n = int(FS * 2.5)
    x = noise(n) + tone(n, -800e3, 0.05) + gated(n, 0.04, 0.001) * noise(n, 0.3)
    r = ch.look(x, FS, 100e3, 50e3)
    assert r["kind"] == "pulsed" and r["pulses"] >= 30
    assert r["width_hz"] > 1.0e6


def test_look_says_nothing_for_nothing_and_ignores_a_signal_elsewhere():
    n = int(FS * 2.5)
    assert ch.look(noise(n), FS, 0.0)["kind"] == "nothing"
    assert ch.look(noise(n) + tone(n, -800e3), FS, 300e3)["kind"] == "nothing"


def test_characteriser_finds_pulses_beside_a_steady_line(tmp_path):
    n = int(FS * 10)
    x = noise(n) + tone(n, -800e3, 0.05) + gated(n, 0.08, 0.001) * tone(n, 200e3, 0.3)
    r = ch.characterise(write_cut(tmp_path, x, FS, 1, 0.2))
    assert str(r["family"]).startswith("pulsed")
    assert r["steady_line_too"] is True
    assert r["pulses"]["count"] > 80
    assert r["pulses"]["width_s"] == pytest.approx(1e-3, rel=0.1)


class LookLaunch(Launches):
    def __init__(self, scene_, signal_hz):
        super().__init__(scene_, signal_hz)


def test_first_capture_is_set_from_the_look_not_the_survey_guess(tmp_path):
    st = station(tmp_path, {"look_s": 2.0})
    f = 1400e6
    # a steady 4 kHz-wide-at-most line: the survey's bin-wide guess of 500 kHz would be far too wide
    L = LookLaunch(lambda off, fs: (0.2 * np.exp(2j * np.pi * off * np.arange(int(fs * 3)) / fs)).astype(np.complex64), f)
    rec = W.Recorder(st, cap.settings(st), R820T, ["rtl_sdr"], "chain", 29.7, L, quiet=True)
    plan, words = rec.plan_for(f, 500e3, cap.band_tier(f, rec.cfg))
    assert plan["source"] == "wide look" and "continuous" in words
    assert plan["store_rate_hz"] < 200e3
    assert plan["target_hz"] == f
    assert float(L.cmds[0][L.cmds[0].index("-s") + 1]) == 2.4e6        # looked at the full rate


def test_look_that_sees_nothing_keeps_the_whole_band(tmp_path):
    st = station(tmp_path, {"look_s": 2.0})
    f = 1400e6
    L = LookLaunch(lambda off, fs: np.zeros(int(fs * 3), np.complex64), f)
    rec = W.Recorder(st, cap.settings(st), R820T, ["rtl_sdr"], "chain", 29.7, L, quiet=True)
    plan, words = rec.plan_for(f, 40e3, cap.band_tier(f, rec.cfg))
    assert plan["store_rate_hz"] == 2.4e6 and "nothing seen" in words


def test_private_band_gets_no_look_and_no_radio_time_for_one(tmp_path):
    st = station(tmp_path, {"look_s": 2.0})
    f = 1400e6
    L = LookLaunch(lambda off, fs: np.zeros(int(fs * 3), np.complex64), f)
    rec = W.Recorder(st, cap.settings(st), R820T, ["rtl_sdr"], "chain", 29.7, L, quiet=True)
    plan, words = rec.plan_for(f, 40e3, {"tier": "private", "band": "x"})
    assert L.cmds == [] and words is None and plan["source"] == "survey width"


def test_look_writes_nothing_to_disk(tmp_path):
    st = station(tmp_path, {"look_s": 2.0})
    f = 1400e6
    L = LookLaunch(lambda off, fs: (0.2 * np.exp(2j * np.pi * off * np.arange(int(fs * 3)) / fs)).astype(np.complex64), f)
    rec = W.Recorder(st, cap.settings(st), R820T, ["rtl_sdr"], "chain", 29.7, L, quiet=True)
    rec.wide_look(f, cap.band_tier(f, rec.cfg), 40e3)
    assert not list(rec.pool.rglob("*.sigmf-data")) and not list(rec.pool.rglob("*.fp.npz"))


# ---------------------------------------------------------------- 4. pulse labels

def pulses_of(x, tmp_path, rate=FS):
    return ch.characterise(write_cut(tmp_path, x, rate, 1, 0.2))


def test_a_noise_like_pulse_wider_than_the_capture_says_so(tmp_path):
    n = int(FS * 8)
    r = pulses_of(noise(n) + gated(n, 0.1, 0.001) * noise(n, 0.3), tmp_path)
    assert r["family"] == "pulsed, wider than the capture"
    assert r["pulses"]["shape"]["span_6db_hz"] > 1.2e6


def test_pulses_at_jumping_frequencies_say_so(tmp_path):
    n = int(FS * 8)
    every = int(FS * 0.1)
    on = np.arange(n) % every < int(FS * 5e-4)
    which = (np.arange(n) // every) % 2
    f = np.where(which == 0, 200e3, -150e3)
    x = noise(n) + on * 0.3 * np.exp(2j * np.pi * np.cumsum(f) / FS)
    r = pulses_of(x.astype(np.complex64), tmp_path)
    assert r["family"] == "pulsed, frequency jumps between pulses"
    assert r["pulses"]["shape"]["centre_spread_hz"] > 300e3


def test_a_steady_tone_in_each_pulse_is_still_an_unmodulated_carrier(tmp_path):
    n = int(FS * 8)
    x = noise(n) + gated(n, 0.1, 0.001) * tone(n, 200e3, 0.3)
    r = pulses_of(x, tmp_path)
    assert r["family"] == "pulsed, unmodulated carrier"
    assert r["pulses"]["shape"]["span_6db_hz"] < 100e3
    assert abs(r["pulses"]["shape"]["centre_hz"] - 200e3) < 20e3


def test_a_chirp_keeps_its_label(tmp_path):
    n = int(FS * 8)
    on = gated(n, 0.05, 1e-4)
    t = (np.arange(n) % int(FS * 0.05)) / FS
    x = noise(n) + on * 0.3 * np.exp(2j * np.pi * (-400e3 * t + 0.5 * 8e9 * t ** 2))
    r = pulses_of(x.astype(np.complex64), tmp_path)
    assert "chirp" in r["family"]


# ---------------------------------------------------------------- 5. the report, 7. episodes, 9. harmonics

def seed(st) -> sqlite3.Connection:
    con = cap.connect(st)
    base = 1.9e9

    def add(i, f, **k):
        cap.add_row(con, {"started": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(base + i * 60)),
                          "started_epoch": base + i * 60, "freq_hz": f, "target_hz": f, "tier": "open",
                          "survey": "survey_L_1h_tagA_20300101-0000", "receiver": "r820t", "mode": "now",
                          "snr_peak_db": 30.0, "iq_status": "not recorded", "captured": 1, **k})
    add(0, 900e6, iq_status="written", iq_bytes=2_000_000_000, family="pulsed, unmodulated carrier",
        capture_snr_db=40.0, recipe={"rtl_rate_hz": 2.4e6, "peak_dbfs": -5.0})
    add(1, 1500e6, iq_status="written", iq_bytes=14_000_000, family="OFDM-like", **{
        k: v for k, v in old_row().items() if k in ("recipe", "store_rate_hz", "rtl_rate_hz", "capture_snr_db")})
    add(2, 1501e6, iq_status=cap.BELOW_KEEP, snr_peak_db=30.0, settings={"keep_min_snr_db": 12},
        notes="recorder SNR 9.0 dB stayed under the keep level 12 dB: fingerprint only")
    add(3, 1502e6, iq_status=cap.BELOW_KEEP, snr_peak_db=9.0, settings={"keep_min_snr_db": 12})
    add(4, 760e6, tier="private", band="secret band", flags=cap.PRIVATE, iq_status="private: not kept")
    add(62, 760e6, tier="private", band="secret band", flags=cap.PRIVATE, iq_status="private: not kept")
    for i, f in enumerate((1100e6, 1104e6, 1108e6)):      # one episode
        add(120 + i * 3, f)
    add(200, 1520e6, timing={"pri_us": [1000.5, 1200.9], "rotation_s": 7.0, "width_us": 90.0})
    add(201, 1330e6, timing={"pri_us": [1001.0, 1201.0], "rotation_s": 7.005, "width_us": 91.0})
    return con


def test_report_says_it_all_and_keeps_private_to_counts(tmp_path):
    st = station(tmp_path, {"band_tiers": [{"lo": 750, "hi": 770, "tier": "private", "band": "secret band"}]})
    con = seed(st)
    before = con.execute("SELECT COUNT(*), SUM(iq_bytes) FROM captures").fetchone()
    con.close()
    db = st.db.read_bytes()
    rep = WR.build(st)
    text = WR.to_text(rep)
    assert rep["events"] == 11 and rep["open"] == 9 and rep["private"] == 2 and rep["captures"] == 2
    assert "2.01 GB" in text
    assert "| 900.000 | pulsed, unmodulated carrier | 1 | 1 | 2.00 |" in text
    assert "| secret band | 2 |" in text
    assert "760" not in text                       # a private row's frequency is never listed
    assert [r["id"] for r in rep["empty"]] == [2]  # noise that looked like a signal
    assert [r["id"] for r in rep["refused"]] == [3]
    assert "only the capture's filter shape" in text
    assert "1520.00" not in text or "same emitter?" in text
    assert "1330.00 and 1520.00 MHz have the same pulse timing: same emitter?" in text
    assert st.db.read_bytes() == db                # read-only, to the byte
    assert before[0] == 11


def test_report_still_counts_captures_set_aside_after_the_watch(tmp_path):
    st = station(tmp_path, {"band_tiers": [{"lo": 750, "hi": 770, "tier": "private", "band": "secret band"}]})
    con = seed(st)
    before = WR.build(st)
    con.execute("UPDATE captures SET iq_status = ? WHERE id IN (1, 2)", (C.SET_ASIDE,))
    con.commit()
    con.close()
    rep = WR.build(st)
    text = WR.to_text(rep)
    assert (rep["captures"], rep["iq_bytes"]) == (before["captures"], before["iq_bytes"]) == (2, 2_014_000_000)
    assert (rep["set_aside"], rep["set_aside_bytes"]) == (2, 2_014_000_000)
    assert "2 IQ captures, 2.01 GB of IQ (2 of them, 2.01 GB, set aside since)" in text
    assert "| 900.000 | pulsed, unmodulated carrier | 1 | 1 | 2.00 |" in text
    assert [r["id"] for r in rep["empty"]] == [2] and "set aside since |" in text
    assert "set aside since" not in WR.to_text(before)


def test_report_finds_a_watch_by_part_of_its_name_and_refuses_plainly(tmp_path):
    st = station(tmp_path)
    con = seed(st)
    assert WR.find_survey(con, "tagA") == "survey_L_1h_tagA_20300101-0000"
    with pytest.raises(SystemExit, match="No watch matches"):
        WR.find_survey(con, "nope")
    con.close()


def test_report_command(tmp_path, capsys):
    st = station(tmp_path)
    seed(st).close()
    md = tmp_path / "r.md"
    assert W.main(["report", "tagA", "--md", str(md)], st) == 0
    assert "# Watch report: survey_L_1h_tagA" in capsys.readouterr().out and md.exists()


def ev(i, f, snr=30.0, **k):
    return {"id": i, "started": f"2030-01-01T00:{i:02d}:00", "started_epoch": 1000.0 + i * 60, "freq_hz": f,
            "snr_peak_db": snr, "private": False, "badges": [], "iq_status": "", **k}


def test_episode_groups_close_strong_events_and_keeps_them_listed():
    rs = [ev(1, 1100e6), ev(2, 1104e6), ev(5, 1110e6), ev(30, 1101e6), ev(31, 1500e6)]
    eps = C.episodes(rs)
    assert len(eps) == 1 and eps[0]["ids"] == [1, 2, 5] and eps[0]["signals"] == 3
    assert (eps[0]["lo_mhz"], eps[0]["hi_mhz"]) == (1100.0, 1110.0)
    assert len(rs) == 5


def test_a_lone_signal_repeating_is_not_an_episode_nor_are_weak_or_private_events():
    assert C.episodes([ev(i, 950e6) for i in range(5)]) == []
    assert C.episodes([ev(i, 1100e6 + i * 1e6, snr=13.0) for i in range(5)]) == []
    assert C.episodes([ev(i, 1100e6 + i * 1e6, private=True) for i in range(5)]) == []


def test_episodes_reach_the_captures_tab(tmp_path):
    st = station(tmp_path)
    seed(st).close()
    d = C.summary(st)
    assert len(d["episodes"]) == 1 and d["episodes"][0]["events"] == 3


def test_harmonic_flag_is_a_flag_and_nothing_else():
    cfg = {"band_tiers": [{"lo": 300, "hi": 320, "tier": "private", "band": "some cellular"},
                          {"lo": 400, "hi": 410, "tier": "open", "band": "an open one"}]}
    assert cap.harmonic_note(620e6, cfg) == "possible harmonic of some cellular"
    assert cap.harmonic_note(810e6, cfg) is None                # twice an open band
    assert cap.harmonic_note(1000e6, cfg) is None
    assert cap.band_tier(620e6, cfg)["tier"] == "open"
    row = {"freq_hz": 620e6, "tier": "open", "iq_status": "written"}
    assert not C.is_private(row, cfg)


def test_harmonic_note_lands_on_the_row_and_the_iq_is_still_written(tmp_path, monkeypatch):
    monkeypatch.setattr(cap, "SHOW_HARMONIC", True)       # hidden for now; the flag itself must still work
    st = station(tmp_path, {"capture_s": 2, "band_tiers": [
        {"lo": 200, "hi": 240, "tier": "private", "band": "some band"}]})
    f = 433.92e6
    L = Launches(scene(6.0, 0.5, 2.0), f)
    rec = W.Recorder(st, cap.settings(st), R820T, ["rtl_sdr"], "chain", 29.7, L, quiet=True, inline_characterise=True)
    W.fixed_watch(rec, f, 5.0, width_hz=20e3)
    rows = rec.con.execute("SELECT * FROM captures").fetchall()
    assert rows and all("possible harmonic of some band" in r["notes"] for r in rows)
    assert all(r["tier"] == "open" and not r["flags"] for r in rows)
    assert any(r["iq_status"] == "written" for r in rows)


# ---------------------------------------------------------------- 6. shared pulse timing

A = {"pri_us": [1000.5, 1200.9, 1400.8], "rotation_s": 7.0, "width_us": 90.0}


def test_timing_match_needs_more_than_one_lone_number():
    assert cap.timing_match(A, {"pri_us": [1001.0, 1201.0, 1401.0], "rotation_s": 7.01})
    assert not cap.timing_match(A, {"pri_us": [1001.0, 1201.0, 1600.0], "rotation_s": 7.0})
    assert not cap.timing_match(A, {"pri_us": [1001.0, 1201.0, 1401.0], "rotation_s": 10.0})
    assert not cap.timing_match({"pri_us": [1000.0]}, {"pri_us": [1000.0]})
    assert cap.timing_match({"pri_us": [1000.0], "rotation_s": 6.0}, {"pri_us": [1001.0], "rotation_s": 6.0})
    assert not cap.timing_match(A, None)


def test_a_link_is_worded_as_a_question_and_never_crosses_into_private(tmp_path):
    st = station(tmp_path)
    con = cap.connect(st)
    cap.add_row(con, {"started_epoch": 1.0, "freq_hz": 950.5e6, "tier": "open", "timing": A, "bandwidth_hz": 1.5e6})
    cap.add_row(con, {"started_epoch": 2.0, "freq_hz": 900e6, "tier": "private", "timing": A})
    other = cap.same_timing_elsewhere(con, 1010.5e6, 50e3, A)
    assert other["freq_hz"] == 950.5e6
    q = cap.timing_question(other, A)
    assert q.endswith("same emitter?") and "950.500" in q and "rotation" in q
    assert cap.same_timing_elsewhere(con, 950.5e6, 50e3, A) is None          # the same signal is no link
    con.close()


def test_timing_signature_leaves_out_minor_spacings():
    r = {"pulses": {"pri": [{"pri_s": 2.7e-3, "count": 90}, {"pri_s": 3.2e-3, "count": 80}, {"pri_s": 2.75e-4, "count": 3}],
                    "rotation": {"period_s": 7.0}, "width_s": 1.1e-4}}
    assert ch.timing_signature(r) == {"pri_us": [2700.0, 3200.0], "rotation_s": 7.0, "width_us": 110.0}
    assert ch.timing_signature({"continuous": {}}) is None


# ---------------------------------------------------------------- 8. gain, and known signals

def res(rails, peak, snr=40.0):
    return {"clipping": {"rail_fraction": rails, "peak_dbfs": peak}, "pulses": {"snr_db_max": snr}}


def test_gain_steps_down_only_for_clipping_that_costs_something():
    rec = lambda r: ch.recipe(2.4e6, 1e9, 0.0, 1e6, r)["gain"]["change_db"]
    assert rec(res(8.5e-6, 0.0)) == 0            # a few samples touching: keep the SNR
    assert rec(res(1.3e-5, 0.0)) == 0
    assert rec(res(5e-5, 0.0)) == -3
    assert rec(res(5e-3, 0.0)) == -6
    assert rec(res(0.0, -2.5)) == 0


def test_gain_climbs_back_once_clear_of_the_rails_and_never_past_the_standing_gain():
    r = lambda used, peak, change=0: {"gain_db_used": used, "peak_dbfs": peak, "gain": {"change_db": change}}
    assert cap.next_gain(29.7, r(16.6, -11.0)) == pytest.approx(24.6)       # keeps 3 dB in hand
    assert cap.next_gain(29.7, r(22.9, -30.0)) == pytest.approx(29.7)       # capped at the standing gain
    assert cap.next_gain(29.7, r(22.9, -3.0)) == pytest.approx(22.9)        # stays: no room, and no further fall
    assert cap.next_gain(29.7, r(29.7, -2.0)) == pytest.approx(29.7)
    assert cap.next_gain(29.7, r(29.7, 0.0, -3)) == pytest.approx(26.7)
    assert cap.next_gain(29.7, None) == pytest.approx(29.7)
    # old recipes carry no peak: they stay where they are until the next capture replaces them
    assert cap.next_gain(29.7, {"gain_db_used": 16.6, "gain": {"change_db": 0}}) == pytest.approx(16.6)


def test_repeated_captures_of_a_strong_pulse_do_not_walk_the_gain_down():
    """Clip a little, step, clear the rails: from there the gain holds or climbs, never falls."""
    ladder = [cap.snap_gain("r820t", g) for g in (29.7,)]
    g = 29.7
    for _ in range(8):
        peak = -2.0 + (g - 29.7)        # the same pulse, level following the gain
        rails = 3e-5 if peak >= -2.0 else 0.0
        rc = ch.recipe(2.4e6, 1e9, 0.0, 1e6, res(rails, peak))
        rc["gain_db_used"] = g
        g = cap.snap_gain("r820t", cap.next_gain(29.7, rc))
        ladder.append(g)
    assert min(ladder) >= 22.9


def add_good(con, n, f=950.5e6, family="pulsed, non-linear FM chirp (radar-like)", snr=40.0, start=1000.0, **k):
    for i in range(n):
        cap.add_row(con, {"started_epoch": start + i, "freq_hz": f, "target_hz": f, "tier": "open", "family": family,
                          "iq_status": "written", "iq_bytes": 1000, "capture_snr_db": snr, "captured": 1,
                          "bandwidth_hz": 1e6, **k})


def test_known_signal_needs_enough_good_captures(tmp_path):
    st = station(tmp_path)
    con = cap.connect(st)
    add_good(con, 2)
    assert cap.known_signal(con, 950.5e6, 25e3, 0.0, 3) is None
    add_good(con, 1, start=2000.0)
    k = cap.known_signal(con, 950.5e6, 25e3, 0.0, 3)
    assert k == {"captures": 3, "last": 2000.0}
    con.close()


def test_noise_never_counts_as_well_known(tmp_path):
    """New 'nothing found' rows, weak rows and old filter-shaped rows all fail."""
    st = station(tmp_path)
    con = cap.connect(st)
    add_good(con, 5, f=1500e6, family="nothing found", snr=48.0)
    add_good(con, 5, f=1501e6, snr=8.0)
    add_good(con, 5, f=1502e6, family="OFDM-like", snr=48.0, **{
        k: v for k, v in old_row().items() if k in ("recipe", "store_rate_hz", "rtl_rate_hz")})
    for f in (1500e6, 1501e6, 1502e6):
        assert cap.known_signal(con, f, 25e3, 0.0, 3) is None
    con.close()


def test_known_limit_is_off_unless_the_station_sets_it(tmp_path):
    st = station(tmp_path)
    assert float(cap.settings(st).get("known_cooldown_min")) == 0
    assert "known signals are not limited" in W.settings_line(cap.settings(st), survey=True)
    st2 = station(tmp_path / "b", {"known_cooldown_min": 360})
    line = W.settings_line(cap.settings(st2), survey=True)
    assert "a signal with 3 good captures records at most every 360 min" in line


def d(f, snr, w=12.5):
    return {"f_mhz": f, "snr_db": snr, "width_khz": w, "tags": [], "tol_khz": 10.0}


def test_survey_notes_a_known_signal_instead_of_taking_the_radio():
    t = W.Triggers({"threshold_db": 12, "passes": 2, "cooldown_min": 30}, known=lambda f, tol, w=0.0: 12345.0)
    assert t.feed([d(950.5, 30)], 1000.0)[0]["kind"] == "ambush"       # one pass: nothing known about it yet here
    t = W.Triggers({"threshold_db": 12, "passes": 2, "ambush_db": 99, "cooldown_min": 30}, known=lambda f, tol, w=0.0: 12345.0)
    assert t.feed([d(950.5, 30)], 1000.0) == []
    a = t.feed([d(950.5, 30)], 1030.0)
    assert a[0]["kind"] == "known" and a[0]["until"] == 12345.0
    assert t.feed([d(950.5, 30)], 1060.0) == []                         # and only once per cooldown


def test_known_row_keeps_the_event_catalogued_with_no_iq(tmp_path):
    st = station(tmp_path, {"band_tiers": []})
    rec = W.Recorder(st, cap.settings(st), R820T, ["rtl_sdr"], "chain", 29.7, Launches(scene(1, 1, 1), 1e9), quiet=True)
    W.known_row(rec, "survey_x", 0.0, {"f_mhz": 950.5, "snr_db": 30.0, "width_khz": 400.0, "until": time.time() + 60,
                                       "why": "2 passes, 30.0 dB, known signal"})
    r = rec.con.execute("SELECT * FROM captures").fetchone()
    assert r["iq_status"] == "known signal: not recorded" and not r["iq_bytes"] and r["receiver"] == "survey"
    assert "next capture allowed after" in r["notes"]


def test_fixed_watch_catalogues_events_of_a_known_signal_without_iq(tmp_path):
    st = station(tmp_path, {"capture_s": 2, "known_cooldown_min": 60, "cooldown_min": 0})
    f = 433.92e6
    con = cap.connect(st)
    add_good(con, 3, f=f, family="CW", start=time.time() - 100)
    con.close()
    L = Launches(scene(6.0, 0.5, 2.0), f)
    rec = W.Recorder(st, cap.settings(st), R820T, ["rtl_sdr"], "chain", 29.7, L, quiet=True, inline_characterise=True)
    W.fixed_watch(rec, f, 5.0, width_hz=20e3)
    new = rec.con.execute("SELECT * FROM captures WHERE fingerprint IS NOT NULL").fetchall()
    assert new and all(r["iq_status"] == "known signal" for r in new)
    assert not list(rec.pool.rglob("*.sigmf-data"))


# ---------------------------------------------------------------- newest after a restore

def write_rec(st, name, started):
    st.surveys.mkdir(parents=True, exist_ok=True)
    p = st.surveys / name
    p.write_text("x")
    p.with_suffix(".json").write_text(json.dumps({"started_local": started}))
    return p


def test_newest_goes_by_the_sidecar_not_the_order_a_restore_unpacked(tmp_path):
    """A restore gives every file a fresh creation time, oldest survey possibly last."""
    st = station(tmp_path)
    new = write_rec(st, "survey_LS_1h_b_20301101-0000.csv", "2030-11-01T00:00:00")
    time.sleep(0.05)
    old = write_rec(st, "survey_wide_5m_a_20301021-0200.csv", "2030-10-21T02:00:00")
    assert live.newest_recording(st) == new
    assert live.started_at(old) < live.started_at(new)


def test_newest_without_a_sidecar_falls_back_to_creation_time(tmp_path):
    st = station(tmp_path)
    st.surveys.mkdir(parents=True, exist_ok=True)
    a = st.surveys / "a.csv"
    a.write_text("x")
    time.sleep(0.05)
    b = st.surveys / "b.csv"
    b.write_text("x")
    assert live.newest_recording(st) == b
