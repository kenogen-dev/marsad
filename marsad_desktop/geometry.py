"""Where things sit on the Chain grid: part sizes, which side of a part a wire
leaves from, the curve it draws, and the Tidy arrangement.

Pure Python and no Qt, so it can be tested on its own. Nothing here touches the
recorded text: a drawing's look never changes what a run writes.
"""
from __future__ import annotations

import math

from marsad import chain as engine_chain

from . import rig as R

MIN_W, MIN_H = 100, 60                 # smallest a part can be resized to
MAX_W, MAX_H = 480, 320
SIDES = ("right", "bottom", "left", "top")
OPPOSITE = {"right": "left", "left": "right", "top": "bottom", "bottom": "top"}
NORMAL = {"right": (1, 0), "left": (-1, 0), "top": (0, -1), "bottom": (0, 1)}
GAP_X, GAP_Y = 60, 40                  # Tidy's space between columns and rows


def size(node: dict) -> tuple[int, int]:
    """A part's box, (width, height); parts never resized use the standard box."""
    w = node.get("w") or R.PART_W
    h = node.get("h") or R.PART_H
    return int(min(max(w, MIN_W), MAX_W)), int(min(max(h, MIN_H), MAX_H))


POWER_MIN_W, POWER_MIN_H = 90, 36


def power_size(b: dict) -> tuple[int, int]:
    """A power source's box: resizable since relay 007, the standard tag size until it is."""
    w = b.get("w") or R.POWER_W
    h = b.get("h") or R.POWER_H
    return int(min(max(w, POWER_MIN_W), MAX_W)), int(min(max(h, POWER_MIN_H), MAX_H))


def centre(node: dict) -> tuple[float, float]:
    w, h = size(node)
    return node["x"] + w / 2, node["y"] + h / 2


def is_source(node: dict) -> bool:
    return bool(engine_chain.PART_TYPES.get(node["part"]["type"], {}).get("source"))


def side_toward(a: dict, b: dict) -> str:
    """The side of part a that faces part b. A part directly above or below (the two
    boxes overlap left to right) is joined top to bottom; anything else is joined
    from the side, since chains read left to right."""
    (ax, ay), (bx, by) = centre(a), centre(b)
    (aw, ah), (bw, bh) = size(a), size(b)
    dx, dy = bx - ax, by - ay
    overlap_across = abs(dx) < (aw + bw) / 2
    if overlap_across and abs(dy) >= (ah + bh) / 2:
        return "bottom" if dy > 0 else "top"
    if dx == 0 and dy == 0:
        return "right"
    if overlap_across:            # boxes on top of each other: whichever way is further
        if abs(dy) > abs(dx):
            return "bottom" if dy > 0 else "top"
    return "right" if dx >= 0 else "left"


def side_to_point(node: dict, x: float, y: float) -> str:
    """The side of a part that faces a point (a wire being dragged), by the same rule."""
    cx, cy = centre(node)
    w, h = size(node)
    dx, dy = x - cx, y - cy
    if abs(dx) <= w / 2 and (abs(dy) > h / 2 or abs(dy) / h > abs(dx) / w):
        return "bottom" if dy > 0 else "top"
    return "right" if dx >= 0 else "left"


def mid_side(node: dict, side: str) -> tuple[float, float]:
    return _point_on(node, side, 0, 1)


def _point_on(node: dict, side: str, k: int, n: int) -> tuple[float, float]:
    w, h = size(node)
    f = (k + 1) / (n + 1)
    x, y = node["x"], node["y"]
    pt = {"right": (x + w, y + h * f), "left": (x, y + h * f),
          "top": (x + w * f, y), "bottom": (x + w * f, y + h)}[side]
    return on_outline(node, *pt)


# ---------------------------------------------------------------- shapes (relay 007)
# A node's outline as a polygon in its own box (0..w, 0..h); None for the shapes whose outline is
# the box itself (rectangle, rounded box, an adapter's pipe, whose ends are its side midpoints).
_POLY: dict = {}


