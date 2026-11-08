"""Relay 017: cables are Coax or USB with a length; no Jumper, no Pigtail; chokes sit on cables.

A cable drawn from now on is written from its fields ("3 ft RG316 coax (with chokes)"). Every chain saved before
keeps its text byte for byte, and a drawing in the old words is only offered a conversion: nothing changes until
it is applied. Synthetic data only."""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest

from marsad import chain as engine_chain
from marsad_desktop import rig as R

DESKTOP = Path(__file__).resolve().parent.parent / "marsad_desktop"


def _p(t, name, detail="", **kw):
    return {"type": t, "name": name, "detail": detail, **kw}


def _row(*parts):
    """A straight chain as a drawing, as an old chain.json chain opens."""
    return R.rig_from_parts([copy.deepcopy(p) for p in parts])


def _cable(type_="coax", **kw):
    c = R.new_cable(type_)
    c.update(kw)
    return R.sync_cable(c)


OLD = [_p("antenna", "Antenna"), _p("coax", "Pigtail", "moulded lead", length="2'"),
       _p("filter", "FM notch"), _p("lna", "LNA"), _p("coax", "3 ft RG316 coax"),
       _p("bulkhead_sma", "SMA bulkhead"), _p("jumper", "6 in SMA jumper", "S-bend, no ferrite"),
       _p("sdr", "radio-a", rx="radio-a"), _p("bulkhead_usb", "USB bulkhead"),
       _p("usb", "USB cable", "w/ chokes", length="1'"), _p("pc", "PC")]


# ---- 1. the text of a cable drawn from now on
def test_a_new_cable_is_written_as_its_length_and_type():
    assert R.cable_text(_cable(length="3 ft", coax_type="RG316")) == "3 ft RG316 coax"
    assert R.cable_text(_cable(length="6 in")) == "6 in coax"
    assert R.cable_text(_cable("usb", length="16 ft")) == "16 ft USB cable"
    assert R.cable_text(_cable("usb", length="1 ft")) == "1 ft USB cable"
    assert R.cable_text(_cable()) == "Coax"                      # nothing typed yet: the old default
    assert R.cable_text(_cable("usb")) == "USB cable"
    assert R.cable_text(_cable("usb", coax_type="RG316", length="2 m")) == "2 m USB cable"   # a type is coax only


def test_chokes_and_a_marked_note_go_in_brackets():
    c = _cable(length="3 ft", coax_type="RG316", chokes=[{"at": 0.3}, {"at": 0.7}])
    assert R.cable_text(c) == "3 ft RG316 coax (with chokes)"
    c["chokes"] = [{"at": 0.5, "note": "ferrite, 3 turns"}]
    assert R.cable_text(c) == "3 ft RG316 coax (with choke)"     # a choke's note is drawing only
    c["note"] = "S-bend"
    assert R.cable_text(c) == "3 ft RG316 coax (with choke)"     # a note stays out until ticked
    c["note_in_text"] = True
    assert R.cable_text(c) == "3 ft RG316 coax (with choke, S-bend)"
    c.pop("chokes")
    assert R.cable_text(c) == "3 ft RG316 coax (S-bend)"


def test_a_new_rig_records_the_cables_as_the_relay_says():
    rig = R.new_rig()
    a, s, pc = R.new_node(rig, "antenna", 0, 0), R.new_node(rig, "sdr", 240, 0), R.new_node(rig, "pc", 480, 0)
    s["part"]["name"] = "radio-a"
    R.connect(rig, a["id"], 0, s["id"])
    R.connect(rig, s["id"], 0, pc["id"])
    w = R.wire_of(rig, a["id"], 0)
    w["cables"] = [_cable(length="3 ft", coax_type="RG316")]
    assert R.add_choke(rig, a["id"], 0, 0.2) is None
    assert R.add_choke(rig, a["id"], 0, 0.8, "ferrite") is None
    R.wire_of(rig, s["id"], 0)["cables"] = [_cable("usb", length="16 ft")]
    assert R.rig_texts("r", rig)["r"] == "Antenna > 3 ft RG316 coax (with chokes) > radio-a > 16 ft USB cable > PC"
    w2 = R.wire_of(rig, a["id"], 0)
    w2["cables"] = [_cable(length="6 in")]
    assert R.rig_texts("r", rig)["r"] == "Antenna > 6 in coax > radio-a > 16 ft USB cable > PC"


