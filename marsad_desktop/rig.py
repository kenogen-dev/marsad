"""Rigs as connected parts, and the bridge to today's chain.json.

A rig is a drawing: parts (nodes) placed on a grid and joined by wires. Most
rigs are a straight line; a splitter gives a part one input and two or more
outputs, so one antenna can feed two radios. Every path through the drawing,
from a source to its last part, is one chain, and its text is what a run records.

Storage keeps the old web app working unchanged:

- chain.json stays exactly as today: named, straight chains. Each path of a rig
  is written there as an ordinary chain. A splitter is written as a custom part
  (name = the splitter's name, detail = the output port's label), because the old
  app rejects part types it does not know.
- chain_layout.json, beside it, holds only the drawing: where each part sits, the
  wires, and which chain.json entries each rig wrote (with the text written), so a
  later change made in the old app is noticed and never overwritten silently.

The text of a path comes from marsad.chain.chain_text, the same function the old
app and every run use, so the two can never format a part differently.

Pure Python, no Qt: the tests run it without a window.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from marsad import chain as engine_chain

LAYOUT_FILE = "chain_layout.json"
GRID = 20                      # px; parts snap to multiples of this
PART_W, PART_H = 140, 64       # a part's box on the canvas
ROW_STEP = 180                 # spacing of an automatic straight row

# The palette: today's part types, plus the splitter, which exists only in drawings.
SPLITTER = "splitter"
# Relay 015: a USB hub, also drawing-only as a type. In a path it is written as a custom part named
# "USB hub", as the splitter is, because the old app rejects part types it does not know.
HUB = "usb_hub"
PART_TYPES = {**copy.deepcopy(engine_chain.PART_TYPES),
              SPLITTER: {"group": "RF", "label": "Splitter", "fields": ["ports"]},
              HUB: {"group": "USB", "label": "USB hub", "fields": []}}
# Relay 015: end devices that take several inputs - both radios plug into one PC, several radios into one hub.
# A free input appears whenever all are used. Every other part has one input.
MULTI_IN_TYPES = ("pc", HUB)
FIELDS = ("length", "db", "rx")    # the extra fields chain.json keeps per part
# Cables are drawn on the wire between two parts, not as parts. In the recorded text a
# wire's cables are written, in order, between the two parts it joins, exactly where a
# cable part sat in a straight chain, so the text does not change.
CABLE_TYPES = ("coax", "jumper", "usb")
# Enclosures: boxes drawn behind parts (the radio in its cage). Drawing only: they live in
# chain_layout.json, in their own list beside the parts, so no path ever walks one and
# they can never reach a recorded text.
ENCLOSURE = "enclosure"
ENC_W, ENC_H = 400, 240
ENC_MIN_W, ENC_MIN_H = 160, 100
ENC_MAX = 4000
# Power (relay 005): power sources are boxes of their own and power wires join them to the
# parts they feed. Both live in their own lists, like enclosures, so the path walk
# (paths(), which reads only "nodes" and "wires") can never follow one into a text.
POWER = "power"
POWER_KINDS = {"biastee": "Bias-tee (from the radio)", "usbc": "USB-C lead", "usb": "USB lead",
               "injector": "Power injector", "battery": "Battery"}
POWER_W, POWER_H = 140, 48
NEEDS_POWER_BY_TYPE = {"lna"}              # parts that need power unless the owner says otherwise
# Rig lists that only the drawing reads.
DRAWING_ONLY = ("enclosures", "groups", "power", "power_wires")
# Keys on a part's node (not on its part) that only the drawing reads. path_parts() copies
# type, name, detail and FIELDS from node["part"] and nothing else, so none of these can
# reach a recorded text. The one deliberate exception (relay 006, the owner's decision A2):
# an adapter's chosen shape and gender, node["sma"], are written as its detail by
# adapter_kind_text(); an adapter with none chosen writes exactly what it always did.
NODE_DRAWING_KEYS = ("x", "y", "w", "h", "z", "ports", "locked", "needs_power", "gain_db", "colour", "sma",
                     "shape", "antenna", "text_pos")

# SMA parts (relay 006): their own section in the Parts list, a gender on each port, and
# direct mating (port to port, no wire drawn), as real adapters screw together.
SMA_TYPES = ("adapter", "dcblock", "attenuator", "bulkhead_sma")
ADAPTER_SHAPES = {"straight": "straight", "90": "90°", "T": "T", "180": "180°"}
# Genders read from the source side: "MF" is male toward the source, female toward the radio.
GENDERS = {"MM": "M:M", "FF": "F:F", "MF": "M:F", "FM": "F:M"}
GENDER_WORD = {"M": "male (pin)", "F": "female (socket)"}
SIDES_CW = ("right", "bottom", "left", "top")     # a turn of 90° moves a side one step along this


def labels_from(part_types: dict | None) -> dict:
    """The service's palette (station's chain_labels applied) plus the splitter."""
    out = copy.deepcopy(part_types or engine_chain.PART_TYPES)
    out.setdefault(SPLITTER, PART_TYPES[SPLITTER])
    out.setdefault(HUB, PART_TYPES[HUB])
    return out


# ---------------------------------------------------------------- the drawing
def new_rig() -> dict:
    return {"nodes": [], "wires": []}


def new_node(rig: dict, type_: str, x: float = 0, y: float = 0, labels: dict | None = None) -> dict:
    labels = labels or PART_TYPES
    nid = _next_id(rig)
    part = {"type": type_, "name": labels.get(type_, {}).get("label", type_), "detail": ""}
    node = {"id": nid, "part": part, "x": snap(x), "y": snap(y)}
    if _layered(rig):                      # a new part lands on top
        node["z"] = max(_z_of(rig).values()) + 1
    if type_ == SPLITTER:
        node["ports"] = ["out 1", "out 2"]
    rig["nodes"].append(node)
    return node


def _next_id(rig: dict) -> str:
    used = {n["id"] for n in rig["nodes"]}
    i = len(used) + 1
    while f"n{i}" in used:
        i += 1
    return f"n{i}"


def _free_id(prefix: str, taken) -> str:
    taken = set(taken)
    k = 1
    while f"{prefix}{k}" in taken:
        k += 1
    return f"{prefix}{k}"


def snap(v: float, grid: int = GRID) -> int:
    return int(round(float(v) / grid) * grid)


def multi_in(node: dict | None) -> bool:
    """Relay 015: a part that takes several inputs (the PC, a USB hub)."""
    return bool(node) and node["part"]["type"] in MULTI_IN_TYPES


def in_slot(w: dict) -> int:
    """Which input of its far part a wire uses: always 0 on a one-input part."""
    v = w.get("in", 0)
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0


def used_inputs(rig: dict, nid: str) -> list[int]:
    return sorted(in_slot(w) for w in rig["wires"] if w["to"] == nid)


def free_input(rig: dict, nid: str) -> int:
    """The input a new wire into this part would take: the lowest one not in use."""
    used = set(used_inputs(rig, nid))
    k = 0
    while k in used:
        k += 1
    return k


def out_ports(node: dict) -> int:
    """How many outputs a part has: a splitter one per port, a T adapter two, anything else one."""
    if node["part"]["type"] == SPLITTER:
        return max(2, len(node.get("ports") or []))
    if node["part"]["type"] == "adapter" and (node.get("sma") or {}).get("shape") == "T":
        return 2
    return 1


# ---------------------------------------------------------------- SMA kinds and genders (relay 006)
def sma(node: dict) -> dict:
    return node.get("sma") or {}


def adapter_kind_text(node: dict) -> str:
    """An adapter's chosen kind as its recorded detail, e.g. "90° M:F"; "" when none is chosen."""
    if node["part"]["type"] != "adapter":
        return ""
    s = sma(node)
    bits = [ADAPTER_SHAPES[s["shape"]]] if s.get("shape") in ADAPTER_SHAPES else []
    if s.get("gender") in GENDERS:
        bits.append(GENDERS[s["gender"]])
    return " ".join(bits)


def port_sides(node: dict) -> dict | None:
    """Fixed sides for an adapter whose shape is chosen: {"in": side, "out": [side, ...]},
    turned by its quarter turns. None for every other part (its points face what they join)."""
    if node["part"]["type"] != "adapter" or sma(node).get("shape") not in ADAPTER_SHAPES:
        return None
    base = {"straight": ("left", ["right"]), "90": ("left", ["bottom"]),
            "T": ("left", ["right", "bottom"]), "180": ("left", ["left"])}[sma(node)["shape"]]
    turn = int(sma(node).get("turn") or 0) % 4

    def t(side):
        return SIDES_CW[(SIDES_CW.index(side) + turn) % 4]

    return {"in": t(base[0]), "out": [t(s) for s in base[1]]}


