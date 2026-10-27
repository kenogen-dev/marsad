"""The argument-less launch: what a double-click on marsad.exe does.

Nothing here binds a port or opens a real browser; the two outside calls are
replaced and recorded.
"""
import sys

import pytest

from conftest import ROOT

sys.path.insert(0, str(ROOT))

from marsad import cli, host, live, webapp   # noqa: E402


class _Proc:
    pid = 1


def _never_in_console(*a, **k):
    raise AssertionError("the launcher ran the app in its own console")


def test_no_arguments_opens_the_running_viewer_instead_of_a_second_one(monkeypatch):
    opened, launched = [], []
    monkeypatch.setattr(webapp, "viewer_answers", lambda port=8731: {"follow": True} if port == 8731 else None)
    monkeypatch.setattr(webapp, "listener_pid", lambda port=8731: 4242)
    monkeypatch.setattr(webapp, "launch_background", lambda *a, **k: launched.append(a))
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setattr(cli, "_linger", lambda *a, **k: None)
    assert cli.main([]) == 0
    assert opened == ["http://127.0.0.1:8731/"]
    assert launched == []                     # no second viewer on a served port


def test_no_arguments_starts_the_app_in_the_background_when_free(monkeypatch):
    """Since 2026-10-27 the launcher no longer runs the app in its own console:
    closing that window took the app, and any Probe-tab scan, down with it."""
    launched, opened = [], []
    monkeypatch.setattr(webapp, "viewer_answers", lambda port=8731: {"follow": True} if launched else None)
    monkeypatch.setattr(webapp, "listener_pid", lambda port=8731: 4242)
    monkeypatch.setattr(host, "port_in_use", lambda port, host_addr="127.0.0.1": False)
    monkeypatch.setattr(webapp, "launch_background",
                        lambda station, port: launched.append(port) or _Proc())
    monkeypatch.setattr(live, "main", _never_in_console)
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setattr(cli, "_linger", lambda *a, **k: None)
    assert cli.main([]) == 0
    assert launched == [8731] and opened == ["http://127.0.0.1:8731/"]


def test_a_typed_command_is_not_the_launcher(monkeypatch):
    monkeypatch.setattr(host, "port_in_use", lambda *a, **k: (_ for _ in ()).throw(AssertionError("launcher ran")))
    assert cli.main(["version"]) == 0


def test_builder_config_carries_both_engines():
    """An rtl_power_fftw preset has no bin width in its range, which once took the page
    down (2026-10-22, fixed by skipping them). Since the Builder gained an engine choice
    the same evening, such presets travel with their own fields - range, centre, bins,
    rate, seconds per hop, overlap, crop - and rtl_power presets are exactly as before.
    A preset the parser cannot read is still skipped rather than fatal."""
    from marsad.config import Station
    st = Station.load(ROOT)
    st.data["presets"] = {
        "LS": {"freq": "1241M:2148M:50k", "gain": 29.0, "integration": 30, "receiver": "xtr", "note": ""},
        "top": {"engine": "rtl_power_fftw", "freq": "2147M:2187M", "bins": 128, "rate": 2400000,
                "time": 1, "overlap": 25, "crop": 0.25,
                "gain": 29.0, "receiver": "xtr", "note": "above the ceiling"},
        "h1": {"engine": "rtl_power_fftw", "freq": "1420.4058M", "bins": 2048, "rate": 2400000,
               "time": 60, "overlap": 0, "gain": 49.6, "receiver": "v5", "note": "hydrogen line"},
        "bad": {"engine": "rtl_power_fftw", "freq": "not-a-range", "gain": 29.0, "receiver": "xtr", "note": ""},
        "odd": {"engine": "something_else", "freq": "1M:2M:1k", "gain": 29.0, "integration": 1, "receiver": "xtr", "note": ""},
    }
    cfg = live.builder_config(st)
    assert cfg["preset_order"][:3] == ["LS", "top", "h1"]
    assert "bad" not in cfg["presets"] and "odd" not in cfg["presets"]
    ls = cfg["presets"]["LS"]
    assert "engine" not in ls and (ls["lo"], ls["hi"], ls["bin"], ls["integ"]) == (1241.0, 2148.0, 50.0, 30)
    top = cfg["presets"]["top"]
    assert top["engine"] == "rtl_power_fftw" and top["centre"] is None
    assert (top["lo"], top["hi"], top["bins"], top["rate"], top["time"], top["overlap"], top["crop"]) == \
        (2147.0, 2187.0, 128, 2400000, 1.0, 25.0, 0.25)
    assert top["bin"] == 18.75 and top["rx"] == "xtr"
    h1 = cfg["presets"]["h1"]
    assert h1["centre"] == 1420.4058 and h1["lo"] == pytest.approx(1419.2058) and h1["hi"] == pytest.approx(1421.6058)
    assert h1["bins"] == 2048 and h1["time"] == 60.0 and h1["overlap"] == 0.0 and h1["crop"] == 0.25
    # the engine's measured overhead model travels for the page's geometry
    f = cfg["fftw"]
    assert f["retune_ms"] == 59.0 and f["read_ms"] == 16.0 and f["default_rate"] == 2400000


def test_launcher_honours_the_test_hooks(monkeypatch):
    """MARSAD_PORT moves it off 8731 and MARSAD_NO_BROWSER keeps the browser shut."""
    calls, opened = [], []
    monkeypatch.setenv("MARSAD_PORT", "8745")
    monkeypatch.setenv("MARSAD_NO_BROWSER", "1")
    monkeypatch.setattr(host, "port_in_use", lambda port, host_addr="127.0.0.1": False)
    monkeypatch.setattr(webapp, "viewer_answers", lambda port=8731: {"follow": True} if calls else None)
    monkeypatch.setattr(webapp, "listener_pid", lambda port=8731: 4242)
    monkeypatch.setattr(webapp, "launch_background", lambda station, port: calls.append(port) or _Proc())
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setattr(cli, "_linger", lambda *a, **k: None)
    assert cli.main([]) == 0
    assert calls == [8745] and opened == []
