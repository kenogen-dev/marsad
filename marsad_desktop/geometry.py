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


DIRECTIONS = ("right", "down")
# Relay 016: a rig is built left to right (the default, no key stored) or top to bottom ("dir": "down").
# FLOW gives (the side a main output sits on, the side a main input sits on).
FLOW = {"right": ("right", "left"), "down": ("bottom", "top")}


def direction(rig: dict | None) -> str:
    """A rig's layout direction: "down" (top to bottom) or "right" (left to right, the default)."""
    return "down" if (rig or {}).get("dir") == "down" else "right"


def oriented(shape: str, down: bool) -> str:
    """The outline a shape is drawn with: a triangle points, and a trapezoid widens, along the rig's flow, so in
    a top-to-bottom rig they are turned to point down (relay 016)."""
    return shape + "_down" if down and shape in ("triangle", "trapezoid") else shape


def side_toward(a: dict, b: dict, down: bool = False) -> str:
    """The side of part a that faces part b. Left to right: a part directly above or below (the two boxes
    overlap left to right) is joined top to bottom; anything else from the side, since chains read left to
    right. Top to bottom (relay 016) is the same rule turned a quarter: a part directly beside it (the boxes
    overlap up and down) is joined from the side, anything else top to bottom."""
    (ax, ay), (bx, by) = centre(a), centre(b)
    (aw, ah), (bw, bh) = size(a), size(b)
    dx, dy = bx - ax, by - ay
    if down:
        overlap_along = abs(dy) < (ah + bh) / 2
        if overlap_along and abs(dx) >= (aw + bw) / 2:
            return "right" if dx > 0 else "left"
        if dx == 0 and dy == 0:
            return "bottom"
        if overlap_along and abs(dx) > abs(dy):
            return "right" if dx > 0 else "left"
        return "bottom" if dy >= 0 else "top"
    overlap_across = abs(dx) < (aw + bw) / 2
    if overlap_across and abs(dy) >= (ah + bh) / 2:
        return "bottom" if dy > 0 else "top"
    if dx == 0 and dy == 0:
        return "right"
    if overlap_across:            # boxes on top of each other: whichever way is further
        if abs(dy) > abs(dx):
            return "bottom" if dy > 0 else "top"
    return "right" if dx >= 0 else "left"


def side_to_point(node: dict, x: float, y: float, down: bool = False) -> str:
    """The side of a part that faces a point (a wire being dragged), by the same rule."""
    cx, cy = centre(node)
    w, h = size(node)
    dx, dy = x - cx, y - cy
    if down:
        if abs(dy) <= h / 2 and (abs(dx) > w / 2 or abs(dx) / w > abs(dy) / h):
            return "right" if dx > 0 else "left"
        return "bottom" if dy >= 0 else "top"
    if abs(dx) <= w / 2 and (abs(dy) > h / 2 or abs(dy) / h > abs(dx) / w):
        return "bottom" if dy > 0 else "top"
    return "right" if dx >= 0 else "left"


def mid_side(node: dict, side: str, down: bool = False) -> tuple[float, float]:
    return _point_on(node, side, 0, 1, down)


def _point_on(node: dict, side: str, k: int, n: int, down: bool = False) -> tuple[float, float]:
    w, h = size(node)
    f = (k + 1) / (n + 1)
    x, y = node["x"], node["y"]
    pt = {"right": (x + w, y + h * f), "left": (x, y + h * f),
          "top": (x + w * f, y), "bottom": (x + w * f, y + h)}[side]
    return on_outline(node, *pt, down=down)


# ---------------------------------------------------------------- right-click menus (relay 016)
MENU_OFFSET = 4                        # px, how far below and right of the pointer a menu's corner sits


def menu_place(px: int, py: int, mw: int, mh: int, area: tuple | None, off: int = MENU_OFFSET) -> tuple[int, int]:
    """Where a right-click menu's top-left corner goes, for the pointer at (px, py) and a menu mw x mh: just below
    and right of the pointer, so it never covers what was clicked. It flips to the pointer's left (or above it) only
    where it would run off `area` (left, top, right, bottom, inclusive: the window); if it fits neither way it is
    held inside the area."""
    x, y = px + off, py + off
    if area is None:
        return x, y
    left, top, right, bottom = area
    if x + mw - 1 > right:
        x = px - off - mw
        if x < left:
            x = max(left, right - mw + 1)
    if y + mh - 1 > bottom:
        y = py - off - mh
        if y < top:
            y = max(top, bottom - mh + 1)
    return x, y


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
    elif shape == "triangle_down":              # relay 016: in a top-to-bottom rig it points down
        poly = [(0, 0), (w, 0), (w / 2, h)]
    elif shape == "trapezoid_down":             # and widens toward its outputs, below
        poly = [(w * 0.22, 0), (w * 0.78, 0), (w, h), (0, h)]
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


