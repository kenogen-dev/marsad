"""The Chain tab: drawings in, the recorded chain text out.

The seeds must reproduce the chain text the station has been recording, word for
word, and staleness must come only from a change to the parts.
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


ANTENNA_CHAIN = (
    "antenna (90 deg L dipole, 60 mm each whip, suction-mounted on the inside face of the window "
    "glass) > Flamingo+ FM notch > LaNA (at the window, on the far end of the coax, powered by the "
    "XTR bias-tee through the coax) > 3 ft RG316 coax > SMA bulkhead > 6 in SMA jumper (S-bend, no "
    "ferrite) > XTR (in aluminium enclosure, plugged direct into USB bulkhead, no internal USB "
    "cable) > USB bulkhead > 1 ft USB3 M:M > 16 ft USB3 > USB isolator > PC"
)


def test_seeds_reproduce_the_recorded_chain_text():
    d = chain.seed_chains()
    # pinned text, not station.json: "Use for runs" rewrites that file by design
    assert chain.chain_text(d["chains"]["antenna-xtr"]["parts"]) == ANTENNA_CHAIN
    assert chain.tail_text(d["chains"]["probe-xtr"]["parts"]) == probe.PROBE_CHAIN_TAIL["xtr"]
    assert chain.tail_text(d["chains"]["probe-v5"]["parts"]) == probe.PROBE_CHAIN_TAIL["v5"]
    assert chain.receiver_of(d["chains"]["probe-v5"]["parts"]) == "v5"
    assert chain.part_text({"type": "attenuator", "name": "SMA attenuator", "db": 10}) == "10 dB SMA attenuator"


def test_load_seeds_once_and_save_stamps_only_changed_chains(tmp_path):
    st = _station(tmp_path)
    d = chain.load(st)
    assert (tmp_path / "chain.json").exists() and d["current"] == "antenna-xtr"
    stamp = d["chains"]["antenna-xtr"]["updated"]
    # mark a run on it, then save an unrelated edit: the antenna chain keeps its stamps
    chain.mark_used(st)
    d = chain.load(st)
    assert d["chains"]["antenna-xtr"]["last_used"]
    assert chain.needs_confirm(d["chains"]["antenna-xtr"])
    d["chains"]["probe-xtr"]["parts"].insert(5, {"type": "attenuator", "name": "pad", "db": 6})
    saved = chain.save(st, d)
    assert saved["chains"]["antenna-xtr"]["updated"] == stamp
    assert saved["chains"]["antenna-xtr"]["last_used"] == d["chains"]["antenna-xtr"]["last_used"]
    assert saved["chains"]["probe-xtr"]["updated"] >= stamp
    assert "> 6 dB SMA attenuator > LaNA (" in chain.tail_text(saved["chains"]["probe-xtr"]["parts"])
    # a change to the used chain clears the question; static silences it regardless
    d = chain.load(st)
    d["chains"]["antenna-xtr"]["parts"][0]["detail"] = "whips now 75 mm"
    saved = chain.save(st, d)
    assert not chain.needs_confirm(saved["chains"]["antenna-xtr"])
    chain.mark_used(st)
    d = chain.load(st)
    assert chain.needs_confirm(d["chains"]["antenna-xtr"])
    d["chains"]["antenna-xtr"]["static"] = True
    assert not chain.needs_confirm(chain.save(st, d)["chains"]["antenna-xtr"])


def test_use_writes_the_station_default(tmp_path):
    st = _station(tmp_path)
    d = chain.use(st, "probe-xtr")
    assert d["current"] == "probe-xtr"
    on_disk = json.loads((tmp_path / "station.json").read_text(encoding="utf-8"))
    assert on_disk["defaults"]["chain_order"].startswith("near-field probe (held where the Probe tab says) > SMA adapter > ")
    with pytest.raises(ValueError, match="no chain"):
        chain.use(st, "nope")


def test_hand_typed_custom_parts_become_canonical():
    out = chain.validate({"chains": {"x": {"parts": [
        {"type": "custom", "name": "Notch", "detail": "at the window"},
        {"type": "custom", "name": "10dB Attenuator"},
        {"type": "attenuator", "name": "6 dB pad"},
        {"type": "custom", "name": "mystery box"},
        {"type": "bulkhead_sma", "name": "cage SMA bulkhead"},
    ]}}})["chains"]["x"]["parts"]
    assert chain.chain_text(out) == ("Flamingo+ FM notch (at the window) > 10 dB SMA attenuator > "
                                     "6 dB SMA attenuator > mystery box > cage SMA bulkhead")
    assert out[0]["type"] == "filter" and out[1] == {"type": "attenuator", "name": "SMA attenuator",
                                                     "detail": "", "db": 10}


def test_validate_refuses_unknown_parts():
    with pytest.raises(ValueError, match="unknown type"):
        chain.validate({"chains": {"x": {"parts": [{"type": "warp-drive"}]}}})
    with pytest.raises(ValueError, match="chains"):
        chain.validate([])
    ok = chain.validate({"current": "zzz", "chains": {"a": {"parts": [{"type": "pc", "name": "PC", "db": ""}]}}})
    assert ok["current"] == "a" and ok["chains"]["a"]["parts"] == [{"type": "pc", "name": "PC", "detail": ""}]
