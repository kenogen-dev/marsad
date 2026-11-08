"""Relay 016: right-click menus at the pointer, top-to-bottom building (ports, Tidy, templates), dragging a wire
moves it, and the window's depth (elevation levels, pressable buttons, text kept readable). Drawing only: every
recorded text stays as it was. Made-up rigs; no Qt."""
from __future__ import annotations

import copy
import json

import pytest

from marsad_desktop import geometry as G
from marsad_desktop import rig as R
from marsad_desktop import theme as T


# ---------------------------------------------------------------- helpers
def column(down: bool):
    """antenna > LNA > splitter > two radios, built in the given direction (only the layout differs)."""
    rig = R.new_rig()
    if down:
        rig["dir"] = "down"
    pos = {"a": (0, 0), "l": (200, 0), "s": (400, 0), "x": (640, -100), "v": (640, 100)}
    if down:
        pos = {k: (y * 1.6, x / 2) for k, (x, y) in pos.items()}
    a = R.new_node(rig, "antenna", *pos["a"])
    a["part"].update(name="antenna", detail="dipole")
    l = R.new_node(rig, "lna", *pos["l"])
    s = R.new_node(rig, R.SPLITTER, *pos["s"])
    x = R.new_node(rig, "sdr", *pos["x"])
    x["part"].update(name="radio B", rx="rx2")
    v = R.new_node(rig, "sdr", *pos["v"])
    v["part"].update(name="radio A", rx="rx1")
    R.connect(rig, a["id"], 0, l["id"])
    R.wire_of(rig, a["id"], 0)["cables"] = [R.new_cable("coax")]
    R.connect(rig, l["id"], 0, s["id"])
    R.connect(rig, s["id"], 0, x["id"])
    R.connect(rig, s["id"], 1, v["id"])
    return rig, {"a": a, "l": l, "s": s, "x": x, "v": v}


def texts(rig):
    return R.rig_texts("Rig", rig)


def boxes(rig, pos):
    out = []
    for n in rig["nodes"]:
        x, y = pos.get(n["id"], (n["x"], n["y"]))
        out.append((x, y, *G.size(n)))
    return out


def overlapping(rects):
    return [(a, b) for i, a in enumerate(rects) for b in rects[i + 1:] if G._overlap(a, b)]


# ---------------------------------------------------------------- 1. right-click menus at the pointer
AREA = (0, 0, 1499, 899)


def test_menu_opens_below_right_of_the_pointer():
    x, y = G.menu_place(400, 300, 220, 260, AREA)
    assert (x, y) == (400 + G.MENU_OFFSET, 300 + G.MENU_OFFSET)
    assert x > 400 and y > 300                       # never over what was clicked


def test_menu_flips_only_at_the_window_edge():
    x, y = G.menu_place(1400, 300, 220, 260, AREA)   # would run off the right: opens to the pointer's left
    assert x + 220 <= 1400 and y == 300 + G.MENU_OFFSET
    x, y = G.menu_place(400, 800, 220, 260, AREA)    # off the bottom: opens above the pointer
    assert y + 260 <= 800 and x == 400 + G.MENU_OFFSET
    x, y = G.menu_place(1400, 800, 220, 260, AREA)   # the corner: both
    assert x + 220 <= 1400 and y + 260 <= 800


def test_a_menu_too_big_for_either_side_is_held_inside_the_window():
    x, y = G.menu_place(100, 450, 220, 880, AREA)
    assert 0 <= y and y + 880 - 1 <= 899


def _backdrops():
    return ["#FFFFFF", "#000000"] + [c for _n, c in T.SWATCHES] + [v for v in T.C.values() if T.is_hex(v)]


def test_menus_are_more_see_through_and_their_text_keeps_4_5_to_1_over_anything():
    assert T.MENU_ALPHA < T.FLOAT_ALPHA
    for bd in _backdrops():
        bg = T._mix(bd, T.C["menu"], T.MENU_ALPHA)
        assert T.contrast(T.C["text"], bg) >= 4.5, bd
        assert T.contrast(T.C["menu_muted"], bg) >= 4.5, bd