def port_gender(node: dict, kind: str, port: int = 0) -> str | None:
    """"M", "F" or None (not known) for one connection point of an SMA part. Drawing only,
    except that an adapter's gender is part of its recorded kind."""
    if node["part"]["type"] not in SMA_TYPES:
        return None
    g = sma(node).get("gender")
    if g not in GENDERS:
        return None
    return g[0] if kind == "in" else g[1]


# ---------------------------------------------------------------- shapes (relay 007, drawing only)
# What a node looks like. A part draws its kind's shape unless the owner picked one ("shape" on
# its node). Adapters draw as a pipe through their own connection points (barrel, elbow, T or U).
SHAPES = {"rect": "Rectangle", "rounded": "Rounded", "pill": "Pill", "circle": "Circle",
          "hexagon": "Hexagon", "triangle": "Triangle", "trapezoid": "Trapezoid", "pipe": "Pipe (adapters)"}
DEFAULT_SHAPE = "rect"            # relay 008: every kind starts as a rectangle; the owner picks any other


def shape_of(node: dict) -> str:
    """The shape a part is drawn in: the owner's pick, else a rectangle (relay 008). A pipe is an
    adapter's own drawing, offered for adapters only."""
    s = node.get("shape")
    if s == "pipe" and node["part"]["type"] != "adapter":
        return DEFAULT_SHAPE
    return s if s in SHAPES else DEFAULT_SHAPE


# Where a node's words sit (relay 008): t/m/b (top, middle, bottom) then l/c/r (left, centre, right).
TEXT_POS = ("tl", "tc", "tr", "ml", "mc", "mr", "bl", "bc", "br")
TEXT_POS_NAMES = {"t": "top", "m": "middle", "b": "bottom", "l": "left", "c": "centre", "r": "right"}
DEFAULT_TEXT_POS = "ml"


def text_pos(box: dict) -> str:
    return box.get("text_pos") if box.get("text_pos") in TEXT_POS else DEFAULT_TEXT_POS


# ---------------------------------------------------------------- antenna settings (relay 007)
# Drawing and Output only: they never enter the recorded text (path_parts reads node["part"],
# and these live on the node). Whether they should reach run records is the owner's call later.
ANTENNA_TYPES = {
    "whip": ("Whip / monopole", [("length", "Length")]),
    "dipole": ("Dipole", [("element", "Element length (each)"), ("orientation", "Orientation")]),
    "discone": ("Discone", [("cutoff", "Lowest frequency")]),
    "yagi": ("Yagi", [("elements", "Elements"), ("boom", "Boom length")]),
    "lpda": ("Log-periodic", [("range", "Frequency range"), ("boom", "Boom length")]),
    "patch": ("Patch", [("size", "Size")]),
    "helix": ("Helix", [("turns", "Turns"), ("hand", "Handedness")]),
    "dish": ("Dish", [("diameter", "Diameter"), ("feed", "Feed")]),
    "loop": ("Loop", [("diameter", "Diameter"), ("loop_turns", "Turns")]),
    "active": ("Active antenna", [("supply", "Supply")]),
    "custom": ("Custom", [("description", "Description")]),
}
ANTENNA_COMMON = [("az", "Pointing azimuth, °"), ("el", "Pointing elevation, °"), ("pol", "Polarisation"),
                  ("height", "Mount height"), ("gain_dbi", "Nominal gain, dBi")]
ANTENNA_NUMBERS = ("az", "el", "gain_dbi", "elements", "turns", "loop_turns")


def antenna(node: dict) -> dict:
    return node.get("antenna") or {}


def antenna_lines(node: dict) -> list[tuple[str, str]]:
    """An antenna's settings as (label, value) rows, in the order the card shows them."""
    a = antenna(node)
    if not a.get("type"):
        return []
    label, fields = ANTENNA_TYPES.get(a["type"], ANTENNA_TYPES["custom"])
    out = [("Type", label)]
    for key, lab in fields + ANTENNA_COMMON:
        v = a.get(key)
        if v not in (None, ""):
            unit = {"az": "°", "el": "°", "gain_dbi": " dBi"}.get(key, "")
            out.append((lab.split(",")[0], f"{v:g}{unit}" if isinstance(v, (int, float)) else f"{v}{unit}"))
    return out


# ---------------------------------------------------------------- templates (relay 007)
TEMPLATES = {"blank": "Blank", "one_radio": "Antenna to one radio",
             "lna_radio": "Antenna, LNA, one radio", "two_radios": "Antenna, splitter, two radios"}
# Relay 015: one line under the preview in New rig, saying what each start gives.
TEMPLATE_NOTES = {"blank": "An empty grid: draw the rig from the Parts list.",
                  "one_radio": "Antenna, coax, one radio and the PC, wired in a row.",
                  "lna_radio": "Antenna, LNA, coax, one radio and the PC, wired in a row.",
                  "two_radios": "Antenna into a splitter feeding two radios: the start of a two-radio rig."}


def template(key: str, labels: dict | None = None, receivers: list | None = None) -> dict:
    """A starting drawing for New rig. Plain parts in a row, wired; nothing saved until Save."""
    rig = new_rig()
    rx = list(receivers or [])

    def node(t, x, y, name=None, rx_i=None):
        n = new_node(rig, t, x, y, labels)
        if name:
            n["part"]["name"] = name
        if t == "sdr" and rx_i is not None and rx_i < len(rx):
            n["part"]["rx"] = rx[rx_i]
            n["part"]["name"] = rx[rx_i]
        return n["id"]

    if key == "one_radio":
        ids = [node("antenna", 0, 0), node("sdr", 240, 0, rx_i=0), node("pc", 480, 0)]
        connect(rig, ids[0], 0, ids[1])
        connect(rig, ids[1], 0, ids[2])
        w = wire_of(rig, ids[0], 0)
        w["cables"] = [new_cable("coax", labels)]
    elif key == "lna_radio":
        ids = [node("antenna", 0, 0), node("lna", 200, 0), node("sdr", 440, 0, rx_i=0), node("pc", 680, 0)]
        for a, b in zip(ids, ids[1:]):
            connect(rig, a, 0, b)
        wire_of(rig, ids[1], 0)["cables"] = [new_cable("coax", labels)]
    elif key == "two_radios":
        a, sp = node("antenna", 0, 100), node(SPLITTER, 220, 100)
        r1, r2 = node("sdr", 480, 0, rx_i=0), node("sdr", 480, 200, rx_i=1)
        connect(rig, a, 0, sp)
        connect(rig, sp, 0, r1)
        connect(rig, sp, 1, r2)
    elif key != "blank":
        raise ValueError(f"unknown template {key!r}")
    return rig


