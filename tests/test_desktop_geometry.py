"""The Chain screen's geometry: wire sides, curves, part sizes and Tidy.

None of it may change a recorded text; these tests also pin that."""
import copy

from marsad_desktop import geometry as G
from marsad_desktop import rig as R


def _row(types):
    return R.rig_from_parts([{"type": t, "name": t, "detail": ""} for t in types])


def _split_rig():
    rig = R.new_rig()
    a = R.new_node(rig, "antenna", 0, 0)
    s = R.new_node(rig, R.SPLITTER, 200, 0)
    up = R.new_node(rig, "sdr", 400, -120)
    down = R.new_node(rig, "sdr", 400, 120)
    assert R.connect(rig, a["id"], 0, s["id"]) is None
    assert R.connect(rig, s["id"], 0, down["id"]) is None      # port 0 goes to the LOWER radio
    assert R.connect(rig, s["id"], 1, up["id"]) is None
    return rig, a, s, up, down


def test_size_defaults_and_limits():
    n = {"id": "n1", "part": {"type": "coax"}, "x": 0, "y": 0}
    assert G.size(n) == (R.PART_W, R.PART_H)
    n.update(w=10, h=9999)
    assert G.size(n) == (G.MIN_W, G.MAX_H)


def test_side_toward_reads_left_to_right_first():
    a = {"id": "a", "part": {"type": "coax"}, "x": 0, "y": 0}
    assert G.side_toward(a, dict(a, x=200)) == "right"
    assert G.side_toward(a, dict(a, x=-200)) == "left"
    assert G.side_toward(a, dict(a, y=200)) == "bottom"
    assert G.side_toward(a, dict(a, y=-200)) == "top"
    assert G.side_toward(a, dict(a, x=200, y=60)) == "right"    # mostly across


def test_straight_row_wires_leave_right_and_arrive_left():
    rig = _row(["antenna", "filter", "sdr"])
    am = G.anchors(rig)
    assert am[("n1", "out", 0)][2] == "right"
    assert am[("n2", "in", 0)][2] == "left"
    assert ("n1", "in", 0) not in am             # a source has no input
    assert am[("n3", "out", 0)][2] == "right"    # unwired output waits on the right


def test_a_part_below_is_joined_from_the_bottom():
    rig = _row(["antenna", "coax"])
    rig["nodes"][1].update(x=0, y=300)
    am = G.anchors(rig)
    assert am[("n1", "out", 0)][2] == "bottom"
    assert am[("n2", "in", 0)][2] == "top"


def test_splitter_outputs_are_ordered_by_where_their_wires_go():
    rig, a, s, up, down = _split_rig()
    am = G.anchors(rig)
    p0, p1 = am[(s["id"], "out", 0)], am[(s["id"], "out", 1)]
    assert p0[2] == p1[2] == "right"
    assert p1[1] < p0[1]          # port 1 feeds the upper radio, so it sits higher: no crossing


def test_curve_leaves_square_to_the_edge():
    c1, c2 = G.curve((0, 0, "right"), (200, 100, "left"))
    assert c1[1] == 0 and c1[0] > 0
    assert c2[1] == 100 and c2[0] < 200
    c1, _ = G.curve((0, 0, "bottom"), (0, 300, "top"))
    assert c1[0] == 0 and c1[1] > 0


def test_tidy_puts_a_row_in_columns_on_one_line():
    rig = _row(["antenna", "filter", "lna", "sdr"])
    for i, n in enumerate(rig["nodes"]):
        n.update(x=(3 - i) * 50, y=i * 70)
    pos = G.tidy(rig)
    xs = [pos[n["id"]][0] for n in rig["nodes"]]
    assert xs == sorted(xs) and len(set(xs)) == 4
    assert len({pos[n["id"]][1] for n in rig["nodes"]}) == 1
    assert all(v % R.GRID == 0 for xy in pos.values() for v in xy)


def test_tidy_keeps_branch_order_and_centres_the_splitter():
    rig, a, s, up, down = _split_rig()
    pos = G.tidy(rig)
    assert pos[up["id"]][1] < pos[down["id"]][1]          # the upper branch stays upper
    assert pos[up["id"]][1] < pos[s["id"]][1] < pos[down["id"]][1]
    assert pos[a["id"]][0] < pos[s["id"]][0] < pos[up["id"]][0] == pos[down["id"]][0]