def on_outline(node: dict, px: float, py: float, down: bool = False) -> tuple[float, float]:
    """Where a point on a part's box falls on its shape: along the line from the part's centre
    to it, the first place that line leaves the outline. On a box-shaped part, the point itself."""
    w, h = size(node)
    poly = outline(oriented(R.shape_of(node), down), w, h)
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
           min_w: float = MIN_W, min_h: float = MIN_H, max_w: float = MAX_W, max_h: float = MAX_H,
           from_centre: bool = False) -> tuple:
    """A box (x, y, w, h) dragged by one of its eight handles by (dx, dy). The opposite edge stays
    put; edges snap to the grid when `snap` is given (Ctrl held: None, fine sizing); with keep_ratio
    (Shift) the box keeps its proportions; with from_centre (Alt, relay 015) the opposite edge moves
    the other way, so the box grows or shrinks about its centre. Sizes are held between the limits."""
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
    if from_centre:                             # the opposite edge mirrors the dragged one
        if "l" in handle:
            x2 = x + w + (x - x1)
        if "r" in handle:
            x1 = x - (x2 - (x + w))
        if "t" in handle:
            y2 = y + h + (y - y1)
        if "b" in handle:
            y1 = y - (y2 - (y + h))
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
    if from_centre:                             # held about the centre, whatever the limits did
        nx, ny = x + (w - nw) / 2, y + (h - nh) / 2
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
    an unwired input on the left (below and on top in a top-to-bottom rig, relay 016);
    a source has no input."""
    nodes = {n["id"]: n for n in rig["nodes"]}
    on_side: dict[tuple[str, str], list] = {}      # (node, side) -> [(sort key, key)]
    down = direction(rig) == "down"
    out_side, in_side = FLOW[direction(rig)]

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
        k_in = R.in_slot(w) if R.multi_in(nodes.get(w["to"])) else 0
        a, b = nodes.get(w["from"]), nodes.get(w["to"])
        if a is None or b is None:
            continue
        s = side_toward(a, b, down)
        sa = side_of(a, "out", w["port"], s)
        # The far end faces the near end's point: a fixed side on one end turns the other.
        sb = side_of(b, "in", 0, OPPOSITE[sa] if fixed.get(a["id"]) and not fixed.get(b["id"]) else OPPOSITE[s])
        if fixed.get(b["id"]) and not fixed.get(a["id"]):
            sa = OPPOSITE[sb]
        put(a["id"], sa, (a["id"], "out", w["port"]), b)
        put(b["id"], sb, (b["id"], "in", k_in), a)
        wired_out.add((a["id"], w["port"]))
        wired_in.add(b["id"])
    for nid, n in nodes.items():
        for p in range(R.out_ports(n)):
            if (nid, p) not in wired_out:
                put(nid, side_of(n, "out", p, out_side), (nid, "out", p), None)
        if nid not in wired_in and not is_source(n):
            put(nid, side_of(n, "in", 0, in_side), (nid, "in", 0), None)
        elif R.multi_in(n):                  # relay 015: a free input always waits on a PC or hub
            k = R.free_input(rig, nid)
            put(nid, in_side, (nid, "in", k), None)
    out = {}
    for (nid, side), items in on_side.items():
        items.sort(key=lambda t: (t[0], t[1][1] == "out", t[1][2]))
        for k, (_, key) in enumerate(items):
            x, y = _point_on(nodes[nid], side, k, len(items), down)
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
    down = direction(rig) == "down"
    cross = 0 if down else 1          # the axis branches are spread along: x top to bottom, y left to right
    children: dict[str, list] = {nid: [] for nid in nodes}
    parent = {}
    for w in rig["wires"]:
        if w["from"] in nodes and w["to"] in nodes:
            children[w["from"]].append((w["port"], w["to"]))
            parent[w["to"]] = w["from"]
    for nid in children:
        children[nid].sort(key=lambda t: (centre(nodes[t[1]])[cross], t[0]))
    wired = set(parent) | {nid for nid, c in children.items() if c}
    roots = sorted((nid for nid in nodes if nid not in parent and nid in wired),
                   key=lambda nid: (centre(nodes[nid])[cross], centre(nodes[nid])[1 - cross]))
    loose = sorted((nid for nid in nodes if nid not in wired),
                   key=lambda nid: (centre(nodes[nid])[1 - cross], centre(nodes[nid])[cross]))

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
    if down:
        return _tidy_down(rig, nodes, depth, row, slot[0], loose, grid)
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


def _tidy_down(rig: dict, nodes: dict, depth: dict, slot_of: dict, slots: float, loose: list, grid: int) -> dict:
    """Tidy for a top-to-bottom rig (relay 016): the left-to-right arrangement turned a quarter. Each part goes
    in the row of its distance from the source, branches side by side, a part with several outputs above the
    middle of what it feeds; parts on no wire line up to the right of the rest."""
    rows = max(depth.values(), default=-1) + 1
    row_h = [0] * max(rows, 1)
    for nid, d in depth.items():
        row_h[d] = max(row_h[d], size(nodes[nid])[1])
    col_w = max((size(n)[0] for n in nodes.values()), default=R.PART_W) + GAP_X
    label_h = 22                                # a cable label's pill, upright on a vertical wire
    gap_before = [GAP_Y + 20] * len(row_h)
    for w in rig["wires"]:
        if w["to"] in depth and depth[w["to"]] > 0 and w.get("cables"):
            d = depth[w["to"]]
            gap_before[d] = max(gap_before[d], GAP_Y + label_h + 20)
    row_y, y = [], 0
    for d, h in enumerate(row_h):
        if d:
            y += gap_before[d]
        row_y.append(y)
        y += h
    out = {}
    for nid in depth:
        w = size(nodes[nid])[0]
        x = slot_of[nid] * col_w + (col_w - GAP_X - w) / 2
        out[nid] = (R.snap(x, grid), R.snap(row_y[depth[nid]], grid))
    x0 = R.snap(slots * col_w + (GAP_X if depth else 0), grid)
    y = 0
    for nid in loose:
        out[nid] = (x0, R.snap(y, grid))
        y += size(nodes[nid])[1] + GAP_Y
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


# ---------------------------------------------------------------- bend points (relay 015)
def _square_through(seq: list, sa: str | None, sb: str | None) -> list:
    """Right angles through a list of points: each step goes across then up or down (or the other way, by
    which is longer). The first step leaves square to its part's edge and the last arrives square."""
    off = 20
    a, b = seq[0], seq[-1]
    out = [a]
    cur = a
    if sa:
        cur = (a[0] + NORMAL[sa][0] * off, a[1] + NORMAL[sa][1] * off)
        out.append(cur)
    goals = list(seq[1:-1])
    end = (b[0] + NORMAL[sb][0] * off, b[1] + NORMAL[sb][1] * off) if sb else b
    goals.append(end)
    across = sa in ("left", "right") if sa else True
    for k, q in enumerate(goals):
        if k:
            across = abs(q[0] - cur[0]) >= abs(q[1] - cur[1])
        out += [(q[0], cur[1]) if across else (cur[0], q[1]), q]
        cur = q
    if sb:
        out.append(b)
    clean = [out[0]]
    for p in out[1:]:
        if p != clean[-1]:
            clean.append(p)
    return clean


