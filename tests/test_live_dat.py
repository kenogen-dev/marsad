"""The live view reads rtl_power_fftw recordings: incrementally, cropped, with
refused hops from the .log, and follows the newest file of either engine.

Pinned on real captures: tests/data/rpf_pcs_20261022.dat (two passes of the PCS
downlink block, XTR, 2026-10-22 07:44) with a sidecar, and the .log of the
owner's first 2147-2187 MHz run that evening, in which the E4000 refused eight
hops one by one as it warmed.
"""
import json
import shutil
import sys
from pathlib import Path

import pytest

from conftest import ROOT

sys.path.insert(0, str(ROOT))
from marsad import rpf                     # noqa: E402
from marsad.live import DatTailer, Tailer  # noqa: E402

DATA = Path(__file__).resolve().parent / "data"
TOP_REFUSED_HZ = [2173266678, 2175057155, 2176847632, 2178638109,
                  2180428586, 2182219063, 2184009540, 2185800017]


def _copy_sample(tmp_path: Path) -> Path:
    dat = tmp_path / "rpf_pcs_20261022.dat"
    shutil.copy(DATA / "rpf_pcs_20261022.dat", dat)
    shutil.copy(DATA / "rpf_pcs_20261022.json", dat.with_suffix(".json"))
    shutil.copy(DATA / "rpf_pcs_20261022.log", dat.with_suffix(".log"))
    return dat


def test_dat_tailer_reads_two_passes_cropped(tmp_path):
    dat = _copy_sample(tmp_path)
    t = DatTailer(dat, 1200, 400)
    t.poll()
    d = t.payload(0)
    assert d["engine"] == "rtl_power_fftw"
    assert d["total"] == 2                          # two passes, closed by the frequency wrap
    assert d["hops"] == 33 and d["tuned_hops"] == 33 and d["planned_hops"] == 33
    assert d["refused_mhz"] == []
    assert d["step_khz"] == pytest.approx(37.5)
    # the sidecar's covered range (crop 0.25 applied) and the engine fingerprint
    assert (d["lo"], d["hi"]) == (1930.3, 1989.7)
    assert d["meta"]["fingerprint"].endswith("| N64 | r2.4M | o25 | c0.25 | rtl_power_fftw | xtr")
    assert d["meta"]["expected"] == 4
    # UTC in the file (05:44 UTC), local in the page; one pass apart
    assert len(d["stamps"]) == 2 and d["stamps"][0] != d["stamps"][1]
    assert all(m is not None for m in d["medians"])
    assert d["medians"][0] == pytest.approx(-53.93, abs=0.3)
    assert d["partial"] is None and d["cursor_est"] is False
    assert d["nonfinite_pct"] == 0.0 and d["parsed_rows"] == 66
    # the cropped grid has 48 of 64 bins per hop: the first kept bin sits 24 bins above the centre
    assert len(d["hop_centres_mhz"]) == 33
    assert d["hop_centres_mhz"][0] == pytest.approx(1931.2)


def test_dat_tailer_incremental_equals_one_read(tmp_path):
    """Feed the file in two unequal pieces (the second cut lands mid-line) and
    compare with one read: same passes, medians, stamps and display rows."""
    dat = _copy_sample(tmp_path)
    whole = DatTailer(dat, 1200, 400)
    whole.poll()
    ref = whole.payload(0)

    raw = (DATA / "rpf_pcs_20261022.dat").read_bytes()
    grow = tmp_path / "grow.dat"
    shutil.copy(DATA / "rpf_pcs_20261022.json", grow.with_suffix(".json"))
    # the program's "Session duration elapsed." is its last line, after the last block
    log = (DATA / "rpf_pcs_20261022.log").read_text(encoding="utf-8")
    grow.with_suffix(".log").write_text(log.replace("Session duration elapsed.\n", ""), encoding="utf-8")
    cut = len(raw) * 2 // 3 + 7
    grow.write_bytes(raw[:cut])
    t = DatTailer(grow, 1200, 400)
    t.poll()
    mid = t.payload(0)
    assert mid["total"] == 1 and mid["partial"] is not None       # pass 2 in progress
    assert mid["cursor"] is not None and 1930 < mid["cursor"] < 1990
    grow.write_bytes(raw)
    grow.with_suffix(".log").write_text(log, encoding="utf-8")
    t.poll()
    out = t.payload(0)
    assert out["total"] == ref["total"] == 2
    assert out["medians"] == ref["medians"]
    assert out["stamps"] == ref["stamps"]
    assert out["rows"] == ref["rows"]
    assert out["hop_centres_mhz"] == ref["hop_centres_mhz"]


