"""Relay 022: Find my location (Windows' own location service, confirmed, rounded), the setup guide's "needs
re-checking", and the eight themes plus Original, each checked. Made-up stations in temporary folders, made-up
serials and made-up coordinates; Windows' location service is never asked here (its answer is replaced)."""
from __future__ import annotations

import json
import itertools

import pytest

from marsad import live, setup as S
from marsad.config import Station
from marsad_desktop import locate as L
from marsad_desktop import theme as T

FAKE = {"status": "ok", "lat": 12.345678, "lon": -45.678912, "accuracy": 50.0, "source": "WiFi"}   # made up
ALLOW = {"pc": "Allow", "account": "Allow", "desktop_apps": "Allow"}


# ---------------------------------------------------------------- 1. Find my location

@pytest.fixture
def windows(monkeypatch):
    """Windows' location API replaced: what it would answer, and how often it was asked."""
    state = {"answer": dict(FAKE), "asked": 0, "consent": dict(ALLOW)}

    def ask(timeout):
        state["asked"] += 1
        return state["answer"]
    monkeypatch.setattr(L, "_ask_windows", ask)
    monkeypatch.setattr(L, "consent", lambda: state["consent"])
    monkeypatch.setattr(L.sys, "platform", "win32")
    return state


def test_find_gives_windows_figures_and_accuracy(windows):
    d = L.find()
    assert d == {"ok": True, "lat": 12.345678, "lon": -45.678912, "accuracy_m": 50.0, "source": "WiFi"}
    assert L.accuracy_words(50) == "± 50 m" and L.accuracy_words(2500) == "± 2.5 km"
    assert L.accuracy_words(None) == "accuracy not reported"


def test_round_to_about_1_km_is_two_decimals():
    assert L.rounded(12.345678, True) == 12.35 and L.rounded(-45.678912, True) == -45.68
    assert L.rounded(12.345678, False) == 12.345678


@pytest.mark.parametrize("switch,words", [("pc", "for this PC"), ("account", "for your Windows account"),
                                          ("desktop_apps", "desktop apps")])
def test_switched_off_says_which_and_how_and_never_asks(windows, switch, words):
    windows["consent"][switch] = "Deny"
    d = L.find()
    assert d["ok"] is False and d["reason"] == "off" and words in d["text"]
    assert "Privacy & security > Location" in d["text"] and "does not change Windows settings" in d["text"]
    assert "by hand always works" in d["text"]
    assert windows["asked"] == 0                       # Windows is not asked while its switch is off


@pytest.mark.parametrize("answer,reason", [({"status": "denied", "access": "Denied"}, "denied"),
                                           ({"status": "timeout"}, "timeout"),
                                           ({"status": "error", "error": "x"}, "unavailable"),
                                           ({"status": "ok", "lat": 200, "lon": 0}, "no fix"),
                                           ({"status": "ok"}, "no fix")])
def test_every_other_failure_is_said_in_words(windows, answer, reason):
    windows["answer"] = answer
    d = L.find()
    assert d["ok"] is False and d["reason"] == reason and d["text"]


def test_find_writes_nothing_and_uses_no_outside_service(windows, tmp_path, monkeypatch):
    """No file is opened for writing while finding; the source holds no web address (no IP lookup)."""
    import builtins
    opened = []
    real_open = builtins.open
    monkeypatch.setattr(builtins, "open", lambda f, mode="r", *a, **k: opened.append((f, mode)) or real_open(f, mode, *a, **k))
    L.find()
    assert not [o for o in opened if any(c in str(o[1]) for c in "wax+")]
    src = (L.__file__ and open(L.__file__, encoding="utf-8").read()).lower()
    assert "http" not in src and "urllib" not in src and "socket" not in src


def test_the_powershell_side_asks_windows_own_geolocator_only():
    assert "Windows.Devices.Geolocation.Geolocator" in L._PS and "RequestAccessAsync" in L._PS
    assert "Set-ItemProperty" not in L._PS and "New-ItemProperty" not in L._PS      # never changes a setting


# ---------------------------------------------------------------- 2. the guide: what rests on what

def new_station(tmp_path) -> Station:
    folder = tmp_path / "Stations" / "Bench"
    S.create_station(folder, "Bench", "a made-up test station")
    return Station.load(folder)


def done(st, nick, step, chain="", **extra):
    S.record(st, nick, dict({"at": S.now(), "step": step, "ok": True, "short": False,
                             "basis": S.basis(st, nick, chain)}, **extra))