def route_via(a: tuple, b: tuple, points=None, style: str = "curved", side_b: str | None = None,
              square: bool = False) -> tuple:
    """A wire through its bend points (relay 015). With none, exactly route(). With points: ("poly", pts) for
    straight and right-angled wires, or ("spline", [(p0, c1, c2, p1), ...]) for curved ones, a smooth curve
    through every point. `square` asks for right angles on this one wire whatever the style."""
    sb = side_b or (b[2] if len(b) > 2 else None)
    pts = [(float(q[0]), float(q[1])) for q in (points or [])]
    if not pts:
        return route(a, b, "angled" if square else style, sb)
    seq = [(float(a[0]), float(a[1]))] + pts + [(float(b[0]), float(b[1]))]
    sa = a[2] if len(a) > 2 else None
    if square or style == "angled":
        return "poly", _square_through(seq, sa, sb)
    if style == "straight":
        return "poly", seq

    def tangent(i):                     # Catmull-Rom, the ends along their sides' normals
        if i == 0 and sa:
            d = math.dist(seq[0], seq[1])
            return (NORMAL[sa][0] * d, NORMAL[sa][1] * d)
        if i == len(seq) - 1 and sb:
            d = math.dist(seq[-1], seq[-2])
            return (-NORMAL[sb][0] * d, -NORMAL[sb][1] * d)
        p, q = seq[max(i - 1, 0)], seq[min(i + 1, len(seq) - 1)]
        return ((q[0] - p[0]) / 2, (q[1] - p[1]) / 2)

    segs = []
    for i in range(len(seq) - 1):
        p0, p1 = seq[i], seq[i + 1]
        t0, t1 = tangent(i), tangent(i + 1)
        segs.append((p0, (p0[0] + t0[0] / 3, p0[1] + t0[1] / 3), (p1[0] - t1[0] / 3, p1[1] - t1[1] / 3), p1))
    return "spline", segs