def test_refused_hops_from_the_owners_first_top_run(tmp_path):
    """tests/data/rpf_top_20261022.log: `marsad run top 1h` on 2026-10-22 18:45. The
    E4000 locked all 22 hops at first and then refused the top eight one at a time
    as it warmed; 2171.48 MHz, refused that morning, held all evening."""
    log = (DATA / "rpf_top_20261022.log").read_text(errors="replace")
    parsed = rpf.parse_log(log)
    assert parsed["dropped_hops"] == sorted(TOP_REFUSED_HZ, reverse=True)   # in the order they fell
    assert parsed["rate_hz_actual"] == 2_400_000 and parsed["gain_tenths"] == 290
    assert parsed["repeats"] == 18750 and parsed["dropped_sample_errors"] == 0
    assert log.count("PLL not locked") == 24                               # three tries each
    # the tailer reads the same log beside an (empty) .dat and turns it into hatched gaps
    dat = tmp_path / "survey_top_1h_x_20261022-1845.dat"
    dat.write_text("", encoding="ascii")
    shutil.copy(DATA / "rpf_top_20261022.log", dat.with_suffix(".log"))
    sc = {"engine": "rtl_power_fftw", "receiver": "xtr", "started_local": "2026-10-22T18:45:00",
          "duration_requested_s": 3600,
          "rtl_power_fftw": {"freq_low_mhz": 2147.0, "freq_high_mhz": 2187.0, "bins": 128,
                             "rate_hz": 2_400_000, "time_per_hop_s": 1.0, "overlap_pct": 25.0,
                             "crop": 0.25, "gain_db": 29.0, "bin_khz": 18.75},
          "geometry": {"hops": 22, "sweeps": 150, "sweep_s": 24.0, "covered_mhz": [2147.3, 2186.7]}}
    dat.with_suffix(".json").write_text(json.dumps(sc), encoding="utf-8")
    t = DatTailer(dat, 1200, 400)
    t.poll()
    d = t.payload(0)
    assert d["total"] == 0 and d["planned_hops"] == 22 and d["tuned_hops"] == 0
    assert len(d["refused_mhz"]) == 8
    lo, hi = d["refused_mhz"][0]
    assert lo == pytest.approx(2173.266678 - 0.9, abs=1e-3) and hi == pytest.approx(2173.266678 + 0.9, abs=1e-3)
    assert d["meta"]["ended"] is False and d["can_stop"] is False     # no stop file in this sidecar


def test_csv_tailer_is_untouched():
    """The rtl_power path keeps its class and payload keys; only a .dat gets the new reader."""
    assert not issubclass(Tailer, DatTailer) and issubclass(DatTailer, Tailer)
    assert "refused_mhz" not in Tailer.payload.__code__.co_consts


def test_newest_recording_follows_either_engine(tmp_path):
    """marsad live follows the newest of surveys/*.csv and surveys/*.dat."""
    from marsad.live import survey_list
    from marsad.config import Station
    st = Station(tmp_path, {"presets": {}}, False)
    sv = tmp_path / "surveys"; sv.mkdir()
    (sv / "survey_LS_1h_a_20261022-0100.csv").write_text("x", encoding="ascii")
    dat = sv / "survey_top_1h_b_20261022-0200.dat"
    shutil.copy(DATA / "rpf_pcs_20261022.dat", dat)
    shutil.copy(DATA / "rpf_pcs_20261022.json", dat.with_suffix(".json"))
    import os
    os.utime(sv / "survey_LS_1h_a_20261022-0100.csv", (1_000_000, 1_000_000))
    os.utime(dat, (2_000_000, 2_000_000))
    rows = survey_list(st)
    assert [r["name"] for r in rows][0] == dat.name            # newest first, .dat included
    fftw = next(r for r in rows if r["name"] == dat.name)
    assert fftw["engine"] == "rtl_power_fftw" and fftw["tph"] == 1.0 and fftw["integ"] is None
    assert next(r for r in rows if r["name"].endswith(".csv"))["engine"] == "rtl_power"
