"""Relay 015: Tidy with enclosures as blocks, one enclosure per part, a PC (or USB hub) with several inputs,
wire bend points and end reconnecting, resize modes. Drawing only: every recorded text stays as it was. Made-up
rigs; no Qt."""
from __future__ import annotations

import copy
import json

from marsad_desktop import geometry as G
from marsad_desktop import rig as R


# ---------------------------------------------------------------- helpers
def split_rig(shared_pc: bool):
    """antenna > notch > LNA > splitter, then two branches to two radios, each to a PC: one PC shared by both,
    or one each (today's drawing)."""
    rig = R.new_rig()
    a = R.new_node(rig, "antenna", 0, 0)
    a["part"].update(name="antenna", detail="dipole")
    n = R.new_node(rig, "filter", 180, 0)
    l = R.new_node(rig, "lna", 360, 0)
    s = R.new_node(rig, R.SPLITTER, 540, 0)
    s["part"]["name"] = "2-way splitter"
    s["ports"] = ["RFout1 (right)", "RFout2 (left)"]
    x = R.new_node(rig, "sdr", 900, -80)
    x["part"].update(name="radio B", rx="rx2")
    v = R.new_node(rig, "sdr", 900, 80)
    v["part"].update(name="radio A", rx="rx1")
    pc1 = R.new_node(rig, "pc", 1080, 0)
    pc1["part"]["name"] = "PC"
    pc2 = pc1
    if not shared_pc:
        pc2 = R.new_node(rig, "pc", 1080, -160)
        pc2["part"]["name"] = "PC"
    for src, port, dst in [(a, 0, n), (n, 0, l), (l, 0, s), (s, 1, x), (s, 0, v), (v, 0, pc1), (x, 0, pc2)]:
        assert R.connect(rig, src["id"], port, dst["id"]) is None
    R.wire_of(rig, x["id"], 0)["cables"] = [{"type": "usb", "name": "16 ft USB", "detail": ""}]
    R.wire_of(rig, v["id"], 0)["cables"] = [{"type": "usb", "name": "6 ft USB", "detail": ""}]
    return rig


def filled_box(rig, x, y, name):
    """An enclosure at (x, y) with two parts in it, wired, grouped with them."""
    e = R.new_enclosure(rig, x, y, 400, 200, name=name)
    p1 = R.new_node(rig, "dcblock", x + 40, y + 60)
    p2 = R.new_node(rig, "sdr", x + 220, y + 60)
    assert R.connect(rig, p1["id"], 0, p2["id"]) is None
    R.make_group(rig, [e["id"], p1["id"], p2["id"]])
    return e, p1, p2


def apply(rig, pos, rects):
    for bid, (x, y) in pos.items():
        b = R.box(rig, bid)
        b["x"], b["y"] = x, y
    for eid, (x, y, w, h) in rects.items():
        R.box(rig, eid).update(x=x, y=y, w=w, h=h)


# ---------------------------------------------------------------- item 5: Tidy and enclosures
def test_the_owners_case_two_grouped_enclosures_tidy_without_overlap_and_pull_apart():
    rig = R.new_rig()
    a = R.new_node(rig, "antenna", 0, 400)
    e1, a1, b1 = filled_box(rig, 300, 0, "cage A")
    e2, a2, b2 = filled_box(rig, 300, 400, "cage B")
    assert R.connect(rig, a["id"], 0, a1["id"]) is None
    pos, rects = G.tidy_all(rig)
    apply(rig, pos, rects)
    r1, r2 = G.rect(rig, e1["id"]), G.rect(rig, e2["id"])
    assert not G._overlap(r1, r2), "Tidy laid the two enclosures over each other"
    every = [b["id"] for b in R._boxes(rig)]
    for i in every:                                        # nothing overlaps anything outside its own block
        for j in every:
            same = {i, j} <= {e1["id"], a1["id"], b1["id"]} or {i, j} <= {e2["id"], a2["id"], b2["id"]}
            if i < j and not same:
                assert not G._overlap(G.rect(rig, i), G.rect(rig, j)), (i, j)
    # each enclosure kept its own parts, and a drag moves only its own
    assert set(G.inside_any(rig, e1)) == {a1["id"], b1["id"]}
    assert set(G.inside_any(rig, e2)) == {a2["id"], b2["id"]}
    before2 = (e2["x"], e2["y"], a2["x"], b2["x"])
    assert G.shift(rig, [e1["id"], a1["id"], b1["id"]], 0, -400)
    assert (e2["x"], e2["y"], a2["x"], b2["x"]) == before2


