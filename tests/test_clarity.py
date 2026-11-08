"""Relay 011, first-time clarity: the run's real timeline, where each default comes from, and the
explanations file (every key the window asks for exists, each with a tip and a comparability word).
Made-up station; no radio, no Qt."""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from marsad import live
from marsad.config import Station

DATA = Path(__file__).parent / "data"
DESKTOP = Path(__file__).resolve().parent.parent / "marsad_desktop"


@pytest.fixture
def station(tmp_path, monkeypatch):
    root = tmp_path / "st"
    root.mkdir()
    shutil.copy(DATA / "builder_station.json", root / "station.json")
    (root / "surveys").mkdir()
    monkeypatch.setattr(live.host, "running_sdr_programs", lambda: [])
    return Station.load(str(root))


def plan(station, **over):
    s = dict(live.scan_from_preset(station, "band1"), **over)
    return live.scan_plan(station, {"scan": s})


def test_timeline_warmup_is_inside_the_duration_in_whole_sweeps(station):
    t = plan(station, durVal=1, unit="h", integ=30, useskip=True, skipmin=16)["timeline"]
    assert t["total_s"] == 3600 and t["skipped"] == 32 and t["warm_s"] == 960 and t["analysed_s"] == 2640
    assert "are warm-up" in t["text"] and "not added to it" in t["text"] and t["level"] == "ok"


def test_timeline_rounds_warmup_up_to_a_whole_sweep(station):
    t = plan(station, durVal=1, unit="h", integ=120, useskip=True, skipmin=5)["timeline"]
    assert t["skipped"] == 3 and t["warm_s"] == 360               # 5 min at 2 min a sweep is 3 sweeps


def test_timeline_warns_when_the_whole_run_is_warmup(station):
    t = plan(station, durVal=10, unit="m", useskip=True, skipmin=16)["timeline"]
    assert t["level"] == "bad" and "whole run would be warm-up" in t["text"]


def test_timeline_without_analysis(station):
    t = plan(station, analyze=False)["timeline"]
    assert t["analysed"] is False and "Analysis is off" in t["text"]


def test_the_plan_is_the_same_command_as_before(station):
    """The timeline only adds a field: the command for the same settings is unchanged."""
    d = plan(station)
    assert d["line"].startswith("marsad run band1") and "timeline" in d


def test_default_sources_say_where_each_comes_from(station):
    raw = json.loads(station.file.read_text())
    raw["retune_ms_per_hop"] = 50
    raw["_retune_ms_per_hop"] = "a made-up note"
    station.file.write_text(json.dumps(raw))
    st = Station.load(str(station.root))
    items = live.default_sources(st, "radio-a")["items"]
    assert items["retune"]["where"] == "station" and items["retune"]["note"] == "a made-up note"
    assert items["retune"]["value"] == "50 ms per hop" and "calibrate-retune" in items["retune"]["measure"]
    assert items["warmup"]["where"] == "window" and items["warmup"]["generic"]
    assert "not measured" in items["warmup"]["text"] and "marsad drift" in items["warmup"]["measure"]
    assert items["gap"]["value"] == "1100–1250 MHz"
    assert items["retune"]["scope"] == "station" and items["gap"]["scope"] == "radio"


def test_default_sources_never_read_the_location_file(station, monkeypatch):
    (station.root / "station.local.json").write_text("{not json at all")
    opened = []
    real = Path.read_text

    def spy(self, *a, **k):
        opened.append(self.name)
        return real(self, *a, **k)
    monkeypatch.setattr(Path, "read_text", spy)
    live.default_sources(station, "radio-a")
    assert "station.local.json" not in opened


# ---- the explanations file

def explanations() -> dict:
    out, key = {}, None
    for ln in (DESKTOP / "explanations.md").read_text(encoding="utf-8").splitlines():
        m = re.match(r"^##\s+(\S+)\s*$", ln)
        if m:
            key = m.group(1)
            out[key] = {}
        elif re.match(r"^#\s", ln):
            key = None
        elif key:
            m = re.match(r"^(tip|locks|default):\s*(.*)$", ln)
            if m and m.group(1) not in out[key]:
                out[key][m.group(1)] = m.group(2).strip()
    return out


def test_every_key_the_window_asks_for_is_explained():
    texts = explanations()
    used = set()
    for f in DESKTOP.glob("*.py"):
        src = f.read_text(encoding="utf-8")
        used |= set(re.findall(r'X\.(?:attach|info|titled|with_info)\([^()]*?"([a-z_]+)"\s*[,)]', src))
        used |= set(re.findall(r'key="([a-z_]+)"', src))
        if f.name == "scan_start.py":
            used |= set(re.findall(r'"(cell_[a-z_]+)"', src))
            used |= set(re.findall(r'"([a-z_]+)"\}?\[?', re.search(r"okeys = \{(.*?)\}", src, re.S).group(1)))
    used -= {"keep_min_snr", "capture", "threshold", "cooldown", "passes", "ambush", "recorder_gain"} - set(texts)
    missing = sorted(k for k in used if k not in texts)
    assert not missing, f"keys with no explanation: {missing}"
    assert len(used) >= 60


def test_every_explanation_has_a_tip_and_a_comparability_word():
    texts = explanations()
    assert len(texts) >= 60
    for k, t in texts.items():
        assert t.get("tip"), k
        assert t.get("locks") in ("yes", "no", "n/a"), k


def test_defaults_named_in_explanations_exist(station):
    items = live.default_sources(station, "radio-a")["items"]
    for k, t in explanations().items():
        if t.get("default"):
            assert t["default"] in items, k
