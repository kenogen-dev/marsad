"""Hunt mode: the band-difference ranking and the tracking tile.

The ranking is pure arithmetic on two bins.csv files, so it is pinned on a
synthetic pair whose answer is known by construction; the tile is checked
through probe.compose with the station's own catalogue.
"""
import csv
import sys

import pytest

from conftest import REPORTS, ROOT

sys.path.insert(0, str(ROOT))

from marsad import hunt, probe                    # noqa: E402
from marsad.config import Station                 # noqa: E402

FORM = {"target": "PC power supply", "probe": "large-loop", "where": "against the PSU fan grille",
        "minutes": "2", "range": "tile", "centre": "433.92", "device": "0"}


def write_bins(path, rows):
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["freq_mhz", "floor_db", "median_db", "max_db", "occupancy_pct"])
        w.writerows(rows)


def synthetic(tmp_path):
    """Two runs on one 87.47 kHz grid over 400-1000 MHz. Both carry the SDR's clock
    harmonic at 720 MHz and a one-sweep impulse in max_db; only the device run adds a
    12 dB line at 433.92 MHz and a 3 dB broadband rise across 900-910 MHz."""
    step = 0.0874673
    freqs = [400.0 + i * step for i in range(int(600 / step))]
    bg, dev = [], []
    for f in freqs:
        base = -30.0
        spur = 14.0 if abs(f - 720.0) < step / 2 else 0.0
        b_med = base + spur
        d_med = base + spur
        if abs(f - 433.92) < step / 2:
            d_med += 12.0
        if 900 <= f < 910:
            d_med += 3.0
        # max-hold carries a hop-wide click in the device run at 650-652 MHz: it must not rank
        d_max = d_med + (20.0 if 650 <= f < 652 else 1.0)
        bg.append([round(f, 6), base - 0.5, round(b_med, 2), round(b_med + 1.0, 2), 0.0])
        dev.append([round(f, 6), base - 0.5, round(d_med, 2), round(d_max, 2), 0.0])
    a, b = tmp_path / "bg", tmp_path / "dev"
    a.mkdir(); b.mkdir()
    write_bins(a / "bins.csv", bg)
    write_bins(b / "bins.csv", dev)
    return a, b


def test_rank_finds_the_line_and_the_pedestal_and_ignores_what_both_runs_share(tmp_path):
    a, b = synthetic(tmp_path)
    bg, dev = hunt.load_bins(a), hunt.load_bins(b)
    r = hunt.rank(bg, dev, 10.0, 5, [(14.4, "14.4 MHz SDR clock harmonic")])
    c = r["candidates"]
    assert r["blocks"] == 60 and r["step_khz"] == pytest.approx(87.47, abs=1.0)   # keys are whole kHz
    # 1st: the narrow line, found to the bin
    assert c[0]["lo_mhz"] == 430 and c[0]["peak_mhz"] == pytest.approx(433.92, abs=0.05)
    assert c[0]["peak_rise_db"] == pytest.approx(12.0, abs=0.01)
    assert c[0]["floor_rise_db"] == pytest.approx(0.0, abs=0.01)     # a line, not a pedestal
    assert c[0]["device_db"] == pytest.approx(-18.0, abs=0.01) and c[0]["tag"] is None
    # 2nd: the pedestal, where the block median rose with the peak
    assert c[1]["lo_mhz"] == 900 and c[1]["peak_rise_db"] == pytest.approx(3.0, abs=0.01)
    assert c[1]["floor_rise_db"] == pytest.approx(3.0, abs=0.01)
    # everything else cancelled: the 720 MHz harmonic and the max-hold click are not candidates
    assert all(abs(x["peak_rise_db"]) < 0.01 for x in c[2:])
    assert all(x["lo_mhz"] not in (650, 720) for x in c[:2])
    assert all(x["rank"] == i + 1 for i, x in enumerate(c))


def test_rank_tags_a_spur_family_and_skips_the_receiver_gap(tmp_path):
    a, b = synthetic(tmp_path)
    bg, dev = hunt.load_bins(a), hunt.load_bins(b)
    # knock the harmonic out of the background only, so it "rises" in the device run
    k = next(k for k in bg if abs(k - 720000) < 44)
    bg[k] = (bg[k][0], bg[k][1] - 14.0, bg[k][2])
    r = hunt.rank(bg, dev, 10.0, 3, [(14.4, "14.4 MHz SDR clock harmonic")], gaps=[(430.0, 440.0)])
    c = r["candidates"]
    assert c[0]["peak_mhz"] == pytest.approx(720.0, abs=0.05) and c[0]["tag"] == "14.4 MHz SDR clock harmonic (x50)"
    assert all(x["lo_mhz"] != 430 for x in c)          # the gap block is not offered
    assert r["blocks"] == 59


def test_rank_refuses_two_different_grids(tmp_path):
    a, b = synthetic(tmp_path)
    bg = hunt.load_bins(a)
    shifted = {k + 7: v for k, v in hunt.load_bins(b).items()}
    with pytest.raises(ValueError, match="same frequency grid"):
        hunt.rank(bg, shifted)


def test_compose_tile_is_a_100_mhz_deep_tile_at_3_s_tagged_as_a_hunt():
    st = Station.load(ROOT)
    d = probe.compose(st, FORM)
    a = d["args"]
    assert a[:2] == ["deep", "2m"]
    assert d["tag"] == "hunt-pc-power-supply-large-loop"
    lo, hi = d["hunt"]["lo"], d["hunt"]["hi"]
    assert hi - lo == pytest.approx(101.0, abs=1e-6) and lo < 433.92 < hi
    assert a[a.index("--freq") + 1].endswith("M:25k")
    assert a[a.index("--gain") + 1] == "29" and a[a.index("--integration") + 1] == "3"
    assert a[a.index("--floor-window") + 1] == "2"
    g = d["geometry"]
    assert g["hops"] == 49 and g["retune_s_per_sweep"] == pytest.approx(2.3, abs=0.05)
    assert d["hunt"]["clearance_mhz"] >= 0.4           # the target off every DC spike
    assert d["warnings"] == [] and d["range_label"].startswith("tile 433.92 MHz (")
    # the 5 MHz hunt tile is tagged the same way, the wide overview is not
    h = probe.compose(st, {**FORM, "range": "hunt"})
    assert h["tag"].startswith("hunt-") and h["geometry"]["hops"] == 3
    w = probe.compose(st, {**FORM, "range": "wide", "minutes": "5"})
    assert w["tag"].startswith("probe-")


def test_candidates_on_the_station_s_own_background_runs():
    """Two open-air runs differ by the room's drift, so nothing should rise much."""
    st = Station.load(ROOT)
    runs = sorted(p.name for p in REPORTS.glob("survey_wide_10m_probe-background*") if (p / "bins.csv").exists())
    if len(runs) < 2:
        pytest.skip("station data not present: two probe-background reports")
    r = hunt.candidates(st, runs[-2], runs[-1], 10.0, 5)
    assert r["background"]["background"] and r["device"]["background"]
    assert r["fingerprint"].startswith("52.000-1660.000 MHz | 87.4673 kHz | g29 | i60")
    assert len(r["candidates"]) == 5 and r["candidates"][0]["rank"] == 1
    assert all(not (1095 <= c["peak_mhz"] < 1240) for c in r["candidates"])   # the PLL gap is skipped
    with pytest.raises(ValueError, match="same report"):
        hunt.candidates(st, runs[-1], runs[-1])
    names = [i["name"] for i in hunt.report_list(st)]
    assert runs[-1] in names and names == sorted(names, key=lambda n: hunt.report_info(st, n)["started"] or "", reverse=True)