def test_tidy_puts_loose_parts_under_the_rig():
    rig = _row(["antenna", "coax"])
    loose = R.new_node(rig, "pc", -500, -500)
    pos = G.tidy(rig)
    assert pos[loose["id"]][1] > max(pos["n1"][1], pos["n2"][1])


def test_look_never_changes_the_recorded_text():
    rig, *_ = _split_rig()
    before = R.rig_texts("split", rig)
    moved = copy.deepcopy(rig)
    for n, (x, y) in G.tidy(moved).items():
        node = next(m for m in moved["nodes"] if m["id"] == n)
        node.update(x=x, y=y, w=260, h=120)
    assert R.rig_texts("split", moved) == before


def test_sizes_survive_the_layout_check():
    rig = _row(["antenna", "coax"])
    rig["nodes"][0].update(w=240, h=100)
    lay = R.check_layout({"rigs": {"r": dict(rig, chains={})}})
    assert lay["rigs"]["r"]["nodes"][0]["w"] == 240


def test_right_angle_wires_only_turn_square():
    kind, pts = G.route((0, 0, "right"), (300, 120, "left"), "angled")
    assert kind == "poly" and pts[0] == (0, 0) and pts[-1] == (300, 120)
    assert all(p[0] == q[0] or p[1] == q[1] for p, q in zip(pts, pts[1:]))
    kind, pts = G.route((0, 0, "bottom"), (200, 300, "left"), "angled")
    assert all(p[0] == q[0] or p[1] == q[1] for p, q in zip(pts, pts[1:]))
    assert G.route((0, 0, "right"), (10, 10, "left"), "straight") == ("poly", [(0, 0), (10, 10)])
    assert G.route((0, 0, "right"), (100, 0, "left"))[0] == "cubic"


def test_parts_inside_an_enclosure_are_those_whose_centre_it_holds():
    rig = _row(["antenna", "filter", "sdr"])            # x = 0, 180, 360
    e = R.new_enclosure(rig, 340, -40, 200, 160)
    assert G.inside(rig, e) == ["n3"]
    e["x"] = 140
    assert G.inside(rig, e) == ["n2"]


def test_tidy_keeps_an_enclosures_parts_inside_it():
    rig = _row(["antenna", "filter", "lna", "sdr"])
    for i, n in enumerate(rig["nodes"]):
        n.update(x=(3 - i) * 200, y=i * 90)               # scrambled
    radio = rig["nodes"][3]
    e = R.new_enclosure(rig, radio["x"] - 40, radio["y"] - 60, 260, 200)
    assert G.inside(rig, e) == [radio["id"]]
    pos = G.tidy(rig)
    x, y, w, h = G.tidy_enclosures(rig, pos)[e["id"]]
    moved = dict(e, x=x, y=y, w=w, h=h)
    assert G.inside(rig, moved, pos) == [radio["id"]]
    assert all(v % R.GRID == 0 for v in (x, y, w, h))
    empty = R.new_enclosure(rig, 3000, 3000)
    assert empty["id"] not in G.tidy_enclosures(rig, pos)   # an empty box stays where it is


def test_tidy_moves_a_group_as_one_piece():
    rig = _split_rig()[0]
    ids = [n["id"] for n in rig["nodes"]]
    for n in rig["nodes"]:                                    # scatter the rig
        n["x"], n["y"] = (int(n["id"][1:]) * 140) % 900, (int(n["id"][1:]) * 220) % 700
    group = ids[1:3]
    R.make_group(rig, group)
    before = {n["id"]: (n["x"], n["y"]) for n in rig["nodes"]}
    pos, _ = G.tidy_all(rig)
    plain = G.tidy(rig)
    rel = lambda p, a, b: (p[b][0] - p[a][0], p[b][1] - p[a][1])
    assert rel(pos, *group) == rel(before, *group)            # members keep their places to each other
    assert pos[group[0]] == plain[group[0]]                    # the group follows its first tidied part
    # Every other part goes to its tidy column; one in the rigid group's way is moved down (relay 005).
    assert all(pos[i][0] == plain[i][0] and pos[i][1] >= plain[i][1] for i in ids if i not in group)


