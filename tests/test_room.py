"""The room equipment catalogue: a device's states against its unplugged run.

A synthetic station in a temporary folder holds one printer surveyed in four
states with the probe in one place, plus a monitor surveyed only while active.
The answer is known by construction: printing adds a 15 dB line at 120.3 MHz
and a 7 dB line at 433.92 MHz, idle adds 3 dB at 120.3, off adds nothing.
"""
import csv
import json
import sys

import pytest

from conftest import ROOT

sys.path.insert(0, str(ROOT))

from marsad import probe, room                    # noqa: E402
from marsad.config import Station, builtin_defaults  # noqa: E402

STEP = 0.0874673
FREQS = [52.0 + i * STEP for i in range(int(900 / STEP))]          # 52-952 MHz
RP = {"freq_low_mhz": 52.0, "freq_high_mhz": 1660.0, "bin_khz": 100, "bin_khz_actual": 87.4673,
      "gain_db": 29, "integration_s": 60, "crop": 0.25, "fir": 9}


def add_run(st: Station, stamp: str, target: str, state: str | None, lines=(), gain=29,
            held="against the marked corners on the lid"):
    """A finished probe run: its sidecar (conditions as probe.compose writes them) and bins.csv."""
    st_slug = f"-{probe.slug(state, 20)}" if state else ""
    name = f"survey_wide_5m_probe-{probe.slug(target)}{st_slug}-stub-a_{stamp}"
    cond = (f"near-field probe survey: {target}, E-field stub probe A held {held}, "
            + (f"device state {state}, " if state else "")
            + "wide 52-1660 MHz (g29, i60), 5m, no pad")
    sc = {"started_local": f"2026-10-26T{stamp[-4:-2]}:{stamp[-2:]}:00", "preset": "wide",
          "receiver": "xtr", "conditions": cond, "chain": {"chain_order": "probe > LaNA > XTR"},
          "rtl_power": {**RP, "gain_db": gain}}
    st.surveys.mkdir(exist_ok=True)
    (st.surveys / f"{name}.json").write_text(json.dumps(sc), encoding="utf-8")
    rep = st.reports / name
    rep.mkdir(parents=True)
    with open(rep / "bins.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["freq_mhz", "floor_db", "median_db", "max_db", "occupancy_pct"])
        for f in FREQS:
            m = -35.0 + sum(db for lf, db in lines if abs(f - lf) < STEP / 2)
            w.writerow([round(f, 6), -35.5, round(m, 2), round(m + 1, 2), 0.0])
    return name


@pytest.fixture
def station(tmp_path):
    st = Station(tmp_path, builtin_defaults(), False)
    add_run(st, "20261026-0900", "printer", "unplugged")
    add_run(st, "20261026-0910", "printer", "off")
    add_run(st, "20261026-0920", "printer", "idle", [(120.3, 3.0)])
    add_run(st, "20261026-0930", "printer", "printing", [(120.3, 15.0), (433.92, 7.0)])
    add_run(st, "20261026-0940", "monitor 1", "active", [(300.0, 9.0)])
    add_run(st, "20261026-0800", "background (probe in open air)", None)
    return st


def test_catalogue_groups_runs_by_target_and_state(station):
    t = {x["target"]: x for x in room.targets(station)}
    assert t["printer"]["states"] == {"unplugged": 1, "off": 1, "idle": 1, "printing": 1}
    assert t["background (probe in open air)"]["background"] is True
    assert t["background (probe in open air)"]["states"] == {room.NO_STATE: 1}


def test_compare_differences_each_state_against_unplugged_and_says_so(station):
    d = room.compare(station, "printer")
    assert d["background"]["state"] == "unplugged" and d["background_label"] == "unplugged"
    by = {s["state"]: s for s in d["states"]}
    assert [s["state"] for s in d["states"]] == ["unplugged", "off", "idle", "printing"]
    assert by["unplugged"]["runs"][0]["is_background"]
    p = by["printing"]["runs"][0]
    assert p["strongest"]["peak_mhz"] == pytest.approx(120.3, abs=0.05)
    assert p["strongest"]["peak_rise_db"] == pytest.approx(15.0, abs=0.01)
    assert p["loud_blocks"] == 2 and p["same_position"] is True
    assert by["printing"]["sentence"].startswith("printing adds 15.0 dB at 120.31 MHz")
    assert "over unplugged; 2 blocks rise 6 dB or more" in by["printing"]["sentence"]
    assert by["idle"]["runs"][0]["loud_blocks"] == 0
    assert "adds nothing over unplugged beyond 6 dB" in by["idle"]["sentence"]
    assert "all" not in p                       # the page gets the top rows, not every block


def test_compare_refuses_a_different_fingerprint_in_a_sentence(station):
    add_run(station, "20261026-0950", "printer", "printing", [(120.3, 15.0)], gain=42)
    d = room.compare(station, "printer")
    runs = next(s for s in d["states"] if s["state"] == "printing")["runs"]
    assert "different fingerprint" in runs[0]["refused"] and "g42" in runs[0]["refused"]
    assert "strongest" in runs[1]                 # the older, comparable run still shows


def test_compare_needs_an_unplugged_run_or_a_named_background(station):
    with pytest.raises(ValueError, match="no unplugged run"):
        room.compare(station, "monitor 1")
    bg = next(i["name"] for i in room.hunt.report_list(station) if i["background"])
    d = room.compare(station, "monitor 1", background=bg)
    assert d["background_label"] == "background (probe in open air)"
    assert d["states"][0]["sentence"].startswith("active adds 9.0 dB")
    with pytest.raises(ValueError, match="no probe runs"):
        room.compare(station, "toaster")


def test_loudest_ranks_active_states_and_lists_what_is_missing(station):
    d = room.loudest(station)
    assert [r["target"] for r in d["ranked"]] == ["printer"]
    r = d["ranked"][0]
    assert r["state"] == "printing" and r["peak_rise_db"] == pytest.approx(15.0, abs=0.01)
    assert r["loud_blocks"] == 2 and r["rank"] == 1
    assert d["skipped"] == [{"target": "monitor 1", "reason": "no unplugged run yet"}]
