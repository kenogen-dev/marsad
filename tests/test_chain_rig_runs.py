"""A split rig used for runs: every run records the path ending at its own radio.

"Use for runs" on a rig with two radios makes the whole rig current. Each run then
records the path that ends at the radio it uses, matched by serial; a radio with no
path in the rig is refused before anything starts; a typed --chain still wins; and a
one-path rig (or anything the web page makes current) behaves exactly as before.
All synthetic: no station folder, no radio.
"""
import argparse
import json
import subprocess
import sys

import pytest

from conftest import PY, ROOT

sys.path.insert(0, str(ROOT))

from marsad import chain, record             # noqa: E402
from marsad.config import Station            # noqa: E402

RX = {"radio-a": {"label": "radio A", "tuner": "r820t", "range_mhz": [52, 2200], "gaps_mhz": [], "device": 0},
      "radio-b": {"label": "radio B", "tuner": "r820t", "range_mhz": [24, 1766], "gaps_mhz": [], "device": 1},
      "radio-c": {"label": "radio C", "tuner": "r820t", "range_mhz": [24, 1766], "gaps_mhz": [], "device": 2}}


def _p(type_, name, detail="", **kw):
    return {"type": type_, "name": name, "detail": detail, **kw}


HEAD = [_p("antenna", "whip", "test"), _p("lna", "LNA"), _p("custom", "splitter", "out A")]
PATH_A = HEAD + [_p("sdr", "radio-a", rx="radio-a"), _p("pc", "PC")]
PATH_B = HEAD[:2] + [_p("custom", "splitter", "out B"), _p("filter", "notch"),
                     _p("sdr", "radio-b", rx="radio-b"), _p("pc", "PC")]
ONE = [_p("antenna", "whip", "test"), _p("sdr", "radio-a", rx="radio-a"), _p("pc", "PC")]


def _station(tmp_path, serials=True):
    data = {"receivers": RX, "default_receiver": "radio-a",
            "defaults": {"chain_order": "old text", "chain_used_at": None}}
    if serials:
        data["recorder"] = {"serials": {"radio-a": "00000001", "radio-b": "00000002", "radio-c": "00000003"},
                            "roles": {"surveyor": "radio-a", "recorder": "radio-b"}}
    (tmp_path / "station.json").write_text(json.dumps(data), encoding="utf-8")
    st = Station.load(tmp_path)
    d = chain.load(st)
    mk = lambda parts: {"static": False, "updated": None, "last_used": None, "parts": parts}
    d["chains"] = {"rig (radio-a)": mk(PATH_A), "rig (radio-b)": mk(PATH_B), "single": mk(ONE)}
    chain.save(st, d)
    return Station.load(tmp_path)


def _ns(**kw):
    return argparse.Namespace(**{"chain": None, "chain_as_saved": False, "non_interactive": True, **kw})


def test_use_rig_files_each_path_under_its_radios_serial(tmp_path):
    st = _station(tmp_path)
    chain.use(st, "rig (radio-a)", rig=["rig (radio-a)", "rig (radio-b)"])
    st = Station.load(tmp_path)
    by = st.defaults["chain_by_serial"]
    assert by == {"00000001": chain.chain_text(PATH_A), "00000002": chain.chain_text(PATH_B)}
    assert chain.rig_text_for(st, "radio-a") == chain.chain_text(PATH_A)
    assert chain.rig_text_for(st, "radio-b") == chain.chain_text(PATH_B)
    assert record.confirm_default_chain(st, _ns(), "radio-b") == chain.chain_text(PATH_B)
    assert chain.summary(st)["runs_record"] == by


def test_without_serials_the_radio_name_is_the_key(tmp_path):
    st = _station(tmp_path, serials=False)
    chain.use(st, "rig (radio-a)", rig=["rig (radio-a)", "rig (radio-b)"])
    st = Station.load(tmp_path)
    assert set(st.defaults["chain_by_serial"]) == {"receiver radio-a", "receiver radio-b"}
    assert chain.rig_text_for(st, "radio-b") == chain.chain_text(PATH_B)


def test_a_radio_with_no_path_is_refused_in_plain_words(tmp_path):
    st = _station(tmp_path)
    chain.use(st, "rig (radio-a)", rig=["rig (radio-a)", "rig (radio-b)"])
    st = Station.load(tmp_path)
    with pytest.raises(SystemExit) as e:
        record.confirm_default_chain(st, _ns(), "radio-c")
    msg = str(e.value)
    assert "no path ending at the radio-c radio" in msg and "00000003" in msg and "Nothing was recorded" in msg
    assert "NONE" in record.chain_line(st, _ns(), "radio-c", "saved default")


def test_a_typed_chain_still_wins(tmp_path):
    st = _station(tmp_path)
    chain.use(st, "rig (radio-a)", rig=["rig (radio-a)", "rig (radio-b)"])
    st = Station.load(tmp_path)
    assert record.confirm_default_chain(st, _ns(chain="typed"), "radio-c") == "typed"
    assert record.chain_line(st, _ns(chain="typed"), "radio-c", "--chain") == "(--chain) typed"


