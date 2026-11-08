"""Relay 014: bulkheads seat across an enclosure's wall, slide along it (round corners too), come off with a firm
drag away, move with the enclosure and stay seated when it is resized. Drawing only: the recorded text does not
change. Made-up rigs; no Qt."""
from __future__ import annotations

import copy

from marsad_desktop import geometry as G
from marsad_desktop import rig as R


def rig_with_box():
    """antenna > SMA bulkhead > radio > PC, and an enclosure at (300, 100), 400 x 240."""
    r = R.new_rig()
    a = R.new_node(r, "antenna", 0, 140)
    b = R.new_node(r, "bulkhead_sma", 200, 140)
    s = R.new_node(r, "sdr", 420, 140)
    p = R.new_node(r, "pc", 760, 140)
    s["part"]["rx"] = "radio-a"
    for x, y in ((a, b), (b, s), (s, p)):
        r["wires"].append({"from": x["id"], "to": y["id"], "port": 0, "cables": []})
    e = R.new_enclosure(r, 300, 100, 400, 240, name="Box")
    return r, b, e


def centre(node):
    w, h = G.size(node)
    return node["x"] + w / 2, node["y"] + h / 2


def test_projection_finds_the_nearest_side_and_wraps_round_corners():
    e = {"x": 0, "y": 0, "w": 400, "h": 200}
    assert G.wall_project(e, 100, -10)[:2] == ("top", 0.25)
    assert G.wall_project(e, 410, 50)[:2] == ("right", 0.25)
    side, t, d = G.wall_project(e, 405, -3)          # just past the top-right corner
    assert (side, round(t, 3)) in (("top", 1.0), ("right", 0.0)) and d < 6


def test_a_bulkhead_near_a_wall_seats_across_it():
    r, b, e = rig_with_box()
    x, y = G.seat_near(r, b, 300 - 70 + 10, 150)      # centre 10 px outside the left wall
    b["x"], b["y"] = x, y
    assert b["wall"]["enc"] == e["id"] and b["wall"]["side"] == "left"
    assert centre(b)[0] == e["x"]                    # centred on the wall line: half in, half out


def test_far_from_any_wall_it_stays_free():
    r, b, _ = rig_with_box()
    G.seat_near(r, b, 0, 400)
    assert "wall" not in b


def test_only_bulkheads_seat():
    r, _, e = rig_with_box()
    s = next(n for n in r["nodes"] if n["part"]["type"] == "sdr")
    assert G.seat_near(r, s, e["x"] - 70, 150) == (e["x"] - 70, 150) and "wall" not in s


def test_seated_it_slides_along_and_round_the_corner_then_comes_off_with_a_firm_pull():
    r, b, e = rig_with_box()
    b["x"], b["y"] = G.seat_near(r, b, e["x"] - 70, 150)
    b["x"], b["y"] = G.seat_near(r, b, e["x"] - 70 + 30, e["y"] - 32 + 5, keep=True)   # dragged up past the corner
    assert b["wall"]["side"] in ("left", "top")
    b["x"], b["y"] = G.seat_near(r, b, e["x"] + 100, e["y"] - 32 - 20, keep=True)     # along the top
    assert b["wall"]["side"] == "top" and centre(b)[1] == e["y"]
    G.seat_near(r, b, e["x"] + 100, e["y"] - 32 - G.WALL_PULL - 20, keep=True)        # a firm drag away
    assert "wall" not in b


def test_it_moves_with_the_enclosure_whatever_the_centre_inside_rule_says():
    r, b, e = rig_with_box()
    b["x"], b["y"] = G.seat_near(r, b, e["x"] - 70, 150)
    b["x"] -= 1                                       # centre a hair outside: the old rule would leave it
    assert b["id"] in G.moving_with(r, [e["id"]])


def test_it_stays_seated_when_the_enclosure_is_resized_or_moved():
    r, b, e = rig_with_box()
    b["x"], b["y"] = G.seat_near(r, b, e["x"] + 200 - 70, e["y"] - 32)   # top wall, half way
    t = b["wall"]["t"]
    e["w"], e["h"] = 600, 300
    assert G.reseat(r, e["id"]) == [b["id"]]
    assert b["wall"]["side"] == "top" and b["wall"]["t"] == t
    assert centre(b) == (e["x"] + t * 600, e["y"])
    e["x"] += 40
    G.reseat(r)
    assert centre(b)[0] == e["x"] + t * 600


def test_settle_slides_a_nudged_part_along_and_drops_a_gone_enclosure():
    r, b, e = rig_with_box()
    b["x"], b["y"] = G.seat_near(r, b, e["x"] - 70, 150)
    b["x"] += 10                                      # an arrow-key nudge off the line
    G.settle_walls(r)
    assert centre(b)[0] == e["x"]
    r["enclosures"] = []
    G.reseat(r)
    assert "wall" not in b


def test_the_recorded_text_is_unchanged_by_seating():
    r, b, e = rig_with_box()
    before = R.rig_texts("Box rig", r)
    b["x"], b["y"] = G.seat_near(r, b, e["x"] - 70, 150)
    assert b.get("wall")
    assert R.rig_texts("Box rig", r) == before
    doc, lay = R.apply_rig({"schema": 1, "current": None, "chains": {}}, R.empty_layout(), "Box rig", r)
    assert lay["rigs"]["Box rig"]["nodes"][1]["wall"] == b["wall"]          # stored in chain_layout.json
    assert R.check_layout(lay)["rigs"]["Box rig"]["nodes"][1]["wall"] == b["wall"]


def test_a_part_already_in_its_seat_keeps_its_stored_place():
    r, b, e = rig_with_box()
    b["x"], b["y"] = G.seat_near(r, b, e["x"] + 0.4844 * 400 - 70, e["y"] - 32)
    b["wall"]["t"] = 0.4844
    G.reseat(r)
    for _ in range(5):
        G.settle_walls(r)
    assert b["wall"]["t"] == 0.4844
