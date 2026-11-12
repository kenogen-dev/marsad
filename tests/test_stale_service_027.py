"""Relay 027: no more "new window, old service", no more hangs, and setup can always move on.

Hermetic: no radio is opened and no real station is touched. Radio programs are a made-up process table; the hanging
radio is a stand-in script; the services are tiny local HTTP servers on free ports.
"""
from __future__ import annotations

import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from marsad import live, probe, radio_holders as RH, service_info as SI, setup as S
from marsad.config import Station


def new_station(tmp_path) -> Station:
    root = tmp_path / "made-up station"
    S.create_station(root, "made-up station")
    return Station.load(root)


def add(st, nick="radio-one", serial="90000011", tuner="r820t", **kw):
    S.add_radio(st, serial, nick, tuner, **kw)
    return Station.load(st.root)


# ---------------------------------------------------------------- 1. the build stamp and the idle exit

def test_the_stamp_names_the_code_and_changes_with_it(tmp_path):
    pkg = tmp_path / "pkg"
    (pkg / "sub").mkdir(parents=True)
    (pkg / "a.py").write_text("x = 1\n")
    (pkg / "sub" / "b.py").write_text("y = 2\n")
    one = SI.stamp_of(pkg)
    assert one and len(one) == 12 and SI.stamp_of(pkg) == one         # same files, same stamp
    (pkg / "sub" / "b.py").write_text("y = 3\n")
    assert SI.stamp_of(pkg) != one                                    # any edit changes it
    assert SI.build_stamp() == SI.build_stamp() and len(SI.build_stamp()) == 12


def test_a_station_with_no_recordings_lets_its_service_go_when_idle(tmp_path, monkeypatch):
    # the owner's service lived 12 h: the idle exit waited for a survey with rows, and a new station has none
    st = new_station(tmp_path)
    monkeypatch.setattr(live, "LAST_REQUEST", [1000.0])
    assert live.idle_exit_due(st, None, 300, False, now=1000.0 + 301)
    assert not live.idle_exit_due(st, None, 300, False, now=1000.0 + 200)        # asked recently
    assert not live.idle_exit_due(st, None, 300, True, now=1000.0 + 9999)       # a Probe-tab run records
    st = add(st)
    log = st.root / "logs" / "x.log"
    log.parent.mkdir(exist_ok=True)
    log.write_text("")
    doc = S.load(st)
    doc["jobs"].append({"id": "1", "radio": "radio-one", "step": "range", "log": str(log), "started": S.now()})
    S._save(st.root, doc)
    assert not live.idle_exit_due(st, None, 300, False, now=1000.0 + 9999)      # a measurement not yet ended
    assert SI.busy(st) == ["the range measurement on radio-one"]
    Path(str(log) + ".exit").write_text('{"exit_code": 0}')
    assert live.idle_exit_due(st, None, 300, False, now=1000.0 + 9999) and SI.busy(st) == []


# ---------------------------------------------------------------- 2. which program holds a radio

def table(*rows):
    return [dict(zip(("pid", "ppid", "name", "created"), r)) for r in rows]


def test_holders_name_every_program_and_offer_only_marsads_own_leftover(tmp_path):
    st = add(new_station(tmp_path))
    t0 = time.time() - 600
    # the ledger: a setup job's wrapper (pid 500, job "J1", ended) and Detect's rtl_eeprom (pid 700, long past its limit)
    RH.note_started(st, 500, "tuning range on radio-one", until_s=60, job="J1", radio="radio-one", step="range")
    RH.note_started(st, 700, "radio check (rtl_eeprom)", until_s=20)
    rows = RH.ledger(st)
    for e in rows:
        e["created"], e["until"] = t0, t0 + 20                              # as if started ten minutes ago
    (st.root / "logs" / RH.LEDGER).write_text(json.dumps({"schema": 1, "processes": rows}))
    procs = table((600, 500, "rtl_sdr.exe", t0 + 5),                         # its wrapper ended: a leftover
                  (700, 1, "rtl_eeprom.exe", t0),                            # Detect's own, outlived its limit
                  (800, 801, "rtl_power.exe", t0), (801, 2, "python.exe", t0 - 1),   # a Marsad scan
                  (900, 3, "sdrpp.exe", t0), (950, 4, "rtl_tcp.exe", t0),
                  (1000, 5, "notepad.exe", t0))
    cmd = {801: f'python -m marsad --station "{tmp_path / "other"}" run LS 1h'}
    hs = {h["pid"]: h for h in RH.holders(st, procs=procs, cmdlines=cmd, now=t0 + 600)}
    assert set(hs) == {600, 700, 800, 900, 950}                             # notepad holds no radio
    assert hs[600]["kind"] == "setup-leftover" and hs[600]["stoppable"]
    assert hs[700]["kind"] == "setup-leftover" and hs[700]["stoppable"]
    assert hs[800]["kind"] == "marsad-run" and not hs[800]["stoppable"] and "never stops" in hs[800]["text"]
    assert hs[900]["kind"] == "sdrpp" and hs[950]["kind"] == "other" and not hs[950]["stoppable"]
    words = RH.timeout_words("Detect", 15, list(hs.values()))
    assert "didn't finish within 15 s" in words and "SDR++" in words and "stop the leftover" in words


