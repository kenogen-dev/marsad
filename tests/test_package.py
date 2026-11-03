"""The packaged `marsad` command: station.json loading and migration, the retune
calibration parser and the command-line front end.
"""
import filecmp
import json
import re
import sys

import pytest

from conftest import PY, ROOT, run

SDR = [PY, "-m", "marsad"]
sys.path.insert(0, str(ROOT))


def test_help_lists_every_command():
    out = run([*SDR, "--help"])
    for cmd in ("run", "analyze", "compare-runs", "spur-stats", "drift", "archive", "live",
                "calibrate-retune", "init", "config"):
        assert re.search(rf"^\s+{re.escape(cmd)}\s", out, re.M), cmd


def test_dry_run_prints_the_ls_geometry():
    out = run([*SDR, "run", "LS", "1h", "--freq", "1241M:2148M:50k", "--tag", "pytest", "--dry-run"])
    assert "432 hops x 2.0983 MHz, FFT 64, 43.7155 kHz actual bins, 20,736 logged" in out
    assert "-f 1241M:2148M:50k -g 29 -i 30 -c 0.25 -F 9 -e 1h" in out
    assert "--dry-run: nothing recorded" in out


def test_station_defaults_are_generic():
    from marsad.config import Station
    st = Station.load()
    assert st.receiver_gaps() == {"e4000": [(1105.0, 1268.0)], "r820t": []}
    assert [f for f, _ in st.spur_families] == [28.8, 480.0, 24.0, 25.0, 100.0]
    assert st.default_receiver == "r820t"
    assert st.data["recorder"]["serials"] == {} and st.data["recorder"]["reference_carriers_mhz"] == []
    assert st.retune_ms_per_hop == 47.0
    assert st.detection_floors == {"handled": 0.5, "untouched": 0.05}
    assert list(st.presets) == ["fm", "air", "adsb", "L", "LS", "milair", "900", "wide", "deep", "custom",
                                "top", "h1"]
    assert st.presets["top"]["engine"] == st.presets["h1"]["engine"] == "rtl_power_fftw"


def test_legacy_station_json_is_read_as_defaults(tmp_path):
    from marsad.config import Station
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
    from marsad.config import Station
    (tmp_path / "station.json").write_text(json.dumps(
        {"retune_ms_per_hop": 52.5, "presets": {"mine": {"freq": "100M:110M:10k", "gain": 29.0,
                                                         "integration": 5, "receiver": "e4000",
                                                         "note": "test"}},
         "defaults": {"chain_order": "antenna > PC"}}), encoding="utf-8")
    st = Station.load(tmp_path)
    assert st.retune_ms_per_hop == 52.5 and list(st.presets) == ["mine"]
    st.save_defaults({"chain_order": "load > PC"})
    on_disk = json.loads((tmp_path / "station.json").read_text(encoding="utf-8"))
    assert on_disk["retune_ms_per_hop"] == 52.5 and "mine" in on_disk["presets"]


def test_retune_calibration_parser(tmp_path):
    """A synthetic fast run: 100 hops, one sweep every 4.7 s -> 47.0 ms per hop."""
    from marsad.calibrate import retune_from_csv
    import datetime as dt
    lines = []
    t0 = dt.datetime(2031, 10, 19, 12, 0, 0)
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
    from marsad.rtlpower import scan_geometry
    g = scan_geometry(1241.0, 2147.483648, 50.0, 120, 3600, retune_ms_per_hop=47.0)
    assert g["hops"] == 432 and g["bins"] == 20736
    assert g["hop_centres_mhz"][0] == pytest.approx(1242.049171, abs=1e-6)
