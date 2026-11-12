"""Relay 028: an R820T's refused tune is not "direct sampling inside the range".

The driver words below are the real shapes the RTL-SDR Blog driver printed for an R820T at the top of its range
(copied from a real sweep log; only the frequencies differ from the station's own record, which the private suite
replays in full). Past about 1766 MHz the driver has no synthesiser setting for most frequencies:

  refused, then the driver's fallback:   No valid PLL values -> r82xx_set_freq: failed=-1 -> Disabled -> Enabled
  refused on the real tune:               PLL not locked -> Disabled -> No valid PLL values -> failed
  a real not-locked:                      PLL not locked -> Disabled -> PLL not locked -> Tuned to
  a scattered "lock" above the refusals:  PLL not locked -> Disabled -> Tuned to

The earlier reading took the fallback's "Enabled direct sampling mode" for the radio being in HF direct sampling at
that frequency, flagged it UNEXPECTED, and the setup step ended in "Needs attention". No radio is opened."""
from __future__ import annotations

from marsad import tuner_sweep as TS

OPEN = "Found Rafael Micro R820T tuner\nEnabled direct sampling mode, input 2\nSampling at 1024000 S/s.\n"


def refused_with_fallback(hz):
    return (OPEN + f"[R82XX] No valid PLL values for {int(hz + 1.7e6)} Hz!\nr82xx_set_freq: failed=-1\n"
            "Disabled direct sampling mode\nEnabled direct sampling mode, input 2\n")


def refused_on_tune(hz):
    return (OPEN + "[R82XX] PLL not locked!\nDisabled direct sampling mode\n"
            f"[R82XX] No valid PLL values for {int(hz + 3.57e6)} Hz!\nr82xx_set_freq: failed=-1\n")


def not_locked(hz):
    return OPEN + "[R82XX] PLL not locked!\nDisabled direct sampling mode\n[R82XX] PLL not locked!\n" \
                  f"Tuned to {int(hz)} Hz.\n"


def locked(hz):
    return OPEN + "[R82XX] PLL not locked!\nDisabled direct sampling mode\n" \
                  f"Tuned to {int(hz)} Hz.\nTuner gain set to automatic.\n"


def hf(hz):
    return OPEN + f"Tuned to {int(hz)} Hz.\nTuner gain set to automatic.\nUser cancel, exiting...\n"


def test_each_real_shape_is_read_as_what_it_is():
    assert TS.classify(refused_with_fallback(1812e6)) == TS.REFUSED          # was "direct"
    assert TS.classify(refused_on_tune(1766.7e6)) == TS.REFUSED
    assert TS.classify(not_locked(1852e6)) == TS.UNLOCK
    assert TS.classify(locked(512e6)) == TS.LOCK
    assert TS.classify(hf(12e6)) == TS.DIRECT                                # real HF direct sampling stays direct
    assert "refused" in TS.STATE_WORD[TS.REFUSED]


def patchy_r820t(hz):
    """Locks 24 to 1766.5; above that the driver's patchwork: refusals with a few scattered "locks" between."""
    f = hz / 1e6
    if f < 24:
        return hf(hz)
    if f <= 1766.5:
        return locked(hz)
    if f <= 1770:
        return refused_on_tune(hz)
    if round(f) % 120 == 52:
        return locked(hz)                       # a scattered island, as the real driver gave at 1972, 2092, 2208
    if round(f) % 120 == 32:
        return not_locked(hz)
    return refused_with_fallback(hz)


def test_the_range_ends_at_the_first_refusal_and_islands_above_are_not_counted():
    res = TS.sweep(lambda hz: (TS.classify(patchy_r820t(hz)), patchy_r820t(hz)), 12, 2208, 20, 0.5)
    assert abs(res["low"] - 24) <= 0.5 and abs(res["high"] - 1766.5) <= 0.5 and res["gaps"] == []
    assert res["high_kind"] == TS.REFUSED and res["direct_above_range"] == []
    assert res["untrusted_locks"] and min(res["untrusted_locks"]) > 1766.5
    assert "up" not in res["extended"]
    lines = TS.report_lines(res, 20, 0.5)
    assert not any("UNEXPECTED" in x for x in lines)
    assert any("doesn't report lock reliably there" in x for x in lines)


def test_a_radio_with_no_refusals_is_read_as_before():
    """An E4000-like radio (a clean gap, "PLL not locked" only) has nothing refused, so nothing changes."""
    def e4k(hz):
        f = hz / 1e6
        s = "Found Elonics E4000 tuner\n[E4K] PLL not locked for 0 Hz!\nSampling at 1024000 S/s.\n"
        if not (52.0 <= f <= 2186.0 and not 1094.0 < f < 1239.0):
            s += f"[E4K] PLL not locked for {int(hz)} Hz!\n"
        return s + f"Tuned to {int(hz)} Hz.\n"
    res = TS.sweep(lambda hz: (TS.classify(e4k(hz)), e4k(hz)), 26, 2750, 20, 0.5)
    assert res["refused_from"] is None and res["untrusted_locks"] == []
    assert len(res["gaps"]) == 1 and abs(res["high"] - 2186) <= 0.5