# ---------------------------------------------------------------- lengths and Output (relay 007)
def length_m(text) -> float | None:
    """A typed cable length in metres: "2 m", "3 ft", '6"', "1'", "150 cm", "8ft". None if unreadable."""
    import re
    if text in (None, ""):
        return None
    t = str(text).strip().lower().replace(",", ".")
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(m|metres?|meters?|cm|mm|ft|feet|foot|'|in|inch(?:es)?|\")?", t)
    if not m:
        return None
    v = float(m.group(1))
    unit = m.group(2) or "m"
    return v * {"m": 1, "cm": 0.01, "mm": 0.001, "ft": 0.3048, "feet": 0.3048, "foot": 0.3048, "'": 0.3048,
                "in": 0.0254, "inch": 0.0254, "inches": 0.0254, '"': 0.0254}.get(unit, 1 if unit.startswith("met") else 1)


def path_report(rig: dict, path, labels: dict | None = None) -> dict:
    """Technical data for one path, for the Output panel. Everything here is read from the drawing;
    the estimate is the same nominal one as the Recorded text panel."""
    nodes = {n["id"]: n for n in rig["nodes"]}
    ids = [nid for nid, _ in path]
    lengths, unread, cables, joins, mates = 0.0, [], 0, 0, 0
    gender_notes = []
    for nid, port in path:
        if port is None:
            continue
        w = wire_of(rig, nid, port)
        if w is None:
            continue
        joins += 1 + len(w.get("cables") or [])
        cables += len(w.get("cables") or [])
        if mated(w):
            mates += 1
            ga, gb = port_gender(nodes[w["from"]], "out", w["port"]), port_gender(nodes[w["to"]], "in")
            if ga and gb:
                gender_notes.append(f"{_pname(nodes[w['from']])} {ga} → {gb} {_pname(nodes[w['to']])}: "
                                    + ("won't screw together" if ga == gb else "fits"))
        for c in w.get("cables") or []:
            v = length_m(c.get("length"))
            if v is None:
                unread.append(str(c.get("name") or c.get("type")))
            else:
                lengths += v
    fed = {w["to"] for w in power_wires(rig)}
    power = [(_pname(nodes[i]), i in fed) for i in ids if needs_power(nodes[i])]
    ant = next((nodes[i] for i in ids if nodes[i]["part"]["type"] == "antenna"), None)
    return {"parts": [_pname(nodes[i]) for i in ids], "text": engine_chain.chain_text(path_parts(rig, path, labels)),
            "cable_m": round(lengths, 3), "cables": cables, "unread_lengths": unread, "joins": joins,
            "mated": mates, "connectors": 2 * joins, "genders": gender_notes, "power": power,
            "estimate": estimate(rig, path),
            "antenna_gain": antenna(ant).get("gain_dbi") if ant is not None else None}


def mated(w: dict) -> bool:
    return bool(w.get("mated"))


def mate(rig: dict, src: str, port: int, dst: str) -> str | None:
    """Join two parts port to port with no wire drawn, as SMA parts screw together. In a path
    a mating is exactly a wire with no cable on it. Returns why not, or None when done."""
    why = connect(rig, src, port, dst)
    if why is None:
        wire_of(rig, src, port)["mated"] = True
    return why


def assembly(rig: dict, nid: str) -> set:
    """The part and everything mated to it, directly or through others: they move as one."""
    out, todo = {nid}, [nid]
    while todo:
        n = todo.pop()
        for w in rig["wires"]:
            if mated(w) and n in (w["from"], w["to"]):
                other = w["to"] if w["from"] == n else w["from"]
                if other not in out:
                    out.add(other)
                    todo.append(other)
    return out


def set_sma(node: dict, **kw) -> None:
    """Set an SMA part's shape, gender or turn (None clears it); an empty choice leaves no key."""
    s = dict(sma(node))
    for k, v in kw.items():
        if not v:                          # None, "" or a turn of 0: nothing chosen
            s.pop(k, None)
        else:
            s[k] = v
    if s:
        node["sma"] = s
    else:
        node.pop("sma", None)


def connect(rig: dict, src: str, port: int, dst: str) -> str | None:
    """Wire src's output `port` to dst's input. Returns why not, or None when done.
    A part has one input; an output carries one wire. Replacing is explicit: the
    caller removes the old wire first."""
    nodes = {n["id"]: n for n in rig["nodes"]}
    if src not in nodes or dst not in nodes:
        return "that part is not in the rig"
    if src == dst:
        return "a part cannot feed itself"
    if not 0 <= port < out_ports(nodes[src]):
        return "that output does not exist"
    if engine_chain.PART_TYPES.get(nodes[dst]["part"]["type"], {}).get("source"):
        return f"{nodes[dst]['part']['name']} is a source: nothing feeds it"
    many = multi_in(nodes[dst])
    for w in rig["wires"]:
        if w["from"] == src and w["port"] == port:
            return "that output is already wired"
        if w["to"] == dst and not many:
            return f"{nodes[dst]['part']['name']} already has an input"
        if w["to"] == dst and w["from"] == src:
            return f"{nodes[src]['part']['name']} is already wired into {nodes[dst]['part']['name']}"
    if _reaches(rig, dst, src):
        return "that wire would make a loop"
    w = {"from": src, "port": port, "to": dst}
    if many:                               # relay 015: which of the PC's (or hub's) inputs it takes; drawing only
        w["in"] = free_input(rig, dst)
    rig["wires"].append(w)
    return None


WIRE_KEEP = ("cables", "points", "square")      # what a wire carries when one of its ends is moved (relay 015)


def free_outputs(rig: dict, nid: str) -> list[int]:
    node = next((n for n in rig["nodes"] if n["id"] == nid), None)
    if node is None:
        return []
    used = {w["port"] for w in rig["wires"] if w["from"] == nid}
    return [i for i in range(out_ports(node)) if i not in used]


def reconnect(rig: dict, src: str, port: int, end: str, part: str, part_port: int | None = None) -> str | None:
    """Relay 015: move one end of a placed wire to another part. end="to" moves the far end onto `part`'s input
    (a free one on a PC or hub); end="from" moves the near end onto one of `part`'s outputs (`part_port` if it is
    free, else its first free output). The wire keeps its cables, bend points and right-angle choice, and its
    place in the list. Returns why not (and changes nothing), or None when done."""
    w = wire_of(rig, src, port)
    if w is None:
        return "that wire is not in the rig"
    if mated(w):
        return "a direct mating has no wire ends: separate the parts first"
    if (end == "to" and part == w["to"]) or (end == "from" and part == src and part_port in (None, port)):
        return None                                   # put back where it was
    k = rig["wires"].index(w)
    rig["wires"].remove(w)
    if end == "to":
        why, new_from, new_port, dst = None, src, port, part
    else:
        free = free_outputs(rig, part)
        if part == src:
            free = sorted(set(free) | {port})
        new_port = part_port if part_port in free else (free[0] if free else None)
        nodes = {n["id"]: n for n in rig["nodes"]}
        name = (nodes.get(part) or {}).get("part", {}).get("name") or "that part"
        why = None if new_port is not None else f"{name} has no free output"
        new_from, dst = part, w["to"]
    why = why or connect(rig, new_from, new_port, dst)
    if why:
        rig["wires"].insert(k, w)
        return why
    new = rig["wires"].pop()
    if end == "from" and "in" in w and "in" in new:  # the far end stays on the same input of a PC or hub
        new["in"] = w["in"]
    for key in WIRE_KEEP:
        if key in w:
            new[key] = w[key]
    rig["wires"].insert(k, new)
    return None


def disconnect(rig: dict, src: str, port: int) -> None:
    rig["wires"] = [w for w in rig["wires"] if not (w["from"] == src and w["port"] == port)]


def remove_node(rig: dict, nid: str) -> None:
    """Take a part out of the drawing (an edit of the rig, not a deletion of data)."""
    rig["nodes"] = [n for n in rig["nodes"] if n["id"] != nid]
    rig["wires"] = [w for w in rig["wires"] if nid not in (w["from"], w["to"])]
    _drop_power_wires(rig, lambda w: w["to"] == nid)
    clean_groups(rig)


def _reaches(rig: dict, a: str, b: str) -> bool:
    """True when following wires from a gets to b."""
    seen, todo = set(), [a]
    while todo:
        n = todo.pop()
        if n == b:
            return True
        if n in seen:
            continue
        seen.add(n)
        todo += [w["to"] for w in rig["wires"] if w["from"] == n]
    return False


# ---------------------------------------------------------------- paths and text
def paths(rig: dict) -> list[list[tuple[str, int | None]]]:
    """Every path from a start part (nothing wired into it) to an end part
    (nothing wired out of it). A step is (node id, output port taken), with None
    on the last part. Order: by the start part's position, then by port."""
    nodes = {n["id"]: n for n in rig["nodes"]}
    into = {w["to"] for w in rig["wires"]}
    starts = sorted((n for n in rig["nodes"] if n["id"] not in into),
                    key=lambda n: (n["y"], n["x"], n["id"]))
    outs: dict[str, list] = {}
    for w in rig["wires"]:
        outs.setdefault(w["from"], []).append(w)
    found = []

    def walk(nid, trail):
        ws = sorted(outs.get(nid, []), key=lambda w: w["port"])
        if not ws:
            found.append(trail + [(nid, None)])
            return
        for w in ws:
            walk(w["to"], trail + [(nid, w["port"])])

    for s in starts:
        walk(s["id"], [])
    return found


def path_parts(rig: dict, path, labels: dict | None = None) -> list[dict]:
    """The path as chain.json parts. A splitter becomes a custom part whose detail
    names the output taken. Normalised the same way chain.json normalises parts."""
    nodes = {n["id"]: n for n in rig["nodes"]}
    wires = {(w["from"], w["port"]): w for w in rig["wires"]}
    labels = labels_from(labels)
    out = []
    for nid, port in path:
        node = nodes[nid]
        p = node["part"]
        if p["type"] == SPLITTER:
            ports = node.get("ports") or []
            port_label = str(ports[port]).strip() if port is not None and port < len(ports) else ""
            q = {"type": "custom", "name": str(p.get("name") or "").strip(),
                 "detail": port_label or str(p.get("detail") or "").strip()}
        elif p["type"] == HUB:              # relay 015: written as a custom part, like the splitter
            q = {"type": "custom", "name": str(p.get("name") or "").strip() or PART_TYPES[HUB]["label"],
                 "detail": str(p.get("detail") or "").strip()}
        else:
            q = {"type": p["type"], "name": str(p.get("name") or "").strip(),
                 "detail": str(p.get("detail") or "").strip()}
            kind = adapter_kind_text(node)          # "" unless the owner chose an adapter's kind
            if kind:
                q["detail"] = f"{kind}, {q['detail']}" if q["detail"] else kind
            for f in FIELDS:
                if p.get(f) not in (None, ""):
                    q[f] = p[f]
        out.append(engine_chain.canonical(q, labels))
        if port is not None:
            for c in (wires.get((nid, port)) or {}).get("cables") or []:
                out.append(engine_chain.canonical(cable_part(c), labels))
    return out


def cable_part(c: dict) -> dict:
    """A wire's cable as a chain.json part."""
    q = {"type": c.get("type"), "name": str(c.get("name") or "").strip(),
         "detail": str(c.get("detail") or "").strip()}
    if c.get("length") not in (None, ""):
        q["length"] = c["length"]
    return q


def new_cable(type_: str, labels: dict | None = None) -> dict:
    labels = labels or PART_TYPES
    return {"type": type_, "name": labels.get(type_, {}).get("label", type_), "detail": ""}


def enclosures(rig: dict) -> list[dict]:
    return rig.get("enclosures") or []


def new_enclosure(rig: dict, x: float, y: float, w: int = ENC_W, h: int = ENC_H,
                  name: str = "Enclosure") -> dict:
    taken = {e["id"] for e in enclosures(rig)}
    k = 1
    while f"e{k}" in taken:
        k += 1
    e = {"id": f"e{k}", "name": name, "note": "", "x": snap(x), "y": snap(y), "w": w, "h": h}
    if _layered(rig):                      # a new box starts behind everything
        e["z"] = min(_z_of(rig).values()) - 1
    rig.setdefault("enclosures", []).append(e)
    return e


def remove_enclosure(rig: dict, eid: str) -> None:
    """Takes the box away; the parts that sat in it stay where they are."""
    rig["enclosures"] = [e for e in enclosures(rig) if e["id"] != eid]
    if not rig["enclosures"]:
        rig.pop("enclosures")
    clean_groups(rig)


# ---------------------------------------------------------------- layering and groups
# Drawing only, like enclosures: a box's stacking order ("z" on a part or an enclosure)
# and the groups list live in chain_layout.json and no path ever reads them. A rig that
# was never layered has no "z" anywhere and stacks as it always did: enclosures behind,
# then parts in list order.
ENC_PRESETS = {"aluminium": "Aluminium enclosure", "plastic": "Plastic enclosure"}


def _boxes(rig: dict) -> list[dict]:
    return list(enclosures(rig)) + list(rig["nodes"]) + list(power_boxes(rig))


def _layered(rig: dict) -> bool:
    return any("z" in b for b in _boxes(rig))


def stack(rig: dict) -> list[str]:
    """Every box's id, bottom to top."""
    order = []
    for k, b in enumerate(_boxes(rig)):
        default = -1 if b["id"] in {e["id"] for e in enclosures(rig)} else 0
        order.append((b.get("z", default), k, b["id"]))
    return [i for _, _, i in sorted(order)]


def _z_of(rig: dict) -> dict:
    return {i: k for k, i in enumerate(stack(rig))}


ARRANGE = ("front", "forward", "backward", "back")


def restack(rig: dict, ids, how: str) -> bool:
    """Bring to front, bring forward, send backward or send to back the boxes named in
    ids (their order among themselves is kept). Writes 0..n-1 as every box's z. Returns
    whether anything moved."""
    if how not in ARRANGE:
        raise ValueError(f"unknown arrangement {how!r}")
    order = stack(rig)
    pick = set(ids) & set(order)
    if not pick:
        return False
    new = list(order)
    if how == "front":
        new = [i for i in order if i not in pick] + [i for i in order if i in pick]
    elif how == "back":
        new = [i for i in order if i in pick] + [i for i in order if i not in pick]
    elif how == "forward":                 # each picked box steps over the next one above it
        for k in range(len(new) - 2, -1, -1):
            if new[k] in pick and new[k + 1] not in pick:
                new[k], new[k + 1] = new[k + 1], new[k]
    else:
        for k in range(1, len(new)):
            if new[k] in pick and new[k - 1] not in pick:
                new[k], new[k - 1] = new[k - 1], new[k]
    if new == order:                       # nothing moves: write nothing either
        return False
    z = {i: k for k, i in enumerate(new)}
    for b in _boxes(rig):
        b["z"] = z[b["id"]]
    return True


def groups(rig: dict) -> list[dict]:
    return rig.get("groups") or []


def group_of(rig: dict, bid: str) -> dict | None:
    return next((g for g in groups(rig) if bid in g["members"]), None)


def make_group(rig: dict, ids) -> dict | None:
    """Group these boxes. A box already in a group brings its whole group in, so a box
    is never in two groups. Needs two boxes or more; returns the new group or None."""
    known = {b["id"] for b in _boxes(rig)}
    members = [i for i in dict.fromkeys(ids) if i in known]
    for g in [g for g in groups(rig) if set(g["members"]) & set(members)]:
        members += [m for m in g["members"] if m not in members]
        rig["groups"].remove(g)
    if len(members) < 2:
        clean_groups(rig)
        return None
    taken = {g["id"] for g in groups(rig)}
    k = 1
    while f"g{k}" in taken:
        k += 1
    g = {"id": f"g{k}", "members": members}
    rig.setdefault("groups", []).append(g)
    return g


def ungroup(rig: dict, ids) -> int:
    """Dissolve every group holding one of these boxes. The boxes stay where they are."""
    hit = [g for g in groups(rig) if set(g["members"]) & set(ids)]
    for g in hit:
        rig["groups"].remove(g)
    clean_groups(rig)
    return len(hit)


def clean_groups(rig: dict) -> None:
    """Drop members that are no longer in the drawing, and groups left with fewer than two."""
    if "groups" not in rig:
        return
    known = {b["id"] for b in _boxes(rig)}
    rig["groups"] = [dict(g, members=[m for m in g["members"] if m in known]) for g in rig["groups"]]
    rig["groups"] = [g for g in rig["groups"] if len(g["members"]) >= 2]
    if not rig["groups"]:
        rig.pop("groups")


# ---------------------------------------------------------------- power (drawing only)
def power_boxes(rig: dict) -> list[dict]:
    return rig.get("power") or []


def power_wires(rig: dict) -> list[dict]:
    return rig.get("power_wires") or []


def new_power(rig: dict, kind: str, x: float, y: float) -> dict:
    """A power source box (USB-C lead, injector, battery, the radio's bias-tee)."""
    if kind not in POWER_KINDS:
        raise ValueError(f"unknown power source {kind!r}")
    p = {"id": _free_id("p", (b["id"] for b in power_boxes(rig))), "kind": kind,
         "name": POWER_KINDS[kind], "note": "", "x": snap(x), "y": snap(y)}
    if _layered(rig):
        p["z"] = max(_z_of(rig).values()) + 1
    rig.setdefault("power", []).append(p)
    return p


def needs_power(node: dict) -> bool:
    """Does this part need a power feed? An LNA does unless the owner unticked it; any
    other part only when the owner ticked "needs power" (an active antenna, say)."""
    if "needs_power" in node:
        return bool(node["needs_power"])
    return node["part"]["type"] in NEEDS_POWER_BY_TYPE


def power_feed(rig: dict, nid: str) -> dict | None:
    return next((w for w in power_wires(rig) if w["to"] == nid), None)


def connect_power(rig: dict, src: str, dst: str) -> str | None:
    """Join a power source to a part that needs power. Returns why not, or None when done."""
    boxes = {b["id"]: b for b in power_boxes(rig)}
    nodes = {n["id"]: n for n in rig["nodes"]}
    if src not in boxes:
        return "a power wire starts at a power source"
    if dst not in nodes:
        return "a power wire ends at a part"
    if not needs_power(nodes[dst]):
        return (f"{nodes[dst]['part'].get('name') or 'that part'} does not need power "
                "(tick \"Needs power\" on its card first)")
    if power_feed(rig, dst):
        return f"{nodes[dst]['part'].get('name') or 'that part'} already has a power feed"
    rig.setdefault("power_wires", []).append({"from": src, "to": dst})
    return None


def _drop_power_wires(rig: dict, gone) -> None:
    if "power_wires" not in rig:
        return
    rig["power_wires"] = [w for w in rig["power_wires"] if not gone(w)]
    if not rig["power_wires"]:
        rig.pop("power_wires")


def disconnect_power(rig: dict, dst: str) -> None:
    _drop_power_wires(rig, lambda w: w["to"] == dst)


def remove_power(rig: dict, pid: str) -> None:
    """Take a power source out of the drawing, with the power wires leaving it."""
    rig["power"] = [b for b in power_boxes(rig) if b["id"] != pid]
    if not rig["power"]:
        rig.pop("power")
    _drop_power_wires(rig, lambda w: w["from"] == pid)
    clean_groups(rig)


def remove_box(rig: dict, bid: str) -> None:
    """Take any box (part, enclosure or power source) out of the drawing."""
    if any(n["id"] == bid for n in rig["nodes"]):
        remove_node(rig, bid)
    elif any(e["id"] == bid for e in enclosures(rig)):
        remove_enclosure(rig, bid)
    elif any(b["id"] == bid for b in power_boxes(rig)):
        remove_power(rig, bid)


# ---------------------------------------------------------------- lock (drawing only)
def box(rig: dict, bid: str) -> dict | None:
    return next((b for b in _boxes(rig) if b["id"] == bid), None)


def kind_of(rig: dict, bid: str) -> str | None:
    """"part", "enclosure" or "power"."""
    if any(n["id"] == bid for n in rig["nodes"]):
        return "part"
    if any(e["id"] == bid for e in enclosures(rig)):
        return "enclosure"
    if any(b["id"] == bid for b in power_boxes(rig)):
        return "power"
    return None


def is_locked(b: dict | None) -> bool:
    return bool(b and b.get("locked"))


def set_locked(rig: dict, ids, on: bool) -> bool:
    """Lock or unlock boxes. A locked box is never moved by a drag, a nudge, Align or
    Tidy. Returns whether anything changed."""
    hit = False
    for b in _boxes(rig):
        if b["id"] in set(ids) and is_locked(b) != on:
            if on:
                b["locked"] = True
            else:
                b.pop("locked", None)
            hit = True
    return hit


# ---------------------------------------------------------------- copy and paste (drawing only)
def fragment(rig: dict, ids) -> dict:
    """What Copy takes: the boxes named, the wires and power wires whose two ends are
    both among them, and the groups that lie wholly inside them. Deep copies."""
    ids = set(ids)
    nodes = [copy.deepcopy(n) for n in rig["nodes"] if n["id"] in ids]
    nids = {n["id"] for n in nodes}
    pids = {b["id"] for b in power_boxes(rig) if b["id"] in ids}
    out = {"nodes": nodes,
           "wires": [copy.deepcopy(w) for w in rig["wires"] if w["from"] in nids and w["to"] in nids],
           "enclosures": [copy.deepcopy(e) for e in enclosures(rig) if e["id"] in ids],
           "power": [copy.deepcopy(b) for b in power_boxes(rig) if b["id"] in pids],
           "power_wires": [copy.deepcopy(w) for w in power_wires(rig) if w["from"] in pids and w["to"] in nids],
           "groups": [copy.deepcopy(g) for g in groups(rig) if set(g["members"]) <= ids]}
    return {k: v for k, v in out.items() if v}


def paste(rig: dict, frag: dict, dx: float, dy: float) -> list[str]:
    """Paste a fragment, moved by (dx, dy), with fresh ids. Pasted boxes land on top of
    a layered rig; in a rig never layered they carry no stacking order. Returns the new ids."""
    frag = copy.deepcopy(frag)
    layered = _layered(rig)
    top = (max(_z_of(rig).values()) + 1) if layered and _boxes(rig) else 0
    tagged = ([("enclosure", b) for b in frag.get("enclosures", [])] + [("part", b) for b in frag.get("nodes", [])]
              + [("power", b) for b in frag.get("power", [])])
    order = sorted(enumerate(tagged), key=lambda t: (t[1][1].get("z", -1 if t[1][0] == "enclosure" else 0), t[0]))
    new_id, made = {}, []
    for _, (kind, b) in order:
        if kind == "part":
            nid = _free_id("n", [n["id"] for n in rig["nodes"]])
            rig["nodes"].append(b)
        elif kind == "enclosure":
            nid = _free_id("e", [e["id"] for e in enclosures(rig)])
            rig.setdefault("enclosures", []).append(b)
        else:
            nid = _free_id("p", [p["id"] for p in power_boxes(rig)])
            rig.setdefault("power", []).append(b)
        new_id[b["id"]] = nid
        b["id"] = nid
        b["x"], b["y"] = int(round(b["x"] + dx)), int(round(b["y"] + dy))
        if layered:
            b["z"] = top
            top += 1
        else:
            b.pop("z", None)
        made.append(nid)
    for w in frag.get("wires", []):
        rig["wires"].append(dict(w, **{"from": new_id[w["from"]], "to": new_id[w["to"]]}))
    for w in frag.get("power_wires", []):
        rig.setdefault("power_wires", []).append({"from": new_id[w["from"]], "to": new_id[w["to"]]})
    for g in frag.get("groups", []):
        rig.setdefault("groups", []).append({"id": _free_id("g", [h["id"] for h in groups(rig)]),
                                             "members": [new_id[m] for m in g["members"]]})
    return made


def wire_of(rig: dict, src: str, port: int) -> dict | None:
    return next((w for w in rig["wires"] if w["from"] == src and w["port"] == port), None)


def radio_of(parts: list[dict]) -> dict | None:
    for p in parts:
        if p.get("type") == "sdr":
            return p
    return None


def problems(rig: dict) -> list[str]:
    """Plain-language reasons the drawing is not a set of complete chains yet.
    The rig can still be saved as a drawing; a path without a radio is still a chain."""
    out = []
    if not rig["nodes"]:
        return ["the rig is empty"]
    for path in paths(rig):
        parts = path_parts(rig, path)
        if not engine_chain.PART_TYPES.get(parts[0]["type"], {}).get("source"):
            out.append(f"a chain starts at {parts[0]['name'] or parts[0]['type']}, not at an antenna, probe or load")
        if radio_of(parts) is None:
            out.append(f"the chain ending at {parts[-1]['name']} reaches no radio")
    for n in rig["nodes"]:
        if n["part"]["type"] == SPLITTER:
            used = {w["port"] for w in rig["wires"] if w["from"] == n["id"]}
            if len(used) < 2:
                out.append(f"{n['part']['name']} has {len(used)} of its outputs wired")
    return out


def _pname(node: dict) -> str:
    return str(node["part"].get("name") or "").strip() or PART_TYPES.get(node["part"]["type"], {}).get("label", "a part")


def checks(rig: dict) -> list[dict]:
    """The Checks strip (relay 005): problems as the owner builds, each with the boxes it
    is about, so a click can select them. They warn and never block.
    [{"kind": "wiring" | "radio" | "power" | "loose", "text": ..., "ids": [...]}]"""
    out = []
    nodes = {n["id"]: n for n in rig["nodes"]}
    if not nodes and not power_boxes(rig):
        return out
    wired = {w["from"] for w in rig["wires"]} | {w["to"] for w in rig["wires"]}
    loose = {nid for nid in nodes if nid not in wired} if len(nodes) > 1 else set()
    for path in paths(rig):
        ids = [nid for nid, _ in path]
        if len(ids) == 1 and ids[0] in loose:
            continue
        start, radio = nodes[ids[0]], next((nodes[i] for i in ids if nodes[i]["part"]["type"] == "sdr"), None)
        sourced = bool(engine_chain.PART_TYPES.get(start["part"]["type"], {}).get("source"))
        if not sourced and radio is not None:
            out.append({"kind": "radio", "ids": [start["id"], radio["id"]],
                        "text": f"No antenna, probe or load reaches {_pname(radio)}: its chain starts at {_pname(start)}"})
        elif not sourced:
            out.append({"kind": "wiring", "ids": [start["id"]], "text": f"Nothing is wired into {_pname(start)}"})
        if radio is None and sourced:
            last = nodes[ids[-1]]
            out.append({"kind": "radio", "ids": [last["id"]],
                        "text": f"The chain from {_pname(start)} ends at {_pname(last)} and reaches no radio"})
    for n in rig["nodes"]:
        if out_ports(n) > 1:                       # a splitter, or a T adapter
            used = {w["port"] for w in rig["wires"] if w["from"] == n["id"]}
            free = out_ports(n) - len(used)
            if free > 0 and n["id"] not in loose:
                out.append({"kind": "wiring", "ids": [n["id"]],
                            "text": f"{_pname(n)} has {free} output{'s' if free > 1 else ''} with nothing on it"})
    for w in rig["wires"]:                         # relay 006: two males or two females cannot mate
        if not mated(w) or w["from"] not in nodes or w["to"] not in nodes:
            continue
        a, b = nodes[w["from"]], nodes[w["to"]]
        ga, gb = port_gender(a, "out", w["port"]), port_gender(b, "in")
        if ga and ga == gb:
            at = a if a["part"]["type"] == "adapter" or b["part"]["type"] not in SMA_TYPES else b
            out.append({"kind": "wiring", "ids": [a["id"], b["id"]],
                        "text": f"{ga} to {gb} at {_pname(at)}: these won't screw together"})
    by_rx: dict[str, list] = {}
    for n in rig["nodes"]:
        if n["part"]["type"] == "sdr" and n["part"].get("rx"):
            by_rx.setdefault(str(n["part"]["rx"]), []).append(n["id"])
    for rx, ids in by_rx.items():
        if len(ids) > 1:
            out.append({"kind": "radio", "ids": ids, "text": f"{len(ids)} radios in this rig are all {rx}"})
    for nid in sorted(loose, key=lambda i: (nodes[i]["y"], nodes[i]["x"])):
        out.append({"kind": "loose", "ids": [nid], "text": f"{_pname(nodes[nid])} is on its own, connected to nothing"})
    fed = {w["to"] for w in power_wires(rig)}
    for n in rig["nodes"]:
        if needs_power(n) and n["id"] not in fed:
            out.append({"kind": "power", "ids": [n["id"]], "text": f"{_pname(n)} has no power feed"})
    feeding = {w["from"] for w in power_wires(rig)}
    for b in power_boxes(rig):
        if b["id"] not in feeding:
            out.append({"kind": "loose", "ids": [b["id"]],
                        "text": f"{b.get('name') or POWER_KINDS.get(b['kind'], 'power source')} powers nothing"})
    return out


# ---------------------------------------------------------------- nominal gain and loss (drawing only)
def figure(node_or_cable: dict, part: dict | None = None) -> float | None:
    """The owner's nominal gain (+) or loss (-) in dB for one part or cable, or None when
    none is typed. An attenuator with no figure typed counts as unknown, like any other part
    (the owner's answer to relay 005, given in relay 006): its marked dB is not assumed."""
    v = node_or_cable.get("gain_db")
    if v not in (None, ""):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    return None


def estimate(rig: dict, path) -> dict | None:
    """A path's nominal running total from its source to its radio: the figures the owner
    typed on the parts and on the cables of the wires in between. The source and the radio
    themselves are not counted. None when the path reaches no radio.
    {"total": dB, "known": n, "unknown": n}. An estimate only: never a measured level."""
    nodes = {n["id"]: n for n in rig["nodes"]}
    ids = [nid for nid, _ in path]
    k_radio = next((k for k, i in enumerate(ids) if nodes[i]["part"]["type"] == "sdr"), None)
    if k_radio is None:
        return None
    start = 1 if engine_chain.PART_TYPES.get(nodes[ids[0]]["part"]["type"], {}).get("source") else 0
    total, known, unknown = 0.0, 0, 0

    def count(v):
        nonlocal total, known, unknown
        if v is None:
            unknown += 1
        else:
            total += v
            known += 1

    for k in range(start, k_radio):
        count(figure(nodes[ids[k]], nodes[ids[k]]["part"]))
    for k in range(0, k_radio):
        w = wire_of(rig, path[k][0], path[k][1])
        for c in (w or {}).get("cables") or []:
            count(figure(c))
    return {"total": round(total, 2), "known": known, "unknown": unknown}


def estimate_text(e: dict | None) -> str:
    if e is None:
        return ""
    if not e["known"]:
        return f"est. no figures yet ({e['unknown']} part{'s' if e['unknown'] != 1 else ''} unknown)"
    t = e["total"]
    num = f"{t:+.1f}".replace("+0.0", "0.0").replace("-0.0", "0.0")
    tail = f" ({e['unknown']} part{'s' if e['unknown'] != 1 else ''} unknown)" if e["unknown"] else " (every part has a figure)"
    return f"est. {num} dB{tail}"


# ---------------------------------------------------------------- the owner's parts library (drawing only)
# Kept in chain_layout.json under "library" (beside chain.json, out of the public repository and
# in the private backup, like the drawings). Dropping an entry copies it: a rig keeps its copy
# whatever later happens to the entry, so nothing in a saved rig changes silently.
LIB_PART_KEYS = ("type", "name", "detail") + FIELDS
LIB_NODE_KEYS = ("w", "h", "ports", "needs_power", "gain_db", "colour", "sma", "shape", "antenna", "text_pos")


def library(layout: dict) -> list[dict]:
    return layout.get("library") or []


def lib_entry(node: dict, taken) -> dict:
    """A library entry from a part on the grid: its kind, name, note, fields and size.
    Not its receiver: a radio dropped from the library is matched to a receiver afresh."""
    part = {k: copy.deepcopy(node["part"][k]) for k in LIB_PART_KEYS
            if k in node["part"] and k != "rx" and node["part"][k] not in (None,)}
    e = {"id": _free_id("L", taken), "part": part}
    for k in LIB_NODE_KEYS:
        if k in node:
            e[k] = copy.deepcopy(node[k])
    return e


def node_from_lib(rig: dict, entry: dict, x: float, y: float) -> dict:
    """A new part on the grid from a library entry: the same name and note every time."""
    node = new_node(rig, entry["part"]["type"], x, y)
    node["part"] = {k: copy.deepcopy(v) for k, v in entry["part"].items() if k != "rx"}
    node["part"].setdefault("detail", "")
    node["part"].setdefault("name", "")
    for k in LIB_NODE_KEYS:
        if k in entry:
            node[k] = copy.deepcopy(entry[k])
    return node


def check_library(lst) -> list:
    if not isinstance(lst, list):
        raise ValueError("the parts library must be a list")
    seen = set()
    for e in lst:
        if (not isinstance(e, dict) or not isinstance(e.get("id"), str) or e["id"] in seen
                or not isinstance(e.get("part"), dict) or e["part"].get("type") not in PART_TYPES):
            raise ValueError("the parts library has an entry without an id or a known kind")
        seen.add(e["id"])
        _check_number(e.get("gain_db"), "the parts library has a gain that is not a number")
        _check_look(e, "the parts library")
    return lst


def _check_look(b: dict, where: str) -> None:
    """A box's own colour and an SMA part's kind (relay 006), checked in plain words."""
    if "colour" in b and not _is_hex(b["colour"]):
        raise ValueError(f"{where} has a colour that is not #rrggbb")
    s = b.get("sma")
    if s is None:
        return
    if (not isinstance(s, dict) or set(s) - {"shape", "gender", "turn"}
            or ("shape" in s and s["shape"] not in ADAPTER_SHAPES)
            or ("gender" in s and s["gender"] not in GENDERS)
            or ("turn" in s and (isinstance(s["turn"], bool) or s["turn"] not in (0, 1, 2, 3)))):
        raise ValueError(f"{where} has an SMA part of unknown shape, gender or turn")


def _is_hex(v) -> bool:
    return isinstance(v, str) and len(v) == 7 and v[0] == "#" and all(c in "0123456789abcdefABCDEF" for c in v[1:])


def _check_number(v, why: str):
    if v in (None, ""):
        return
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError(why)


def chain_names(rig_name: str, rig: dict) -> list[str]:
    """The chain.json name for each path: the rig's own name when it has one path,
    else 'rig (radio)', numbered if two paths reach the same radio name."""
    ps = paths(rig)
    if len(ps) == 1:
        return [rig_name]
    names, seen = [], {}
    for path in ps:
        r = radio_of(path_parts(rig, path))
        label = (r or {}).get("name") or f"path {len(names) + 1}"
        seen[label] = seen.get(label, 0) + 1
        names.append(f"{rig_name} ({label})" if seen[label] == 1 else f"{rig_name} ({label} {seen[label]})")
    return names


def rig_chains(rig_name: str, rig: dict, labels: dict | None = None) -> dict[str, list[dict]]:
    """{chain name: parts} for every path of the rig."""
    ps = paths(rig)
    return {name: path_parts(rig, p, labels) for name, p in zip(chain_names(rig_name, rig), ps)}


def rig_texts(rig_name: str, rig: dict, labels: dict | None = None) -> dict[str, str]:
    return {n: engine_chain.chain_text(p) for n, p in rig_chains(rig_name, rig, labels).items()}


# ---------------------------------------------------------------- old chains as drawings
def label_room(cables: list | None) -> int:
    """Rough width a wire's cable label needs, so automatic layouts leave space for it."""
    if not cables:
        return 0
    text = "  +  ".join(str(c.get("name") or c.get("type") or "") + (f" · {c['length']}" if c.get("length") else "")
                        for c in cables)
    return min(int(len(text) * 6.2) + 18, 260) + 40


def rig_from_parts(parts: list[dict]) -> dict:
    """A straight chain as a straight row of parts, wired in order. A cable between
    two parts goes onto the wire joining them (several cables in a row share one
    wire, in order); a cable with no part after it stays a part, so nothing is lost."""
    rig = new_rig()
    prev, pending, k, x = None, [], 0, 0
    for i, p in enumerate(parts):
        later_part = any(q.get("type") not in CABLE_TYPES for q in parts[i + 1:])
        if p.get("type") in CABLE_TYPES and prev is not None and later_part:
            pending.append(copy.deepcopy(p))
            continue
        k += 1
        if prev is not None:
            x += PART_W + max(ROW_STEP - PART_W, label_room(pending))
            x = int(round(x / GRID) * GRID)
        node = {"id": f"n{k}", "part": copy.deepcopy(p), "x": x, "y": 0}
        rig["nodes"].append(node)
        if prev is not None:
            w = {"from": prev, "port": 0, "to": node["id"]}
            if pending:
                w["cables"] = pending
            rig["wires"].append(w)
        prev, pending = node["id"], []
    return rig


# ---------------------------------------------------------------- storage
def layout_path(root: Path) -> Path:
    return Path(root) / LAYOUT_FILE


def empty_layout() -> dict:
    return {"schema": 1, "rigs": {}}


def check_layout(data) -> dict:
    """A layout document, checked. Raises ValueError in plain words."""
    if not isinstance(data, dict) or not isinstance(data.get("rigs", {}), dict):
        raise ValueError("the layout must have a 'rigs' object")
    out = empty_layout()
    for name, r in (data.get("rigs") or {}).items():
        if not isinstance(r, dict) or not isinstance(r.get("nodes"), list) or not isinstance(r.get("wires"), list):
            raise ValueError(f"rig {name!r} needs 'nodes' and 'wires' lists")
        ids = set()
        for n in r["nodes"]:
            if not isinstance(n, dict) or "id" not in n or not isinstance(n.get("part"), dict):
                raise ValueError(f"rig {name!r} has a part without an id")
            if n["part"].get("type") not in PART_TYPES:
                raise ValueError(f"rig {name!r} has a part of unknown type {n['part'].get('type')!r}")
            ids.add(n["id"])
        for w in r["wires"]:
            if not isinstance(w, dict) or w.get("from") not in ids or w.get("to") not in ids:
                raise ValueError(f"rig {name!r} has a wire to a part that is not there")
            cables = w.get("cables", [])
            if not isinstance(cables, list) or any(not isinstance(c, dict) or c.get("type") not in CABLE_TYPES
                                                   for c in cables):
                raise ValueError(f"rig {name!r} has a wire with a cable of unknown type")
        encs = r.get("enclosures") or []
        if not isinstance(encs, list):
            raise ValueError(f"rig {name!r} has enclosures that are not a list")
        for e in encs:
            if (not isinstance(e, dict) or not isinstance(e.get("id"), str)
                    or not all(isinstance(e.get(k), (int, float)) for k in ("x", "y", "w", "h"))):
                raise ValueError(f"rig {name!r} has an enclosure without an id, place or size")
            if not (ENC_MIN_W <= e["w"] <= ENC_MAX and ENC_MIN_H <= e["h"] <= ENC_MAX):
                raise ValueError(f"rig {name!r} has an enclosure of impossible size")
        pows = r.get("power") or []
        if not isinstance(pows, list):
            raise ValueError(f"rig {name!r} has power sources that are not a list")
        for p in pows:
            if (not isinstance(p, dict) or not isinstance(p.get("id"), str) or p.get("kind") not in POWER_KINDS
                    or not all(isinstance(p.get(k), (int, float)) for k in ("x", "y"))):
                raise ValueError(f"rig {name!r} has a power source without an id, a known kind or a place")
        pids = {p["id"] for p in pows}
        pw = r.get("power_wires") or []
        if not isinstance(pw, list):
            raise ValueError(f"rig {name!r} has power wires that are not a list")
        fed = set()
        for w in pw:
            if not isinstance(w, dict) or w.get("from") not in pids or w.get("to") not in ids:
                raise ValueError(f"rig {name!r} has a power wire to a part or source that is not there")
            if w["to"] in fed:
                raise ValueError(f"rig {name!r} has a part with two power feeds")
            fed.add(w["to"])
        for n in r["nodes"]:
            _check_number(n.get("gain_db"), f"rig {name!r} has a gain or loss that is not a number")
            for k in ("needs_power", "locked"):
                if k in n and not isinstance(n[k], bool):
                    raise ValueError(f"rig {name!r} has a part whose {k.replace('_', ' ')} is not yes or no")
            _check_look(n, f"rig {name!r}")
        for b in list(encs) + list(pows):
            if "colour" in b and not _is_hex(b["colour"]):
                raise ValueError(f"rig {name!r} has a colour that is not #rrggbb")
        for w in r["wires"]:
            for c in w.get("cables") or []:
                _check_number(c.get("gain_db"), f"rig {name!r} has a cable whose loss is not a number")
            if "mated" in w and (not isinstance(w["mated"], bool) or (w["mated"] and w.get("cables"))):
                raise ValueError(f"rig {name!r} has a direct mating that is not yes or no, or carries a cable")
            if "in" in w and (isinstance(w["in"], bool) or not isinstance(w["in"], int) or w["in"] < 0):
                raise ValueError(f"rig {name!r} has a wire into an input that does not exist")
            pts = w.get("points")
            if pts is not None and (not isinstance(pts, list) or any(
                    not isinstance(q, list) or len(q) != 2
                    or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in q) for q in pts)):
                raise ValueError(f"rig {name!r} has a wire with bend points that are not places on the grid")
            if "square" in w and not isinstance(w["square"], bool):
                raise ValueError(f"rig {name!r} has a wire whose right-angle choice is not yes or no")
        into: dict = {}
        for w in r["wires"]:
            key = (w["to"], w.get("in", 0))
            if key in into:
                raise ValueError(f"rig {name!r} has two wires into the same input")
            into[key] = True
        boxes = list(ids) + [e["id"] for e in encs] + list(pids)
        if len(set(boxes)) != len(boxes):
            raise ValueError(f"rig {name!r} has two boxes with the same id")
        for b in list(r["nodes"]) + list(encs) + list(pows):
            if "z" in b and (isinstance(b["z"], bool) or not isinstance(b["z"], (int, float))):
                raise ValueError(f"rig {name!r} has a stacking order that is not a number")
        grps = r.get("groups") or []
        if not isinstance(grps, list):
            raise ValueError(f"rig {name!r} has groups that are not a list")
        seen = set()
        for g in grps:
            if (not isinstance(g, dict) or not isinstance(g.get("id"), str)
                    or not isinstance(g.get("members"), list) or len(g["members"]) < 2
                    or any(m not in boxes for m in g["members"])):
                raise ValueError(f"rig {name!r} has a group of parts that are not there")
            if seen & set(g["members"]):
                raise ValueError(f"rig {name!r} has a part in two groups")
            seen |= set(g["members"])
        for n in list(r["nodes"]) + list(pows):
            if "text_pos" in n and n["text_pos"] not in TEXT_POS:
                raise ValueError(f"rig {name!r} has text in a position that does not exist")
        for n in r["nodes"]:
            if "shape" in n and n["shape"] not in SHAPES:
                raise ValueError(f"rig {name!r} has a part of unknown shape")
            if "antenna" in n and (not isinstance(n["antenna"], dict)
                                   or n["antenna"].get("type", "custom") not in ANTENNA_TYPES):
                raise ValueError(f"rig {name!r} has an antenna of unknown type")
        for b in list(pows):
            if "shape" in b and b["shape"] not in SHAPES:
                raise ValueError(f"rig {name!r} has a power source of unknown shape")
        out["rigs"][str(name)] = {"nodes": r["nodes"], "wires": r["wires"],
                                  "chains": dict(r.get("chains") or {})}
        for key, val in (("enclosures", encs), ("groups", grps), ("power", pows), ("power_wires", pw)):
            if val:
                out["rigs"][str(name)][key] = val
        if "archived" in r:
            if not isinstance(r["archived"], bool):
                raise ValueError(f"rig {name!r} is archived neither yes nor no")
            if r["archived"]:
                out["rigs"][str(name)]["archived"] = True
        hist = r.get("history") or []
        if not isinstance(hist, list) or any(not isinstance(h, dict) or not isinstance(h.get("rig"), dict)
                                             or not isinstance(h["rig"].get("nodes"), list) for h in hist):
            raise ValueError(f"rig {name!r} has a history that cannot be read")
        if hist:
            out["rigs"][str(name)]["history"] = hist
    if data.get("library"):
        out["library"] = check_library(data["library"])
    return out