def test_a_group_half_inside_an_enclosure():
    """The rule for the awkward case: the enclosure is re-drawn round the parts that were
    in it, and the group moves as one piece, so both hold after Tidy."""
    rig = _split_rig()[0]
    ids = [n["id"] for n in rig["nodes"]]
    a, b = ids[0], ids[-1]
    na = next(n for n in rig["nodes"] if n["id"] == a)
    e = R.new_enclosure(rig, na["x"] - 20, na["y"] - 40, 200, 140)
    assert G.inside(rig, e) == [a]
    R.make_group(rig, [a, b])                                  # a is inside, b is not
    pos, rects = G.tidy_all(rig)
    moved = dict(e, **dict(zip("xywh", rects[e["id"]])))
    assert a in G.inside(rig, moved, pos)
    na2, nb2 = pos[a], pos[b]
    nb = next(n for n in rig["nodes"] if n["id"] == b)
    assert (nb2[0] - na2[0], nb2[1] - na2[1]) == (nb["x"] - na["x"], nb["y"] - na["y"])


def test_a_grouped_enclosure_moves_with_its_group_unresized():
    rig = _split_rig()[0]
    ids = [n["id"] for n in rig["nodes"]]
    e = R.new_enclosure(rig, 2000, 2000, 300, 200)
    R.make_group(rig, [ids[0], e["id"]])
    pos, rects = G.tidy_all(rig)
    n0 = rig["nodes"][0]
    dx, dy = pos[ids[0]][0] - n0["x"], pos[ids[0]][1] - n0["y"]
    assert rects[e["id"]] == (2000 + dx, 2000 + dy, 300, 200)


# ---------------------------------------------------------------- relay 007
def test_points_sit_on_each_shapes_outline():
    rig = R.new_rig()
    lna = R.new_node(rig, "lna", 0, 0)
    lna["shape"] = "triangle"                                # a triangle pointing right
    assert G.on_outline(lna, 140, 32) == (140.0, 32.0)       # the output at its tip
    x, y = G.on_outline(lna, 70, 0)                          # the top: on the slanted side
    assert abs(y - (32 - 32 * x / 140)) < 1e-6
    sp = R.new_node(rig, R.SPLITTER, 0, 200)
    sp["shape"] = "trapezoid"                                # a trapezoid, wide toward the outputs
    am = G.anchors({"nodes": [sp], "wires": []})
    assert am[(sp["id"], "in", 0)][:2] == (0.0, 232.0)
    assert {am[(sp["id"], "out", k)][0] for k in (0, 1)} == {140.0}
    lna["shape"] = "rect"
    assert G.on_outline(lna, 70, 0) == (70, 0)               # a rectangle: the box itself


def test_resize_from_every_handle_with_snap_and_proportions():
    r = (0, 0, 140, 64)
    assert G.resize(r, "br", 40, 40) == (0, 0, 180, 104)
    assert G.resize(r, "tl", -40, -40) == (-40, -40, 180, 104)
    assert G.resize(r, "t", 13, -36, snap=20) == (0, -40, 140, 104)
    assert G.resize(r, "l", 300, 0) == (40, 0, 100, 64)           # held at the smallest width
    x, y, w, h = G.resize(r, "br", 70, 0, keep_ratio=True)
    assert abs(w / h - 140 / 64) < 0.02
    x, y, w, h = G.resize(r, "b", 0, 64, keep_ratio=True)
    assert h == 128 and w == 280 and x == -70                      # an edge drag keeps it centred across


def test_power_sources_resize_and_wires_follow():
    rig = R.new_rig()
    p = R.new_power(rig, "usbc", 0, 0)
    assert G.power_size(p) == (R.POWER_W, R.POWER_H)
    p["w"], p["h"] = 200, 60
    assert G.rect(rig, p["id"]) == (0, 0, 200, 60)
    assert G.power_points(rig)[1][p["id"]][:2] == (200, 30)
