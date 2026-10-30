"""marsad web / the check before every scan (2026-10-27).

The web app on 8731 must be up whenever a scan starts, started in the background
with no window and no idle timeout if it is not, and never restarted under a
Probe-tab run (which is the app's own child). No port is bound and no process
is started here: the launch, the HTTP answers and netstat are all replaced.
"""
import subprocess
import sys

from conftest import ROOT

sys.path.insert(0, str(ROOT))

from marsad import host, record, webapp   # noqa: E402
from marsad.config import Station          # noqa: E402


class FakeProc:
    pid = 777


def _station(tmp_path):
    return Station.load(tmp_path)    # live_view.log lands in tmp, not the real reports/


def test_already_up_is_reused_and_nothing_is_started(monkeypatch, tmp_path, capsys):
    launched = []
    monkeypatch.setattr(webapp, "viewer_answers", lambda port=8731: {"follow": True})
    monkeypatch.setattr(webapp, "listener_pid", lambda port=8731: 4242)
    monkeypatch.setattr(webapp, "launch_background", lambda *a, **k: launched.append(a))
    assert webapp.ensure_running(_station(tmp_path)) == "up"
    assert launched == []
    assert "web app: already up on 8731" in capsys.readouterr().out


def test_nothing_answering_starts_it_and_confirms_with_a_get(monkeypatch, tmp_path, capsys):
    launched = []
    monkeypatch.setattr(webapp, "viewer_answers", lambda port=8731: {"follow": True} if launched else None)
    monkeypatch.setattr(host, "port_in_use", lambda port, host_addr="127.0.0.1": False)
    monkeypatch.setattr(webapp, "listener_pid", lambda port=8731: 4242 if launched else None)
    monkeypatch.setattr(webapp, "launch_background",
                        lambda station, port: launched.append(port) or FakeProc())
    assert webapp.ensure_running(_station(tmp_path)) == "started"
    assert launched == [8731]
    assert "web app: started on 8731, pid 4242" in capsys.readouterr().out


def test_a_port_held_by_another_program_is_left_alone(monkeypatch, tmp_path, capsys):
    launched = []
    monkeypatch.setattr(webapp, "viewer_answers", lambda port=8731: None)
    monkeypatch.setattr(host, "port_in_use", lambda port, host_addr="127.0.0.1": True)
    monkeypatch.setattr(webapp, "listener_pid", lambda port=8731: 99)
    monkeypatch.setattr(webapp, "launch_background", lambda *a, **k: launched.append(a))
    assert webapp.ensure_running(_station(tmp_path)) == "blocked"
    assert launched == []
    assert "the scan goes on" in capsys.readouterr().out


def test_a_failed_start_never_stops_the_scan(monkeypatch, tmp_path, capsys):
    def boom(*a, **k):
        raise OSError("no python")
    monkeypatch.setattr(webapp, "viewer_answers", lambda port=8731: None)
    monkeypatch.setattr(host, "port_in_use", lambda port, host_addr="127.0.0.1": False)
    monkeypatch.setattr(webapp, "launch_background", boom)
    assert webapp.ensure_running(_station(tmp_path)) == "failed"
    assert "the scan goes on" in capsys.readouterr().out


def _real_station(tmp_path):
    (tmp_path / "station.json").write_text("{}", encoding="utf-8")
    return Station.load(tmp_path)