def outline(shape: str, w: float, h: float):
    key = (shape, round(w), round(h))
    if key in _POLY:
        return _POLY[key]
    if shape == "triangle":                     # points downstream, to the right
        poly = [(0, 0), (w, h / 2), (0, h)]
    elif shape == "trapezoid":                  # widens toward its outputs, on the right
        poly = [(0, h * 0.22), (w, 0), (w, h), (0, h * 0.78)]
    elif shape == "hexagon":
        i = min(w * 0.2, h * 0.5)
        poly = [(0, h / 2), (i, 0), (w - i, 0), (w, h / 2), (w - i, h), (i, h)]
    elif shape in ("circle", "pill"):
        poly = []
        r = min(w, h) / 2
        for k in range(48):
            a = 2 * math.pi * k / 48
            if shape == "circle":
                poly.append((w / 2 + w / 2 * math.cos(a), h / 2 + h / 2 * math.sin(a)))
            else:                               # a box with half-circle ends
                cx = w - r if math.cos(a) >= 0 else r
                poly.append((cx + r * math.cos(a), h / 2 + r * math.sin(a)))
    else:
        poly = None
    _POLY[key] = poly
    return poly


def on_outline(node: dict, px: float, py: float) -> tuple[float, float]:
    """Where a point on a part's box falls on its shape: along the line from the part's centre
    to it, the first place that line leaves the outline. On a box-shaped part, the point itself."""
    w, h = size(node)
    poly = outline(R.shape_of(node), w, h)
    if not poly:
        return px, py
    x0, y0 = node["x"], node["y"]
    cx, cy = w / 2, h / 2
    dx, dy = px - x0 - cx, py - y0 - cy
    best = None
    for (ax, ay), (bx, by) in zip(poly, poly[1:] + poly[:1]):
        ex, ey = bx - ax, by - ay
        den = dx * ey - dy * ex
        if abs(den) < 1e-9:
            continue
        t = ((ax - cx) * ey - (ay - cy) * ex) / den
        u = ((ax - cx) * dy - (ay - cy) * dx) / den
        if t > 1e-9 and -1e-9 <= u <= 1 + 1e-9 and (best is None or t < best):
            best = t
    if best is None:
        return px, py
    return x0 + cx + dx * best, y0 + cy + dy * best


# ---------------------------------------------------------------- resizing from any handle (relay 007)
HANDLES = ("tl", "t", "tr", "r", "br", "b", "bl", "l")


def resize(rect: tuple, handle: str, dx: float, dy: float, *, keep_ratio: bool = False, snap: int | None = None,
           min_w: float = MIN_W, min_h: float = MIN_H, max_w: float = MAX_W, max_h: float = MAX_H) -> tuple:
    """A box (x, y, w, h) dragged by one of its eight handles by (dx, dy). The opposite edge stays
    put; edges snap to the grid when `snap` is given; with keep_ratio (Shift) the box keeps its
    proportions. Sizes are held between the limits."""
    x, y, w, h = rect
    x1, y1, x2, y2 = x, y, x + w, y + h
    if "l" in handle:
        x1 += dx
    if "r" in handle:
        x2 += dx
    if "t" in handle:
        y1 += dy
    if "b" in handle:
        y2 += dy
    if snap:
        x1, x2 = (R.snap(x1, snap) if "l" in handle else x1), (R.snap(x2, snap) if "r" in handle else x2)
        y1, y2 = (R.snap(y1, snap) if "t" in handle else y1), (R.snap(y2, snap) if "b" in handle else y2)
    nw, nh = x2 - x1, y2 - y1
    if keep_ratio and w and h:
        ratio = w / h
        if handle in ("t", "b"):
            nw = nh * ratio
        elif handle in ("l", "r"):
            nh = nw / ratio
        elif abs(nw / w) >= abs(nh / h):
            nh = nw / ratio
        else:
            nw = nh * ratio
    nw = min(max(nw, min_w), max_w)
    nh = min(max(nh, min_h), max_h)
    if keep_ratio and w and h:                  # the limits must not break the proportions either
        ratio = w / h
        nw = min(max(nw, min_w, min_h * ratio), max_w, max_h * ratio)
        nh = nw / ratio
    nx = x2 - nw if "l" in handle else x1
    ny = y2 - nh if "t" in handle else y1
    if handle in ("t", "b") and keep_ratio:     # an edge drag keeps the box centred across
        nx = x + (w - nw) / 2
    if handle in ("l", "r") and keep_ratio:
        ny = y + (h - nh) / 2
    return (round(nx), round(ny), round(nw), round(nh))


# ---------------------------------------------------------------- mating (relay 007)
MATE_GAP = 14                          # px between mated points: room for the connector stub


