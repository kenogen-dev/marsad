"""Starting scans from the window (relay 010): the service's scan setup against the old Scan
Builder, the checks before a start, the launch outside every process tree, saved scans and
recorder sessions. Made-up station only; no radio is ever opened (detection, the process list
and the launch are replaced, except in the one launch test, which starts a harmless stand-in)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from marsad import live
from marsad.config import Station

DATA = Path(__file__).parent / "data"
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def station(tmp_path):
    root = tmp_path / "st"
    root.mkdir()
    shutil.copy(DATA / "builder_station.json", root / "station.json")
    (root / "surveys").mkdir()
    (root / "reports").mkdir()
    return Station.load(str(root))


def fixtures():
    return json.loads((DATA / "builder_fixtures.json").read_text(encoding="utf-8"))


def chain_of(mods):
    return " > ".join(m["name"] for m in mods if m["on"])


def test_config_is_what_the_fixtures_were_made_from(station):
    """The Node fixtures ran on this config; if builder_config changes, remake them."""
    cfg = live.builder_config(station)
    cfg.pop("station_root")
    assert cfg == json.loads((DATA / "builder_config.json").read_text())


@pytest.mark.parametrize("case", fixtures(), ids=lambda c: c["name"])
def test_command_matches_the_builder(station, case):
    """Done-when 1: the same settings give the same complete command, --chain and --conditions
    included, as the old Builder printed."""
    s = live.scan_settings(station, case["scan"])
    g = live.scan_geometry_for(station, s)
    ch = chain_of(case["modules"])
    assert ch == case["chain"]
    assert live.conditions_text(s) == case["conditions"]
    assert live.scan_command(station, g, s, ch)["text"] == case["command"]


@pytest.mark.parametrize("case", fixtures(), ids=lambda c: c["name"])
def test_warnings_match_the_builder(station, case):
    """Done-when 2: every Builder warning appears for its case, on the engine's own geometry."""
    s = live.scan_settings(station, case["scan"])
    g = live.scan_geometry_for(station, s)
    assert live.scan_warnings(station, g, s, chain_of(case["modules"])) == case["flags"]


@pytest.mark.parametrize("case", fixtures(), ids=lambda c: c["name"])
def test_span_facts_match_the_builder(station, case):
    """The Builder's live facts for the span: the receiver's range and the known lines in it."""
    s = live.scan_settings(station, case["scan"])
    assert live.scan_facts(station, s) == case["facts"]


def test_every_builder_warning_is_covered():
    """Each warning title the Builder can give appears in at least one fixture case."""
    titles = {f[1] for c in fixtures() for f in c["flags"]}
    for t in ("Range", "Tuner", "Ceiling", "Bins", "Overlap", "Dwell", "PLL gap", "FFT", "Sweeps", "Warm-up",
              "Convention", "Hop centre", "Floor window", "Enclosure", "Chain", "Device", "Terminated", "Tag",
              "Made-up note", "Gain note", "Pad note"):
        assert t in titles, t


def test_geometry_is_the_engines(station):
    """The figures come from the engine (scan_geometry / rpf.geometry), and agree with the
    page's own arithmetic on every case."""
    for c in fixtures():
        s = live.scan_settings(station, c["scan"])
        g = live.scan_geometry_for(station, s)
        assert g["hops"] == c["geometry"]["hops"], c["name"]
        assert g["bins"] == c["geometry"]["bins"], c["name"]
        assert g["sweeps"] == c["geometry"]["sweeps"], c["name"]
        assert abs(g["listening"] - c["geometry"]["listening"]) < 1e-9, c["name"]


def test_fingerprint_is_the_engines_and_new_set_is_said(station):
    """Done-when 3: the live fingerprint is builder_fingerprint's (the engine's own function on
    the sidecar marsad run would write); a locked change makes new_set true, a free one not."""
    base = {"preset": "band1"}
    same = live.scan_plan(station, {"scan": {"preset": "band1", "tag": "x", "durVal": 3}, "base": base,
                                    "chain": "a > b"})
    assert same["new_set"] is False and same["fingerprint"] == same["base_fingerprint"]
    q = {"engine": "rtl_power", "rx": "radio-a", "lo": 300, "hi": 420, "gain": 29, "bin": 25, "integ": 30,
         "crop": 0.25, "fir": 9}
    assert same["fingerprint"] == live.builder_fingerprint(station, q)
    for change in ({"gain": 34}, {"integ": 60}, {"bin": 12.5}, {"lo": 301}, {"crop": 0.3}, {"fir": 4}):
        got = live.scan_plan(station, {"scan": {"preset": "band1", **change}, "base": base, "chain": "a > b"})
        assert got["new_set"] is True, change