# ---------------------------------------------------------------- 2. top to bottom
def test_direction_is_left_to_right_unless_stored():
    rig, _ = column(False)
    assert G.direction(rig) == "right" and "dir" not in rig
    rig["dir"] = "down"
    assert G.direction(rig) == "down"


def test_top_to_bottom_ports_sit_on_the_bottom_and_the_top():
    rig, n = column(True)
    am = G.anchors(rig)
    assert am[(n["a"]["id"], "out", 0)][2] == "bottom"
    assert am[(n["l"]["id"], "in", 0)][2] == "top"
    assert am[(n["l"]["id"], "out", 0)][2] == "bottom"
    for r in ("x", "v"):
        assert am[(n[r]["id"], "in", 0)][2] == "top"
    lone = R.new_node(rig, "filter", 2000, 2000)        # an unwired part waits with its input on top, output below
    am = G.anchors(rig)
    assert am[(lone["id"], "in", 0)][2] == "top" and am[(lone["id"], "out", 0)][2] == "bottom"


def test_the_same_rig_left_to_right_keeps_its_sides():
    rig, n = column(False)
    am = G.anchors(rig)
    assert am[(n["a"]["id"], "out", 0)][2] == "right"
    assert am[(n["l"]["id"], "in", 0)][2] == "left"


def test_a_free_input_on_a_pc_waits_on_top_in_a_top_to_bottom_rig():
    rig, n = column(True)
    pc = R.new_node(rig, "pc", 300, 600)
    R.connect(rig, n["x"]["id"], 0, pc["id"])
    am = G.anchors(rig)
    free = [k for k in am if k[0] == pc["id"] and k[1] == "in" and k[2] == R.free_input(rig, pc["id"])]
    assert free and am[free[0]][2] == "top"


def test_side_by_side_parts_join_from_the_side_top_to_bottom():
    a = {"id": "a", "x": 0, "y": 0, "part": {"type": "lna"}}
    b = {"id": "b", "x": 300, "y": 0, "part": {"type": "lna"}}
    assert G.side_toward(a, b, down=True) == "right"
    c = {"id": "c", "x": 0, "y": 200, "part": {"type": "lna"}}
    assert G.side_toward(a, c, down=True) == "bottom"
    assert G.side_toward(c, a, down=True) == "top"


def test_shapes_point_along_the_flow():
    assert G.oriented("triangle", True) == "triangle_down"
    assert G.oriented("trapezoid", True) == "trapezoid_down"
    assert G.oriented("triangle", False) == "triangle"
    assert G.oriented("rounded", True) == "rounded"
    tri = G.outline("triangle_down", 100, 60)
    assert max(tri, key=lambda p: p[1]) == (50, 60)        # the point is at the bottom


def test_power_inputs_move_to_a_side_top_to_bottom():
    rig, n = column(True)
    p = R.new_power(rig, "usbc", -300, 100)
    R.connect_power(rig, p["id"], n["l"]["id"])
    inputs, _out = G.power_points(rig)
    assert inputs[n["l"]["id"]][2] in ("left", "right")
    rig.pop("dir")
    inputs, _out = G.power_points(rig)
    assert inputs[n["l"]["id"]][2] in ("top", "bottom")


def test_tidy_top_to_bottom_lays_rows_with_branches_side_by_side():
    rig, n = column(True)
    pos = G.tidy(rig)
    ys = [pos[n[k]["id"]][1] for k in ("a", "l", "s", "x")]
    assert ys == sorted(ys) and len(set(ys)) == 4           # one row per step from the source, going down
    assert pos[n["x"]["id"]][1] == pos[n["v"]["id"]][1]     # the two branches side by side
    assert pos[n["x"]["id"]][0] != pos[n["v"]["id"]][0]
    sx = pos[n["s"]["id"]][0] + G.size(n["s"])[0] / 2
    mid = (pos[n["x"]["id"]][0] + pos[n["v"]["id"]][0] + G.size(n["x"])[0]) / 2
    assert abs(sx - mid) <= R.GRID                           # the splitter above the middle of what it feeds
    assert not overlapping(boxes(rig, pos))


