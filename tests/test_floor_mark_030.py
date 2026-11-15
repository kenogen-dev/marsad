"""Relay 030 (known issue 11): the setup floor run is marked terminated as soon as its report exists, not only once
it happens to be archived. Made-up station; no radio is opened."""
import csv
import json
import sqlite3
import sys

from conftest import ROOT

sys.path.insert(0, str(ROOT))

from marsad import setup                           # noqa: E402
from marsad.config import Station, builtin_defaults  # noqa: E402

RP = {"freq_low_mhz": 100.0, "freq_high_mhz": 104.0, "bin_khz": 500, "bin_khz_actual": 500.0,
      "gain_db": 29, "integration_s": 10, "crop": 0.25, "fir": 9}


def make_report(st, name, conditions):
    sc = {"started_local": "2031-10-26T08:00:00", "receiver": "e4000", "conditions": conditions,
          "chain": {"chain_order": "made-up radio"}, "rtl_power": RP, "geometry": {"hops": 2}}
    st.surveys.mkdir(exist_ok=True)
    (st.surveys / f"{name}.json").write_text(json.dumps(sc))
    d = st.reports / name
    d.mkdir(parents=True)
    with open(d / "bins.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["freq_mhz", "floor_db", "median_db", "max_db", "occupancy_pct"])
        w.writerow([100.0, -37.5, -37.5, -36.5, 0])
    (d / "peaks.csv").write_text("freq_mhz,snr_db\n")


def flag(st, name):
    con = sqlite3.connect(st.db)
    row = con.execute("SELECT terminated FROM surveys WHERE name=?", (name,)).fetchone()
    con.close()
    return None if row is None else row[0]


def test_an_unarchived_floor_run_is_archived_and_marked_at_once(tmp_path):
    st = Station(tmp_path, builtin_defaults(), False)
    name = "survey_floor_10m_setup_20311026-0800"
    make_report(st, name, "made-up words that do not say it")      # the text guess alone would leave it 0
    assert setup._mark_terminated(st, f"{name}.csv") is True
    assert flag(st, name) == 1


def test_no_report_yet_says_not_marked(tmp_path):
    st = Station(tmp_path, builtin_defaults(), False)
    assert setup._mark_terminated(st, "survey_floor_10m_setup_20311026-0900.csv") is False