def test_a_tangled_drawing_comes_apart_two_grouped_enclosures_laid_on_top_of_each_other():
    """The state the old Tidy left: cage B's box and parts sitting right over cage A's. Each enclosure still
    owns only its own group's parts, so dragging one carries nothing of the other."""
    rig = R.new_rig()
    e1, a1, b1 = filled_box(rig, 0, 0, "cage A")
    e2, a2, b2 = filled_box(rig, 0, 400, "cage B")
    G.shift(rig, [e2["id"], a2["id"], b2["id"]], 20, -380)        # B now overlaps A almost exactly
    assert G._overlap(G.rect(rig, e1["id"]), G.rect(rig, e2["id"]))
    own = G.owners(rig)
    assert {own[a1["id"]], own[b1["id"]]} == {e1["id"]} and {own[a2["id"]], own[b2["id"]]} == {e2["id"]}
    moving = G.moving_with(rig, [e1["id"], a1["id"], b1["id"]])
    assert moving == {e1["id"], a1["id"], b1["id"]}
    G.shift(rig, sorted(moving), 0, 600)
    assert not G._overlap(G.rect(rig, e1["id"]), G.rect(rig, e2["id"]))


def test_a_part_belongs_to_the_smallest_enclosure_holding_its_centre():
    rig = R.new_rig()
    big = R.new_enclosure(rig, 0, 0, 800, 400, name="shelf")
    small = R.new_enclosure(rig, 100, 100, 300, 200, name="cage")
    radio = R.new_node(rig, "sdr", 160, 160)
    pc = R.new_node(rig, "pc", 560, 160)
    own = G.owners(rig)
    assert own[radio["id"]] == small["id"] and own[pc["id"]] == big["id"]
    assert G.inside_any(rig, big) == [pc["id"]]                    # never the cage's radio
    assert radio["id"] not in G.moving_with(rig, [big["id"]])


def test_tidy_never_overlaps_scrambled_enclosures_groups_and_parts():
    rig = split_rig(shared_pc=False)
    for i, n in enumerate(rig["nodes"]):
        n["x"], n["y"] = (i * 260) % 700, (i * 170) % 500
    ids = [n["id"] for n in rig["nodes"]]
    R.new_enclosure(rig, rig["nodes"][4]["x"] - 20, rig["nodes"][4]["y"] - 40, 220, 160)
    R.new_enclosure(rig, rig["nodes"][5]["x"] - 20, rig["nodes"][5]["y"] - 40, 220, 160)
    R.make_group(rig, ids[1:3])
    pos, rects = G.tidy_all(rig)
    apply(rig, pos, rects)
    blocks = G.blocks(rig)
    block_of = {m: k for k, b in enumerate(blocks) for m in b}
    every = [b["id"] for b in R._boxes(rig)]
    for i in every:
        for j in every:
            if i < j and not (i in block_of and block_of.get(i) == block_of.get(j)):
                assert not G._overlap(G.rect(rig, i), G.rect(rig, j)), (i, j)


def test_tidy_leaves_every_recorded_text_as_it_was():
    rig = split_rig(shared_pc=True)
    R.new_enclosure(rig, 880, -120, 400, 300)
    before = R.rig_texts("r", rig)
    apply(rig, *G.tidy_all(rig))
    assert R.rig_texts("r", rig) == before


# ---------------------------------------------------------------- item 4: one PC, several inputs
def test_both_radios_plug_into_one_pc_and_each_path_keeps_its_own_text():
    shared, apart = split_rig(shared_pc=True), split_rig(shared_pc=False)
    assert R.rig_texts("Split", shared) == R.rig_texts("Split", apart)
    texts = R.rig_texts("Split", shared)
    assert len(texts) == 2
    a, b = texts["Split (radio A)"], texts["Split (radio B)"]
    assert a.endswith("> radio A > 6 ft USB > PC") and "radio B" not in a and "16 ft" not in a
    assert b.endswith("> radio B > 16 ft USB > PC") and "radio A" not in b and "> 6 ft" not in b


def test_a_pc_takes_another_input_whenever_all_are_used():
    rig = split_rig(shared_pc=True)
    pc = next(n for n in rig["nodes"] if n["part"]["type"] == "pc")
    assert R.used_inputs(rig, pc["id"]) == [0, 1] and R.free_input(rig, pc["id"]) == 2
    am = G.anchors(rig)
    assert {(pc["id"], "in", 0), (pc["id"], "in", 1), (pc["id"], "in", 2)} <= set(am)
    # a third radio straight in
    r3 = R.new_node(rig, "sdr", 900, 300)
    assert R.connect(rig, r3["id"], 0, pc["id"]) is None
    assert R.used_inputs(rig, pc["id"]) == [0, 1, 2]
    # a one-input part still refuses a second wire, with a reason
    lna = next(n for n in rig["nodes"] if n["part"]["type"] == "lna")
    why = R.connect(rig, r3["id"], 0, lna["id"])
    assert why and "already" in why
    # freed inputs are taken again first
    R.disconnect(rig, next(n["id"] for n in rig["nodes"] if n["part"].get("name") == "radio A"), 0)
    assert R.free_input(rig, pc["id"]) == 0