def test_new_cables_are_coax_or_usb_only():
    assert R.CABLE_TYPES == ("coax", "usb")
    for bad in ("jumper", "pigtail", "lna"):
        with pytest.raises(ValueError):
            R.new_cable(bad)
    for key in R.TEMPLATES:
        for down in (False, True):
            rig = R.template(key, receivers=["radio-a", "radio-b"], down=down)
            for w in rig["wires"]:
                for c in w.get("cables") or []:
                    assert c["type"] in R.CABLE_TYPES and c.get("auto")
            assert "jumper" not in json.dumps(rig).lower() and "pigtail" not in json.dumps(rig).lower()


def test_templates_still_write_what_they_wrote():
    assert R.rig_texts("t", R.template("one_radio", receivers=["radio-a"]))["t"] == "Antenna > Coax > radio-a > PC"


# ---- 2/3. what was saved keeps its text
def test_every_saved_chain_keeps_its_text_byte_for_byte():
    want = engine_chain.chain_text(OLD)
    rig = _row(*OLD)
    assert R.rig_texts("old", rig)["old"] == want
    lay = {"rigs": {"old": dict(copy.deepcopy(rig), chains={"old": want})}}
    back = R.check_layout(copy.deepcopy(lay))
    assert R.rig_texts("old", back["rigs"]["old"])["old"] == want
    assert json.dumps(back["rigs"]["old"]["wires"], sort_keys=True) == json.dumps(rig["wires"], sort_keys=True)


def test_old_words_are_found_and_nothing_changes_until_converted():
    rig = _row(*OLD)
    before = copy.deepcopy(rig)
    found = R.old_words(rig)
    kinds = sorted((x["kind"], x["where"]) for x in found)
    assert kinds == [("jumper", "cable"), ("pigtail", "cable")]
    assert rig == before                                     # looking changes nothing
    pig = next(x for x in found if x["kind"] == "pigtail")
    jum = next(x for x in found if x["kind"] == "jumper")
    assert pig.get("merge") and pig["was"] == "Pigtail (moulded lead)"
    assert jum["was"] == "6 in SMA jumper (S-bend, no ferrite)" and jum["becomes"] == "6 in coax (S-bend, no ferrite)"


def test_converting_a_jumper_reads_its_length_and_keeps_its_note():
    rig = _row(*OLD)
    want = engine_chain.chain_text(OLD)
    R.convert_old(rig, next(x for x in R.old_words(rig) if x["kind"] == "jumper"))
    got = R.rig_texts("old", rig)["old"]
    assert got == want.replace("6 in SMA jumper (S-bend, no ferrite)", "6 in coax (S-bend, no ferrite)")
    assert [x["kind"] for x in R.old_words(rig)] == ["pigtail"]
    c = R.converted_cable({"type": "jumper", "name": "SMA jumper",
                           "detail": '6" coax jumper from SDR to SMA bulkhead, S-bend, no ferrite'})
    assert c["type"] == "coax" and c["length"] == "6 in" and c["note_in_text"]
    assert R.converted_cable({"type": "jumper", "name": "RG316 jumper", "length": "30 cm"})["coax_type"] == "RG316"
    assert R.cable_text(R.converted_cable({"type": "jumper", "name": "SMA jumper"})) == "Coax"


def test_merging_a_pigtail_gives_the_antenna_its_lead_and_drops_it_from_the_text():
    rig = _row(*OLD)
    want = engine_chain.chain_text(OLD)
    R.convert_old(rig, next(x for x in R.old_words(rig) if x["kind"] == "pigtail"))
    ant = next(n for n in rig["nodes"] if n["part"]["type"] == "antenna")
    assert ant["antenna"]["lead"] == {"length": "2 ft", "note": "moulded lead"}
    assert R.lead_text(ant) == "2 ft (moulded lead)"
    assert R.rig_texts("old", rig)["old"] == want.replace("Antenna > Pigtail (moulded lead) > ", "Antenna > ")
    R.check_layout({"rigs": {"r": rig}})                     # the lead is a valid drawing field
    ant["antenna"]["lead"]["connector"] = "SMA male"
    assert R.lead_text(ant) == "2 ft, SMA male (moulded lead)"
    assert R.rig_texts("old", rig)["old"] == want.replace("Antenna > Pigtail (moulded lead) > ", "Antenna > ")
    rep = R.path_report(rig, R.paths(rig)[0])
    assert rep["antenna_lead"] == "2 ft, SMA male (moulded lead)"