def mate_target(point: tuple) -> tuple[float, float]:
    """Where a part's point must sit to mate with `point` = (x, y, side): a small gap out from it."""
    nx, ny = NORMAL[point[2]]
    return point[0] + nx * MATE_GAP, point[1] + ny * MATE_GAP


def anchors(rig: dict) -> dict:
    """Every connection point on the grid: {(node id, "in"|"out", port): (x, y, side)}.

    A wired point sits on the side that faces the part at its other end; points
    sharing a side are spread along it in the order of their far ends, so wires
    leaving one side do not cross each other. Unwired outputs wait on the right,
    an unwired input on the left; a source has no input."""
    nodes = {n["id"]: n for n in rig["nodes"]}
    on_side: dict[tuple[str, str], list] = {}      # (node, side) -> [(sort key, key)]

    def put(nid, side, key, far):
        axis = 1 if side in ("left", "right") else 0
        sort = centre(far)[axis] if far is not None else 1e9 + key[2]
        on_side.setdefault((nid, side), []).append((sort, key))

    fixed = {nid: R.port_sides(n) for nid, n in nodes.items()}      # adapters with a chosen shape

    def side_of(n, kind, port, default):
        f = fixed.get(n["id"])
        if not f:
            return default
        return f["in"] if kind == "in" else f["out"][min(port, len(f["out"]) - 1)]

    wired_in, wired_out = set(), set()
    for w in rig["wires"]:
        a, b = nodes.get(w["from"]), nodes.get(w["to"])
        if a is None or b is None:
            continue
        s = side_toward(a, b)
        sa = side_of(a, "out", w["port"], s)
        # The far end faces the near end's point: a fixed side on one end turns the other.
        sb = side_of(b, "in", 0, OPPOSITE[sa] if fixed.get(a["id"]) and not fixed.get(b["id"]) else OPPOSITE[s])
        if fixed.get(b["id"]) and not fixed.get(a["id"]):
            sa = OPPOSITE[sb]
        put(a["id"], sa, (a["id"], "out", w["port"]), b)
        put(b["id"], sb, (b["id"], "in", 0), a)
        wired_out.add((a["id"], w["port"]))
        wired_in.add(b["id"])
    for nid, n in nodes.items():
        for p in range(R.out_ports(n)):
            if (nid, p) not in wired_out:
                put(nid, side_of(n, "out", p, "right"), (nid, "out", p), None)
        if nid not in wired_in and not is_source(n):
            put(nid, side_of(n, "in", 0, "left"), (nid, "in", 0), None)
    out = {}
    for (nid, side), items in on_side.items():
        items.sort(key=lambda t: (t[0], t[1][1] == "out", t[1][2]))
        for k, (_, key) in enumerate(items):
            x, y = _point_on(nodes[nid], side, k, len(items))
            out[key] = (x, y, side)
    return out


def curve(a: tuple, b: tuple, side_b: str | None = None) -> tuple:
    """A smooth wire from point a = (x, y, side) to b: the two control points of a
    cubic curve that leaves and arrives square to the part's edge. With no side at
    b (a wire still being dragged) it arrives from the direction it is heading."""
    ax, ay, sa = a
    bx, by = b[0], b[1]
    sb = side_b or (b[2] if len(b) > 2 else None)
    dist = math.hypot(bx - ax, by - ay)
    d = min(max(dist * 0.45, 30.0), 160.0)
    nax, nay = NORMAL[sa]
    c1 = (ax + nax * d, ay + nay * d)
    if sb is None:
        c2 = (bx - nax * d * 0.5, by - nay * d * 0.5)
    else:
        nbx, nby = NORMAL[sb]
        c2 = (bx + nbx * d, by + nby * d)
    return c1, c2


