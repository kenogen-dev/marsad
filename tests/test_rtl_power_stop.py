"""Stop for rtl_power runs (relay 010, part 3). Every rtl_power run now has a stop file, as
every rtl_power_fftw run already had: Stop (the window's, or the Scan page's /scan/stop) writes
it, `marsad run` asks rtl_power to finish, and the partial recording is kept, analysed and
archived exactly as after Ctrl-C. Tested with a made-up rtl_power that writes synthetic sweeps;
no radio is involved."""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

from marsad import live, record
from marsad.config import Station

DATA = Path(__file__).parent / "data"

FAKE = r'''
import signal, sys, time, datetime as dt
args = sys.argv[1:]
lo, hi, _ = args[args.index("-f") + 1].split(":")
out = args[-1]
mhz = lambda t: float(t.rstrip("M")) * 1e6
lo, hi = mhz(lo), mhz(hi)
stop = []
for name in ("SIGBREAK", "SIGINT"):
    if hasattr(signal, name):
        signal.signal(getattr(signal, name), lambda *a: stop.append(1))
n, step = 64, (hi - lo) / 2 / 64
with open(out, "w") as f:
    k = 0
    while not stop and k < 400:
        now = dt.datetime.now()
        for h in range(2):
            a = lo + h * (hi - lo) / 2
            vals = ", ".join(f"{-40 + ((i * 7 + k) % 5) * 0.3 + (12 if i == 20 else 0):.2f}" for i in range(n))
            f.write(f"{now:%Y-%m-%d}, {now:%H:%M:%S}, {int(a)}, {int(a + n * step)}, {step:.2f}, 100, {vals}\n")
        f.flush()
        k += 1
        time.sleep(0.3)
sys.exit(0)
'''


def test_stop_keeps_analyses_and_archives_a_partial_rtl_power_run(tmp_path, monkeypatch):
    root = tmp_path / "st"
    root.mkdir()
    shutil.copy(DATA / "builder_station.json", root / "station.json")
    station = Station.load(str(root))
    fake = tmp_path / "fake_rtl_power.py"
    fake.write_text(FAKE)
    real_popen = subprocess.Popen

    def popen(cmd, *a, **k):
        if isinstance(cmd, list) and cmd and cmd[0] == "FAKE_RTL_POWER":
            cmd = [sys.executable, str(fake), *cmd[1:]]
        return real_popen(cmd, *a, **k)
    monkeypatch.setattr(record.subprocess, "Popen", popen)
    monkeypatch.setattr(Station, "rtl_power", lambda self: (Path("FAKE_RTL_POWER"), None))
    monkeypatch.setattr(record, "check_device_free", lambda non_interactive: None)     # no radio
    monkeypatch.setattr(record, "device_check", lambda st, device: None)              # no rtl_eeprom
    rc = []
    args = ["band1", "1h", "--tag", "stoptest", "--chain", "made-up antenna > Coax > radio-a > USB > PC",
            "--conditions", "made-up conditions", "--non-interactive", "--no-notify", "--no-live"]
    t = threading.Thread(target=lambda: rc.append(record.main(args, station)))
    t.start()
    surveys = root / "surveys"
    side = None
    for _ in range(200):
        js = list(surveys.glob("survey_band1_1h_stoptest_*.json"))
        csv = js[0].with_suffix(".csv") if js else None
        if csv and csv.exists() and len(csv.read_text().splitlines()) >= 8:
            side = json.loads(js[0].read_text())
            break
        time.sleep(0.1)
    assert side, "the made-up rtl_power never wrote four sweeps"
    stem = js[0].stem
    # every rtl_power run carries a stop file now, named in its sidecar, under reports/
    assert side["stop_file"] == str(station.reports / f"{stem}.stop")

    class Tail:                                  # what /scan/stop hands stop_recording
        sidecar = side
    assert live.stop_recording(station, Tail())["ok"]
    t.join(60)
    assert not t.is_alive()

    done = json.loads(js[0].read_text())
    assert done["interrupted"] is True and done["exit_code"] == 130 and done.get("finished_local")
    assert csv.stat().st_size > 0 and done["csv_bytes"] == csv.stat().st_size      # the partial CSV is kept
    assert rc == [0]                                                                   # the analyser's exit
    rep = station.reports / stem
    assert (rep / "report.md").exists() and (rep / "scan_build.md").exists()          # analysed
    assert "interrupted=True" in (rep / "scan_build.md").read_text()
    con = sqlite3.connect(station.db)
    assert con.execute("SELECT COUNT(*) FROM surveys WHERE name LIKE ?", (f"%{stem}%",)).fetchone()[0] == 1   # archived
    con.close()
    assert not Path(side["stop_file"]).exists()                                        # the flag is cleared


def test_old_run_without_stop_file_is_refused_in_plain_words():
    class Tail:
        sidecar = {"csv": "old.csv"}
    try:
        live.stop_recording(None, Tail())
    except ValueError as exc:
        assert "older marsad" in str(exc)
    else:
        raise AssertionError("an old run without a stop file must be refused")


def test_service_offers_and_accepts_stop_for_a_running_rtl_power_csv(tmp_path):
    """Found on the first real run from the window: the CSV reader dropped the stop file, so the
    service never offered Stop for rtl_power runs and /scan/stop would have refused it."""
    root = tmp_path / "st"
    (root / "surveys").mkdir(parents=True)
    shutil.copy(DATA / "builder_station.json", root / "station.json")
    station = Station.load(str(root))
    csv = root / "surveys" / "survey_band1_5m_x_20010101-0000.csv"
    side = {"csv": csv.name, "receiver": "radio-a", "duration_requested_s": 300, "started_local": "2001-01-01T00:00:00",
            "stop_file": str(root / "reports" / "x.stop"),
            "rtl_power": {"freq_low_mhz": 300.0, "freq_high_mhz": 301.0, "integration_s": 30, "gain_db": 29}}
    csv.with_suffix(".json").write_text(json.dumps(side))
    csv.write_text("2001-01-01, 00:00:30, 300000000, 301000000, 100000.00, 10, " + ", ".join(["-40"] * 10) + "\n")
    t = live.Tailer(csv, 100, 50)
    t.poll()
    assert t.payload(0)["can_stop"] is True
    assert live.stop_recording(station, t)["ok"] and Path(side["stop_file"]).exists()
    side["finished_local"] = "2001-01-01T00:03:00"                     # the run is over: no Stop
    csv.with_suffix(".json").write_text(json.dumps(side))
    t._sc_at = 0
    assert t.payload(0)["can_stop"] is False
    del side["stop_file"], side["finished_local"]                      # an older run: no stop file, no Stop
    csv.with_suffix(".json").write_text(json.dumps(side))
    old = live.Tailer(csv, 100, 50)
    old.poll()
    assert old.payload(0)["can_stop"] is False