def states(st, nick, chain):
    c = S.checklist(st, nick, chain, set())
    out = {s["step"]: s["state"] for s in c["steps"]}
    out.update({sub["step"]: sub["state"] for s in c["steps"] for sub in s["sub"]})
    return out


def ready_station(tmp_path):
    st = new_station(tmp_path)
    S.add_radio(st, "90000001", "e", "e4000")
    st = Station.load(st.root)
    done(st, "e", "range", range_mhz=[52.0, 2186.0], gaps_mhz=[[1094.0, 1239.0]])
    st = Station.load(st.root)
    for step in ("warmup", "retune", "floor"):
        done(st, "e", step)
    S.confirm_rig(st, "e", "ant > e")
    for step in ("adsb", "baseline"):
        done(st, "e", step, "ant > e")
    return Station.load(st.root)


def test_the_guide_order_puts_warmup_straight_after_the_range():
    assert S.MEASURES == ["range", "warmup", "retune", "floor"]
    assert set(S.DEPENDS) == set(S.MEASURES + S.PATHS)


def test_a_finished_station_is_ready(tmp_path):
    st = ready_station(tmp_path)
    assert set(states(st, "e", "ant > e").values()) == {"done"}
    assert S.lock_reason(st, "e", "ant > e") is None


def test_changing_the_radios_range_rechecks_only_what_runs_on_its_frequencies(tmp_path):
    st = ready_station(tmp_path)
    S.update_radio(st, "e", range_mhz=[52, 1766])
    st = Station.load(st.root)
    s = states(st, "e", "ant > e")
    assert s["range"] == s["warmup"] == s["floor"] == s["baseline"] == "needs re-checking"
    assert s["retune"] == s["adsb"] == s["confirm"] == "done"
    assert s["measure"] == "needs re-checking"
    why = S.lock_reason(st, "e", "ant > e")
    assert why and "step 2, Measure the radio" in why                  # locked again until re-run
    c = S.checklist(st, "e", "ant > e", set())
    detail = next(x["detail"] for s_ in c["steps"] for x in s_["sub"] if x["step"] == "warmup")
    assert "changed since this was done" in detail


def test_a_small_gap_change_that_leaves_the_tile_alone_rechecks_only_the_range(tmp_path):
    st = ready_station(tmp_path)
    S.update_radio(st, "e", gaps_mhz=[[1090, 1235]])         # warm-dongle edges: the 100 MHz tile does not move
    st = Station.load(st.root)
    s = states(st, "e", "ant > e")
    assert s["range"] == "needs re-checking"                   # its own figures were changed by hand
    assert s["warmup"] == s["floor"] == s["baseline"] == "done"


def test_changing_the_rig_rechecks_the_confirmation_and_both_path_checks(tmp_path):
    st = ready_station(tmp_path)
    s = states(st, "e", "ant > lna > e")
    assert s["confirm"] == s["adsb"] == s["baseline"] == "needs re-checking"
    assert s["range"] == s["warmup"] == s["retune"] == s["floor"] == "done"


def test_a_new_serial_or_tuner_rechecks_everything_after_it(tmp_path):
    st = ready_station(tmp_path)
    S.update_radio(st, "e", serial="90000009")
    st = Station.load(st.root)
    s = states(st, "e", "ant > e")
    assert all(s[k] == "needs re-checking" for k in S.MEASURES + S.PATHS)
    assert st.data["recorder"]["serials"]["e"] == "90000009"


def test_running_it_again_clears_it(tmp_path):
    st = ready_station(tmp_path)
    S.update_radio(st, "e", range_mhz=[52, 1766])
    st = Station.load(st.root)
    # measured again (relay 024: keeping an estimate would leave the step "estimate kept", never done)
    done(st, "e", "range", range_mhz=[52.0, 1766.0], gaps_mhz=[])
    st = Station.load(st.root)
    for step in ("warmup", "floor"):
        done(st, "e", step)
    done(st, "e", "baseline", "ant > e")
    st = Station.load(st.root)
    assert set(states(st, "e", "ant > e").values()) == {"done"}


def test_results_from_before_relay_022_are_taken_as_they_are(tmp_path):
    st = new_station(tmp_path)
    S.add_radio(st, "90000001", "e", "e4000")
    st = Station.load(st.root)
    S.record(st, "e", {"at": S.now(), "step": "retune", "ok": True, "short": False})      # no basis
    S.update_radio(st, "e", serial="90000009")
    st = Station.load(st.root)
    assert states(st, "e", "")["retune"] == "done"