def test_a_measurement_still_running_is_named_never_offered(tmp_path):
    st = add(new_station(tmp_path))
    log = st.root / "logs" / "run.log"
    log.parent.mkdir(exist_ok=True)
    log.write_text("")
    doc = S.load(st)
    doc["jobs"].append({"id": "J2", "radio": "radio-one", "step": "floor", "log": str(log), "started": S.now()})
    S._save(st.root, doc)
    e = RH.note_started(st, 510, "its own floor on radio-one", until_s=600, job="J2", radio="radio-one", step="floor")
    procs = table((610, 510, "rtl_power.exe", e["created"] + 1))
    (h,) = RH.holders(st, procs=procs, cmdlines={}, now=e["created"] + 30)
    assert h["kind"] == "setup-running" and not h["stoppable"] and "Cancel it on its step" in h["text"]


def test_a_reused_process_number_is_not_mistaken_for_marsads(tmp_path):
    st = add(new_station(tmp_path))
    e = RH.note_started(st, 520, "tuner sweep on radio-one", until_s=60, radio="radio-one", step="range")
    # pid 520 now belongs to an unrelated program started later; an rtl_sdr older than the ledger entry is not ours
    procs = table((520, 1, "explorer.exe", e["created"] + 500), (620, 9, "rtl_sdr.exe", e["created"] - 100))
    (h,) = RH.holders(st, procs=procs, cmdlines={}, now=e["created"] + 600)
    assert h["kind"] == "other" and not h["stoppable"]


def test_only_a_leftover_can_be_stopped(tmp_path, monkeypatch):
    st = add(new_station(tmp_path))
    monkeypatch.setattr(RH, "holders", lambda station, **k: [
        {"pid": 42, "program": "rtl_power", "kind": "marsad-run", "stoppable": False, "text": "rtl_power: a scan"}])
    killed = []
    monkeypatch.setattr(RH.subprocess, "run", lambda *a, **k: killed.append(a))
    monkeypatch.setattr(RH.host, "kill_pid", lambda pid: killed.append(pid))
    with pytest.raises(ValueError, match="only stops its own leftover"):
        RH.stop_leftover(st, 42)
    assert not killed                                                     # rule 1: a scan is never touched
    with pytest.raises(ValueError, match="confirmation"):
        live.setup_post(st, "/setup/stop_leftover", {"pid": 42})


# ---------------------------------------------------------------- 3. Detect never hangs

HANG = "import time, sys\nopen(sys.argv[0] + '.started', 'w').write('x')\ntime.sleep(120)\n"


def test_detect_stops_a_radio_that_does_not_answer_and_says_so(tmp_path, monkeypatch):
    st = new_station(tmp_path)
    fake = tmp_path / "hang.py"
    fake.write_text(HANG)
    monkeypatch.setenv("MARSAD_TEST_EEPROM", str(fake))
    t = time.monotonic()
    got = probe.detect_receivers(st, timeout_s=1.5, purpose="Detect (rtl_eeprom)")
    assert got["timed_out"] and got["index"] == 0 and time.monotonic() - t < 15
    (e,) = RH.ledger(st)
    assert e["purpose"] == "Detect (rtl_eeprom)"
    time.sleep(0.5)
    assert not any(p["pid"] == e["pid"] for p in RH.snapshot())            # the stand-in was ended, not left behind