def test_plan_carries_cells_tradeoff_and_listening_flag(station):
    p = live.scan_plan(station, {"scan": {"preset": "band1"}, "chain": "a > b"})
    labels = [c[0] for c in p["cells"]]
    assert labels[:4] == ["Hops", "Sample rate", "FFT size", "Actual bin"] and "Listening" in labels
    assert any(r["now"] for r in p["tradeoff"]) and p["listening_under_review"] is True
    f = live.scan_plan(station, {"scan": {"preset": "upper"}, "chain": "a > b"})
    assert f["engine"] == "rtl_power_fftw" and f["listening_under_review"] is False
    assert [r["label"] for r in f["tradeoff"]][0] == "-t 0.05"


# ---------------------------------------------------------------- checks before a start

def no_radios(monkeypatch, busy=(), found=None):
    monkeypatch.setattr(live.host, "running_sdr_programs", lambda: list(busy))
    found = found if found is not None else [
        {"device": 0, "product": "made-up", "serial": "00000011", "tuner": "Elonics E4000"},
        {"device": 1, "product": "made-up", "serial": "00000022", "tuner": "Rafael Micro R820T"}]
    monkeypatch.setattr(live, "detect_receivers", lambda st: {"found": found, "error": None})


def test_radio_in_use_blocks_and_names_the_program(station, monkeypatch):
    """Done-when 4: a radio "in use" blocks the start and names the program."""
    no_radios(monkeypatch, busy=["sdrpp.exe"])
    chk = live.scan_checks(station, "radio-a")
    assert chk["in_use"]["blocked"] and "sdrpp.exe" in chk["in_use"]["text"]
    with pytest.raises(ValueError, match="sdrpp.exe"):
        live.scan_start(station, {"scan": {"preset": "band1"}})


def test_radio_found_by_serial(station, monkeypatch):
    no_radios(monkeypatch, found=[{"device": 0, "product": "made-up", "serial": "00000022", "tuner": "R820T"},
                                  {"device": 1, "product": "made-up", "serial": "00000011", "tuner": "E4000"}])
    r = live.find_radio(station, "radio-a")
    assert r["found"] and r["device"] == 1 and r["serial"] == "00000011"
    no_radios(monkeypatch, found=[{"device": 0, "product": "made-up", "serial": "00000022", "tuner": "R820T"}])
    r = live.find_radio(station, "radio-a")
    assert not r["found"] and "00000011" in r["text"]


def test_stale_chain_asks_still_built_as_drawn(station, monkeypatch):
    """Done-when 5: a chain already used by a run, not marked static, is due the question;
    a real start refuses until it is confirmed."""
    no_radios(monkeypatch)
    d = json.loads((station.root / "station.json").read_text())
    d["defaults"]["chain_used_at"] = "2001-01-01T00:00:00"
    (station.root / "station.json").write_text(json.dumps(d))
    st = Station.load(str(station.root))
    c = live.chain_for(st, "radio-a")
    assert c["due"] and c["text"] == "made-up antenna > Coax > radio-a > USB > PC"
    launched = []
    monkeypatch.setattr(live, "launch_outside", lambda st_, argv, label: launched.append(argv) or {"pid": 1, "log": "x"})
    with pytest.raises(ValueError, match="still built as drawn"):
        live.scan_start(st, {"scan": {"preset": "band1"}})
    assert not launched
    out = live.scan_start(st, {"scan": {"preset": "band1"}, "chain_confirmed": True})
    assert out["started"] and launched and "--chain" in launched[0]


def test_bad_warning_needs_confirming(station, monkeypatch):
    no_radios(monkeypatch)
    monkeypatch.setattr(live, "launch_outside", lambda st_, argv, label: {"pid": 1, "log": "x"})
    with pytest.raises(ValueError, match="Dwell"):
        live.scan_start(station, {"scan": {"preset": "wideish", "integ": 10}})
    assert live.scan_start(station, {"scan": {"preset": "wideish", "integ": 10}, "problems_confirmed": True})["started"]


