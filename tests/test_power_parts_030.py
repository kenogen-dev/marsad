"""Relay 030, Part B: bias-tee injectors, DC power supplies and + / GND power leads on the Chain screen, and the IP
rating on enclosures. The injector is in the signal path and in the recorded text; supplies, leads, power pins and
IP ratings are drawing only. Every existing chain's text stays byte for byte. Made-up rigs only."""
import copy
import json
import sys
from pathlib import Path

import pytest

from conftest import ROOT

sys.path.insert(0, str(ROOT))

from marsad import chain as engine_chain         # noqa: E402
from marsad_desktop import power as P            # noqa: E402
from marsad_desktop import rig as R              # noqa: E402


def node(nid, type_, name=None, x=0, **kw):
    return {"id": nid, "part": {"type": type_, "name": name or R.PART_TYPES[type_]["label"], "detail": ""},
            "x": x, "y": 0, **kw}


def wire(a, b, port=0, cables=None):
    w = {"from": a, "to": b, "port": port}
    if cables:
        w["cables"] = cables
    return w


def rig_with(between=(), supply_v=5.0):
    """antenna > LNA > [between...] > coax > injector > SDR, a 5 V supply beside it."""
    nodes = [node("a", "antenna", "Made-up antenna"), node("l", "lna", "Made-up LNA", 100)]
    ids = ["a", "l"]
    for i, t in enumerate(between):
        nodes.append(node(f"b{i}", t, x=200 + 50 * i))
        ids.append(f"b{i}")
    nodes += [node("i", P.INJECTOR, "Made-up injector", 400), node("s", "sdr", "made-up radio", 500)]
    ids += ["i", "s"]
    coax = [{"type": "coax", "auto": True, "length": "3 ft"}]
    wires = [wire(x, y, cables=coax if y == "i" else None) for x, y in zip(ids, ids[1:])]
    return {"nodes": nodes, "wires": wires,
            "power": [{"id": "p1", "kind": P.SUPPLY, "name": "Made-up bench supply", "volts": supply_v,
                       "amps": 0.5, "note": "", "x": 400, "y": 200}]}


def levels(rig):
    return [(c["level"], c["text"]) for c in R.checks_by_level(rig)]


# ---------------------------------------------------------------- the parts
def test_the_new_parts_are_offered():
    assert R.PART_TYPES[P.INJECTOR]["label"] == "Bias-tee injector"
    assert P.SUPPLY in R.POWER_KINDS and R.POWER_KINDS[P.SUPPLY] == "DC linear power supply"
    assert P.INJECTOR in R.labels_from(None)


def test_injector_is_in_the_recorded_text_and_power_is_not():
    rig = rig_with()
    P.connect_pair(rig, "p1", "i")
    path = R.paths(rig)[0]
    text = engine_chain.chain_text(R.path_parts(rig, path))
    assert "Made-up injector" in text
    bare = copy.deepcopy(rig)
    for k in ("power", "power_wires"):
        bare.pop(k, None)
    assert engine_chain.chain_text(R.path_parts(bare, R.paths(bare)[0])) == text      # leads change nothing
    assert "supply" not in text.lower() and "VCC" not in text and "GND" not in text


def test_injector_default_name_is_written():
    rig = rig_with()
    rig["nodes"][2]["part"]["name"] = ""
    assert "Bias-tee injector" in engine_chain.chain_text(R.path_parts(rig, R.paths(rig)[0]))


def test_power_leads_never_enter_a_path():
    rig = rig_with()
    P.connect_pair(rig, "p1", "i")
    assert all("p1" not in [nid for nid, _ in p] for p in R.paths(rig))