def test_detect_answers_with_the_reason_and_the_way_round(tmp_path, monkeypatch):
    st = new_station(tmp_path)
    monkeypatch.setattr(live.host, "running_sdr_programs", lambda: [])
    monkeypatch.setattr(live, "detect_receivers", lambda st_: {"found": [], "error": "timed out", "timed_out": True,
                                                               "index": 0, "timeout_s": 15})
    monkeypatch.setattr(live, "radio_holders", lambda st_: [])
    d = live.setup_post(st, "/setup/detect", {})
    assert d["kind"] == "timeout" and "unplug it, wait 5 seconds, plug it back in" in d["problem"].lower()
    assert "by hand" in d["by_hand"] and "error" not in d                  # an answer, so the window gets it all
    leftover = {"pid": 7, "program": "rtl_sdr", "kind": "setup-leftover", "stoppable": True,
                "text": "rtl_sdr: left over from Marsad's tuner sweep on radio-one"}
    monkeypatch.setattr(live, "radio_holders", lambda st_: [leftover])
    d = live.setup_post(st, "/setup/detect", {})
    assert d["holders"] == [leftover] and "left over" in d["problem"]


# ---------------------------------------------------------------- 4. measurements time out

def test_a_measurement_past_its_limit_is_ended_and_kept_as_failed_with_why(tmp_path, monkeypatch):
    st = add(new_station(tmp_path))
    log = st.root / "logs" / "sweep.log"
    log.parent.mkdir(exist_ok=True)
    log.write_text("tuner-sweep: progress 3 of 120: tuning 72 MHz\n")
    long_ago = time.time() - live.step_timeout_s("range", False) - 60
    import datetime as dt
    doc = S.load(st)
    doc["jobs"].append({"id": "J9", "radio": "radio-one", "step": "range", "log": str(log), "pid": 4321,
                        "started": dt.datetime.fromtimestamp(long_ago).isoformat(timespec="seconds")})
    S._save(st.root, doc)
    ended = []
    monkeypatch.setattr(live, "_end_setup_job", lambda station, job: ended.append(job["id"]) or
                        Path(job["log"] + ".exit").write_text('{"exit_code": -2, "cancelled": true}'))
    monkeypatch.setattr(live, "radio_holders", lambda st_: [])
    monkeypatch.setattr(live, "usb_radios", lambda: {})
    live.setup_status(st)
    assert ended == ["J9"]                                                 # its own tree only
    (res,) = S.load(st)["radios"]["radio-one"]["results"]["range"]
    assert res["timed_out"] and res["outcome"] == "failed" and "didn't finish within 20 min" in res["what"]
    assert "plug it back in" in " ".join(res["advice"]).lower()
    view = S.run_view(st, "radio-one", "range", lambda log_: {})
    assert view["state"] == "ended" and view["outcome"] == "failed"
    assert not S.load(st)["jobs"][-1].get("cancelled")


# ---------------------------------------------------------------- 5. step 1 can always be completed

def test_a_radio_added_by_hand_is_marked_until_detect_confirms_it(tmp_path):
    st = new_station(tmp_path)
    st = add(st, tuner_confirmed=False)
    c = S.checklist(st, "radio-one", "", set())
    assert c["steps"][0]["state"] == "done"                                # Continue works: step 1 is done
    assert "tuner not confirmed by Detect" in c["steps"][0]["detail"] and c["tuner_confirmed"] is False
    S.update_radio(st, "radio-one", tuner="r820t", tuner_confirmed=True)
    c = S.checklist(Station.load(st.root), "radio-one", "", set())
    assert c["tuner_confirmed"] and "not confirmed" not in c["steps"][0]["detail"]
    st2 = add(new_station(tmp_path / "b"), nick="radio-two")                # older callers: nothing said, no mark
    assert S.checklist(st2, "radio-two", "", set())["tuner_confirmed"]


def test_the_guide_starts_the_sweep_with_the_ledger_and_by_hand_it_writes_nothing(tmp_path):
    st = add(new_station(tmp_path))
    args = S.step_command(st, "radio-one", "range", False, 0, stop_file=str(tmp_path / "s.stop"))["args"]
    assert args[-1] == "--ledger" and "--stop-file" in args
    assert S.step_command(st, "radio-one", "range", False, 0)["args"] == ["tuner-sweep", "--receiver", "radio-one"]