def _seg_dist(p, a, b) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    L = dx * dx + dy * dy
    t = 0.0 if not L else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L))
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))


def insert_index(a: tuple, b: tuple, points, x: float, y: float) -> int:
    """Where a new bend point at (x, y) goes in a wire's list: in the stretch of the wire it is nearest,
    judged on the straight line through the existing points."""
    seq = [(a[0], a[1])] + [tuple(q) for q in (points or [])] + [(b[0], b[1])]
    return min(range(len(seq) - 1), key=lambda i: _seg_dist((x, y), seq[i], seq[i + 1]))


def _simplify(seq: list, keep: set) -> list:
    """A routed line with repeated points and points in the middle of a straight run taken out (except the
    indices in `keep`)."""
    out = []
    for k, p in enumerate(seq):
        if out and p == out[-1][1] and k not in keep:
            continue
        out.append((k, p))
    clean = []
    for j, (k, p) in enumerate(out):
        if 0 < j < len(out) - 1 and k not in keep:
            q, r = out[j - 1][1], out[j + 1][1]
            if (q[0] == p[0] == r[0]) or (q[1] == p[1] == r[1]):
                continue
        clean.append((k, p))
    return clean


def move_wire(a: tuple, b: tuple, points, style: str, square: bool, grab: tuple, dx: float, dy: float,
              snap: int | None = None) -> list:
    """Relay 016: a wire dragged by its body, by (dx, dy) from where it was grabbed. Its ends stay on their
    parts; the stretch under the pointer moves and the wire is re-routed round it. Returns the wire's new bend
    points (whole grid units when `snap` is given). Always worked out from the points as they were when the drag
    began, so a drag can be redone from scratch on every pointer move.

    Right angles (the angled style, or the wire's own choice): the routed line's segment under the pointer moves
    across itself (a level segment up or down, an upright one sideways), and its neighbours stretch to meet it; a
    segment that starts at a part's own short stub gets a new corner there, so the part's end stays square. Curved
    and straight wires: a stretch between two bend points moves with both of them; a stretch from a part's end, or
    a wire with no bends yet, is pulled out at the place it was grabbed."""
    def sn(v):
        return R.snap(v, snap) if snap else int(round(v))

    sa = a[2] if len(a) > 2 else None
    sb = b[2] if len(b) > 2 else None
    pts = [(float(q[0]), float(q[1])) for q in (points or [])]
    if square or style == "angled":
        kind, P = route_via(a, b, pts, style, sb, square)
        P = [(float(p[0]), float(p[1])) for p in P]
        n = len(P)
        i = min(range(n - 1), key=lambda k: _seg_dist(grab, P[k], P[k + 1]))
        if sa and i == 0 and n > 3:          # a part's own short stub moves with the stretch after it
            i = 1
        if sb and i == n - 2 and n > 3:
            i = n - 3
        level, upright = P[i][1] == P[i + 1][1], P[i][0] == P[i + 1][0]
        mx, my = (0.0, dy) if level else (dx, 0.0) if upright else (dx, dy)
        fixed = {0, n - 1} | ({1} if sa else set()) | ({n - 2} if sb else set())
        left, right = P[:i + 1], P[i + 1:]
        if i in fixed:
            left = left + [P[i]]
        if i + 1 in fixed:
            right = [P[i + 1]] + right
        p, q = left[-1], right[0]
        left[-1] = (sn(p[0] + mx) if mx else p[0], sn(p[1] + my) if my else p[1])
        right[0] = (sn(q[0] + mx) if mx else q[0], sn(q[1] + my) if my else q[1])
        seq = left + right
        m = len(seq)
        ends = {0, m - 1} | ({1} if sa else set()) | ({m - 2} if sb else set())
        kept = _simplify(seq, ends)
        return [[int(round(p[0])), int(round(p[1]))] for k, p in kept if k not in ends]
    seq = [(float(a[0]), float(a[1]))] + pts + [(float(b[0]), float(b[1]))]
    i = insert_index(a, b, pts, grab[0], grab[1])
    new = [list(q) for q in pts]
    if 0 < i and i + 1 < len(seq) - 1:               # between two bend points: both move
        for j in (i - 1, i):
            new[j] = [sn(pts[j][0] + dx), sn(pts[j][1] + dy)]
        return [[int(q[0]), int(q[1])] for q in new]
    new.insert(i, [sn(grab[0] + dx), sn(grab[1] + dy)])   # from a part's end, or no bends yet: pull it out here
    return [[int(round(q[0])), int(round(q[1]))] for q in new]


