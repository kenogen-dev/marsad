"""The recorded A/B figures, reproduced through the command lines that produced them.

CLAUDE.md records each of these to the last digit. They are what the station's
error budget rests on, so they are pinned here as text the tools must print.
"""
import json
import re
import sys

from conftest import PY, TOOLS, run, report_dir, survey_csv


def test_spur_stats_usb_bulkhead_pair():
    # USB bulkhead bond repair, 2026-10-14: mean -0.14 dB, std 1.36, n=31, SE 0.24
    a = report_dir("survey_LS_45m_terminated_20261014-1706")
    b = report_dir("survey_LS_45m_terminated-usbfix_20261014-1818")
    out = run([PY, TOOLS / "spur_stats.py", a / "bins.csv", b / "bins.csv"])
    assert "n = 31 harmonics" in out
    assert re.search(r"mean change\s+-0\.14 dB", out)
    assert re.search(r"std / SE\s+1\.36 / 0\.24 dB", out)
    assert "NOT SIGNIFICANT" in out


def test_compare_runs_a1_a2_control():
    # A1 vs A2 cage-off control, 2026-10-17: median +0.050 dB, family -0.14 dB,
    # SE 0.41, n=70. compare_runs.py resolves the two tags against reports/.
    report_dir("survey_custom_1h_baseline-term_20261016-2138")
    report_dir("survey_custom_1h_cage-off-2_20261017-0121")
    out = run([PY, TOOLS / "compare_runs.py", "baseline-term", "cage-off-2"])
    assert "700.0-1700.0 MHz | 50.0 kHz | g29.7 | i120 | c0.25 | F9 | v5  (match)" in out
    assert re.search(r"WHOLE-BAND MEDIAN\s+-35\.890 -> -35\.840 dB\s+change \+0\.050 dB", out)
    assert "n = 70 harmonics" in out
    assert re.search(r"mean change\s+-0\.14 dB", out)
    assert re.search(r"std / SE\s+3\.47 / 0\.41 dB", out)
    assert "NOT SIGNIFICANT (0.3 sigma)" in out


def test_drift_six_hour_terminated_run():
    # 6 h terminated run 2026-10-17: settles by sweep 4, total -0.35 dB, settled std 0.025
    survey_csv("survey_custom_6h_overnight-noprinter_20261017-0236")
    out = run([PY, TOOLS / "drift.py", "overnight-noprinter"])
    assert re.search(r"sweeps\s+180", out)
    assert re.search(r"total change\s+-0\.350 dB", out)
    assert re.search(r"settled by sweep\s+4 of 180", out)
    assert re.search(r"std\s+0\.025 dB", out)
    assert "VERDICT: PASS" in out


def test_fingerprint_wording_is_stable():
    sys.path.insert(0, str(TOOLS))
    from run_survey import fingerprint
    side = json.loads(survey_csv("survey_custom_1h_baseline-term_20261016-2138")
                      .with_suffix(".json").read_text(encoding="utf-8"))
    fp = fingerprint(side, side["rtl_power"]["bin_khz_actual"])
    assert fp == "700.000-1700.000 MHz | 43.6758 kHz | g29.7 | i120 | c0.25 | F9 | v5"
