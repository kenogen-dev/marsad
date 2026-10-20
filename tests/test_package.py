"""The packaged `sdr` command gives the same answers as the tools/ scripts.

Same figures as test_statistics.py and test_analyze_golden.py, reached through
`python -m sdrstation ...` instead of `py tools/<script>.py`. Also covers the
pieces that only exist in the package: station.json loading and migration,
the retune calibration parser, and the command-line front end.
"""
import filecmp
import json
import re
import sys

import pytest

from conftest import PY, ROOT, run, report_dir, survey_csv

SDR = [PY, "-m", "sdrstation"]
sys.path.insert(0, str(ROOT))


def test_help_lists_every_command():
    out = run([*SDR, "--help"])
    for cmd in ("run", "analyze", "compare-runs", "spur-stats", "drift", "archive", "live",
                "calibrate-retune", "init", "config"):
        assert re.search(rf"^\s+{re.escape(cmd)}\s", out, re.M), cmd


def test_dry_run_prints_the_ls_geometry():
    out = run([*SDR, "run", "LS", "1h", "--tag", "pytest", "--dry-run"])
    assert "432 hops x 2.0983 MHz, FFT 64, 43.7155 kHz actual bins, 20,736 logged" in out
    assert "-f 1241M:2148M:50k -g 29 -i 30 -c 0.25 -F 9 -e 1h" in out
    assert "--dry-run: nothing recorded" in out


def test_spur_stats_matches_tools_script():
    a = report_dir("survey_LS_45m_terminated_20261014-1706")
    b = report_dir("survey_LS_45m_terminated-usbfix_20261014-1818")
    out = run([*SDR, "spur-stats", a / "bins.csv", b / "bins.csv"])
    assert re.search(r"mean change\s+-0\.14 dB", out)
    assert re.search(r"std / SE\s+1\.36 / 0\.24 dB", out)
    assert "n = 31 harmonics" in out


def test_compare_runs_matches_tools_script():
    report_dir("survey_custom_1h_baseline-term_20261016-2138")
    report_dir("survey_custom_1h_cage-off-2_20261017-0121")
    out = run([*SDR, "compare-runs", "baseline-term", "cage-off-2"])
    assert re.search(r"change \+0\.050 dB", out)
    assert re.search(r"std / SE\s+3\.47 / 0\.41 dB", out)
    assert "n = 70 harmonics" in out


def test_drift_matches_tools_script():
    survey_csv("survey_custom_6h_overnight-noprinter_20261017-0236")
    out = run([*SDR, "drift", "overnight-noprinter"])
    assert re.search(r"settled by sweep\s+4 of 180", out)
    assert re.search(r"total change\s+-0\.350 dB", out)
    assert re.search(r"std\s+0\.025 dB", out)


def test_analyze_matches_archived_report(tmp_path):
    name = "survey_custom_5m_fm-check-withnotch_20261019-0024"
    csv = survey_csv(name)
    ref = report_dir(name)
    run([*SDR, "analyze", csv, "--receiver", "v5", "--out", tmp_path])
    for f in ("bins.csv", "peaks.csv"):
        assert filecmp.cmp(tmp_path / name / f, ref / f, shallow=False), f


def test_station_defaults_match_the_original_tables():
    from sdrstation.config import Station
    st = Station.load(ROOT)
    assert st.receiver_gaps() == {"xtr": [(1095.0, 1240.0)], "v5": []}
    assert [f for f, _ in st.spur_families] == [28.8, 0.6, 480.0, 24.0, 25.0, 100.0]
    assert st.retune_ms_per_hop == 47.0
    assert st.detection_floors == {"handled": 0.5, "untouched": 0.05}
    assert list(st.presets) == ["LS", "L", "milair", "900", "wide", "burst23", "deep", "custom"]
    assert st.presets["burst23"]["floor_window"] == 0.3


def test_legacy_station_json_is_read_as_defaults(tmp_path):
    from sdrstation.config import Station
    legacy = {"antenna_shape": "90 L", "whip_length_mm": 60, "lna": "x", "filter": "y",
              "chain_order": "antenna > PC"}
    (tmp_path / "station.json").write_text(json.dumps(legacy), encoding="utf-8")
    st = Station.load(tmp_path)
    assert st.defaults["chain_order"] == "antenna > PC"
    assert st.retune_ms_per_hop == 47.0          # built-in facts fill the rest
    st.save_defaults({"chain_order": "50 ohm load > PC", "whip_length_mm": 46})
    on_disk = json.loads((tmp_path / "station.json").read_text(encoding="utf-8"))
    assert on_disk["defaults"]["chain_order"] == "50 ohm load > PC"
    assert on_disk["defaults"]["antenna_shape"] == "90 L"      # untouched keys survive


def test_user_edits_to_station_json_survive_a_run(tmp_path):
    from sdrstation.config import Station
    (tmp_path / "station.json").write_text(json.dumps(
        {"retune_ms_per_hop": 52.5, "presets": {"mine": {"freq": "100M:110M:10k", "gain": 29.0,
                                                         "integration": 5, "receiver": "xtr",
                                                         "note": "test"}},
         "defaults": {"chain_order": "antenna > PC"}}), encoding="utf-8")
    st = Station.load(tmp_path)
    assert st.retune_ms_per_hop == 52.5 and list(st.presets) == ["mine"]
    st.save_defaults({"chain_order": "load > PC"})
    on_disk = json.loads((tmp_path / "station.json").read_text(encoding="utf-8"))
    assert on_disk["retune_ms_per_hop"] == 52.5 and "mine" in on_disk["presets"]


def test_retune_calibration_parser(tmp_path):
    """A synthetic fast run: 100 hops, one sweep every 4.7 s -> 47.0 ms per hop."""
    from sdrstation.calibrate import retune_from_csv
    import datetime as dt
    lines = []
    t0 = dt.datetime(2026, 10, 19, 12, 0, 0)
    for sweep in range(13):
        ts = t0 + dt.timedelta(seconds=round(sweep * 4.7))
        for hop in range(100):
            lo = 400e6 + hop * 2.1e6
            lines.append(f"{ts:%Y-%m-%d}, {ts:%H:%M:%S}, {lo:.0f}, {lo + 2.1e6:.0f}, "
                         f"100000, 32, -30.0, -30.1")
    p = tmp_path / "fast.csv"
    p.write_text("\n".join(lines) + "\n")
    r = retune_from_csv(p)
    assert r["hops"] == 100 and r["sweeps"] == 12
    assert r["retune_ms_per_hop"] == pytest.approx(1000 * 56 / 12 / 100, abs=0.01)  # 46.7 ms with 1 s stamps


def test_scan_geometry_same_through_package():
    from sdrstation.rtlpower import scan_geometry
    g = scan_geometry(1241.0, 2147.483648, 50.0, 120, 3600, retune_ms_per_hop=47.0)
    assert g["hops"] == 432 and g["bins"] == 20736
    assert g["hop_centres_mhz"][0] == pytest.approx(1242.049171, abs=1e-6)