def test_a_usb_hub_has_one_upstream_and_many_downstream_and_writes_as_a_custom_part():
    rig = R.new_rig()
    ants = [R.new_node(rig, "antenna", 0, y) for y in (0, 200)]
    radios = [R.new_node(rig, "sdr", 200, y) for y in (0, 200)]
    radios[0]["part"]["name"], radios[1]["part"]["name"] = "radio A", "radio B"
    hub = R.new_node(rig, R.HUB, 400, 100)
    pc = R.new_node(rig, "pc", 600, 100)
    for a, r in zip(ants, radios):
        assert R.connect(rig, a["id"], 0, r["id"]) is None
        assert R.connect(rig, r["id"], 0, hub["id"]) is None
    assert R.out_ports(hub) == 1
    assert R.connect(rig, hub["id"], 0, pc["id"]) is None
    texts = sorted(R.rig_texts("Hub", rig).values())
    assert texts == ["Antenna > radio A > USB hub > PC", "Antenna > radio B > USB hub > PC"]
    for parts in R.rig_chains("Hub", rig).values():
        assert all(p["type"] in R.engine_chain.PART_TYPES for p in parts)      # the old app can read it


def test_the_layout_check_keeps_inputs_and_refuses_two_wires_into_one_input():
    rig = split_rig(shared_pc=True)
    doc = json.loads(json.dumps({"rigs": {"r": rig}}))
    assert R.check_layout(doc)["rigs"]["r"]["wires"] == rig["wires"]
    bad = copy.deepcopy(doc)
    for w in bad["rigs"]["r"]["wires"]:
        if "in" in w:
            w["in"] = 0
    try:
        R.check_layout(bad)
    except ValueError as exc:
        assert "same input" in str(exc)
    else:
        raise AssertionError("two wires into one input passed the check")


def test_drawings_without_shared_inputs_save_byte_for_byte_as_before():
    """A wire into a one-input part gets no "in" key, so an existing layout's wires do not change."""
    rig = split_rig(shared_pc=False)
    assert all("in" not in w or R.multi_in(R.box(rig, w["to"])) for w in rig["wires"])
    lna_wires = [w for w in rig["wires"] if R.box(rig, w["to"])["part"]["type"] != "pc"]
    assert all("in" not in w for w in lna_wires)


# ---------------------------------------------------------------- item 2: resize modes
def test_resize_from_the_centre_keeps_the_centre_and_shift_alt_keeps_both():
    rect = (100, 100, 200, 100)                       # centre (200, 150)
    x, y, w, h = G.resize(rect, "r", 40, 0, from_centre=True)
    assert (w, x + w / 2, y + h / 2) == (280, 200, 150)
    x, y, w, h = G.resize(rect, "br", 40, 10, keep_ratio=True, from_centre=True)
    assert abs(w / h - 2) < 0.02 and (x + w / 2, y + h / 2) == (200, 150)
    x, y, w, h = G.resize(rect, "tl", -10, -10, from_centre=True)
    assert (x + w / 2, y + h / 2) == (200, 150) and w == 220 and h == 120


def test_ctrl_ignores_the_grid_shift_keeps_proportions():
    rect = (0, 0, 200, 100)
    assert G.resize(rect, "r", 7, 0, snap=R.GRID)[2] == 200            # snaps back to the grid
    assert G.resize(rect, "r", 7, 0, snap=None)[2] == 207              # Ctrl: fine sizing
    x, y, w, h = G.resize(rect, "br", 100, 0, keep_ratio=True)
    assert w / h == 2


# ---------------------------------------------------------------- item 3: bend points
def test_a_wire_routes_through_its_bend_points_in_every_style():
    a, b = (0, 0, "right"), (400, 200, "left")
    pts = [[200, -100]]
    kind, segs = G.route_via(a, b, pts, "curved")
    assert kind == "spline" and segs[0][0] == (0.0, 0.0) and segs[0][3] == (200.0, -100.0) and segs[-1][3] == (400.0, 200.0)
    kind, line = G.route_via(a, b, pts, "straight")
    assert line == [(0.0, 0.0), (200.0, -100.0), (400.0, 200.0)]
    for style, square in (("angled", False), ("curved", True)):
        kind, line = G.route_via(a, b, pts, style, square=square)
        assert kind == "poly" and line[0] == (0.0, 0.0) and line[-1] == (400.0, 200.0)
        assert all(p[0] == q[0] or p[1] == q[1] for p, q in zip(line, line[1:])), line   # right angles only
        assert (200.0, -100.0) in line
    assert G.route_via(a, b, [], "curved") == G.route(a, b, "curved")        # no points: as before