def tidy(rig: dict, grid: int = R.GRID) -> dict:
    """A tidy arrangement: {node id: (x, y)}. Each part goes in the column of its
    distance from the source, branches below one another, and a part with several
    outputs sits level with the middle of what it feeds. Parts on no wire line up
    beneath the rest. The current order of branches (top to bottom) is kept."""
    nodes = {n["id"]: n for n in rig["nodes"]}
    if not nodes:
        return {}
    children: dict[str, list] = {nid: [] for nid in nodes}
    parent = {}
    for w in rig["wires"]:
        if w["from"] in nodes and w["to"] in nodes:
            children[w["from"]].append((w["port"], w["to"]))
            parent[w["to"]] = w["from"]
    for nid in children:
        children[nid].sort(key=lambda t: (centre(nodes[t[1]])[1], t[0]))
    wired = set(parent) | {nid for nid, c in children.items() if c}
    roots = sorted((nid for nid in nodes if nid not in parent and nid in wired),
                   key=lambda nid: (centre(nodes[nid])[1], centre(nodes[nid])[0]))
    loose = sorted((nid for nid in nodes if nid not in wired),
                   key=lambda nid: (centre(nodes[nid])[0], centre(nodes[nid])[1]))

    depth, row = {}, {}
    slot = [0.0]

    def place(nid, d, seen):
        if nid in seen:                       # cannot happen in a checked rig; be safe
            return row.get(nid, 0.0)
        seen.add(nid)
        depth[nid] = d
        kids = children[nid]
        if not kids:
            row[nid] = slot[0]
            slot[0] += 1
        else:
            rows = [place(k, d + 1, seen) for _, k in kids]
            row[nid] = (rows[0] + rows[-1]) / 2
        return row[nid]

    seen: set = set()
    for r in roots:
        place(r, 0, seen)
    cols = max(depth.values(), default=-1) + 1
    col_w = [0] * max(cols, 1)
    for nid, d in depth.items():
        col_w[d] = max(col_w[d], size(nodes[nid])[0])
    row_h = max((size(n)[1] for n in nodes.values()), default=R.PART_H) + GAP_Y
    gap_before = [GAP_X] * len(col_w)          # room for the cable labels on wires into a column
    for w in rig["wires"]:
        if w["to"] in depth and depth[w["to"]] > 0:
            d = depth[w["to"]]
            gap_before[d] = max(gap_before[d], R.label_room(w.get("cables")))
    col_x, x = [], 0
    for d, w in enumerate(col_w):
        if d:
            x += gap_before[d]
        col_x.append(x)
        x += w
    out = {}
    for nid in depth:
        h = size(nodes[nid])[1]
        y = row[nid] * row_h + (row_h - GAP_Y - h) / 2
        out[nid] = (R.snap(col_x[depth[nid]], grid), R.snap(y, grid))
    y0 = R.snap(slot[0] * row_h + (GAP_Y if depth else 0), grid)
    x = 0
    for nid in loose:
        out[nid] = (R.snap(x, grid), y0)
        x += size(nodes[nid])[0] + GAP_X
    return out


WIRE_STYLES = ("curved", "angled", "straight")


def route(a: tuple, b: tuple, style: str = "curved", side_b: str | None = None) -> tuple:
    """How a wire is drawn from a = (x, y, side) to b = (x, y[, side]).

    ("cubic", c1, c2) for a curve, or ("poly", [points]) for right angles or a
    straight line. Right angles leave and arrive square to each part's edge."""
    sb = side_b or (b[2] if len(b) > 2 else None)
    if style == "straight":
        return "poly", [(a[0], a[1]), (b[0], b[1])]
    if style != "angled":
        return ("cubic",) + curve(a, b, sb)
    ax, ay, sa = a
    bx, by = b[0], b[1]
    if sb is None:
        sb = OPPOSITE[sa]
    off = 20
    p1 = (ax + NORMAL[sa][0] * off, ay + NORMAL[sa][1] * off)
    p2 = (bx + NORMAL[sb][0] * off, by + NORMAL[sb][1] * off)
    across_a, across_b = sa in ("left", "right"), sb in ("left", "right")
    if across_a and across_b:
        mx = (p1[0] + p2[0]) / 2
        mid = [(mx, p1[1]), (mx, p2[1])]
    elif not across_a and not across_b:
        my = (p1[1] + p2[1]) / 2
        mid = [(p1[0], my), (p2[0], my)]
    elif across_a:
        mid = [(p2[0], p1[1])]
    else:
        mid = [(p1[0], p2[1])]
    pts = [(ax, ay), p1, *mid, p2, (bx, by)]
    out = [pts[0]]
    for p in pts[1:]:                      # drop repeated points
        if p != out[-1]:
            out.append(p)
    return "poly", out


# ---------------------------------------------------------------- enclosures
ENC_PAD, ENC_HEAD = 20, 36             # room around the parts, and for the box's name