def test_start_launches_the_complete_command(station, monkeypatch):
    """The launched argv is `marsad --station ROOT run ...` with the device found by serial,
    the radio's chain, the conditions and --non-interactive; the conditions are remembered."""
    no_radios(monkeypatch, found=[{"device": 2, "product": "made-up", "serial": "00000011", "tuner": "E4000"}])
    got = []
    monkeypatch.setattr(live, "launch_outside", lambda st_, argv, label: got.append(argv) or {"pid": 7, "log": "l"})
    out = live.scan_start(station, {"scan": {"preset": "band1", "tag": "t", "temp": "20", "sky": "clear"}})
    argv = got[0]
    i = argv.index("run")
    assert argv[:i] == live.marsad_argv(station, [])
    assert argv[i:] == ["run", "band1", "1h", "--device", "2", "--tag", "t", "--chain",
                        "made-up antenna > Coax > radio-a > USB > PC", "--conditions", "clear, 20C inside",
                        "--skip-warmup", "16", "--non-interactive"]
    assert out["line"] == ('marsad run band1 1h --device 2 --tag t --chain "made-up antenna > Coax > radio-a > USB > PC"'
                           ' --conditions "clear, 20C inside" --skip-warmup 16 --non-interactive')
    assert live.saved_doc(station)["conditions_last"]["sky"] == "clear"


def test_dry_run_is_the_engines_own(station, monkeypatch):
    """Done-when 1 again, end to end: Start (dry run) runs `marsad run ... --dry-run` with the
    composed command; the engine parses it for real and prints what it would record."""
    no_radios(monkeypatch)
    out = live.scan_start(station, {"scan": {"preset": "band1", "tag": "dry-tag"}, "dry_run": True, "detect": True})
    assert out["exit_code"] == 0, out["output"]
    assert "--dry-run: nothing recorded." in out["output"]
    assert "survey_band1_1h_dry-tag_" in out["output"]
    assert "(--chain) made-up antenna > Coax > radio-a > USB > PC" in out["output"]
    assert out["argv"][out["argv"].index("--device") + 1] == "0"
    assert not list((station.root / "surveys").glob("*.json"))      # a dry run writes no sidecar


# ---------------------------------------------------------------- saved scans

def test_saved_scans_save_rename_duplicate_archive_restore(station):
    doc = live.saved_change(station, {"kind": "scan", "action": "save", "name": "Mine",
                                      "settings": {"preset": "band1", "tag": "x", "gain": 34}})
    mine = next(e for e in doc["scans"] if e["name"] == "Mine")
    assert mine["settings"]["gain"] == 34 and mine["range"] == "300–420 MHz" and mine["rx"] == "radio-a"
    live.saved_change(station, {"kind": "scan", "action": "rename", "name": "Mine", "new_name": "Mine 2"})
    live.saved_change(station, {"kind": "scan", "action": "duplicate", "name": "Mine 2", "new_name": "Copy"})
    doc = live.saved_change(station, {"kind": "scan", "action": "archive", "name": "Copy"})
    names = {e["name"]: e["archived"] for e in doc["scans"]}
    assert names == {"Mine 2": False, "Copy": True}
    doc = live.saved_change(station, {"kind": "scan", "action": "restore", "name": "Copy"})
    assert all(not e["archived"] for e in doc["scans"])
    raw = json.loads((station.root / live.SAVED_SCANS_FILE).read_text())
    assert set(raw["scans"]) == {"Mine 2", "Copy"}            # nothing deleted, ever
    with pytest.raises(ValueError):
        live.saved_change(station, {"kind": "scan", "action": "delete", "name": "Copy"})