def test_a_pigtail_part_merges_and_its_antenna_feeds_what_it_fed():
    rig = R.new_rig()
    a = R.new_node(rig, "antenna", 0, 0)
    pig = R.new_node(rig, "custom", 200, 0)
    pig["part"].update(name="Pigtail", detail="moulded, 1 ft")
    f = R.new_node(rig, "filter", 400, 0)
    s = R.new_node(rig, "sdr", 600, 0)
    s["part"]["name"] = "radio-a"
    for x, y in ((a, pig), (pig, f), (f, s)):
        R.connect(rig, x["id"], 0, y["id"])
    R.wire_of(rig, pig["id"], 0)["cables"] = [_cable(length="3 ft")]
    item = next(x for x in R.old_words(rig) if x["where"] == "part")
    assert item.get("merge")
    R.convert_old(rig, item)
    assert pig["id"] not in {n["id"] for n in rig["nodes"]}
    assert R.rig_texts("r", rig)["r"] == "Antenna > 3 ft coax > FM notch > radio-a"
    assert R.antenna(a)["lead"] == {"length": "1 ft", "note": "moulded, 1 ft"}


def test_a_jumper_left_as_a_part_converts_as_a_part():
    rig = _row(_p("antenna", "Antenna"), _p("sdr", "radio-a"), _p("jumper", "SMA jumper", "spare", length='6"'))
    item = next(x for x in R.old_words(rig) if x["where"] == "part")
    assert not item.get("merge") and item["becomes"] == "6 in coax (spare)"
    R.convert_old(rig, item)
    assert R.rig_texts("r", rig)["r"] == "Antenna > radio-a > 6 in coax (spare)"


def test_my_parts_in_the_old_words_are_offered_and_converted_only_on_request():
    lay = {"rigs": {}, "library": [{"id": "L1", "part": _p("jumper", "6 in SMA jumper", "S-bend")},
                                   {"id": "L2", "part": _p("lna", "LNA")}]}
    before = copy.deepcopy(lay)
    offers = R.library_old_words(lay)
    assert [(o["id"], o["becomes"]) for o in offers] == [("L1", "6 in coax (S-bend)")]
    assert lay == before
    R.convert_library_entry(lay, "L1")
    assert lay["library"][0]["part"]["type"] == "coax" and lay["library"][1] == before["library"][1]
    R.check_library(lay["library"])


# ---- 4. chokes
def test_a_choke_needs_a_cable_and_never_sits_alone():
    rig = _row(_p("antenna", "Antenna"), _p("sdr", "radio-a"))
    why = R.add_choke(rig, "n1", 0, 0.5)
    assert why and "cable" in why.lower()
    assert "chokes" not in json.dumps(rig)
    R.wire_of(rig, "n1", 0)["mated"] = True
    assert "mated" in R.add_choke(rig, "n1", 0, 0.5)


def test_chokes_land_on_the_cable_under_the_drop_and_slide_without_changing_the_text():
    rig = _row(_p("antenna", "Antenna"), _p("sdr", "radio-a"))
    w = R.wire_of(rig, "n1", 0)
    w["cables"] = [_cable(length="3 ft"), _cable(length="6 in")]
    assert R.add_choke(rig, "n1", 0, 0.8) is None               # second half of the wire: the second cable
    assert "chokes" not in w["cables"][0] and len(w["cables"][1]["chokes"]) == 1
    assert w["cables"][1]["chokes"][0]["at"] == pytest.approx(0.6)
    text = R.rig_texts("r", rig)["r"]
    assert text == "Antenna > 3 ft coax > 6 in coax (with choke) > radio-a"
    w["cables"][1]["chokes"][0]["at"] = R.choke_local(2, 1, 0.99)  # slid along: kept on its own cable
    assert w["cables"][1]["chokes"][0]["at"] == 0.96 and R.rig_texts("r", rig)["r"] == text
    assert R.choke_local(2, 1, 0.1) == 0.04                      # cannot slide onto the other cable
    assert R.choke_global(2, 1, 0.5) == pytest.approx(0.75)
    R.remove_choke(rig, "n1", 0, 1, 0)
    assert "chokes" not in w["cables"][1]
    assert R.rig_texts("r", rig)["r"] == "Antenna > 3 ft coax > 6 in coax > radio-a"


