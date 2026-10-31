"""Hunt mode after its first shakedown (2026-10-26).

The fast `live` range (one rtl_power_fftw hop), the line follower's hysteresis and
lock, the floor-level reading for broadband sources, the confirmation a hunt run
needs before it starts, and the live reader closing a one-hop pass at once.
"""
import json
import sys
import time
from pathlib import Path

import pytest

from conftest import ROOT

sys.path.insert(0, str(ROOT))

from marsad import hunt, live, probe, rpf      # noqa: E402
from marsad.config import Station              # noqa: E402

FORM = {"target": "printer", "probe": "stub-a", "where": "", "minutes": "2",
        "device": "0", "state": "unplugged"}


# ---------------------------------------------------------------- the live range
def test_live_spec_puts_the_target_a_quarter_hop_above_the_dc_spike():
    s = probe.live_spec(855.6)
    assert s["tune_mhz"] == pytest.approx(855.0)
    assert s["spec"] == "855M"
    assert s["clearance_mhz"] == pytest.approx(0.6)
    # inside the kept middle 75% of the hop (+/-0.9 MHz), 0.3 MHz clear of the cut
    assert s["lo"] < 855.6 < s["hi"] and s["hi"] - 855.6 == pytest.approx(0.3)


def test_compose_live_is_one_fftw_hop_at_more_than_5_spectra_a_second():
    st = Station.load(ROOT)
    d = probe.compose(st, {**FORM, "range": "live", "centre": "1633.47"})
    a = d["args"]
    assert a[:2] == ["custom", "2m"]
    assert a[a.index("--engine") + 1] == "rtl_power_fftw"
    assert a[a.index("--freq") + 1] == "1632.87M"
    assert a[a.index("--bins") + 1] == "512"
    assert a[a.index("--time") + 1] == "0.1"
    assert a[a.index("--rate") + 1] == "2400000"
    assert a[a.index("--overlap") + 1] == "0"
    assert "--integration" not in a
    g = d["geometry"]
    assert g["hops"] == 1 and g["reads_per_hop"] == 1
    # 0.1 s listening + 59 ms measured overhead = 0.159 s a spectrum
    assert g["spectra_per_s"] == pytest.approx(1 / 0.159, rel=0.02)
    assert g["spectra_per_s"] >= 5
    assert d["tag"] == "hunt-printer-unplugged-stub-a"
    assert d["tiled"] and d["engine"] == "rtl_power_fftw"
    assert d["rtl_power_line"].startswith("rtl_power_fftw -f 1632.87M -g 290 -b 512 -r 2400000 -t 0.1")
    assert d["warnings"] == []


def test_hunt_runs_default_to_hand_held_and_say_so_in_the_conditions():
    st = Station.load(ROOT)
    for rng in ("tile", "hunt", "live"):
        d = probe.compose(st, {**FORM, "range": rng, "centre": "855.6"})
        assert d["held"] == probe.HUNT_HELD_DEFAULT == "hand-held, moved around"
        assert hunt.HELD_RE.search(d["conditions"]).group(1) == "hand-held"
    # a wide catalogue run still has to say where the probe is
    with pytest.raises(ValueError, match="where the probe is held"):
        probe.compose(st, {**FORM, "range": "wide"})


def test_a_hunt_run_will_not_start_unconfirmed(tmp_path):
    st = Station.load(ROOT)
    runner = probe.ProbeRunner(st)
    with pytest.raises(ValueError, match="confirm what this hunt run will record first: target "
                                         "'printer', held 'hand-held, moved around'"):
        runner.start({**FORM, "range": "live", "centre": "855.6"})
    assert not runner.running()


# ---------------------------------------------------------------- the follower
def sweep(lines: dict, floor=-40.0, lo=850.0, hi=860.0, step_khz=10.0) -> dict:
    """A synthetic sweep {kHz: dB}: flat floor plus narrow lines {MHz: dB}."""
    out = {}
    k = lo * 1e3
    while k <= hi * 1e3:
        out[int(round(k))] = floor
        k += step_khz
    for f, v in lines.items():
        out[int(round(f * 1e3))] = v
    return out


def test_follower_does_not_jump_to_a_line_a_dB_stronger():
    f = hunt.Follower()
    r = f.update(sweep({855.6: -20.0, 858.0: -22.0}))
    assert r["f_mhz"] == 855.6 and r["over_db"] == 20.0
    # the other carrier is now 2 dB stronger, for ten sweeps: no move
    for _ in range(10):
        r = f.update(sweep({855.6: -22.0, 858.0: -20.0}))
        assert r["f_mhz"] == 855.6 and not r["switched"]
    assert f.switches == 0