def inside(rig: dict, enc: dict, pos: dict | None = None) -> list[str]:
    """The parts that sit in an enclosure: those whose centre is inside its box.
    `pos` gives other places to judge from ({node id: (x, y)})."""
    out = []
    for n in rig["nodes"]:
        x, y = (pos or {}).get(n["id"], (n["x"], n["y"]))
        w, h = size(n)
        cx, cy = x + w / 2, y + h / 2
        if enc["x"] <= cx <= enc["x"] + enc["w"] and enc["y"] <= cy <= enc["y"] + enc["h"]:
            out.append(n["id"])
    return out


def wrap(rig: dict, members: list[str], pos: dict, grid: int = R.GRID) -> tuple[int, int, int, int]:
    """The box (x, y, w, h) that holds these parts at these places, with room for a name."""
    nodes = {n["id"]: n for n in rig["nodes"]}
    xs, ys, xe, ye = [], [], [], []
    for nid in members:
        x, y = pos[nid]
        w, h = size(nodes[nid])
        xs.append(x), ys.append(y), xe.append(x + w), ye.append(y + h)
    x0 = R.snap(min(xs) - ENC_PAD, grid)
    y0 = R.snap(min(ys) - ENC_HEAD, grid)
    w = max(R.ENC_MIN_W, R.snap(max(xe) + ENC_PAD - x0, grid))
    h = max(R.ENC_MIN_H, R.snap(max(ye) + ENC_PAD - y0, grid))
    return x0, y0, w, h


def tidy_enclosures(rig: dict, pos: dict) -> dict:
    """After Tidy: each enclosure is re-drawn round the parts that were in it, at their
    new places, so they stay inside. An empty enclosure keeps its place."""
    out = {}
    for e in R.enclosures(rig):
        members = inside(rig, e)
        if members:
            out[e["id"]] = wrap(rig, members, {**{n["id"]: (n["x"], n["y"]) for n in rig["nodes"]}, **pos})
    return out


# ---------------------------------------------------------------- Tidy with groups
def _overlap(a: tuple, b: tuple, gap: float = 0) -> bool:
    return (a[0] < b[0] + b[2] + gap and b[0] < a[0] + a[2] + gap
            and a[1] < b[1] + b[3] + gap and b[1] < a[1] + a[3] + gap)


def rigid_sets(rig: dict) -> list[dict]:
    """Pieces that keep their shape through Tidy: each group, and each set of parts mated
    port to port (relay 006), merged where they share a box. [{"members": [...]}]"""
    sets = [set(g["members"]) for g in R.groups(rig)]
    seen = set()
    for n in rig["nodes"]:
        if n["id"] in seen:
            continue
        piece = R.assembly(rig, n["id"])
        seen |= piece
        if len(piece) > 1:
            sets.append(piece)
    merged = []
    for s in sets:
        for m in [m for m in merged if m & s]:
            s |= m
            merged.remove(m)
        merged.append(s)
    order = {b["id"]: k for k, b in enumerate(R._boxes(rig))}
    return [{"members": sorted(m, key=lambda b: order.get(b, 0))} for m in merged]


