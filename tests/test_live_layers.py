"""The Scan tab's waterfall: sweep start times and the internal layer's floor.

A sweep is labelled with the time its first row was written, whichever way it is
closed (by the hop count from the sidecar, or by the frequency wrapping round).
The internal layer's floor is the newest terminated run with the same
fingerprint, put on the display grid; with none it says so.
"""
import csv
import json
import sys

from conftest import ROOT

sys.path.insert(0, str(ROOT))

from marsad import live                            # noqa: E402
from marsad.config import Station, builtin_defaults  # noqa: E402

RP = {"freq_low_mhz": 100.0, "freq_high_mhz": 104.0, "bin_khz": 500, "bin_khz_actual": 500.0,
      "gain_db": 29, "integration_s": 10, "crop": 0.25, "fir": 9}


def write_csv(path, sweeps):
    """Two hops per sweep, 100-102 and 102-104 MHz, four 500 kHz bins each; each
    sweep's rows are written one second apart, as rtl_power's hops are."""
    with open(path, "w", newline="") as fh:
        for t, level in sweeps:
            for k, lo in enumerate((100e6, 102e6)):
                fh.write(f"2026-10-26, 08:{t:02d}:0{k}, {lo:.0f}, {lo + 2e6:.0f}, 500000.00, 100, "
                         + ", ".join([f"{level:.2f}"] * 4) + "\n")


def test_each_sweep_is_stamped_with_its_own_start(tmp_path):
    p = tmp_path / "survey_x_1m_t_20261026-0800.csv"
    write_csv(p, [(10, -30), (11, -31), (12, -32)])
    # no sidecar: the first sweep closes on the frequency wrap, when the NEXT sweep's first
    # row arrives (its stamp used to be taken from that row); later ones on the learned count
    t = live.Tailer(p, 40, 50)
    t.poll()
    assert t.stamps == ["2026-10-26 08:10:00", "2026-10-26 08:11:00", "2026-10-26 08:12:00"]
    # with the hop count known, they close on their last row: same answer
    p.with_suffix(".json").write_text(json.dumps({"geometry": {"hops": 2}, "rtl_power": RP}))
    t2 = live.Tailer(p, 40, 50)
    t2.poll()
    assert t2.stamps == ["2026-10-26 08:10:00", "2026-10-26 08:11:00", "2026-10-26 08:12:00"]


def add_report(st, name, conditions, level):
    sc = {"started_local": "2026-10-26T" + name[-4:-2] + ":" + name[-2:] + ":00", "receiver": "xtr",
          "conditions": conditions, "chain": {"chain_order": conditions}, "rtl_power": RP,
          "geometry": {"hops": 2}}
    st.surveys.mkdir(exist_ok=True)
    (st.surveys / f"{name}.json").write_text(json.dumps(sc))
    d = st.reports / name
    d.mkdir(parents=True)
    with open(d / "bins.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["freq_mhz", "floor_db", "median_db", "max_db", "occupancy_pct"])
        for i in range(8):
            w.writerow([100.0 + 0.5 * i, level, level, level + 1, 0])


def test_internal_floor_finds_the_terminated_twin_or_says_why(tmp_path):
    st = Station(tmp_path, builtin_defaults(), False)
    name = "survey_x_1m_antenna_20261026-0900"
    add_report(st, name, "antenna on the notch", -30.0)
    write_csv(st.surveys / f"{name}.csv", [(0, -30), (1, -30)])
    none = live.internal_floor(st, f"{name}.csv", 100.0, 104.0, 8)
    assert none["found"] is False and "no terminated run" in none["reason"]
    assert none["families_mhz"] == [14.4]
    # an antenna run with the same fingerprint is not a floor; a terminated one is
    add_report(st, "survey_x_1m_other_20261026-0700", "antenna on the window", -31.0)
    add_report(st, "survey_x_1m_term_20261026-0800", "50 ohm load at the LaNA input", -37.5)
    got = live.internal_floor(st, f"{name}.csv", 100.0, 104.0, 8)
    assert got["found"] is True and got["name"] == "survey_x_1m_term_20261026-0800"
    assert len(got["floor"]) == 8 and set(v for v in got["floor"] if v is not None) == {-37.5}