def test_follower_moves_after_three_updates_of_a_clearly_stronger_line():
    f = hunt.Follower()
    f.update(sweep({855.6: -25.0}))
    for n in (1, 2):
        r = f.update(sweep({855.6: -25.0, 858.0: -15.0}))
        assert r["f_mhz"] == 855.6 and r["challenger_mhz"] == 858.0 and r["challenger_n"] == n
    r = f.update(sweep({855.6: -25.0, 858.0: -15.0}))
    assert r["f_mhz"] == 858.0 and r["switched"]
    # a challenger that blinks off resets the count
    f2 = hunt.Follower()
    f2.update(sweep({855.6: -25.0}))
    f2.update(sweep({855.6: -25.0, 858.0: -15.0}))
    f2.update(sweep({855.6: -25.0}))
    r = f2.update(sweep({855.6: -25.0, 858.0: -15.0}))
    assert r["f_mhz"] == 855.6 and r["challenger_n"] == 1


def test_follower_lets_go_of_a_line_that_has_gone():
    f = hunt.Follower()
    f.update(sweep({855.6: -20.0, 858.0: -30.0}))
    for _ in range(2):
        assert f.update(sweep({858.0: -30.0}))["f_mhz"] == 855.6
    assert f.update(sweep({858.0: -30.0}))["f_mhz"] == 858.0


def test_a_locked_line_stays_whatever_else_appears_and_follows_a_small_drift():
    f = hunt.Follower()
    f.update(sweep({855.6: -25.0}))
    for _ in range(6):
        r = f.update(sweep({855.61: -25.0, 858.0: -5.0}), lock_mhz=855.6)
        assert r["locked"] and r["f_mhz"] == 855.61 and r["over_db"] == 15.0


def test_the_dc_spike_never_wins():
    f = hunt.Follower()
    r = f.update(sweep({855.0: -5.0, 855.6: -25.0}), hop_centres_hz=[855_000_000])
    assert r["f_mhz"] == 855.6


def test_floor_mode_hears_a_broadband_source_the_line_reading_misses():
    """The shakedown: the blade on a Wi-Fi bulb base lifted the tile's median from
    -31 to -15 dB, and the line over that floor fell."""
    f = hunt.Follower()
    quiet = f.update(sweep({855.6: -11.0}, floor=-31.0), mode="floor")
    assert quiet["value_db"] == 0.0 and quiet["over_db"] == 20.0
    hot = f.update(sweep({855.6: -3.0}, floor=-15.0), mode="floor")
    assert hot["floor_db"] == -15.0 and hot["quiet_db"] == -31.0
    assert hot["floor_rise_db"] == 16.0 and hot["value_db"] == 16.0
    assert hot["over_db"] == 12.0          # the line reading FELL, from 20 to 12
    back = f.update(sweep({855.6: -11.0}, floor=-31.0), mode="floor")
    assert back["value_db"] == 0.0


def test_typed_mode_reads_the_centre_frequency():
    f = hunt.Follower()
    r = f.update(sweep({855.6: -20.0, 858.0: -5.0}), mode="typed", centre_mhz=855.6)
    assert r["f_mhz"] == 855.6 and r["level_db"] == -20.0


# ---------------------------------------------------------------- the live reader
def one_hop_dat(tmp_path: Path, blocks: int) -> Path:
    """A one-hop rtl_power_fftw recording, as the live range writes it."""
    dat = tmp_path / "survey_custom_2m_hunt-x-stub-a_20261026-2300.dat"
    side = {"engine": "rtl_power_fftw", "preset": "custom", "receiver": "xtr",
            "csv": dat.name, "started_local": "2026-10-26T23:00:00",
            "rtl_power_fftw": {"freq_low_mhz": 853.8, "freq_high_mhz": 856.2, "bins": 16,
                               "rate_hz": 2400000, "time_per_hop_s": 0.1, "overlap_pct": 0,
                               "crop": 0.25, "gain_db": 29.0, "bin_khz": 150.0},
            "geometry": {"hops": 1, "covered_mhz": [854.1, 855.9], "sweep_s": 0.159}}
    dat.with_suffix(".json").write_text(json.dumps(side), encoding="utf-8")
    lines = []
    for b in range(blocks):
        lines += ["# rtl-power-fftw output", "# Acquisition start: 2026-10-26 23:00:01 UTC",
                  "# Acquisition end: 2026-10-26 23:00:01 UTC", "#", "# frequency [Hz] power spectral density [dB/Hz]"]
        for i in range(16):
            f = 853_800_000 + i * 150_000
            lines.append(f"{f} {-60.0 + (20.0 if i == 12 else 0.0) + b}")
        lines.append("")
    dat.write_text("\n".join(lines) + "\n", encoding="ascii")
    return dat


def test_a_one_hop_pass_closes_the_moment_its_block_ends(tmp_path):
    t = live.DatTailer(one_hop_dat(tmp_path, 3), 1200, 400)
    t.poll()
    # three blocks, three complete passes: none held back waiting for a successor
    assert len(t.sweeps) == 3 and not t.cur
    assert t.medians[-1] == pytest.approx(-58.0)