# ---------------------------------------------------------------- 6. the window: plain words, restart rules

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(answers: dict):
    """A made-up service: GET/POST path -> (code, body dict)."""
    class H(BaseHTTPRequestHandler):
        def _go(self):
            code, body = answers.get(self.path.split("?")[0], (404, None))
            self.send_response(code)
            self.end_headers()
            if body is not None:
                self.wfile.write(json.dumps(body).encode())
        do_GET = do_POST = _go

        def log_message(self, *a):
            pass
    port = free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


def test_unknown_route_never_reaches_the_owner(monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from marsad_desktop import service as SV
    srv, port = serve({"/ping": (200, {"app": "marsad"}),            # an older service: it reports no build
                       "/setup/radio_remove": (400, {"error": "unknown route /setup/radio_remove"})})
    try:
        svc = SV.Service(f"http://127.0.0.1:{port}")
        with pytest.raises(SV.ServiceError) as e:
            svc._call_once("/setup/radio_remove", {"radio": "radio-one"})
        assert e.value.stale and "unknown route" not in str(e.value) and "restart" in str(e.value)
        assert svc.stale_seen
        with pytest.raises(SV.ServiceError) as e:                    # a bare 404 from an older service, too
            svc._call_once("/setup/holders", {})
        assert e.value.stale
    finally:
        srv.shutdown()
    srv, port = serve({"/ping": (200, {"app": "marsad", "build": SV.own_build()}),
                       "/x": (400, {"error": "unknown route /x"})})
    try:                                                             # same code: a fault, still in plain words
        with pytest.raises(SV.ServiceError) as e:
            SV.Service(f"http://127.0.0.1:{port}")._call_once("/x", {})
        assert not e.value.stale and "unknown route" not in str(e.value) and "fault in Marsad" in str(e.value)
    finally:
        srv.shutdown()


def test_an_older_service_is_restarted_only_when_idle_and_never_on_8731(tmp_path, monkeypatch):
    from marsad_desktop import stations as ST
    stop_listener = ST.stop_listener
    folder = new_station(tmp_path).root
    calls = []
    monkeypatch.setattr(ST, "own_build", lambda: "new000000000")
    monkeypatch.setattr(ST, "ping", lambda port, timeout=0.6: {"app": "marsad", "station": str(folder),
                                                               "build": "old000000000"})
    monkeypatch.setattr(ST, "stop_listener", lambda port, wait_s=15.0: calls.append(("stop", port)) or True)
    monkeypatch.setattr(ST, "_start_own", lambda f, port: calls.append(("start", port)))
    monkeypatch.setattr(ST, "_wait_answer", lambda f, port, w, build=None: f"http://127.0.0.1:{port}")
    # a station the window serves, nothing running: restarted on the spot, one line said
    monkeypatch.setattr(ST, "serving", lambda f: "http://127.0.0.1:8752")
    monkeypatch.setattr(ST, "station_busy", lambda f, port, d=None: [])
    st = ST.ensure_service_ex(folder)
    assert calls == [("stop", 8752), ("start", 8752)] and st["note"] == "Station service updated." and not st["stale"]
    # while a run is on: kept, never killed (rule 1); the window says it waits for the run
    calls.clear()
    monkeypatch.setattr(ST, "station_busy", lambda f, port, d=None: ["the floor measurement on radio-one"])
    st = ST.ensure_service_ex(folder)
    assert calls == [] and st["stale"] and st["busy"] == ["the floor measurement on radio-one"]
    # the home station's service on 8731: never restarted by itself
    monkeypatch.setattr(ST, "station_busy", lambda f, port, d=None: [])
    monkeypatch.setattr(ST, "serving", lambda f: "http://127.0.0.1:8731")
    st = ST.ensure_service_ex(folder)
    assert calls == [] and st["stale"] and st["home"]
    assert not ST.restart_service("http://127.0.0.1:8731", folder)["ok"]          # only on Restart now
    with pytest.raises(ValueError, match="only restarts the services it starts"):
        stop_listener(8731)                                                       # the real one refuses 8731
    assert calls == []