def carry_points(rig: dict, ids, dx: float, dy: float) -> int:
    """Bend points of every wire whose two ends both moved by (dx, dy) move with them, so a moved group keeps
    its wires' shapes. Returns how many wires were carried."""
    ids = set(ids)
    k = 0
    for w in rig["wires"]:
        if w.get("points") and w["from"] in ids and w["to"] in ids:
            w["points"] = [[int(round(q[0] + dx)), int(round(q[1] + dy))] for q in w["points"]]
            k += 1
    return k


def bends_after_move(rig: dict, before: dict) -> None:
    """After many boxes moved at once (Tidy): a wire whose two ends moved by the same step keeps its bends,
    moved with them; any other wire's bends no longer fit the drawing and are taken off (Undo puts them
    back). `before` is {part id: (x, y)} from before the move."""
    nodes = {n["id"]: n for n in rig["nodes"]}
    for w in rig["wires"]:
        if not w.get("points") or w["from"] not in nodes or w["to"] not in nodes:
            continue
        a, b = nodes[w["from"]], nodes[w["to"]]
        da = (a["x"] - before.get(a["id"], (a["x"], a["y"]))[0], a["y"] - before.get(a["id"], (a["x"], a["y"]))[1])
        db = (b["x"] - before.get(b["id"], (b["x"], b["y"]))[0], b["y"] - before.get(b["id"], (b["x"], b["y"]))[1])
        if da == db:
            w["points"] = [[q[0] + da[0], q[1] + da[1]] for q in w["points"]]
        else:
            w.pop("points", None)


def label_spot(r: tuple) -> tuple[float, float] | None:
    """Where a cable label sits on a routed wire with bends: the middle of its longest straight stretch
    (poly), or the middle of its longest curve segment (spline), so it never sits on a corner."""
    if r[0] == "poly" and len(r[1]) > 2:
        p = r[1]
        i = max(range(len(p) - 1), key=lambda k: math.dist(p[k], p[k + 1]))
        return ((p[i][0] + p[i + 1][0]) / 2, (p[i][1] + p[i + 1][1]) / 2)
    if r[0] == "spline":
        segs = r[1]
        s = max(segs, key=lambda q: math.dist(q[0], q[3]))
        p0, c1, c2, p1 = s                     # the cubic's own middle (t = 0.5)
        return ((p0[0] + 3 * c1[0] + 3 * c2[0] + p1[0]) / 8, (p0[1] + 3 * c1[1] + 3 * c2[1] + p1[1]) / 8)
    return None


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


def blocks(rig: dict) -> list[list[str]]:
    """Relay 015: the pieces Tidy keeps in one piece, each a list of box ids: every group, every set of parts
    mated port to port, and every enclosure with what it owns (owners()), merged where they share a box. An
    enclosure's block never takes another enclosure's parts, so two enclosures stay two blocks unless a group
    or a mating joins them."""
    own = owners(rig)
    sets = [set(g["members"]) for g in R.groups(rig)]
    for e in R.enclosures(rig):
        sets.append({e["id"]} | {b for b, eid in own.items() if eid == e["id"]})
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
    out = [sorted(m, key=lambda b: order.get(b, 0)) for m in merged if len(m) > 1 or R.kind_of(rig, next(iter(m))) == "enclosure"]
    out.sort(key=lambda m: order.get(m[0], 0))
    return out