def test_hunt_reading_steps_once_per_sweep_however_often_it_is_asked(tmp_path):
    t = live.DatTailer(one_hop_dat(tmp_path, 2), 1200, 400)
    t.poll()
    name = "unit-test-reading"
    live.FOLLOWERS.pop(name, None)
    a = live.hunt_reading(t, name)
    for _ in range(5):
        b = live.hunt_reading(t, name)
    assert a["sweeps"] == b["sweeps"] == 2
    assert live.FOLLOWERS[name]["f"].updates == 1
    r = b["reading"]
    assert r["f_mhz"] == pytest.approx(855.6) and r["over_db"] == pytest.approx(20.0)
    with pytest.raises(ValueError):
        live.hunt_reading(t, name, mode="loudest")
    live.FOLLOWERS.pop(name, None)


def test_updates_per_second_counts_completed_sweeps(tmp_path):
    t = live.DatTailer(one_hop_dat(tmp_path, 1), 1200, 400)
    now = time.time()
    t.finish_times = [now - 2.0 + i * 0.16 for i in range(13)]
    assert t.updates_per_s() == pytest.approx(1 / 0.16, rel=0.01)


def test_a_locked_line_that_fades_keeps_its_frequency():
    f = hunt.Follower()
    f.update(sweep({855.6: -25.0}))
    r = f.update(sweep({858.0: -5.0}), lock_mhz=855.6)
    assert r["locked"] and r["f_mhz"] == 855.6 and r["over_db"] == 0.0


def test_spectrum_strip_keeps_a_narrow_line_and_marks_the_dc_exclusion():
    s = hunt.spectrum_strip(sweep({855.6: -10.0}), hop_centres_hz=[855_000_000], cols=60)
    assert s["lo_mhz"] == 850.0 and s["hi_mhz"] == 860.0 and len(s["db"]) == 60
    assert max(s["db"]) == -10.0                       # one bin in 1000 survives the squeeze
    assert s["excluded_mhz"] == [[854.975, 855.025]]   # +/-25 kHz round the hop centre


def test_hunt_reading_carries_the_spectrum(tmp_path):
    t = live.DatTailer(one_hop_dat(tmp_path, 1), 1200, 400)
    t.poll()
    live.FOLLOWERS.pop("unit-strip", None)
    d = live.hunt_reading(t, "unit-strip")
    assert d["spectrum"]["db"] and d["spectrum"]["lo_mhz"] < 855.6 < d["spectrum"]["hi_mhz"]
    live.FOLLOWERS.pop("unit-strip", None)


def test_the_wand_is_a_quick_choice_for_hunt_runs():
    c = probe.catalog(Station.load(ROOT))
    assert c["hunt_held_choices"][0] == c["hunt_held_default"] == "hand-held, moved around"
    assert "hand-held on a wand, moved around" in c["hunt_held_choices"]
    st = Station.load(ROOT)
    d = probe.compose(st, {**FORM, "range": "live", "centre": "855.6",
                           "where": "hand-held on a wand, moved around"})
    assert "held hand-held on a wand" in d["conditions"]


def test_status_finds_an_fftw_recording_in_the_run_log():
    """The live range writes a .dat; the page only follows a run whose file the
    status names (found broken in the end-to-end check, 2026-10-26)."""
    st = Station.load(ROOT)
    runner = probe.ProbeRunner(st)
    plan = probe.compose(st, {**FORM, "range": "live", "centre": "855.6"})
    runner.job = {"plan": plan, "started": time.time(), "rec_t0": None,
                  "started_local": "2026-10-26T23:47:00", "stem": None, "stopped": False,
                  "logged": True, "summary": None, "row": None, "log_error": None}
    runner.proc = type("Done", (), {"returncode": 0, "poll": lambda self: 0})()
    runner._log_text = lambda: ("    writing     surveys/survey_custom_2m_hunt-x_20261026-2347.dat"
                                "  (+ .log with the program's own messages)\n")
    d = runner.status()
    assert d["stem"] == "survey_custom_2m_hunt-x_20261026-2347"
    assert d["csv"] == "survey_custom_2m_hunt-x_20261026-2347.dat"
    assert d["planned_per_s"] == pytest.approx(6.29, abs=0.01)


def test_a_dongle_that_drops_off_usb_is_noticed_in_the_log(tmp_path):
    """23:51:57 on 2026-10-26: the XTR left the bus and rtl_power_fftw looped on
    failed register writes instead of exiting."""
    log = tmp_path / "x.log"
    log.write_text("Device tuned to: 855000000 Hz\nAcquisition started\n", encoding="utf-8")
    assert not rpf.device_lost(log)
    with open(log, "a", encoding="utf-8") as fh:
        fh.write("Error: dropped samples.\n" + "rtlsdr_write_reg failed with -4\n" * 2)
    assert not rpf.device_lost(log)                     # a hiccup is not a lost device
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(("rtlsdr_write_reg failed with -4\n" * 2 + "Error: dropped samples.\n") * 40)
    assert rpf.device_lost(log)
    assert not rpf.device_lost(tmp_path / "missing.log")
