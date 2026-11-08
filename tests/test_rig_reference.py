"""Relay 012 section 3: every run records a rig reference (the drawn rig, its path, the drawing's
version) beside its chain text, outside the fingerprint; rig history keeps every version a run
can name, through later Saves, archiving and renaming. Made-up station; no radio, no Qt."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from marsad import chain, rtlpower
from marsad.config import Station
from marsad_desktop import rig as R

DATA = Path(__file__).parent / "data"


@pytest.fixture
def station(tmp_path):
    root = tmp_path / "st"
    root.mkdir()
    shutil.copy(DATA / "builder_station.json", root / "station.json")
    (root / "surveys").mkdir()
    return Station.load(str(root))


PARTS = [{"type": "antenna", "name": "Made-up whip"}, {"type": "coax", "name": "Coax", "length": "1 m"},
         {"type": "sdr", "name": "radio-a", "rx": "radio-a"}, {"type": "usb", "name": "USB"},
         {"type": "pc", "name": "PC"}]


def save_rig(station, name, parts, doc=None, lay=None):
    """One Save, the way the Chain screen does it (rig.apply_rig), written to both files."""
    doc = doc or {"schema": 1, "current": None, "chains": {}}
    lay = lay or R.empty_layout()
    doc, lay = R.apply_rig(doc, lay, name, R.rig_from_parts(parts))
    (station.root / "chain.json").write_text(json.dumps(doc))
    (station.root / chain.LAYOUT_FILE).write_text(json.dumps(lay, indent=1))
    return doc, lay


def text_of(lay, name):
    return next(iter(lay["rigs"][name]["chains"].values()))


def test_a_run_from_a_drawn_rig_records_its_reference(station):
    doc, lay = save_rig(station, "Window rig", PARTS)
    ref = chain.rig_reference(station, text_of(lay, "Window rig"))
    assert ref["rig"] == "Window rig" and ref["path"] == "Window rig" and len(ref["version"]) == 12
    assert ref["version"] == chain.drawing_version(lay["rigs"]["Window rig"])


def test_a_typed_or_old_chain_records_none_and_says_why(station):
    assert chain.rig_reference(station, "a > b > c")["rig"] is None                  # no layout at all
    save_rig(station, "Window rig", PARTS)
    ref = chain.rig_reference(station, "typed antenna > radio-a > PC")
    assert ref["rig"] is None and "--chain" in ref["why"]
    assert chain.rig_reference_rows({"rig_reference": ref})[0].startswith("| Rig drawing | none recorded")
    assert chain.rig_reference_rows({}) == []                                        # old runs: no row


def test_every_version_a_run_named_is_kept_through_saves_archive_and_rename(station):
    doc, lay = save_rig(station, "Window rig", PARTS)
    v1 = chain.rig_reference(station, text_of(lay, "Window rig"))["version"]
    parts2 = PARTS[:1] + [{"type": "lna", "name": "LNA"}] + PARTS[1:]
    doc, lay = save_rig(station, "Window rig", parts2, doc, lay)
    v2 = chain.rig_reference(station, text_of(lay, "Window rig"))["version"]
    assert v1 != v2
    lay = R.set_archived(doc, lay, "Window rig", R.rig_from_parts(parts2), True)
    doc, lay = R.apply_rig(doc, lay, "Roof rig", R.rig_from_parts(parts2), renamed_from="Window rig")
    found1 = chain.find_drawing(lay, v1, "Window rig")
    found2 = chain.find_drawing(lay, v2, "Window rig")
    assert found1 and found1["where"] == "history" and found1["rig"] == "Roof rig"
    assert found2 and found2["rig"] == "Roof rig"
    assert chain.drawing_version(found1["drawing"]) == v1
    assert chain.find_drawing(lay, "000000000000") is None


def test_an_archived_rig_is_named_only_when_no_current_one_wrote_the_text(station):
    doc, lay = save_rig(station, "Old rig", PARTS)
    lay = R.set_archived(doc, lay, "Old rig", R.rig_from_parts(PARTS), True)
    (station.root / chain.LAYOUT_FILE).write_text(json.dumps(lay))
    ref = chain.rig_reference(station, text_of(lay, "Old rig"))
    assert ref["rig"] == "Old rig" and ref.get("archived") is True


def test_version_ignores_bookkeeping_but_not_the_drawing():
    r = R.rig_from_parts(PARTS)
    base = chain.drawing_version(r)
    assert chain.drawing_version({**r, "chains": {"x": "y"}, "archived": True, "history": [1]}) == base
    assert chain.drawing_version({**r, "groups": []}) == base                          # empty extras are not kept
    moved = json.loads(json.dumps(r))
    moved["nodes"][0]["x"] = moved["nodes"][0].get("x", 0) + 40
    assert chain.drawing_version(moved) != base


def test_the_reference_leaves_fingerprint_and_chain_text_unchanged(station):
    doc, lay = save_rig(station, "Window rig", PARTS)
    text = text_of(lay, "Window rig")
    side = {"rtl_power": {"freq_low_mhz": 300.0, "freq_high_mhz": 420.0, "bin_khz": 50, "gain_db": 29,
                          "integration_s": 30, "crop": 0.25, "fir": 9, "tuner": "e4000"},
            "receiver": "radio-a", "chain": {"chain_order": text}}
    with_ref = dict(side, rig_reference=chain.rig_reference(station, text))
    assert rtlpower.fingerprint(side, 43.9) == rtlpower.fingerprint(with_ref, 43.9)
    assert with_ref["chain"]["chain_order"] == text