def test_a_run_on_either_radio_makes_the_rig_ask_again_unless_static(tmp_path):
    st = _station(tmp_path)
    chain.use(st, "rig (radio-a)", rig=["rig (radio-a)", "rig (radio-b)"])
    st = Station.load(tmp_path)
    st.save_defaults({"chain_used_at": "2031-01-01T00:00:00"})       # a run on radio-a
    with pytest.raises(SystemExit) as e:
        record.confirm_default_chain(st, _ns(), "radio-b")
    assert "--chain-as-saved" in str(e.value)
    assert record.confirm_default_chain(st, _ns(chain_as_saved=True), "radio-b") == chain.chain_text(PATH_B)
    d = chain.load(st)
    for n in ("rig (radio-a)", "rig (radio-b)"):
        d["chains"][n]["static"] = True
    chain.save(st, d)
    assert record.confirm_default_chain(st, _ns(), "radio-b") == chain.chain_text(PATH_B)


def test_two_paths_to_one_radio_or_a_path_with_no_radio_are_refused(tmp_path):
    st = _station(tmp_path)
    with pytest.raises(ValueError, match="same radio"):
        chain.use(st, "rig (radio-a)", rig=["rig (radio-a)", "single"])
    d = chain.load(st)
    d["chains"]["noradio"] = {"static": False, "updated": None, "last_used": None,
                              "parts": [_p("antenna", "whip"), _p("pc", "PC")]}
    chain.save(st, d)
    with pytest.raises(ValueError, match="names no radio"):
        chain.use(st, "rig (radio-a)", rig=["rig (radio-a)", "noradio"])


def test_one_path_is_exactly_as_before(tmp_path):
    """No rig: station.json gains no new key, and the text and its source are today's."""
    st = _station(tmp_path)
    chain.use(st, "single")
    raw = json.loads((tmp_path / "station.json").read_text(encoding="utf-8"))
    assert "chain_by_serial" not in raw["defaults"]
    st = Station.load(tmp_path)
    text = chain.chain_text(ONE)
    assert st.defaults["chain_order"] == text
    for rx in ("radio-a", "radio-b", "radio-c", None):
        assert chain.rig_text_for(st, rx) is None
        assert record.confirm_default_chain(st, _ns(), rx) == text
        assert record.chain_line(st, _ns(), rx, "saved default") == f"(saved default) {text}"


def test_using_one_path_after_a_rig_clears_the_rig(tmp_path):
    """The web page's Use for runs (one name, no rig) puts everything back as it was."""
    st = _station(tmp_path)
    chain.use(st, "rig (radio-a)", rig=["rig (radio-a)", "rig (radio-b)"])
    chain.use(Station.load(tmp_path), "single")
    st = Station.load(tmp_path)
    assert st.defaults.get("chain_by_serial") is None
    assert record.confirm_default_chain(st, _ns(), "radio-b") == chain.chain_text(ONE)


def _dry(tmp_path, *args):
    r = subprocess.run([PY, "-m", "marsad", "--station", str(tmp_path), *args],
                       cwd=str(ROOT), capture_output=True, text=True, timeout=120)
    return r.returncode, r.stdout + r.stderr


def test_dry_runs_name_each_radios_own_path(tmp_path):
    st = _station(tmp_path)
    chain.use(st, "rig (radio-a)", rig=["rig (radio-a)", "rig (radio-b)"])
    for rx, parts in (("radio-a", PATH_A), ("radio-b", PATH_B)):
        rc, out = _dry(tmp_path, "run", "custom", "1m", "--freq", "400M:410M:50k", "--receiver", rx,
                       "--non-interactive", "--no-live", "--dry-run")
        assert rc == 0, out
        assert f"path to the {rx} radio" in out and chain.chain_text(parts) in out
    rc, out = _dry(tmp_path, "run", "custom", "1m", "--freq", "400M:410M:50k", "--receiver", "radio-c",
                   "--non-interactive", "--no-live", "--dry-run")
    assert "no path ending at the radio-c radio" in out and "would refuse" in out
    rc, out = _dry(tmp_path, "watch", "fixed", "--freq", "400M", "--receiver", "radio-b",
                   "--duration", "1m", "--dry-run")
    assert rc == 0, out
    assert f"chain: {chain.chain_text(PATH_B)}" in out
    rc, out = _dry(tmp_path, "watch", "fixed", "--freq", "400M", "--receiver", "radio-c",
                   "--duration", "1m", "--dry-run")
    assert rc != 0 and "no path ending at the radio-c radio" in out
    rc, out = _dry(tmp_path, "watch", "survey", "custom", "1m", "--freq", "400M:410M:50k",
                   "--non-interactive", "--dry-run")
    assert rc == 0, out
    assert f"recorder chain: {chain.chain_text(PATH_B)}" in out
    assert chain.chain_text(PATH_A) in out.split("run: marsad run", 1)[1].splitlines()[0]
