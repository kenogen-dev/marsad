"""Which rtl_power marsad runs, and what a sidecar records about it (2026-10-27).

rtl_power_gainfix is chosen when installed, the stock rtl_power otherwise with a
warning, and station.json's rtl_power_exe overrides both. The fingerprint carries
a `| gainfix` token for R820T/R860 (v5) runs only: the stock build ignored their
gain, so they must not compare with old v5 runs. XTR runs keep their fingerprint.
No radio and no station data needed.
"""
import hashlib
import sys

import pytest

from conftest import ROOT

sys.path.insert(0, str(ROOT))

from marsad import rtlpower  # noqa: E402
from marsad.analyze import compare_sidecars, sidecar_lines  # noqa: E402
from marsad.config import Station  # noqa: E402
from marsad.rtlpower import (build_label, build_token, choose_rtl_power,  # noqa: E402
                             exe_name, exe_record, fingerprint, program_line)
from marsad.stats import fingerprint_loose  # noqa: E402


@pytest.fixture(autouse=True)
def no_path_lookup(monkeypatch):
    # Nothing on this PC's PATH may answer for the fake folders.
    monkeypatch.setattr(rtlpower.shutil, "which", lambda tool: None)


def _install(folder, *tools):
    folder.mkdir(parents=True, exist_ok=True)
    for t in tools:
        (folder / exe_name(t)).write_bytes(f"fake {t}".encode())
    return folder


def _sidecar(receiver, tuner, build=None):
    rp = {"freq_low_mhz": 225.0, "freq_high_mhz": 400.0, "freq_high_mhz_effective": 400.0,
          "bin_khz": 12.5, "gain_db": 40.2, "integration_s": 30, "crop": 0.25, "fir": 9,
          "tuner": tuner}
    if build:
        rp.update({"exe": exe_name(f"rtl_power_{build}" if build == "gainfix" else "rtl_power"),
                   "build": build, "sha256": "ab" * 32})
    return {"rtl_power": rp, "receiver": receiver}


# ---- choosing the executable ------------------------------------------------
def test_gainfix_is_preferred_when_installed(tmp_path):
    d = _install(tmp_path / "rtl-sdr", "rtl_power", "rtl_power_gainfix")
    exe, warning = choose_rtl_power(None, [d])
    assert exe == d / exe_name("rtl_power_gainfix")
    assert warning is None
    assert build_label(exe) == "gainfix"


def test_falls_back_to_stock_with_one_line_warning(tmp_path):
    d = _install(tmp_path / "rtl-sdr", "rtl_power")
    exe, warning = choose_rtl_power(None, [d])
    assert exe == d / exe_name("rtl_power")
    assert build_label(exe) == "original"
    assert "rtl_power_gainfix" in warning and "\n" not in warning


def test_nothing_installed_gives_none(tmp_path):
    assert choose_rtl_power(None, [tmp_path]) == (None, None)


def test_station_json_can_choose_the_stock_build(tmp_path):
    d = _install(tmp_path / "rtl-sdr", "rtl_power", "rtl_power_gainfix")
    for override in ("rtl_power", "rtl_power.exe"):
        exe, warning = choose_rtl_power(override, [d])
        assert exe == d / exe_name("rtl_power") and warning is None
    # a path is taken relative to the station folder
    exe, _ = choose_rtl_power(f"rtl-sdr/{exe_name('rtl_power')}", [], tmp_path)
    assert exe == tmp_path / "rtl-sdr" / exe_name("rtl_power")
    exe, warning = choose_rtl_power("elsewhere/rtl_power.exe", [], tmp_path)
    assert exe is None and "does not exist" in warning


def test_station_reads_rtl_power_exe(tmp_path):
    _install(tmp_path / "rtl-sdr", "rtl_power", "rtl_power_gainfix")
    st = Station(tmp_path, {"rtl_sdr_dir": "rtl-sdr", "rtl_power_exe": "rtl_power"}, True)
    assert st.rtl_power()[0].name == exe_name("rtl_power")
    st.data.pop("rtl_power_exe")
    assert st.rtl_power()[0].name == exe_name("rtl_power_gainfix")


def test_a_running_gainfix_counts_as_holding_the_sdr():
    assert "rtl_power_gainfix" in rtlpower.SDR_PROGRAMS


# ---- what the sidecar records -------------------------------------------------
def test_exe_record_names_build_and_hash(tmp_path):
    d = _install(tmp_path, "rtl_power_gainfix")
    exe = d / exe_name("rtl_power_gainfix")
    rec = exe_record(exe)
    assert rec == {"exe": exe.name, "build": "gainfix",
                   "sha256": hashlib.sha256(b"fake rtl_power_gainfix").hexdigest()}
    assert exe_record(None) == {}


def test_program_line_in_scan_build_and_report():
    sc = _sidecar("v5", "r820t", "gainfix")
    line = program_line(sc["rtl_power"])
    assert "gainfix build" in line and "sha256 abababab" in line
    assert any("| Program |" in row and "gainfix build" in row for row in sidecar_lines(sc))
    # an old sidecar says plainly that it predates the record
    assert "build not recorded" in program_line(_sidecar("v5", None)["rtl_power"])


# ---- the fingerprint rule ---------------------------------------------------------
def test_v5_gainfix_run_carries_the_build_token():
    fp = fingerprint(_sidecar("v5", "r820t", "gainfix"), 10.8507)
    assert fp.endswith("| v5 | gainfix")
    assert fingerprint(_sidecar("v5", "r860", "gainfix"), 10.8507).endswith("| gainfix")


def test_old_and_stock_v5_runs_have_no_token():
    old = fingerprint(_sidecar("v5", None), 10.8507)       # before 2026-10-27
    stock = fingerprint(_sidecar("v5", "r820t", "original"), 10.8507)
    assert old == stock and old.endswith("| v5")
    assert fingerprint(_sidecar("v5", "r820t", "gainfix"), 10.8507) != old


def test_xtr_fingerprint_unchanged_by_the_build():
    old = fingerprint(_sidecar("xtr", None), 10.8507)
    new = fingerprint(_sidecar("xtr", "e4000", "gainfix"), 10.8507)
    assert new == old == ("225.000-400.000 MHz | 10.8507 kHz | g40.2 | i30 | c0.25 | F9 | xtr")
    assert build_token(_sidecar("xtr", "e4000", "gainfix")) == ""


def test_loose_fingerprint_and_compare_follow_the_same_rule():
    new, old = _sidecar("v5", "r820t", "gainfix"), _sidecar("v5", None)
    assert fingerprint_loose(new) != fingerprint_loose(old)
    problems = "\n".join(compare_sidecars("a", old, "b", new, 10.85, 10.85))
    assert "rtl_power build differs" in problems
    xa, xb = _sidecar("xtr", None), _sidecar("xtr", "e4000", "gainfix")
    assert "build differs" not in "\n".join(compare_sidecars("a", xa, "b", xb, 10.85, 10.85))