def test_last_ran_is_the_newest_run_with_the_same_fingerprint(station):
    for when, gain in (("2001-01-01T10:00:00", 29), ("2001-01-02T10:00:00", 29), ("2001-01-03T10:00:00", 34)):
        side = {"receiver": "radio-a", "started_local": when, "rtl_power": {
            "freq_low_mhz": 300.0, "freq_high_mhz": 420.0, "freq_high_mhz_effective": 420.0, "bin_khz": 25.0,
            "gain_db": gain, "integration_s": 30, "crop": 0.25, "fir": 9, "tuner": "e4000", "build": None}}
        (station.surveys / f"survey_band1_{when[:10]}.json").write_text(json.dumps(side))
    e = next(x for x in live.saved_list(station)["presets"] if x["name"] == "band1")
    assert e["last_ran"] == "2001-01-02T10:00:00" and e["last_survey"] == "survey_band1_2001-01-02"


# ---------------------------------------------------------------- recorder sessions

def test_watch_survey_command_has_both_radios_chains(station, monkeypatch):
    """Done-when 8: survey mode, the surveyor's path as --chain, the recorder's as
    --recorder-chain, keep level and trigger flags, no --device (watch finds both by serial)."""
    no_radios(monkeypatch)
    monkeypatch.setattr(live, "chain_for", lambda st, rx: {"text": f"made-up antenna > splitter > {rx} > PC",
                                                            "due": False, "ok": True})
    p = live.watch_plan(station, {"watch": {"mode": "survey", "scan": {"preset": "band1", "tag": "w"},
                                            "keep_min_snr": 14, "capture": 30, "recorder_gain": 29.7}})
    assert p["argv"] == ["watch", "survey", "band1", "1h", "--tag", "w", "--chain",
                         "made-up antenna > splitter > radio-a > PC", "--skip-warmup", "16",
                         "--recorder-gain", "29.7", "--recorder-chain", "made-up antenna > splitter > radio-b > PC",
                         "--capture", "30", "--keep-min-snr", "14"]
    assert "--device" not in p["argv"] and "--non-interactive" not in p["argv"]
    assert set(p["checks"]["radios"]) == {"radio-a", "radio-b"}


def test_watch_fixed_command(station, monkeypatch):
    no_radios(monkeypatch)
    monkeypatch.setattr(live, "chain_for", lambda st, rx: {"text": f"made-up antenna > {rx} > PC", "due": False, "ok": True})
    p = live.watch_plan(station, {"watch": {"mode": "fixed", "receiver": "radio-b", "freq": "433.92M", "durVal": 30,
                                            "unit": "m", "width": "50k", "gain": 29.7, "threshold": 12}})
    assert p["argv"] == ["watch", "fixed", "--freq", "433.92M", "--receiver", "radio-b", "--duration", "30m",
                         "--width", "50k", "--gain", "29.7", "--chain", "made-up antenna > radio-b > PC",
                         "--threshold", "12"]
    assert p["line"].startswith('marsad watch fixed --freq 433.92M') and '--chain "made-up antenna > radio-b > PC"' in p["line"]


def test_watch_dry_runs_are_the_engines(station, monkeypatch):
    """Both modes through `marsad watch ... --dry-run`, which opens no radio."""
    no_radios(monkeypatch)
    sv = live.watch_start(station, {"watch": {"mode": "survey", "scan": {"preset": "band1"}}, "dry_run": True})
    assert sv["exit_code"] == 0, sv["output"]
    assert "record on the radio-b" in sv["output"] and "recorder chain:" in sv["output"]
    fx = live.watch_start(station, {"watch": {"mode": "fixed", "receiver": "radio-b", "freq": "433.92M", "durVal": 5,
                                              "unit": "m"}, "dry_run": True})
    assert fx["exit_code"] == 0, fx["output"]
    assert "(dry run: no radio opened)" in fx["output"]


def test_watch_in_use_blocks(station, monkeypatch):
    no_radios(monkeypatch, busy=["rtl_sdr.exe"])
    with pytest.raises(ValueError, match="rtl_sdr.exe"):
        live.watch_start(station, {"watch": {"mode": "fixed", "receiver": "radio-b", "freq": "433.92M"}})


def test_no_cap_or_tier_field_reaches_the_command(station, monkeypatch):
    """Rule 2: the private-band table and storage caps cannot be set from here; unknown keys
    in a session are ignored, not passed on."""
    no_radios(monkeypatch)
    monkeypatch.setattr(live, "chain_for", lambda st, rx: {"text": "a > b", "due": False, "ok": True})
    p = live.watch_plan(station, {"watch": {"mode": "fixed", "receiver": "radio-b", "freq": "433.92M",
                                            "daily_gb": 999, "pool_gb": 999, "band_tiers": []}})
    assert not any(x in " ".join(p["argv"]) for x in ("daily", "pool", "tier"))