def test_a_choke_on_a_cable_saved_before_adds_its_word_once():
    old = {"type": "usb", "name": "USB cable", "detail": "w/ multiple chokes", "length": "16'"}
    assert R.cable_text(old) == "USB cable (w/ multiple chokes)"
    old["chokes"] = [{"at": 0.5}]
    assert R.cable_text(old) == "USB cable (w/ multiple chokes)"       # it already says chokes
    plain = {"type": "coax", "name": "Coax", "detail": ""}
    plain["chokes"] = [{"at": 0.5}, {"at": 0.6}]
    assert R.cable_text(plain) == "Coax (with chokes)"


def test_the_layout_check_reads_chokes_and_leads_and_refuses_nonsense():
    rig = _row(_p("antenna", "Antenna"), _p("sdr", "radio-a"))
    R.wire_of(rig, "n1", 0)["cables"] = [_cable(length="3 ft", chokes=[{"at": 0.4, "note": "ferrite"}])]
    R.check_layout({"rigs": {"r": copy.deepcopy(rig)}})
    for bad in ({"chokes": [{"at": 1.5}]}, {"chokes": [{"note": "x"}]}, {"chokes": "two"}, {"note_in_text": "yes"},
                {"coax_type": 316}):
        r2 = copy.deepcopy(rig)
        r2["wires"][0]["cables"][0].update(bad)
        with pytest.raises(ValueError):
            R.check_layout({"rigs": {"r": r2}})
    r3 = copy.deepcopy(rig)
    r3["nodes"][0]["antenna"] = {"lead": {"length": 2}}
    with pytest.raises(ValueError):
        R.check_layout({"rigs": {"r": r3}})


def test_lengths_are_read_from_names_and_notes():
    assert R.read_length("6 in SMA jumper") == ("6", "in")
    assert R.read_length('6" coax') == ("6", "in")
    assert R.read_length("2'") == ("2", "ft")
    assert R.read_length("16 ft USB3") == ("16", "ft")
    assert R.read_length("150 cm") == ("150", "cm")
    assert R.read_length("25 mm") == ("2.5", "cm")
    assert R.read_length("SMA jumper", None, "") is None
    assert R.split_length("3 ft") == ("3", "ft") and R.split_length(None) == ("", "in")
    assert R.length_text("3", "ft") == "3 ft" and R.length_text("", "ft") == ""
    assert R.length_m("3 ft") == pytest.approx(0.9144) and R.length_m("6 in") == pytest.approx(0.1524)


# ---- 5. no Jumper or Pigtail offered in the window, and the explanations
def test_the_window_offers_no_jumper_and_no_pigtail():
    src = (DESKTOP / "chain_screen.py").read_text(encoding="utf-8")
    order = re.search(r"PALETTE_ORDER = \[(.*?)\]", src, re.S).group(1)
    assert '"jumper"' not in order
    assert "+ Jumper" not in src and '"jumper": "Jumper"' not in src     # no add-a-jumper button anywhere
    offered = re.findall(r"""self\._entry\((?:labels\[t\]\["label"\]|"([^"]+)")""", src)
    assert not [o for o in offered if o and re.search(r"(?i)jumper|pigtail", o)]
    assert not re.search(r"(?i)pigtail\"\s*:", src)
    for t in R.OLD_CABLE_TYPES:
        assert t not in R.CABLE_TYPES


def test_explanations_cover_cables_chokes_and_the_lead():
    md = (DESKTOP / "explanations.md").read_text(encoding="utf-8")
    for key in ("cable_coax", "cable_usb", "cable_note_in_text", "choke", "antenna_lead", "old_words"):
        assert re.search(rf"^## {key}\s*$", md, re.M), key
    flat = " ".join(md.split()).lower()
    assert "in records made before november 2026, \"jumper\" means a short cable." in flat
    assert "shielded" in md and "ferrite" in md
    for line in md.splitlines():                  # "jumper" only ever as the record note or the old-word offer
        if "jumper" in line.lower():
            assert "before November 2026" in line or "Jumper" in line or "jumper\" means" in line, line