def tidy_all(rig: dict, grid: int = R.GRID) -> tuple[dict, dict]:
    """Tidy, keeping each block in one piece: ({box id: (x, y)}, {enclosure id: (x, y, w, h)}).

    Relay 015: each group, each mated piece and each enclosure with what it owns is one rigid block. A block
    moves by the step its first tidied part takes (an enclosure with nothing in it stays where it is), and
    keeps its shape; a block with a locked box in it stays where it is, as does a locked box. Blocks are then
    laid out so none overlaps another (one in the way is moved down), and every other part goes where tidy()
    puts it and is moved down out of the way of the blocks (relay 005). Nothing ends up overlapping."""
    pos = tidy(rig, grid)
    now = {b["id"]: (b["x"], b["y"]) for b in R._boxes(rig)}
    nodes = {n["id"]: n for n in rig["nodes"]}
    encs = {e["id"]: e for e in R.enclosures(rig)}
    locked = {b["id"] for b in R._boxes(rig) if R.is_locked(b)}
    gap = GAP_Y / 2 - 1

    def box_rect(bid, dx=0, dy=0):
        r = rect(rig, bid)
        return (r[0] + dx, r[1] + dy, r[2], r[3])

    def union(rs):
        x0, y0 = min(r[0] for r in rs), min(r[1] for r in rs)
        return (x0, y0, max(r[0] + r[2] for r in rs) - x0, max(r[1] + r[3] for r in rs) - y0)

    out, rects, grouped = {}, {}, set()
    fixed, todo = [], []
    for blk in blocks(rig):
        grouped |= set(blk)
        if any(m in locked for m in blk):
            fixed.append(union([box_rect(m) for m in blk]))     # a locked box holds its whole block
            continue
        lead = next((m for m in blk if m in pos and m in nodes), None)
        if lead is None:                                         # an empty enclosure keeps its place
            fixed.append(union([box_rect(m) for m in blk]))
            continue
        dx, dy = pos[lead][0] - now[lead][0], pos[lead][1] - now[lead][1]
        todo.append((blk, dx, dy, union([box_rect(m, dx, dy) for m in blk])))
    for nid in locked - grouped:
        fixed.append(box_rect(nid))
    down = direction(rig) == "down"

    def clear_of(hit):
        """Where a box in the way of `hit` moves to: below it, or to its right in a top-to-bottom rig (relay 016),
        so blocks give way across the flow, not along it."""
        return ("x", R.snap(hit[0] + hit[2] + GAP_X, grid)) if down else ("y", R.snap(hit[1] + hit[3] + GAP_Y, grid))

    todo.sort(key=lambda t: (t[3][0], t[3][1]) if down else (t[3][1], t[3][0]))
    placed = list(fixed)
    for blk, dx, dy, r in todo:
        x, y, w, h = r
        for _ in range(len(placed) + 1):
            hit = next((f for f in placed if _overlap((x, y, w, h), f, gap)), None)
            if hit is None:
                break
            axis, v = clear_of(hit)
            if axis == "x":
                x = v
            else:
                y = v
        dx += x - r[0]
        dy += y - r[1]
        placed.append((x, y, w, h))
        for m in blk:
            if m in encs:
                e = encs[m]
                rects[m] = (int(e["x"] + dx), int(e["y"] + dy), e["w"], e["h"])
            else:
                out[m] = (int(now[m][0] + dx), int(now[m][1] + dy))
    for nid in locked & set(now):
        if nid not in encs:
            out[nid] = now[nid]
    free = sorted((nid for nid in pos if nid not in grouped and nid not in locked),
                  key=lambda nid: (pos[nid][0], pos[nid][1]) if down else (pos[nid][1], pos[nid][0]))
    for nid in free:
        w, h = size(nodes[nid])
        x, y = pos[nid]
        for _ in range(len(placed) + 1):
            hit = next((f for f in placed if _overlap((x, y, w, h), f, gap)), None)
            if hit is None:
                break
            axis, v = clear_of(hit)
            if axis == "x":
                x = v
            else:
                y = v
        out[nid] = (x, y)
        placed.append((x, y, w, h))
    # A power source in no block keeps its place beside the first part it feeds.
    for p in R.power_boxes(rig):
        if p["id"] in locked or p["id"] in grouped:
            continue
        lead = next((w["to"] for w in R.power_wires(rig) if w["from"] == p["id"]), None)
        if lead is not None and lead in out:
            out[p["id"]] = (p["x"] + out[lead][0] - now[lead][0], p["y"] + out[lead][1] - now[lead][1])
    return out, rects


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
    down = direction(rig) == "down"
    for nid, n in nodes.items():
        if not (R.needs_power(n) or nid in feed):
            continue
        w, h = size(n)
        if down:            # relay 016: top to bottom, RF runs through the top and bottom, so power is on a side
            side = "right"
            if nid in feed:
                b = boxes[feed[nid]]
                side = "left" if b["x"] + power_size(b)[0] / 2 < n["x"] + w / 2 else "right"
            inputs[nid] = (*on_outline(n, n["x"] if side == "left" else n["x"] + w, n["y"] + h * 0.8, True), side)
            continue
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
            # relay 014: a bulkhead seated on this enclosure's wall always comes along (owners() says so);
            # relay 015: only what this enclosure owns, never another enclosure's parts
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