def test_marsad_run_uses_the_same_check(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(webapp, "ensure_running", lambda station, port=8731: seen.append(port) or "up")
    assert record.start_live_view(_real_station(tmp_path)) == "up"
    assert seen == [8731]


# --- a station with no station.json never takes 8731 (2026-10-27) ---------------

def test_empty_station_gets_a_test_port_when_8731_is_down(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(webapp, "viewer_answers", lambda port=8731: None)
    monkeypatch.setattr(host, "port_in_use", lambda port, host_addr="127.0.0.1": False)
    assert webapp.port_for(_station(tmp_path)) == 8745
    out = capsys.readouterr().out
    assert "no station.json" in out and "test port 8745" in out
    assert r"marsad --station C:\SDR web restart" in out
    assert len(out.splitlines()) == 1                    # one line


def test_empty_station_skips_busy_test_ports(monkeypatch, tmp_path):
    monkeypatch.setattr(webapp, "viewer_answers", lambda port=8731: None)
    monkeypatch.setattr(host, "port_in_use", lambda port, host_addr="127.0.0.1": port in (8745, 8740))
    assert webapp.port_for(_station(tmp_path)) == 8741


def test_empty_station_with_every_test_port_busy_starts_nothing(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(webapp, "viewer_answers", lambda port=8731: None)
    monkeypatch.setattr(host, "port_in_use", lambda port, host_addr="127.0.0.1": True)
    assert webapp.port_for(_station(tmp_path)) is None
    assert "not started" in capsys.readouterr().out


def test_empty_station_uses_8731_when_it_is_already_up(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(webapp, "viewer_answers", lambda port=8731: {"follow": True} if port == 8731 else None)
    assert webapp.port_for(_station(tmp_path)) == 8731
    assert capsys.readouterr().out == ""


def test_a_real_station_keeps_8731(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(webapp, "viewer_answers", lambda port=8731: None)
    assert webapp.port_for(_real_station(tmp_path)) == 8731
    assert capsys.readouterr().out == ""


def test_marsad_run_from_an_empty_station_does_not_take_8731(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(webapp, "viewer_answers", lambda port=8731: None)
    monkeypatch.setattr(host, "port_in_use", lambda port, host_addr="127.0.0.1": False)
    monkeypatch.setattr(webapp, "ensure_running", lambda station, port=8731: seen.append(port) or "started")
    record.start_live_view(_station(tmp_path))
    assert seen == [8745]


def test_web_restart_from_an_empty_station_is_refused(monkeypatch, tmp_path, capsys):
    """Otherwise it would stop the owner's app and start an empty one in its place."""
    stopped = []
    monkeypatch.setattr(webapp, "probe_state", lambda port=8731: "idle")
    monkeypatch.setattr(webapp, "stop", lambda port=8731: stopped.append(port) or (True, 1))
    assert webapp.main(["restart"], _station(tmp_path)) == 1
    assert stopped == []
    assert "web restart" in capsys.readouterr().out


# --- web restart waits long enough for a slow start (2026-10-27) ----------------

class SlowProc:
    pid = 888

    def poll(self):
        return None                                     # still running


def test_restart_waits_longer_than_the_scan_check(monkeypatch, tmp_path):
    timeouts = []
    monkeypatch.setattr(webapp, "probe_state", lambda port=8731: "idle")
    monkeypatch.setattr(webapp, "viewer_answers", lambda port=8731: {"follow": True})
    monkeypatch.setattr(webapp, "stop", lambda port=8731: (True, 11))
    monkeypatch.setattr(webapp, "start",
                        lambda station, port=8731, timeout=20: timeouts.append(timeout) or ("started", 22))
    assert webapp.main(["restart"], _real_station(tmp_path)) == 0
    assert timeouts == [webapp.RESTART_TIMEOUT] and webapp.RESTART_TIMEOUT >= 60


def test_a_live_process_not_yet_answering_is_starting_not_failed(monkeypatch, tmp_path):
    monkeypatch.setattr(webapp, "viewer_answers", lambda port=8731: None)
    monkeypatch.setattr(host, "port_in_use", lambda port, host_addr="127.0.0.1": False)
    monkeypatch.setattr(webapp, "launch_background", lambda station, port: SlowProc())
    assert webapp.start(_real_station(tmp_path), 8731, timeout=0) == ("starting", 888)


def test_restart_reports_a_slow_start_honestly(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(webapp, "probe_state", lambda port=8731: "idle")
    monkeypatch.setattr(webapp, "viewer_answers", lambda port=8731: {"follow": True})
    monkeypatch.setattr(webapp, "stop", lambda port=8731: (True, 11))
    monkeypatch.setattr(webapp, "start", lambda station, port=8731, timeout=20: ("starting", 22))
    assert webapp.main(["restart"], _real_station(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "still loading" in out and "did not come back" not in out


def test_background_launch_has_no_window_no_idle_timeout_and_its_own_folder(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: calls.append((cmd, kw)) or FakeProc())
    st = _station(tmp_path)
    webapp.launch_background(st, 8731)
    cmd, kw = calls[0]
    assert "live" in cmd and cmd[cmd.index("--port") + 1] == "8731"
    assert "--exit-after-idle" not in cmd
    assert kw["cwd"] == str(st.root)
    assert (tmp_path / "reports" / "live_view.log").exists()
    if host.IS_WINDOWS:
        assert kw["creationflags"] & subprocess.CREATE_NEW_CONSOLE
        assert kw["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP
        assert kw["startupinfo"].wShowWindow == 0          # hidden


def test_restart_is_refused_while_a_probe_run_records(monkeypatch, tmp_path, capsys):
    stopped = []
    monkeypatch.setattr(webapp, "probe_state", lambda port=8731: "recording")
    monkeypatch.setattr(webapp, "stop", lambda port=8731: stopped.append(port) or (True, 1))
    assert webapp.main(["restart"], _real_station(tmp_path)) == 1
    assert stopped == []
    assert "Probe-tab run is recording" in capsys.readouterr().out


def test_restart_with_force_goes_ahead_and_confirms(monkeypatch, tmp_path, capsys):
    stopped = []
    monkeypatch.setattr(webapp, "probe_state", lambda port=8731: "recording")
    monkeypatch.setattr(webapp, "viewer_answers", lambda port=8731: {"follow": True})
    monkeypatch.setattr(webapp, "stop", lambda port=8731: stopped.append(port) or (True, 11))
    monkeypatch.setattr(webapp, "start", lambda station, port=8731, timeout=20: ("started", 22))
    assert webapp.main(["restart", "--force"], _real_station(tmp_path)) == 0
    assert stopped == [8731]
    out = capsys.readouterr().out
    assert "stopped (pid 11)" in out and "started on 8731, pid 22" in out


def test_restart_when_idle_stops_then_starts(monkeypatch, tmp_path):
    order = []
    monkeypatch.setattr(webapp, "probe_state", lambda port=8731: "idle")
    monkeypatch.setattr(webapp, "viewer_answers", lambda port=8731: {"follow": True})
    monkeypatch.setattr(webapp, "stop", lambda port=8731: order.append("stop") or (True, 11))
    monkeypatch.setattr(webapp, "start", lambda station, port=8731, timeout=20: order.append("start") or ("started", 22))
    assert webapp.main(["restart"], _real_station(tmp_path)) == 0
    assert order == ["stop", "start"]


def test_listener_pid_reads_netstat(monkeypatch):
    if not host.IS_WINDOWS:
        return
    out = ("  Proto  Local Address          Foreign Address        State           PID\n"
           "  TCP    127.0.0.1:8730         0.0.0.0:0              LISTENING       1\n"
           "  TCP    127.0.0.1:8731         0.0.0.0:0              LISTENING       5150\n"
           "  TCP    127.0.0.1:8731         127.0.0.1:50000        ESTABLISHED     6\n")
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=out))
    assert webapp.listener_pid(8731) == 5150
    assert webapp.listener_pid(8740) is None


def test_a_test_viewer_off_8731_still_exits_when_idle(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: calls.append(cmd) or FakeProc())
    webapp.launch_background(_station(tmp_path), 8745)
    cmd = calls[0]
    assert cmd[cmd.index("--exit-after-idle") + 1] == "5"


def test_stop_terminates_the_listener_directly_never_through_a_child(monkeypatch):
    """2026-10-29: `marsad web stop` ran taskkill with pipes, which never returns on this
    PC; the timeout was swallowed and 8731 stayed up. Now it is one direct call."""
    killed, freed = [], []
    monkeypatch.setattr(webapp, "listener_pid", lambda port=8731: 5150)
    monkeypatch.setattr(host, "kill_pid", lambda pid: killed.append(pid) or freed.append(True))
    monkeypatch.setattr(host, "port_in_use", lambda port: not freed)

    def no_child(*a, **k):
        raise AssertionError(f"stop() started a child process: {a}")
    monkeypatch.setattr(subprocess, "run", no_child)
    monkeypatch.setattr(subprocess, "Popen", no_child)
    assert webapp.stop(8745, timeout=2) == (True, 5150)
    assert killed == [5150]


def test_stop_reports_a_listener_it_could_not_end(monkeypatch):
    monkeypatch.setattr(webapp, "listener_pid", lambda port=8731: 5150)

    def denied(pid):
        raise PermissionError("access denied")
    monkeypatch.setattr(host, "kill_pid", denied)
    monkeypatch.setattr(host, "port_in_use", lambda port: True)
    assert webapp.stop(8745, timeout=0.3) == (False, 5150)


def test_sdr_check_lists_processes_without_tasklist(monkeypatch):
    """tasklist from Python also hangs on this PC, so the SDR-in-use check reads the
    process list with one system call."""
    if not host.IS_WINDOWS:
        return

    def no_child(*a, **k):
        raise AssertionError("running_sdr_programs started a child process")
    monkeypatch.setattr(subprocess, "run", no_child)
    monkeypatch.setattr(host, "windows_process_names",
                        lambda: ["system", "rtl_power_gainfix.exe", "python.exe", "sdrpp.exe"])
    assert host.running_sdr_programs() == ["rtl_power_gainfix.exe", "sdrpp.exe"]
    monkeypatch.undo()
    assert "python.exe" in host.windows_process_names()     # the real call, on this machine