def test_tidy_left_to_right_is_unchanged_by_the_direction_code():
    rig, n = column(False)
    pos = G.tidy(rig)
    xs = [pos[n[k]["id"]][0] for k in ("a", "l", "s", "x")]
    assert xs == sorted(xs) and len(set(xs)) == 4
    assert pos[n["x"]["id"]][0] == pos[n["v"]["id"]][0]


def test_tidy_all_top_to_bottom_keeps_blocks_apart():
    rig, n = column(True)
    R.make_group(rig, [n["x"]["id"], n["v"]["id"]])
    e = R.new_enclosure(rig, -400, -400, 400, 240)
    lone = R.new_node(rig, "filter", -300, -300)
    out, rects = G.tidy_all(rig)
    trial = copy.deepcopy(rig)
    for b in R._boxes(trial):
        if b["id"] in out:
            b["x"], b["y"] = out[b["id"]]
    for eid, (x, y, w, h) in rects.items():
        R.box(trial, eid).update(x=x, y=y, w=w, h=h)
    rs = [G.rect(trial, b["id"]) for b in trial["nodes"]]
    assert not overlapping(rs)
    assert lone["id"] in G.inside(trial, R.box(trial, e["id"]))   # the enclosure carried what it owns


def test_changing_direction_changes_no_recorded_text():
    rig, _ = column(False)
    before = texts(rig)
    rig["dir"] = "down"
    assert texts(rig) == before
    pos = G.tidy(rig)
    for nd in rig["nodes"]:
        nd["x"], nd["y"] = pos[nd["id"]]
    assert texts(rig) == before


@pytest.mark.parametrize("key", list(R.TEMPLATES))
def test_templates_come_in_both_directions_with_the_same_text(key):
    right = R.template(key)
    down = R.template(key, down=True)
    assert "dir" not in right and (down.get("dir") == "down")
    assert texts(right) == texts(down)
    assert not overlapping(boxes(down, {}))
    if right["wires"]:
        am = G.anchors(down)
        w = down["wires"][0]
        assert am[(w["from"], "out", w["port"])][2] == "bottom"


def test_the_layout_keeps_top_to_bottom_and_old_drawings_stay_byte_for_byte():
    rig, _ = column(False)
    lay = R.empty_layout()
    lay["rigs"]["Rig"] = {"nodes": rig["nodes"], "wires": rig["wires"], "chains": {}}
    before = json.dumps(lay, sort_keys=True)
    assert json.dumps(R.check_layout(copy.deepcopy(lay)), sort_keys=True) == before
    lay["rigs"]["Rig"]["dir"] = "down"
    assert R.check_layout(copy.deepcopy(lay))["rigs"]["Rig"]["dir"] == "down"
    lay["rigs"]["Rig"]["dir"] = "sideways"
    with pytest.raises(ValueError):
        R.check_layout(lay)


def test_saving_carries_the_direction_and_the_same_texts():
    rig, _ = column(True)
    doc, lay = R.apply_rig({"chains": {}}, R.empty_layout(), "Rig", rig)
    assert lay["rigs"]["Rig"]["dir"] == "down"
    flat, _ = column(False)
    doc2, _ = R.apply_rig({"chains": {}}, R.empty_layout(), "Rig", flat)
    assert doc["chains"] == doc2["chains"]


# ---------------------------------------------------------------- 4. dragging a wire moves the wire
A, B = (0.0, 0.0, "right"), (300.0, 100.0, "left")


def _ends_kept(a, b, points, style, square=False):
    kind, pts = G.route_via(a, b, points, style, square=square)[:2]
    if kind == "poly":
        assert tuple(pts[0]) == (a[0], a[1]) and tuple(pts[-1]) == (b[0], b[1])
    return kind, pts