def _holds(enc: dict, cx: float, cy: float) -> bool:
    return enc["x"] <= cx <= enc["x"] + enc["w"] and enc["y"] <= cy <= enc["y"] + enc["h"]


def owners(rig: dict) -> dict:
    """Relay 015: which enclosure each part and power source belongs to, {box id: enclosure id}. A box
    belongs to at most one: a bulkhead seated on a wall to that enclosure; otherwise, of the enclosures
    holding its centre, one in the same group as the box first (so two grouped enclosures that Tidy once
    overlapped keep their own parts), then the smallest, then the last drawn. An enclosure never owns
    another enclosure or what another one owns."""
    encs = R.enclosures(rig)
    if not encs:
        return {}
    gid = {}
    for g in R.groups(rig):
        for m in g["members"]:
            gid[m] = g["id"]
    order = {e["id"]: k for k, e in enumerate(encs)}
    out = {}
    boxes = [(n["id"], n["x"] + size(n)[0] / 2, n["y"] + size(n)[1] / 2, n) for n in rig["nodes"]]
    boxes += [(p["id"], p["x"] + power_size(p)[0] / 2, p["y"] + power_size(p)[1] / 2, p) for p in R.power_boxes(rig)]
    for bid, cx, cy, b in boxes:
        seat = (b.get("wall") or {}).get("enc")
        if seat is not None and seat in order:
            out[bid] = seat
            continue
        cands = [e for e in encs if _holds(e, cx, cy)]
        if not cands:
            continue
        mine = gid.get(bid)
        cands.sort(key=lambda e: (not (mine is not None and gid.get(e["id"]) == mine),
                                  e["w"] * e["h"], -order[e["id"]]))
        out[bid] = cands[0]["id"]
    return out


def inside_any(rig: dict, enc: dict) -> list[str]:
    """Parts and power sources that belong to an enclosure (owners(): the smallest one holding the centre,
    relay 015), seated bulkheads included."""
    own = owners(rig)
    return [bid for bid in [n["id"] for n in rig["nodes"]] + [p["id"] for p in R.power_boxes(rig)]
            if own.get(bid) == enc["id"]]


def shift(rig: dict, ids, dx: float, dy: float) -> bool:
    """Move boxes by (dx, dy), with what they carry (moving_with). Returns whether anything moved."""
    if not dx and not dy:
        return False
    hit = False
    moved = moving_with(rig, ids)
    for bid in moved:
        b = R.box(rig, bid)
        b["x"], b["y"] = int(round(b["x"] + dx)), int(round(b["y"] + dy))
        hit = True
    carry_points(rig, moved, dx, dy)                # relay 015: a moved group keeps its wires' bends
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


# ---------------------------------------------------------------- bulkheads on enclosure walls (relay 014)
# Drawing only. A bulkhead (SMA or USB) near an enclosure's edge seats across the wall: its centre on the wall
# line, half inside and half outside. The seat is stored on the part as {"wall": {"enc", "side", "t"}}: the
# enclosure, which side and how far along it (0..1). Seated, it slides along the wall (round the corners too)
# and comes off with a firm drag away; it always moves with its enclosure and stays seated when the enclosure is
# resized. Nothing here reaches the recorded text: the bulkhead writes into it by its place in the path.
BULKHEADS = ("bulkhead_sma", "bulkhead_usb")
WALL_SNAP = 24          # a bulkhead's centre this close to an edge seats it
WALL_PULL = 56          # dragged this far off the wall, it comes off
WALL_SIDES = ("top", "right", "bottom", "left")


def is_bulkhead(node: dict) -> bool:
    return (node.get("part") or {}).get("type") in BULKHEADS


