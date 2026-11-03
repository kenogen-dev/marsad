"""The Chain tab: drawings in, the recorded chain text out. Staleness must come only
from a change to the parts.
"""
import json
import sys

import pytest

from conftest import ROOT

sys.path.insert(0, str(ROOT))

from marsad import chain, probe              # noqa: E402
from marsad.config import Station            # noqa: E402


def _station(tmp_path):
    (tmp_path / "station.json").write_text(json.dumps({"defaults": {"chain_order": "old"}}),
                                           encoding="utf-8")
    return Station.load(tmp_path)


def test_seeds_are_a_plain_rig_on_the_given_receiver():
    d = chain.seed_chains("rx1")
    assert chain.chain_text(d["chains"]["antenna"]["parts"]) == "antenna > Coax > rx1 > USB cable > PC"
    assert chain.tail_text(d["chains"]["probe"]["parts"]) == "SMA adapter > SMA DC block > Coax > rx1 > USB cable > PC"
    assert chain.receiver_of(d["chains"]["probe"]["parts"]) == "rx1"
    assert chain.part_text({"type": "attenuator", "name": "SMA attenuator", "db": 10}) == "10 dB SMA attenuator"


def test_chain_labels_set_the_standard_names(tmp_path):
    (tmp_path / "station.json").write_text(json.dumps({"chain_labels": {"lna": "Mast LNA"}}), encoding="utf-8")
    st = Station.load(tmp_path)
    assert chain.part_types(st)["lna"]["label"] == "Mast LNA"
    saved = chain.save(st, {"chains": {"x": {"parts": [{"type": "custom", "name": "lna"}]}}})
    assert saved["chains"]["x"]["parts"][0]["name"] == "Mast LNA"
    assert chain.summary(st)["receivers"] == ["r820t", "e4000"]


def test_load_seeds_once_and_save_stamps_only_changed_chains(tmp_path):
    st = _station(tmp_path)
    d = chain.load(st)
    assert (tmp_path / "chain.json").exists() and d["current"] == "antenna"
    stamp = d["chains"]["antenna"]["updated"]
    # mark a run on it, then save an unrelated edit: the antenna chain keeps its stamps
    chain.mark_used(st)
    d = chain.load(st)
    assert d["chains"]["antenna"]["last_used"]
    assert chain.needs_confirm(d["chains"]["antenna"])
    d["chains"]["probe"]["parts"].insert(3, {"type": "attenuator", "name": "pad", "db": 6})
    saved = chain.save(st, d)
    assert saved["chains"]["antenna"]["updated"] == stamp
    assert saved["chains"]["antenna"]["last_used"] == d["chains"]["antenna"]["last_used"]
    assert saved["chains"]["probe"]["updated"] >= stamp
    assert "> 6 dB SMA attenuator >" in chain.tail_text(saved["chains"]["probe"]["parts"])
    # a change to the used chain clears the question; static silences it regardless
    d = chain.load(st)
    d["chains"]["antenna"]["parts"][0]["detail"] = "elements now 75 mm"
    saved = chain.save(st, d)
    assert not chain.needs_confirm(saved["chains"]["antenna"])
    chain.mark_used(st)
    d = chain.load(st)
    assert chain.needs_confirm(d["chains"]["antenna"])
    d["chains"]["antenna"]["static"] = True
    assert not chain.needs_confirm(chain.save(st, d)["chains"]["antenna"])


def test_use_writes_the_station_default(tmp_path):
    st = _station(tmp_path)
    d = chain.use(st, "probe")
    assert d["current"] == "probe"
    on_disk = json.loads((tmp_path / "station.json").read_text(encoding="utf-8"))
    assert on_disk["defaults"]["chain_order"].startswith("near-field probe (held where the Probe tab says) > SMA adapter > ")
    with pytest.raises(ValueError, match="no chain"):
        chain.use(st, "nope")


def test_hand_typed_custom_parts_become_canonical():
    out = chain.validate({"chains": {"x": {"parts": [
        {"type": "custom", "name": "Notch", "detail": "input side"},
        {"type": "custom", "name": "10dB Attenuator"},
        {"type": "attenuator", "name": "6 dB pad"},
        {"type": "custom", "name": "mystery box"},
        {"type": "bulkhead_sma", "name": "box SMA bulkhead"},
    ]}}})["chains"]["x"]["parts"]
    assert chain.chain_text(out) == ("FM notch (input side) > 10 dB SMA attenuator > "
                                     "6 dB SMA attenuator > mystery box > box SMA bulkhead")
    assert out[0]["type"] == "filter" and out[1] == {"type": "attenuator", "name": "SMA attenuator",
                                                     "detail": "", "db": 10}


def test_validate_refuses_unknown_parts():
    with pytest.raises(ValueError, match="unknown type"):
        chain.validate({"chains": {"x": {"parts": [{"type": "warp-drive"}]}}})
    with pytest.raises(ValueError, match="chains"):
        chain.validate([])
    ok = chain.validate({"current": "zzz", "chains": {"a": {"parts": [{"type": "pc", "name": "PC", "db": ""}]}}})
    assert ok["current"] == "a" and ok["chains"]["a"]["parts"] == [{"type": "pc", "name": "PC", "detail": ""}]