def test_dragging_a_right_angled_wires_upright_segment_moves_it_sideways():
    _k, before = G.route_via(A, B, [], "angled")
    mid_x = before[2][0]
    new = G.move_wire(A, B, [], "angled", False, (mid_x, 50), 41, 17, snap=R.GRID)
    _k, after = _ends_kept(A, B, new, "angled")
    uprights = [p for p, q in zip(after, after[1:]) if p[0] == q[0] and p[1] != q[1]]
    assert uprights and uprights[0][0] == R.snap(mid_x + 41)     # moved across itself only, on the grid


def test_dragging_a_level_segment_next_to_a_part_keeps_the_part_end_square():
    new = G.move_wire(A, B, [], "angled", False, (80, 0), 0, -60, snap=R.GRID)
    _k, after = _ends_kept(A, B, new, "angled")
    assert after[1] == (20.0, 0.0)                              # the part's own stub still leaves straight out
    assert any(p[1] == -60 for p in after)


def test_dragging_a_curved_wire_pulls_it_out_where_it_was_grabbed():
    new = G.move_wire(A, B, [], "curved", False, (150, 50), 10, 30, snap=None)
    assert new == [[160, 80]]
    _ends_kept(A, B, new, "curved")


def test_dragging_between_two_bend_points_moves_both():
    new = G.move_wire(A, B, [[100, 0], [200, 100]], "curved", False, (150, 50), 20, 40, snap=R.GRID)
    assert new == [[120, 40], [220, 140]]


def test_moving_a_wire_changes_no_recorded_text():
    rig, n = column(False)
    before = texts(rig)
    w = R.wire_of(rig, n["a"]["id"], 0)
    am = G.anchors(rig)
    a, b = am[(w["from"], "out", 0)], am[(w["to"], "in", 0)]
    w["points"] = G.move_wire(a, b, [], "angled", False, ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2), 0, 80, R.GRID)
    assert w["points"] and texts(rig) == before
    R.check_layout({"rigs": {"Rig": {"nodes": rig["nodes"], "wires": rig["wires"], "chains": {}}}})


# ---------------------------------------------------------------- 5. depth
def test_each_elevation_is_a_step_lighter_than_the_one_below():
    lums = [T.luminance(T.C["elev_" + lv]) for lv in T.ELEVATIONS]
    assert lums == sorted(lums) and len(set(lums)) == len(lums)


def test_shadows_and_highlights_grow_with_height():
    depth = [max((dy for dy, _s, _a in T.SHADOWS[lv]), default=0) for lv in T.ELEVATIONS]
    assert depth == sorted(depth) and depth[0] == 0 and depth[-1] > depth[1]
    hl = [T.HIGHLIGHT_ALPHA[lv] for lv in T.ELEVATIONS]
    assert hl == sorted(hl)


def test_text_stays_readable_on_every_level_and_on_the_buttons():
    for lv in T.ELEVATIONS:
        assert T.contrast(T.C["text"], T.C["elev_" + lv]) >= 4.5
    for fill in ("btn_top", "btn_bottom", "btn_hover_top", "btn_hover_bottom", "btn_sunk", "seg_top", "seg_bottom"):
        assert T.contrast(T.C["text"], T.C[fill]) >= 4.5, fill
    for fill in ("primary_top", "primary_bottom", "primary_hover_top", "primary_hover_bottom", "primary_pressed"):
        assert T.contrast(T.DARK_TEXT, T.C[fill]) >= 4.5, fill


def test_the_focus_ring_stands_out():
    for bg in ("elev_ground", "elev_surface", "btn_top"):
        assert T.contrast(T.C["focus"], T.C[bg]) >= 3.0


def test_the_style_sheet_names_its_depth_from_the_theme():
    css = T.stylesheet()
    for v in ("btn_top", "btn_sunk", "primary_top", "focus", "seg_top", "elev_overlay", "edge_top"):
        assert T.C[v] in css, v
    assert "qlineargradient" in css and ":pressed" in css and ":focus" in css