# ---------------------------------------------------------------- launching outside the trees

STANDIN = """import os, sys, time
p = sys.argv[1]
for _ in range(600):
    open(p, "a").write(f"{os.getpid()} {time.time():.1f}\\n")
    time.sleep(0.5)
"""


def _pid_alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    code = ctypes.c_ulong()
    ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(h)
    return code.value == 259                                              # STILL_ACTIVE


def _parent_of(pid: int) -> int | None:
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class PE(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("pid", wintypes.DWORD),
                    ("heap", ctypes.c_void_p), ("mod", wintypes.DWORD), ("threads", wintypes.DWORD),
                    ("ppid", wintypes.DWORD), ("pri", ctypes.c_long), ("flags", wintypes.DWORD),
                    ("exe", ctypes.c_wchar * 260)]
    k = ctypes.windll.kernel32
    k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = k.CreateToolhelp32Snapshot(0x2, 0)
    e = PE()
    e.dwSize = ctypes.sizeof(e)
    ok = k.Process32FirstW(snap, ctypes.byref(e))
    try:
        while ok:
            if e.pid == pid:
                return e.ppid
            ok = k.Process32NextW(snap, ctypes.byref(e))
    finally:
        k.CloseHandle(snap)
    return None


SERVICE = """import sys, json
from pathlib import Path
import marsad.live as L
standin, beat = sys.argv[3], sys.argv[4]
L.host.running_sdr_programs = lambda: []
L.detect_receivers = lambda st: {"found": [{"device": 0, "product": "made-up", "serial": "00000011",
                                            "tuner": "E4000"}], "error": None}
L.marsad_argv = lambda st, args: [sys.executable, standin, beat]
L.main(["--port", sys.argv[2], "--exit-after-idle", "5"], L.Station.load(sys.argv[1]), "test")
"""


@pytest.mark.skipif(os.name != "nt", reason="the WMI launch is Windows-only")
def test_launched_run_outlives_the_service(station, tmp_path):
    """Done-when 6: the service launches a harmless stand-in through its real start path; the
    stand-in's parent is not the service, and killing the service leaves it running."""
    standin = tmp_path / "standin.py"
    standin.write_text(STANDIN)
    beat = tmp_path / "beat.txt"
    port = 8747
    svc = subprocess.Popen([sys.executable, "-c", SERVICE, str(station.root), str(port), str(standin), str(beat)],
                           cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pid = None
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=1)
                break
            except OSError:
                time.sleep(0.2)
        req = urllib.request.Request(f"http://127.0.0.1:{port}/scan/start",
                                     data=json.dumps({"scan": {"preset": "band1", "tag": "standin"}}).encode(),
                                     headers={"Content-Type": "application/json"})
        out = json.loads(urllib.request.urlopen(req, timeout=90).read())
        assert out["started"] and out["pid"]
        for _ in range(100):
            if beat.exists() and beat.read_text().strip():
                break
            time.sleep(0.2)
        pid = int(beat.read_text().split()[0])
        wrapper = out["pid"]
        line, p = [], pid
        while p and p not in line and len(line) < 12:          # the stand-in's parents, nearest first
            line.append(p)
            p = _parent_of(p)
        # the stand-in runs under the launch wrapper (the venv's python.exe is a launcher, so a
        # level or two between), and neither the service nor this test is anywhere above it
        assert wrapper in line
        assert svc.pid not in line and os.getpid() not in line
        svc.kill()
        svc.wait(10)
        n = len(beat.read_text().splitlines())
        time.sleep(2.0)
        assert _pid_alive(pid) and len(beat.read_text().splitlines()) > n      # still beating
    finally:
        if svc.poll() is None:
            svc.kill()
        for p in (pid, ):
            if p and _pid_alive(p):
                os.kill(p, 15)
        try:
            out_pid = out["pid"]
            if _pid_alive(out_pid):
                os.kill(out_pid, 15)
        except Exception:  # noqa: BLE001
            pass
