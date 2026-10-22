"""rtl_power_fftw engine: hop plan, geometry, fingerprint, command line, parser.

The hop arithmetic is Plan::Plan from rtl-power-fftw's acquisition.cxx, so the
centres here are what the program itself prints as "Tuning to N Hz". The
parser is pinned on a file in the program's output format (tests/data/).
A run from this engine must never share a fingerprint with an rtl_power run.
"""
import json
import sys
from pathlib import Path

import pytest

from conftest import PY, ROOT, run

sys.path.insert(0, str(ROOT))
from marsad import rpf                                     # noqa: E402
from marsad.rtlpower import engine_of, engine_section, fingerprint   # noqa: E402

DATA = Path(__file__).resolve().parent / "data"


def test_top_slice_hop_plan():
    # 2147M:2187M at 2.4 MS/s with 25% overlap: 22 hops, first centre half a
    # rate above the low edge, spacing rate - int overhang, 17 Hz of integer slop
    # at the top, exactly as the C++ computes it.
    c = rpf.plan_hops(2147e6, 2187e6, 2_400_000, 25)
    assert len(c) == 22
    assert c[0] == 2_148_200_000
    assert c[1] - c[0] == 1_790_477
    assert c[-1] == 2_185_800_017
    assert all(b - a == 1_790_477 for a, b in zip(c, c[1:]))


def test_single_frequency_is_one_hop():
    assert rpf.plan_hops(0, 0, 2_400_000, 0, centre_hz=1_420_405_800) == [1_420_405_800]
    lo, hi, c = rpf.parse_freq("1420.4058M", 2_400_000)
    assert c == pytest.approx(1420.4058)
    assert (lo, hi) == pytest.approx((1419.2058, 1421.6058))


def test_top_geometry():
    g = rpf.geometry(2147, 2187, 2_400_000, 128, 1.0, 25, 0.25, 1800)
    assert g["hops"] == 22 and g["fft"] == 128
    assert g["bin_khz_predicted"] == pytest.approx(18.75)
    assert g["kept_bins_per_hop"] == 96 and g["bins"] == 2112
    assert g["repeats_per_hop"] == 18750          # ceil(2.4e6 * 1 / 128)
    assert g["listen_per_hop_s"] == pytest.approx(1.0)
    # 4.8 MB per hop = three 1.6 MB reads: 59 ms + 2 x 16 ms = 91 ms overhead,
    # the 1.091 s per hop measured on 2026-10-22 (132 hops in 144 s)
    assert g["reads_per_hop"] == 3
    assert g["overhead_per_hop_s"] == pytest.approx(0.091)
    assert g["sweep_s"] == pytest.approx(22 * 1.091)
    assert g["sweeps"] == 75
    # a 0.05 s hop needs one 240 KB read: just the 59 ms
    f = rpf.geometry(1930, 1990, 2_400_000, 64, 0.05, 25, 0.25, 60)
    assert f["reads_per_hop"] == 1 and f["overhead_per_hop_s"] == pytest.approx(0.059)
    assert g["covered_mhz"] == pytest.approx([2147.3, 2186.7])


def test_command_line_uses_tenths_of_a_db():
    cmd = rpf.build_command("rtl_power_fftw", "2147M:2187M", 29.0, 128, 2_400_000, 1.0,
                            25, 1800, device=1)
    assert cmd == ["rtl_power_fftw", "-f", "2147M:2187M", "-g", "290", "-b", "128",
                   "-r", "2400000", "-t", "1", "-e", "1800", "-q", "-o", "25", "-d", "1"]


def _fftw_sidecar(**over):
    sec = {"freq_spec": "2147M:2187M", "freq_low_mhz": 2147.0, "freq_high_mhz": 2187.0,
           "freq_high_mhz_effective": 2187.0, "centre_mhz": None, "bins": 128,
           "rate_hz": 2_400_000, "time_per_hop_s": 1.0, "overlap_pct": 25.0, "crop": 0.25,
           "gain_db": 29.0, "bin_khz": 18.75, "device": 1, "baseline": None, "window": None}
    sec.update(over)
    return {"schema": 1, "engine": "rtl_power_fftw", "rtl_power_fftw": sec, "receiver": "xtr"}


def test_fingerprint_names_the_engine_and_never_matches_rtl_power():
    sc = _fftw_sidecar()
    fp = fingerprint(sc, None)
    assert fp == "2147.000-2187.000 MHz | ~18.7500 kHz | g29 | t1 | N128 | r2.4M | o25 | c0.25 | rtl_power_fftw | xtr"
    assert fingerprint(sc, 18.75) == fp.replace("~18.7500", "18.7500")
    assert engine_of(sc) == "rtl_power_fftw" and engine_section(sc)["bins"] == 128
    # The same range, gain and receiver recorded by rtl_power: a different line.
    rp = {"rtl_power": {"freq_low_mhz": 2147.0, "freq_high_mhz": 2187.0,
                        "freq_high_mhz_effective": 2147.483648, "bin_khz": 18.75,
                        "gain_db": 29.0, "integration_s": 1, "crop": 0.25, "fir": 9},
          "receiver": "xtr"}
    assert engine_of(rp) == "rtl_power"
    assert fingerprint(rp, 18.75) != fp
    assert "rtl_power_fftw" not in fingerprint(rp, 18.75)


def test_compare_sidecars_refuses_an_engine_mix():
    from marsad.analyze import compare_sidecars
    a = _fftw_sidecar()
    b = {"rtl_power": {"gain_db": 29.0, "integration_s": 1, "crop": 0.25, "fir": 9,
                       "bin_khz": 18.75}, "receiver": "xtr", "chain": {}}
    lines = "\n".join(compare_sidecars("a", a, "b", b, 18.75, 18.75))
    assert "engine differs" in lines and "Settings mismatch" in lines