def rigs_for_display(chain_doc: dict, layout: dict, labels: dict | None = None) -> dict:
    """What the Chain screen shows, from chain.json and the layout together.

    A layout rig is drawn as saved only when every chain it wrote still has the text
    it wrote. If the old app changed one since, that rig's drawing is set aside and
    each of its chains appears as a straight row, exactly as chain.json now holds it:
    chain.json is always the truth. Every chain not claimed by a drawn rig is shown
    as a straight row under its own name. Returns {rig name: {"rig", "chains",
    "stale_drawing"}}; "chains" maps each path's chain name to its chain.json entry."""
    chains = chain_doc.get("chains") or {}
    out, claimed = {}, set()
    for name, r in (layout.get("rigs") or {}).items():
        wrote = r.get("chains") or {}
        rig = {"nodes": r["nodes"], "wires": r["wires"]}
        for key in DRAWING_ONLY:
            if r.get(key):
                rig[key] = r[key]
        now = rig_texts(name, rig, labels)
        if not wrote and name not in chains and not any(n in chains for n in now):
            # a draft (relay 014): named and kept as a drawing before it has written any chain; its chains
            # reach chain.json on the first Save that has a path
            out[name] = {"rig": rig, "chains": {}, "stale_drawing": False, "archived": bool(r.get("archived")),
                         "history": list(r.get("history") or []), "draft": True}
            continue
        ok = (bool(wrote) and set(wrote) == set(now)
              and all(n in chains and engine_chain.chain_text(chains[n]["parts"]) == wrote[n] == now[n]
                      for n in wrote))
        if ok:
            out[name] = {"rig": rig, "chains": {n: chains[n] for n in now}, "stale_drawing": False,
                         "archived": bool(r.get("archived")), "history": list(r.get("history") or [])}
            claimed |= set(now)
    for name, c in chains.items():
        if name in claimed:
            continue
        stale = any(name in (r.get("chains") or {}) for r in (layout.get("rigs") or {}).values())
        out[name] = {"rig": rig_from_parts(c["parts"]), "chains": {name: c}, "stale_drawing": stale,
                     "archived": False, "history": []}
    return out