def test_a_run_records_what_it_rested_on(tmp_path):
    st = ready_station(tmp_path)
    job = {"step": "retune", "log": str(tmp_path / "x.log"), "basis": S.basis(st, "e", "ant > e")}
    (tmp_path / "x.log").write_text("  retune per hop        47.3 ms\n")
    res = S.result_of(st, job, {"exit_code": 0})
    assert res["basis"] == job["basis"] and res["ok"]


def test_update_radio_refuses_a_serial_taken_by_another_radio(tmp_path):
    st = new_station(tmp_path)
    S.add_radio(st, "90000001", "e", "e4000")
    S.add_radio(Station.load(st.root), "90000002", "r", "r820t")
    with pytest.raises(ValueError, match="already this station's e"):
        S.update_radio(Station.load(st.root), "r", serial="90000001")


def test_the_service_refuses_to_change_a_radio_while_its_step_runs(tmp_path, monkeypatch):
    st = ready_station(tmp_path)
    monkeypatch.setattr(live, "setup_running", lambda st_: {("e", "warmup")})
    with pytest.raises(ValueError, match="running"):
        live._setup_post(st, "/setup/radio_update", {"radio": "e", "range_mhz": [52, 1766]})
    assert json.loads(st.file.read_text())["receivers"]["e"]["range_mhz"] == [52.0, 2186.0]


# ---------------------------------------------------------------- 3. themes

NEW = [n for n in T.THEMES if n != "Original"]


def test_eight_new_themes_plus_original_with_design_system_names():
    assert len(NEW) == 8 and list(T.THEMES)[0] == "Original"
    for name, d in T.THEMES.items():
        assert set(T.TOKENS) <= set(d), name                # the Marsad Design System's names
        assert all(T.is_hex(d[k]) for k in T.TOKENS), name
    assert {k: v for k, v in T.THEMES["Original"].items() if k != "note"} == T.BASE     # Original unchanged


@pytest.mark.parametrize("name", list(T.THEMES))
def test_every_theme_passes_every_check(name):
    assert T.problems(T.palette(name), name) == []


@pytest.mark.parametrize("name", NEW)
def test_survey_and_recorder_differ_in_lightness_in_every_new_theme(name):
    p = T.palette(name)
    assert abs(T.lightness(p["survey"]) - T.lightness(p["recorder"])) >= T.SURVEY_RECORDER_L
    assert "survey_recorder_lightness" not in T.THEME_EXEMPT.get(name, {})


@pytest.mark.parametrize("name", list(T.THEMES))
def test_meanings_stay_distinct_and_depth_keeps_its_steps(name):
    p = T.palette(name)
    for a, b in itertools.combinations(T.MEANINGS, 2):
        assert T.delta_e(p[a], p[b]) >= T.MEANING_DE, (name, a, b)
    lum = [T.luminance(p["elev_" + k]) for k in T.ELEVATIONS]
    assert lum == sorted(lum) and len(set(lum)) == 4, name


def test_the_checks_catch_a_bad_palette():
    bad = dict(T.BASE, muted="#30343A", recorder=T.BASE["survey"], private=T.BASE["open"])
    probs = T.problems(T._derive(bad), "bad")
    assert any("text muted" in p for p in probs) and any("survey and recorder" in p for p in probs)
    assert any("open and private" in p for p in probs)


def test_use_switches_the_palette_in_place_and_back():
    c = T.C
    before = dict(c)
    try:
        assert T.use("Phosphor") == "Phosphor" and T.C is c and c["survey"] == T.THEMES["Phosphor"]["survey"]
        assert T.current() == "Phosphor" and T.stylesheet() == T.stylesheet(T.palette("Phosphor"))
        assert T.text_on("#000000")[0] in (c["text"], T.DARK_TEXT)
    finally:
        T.use("Original")
    assert dict(T.C) == before and T.use("no such theme") == "Original"


def test_a_preview_never_touches_the_window_palette():
    before = dict(T.C)
    sheet = T.stylesheet(T.palette("High contrast"))
    assert T.THEMES["High contrast"]["background"].lower() in sheet.lower() and dict(T.C) == before


def test_the_waterfall_colour_map_is_not_part_of_any_theme():
    for d in T.THEMES.values():
        assert not any("cmap" in k or "waterfall" in k for k in d)
