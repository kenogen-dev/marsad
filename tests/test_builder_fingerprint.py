"""The Builder's fingerprint preview (2026-10-30) is made on the server by marsad's own
fingerprint function, on the sidecar `marsad run` would write. Pinned:

- rtlpower.csv_bin_khz reproduces rtl_power's integer bin width, including the cases the
  float geometry gets wrong in the 4th decimal (LS, wide, the clamped 2148 tops);
- the preview for both receivers and both engines, with "| gainfix" only on a v5
  rtl_power run made with the gainfix build, and no thousands separators;
- on this station, the preview rebuilt from every sidecar that records its build equals
  the fingerprint marsad wrote for that run (skipped where the surveys are not present);
- the HTTP endpoint the page calls.
"""
from __future__ import annotations

import json
import socket
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from marsad import live, rpf
from marsad.config import Station
from marsad.rtlpower import csv_bin_khz, engine_of, fingerprint, parse_freq_spec, scan_geometry

ROOT = Path(__file__).resolve().parent.parent

# (freq spec, bin_khz_actual read from the CSV) for ranges the station has recorded
RECORDED = [("1241M:2148M:50k", 43.7154), ("700M:1700M:50k", 43.6758), ("225M:400M:12.5k", 10.8507),
            ("52M:1660M:100k", 87.4673), ("1930M:1990M:50k", 43.1034), ("2049M:2148M:25k", 21.827),
            ("1850M:2148M:25k", 21.8224), ("1264M:1268M:5k", 2.6042), ("1095M:1240M:25k", 21.5774),
            ("1241M:2187M:50k", 43.7154)]


@pytest.mark.parametrize("spec,actual", RECORDED)
def test_csv_bin_width_is_rtl_powers_own(spec, actual):
    lo, hi, b = parse_freq_spec(spec)
    assert csv_bin_khz(lo, hi, b) == actual


def test_float_geometry_is_what_csv_bin_khz_corrects():
    lo, hi, b = parse_freq_spec("1241M:2148M:50k")
    assert round(scan_geometry(lo, min(hi, 2147.483648), b, 30, 60)["bin_khz_predicted"], 4) == 43.7155


def station(tmp_path: Path, exe: str) -> Station:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / exe).write_bytes(b"not a real program")
    (tmp_path / "station.json").write_text(json.dumps({"rtl_power_exe": str(tmp_path / exe)}))
    return Station.load(tmp_path)


def rp_query(rx, lo, hi, gain, b, integ):
    return {"engine": "rtl_power", "rx": rx, "lo": str(lo), "hi": str(hi), "gain": str(gain),
            "bin": str(b), "integ": str(integ), "crop": "0.25", "fir": "9"}


def test_preview_both_receivers_both_builds(tmp_path):
    fix = station(tmp_path / "a", "rtl_power_gainfix.exe")
    stock = station(tmp_path / "b", "rtl_power.exe")
    q = rp_query("v5", 225, 400, 29.7, 12.5, 30)
    assert live.builder_fingerprint(fix, q) == \
        "225.000-400.000 MHz | 10.8507 kHz | g29.7 | i30 | c0.25 | F9 | v5 | gainfix"
    assert live.builder_fingerprint(stock, q) == \
        "225.000-400.000 MHz | 10.8507 kHz | g29.7 | i30 | c0.25 | F9 | v5"
    # the XTR keeps its old fingerprint under either build; the top clamps to the ceiling
    x = rp_query("xtr", 1241, 2148, 29, 50, 120)
    want = "1241.000-2147.484 MHz | 43.7154 kHz | g29 | i120 | c0.25 | F9 | xtr"
    assert live.builder_fingerprint(fix, x) == live.builder_fingerprint(stock, x) == want


def test_preview_rtl_power_fftw(tmp_path):
    st = station(tmp_path, "rtl_power_gainfix.exe")
    q = {"engine": "rtl_power_fftw", "rx": "xtr", "lo": "2147", "hi": "2187", "gain": "29",
         "bins": "128", "rate": "2400000", "time": "1", "overlap": "25", "crop": "0.25"}
    assert live.builder_fingerprint(st, q) == ("2147.000-2187.000 MHz | 18.7500 kHz | g29 | t1 | N128 | "
                                               "r2.4M | o25 | c0.25 | rtl_power_fftw | xtr")


def recorded_cases():
    out = []
    for f in sorted((ROOT / "surveys").glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if engine_of(d) == "rtl_power_fftw":
            rp = d.get(rpf.ENGINE) or {}
            if not rp.get("rate_hz_actual"):
                continue
            real = fingerprint(d, rpf.actual_bin_width_khz(rp))
            q = {"engine": rpf.ENGINE, "rx": d["receiver"], "lo": rp["freq_low_mhz"], "hi": rp["freq_high_mhz"],
                 "gain": rp["gain_db"], "bins": rp["bins"], "rate": rp["rate_hz"],
                 "time": rp["time_per_hop_s"], "overlap": rp["overlap_pct"], "crop": rp["crop"]}
        else:
            rp = d.get("rtl_power") or {}
            if not (rp.get("bin_khz_actual") and rp.get("build")):
                continue
            real = fingerprint(d, rp["bin_khz_actual"])
            q = {"engine": "rtl_power", "rx": d["receiver"], "lo": rp["freq_low_mhz"], "hi": rp["freq_high_mhz"],
                 "gain": rp["gain_db"], "bin": rp["bin_khz"], "integ": rp["integration_s"],
                 "crop": rp["crop"], "fir": rp["fir"]}
        out.append((f.stem, {k: str(v) for k, v in q.items()}, real))
    return out


def test_preview_equals_every_recorded_fingerprint():
    cases = recorded_cases()
    if not cases:
        pytest.skip("no sidecars with a recorded build on this machine")
    st = Station.load(ROOT)
    wrong = [(n, live.builder_fingerprint(st, q), real) for n, q, real in cases
             if live.builder_fingerprint(st, q) != real]
    assert not wrong, wrong[:3]


def test_endpoint(tmp_path):
    st = station(tmp_path, "rtl_power_gainfix.exe")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    threading.Thread(target=live.main, args=(["--port", str(port)], st), daemon=True).start()
    url = f"http://127.0.0.1:{port}/builder/fingerprint?" + "&".join(
        f"{k}={v}" for k, v in rp_query("v5", 225, 400, 29.7, 12.5, 30).items())
    for _ in range(50):
        try:
            got = json.loads(urllib.request.urlopen(url, timeout=2).read())
            break
        except OSError:
            time.sleep(0.1)
    assert got["fingerprint"].endswith("| v5 | gainfix")