def wall_project(enc: dict, cx: float, cy: float) -> tuple[str, float, float]:
    """The nearest point of an enclosure's outline to (cx, cy): (side, t along that side 0..1, distance)."""
    x, y, w, h = float(enc["x"]), float(enc["y"]), float(enc["w"]), float(enc["h"])
    best = None
    for side in WALL_SIDES:
        if side in ("top", "bottom"):
            px, py = min(max(cx, x), x + w), (y if side == "top" else y + h)
            t = (px - x) / w if w else 0.0
        else:
            px, py = (x if side == "left" else x + w), min(max(cy, y), y + h)
            t = (py - y) / h if h else 0.0
        d = math.hypot(cx - px, cy - py)
        if best is None or d < best[2] - 1e-9:
            best = (side, t, d)
    return best


def wall_point(enc: dict, side: str, t: float) -> tuple[float, float]:
    x, y, w, h = float(enc["x"]), float(enc["y"]), float(enc["w"]), float(enc["h"])
    t = min(max(float(t), 0.0), 1.0)
    return {"top": (x + t * w, y), "bottom": (x + t * w, y + h),
            "left": (x, y + t * h), "right": (x + w, y + t * h)}[side]


def seated_xy(enc: dict, wall: dict, node: dict) -> tuple[int, int]:
    """Where a seated part's box sits: its centre on the wall point."""
    w, h = size(node)
    px, py = wall_point(enc, wall["side"], wall["t"])
    return int(round(px - w / 2)), int(round(py - h / 2))


def _enc(rig: dict, eid) -> dict | None:
    return next((e for e in R.enclosures(rig) if e["id"] == eid), None)


def seat_near(rig: dict, node: dict, x: float, y: float, keep: bool = False):
    """Where a bulkhead goes when it is put at box position (x, y): seated on the nearest wall within reach
    (WALL_SNAP to seat, WALL_PULL to stay seated when it already is), else free. Updates node["wall"]; returns
    the box position to use. Parts that are not bulkheads are returned as they are."""
    if not is_bulkhead(node):
        return x, y
    w, h = size(node)
    cx, cy = x + w / 2, y + h / 2
    cur = node.get("wall")
    if keep and cur and _enc(rig, cur.get("enc")) is not None:
        enc = _enc(rig, cur["enc"])
        side, t, d = wall_project(enc, cx, cy)
        if d <= WALL_PULL:
            node["wall"] = {"enc": enc["id"], "side": side, "t": round(t, 4)}
            return seated_xy(enc, node["wall"], node)
    best = None
    for enc in R.enclosures(rig):
        side, t, d = wall_project(enc, cx, cy)
        if d <= WALL_SNAP and (best is None or d < best[3]):
            best = (enc, side, t, d)
    if best is None:
        node.pop("wall", None)
        return x, y
    enc, side, t, _ = best
    node["wall"] = {"enc": enc["id"], "side": side, "t": round(t, 4)}
    return seated_xy(enc, node["wall"], node)


def reseat(rig: dict, eid=None) -> list[str]:
    """Put every seated part (or those on one enclosure) back on its stored wall point, as after the enclosure
    moved or was resized; a seat whose enclosure is gone is dropped. Returns the ids that moved."""
    moved = []
    for n in rig["nodes"]:
        wall = n.get("wall")
        if not wall or (eid is not None and wall.get("enc") != eid):
            continue
        enc = _enc(rig, wall.get("enc"))
        if enc is None or wall.get("side") not in WALL_SIDES:
            n.pop("wall", None)
            continue
        x, y = seated_xy(enc, wall, n)
        if (n.get("x"), n.get("y")) != (x, y):
            n["x"], n["y"] = x, y
            moved.append(n["id"])
    return moved


def settle_walls(rig: dict) -> list[str]:
    """After any edit: each seated part is projected back onto its enclosure's outline from where it now is
    (a nudge slides it along the wall); one pushed further than WALL_PULL comes off. Returns the ids moved."""
    moved = []
    for n in rig["nodes"]:
        if not n.get("wall"):
            continue
        x0, y0 = n.get("x", 0), n.get("y", 0)
        enc = _enc(rig, n["wall"].get("enc"))
        if enc is not None and n["wall"].get("side") in WALL_SIDES and seated_xy(enc, n["wall"], n) == (x0, y0):
            continue                                # already in its seat: its stored place stands (no drift)
        x, y = seat_near(rig, n, x0, y0, keep=True)
        if not n.get("wall"):
            continue
        if (x, y) != (x0, y0):
            n["x"], n["y"] = x, y
            moved.append(n["id"])
    return moved