def test_parse_log_reads_the_programs_stderr():
    text = ("Available gains (in 1/10th of dB): -10, 15, 40, 65, 90, 115, 140, 165, 190, 215, 240, 290, 340, 420\n"
            "Selected nearest available gain: 290 (29 dB)\n"
            "Actual sample rate: 2400000 Hz\n"
            "Number of bins: 128\n"
            "Estimated time of measurements: 1 seconds\n"
            "Number of averaged spectra: 18750\n"
            "Tuning to 2148200000 Hz (try 1)\nDevice tuned to: 2148200000 Hz\n"
            "Tuning to 2149990477 Hz (try 1)\nDevice tuned to: 2149990477 Hz\n"
            "Error: dropped samples.\n"
            "Unable to tune to 2185800017. Dropping from frequency list.\n"
            "Buffer queue histogram: 0 0 0 6 34 1 \n"
            "Buffer queue histogram: 2 0 0 6 34 1 \n")
    log = rpf.parse_log(text)
    assert log["rate_hz_actual"] == 2_400_000 and log["gain_tenths"] == 290
    assert log["tuned_hz"] == [2_148_200_000, 2_149_990_477]
    assert log["dropped_hops"] == [2_185_800_017]
    assert log["dropped_sample_errors"] == 1 and log["buffer_underruns"] == 2
    assert log["repeats"] == 18750


def test_parser_on_sample_file():
    """tests/data/rpf_synthetic.dat: 3 hops x 2 passes, 64 bins at 2.4 MS/s, one
    carrier planted 20 dB up in bin 40 of the middle hop."""
    from marsad.analyze import load_rtl_power_fftw, survey_engine
    path = DATA / "rpf_synthetic.dat"
    assert survey_engine(path) == "rtl_power_fftw"
    s = load_rtl_power_fftw(path, crop=0.0)
    assert s.power_db.shape[0] == 2                     # two passes = two sweeps
    assert s.step_hz == pytest.approx(37_500.0)        # 2.4e6 / 64
    assert list(s.hop_centres_hz) == [1931200000.0, 1932990477.0, 1934780954.0]
    # bins are snapped to a common grid of whole steps, so within half a bin
    assert abs(s.freqs_hz[0] - (1931200000 - 32 * 37500)) <= 37500 / 2
    # the planted carrier: hop 2, bin 40 -> centre + 8 bins
    f_car = 1932990477 + 8 * 37500
    col = int(abs(s.freqs_hz - f_car).argmin())
    assert s.power_db[:, col].mean() > s.power_db[:, col - 3].mean() + 15
    # cropping drops 8 bins off each end of every hop
    c = load_rtl_power_fftw(path, crop=0.25)
    assert abs(c.freqs_hz[0] - (1931200000 - 24 * 37500)) <= 37500 / 2
    assert c.power_db.shape[1] < s.power_db.shape[1]
    # local-time conversion: the file says 20:00 UTC; the index is naive local time
    assert s.times[0].tzinfo is None and s.times[1] > s.times[0]


def test_dry_run_of_the_top_preset():
    out = run([PY, "-m", "marsad", "run", "top", "30m", "--tag", "pytest", "--dry-run", "--device", "1"])
    assert "engine: rtl_power_fftw" in out
    assert "22 hops x 2.4000 MHz (spaced 1.7905), 128 bins, 18.7500 kHz bins, 2,112 logged" in out
    assert "-f 2147M:2187M -g 290 -b 128 -r 2400000 -t 1 -e 1800 -q -o 25 -d 1" in out
    assert "--dry-run: nothing recorded" in out


def test_h1_preset_is_a_single_hop_and_allows_zero_overlap():
    out = run([PY, "-m", "marsad", "run", "h1", "2h", "--tag", "pytest", "--dry-run"])
    assert "one hop centred on 1420.41 MHz" in out
    assert "2048 bins, 1.1719 kHz bins" in out
    assert "-f 1420.4058M -g 496 -b 2048 -r 2400000 -t 60 -e 7200 -q" in out


def test_parser_on_captured_pcs_sample():
    """tests/data/rpf_pcs_20261022.dat: the first two passes of a real capture on
    the XTR, 2026-10-22 07:44 local: 1930M:1990M at 2.4 MS/s, -b 64 -t 1 -o 25,
    g29, UWB antenna, the PCS downlink block. 33 hops per pass."""
    from marsad.analyze import load_rtl_power_fftw, survey_engine
    import numpy as np
    path = DATA / "rpf_pcs_20261022.dat"
    assert survey_engine(path) == "rtl_power_fftw"
    s = load_rtl_power_fftw(path, crop=0.25)
    assert s.power_db.shape == (2, 1584)               # 33 hops x 48 kept bins
    assert s.step_hz == pytest.approx(37_500.0)
    assert [int(c) for c in s.hop_centres_hz] == rpf.plan_hops(1930e6, 1990e6, 2_400_000, 25)
    assert s.freqs_hz[0] == pytest.approx(1_930_312_500.0)
    assert s.freqs_hz[-1] == pytest.approx(1_989_675_000.0)
    assert not np.isnan(s.power_db).any()
    med = np.nanmedian(s.power_db, axis=0)
    assert float(np.nanmedian(med)) == pytest.approx(-53.93, abs=0.01)
    assert float(np.nanmax(med)) == pytest.approx(-46.98, abs=0.01)
    assert (s.times[1] - s.times[0]).total_seconds() == 36.0   # one 33-hop pass at -t 1
    assert load_rtl_power_fftw(path, crop=0.0).power_db.shape == (2, 1600)