def apply_rig(chain_doc: dict, layout: dict, rig_name: str, rig: dict, *, static: bool | None = None,
              renamed_from: str | None = None, labels: dict | None = None) -> tuple[dict, dict]:
    """New (chain doc, layout) with one rig written in.

    Nothing is ever dropped from chain.json except under a rename: a chain this rig
    wrote before and no longer writes (a branch taken out, a radio renamed) stays as
    its own straight chain. A rename moves the rig's chains to the new names, and
    'current' follows them."""
    doc = copy.deepcopy(chain_doc)
    lay = copy.deepcopy(layout)
    doc.setdefault("chains", {})
    new = rig_chains(rig_name, rig, labels)
    old_rig_name = renamed_from or rig_name
    old_written = dict((lay.get("rigs") or {}).get(old_rig_name, {}).get("chains") or {})
    if not old_written and old_rig_name in doc["chains"]:
        old_written = {old_rig_name: None}          # an old straight chain, drawn here for the first time
    clash = [n for n in new if n in doc["chains"] and n not in old_written]
    if clash:
        raise ValueError(f"a chain called {clash[0]!r} already exists; pick another rig name")
    # read before a rename removes the old entry: its history and drawing must move with the rig
    # (relay 012: every version a run has named is kept, renamed or not)
    prev = copy.deepcopy((lay.get("rigs") or {}).get(old_rig_name))
    if renamed_from:
        old_list = list(old_written)
        for i, n in enumerate(new):
            if i < len(old_list) and old_list[i] in doc["chains"] and old_list[i] != n:
                was = doc["chains"].pop(old_list[i])
                doc["chains"][n] = was
                if doc.get("current") == old_list[i]:
                    doc["current"] = n
        lay.get("rigs", {}).pop(renamed_from, None)
    for n, parts in new.items():
        was = doc["chains"].get(n) or {}
        doc["chains"][n] = {"static": bool(was.get("static")) if static is None else bool(static),
                            "updated": was.get("updated"), "last_used": was.get("last_used"),
                            "parts": parts}
    history = list((prev or {}).get("history") or [])
    if prev is not None:                       # relay 007: every Save keeps the drawing it replaces
        history.append(version_of(prev))
    lay.setdefault("rigs", {})[rig_name] = {
        "nodes": copy.deepcopy(rig["nodes"]), "wires": copy.deepcopy(rig["wires"]),
        "chains": {n: engine_chain.chain_text(p) for n, p in new.items()}}
    for key in DRAWING_ONLY:
        if rig.get(key):
            lay["rigs"][rig_name][key] = copy.deepcopy(rig[key])
    if history:
        lay["rigs"][rig_name]["history"] = history
    return doc, lay