def test_a_new_point_goes_into_the_stretch_it_was_made_on():
    a, b = (0, 0), (400, 0)
    pts = [[100, 100], [300, 100]]
    assert G.insert_index(a, b, pts, 50, 40) == 0
    assert G.insert_index(a, b, pts, 200, 110) == 1
    assert G.insert_index(a, b, pts, 360, 40) == 2


def test_bends_move_with_a_moved_group_and_tidy_takes_off_bends_that_no_longer_fit():
    rig = split_rig(shared_pc=True)
    radio = next(n for n in rig["nodes"] if n["part"].get("name") == "radio A")
    pc = next(n for n in rig["nodes"] if n["part"]["type"] == "pc")
    w = R.wire_of(rig, radio["id"], 0)
    w["points"] = [[1000, 200]]
    G.shift(rig, [radio["id"], pc["id"]], 40, 20)
    assert w["points"] == [[1040, 220]]
    G.shift(rig, [radio["id"]], 0, 20)                 # one end only: the bend stays where it is
    assert w["points"] == [[1040, 220]]
    before = {n["id"]: (n["x"], n["y"]) for n in rig["nodes"]}
    radio["x"] += 100
    G.bends_after_move(rig, before)
    assert "points" not in w


def test_bends_and_inputs_never_reach_the_recorded_text_and_pass_the_layout_check():
    rig = split_rig(shared_pc=True)
    before = R.rig_texts("r", rig)
    for k, w in enumerate(rig["wires"]):
        w["points"] = [[100 * k, 50], [100 * k + 40, 90]]
        w["square"] = bool(k % 2)
    assert R.rig_texts("r", rig) == before
    doc = R.check_layout(json.loads(json.dumps({"rigs": {"r": rig}})))
    assert doc["rigs"]["r"]["wires"][0]["points"] == [[0, 50], [40, 90]]
    bad = json.loads(json.dumps({"rigs": {"r": rig}}))
    bad["rigs"]["r"]["wires"][0]["points"] = [[1, "x"]]
    try:
        R.check_layout(bad)
    except ValueError as exc:
        assert "bend points" in str(exc)
    else:
        raise AssertionError("a bad bend point passed the check")


def test_a_label_sits_on_the_longest_stretch_not_on_a_corner():
    spot = G.label_spot(("poly", [(0, 0), (10, 0), (10, 300), (20, 300)]))
    assert spot == (10, 150)


def test_a_wire_end_reconnects_keeping_its_cables_and_bends_and_refuses_with_a_reason():
    rig = split_rig(shared_pc=False)
    names = {n["part"].get("name"): n["id"] for n in rig["nodes"]}
    pcs = [n["id"] for n in rig["nodes"] if n["part"]["type"] == "pc"]
    radio_b = names["radio B"]
    w = R.wire_of(rig, radio_b, 0)
    w["points"] = [[1000, -60]]
    old_to = w["to"]
    new_pc = next(p for p in pcs if p != old_to)
    assert R.reconnect(rig, radio_b, 0, "to", new_pc) is None          # onto the other PC: both on one now
    w2 = R.wire_of(rig, radio_b, 0)
    assert w2["to"] == new_pc and w2["points"] == [[1000, -60]] and w2["cables"][0]["name"] == "16 ft USB"
    assert len(R.used_inputs(rig, new_pc)) == 2
    lna = names["LNA"]
    why = R.reconnect(rig, radio_b, 0, "to", lna)                      # LNA already has its input
    assert why and "already has an input" in why
    assert R.wire_of(rig, radio_b, 0)["to"] == new_pc                  # nothing changed
    why = R.reconnect(rig, radio_b, 0, "to", names["antenna"])
    assert why and "source" in why
    # the near end: the wire into the PC now leaves the other radio... which already has its output wired
    why = R.reconnect(rig, radio_b, 0, "from", names["radio A"])
    assert why and "no free output" in why
    spare = R.new_node(rig, "sdr", 900, 300)
    assert R.reconnect(rig, radio_b, 0, "from", spare["id"]) is None
    w3 = R.wire_of(rig, spare["id"], 0)
    assert w3["to"] == new_pc and w3["points"] == [[1000, -60]] and R.wire_of(rig, radio_b, 0) is None
