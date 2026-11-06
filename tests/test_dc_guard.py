"""The tuner's own bump at the tuned frequency must not decide a level.

A narrow cut keeps the signal a few tens of kHz off the tuned frequency, so the watched window
spans DC, where an R820T/R860 shows a small steady bump. Synthetic stream: noise, a strong
constant tone exactly at DC (the bump) and a weak line at the target. The bump must not be the
event's peak, must not reach the keep level and must not set the grade. Stored data are unchanged:
the fingerprint still holds the DC bins. No radio, no files.
"""
import numpy as np
import pytest

from marsad import capture as cap

SIGMA = 0.02


def stream(plan, seconds, dc_db, target_db, seed=1):
    """Noise plus a tone at DC and a tone at the target, each set to its level per FFT bin over the noise."""
    rate = plan["rtl_rate_hz"]
    nfft = cap.fft_size(rate)
    n = int(rate * seconds)
    rng = np.random.default_rng(seed)
    x = SIGMA / np.sqrt(2) * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    gain = 0.5 / np.sqrt(0.375)                              # coherent gain of the rms-normalised Hann window

    def amp(db):
        return np.sqrt(10 ** (db / 10) * SIGMA ** 2 / (nfft * gain ** 2))

    t = np.arange(n) / rate
    off = plan["signal_hz"] - plan["tune_hz"]
    x = x + amp(dc_db) + amp(target_db) * np.exp(2j * np.pi * off * t)
    return x.astype(np.complex64)


def run(plan, x, **kw):
    opened = []
    s = cap.Session(plan, "open", mode="now", duration_s=len(x) / plan["rtl_rate_hz"], capture_s=2.0,
                    open_writer=lambda sess, epoch: opened.append(epoch) or Writer(), **kw)
    for i in range(0, len(x) - s.block + 1, s.block):
        s.feed(x[i:i + s.block])
    s.finish()
    return s, opened


class Writer:
    bytes = raw_samples = 0

    def write(self, x):
        self.raw_samples += len(x)

    def annotate(self, *a):
        pass

    def close(self):
        return "nowhere"


@pytest.fixture
def plan():
    p = cap.plan_capture(1450e6, 8e3, "r860")                # narrow: signal 24 kHz above the tuned frequency
    assert abs(p["signal_hz"] - p["tune_hz"]) < p["width_hz"] / 2   # so DC lies inside the watched window
    return p


def test_the_bump_does_not_decide_peak_keep_or_grade(plan):
    x = stream(plan, 3.0, dc_db=23.0, target_db=7.0)
    s, opened = run(plan, x, keep_min_db=12.0, keep_check_s=1.0)
    ev = s.events[0]
    dc = plan["tune_hz"] - plan["signal_hz"]
    # the bump really is the loudest thing in the window: judged with every bin it would pass the keep level
    assert cap.group_level(ev["rows"], ev["floor"], s.edges) >= 12.0
    assert ev["snr_peak_db"] < 9.0                            # the target's own level, not the bump's
    assert abs(ev["peak_rel_hz"] - dc) > cap.DC_GUARD_HZ
    assert abs(ev["peak_rel_hz"]) < 2e3                       # the peak is the target line
    assert opened == [] and s.below_keep == 1 and ev["iq_refused"] == cap.BELOW_KEEP
    assert cap.grade_row({"snr_peak_db": ev["snr_peak_db"]})[0] in ("weak", "empty")
    # stored as it was: the DC bins are still in the fingerprint's rows
    k = int(np.argmin(np.abs(ev["f_rel_hz"] - dc)))
    assert 10 * np.log10(np.median(ev["rows"][:, k] / ev["floor"][k])) > 10


def test_a_strong_target_beside_the_bump_is_still_kept(plan):
    x = stream(plan, 3.0, dc_db=23.0, target_db=24.0)
    s, opened = run(plan, x, keep_min_db=12.0, keep_check_s=1.0)
    ev = s.events[0]
    assert len(opened) == 1 and s.below_keep == 0
    assert ev["snr_peak_db"] >= 12.0 and abs(ev["peak_rel_hz"]) < 2e3


def test_judge_mask_leaves_out_only_the_bins_near_dc():
    f = np.arange(-30, 31) * 1e3
    m = cap.judge_mask(f, -22e3, 1e3)
    assert not m[np.abs(f + 22e3) <= 2.5e3].any() and m[np.abs(f + 22e3) > 2.5e3].all()
    assert cap.judge_mask(f, -500e3, 1e3).all()              # DC outside the window: nothing left out
