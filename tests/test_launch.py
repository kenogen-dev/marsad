"""The argument-less launch: what a double-click on marsad.exe does.

Nothing here binds a port or opens a real browser; the two outside calls are
replaced and recorded.
"""
import sys

from conftest import ROOT

sys.path.insert(0, str(ROOT))

from marsad import cli, host, live   # noqa: E402


def test_no_arguments_opens_the_running_viewer_instead_of_a_second_one(monkeypatch):
    opened, started = [], []
    monkeypatch.setattr(host, "port_in_use", lambda port, host_addr="127.0.0.1": port == 8731)
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setattr(live, "main", lambda *a, **k: started.append(a) or 0)
    monkeypatch.setattr(cli, "_linger", lambda *a, **k: None)
    assert cli.main([]) == 0
    assert opened == ["http://127.0.0.1:8731/"]
    assert started == []                      # no second viewer on a served port


def test_no_arguments_starts_the_app_on_8731_with_the_browser_when_free(monkeypatch):
    calls = []
    monkeypatch.setattr(host, "port_in_use", lambda port, host_addr="127.0.0.1": False)
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: None)

    def fake_live(argv, station, prog="marsad live", banner=None):
        calls.append((argv, banner))
        return 0
    monkeypatch.setattr(live, "main", fake_live)
    assert cli.main([]) == 0
    argv, banner = calls[0]
    assert "--open" in argv and argv[argv.index("--port") + 1] == "8731"
    assert "close this window to stop" in banner


def test_a_typed_command_is_not_the_launcher(monkeypatch):
    monkeypatch.setattr(host, "port_in_use", lambda *a, **k: (_ for _ in ()).throw(AssertionError("launcher ran")))
    assert cli.main(["version"]) == 0


def test_builder_config_skips_presets_the_builder_cannot_write():
    """An rtl_power_fftw preset has no bin width in its range; it must not take the page down."""
    from marsad.config import Station
    st = Station.load(ROOT)
    st.data["presets"] = {
        "LS": {"freq": "1241M:2148M:50k", "gain": 29.0, "integration": 30, "receiver": "xtr", "note": ""},
        "top": {"engine": "rtl_power_fftw", "freq": "2147M:2187M", "bins": 128, "rate": 2400000,
                "gain": 29.0, "integration": 1, "receiver": "xtr", "note": "above the ceiling"},
        "h1": {"engine": "rtl_power_fftw", "freq": "1420.4058M", "bins": 2048, "rate": 2400000,
               "gain": 29.7, "integration": 60, "receiver": "v5", "note": "hydrogen line"},
    }
    cfg = live.builder_config(st)
    assert "LS" in cfg["presets"] and "top" not in cfg["presets"] and "h1" not in cfg["presets"]
    assert cfg["preset_order"][0] == "LS"


def test_launcher_honours_the_test_hooks(monkeypatch):
    """MARSAD_PORT moves it off 8731 and MARSAD_NO_BROWSER keeps the browser shut."""
    calls, opened = [], []
    monkeypatch.setenv("MARSAD_PORT", "8745")
    monkeypatch.setenv("MARSAD_NO_BROWSER", "1")
    monkeypatch.setattr(host, "port_in_use", lambda port, host_addr="127.0.0.1": False)
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setattr(live, "main", lambda argv, station, prog="x", banner=None: calls.append(argv) or 0)
    assert cli.main([]) == 0
    assert calls == [["--port", "8745"]] and opened == []