def tidy_all(rig: dict, grid: int = R.GRID) -> tuple[dict, dict]:
    """Tidy, keeping each group in one piece: ({node id: (x, y)}, {enclosure id: (x, y, w, h)}).

    A group moves as a rigid whole, by the step its first tidied part takes, so its
    members keep their places relative to one another (an enclosure in the group moves
    with it, unresized). A locked box stays where it is. Every other part goes where
    tidy() puts it, and is then moved down out of the way of any rigid group or locked
    box it would overlap (relay 005: a rigid group never overlaps a tidied part). An
    enclosure outside any group is re-drawn round the parts that were in it."""
    pos = tidy(rig, grid)
    rigid = rigid_sets(rig)
    now = {n["id"]: (n["x"], n["y"]) for n in rig["nodes"]}
    nodes = {n["id"]: n for n in rig["nodes"]}
    encs = {e["id"]: e for e in R.enclosures(rig)}
    locked = {b["id"] for b in R._boxes(rig) if R.is_locked(b)}
    rects, grouped = {}, set()
    for g in rigid:
        lead = next((m for m in g["members"] if m in pos and m in now and m not in locked), None)
        if lead is None:
            continue
        dx, dy = pos[lead][0] - now[lead][0], pos[lead][1] - now[lead][1]
        for m in g["members"]:
            grouped.add(m)
            if m in locked:
                continue
            if m in now:
                pos[m] = (now[m][0] + dx, now[m][1] + dy)
            elif m in encs:
                e = encs[m]
                rects[m] = (e["x"] + dx, e["y"] + dy, e["w"], e["h"])
    for nid in locked & set(now):
        pos[nid] = now[nid]
    # Rigid pieces (each group, each locked part) are fixed; tidied parts make way for them.
    fixed = []
    for g in rigid:
        r = None
        for m in g["members"]:
            if m in nodes:
                x, y = pos[m]
                w, h = size(nodes[m])
                box = (x, y, w, h)
            elif m in rects:
                box = rects[m]
            else:
                continue
            r = box if r is None else (min(r[0], box[0]), min(r[1], box[1]),
                                       max(r[0] + r[2], box[0] + box[2]) - min(r[0], box[0]),
                                       max(r[1] + r[3], box[1] + box[3]) - min(r[1], box[1]))
        if r is not None:
            fixed.append(r)
    for nid in locked & set(now):
        if nid not in grouped:
            fixed.append((*now[nid], *size(nodes[nid])))
    free = sorted((nid for nid in pos if nid not in grouped and nid not in locked),
                  key=lambda nid: (pos[nid][1], pos[nid][0]))
    for nid in free:
        w, h = size(nodes[nid])
        x, y = pos[nid]
        for _ in range(len(fixed) + len(free) + 1):
            hit = next((f for f in fixed if _overlap((x, y, w, h), f, GAP_Y / 2 - 1)), None)
            if hit is None:
                break
            y = R.snap(hit[1] + hit[3] + GAP_Y, grid)
        pos[nid] = (x, y)
        fixed.append((x, y, w, h))
    # A power source keeps its place beside the first part it feeds (or with its group).
    for p in R.power_boxes(rig):
        if p["id"] in locked:
            continue
        g = R.group_of(rig, p["id"])
        lead = next((m for m in (g or {}).get("members", []) if m in now and m not in locked), None)
        if lead is None:
            lead = next((w["to"] for w in R.power_wires(rig) if w["from"] == p["id"]), None)
        if lead is not None and lead in pos:
            pos[p["id"]] = (p["x"] + pos[lead][0] - now[lead][0], p["y"] + pos[lead][1] - now[lead][1])
    for eid, rect in tidy_enclosures(rig, pos).items():
        if eid not in grouped and eid not in locked:
            rects[eid] = rect
    return pos, rects


# ---------------------------------------------------------------- power wires (relay 005)
def power_points(rig: dict) -> tuple[dict, dict]:
    """Where power wires attach: ({part id: (x, y, side)} for every part that needs power or
    has a feed, {power source id: (x, y, side)}). A part's power input sits on its top or
    bottom edge, toward its feed, at four-fifths of its width (clear of the RF dots, which sit
    mid-side); a power source's dot sits on the side facing the first part it feeds."""
    nodes = {n["id"]: n for n in rig["nodes"]}
    boxes = {b["id"]: b for b in R.power_boxes(rig)}
    feed = {w["to"]: w["from"] for w in R.power_wires(rig) if w["from"] in boxes and w["to"] in nodes}
    inputs, outputs = {}, {}
    for nid, n in nodes.items():
        if not (R.needs_power(n) or nid in feed):
            continue
        w, h = size(n)
        side = "top"
        if nid in feed:
            b = boxes[feed[nid]]
            side = "top" if b["y"] + power_size(b)[1] / 2 < n["y"] + h / 2 else "bottom"
        inputs[nid] = (*on_outline(n, n["x"] + w * 0.8, n["y"] if side == "top" else n["y"] + h), side)
    for pid, b in boxes.items():
        bw, bh = power_size(b)
        cx, cy = b["x"] + bw / 2, b["y"] + bh / 2
        to = next((w["to"] for w in R.power_wires(rig) if w["from"] == pid and w["to"] in inputs), None)
        side = "right"
        if to is not None:
            tx, ty = inputs[to][0], inputs[to][1]
            dx, dy = tx - cx, ty - cy
            if abs(dx) * bh >= abs(dy) * bw:
                side = "right" if dx >= 0 else "left"
            else:
                side = "bottom" if dy > 0 else "top"
        outputs[pid] = {"right": (b["x"] + bw, cy, side), "left": (b["x"], cy, side),
                        "top": (cx, b["y"], side), "bottom": (cx, b["y"] + bh, side)}[side]
    return inputs, outputs