# ---------------------------------------------------------------- leads and checks
def test_connect_both_at_once_draws_the_pair_and_powers_the_lna():
    rig = rig_with()
    assert P.connect_pair(rig, "p1", "i") is None
    pins = sorted((w["pin"], w["to_pin"]) for w in rig["power_wires"])
    assert pins == [("+", "vcc"), ("gnd", "gnd")]
    st = P.status(rig, R.needs_power)["l"]
    assert st["powered"] and "Made-up injector" in st["how"] and st["volts"] == 5.0
    assert not any(c["kind"] == "power" for c in R.checks(rig))


def test_reversed_polarity_is_a_problem():
    rig = rig_with()
    assert P.connect_lead(rig, "p1", "+", "i", "gnd") is None
    assert P.connect_lead(rig, "p1", "gnd", "i", "vcc") is None
    lv = levels(rig)
    assert sum(1 for level, t in lv if level == "problem" and t.startswith("Reversed polarity")) == 2
    assert not P.status(rig, R.needs_power)["l"]["powered"]


def test_a_missing_ground_or_plus_is_a_warning():
    rig = rig_with()
    P.connect_lead(rig, "p1", "+", "i", "vcc")
    lv = levels(rig)
    assert ("warning", "Made-up injector has no GND lead") in lv
    rig2 = rig_with()
    P.connect_lead(rig2, "p1", "gnd", "i", "gnd")
    assert ("warning", "Made-up injector has no + lead") in levels(rig2)


def test_a_dc_block_between_is_a_problem():
    rig = rig_with(between=("dcblock",))
    P.connect_pair(rig, "p1", "i")
    lv = levels(rig)
    assert ("problem", "The DC block stops the injector's power reaching Made-up LNA") in lv
    assert not P.status(rig, R.needs_power)["l"]["powered"]


def test_dc_passes_adapters_bulkheads_and_the_notch():
    rig = rig_with(between=("adapter", "bulkhead_sma", "filter"))
    P.connect_pair(rig, "p1", "i")
    assert P.status(rig, R.needs_power)["l"]["powered"]


def test_voltage_outside_the_stated_range_is_a_warning():
    rig = rig_with(supply_v=12)
    rig["nodes"][1]["needs_v"] = "4.5-5.5"
    P.connect_pair(rig, "p1", "i")
    assert ("warning", "Made-up LNA needs 4.5–5.5 V but its supply is set to 12 V") in levels(rig)
    rig["power"][0]["volts"] = 5
    assert not any("needs" in t for _, t in levels(rig))


def test_supply_straight_to_the_lna_counts():
    rig = rig_with()
    assert P.connect_pair(rig, "p1", "l") is None
    assert P.status(rig, R.needs_power)["l"]["powered"]


def test_old_power_wires_keep_working():
    rig = rig_with()
    rig["power"].append({"id": "p2", "kind": "usbc", "name": "USB-C lead", "note": "", "x": 0, "y": 200})
    assert R.connect_power(rig, "p2", "l") is None
    assert P.status(rig, R.needs_power)["l"]["powered"]
    assert not any(c["kind"] == "power" and "LNA" in c["text"] for c in R.checks(rig))


def test_output_names_the_power_path():
    rig = rig_with()
    P.connect_pair(rig, "p1", "i")
    rep = R.path_report(rig, R.paths(rig)[0])
    assert any("Made-up LNA: powered, through Made-up injector" in line for line in rep["power_lines"])


# ---------------------------------------------------------------- saving
def test_layout_accepts_supplies_leads_injectors_and_ip_ratings():
    rig = rig_with()
    P.connect_pair(rig, "p1", "i")
    rig["enclosures"] = [{"id": "e1", "x": 0, "y": 0, "w": 400, "h": 240, "name": "Aluminium enclosure", "ip": "IP65"}]
    lay = R.check_layout({"rigs": {"Made-up rig": rig}})
    got = lay["rigs"]["Made-up rig"]
    assert got["enclosures"][0]["ip"] == "IP65" and len(got["power_wires"]) == 2