def add_draft(layout: dict, rig_name: str, rig: dict, chain_doc: dict | None = None) -> dict:
    """A new layout with a new rig kept as a draft (relay 014): its drawing, under its name, with no chains
    written yet, so the name exists in the picker from the start. Refused, in words, when the name is taken
    by a rig or a chain."""
    name = (rig_name or "").strip()
    if not name:
        raise ValueError("give the rig a name")
    if name in (layout.get("rigs") or {}) or name in ((chain_doc or {}).get("chains") or {}):
        raise ValueError(f"there is already a rig or chain called “{name}”")
    lay = copy.deepcopy(layout)
    entry = {"nodes": copy.deepcopy(rig.get("nodes") or []), "wires": copy.deepcopy(rig.get("wires") or []),
             "chains": {}}
    for key in DRAWING_ONLY:
        if rig.get(key):
            entry[key] = copy.deepcopy(rig[key])
    lay.setdefault("rigs", {})[name] = entry
    return lay


def version_of(saved: dict, when: str | None = None) -> dict:
    """One entry of a rig's history: the whole drawing as it was saved, with the texts it wrote.
    History is drawing only: no recorded text is ever made from it (only the current version)."""
    import datetime as _dt
    rig = {"nodes": copy.deepcopy(saved["nodes"]), "wires": copy.deepcopy(saved["wires"])}
    for key in DRAWING_ONLY:
        if saved.get(key):
            rig[key] = copy.deepcopy(saved[key])
    return {"saved": when or _dt.datetime.now().isoformat(timespec="seconds"), "rig": rig,
            "chains": dict(saved.get("chains") or {})}


