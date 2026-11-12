"""Relay 028: the window never holds, re-creates or deletes a station folder.

Remove from list touches no files; a missing station is refused in words and never re-created; releasing a station
stops only a service the window started, only when nothing runs on it (rule 1), and never the home station's app on
8731. The real-process half (a real service, then rmtree straight after) is prototypes/check_028.py. No radio, no
service started here."""
from __future__ import annotations

import json

import pytest

from marsad_desktop import stations as ST


@pytest.fixture(autouse=True)
def appdata(tmp_path, monkeypatch):
    monkeypatch.setenv("MARSAD_APPDATA", str(tmp_path / "appdata"))


def station(tmp_path, name):
    f = tmp_path / name
    f.mkdir()
    (f / "station.json").write_text("{}", encoding="utf-8")
    return f


def test_remove_from_list_forgets_and_touches_no_file(tmp_path):
    a, b = station(tmp_path, "a"), station(tmp_path, "b")
    ST.remember(b)
    ST.remember(a)
    before = sorted(p.name for p in a.rglob("*"))
    assert ST.forget(a) is True
    d = ST.registry()
    assert [e["folder"] for e in d["stations"]] == [str(b.resolve())] and d["last"] is None
    assert sorted(p.name for p in a.rglob("*")) == before            # nothing in the folder changed
    assert ST.forget(a) is False


def test_a_missing_station_is_refused_in_words_and_never_created(tmp_path, monkeypatch):
    gone = tmp_path / "Stations" / "gone"
    monkeypatch.setattr(ST, "serving", lambda f: pytest.fail("must not look for a service"))
    with pytest.raises(ST.MissingStation, match="does not re-create"):
        ST.ensure_service_ex(gone)
    with pytest.raises(ST.MissingStation):
        ST._start_own(gone, 8750)
    assert not gone.exists() and not (tmp_path / "Stations").exists()
    half = tmp_path / "half"                                    # a folder left after a failed delete: no station.json
    (half / "logs").mkdir(parents=True)
    assert ST.is_missing(half)
    with pytest.raises(ST.MissingStation):
        ST.ensure_service_ex(half)
    assert sorted(p.name for p in half.rglob("*")) == ["logs"]


def test_show_in_explorer_opens_the_folder_or_the_nearest_one_that_exists(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(ST.subprocess, "Popen", lambda argv, **kw: opened.append(argv))
    a = station(tmp_path, "a")
    assert ST.show_in_explorer(a) == str(a)
    assert ST.show_in_explorer(tmp_path / "x" / "y") == str(tmp_path)
    assert not (tmp_path / "x").exists()


def test_release_stops_only_the_windows_own_idle_service(tmp_path, monkeypatch):
    a = station(tmp_path, "a")
    stopped = []
    monkeypatch.setattr(ST, "stop_listener", lambda port, wait_s=15.0: stopped.append(port) or True)
    monkeypatch.setattr(ST, "ping", lambda port, timeout=0.6: {"app": "marsad", "station": str(a)})
    monkeypatch.setattr(ST, "station_busy", lambda folder, port, d=None: [])
    r = ST.release_station("http://127.0.0.1:8752", a)
    assert not r["released"] and stopped == []                 # no window started it: no window log for that port
    (a / "logs").mkdir()
    (a / "logs" / "window_service_8752_20300101-000000.log").write_text("", encoding="utf-8")
    r = ST.release_station("http://127.0.0.1:8752", a)
    assert r["released"] and stopped == [8752]
    # the home station's app, a test viewer, anything else: left alone
    for port in (8731, 8745, 9000):
        r = ST.release_station(f"http://127.0.0.1:{port}", a)
        assert not r["released"] and stopped == [8752]
    # another station's service on one of the window's ports: left alone
    monkeypatch.setattr(ST, "ping", lambda port, timeout=0.6: {"app": "marsad", "station": str(tmp_path / "other")})
    assert not ST.release_station("http://127.0.0.1:8753", a)["released"] and stopped == [8752]


def test_release_never_stops_a_station_with_something_running(tmp_path, monkeypatch):
    """Rule 1: a running measurement or Probe-tab run keeps the service; the window says the folder stays in use."""
    a = station(tmp_path, "a")
    monkeypatch.setattr(ST, "stop_listener", lambda *a, **k: pytest.fail("must not stop a busy station's service"))
    monkeypatch.setattr(ST, "ping", lambda port, timeout=0.6: {"app": "marsad", "station": str(a)})
    monkeypatch.setattr(ST, "station_busy", lambda folder, port, d=None: ["a setup measurement (range)"])
    (a / "logs").mkdir()
    (a / "logs" / "window_service_8750_20300101-000000.log").write_text("", encoding="utf-8")
    r = ST.release_station("http://127.0.0.1:8750", a)
    assert not r["released"] and r["busy"] and "in use" in r["text"]


def test_the_service_log_handle_is_not_kept_by_the_window(tmp_path, monkeypatch):
    a = station(tmp_path, "a")
    handles = []

    class P:
        def __init__(self, argv, stdout=None, **kw):
            handles.append(stdout)
    monkeypatch.setattr(ST.subprocess, "Popen", P)
    ST._start_own(a, 8754)
    assert handles and handles[0].closed                       # the child has its own copy; the window's is closed
    assert json.loads((a / "station.json").read_text()) == {}