def test_layout_refuses_a_bad_ip_rating_and_a_doubled_pin():
    rig = rig_with()
    rig["enclosures"] = [{"id": "e1", "x": 0, "y": 0, "w": 400, "h": 240, "ip": 65}]
    with pytest.raises(ValueError, match="IP rating"):
        R.check_layout({"rigs": {"r": rig}})
    rig = rig_with()
    rig["power_wires"] = [{"from": "p1", "to": "i", "pin": "+", "to_pin": "vcc"},
                          {"from": "p1", "to": "i", "pin": "+", "to_pin": "vcc"}]
    with pytest.raises(ValueError):
        R.check_layout({"rigs": {"r": rig}})


def test_enclosure_presets_carry_an_ip_rating_field():
    rig = R.new_rig()
    e = R.new_enclosure(rig, 0, 0, name=R.ENC_PRESETS["aluminium"])
    assert "ip" in e and e["ip"] == ""
    assert R.ip_text(e) == ""
    e["ip"] = "ip65"
    assert R.ip_text(e) == "IP65"


# ---------------------------------------------------------------- nothing old changes
def made_up_rigs():
    """Rigs every template makes, and a hand-built split rig with a cable, a DC block, an adapter and a USB hub."""
    rigs = {}
    for name, tmpl in (("one", "one_radio"),) + tuple((k, k) for k in R.TEMPLATES if k != "blank"):
        rigs[name] = R.template(tmpl, R.labels_from(None), ["radio-a", "radio-b"])
    r = R.new_rig()
    ids = {}
    for t, x in (("antenna", 0), ("filter", 100), ("lna", 200), (R.SPLITTER, 300), ("dcblock", 400),
                 ("sdr", 500), ("adapter", 400), ("sdr", 600), ("usb_hub", 700), ("pc", 800)):
        n = R.new_node(r, t, x, 0, R.labels_from(None))
        ids.setdefault(t, []).append(n["id"])
    a, f, l, sp, dc = ids["antenna"][0], ids["filter"][0], ids["lna"][0], ids[R.SPLITTER][0], ids["dcblock"][0]
    s1, ad, s2, hub, pc = ids["sdr"][0], ids["adapter"][0], ids["sdr"][1], ids["usb_hub"][0], ids["pc"][0]
    by = {n["id"]: n for n in r["nodes"]}
    by[s1]["part"].update(name="made-up radio A", rx="radio-a")
    by[s2]["part"].update(name="made-up radio B", rx="radio-b")
    for x, y, p in ((a, f, 0), (f, l, 0), (l, sp, 0), (sp, dc, 0), (dc, s1, 0), (sp, ad, 1), (ad, s2, 0), (s1, hub, 0)):
        R.connect(r, x, p, y)
    R.connect(r, hub, 0, pc)
    r["wires"][0]["cables"] = [{"type": "coax", "name": "Coax", "detail": "made-up RG316", "length": "1 m"}]
    rigs["split"] = r
    return rigs


def test_every_existing_chain_text_is_byte_for_byte():
    """Made-up rigs write every path text exactly as the code before relay 030 wrote it (the golden file was written
    by running this same builder on the rig.py of the commit before relay 030)."""
    gold = json.loads((Path(__file__).parent / "data" / "chain_texts_before_030.json").read_text(encoding="utf-8"))
    labels = R.labels_from(None)
    now = {n: [engine_chain.chain_text(R.path_parts(r, p, labels)) for p in R.paths(r)]
           for n, r in made_up_rigs().items()}
    assert now == gold
    # and adding power parts beside them changes none of it
    for n, r in made_up_rigs().items():
        r.setdefault("power", []).append({"id": "p9", "kind": P.SUPPLY, "name": "s", "volts": 5, "amps": 1,
                                          "note": "", "x": 0, "y": 300})
        lna = next((x for x in r["nodes"] if x["part"]["type"] == "lna"), None)
        if lna is not None:
            P.connect_pair(r, "p9", lna["id"])
        assert [engine_chain.chain_text(R.path_parts(r, p, labels)) for p in R.paths(r)] == gold[n]
