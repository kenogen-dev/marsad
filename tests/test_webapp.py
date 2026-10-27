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


def test_marsad_run_uses_the_same_check(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(webapp, "ensure_running", lambda station, port=8731: seen.append(port) or "up")
    assert record.start_live_view(_station(tmp_path)) == "up"
    assert seen == [8731]


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
    assert webapp.main(["restart"], _station(tmp_path)) == 1
    assert stopped == []
    assert "Probe-tab run is recording" in capsys.readouterr().out


def test_restart_with_force_goes_ahead_and_confirms(monkeypatch, tmp_path, capsys):
    stopped = []
    monkeypatch.setattr(webapp, "probe_state", lambda port=8731: "recording")
    monkeypatch.setattr(webapp, "viewer_answers", lambda port=8731: {"follow": True})
    monkeypatch.setattr(webapp, "stop", lambda port=8731: stopped.append(port) or (True, 11))
    monkeypatch.setattr(webapp, "start", lambda station, port=8731: ("started", 22))
    assert webapp.main(["restart", "--force"], _station(tmp_path)) == 0
    assert stopped == [8731]
    out = capsys.readouterr().out
    assert "stopped (pid 11)" in out and "started on 8731, pid 22" in out


def test_restart_when_idle_stops_then_starts(monkeypatch, tmp_path):
    order = []
    monkeypatch.setattr(webapp, "probe_state", lambda port=8731: "idle")
    monkeypatch.setattr(webapp, "viewer_answers", lambda port=8731: {"follow": True})
    monkeypatch.setattr(webapp, "stop", lambda port=8731: order.append("stop") or (True, 11))
    monkeypatch.setattr(webapp, "start", lambda station, port=8731: order.append("start") or ("started", 22))
    assert webapp.main(["restart"], _station(tmp_path)) == 0
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