# ---------------------------------------------------------------- boxes of every kind (relay 005)
def rect(rig: dict, bid: str) -> tuple | None:
    """Any box's (x, y, w, h): a part, an enclosure or a power source."""
    b = R.box(rig, bid)
    if b is None:
        return None
    kind = R.kind_of(rig, bid)
    if kind == "part":
        return (b["x"], b["y"], *size(b))
    if kind == "enclosure":
        return (b["x"], b["y"], b["w"], b["h"])
    return (b["x"], b["y"], *power_size(b))


def moving_with(rig: dict, ids) -> set:
    """Everything that moves when these boxes move: the boxes themselves, the parts and
    power sources in each enclosure among them, and the whole group of each such part.
    Locked boxes never move."""
    ids = set(ids)
    out = set(ids)
    for e in R.enclosures(rig):
        if e["id"] in ids and not R.is_locked(e):
            for bid in inside_any(rig, e):
                out.add(bid)
                g = R.group_of(rig, bid)
                if g:
                    out |= set(g["members"])
    # Relay 006: parts mated port to port move as one piece, and a piece with a locked part
    # in it does not move at all (moving the rest would pull the mating apart).
    nodes = {n["id"] for n in rig["nodes"]}
    for bid in list(out):
        if bid in nodes:
            piece = R.assembly(rig, bid)
            if len(piece) > 1:
                if any(R.is_locked(R.box(rig, m)) for m in piece):
                    out -= piece
                else:
                    out |= piece
    return {b for b in out if not R.is_locked(R.box(rig, b))}


def inside_any(rig: dict, enc: dict) -> list[str]:
    """Parts and power sources whose centre sits inside an enclosure."""
    out = list(inside(rig, enc))
    for p in R.power_boxes(rig):
        cx, cy = p["x"] + power_size(p)[0] / 2, p["y"] + power_size(p)[1] / 2
        if enc["x"] <= cx <= enc["x"] + enc["w"] and enc["y"] <= cy <= enc["y"] + enc["h"]:
            out.append(p["id"])
    return out


def shift(rig: dict, ids, dx: float, dy: float) -> bool:
    """Move boxes by (dx, dy), with what they carry (moving_with). Returns whether anything moved."""
    if not dx and not dy:
        return False
    hit = False
    for bid in moving_with(rig, ids):
        b = R.box(rig, bid)
        b["x"], b["y"] = int(round(b["x"] + dx)), int(round(b["y"] + dy))
        hit = True
    return hit


def place(rig: dict, targets: dict) -> bool:
    """Put boxes at new places {id: (x, y)}; an enclosure brings what it holds by the same
    step (unless that is placed itself). Locked boxes stay. Returns whether anything moved."""
    hit = False
    moves, pieces = {}, set()
    for bid, xy in targets.items():
        if R.is_locked(R.box(rig, bid)):
            continue
        piece = frozenset(R.assembly(rig, bid)) if R.kind_of(rig, bid) == "part" else frozenset([bid])
        if len(piece) > 1:            # a mated piece goes where its first member is sent; the rest follow
            if piece in pieces or any(R.is_locked(R.box(rig, m)) for m in piece):
                continue
            pieces.add(piece)
        moves[bid] = xy
    carried = {}
    for bid, (x, y) in moves.items():
        b = R.box(rig, bid)
        dx, dy = x - b["x"], y - b["y"]
        if (R.kind_of(rig, bid) == "enclosure" or len(R.assembly(rig, bid)) > 1) and (dx or dy):
            for other in moving_with(rig, [bid]) - {bid} - set(moves):
                carried.setdefault(other, (dx, dy))
    for bid, (x, y) in moves.items():
        b = R.box(rig, bid)
        if (b["x"], b["y"]) != (int(round(x)), int(round(y))):
            b["x"], b["y"] = int(round(x)), int(round(y))
            hit = True
    for bid, (dx, dy) in carried.items():
        b = R.box(rig, bid)
        b["x"], b["y"] = int(round(b["x"] + dx)), int(round(b["y"] + dy))
        hit = True
    return hit