def set_archived(chain_doc: dict, layout: dict, rig_name: str, rig: dict, on: bool,
                 labels: dict | None = None) -> dict:
    """A new layout with the rig archived (or restored). chain.json is not touched at all (rule 4):
    its chains stay exactly as they are; only the drawing's archived flag changes. A chain never
    drawn gets its straight-row drawing written, so the flag has somewhere to live."""
    lay = copy.deepcopy(layout)
    rigs = lay.setdefault("rigs", {})
    if rig_name not in rigs:
        texts = rig_texts(rig_name, rig, labels)
        chains = chain_doc.get("chains") or {}
        if any(n not in chains or engine_chain.chain_text(chains[n]["parts"]) != t for n, t in texts.items()):
            raise ValueError("save the rig first: only a saved rig can be archived")
        rigs[rig_name] = {"nodes": copy.deepcopy(rig["nodes"]), "wires": copy.deepcopy(rig["wires"]),
                          "chains": texts}
        for key in DRAWING_ONLY:
            if rig.get(key):
                rigs[rig_name][key] = copy.deepcopy(rig[key])
    if on:
        rigs[rig_name]["archived"] = True
    else:
        rigs[rig_name].pop("archived", None)
    return lay


def rig_state(chains: dict) -> dict:
    """A rig's run state from its chains, with today's meanings: static only when
    every chain is; last ran is the newest; ask 'still built as drawn?' when any
    chain would ask."""
    cs = list(chains.values())
    used = [c.get("last_used") for c in cs if c.get("last_used")]
    return {"static": bool(cs) and all(c.get("static") for c in cs),
            "last_used": max(used) if used else None,
            "needs_confirm": any(engine_chain.needs_confirm(c) for c in cs)}


