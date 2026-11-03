"""The live view reads rtl_power_fftw recordings: incrementally, cropped, with refused
hops from the .log, and follows the newest file of either engine. All generated here:
two passes of a 33-hop survey in the program's own output format, a sidecar and logs.
"""
import datetime as dt
import json
import sys
from pathlib import Path

import numpy as np
import pytest

from conftest import ROOT

sys.path.insert(0, str(ROOT))
from marsad import rpf                     # noqa: E402
from marsad.live import DatTailer, Tailer  # noqa: E402

RATE, BINS = 2_400_000, 64
SPAN = (1930.0, 1990.0)
HOPS = [1931.2e6 + i * 1.8e6 for i in range(33)]
REFUSED_HZ = [2173266678, 2175057155, 2176847632, 2178638109,
              2180428586, 2182219063, 2184009540, 2185800017]

SETUP_LOG = f"""Found Elonics E4000 tuner
Scan session duration: 120 seconds
Selected nearest available gain: 290 (29 dB)
Actual sample rate: {RATE} Hz
Number of bins: {BINS}
Number of averaged spectra: 37500
Estimated time of measurements: 1 seconds
"""


def dat_text(seed=11) -> str:
    """Two passes, each hop a block of N `frequency power` lines around -54 dB/Hz."""
    rng = np.random.default_rng(seed)
    out = []
    for p in range(2):
        for i, c in enumerate(HOPS):
            t0 = dt.datetime(2031, 1, 15, 12, 0, 0) + dt.timedelta(seconds=p * 40 + i * 1)
            out += ["# rtl-power-fftw output", f"# Acquisition start: {t0:%Y-%m-%d %H:%M:%S} UTC",
                    f"# Acquisition end: {t0 + dt.timedelta(seconds=1):%Y-%m-%d %H:%M:%S} UTC", "#",
                    "# frequency [Hz] power spectral density [dB/Hz]"]
            lo = c - RATE / 2
            for k in range(BINS):
                out.append(f"{lo + k * RATE / BINS:.10g} {-54 + 0.3 * rng.standard_normal():.4f}")
            out.append("")
    return "\n".join(out) + "\n"


def sidecar(name: str) -> dict:
    return {"schema": 1, "engine": "rtl_power_fftw", "csv": name, "preset": "custom",
            "started_local": "2031-01-15T07:44:15", "duration_requested": "2m", "duration_requested_s": 120,
            "command_line": "rtl_power_fftw -f 1930M:1990M -g 290 -b 64 -r 2400000 -t 1 -o 25 -e 120 -d 0",
            "stop_file": None, "receiver": "e4000", "chain": {"chain_order": "antenna > SDR > PC"},
            "conditions": "generated",
            "rtl_power_fftw": {"freq_spec": "1930M:1990M", "freq_low_mhz": SPAN[0], "freq_high_mhz": SPAN[1],
                               "freq_high_mhz_effective": SPAN[1], "centre_mhz": None, "bins": BINS, "rate_hz": RATE,
                               "rate_hz_actual": RATE, "time_per_hop_s": 1.0, "overlap_pct": 25.0, "crop": 0.25,
                               "gain_db": 29.0, "bin_khz": 37.5, "bin_khz_actual": 37.5, "device": 0,
                               "baseline": None, "window": None},
            "geometry": {"hops": 33, "hop_mhz": 2.4, "hop_spacing_mhz": 1.8, "sample_rate_hz": RATE, "fft": BINS,
                         "bin_khz_predicted": 37.5, "kept_bins_per_hop": 48, "bins": 1584, "repeats_per_hop": 37500,
                         "reads_per_hop": 3, "overhead_per_hop_s": 0.091, "listen_per_hop_s": 1.0,
                         "retune_s_per_sweep": 3.003, "sweep_s": 36.003, "sweeps": 4, "listening_frac": 0.916589,
                         "listen_per_freq_s": 5.0, "occupancy_step_pct": 25.0, "csv_mb": 0.223872,
                         "covered_mhz": [1930.3, 1989.7]}}


def _write_sample(dat: Path, text: str | None = None, ended: bool = True) -> Path:
    dat.write_text(dat_text() if text is None else text, encoding="ascii", newline="\n")
    dat.with_suffix(".json").write_text(json.dumps(sidecar(dat.name)), encoding="utf-8")
    dat.with_suffix(".log").write_text(SETUP_LOG + ("Session duration elapsed.\n" if ended else ""), encoding="utf-8")
    return dat


def top_log() -> str:
    """A run that tunes 22 hops at first, then refuses the top eight one at a time."""
    lines = [SETUP_LOG.replace("37500", "18750")]
    for hz in sorted(REFUSED_HZ, reverse=True):
        lines += [f"[E4K] PLL not locked for {hz} Hz!"] * 3 + [f"Unable to tune to {hz}. Dropping from frequency list."]
    return "\n".join(lines) + "\n"


def _copy_sample(tmp_path: Path) -> Path:
    return _write_sample(tmp_path / "sample.dat")


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
    assert d["meta"]["fingerprint"].endswith("| N64 | r2.4M | o25 | c0.25 | rtl_power_fftw | e4000")
    assert d["meta"]["expected"] == 4
    # UTC in the file, local in the page; one pass apart
    assert len(d["stamps"]) == 2 and d["stamps"][0] != d["stamps"][1]
    assert all(m is not None for m in d["medians"])
    assert d["medians"][0] == pytest.approx(-54.0, abs=0.1)
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

    raw = dat_text().encode("ascii")
    grow = _write_sample(tmp_path / "grow.dat", ended=False)
    log = grow.with_suffix(".log").read_text(encoding="utf-8") + "Session duration elapsed.\n"
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


def test_refused_hops_become_hatched_gaps(tmp_path):
    """A tuner that refuses its top hops one at a time: the log names each, in the
    order they fell, and the tailer turns them into gaps."""
    log = top_log()
    parsed = rpf.parse_log(log)
    assert parsed["dropped_hops"] == sorted(REFUSED_HZ, reverse=True)   # in the order they fell
    assert parsed["rate_hz_actual"] == 2_400_000 and parsed["gain_tenths"] == 290
    assert parsed["repeats"] == 18750 and parsed["dropped_sample_errors"] == 0
    assert log.count("PLL not locked") == 24                               # three tries each
    # the tailer reads the same log beside an (empty) .dat and turns it into hatched gaps
    dat = tmp_path / "survey_top_1h_x_20311022-1845.dat"
    dat.write_text("", encoding="ascii")
    dat.with_suffix(".log").write_text(log, encoding="utf-8")
    sc = {"engine": "rtl_power_fftw", "receiver": "e4000", "started_local": "2031-10-22T18:45:00",
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
    (sv / "survey_LS_1h_a_20311022-0100.csv").write_text("x", encoding="ascii")
    dat = sv / "survey_top_1h_b_20311022-0200.dat"
    _write_sample(dat)
    import os
    os.utime(sv / "survey_LS_1h_a_20311022-0100.csv", (1_000_000, 1_000_000))
    os.utime(dat, (2_000_000, 2_000_000))
    rows = survey_list(st)
    assert [r["name"] for r in rows][0] == dat.name            # newest first, .dat included
    fftw = next(r for r in rows if r["name"] == dat.name)
    assert fftw["engine"] == "rtl_power_fftw" and fftw["tph"] == 1.0 and fftw["integ"] is None
    assert next(r for r in rows if r["name"].endswith(".csv"))["engine"] == "rtl_power"