ALIGN = ("left", "centre", "right", "top", "middle", "bottom")


def align(rig: dict, ids, how: str) -> dict:
    """New places {id: (x, y)} lining the boxes up on the selection's left edge, centre,
    right edge, top, middle or bottom. Locked boxes stay put but still set the edges."""
    if how not in ALIGN:
        raise ValueError(f"unknown alignment {how!r}")
    rs = {bid: rect(rig, bid) for bid in ids if rect(rig, bid) is not None}
    if len(rs) < 2:
        return {}
    left = min(r[0] for r in rs.values())
    right = max(r[0] + r[2] for r in rs.values())
    top = min(r[1] for r in rs.values())
    bottom = max(r[1] + r[3] for r in rs.values())
    out = {}
    for bid, (x, y, w, h) in rs.items():
        if R.is_locked(R.box(rig, bid)):
            continue
        nx, ny = x, y
        if how == "left":
            nx = left
        elif how == "right":
            nx = right - w
        elif how == "centre":
            nx = round((left + right) / 2 - w / 2)
        elif how == "top":
            ny = top
        elif how == "bottom":
            ny = bottom - h
        else:
            ny = round((top + bottom) / 2 - h / 2)
        if (nx, ny) != (x, y):
            out[bid] = (nx, ny)
    return out


def distribute(rig: dict, ids, axis: str) -> dict:
    """New places spreading three or more boxes evenly across ("across") or down
    ("down"): the first and last stay, the gaps between neighbours become equal."""
    rs = {bid: rect(rig, bid) for bid in ids if rect(rig, bid) is not None}
    if len(rs) < 3:
        return {}
    k = 0 if axis == "across" else 1
    order = sorted(rs, key=lambda b: (rs[b][k], rs[b][1 - k]))
    span = rs[order[-1]][k] + rs[order[-1]][k + 2] - rs[order[0]][k]
    room = span - sum(rs[b][k + 2] for b in order)
    gap = room / (len(order) - 1)
    out, at = {}, rs[order[0]][k]
    for bid in order:
        x, y, w, h = rs[bid]
        new = round(at)
        if bid not in (order[0], order[-1]) and not R.is_locked(R.box(rig, bid)) and new != rs[bid][k]:
            out[bid] = (new, y) if k == 0 else (x, new)
        at += rs[bid][k + 2] + gap
    return out


GUIDE_TOL = 6                             # px: how close an edge or centre must come to snap


def guide_snap(moving: tuple, others: list, tol: float = GUIDE_TOL) -> tuple:
    """Smart guides: a box being dragged at `moving` = (x, y, w, h) lines up with another
    box's left edge, centre or right edge (and top, middle, bottom) when it comes within
    `tol`. Returns (dx, dy, lines): the nudge that snaps it and the guide lines to draw,
    each ("v", x, y0, y1) or ("h", y, x0, x1) in scene units."""
    x, y, w, h = moving
    best_x = best_y = None
    for o in others:
        ox, oy, ow, oh = o
        for mine in (x, x + w / 2, x + w):
            for theirs in (ox, ox + ow / 2, ox + ow):
                d = theirs - mine
                if abs(d) <= tol and (best_x is None or abs(d) < abs(best_x[0])):
                    best_x = (d, theirs)
        for mine in (y, y + h / 2, y + h):
            for theirs in (oy, oy + oh / 2, oy + oh):
                d = theirs - mine
                if abs(d) <= tol and (best_y is None or abs(d) < abs(best_y[0])):
                    best_y = (d, theirs)
    dx = best_x[0] if best_x else 0
    dy = best_y[0] if best_y else 0
    lines = []
    if best_x:
        line = best_x[1]
        ys = [y + dy, y + dy + h]
        for o in others:
            if any(abs(line - v) < 0.5 for v in (o[0], o[0] + o[2] / 2, o[0] + o[2])):
                ys += [o[1], o[1] + o[3]]
        lines.append(("v", line, min(ys) - 12, max(ys) + 12))
    if best_y:
        line = best_y[1]
        xs = [x + dx, x + dx + w]
        for o in others:
            if any(abs(line - v) < 0.5 for v in (o[1], o[1] + o[3] / 2, o[1] + o[3])):
                xs += [o[0], o[0] + o[2]]
        lines.append(("h", line, min(xs) - 12, max(xs) + 12))
    return dx, dy, lines