def read_layout(root: Path) -> dict:
    p = layout_path(root)
    if not p.exists():
        return empty_layout()
    return check_layout(json.loads(p.read_text(encoding="utf-8")))


def checks_for_runs(summary: dict) -> list:
    """The Chain screen's open checks (rig.checks) on the rig used for runs, e.g. "LNA has no
    power feed". Warnings only: they never block (relay 005)."""
    if not summary:
        return []
    chain_sum = summary.get("chain") or {}
    labels = labels_from(chain_sum.get("parts"))
    derived = ("text", "tail", "receiver", "source", "needs_confirm")    # chain_screen.DERIVED
    chains = {n: {k: v for k, v in c.items() if k not in derived}
              for n, c in (chain_sum.get("chains") or {}).items()}
    doc = {"schema": 1, "current": chain_sum.get("current"), "chains": chains}
    try:
        layout = check_layout(summary.get("layout") or {})
        display = rigs_for_display(doc, layout, labels)
    except (ValueError, KeyError, TypeError):
        return []
    by = chain_sum.get("runs_record")
    for name, d in display.items():
        if d.get("archived"):
            continue
        if by:
            texts = {engine_chain.chain_text(c["parts"]) for c in d["chains"].values()}
            used = len(d["chains"]) > 1 and texts == set(by.values())
        else:
            used = chain_sum.get("current") in d["chains"]
        if used:
            try:
                return [{"rig": name, **c} for c in checks(d["rig"])]
            except (KeyError, TypeError, ValueError):
                return []
    return []
