"""scan_geometry() reproduces rtl_power's own arithmetic.

Every figure here was verified against a real run (CLAUDE.md, run_survey.py
docstring, and the geometry blocks of the sidecars in surveys/). If one of
these moves, the pre-run summary and the scan builder are lying.
"""
import sys

import pytest

from conftest import TOOLS

sys.path.insert(0, str(TOOLS))
from run_survey import scan_geometry, RTL_POWER_MAX_MHZ  # noqa: E402

LS_TOP = RTL_POWER_MAX_MHZ   # 2147.483648, rtl_power's 2^31 Hz ceiling


def test_ceiling_is_two_to_the_31():
    assert RTL_POWER_MAX_MHZ == pytest.approx(2147.483648)


def test_ls_geometry_matches_measured_run():
    # 1241M:2148M:50k -i 120, 1 h. rtl_power reported 2797788.08 Hz and
    # "PLL not locked" at 1242.049 MHz, its first hop centre.
    g = scan_geometry(1241.0, LS_TOP, 50.0, 120, 3600)
    assert g["hops"] == 432
    assert g["fft"] == 64
    assert g["sample_rate_hz"] == pytest.approx(2797789.037, abs=0.01)
    assert g["bin_khz_predicted"] == pytest.approx(43.7154537, abs=1e-6)
    assert g["kept_bins_per_hop"] == 48
    assert g["bins"] == 20736
    assert g["hop_centres_mhz"][0] == pytest.approx(1242.049171, abs=1e-6)
    assert g["sweeps"] == 30
    assert g["retune_s_per_sweep"] == pytest.approx(20.304)
    assert g["listening_frac"] == pytest.approx(0.8308)


def test_baseline_700_1700_geometry_matches_sidecar():
    # From surveys/survey_custom_1h_baseline-term_20261016-2138.json
    g = scan_geometry(700.0, 1700.0, 50.0, 120, 3600)
    assert g["hops"] == 477
    assert g["hop_mhz"] == pytest.approx(2.096436, abs=1e-6)
    assert g["sample_rate_hz"] == pytest.approx(2795248.078267, abs=1e-3)
    assert g["bin_khz_predicted"] == pytest.approx(43.675751, abs=1e-6)
    assert g["bins"] == 22896
    assert g["retune_s_per_sweep"] == pytest.approx(22.419)
    assert g["listening_frac"] == pytest.approx(0.813175, abs=1e-6)
    assert g["listen_per_freq_s"] == pytest.approx(6.13717, abs=1e-5)
    assert g["csv_mb"] == pytest.approx(6.35364, abs=1e-5)


def test_burst23_hop_centres_clear_the_target():
    # The preset starts at 1263M so the centres avoid 1265.44 MHz; at 1264M they
    # sat on 1265.000/1267.000 and the DC spikes masked the radar.
    g = scan_geometry(1263.0, 1267.0, 5.0, 2, 3600)
    assert g["hops"] == 2 and g["fft"] == 1024
    assert g["bin_khz_predicted"] == pytest.approx(2.6041667, abs=1e-6)
    assert g["hop_centres_mhz"] == pytest.approx([1264.0, 1266.0])
    bad = scan_geometry(1264.0, 1268.0, 5.0, 2, 3600)
    assert bad["hop_centres_mhz"] == pytest.approx([1265.0, 1267.0])
