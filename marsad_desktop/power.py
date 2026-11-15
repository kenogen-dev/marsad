"""Relay 030: bias-tee injectors, DC power supplies and + / GND power leads on the Chain screen. Qt-free.

What reaches recorded text and what does not:
- A **bias-tee injector** is a part in the signal path (its DC+RF side toward the antenna or LNA, its RF side toward the
  radio), so a path through it writes it into the recorded text, as a custom part named by its name and detail, the
  same way as the splitter and the USB hub. Its VCC+ and GND- pins are power pins: drawing only.
- A **DC linear power supply** is a power box (outputs V+ and GND-; voltage, current limit, note): drawing only.
- **Power leads** are power wires with a `pin` at the supply ("+" or "gnd") and a `to_pin` at the part ("vcc" or "gnd").
  They live in the rig's "power_wires" list, which the path walk never reads, so they can never enter a recorded text.
  Older power wires (no pins: a USB-C lead, the radio's bias-tee) keep working as a single feed.

A part that needs power (an LNA) counts as powered when it has a feed of its own (an older power wire, or + and GND
leads straight from a supply), or when its RF output reaches an injector's DC+RF side along the signal path, through
cables and parts that pass DC, with no DC block in between, and that injector has + and GND from a supply.
"""
from __future__ import annotations

import re

INJECTOR = "biastee_injector"
SUPPLY = "dc_supply"
PINS_OUT = {"+": "V+", "gnd": "GND−"}          # a supply's outputs
PINS_IN = {"vcc": "VCC+", "gnd": "GND−"}         # a powered part's inputs (an injector's, or an LNA fed directly)
POLE = {"+": "vcc", "gnd": "gnd"}                # which input each output belongs on
LEAD_COLOUR = {"+": "#D64545", "gnd": "#202020"}  # red and black, as the leads really are (fixed, not theme colours)
DC_PASSES = ("adapter", "bulkhead_sma", "filter")  # parts a DC feed goes through (cables always do)
DC_STOPS_WHY = {"dcblock": "the DC block stops the injector's power reaching {lna}",
                "splitter": "{lna}'s power cannot pass the splitter (a resistive divider collapses the bias voltage)",
                "attenuator": "{lna}'s power has to pass an attenuator, which does not carry DC reliably"}


def _nodes(rig):
    return {n["id"]: n for n in rig.get("nodes") or []}


def _boxes(rig):
    return {b["id"]: b for b in rig.get("power") or []}


def is_lead(w: dict) -> bool:
    return "pin" in w or "to_pin" in w


def leads_into(rig: dict, nid: str) -> list[dict]:
    return [w for w in rig.get("power_wires") or [] if w["to"] == nid and is_lead(w)]


def _name(x: dict | None, fallback: str) -> str:
    if not x:
        return fallback
    p = x.get("part") or x
    return str(p.get("name") or "").strip() or fallback


def parse_volts(text) -> tuple[float, float] | None:
    """"5", "5 V", "4.5-5.5", "3.3 to 12 V" -> (low, high); None when nothing is stated or it cannot be read."""
    if text is None:
        return None
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return float(text), float(text)
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", str(text))]
    if not nums:
        return None
    return (min(nums[:2]), max(nums[:2])) if len(nums) >= 2 else (nums[0], nums[0])


def supply_volts(box: dict) -> float | None:
    v = box.get("volts")
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- drawing leads
def connect_lead(rig: dict, src: str, pin: str, dst: str, to_pin: str) -> str | None:
    """One lead from a supply's output to a part's power pin. Returns why not, or None when drawn. A reversed lead is
    drawn (it is what may really be on the bench) and flagged as a problem by the checks."""
    boxes, nodes = _boxes(rig), _nodes(rig)
    if src not in boxes or boxes[src].get("kind") != SUPPLY:
        return "a power lead starts at a DC power supply"
    if dst not in nodes:
        return "a power lead ends at a part"
    if pin not in PINS_OUT or to_pin not in PINS_IN:
        return "a power lead joins V+ or GND− to VCC+ or GND−"
    if any(w.get("to_pin") == to_pin for w in leads_into(rig, dst)):
        return f"{_name(nodes[dst], 'that part')}'s {PINS_IN[to_pin]} already has a lead"
    if any(w["to"] == dst and not is_lead(w) for w in rig.get("power_wires") or []):
        return f"{_name(nodes[dst], 'that part')} already has a power feed of another kind"
    rig.setdefault("power_wires", []).append({"from": src, "to": dst, "pin": pin, "to_pin": to_pin})
    return None


def connect_pair(rig: dict, src: str, dst: str) -> str | None:
    """"Connect + and GND": both leads at once, V+ to VCC+ and GND− to GND−."""
    taken = {w.get("to_pin") for w in leads_into(rig, dst)}
    done = []
    for pin in ("+", "gnd"):
        if POLE[pin] in taken:
            continue
        why = connect_lead(rig, src, pin, dst, POLE[pin])
        if why:
            for w in done:
                rig["power_wires"].remove(w)
            return why
        done.append(rig["power_wires"][-1])
    return None if done else f"{_name(_nodes(rig).get(dst), 'that part')} already has both leads"


def can_take_leads(node: dict, needs_power) -> bool:
    return node["part"]["type"] == INJECTOR or bool(needs_power(node))


# ---------------------------------------------------------------- reading the power
def _has_pair(rig: dict, nid: str) -> tuple[bool, bool, dict | None]:
    """(+ joined, GND joined, the supply) for a part's leads, each counted only when the right way round."""
    plus = gnd = False
    sup = None
    boxes = _boxes(rig)
    for w in leads_into(rig, nid):
        if w.get("pin") == "+" and w.get("to_pin") == "vcc":
            plus, sup = True, boxes.get(w["from"]) or sup
        elif w.get("pin") == "gnd" and w.get("to_pin") == "gnd":
            gnd, sup = True, boxes.get(w["from"]) or sup
    return plus, gnd, sup


def _downstream_injector(rig: dict, nid: str) -> tuple[dict | None, list[str], str | None, str | None]:
    """From a part's output, along the signal path toward the radio: the first injector reached on its DC+RF side,
    the ids walked, and what stops DC on the way (its id and type), if anything."""
    nodes = _nodes(rig)
    outs: dict[str, list] = {}
    for w in rig.get("wires") or []:
        outs.setdefault(w["from"], []).append(w)
    seen, trail, stop = {nid}, [nid], None
    frontier = [w["to"] for w in sorted(outs.get(nid, []), key=lambda w: w["port"])]
    while frontier:
        cur = frontier.pop(0)
        if cur in seen or cur not in nodes:
            continue
        seen.add(cur)
        trail.append(cur)
        t = nodes[cur]["part"]["type"]
        if t == INJECTOR:                         # a stop on the way counts only when an injector lies beyond it
            return (nodes[cur], trail, None, None) if stop is None else (None, trail, stop, nodes[stop]["part"]["type"])
        if t in DC_STOPS_WHY:
            stop = stop or cur
        elif t not in DC_PASSES:
            continue
        frontier += [w["to"] for w in sorted(outs.get(cur, []), key=lambda w: w["port"])]
    return None, trail, None, None


def status(rig: dict, needs_power) -> dict:
    """{node id: {"powered", "how" (words), "path" (ids, the power's way), "problem", "volts"}} for every part that
    needs power. `needs_power` is rig.needs_power (passed in, so this module has no import cycle)."""
    nodes, boxes = _nodes(rig), _boxes(rig)
    out = {}
    for n in nodes.values():
        if not needs_power(n):
            continue
        me = _name(n, "the part")
        old = next((w for w in rig.get("power_wires") or [] if w["to"] == n["id"] and not is_lead(w)), None)
        if old is not None:
            b = boxes.get(old["from"]) or {}
            out[n["id"]] = {"powered": True, "how": f"its own feed: {_name(b, 'a power source')}",
                            "path": [old["from"], n["id"]], "problem": None, "volts": supply_volts(b)}
            continue
        plus, gnd, sup = _has_pair(rig, n["id"])
        if plus and gnd:
            out[n["id"]] = {"powered": True, "how": f"+ and GND leads from {_name(sup, 'the supply')}",
                            "path": [sup["id"], n["id"]], "problem": None, "volts": supply_volts(sup)}
            continue
        inj, trail, stop, stop_t = _downstream_injector(rig, n["id"])
        if stop is not None:
            out[n["id"]] = {"powered": False, "how": "", "path": trail, "volts": None,
                            "problem": DC_STOPS_WHY[stop_t].format(lna=me), "stop": stop, "stop_type": stop_t}
            continue
        if inj is not None:
            iplus, ignd, isup = _has_pair(rig, inj["id"])
            if iplus and ignd:
                out[n["id"]] = {"powered": True, "path": [isup["id"]] + trail[::-1],
                                "how": f"through {_name(inj, 'the bias-tee injector')} (DC up the coax), "
                                       f"fed by {_name(isup, 'the supply')}", "problem": None,
                                "volts": supply_volts(isup), "injector": inj["id"]}
            else:
                miss = " and ".join(x for x, ok in (("+", iplus), ("GND", ignd)) if not ok)
                out[n["id"]] = {"powered": False, "path": trail, "problem": None, "volts": None,
                                "injector": inj["id"],
                                "how": f"{_name(inj, 'the bias-tee injector')} reaches it, but has no {miss} "
                                       "from a supply"}
            continue
        part = ("a + lead but no GND" if plus else "a GND lead but no +" if gnd else "")
        out[n["id"]] = {"powered": False, "path": [n["id"]], "problem": None, "volts": None,
                        "how": "no power feed" + (f" (it has {part})" if part else "")}
    return out


def checks(rig: dict, needs_power) -> list[dict]:
    """The power checks, with their level: reversed polarity and a DC block in the injector's way are problems; a
    missing + or GND, an unpowered part and a voltage outside a part's stated range are warnings."""
    nodes, boxes = _nodes(rig), _boxes(rig)
    out = []
    for w in rig.get("power_wires") or []:
        if not is_lead(w):
            continue
        if POLE.get(w.get("pin")) != w.get("to_pin"):
            n = nodes.get(w["to"])
            out.append({"kind": "power", "level": "problem", "ids": [w["from"], w["to"]],
                        "text": f"Reversed polarity at {_name(n, 'a part')}: {PINS_OUT.get(w.get('pin'), '?')} goes to "
                                f"{PINS_IN.get(w.get('to_pin'), '?')}"})
    powered_by_inj = set()
    st = status(rig, needs_power)
    for nid, s in st.items():
        me = _name(nodes[nid], "a part")
        if s.get("problem"):
            out.append({"kind": "power", "level": "problem", "ids": [nid, s.get("stop")] if s.get("stop") else [nid],
                        "text": s["problem"][0].upper() + s["problem"][1:]})
        elif not s["powered"]:
            out.append({"kind": "power", "level": "warning", "ids": [nid] + ([s["injector"]] if s.get("injector") else []),
                        "text": f"{me} has no power feed" if s["how"] == "no power feed" else
                                f"{me} has no power: {s['how']}"})
        if s.get("injector"):
            powered_by_inj.add(s["injector"])
        need = parse_volts(nodes[nid].get("needs_v"))
        if s["powered"] and need and s.get("volts") is not None and not need[0] - 1e-9 <= s["volts"] <= need[1] + 1e-9:
            want = f"{need[0]:g}" if need[0] == need[1] else f"{need[0]:g}–{need[1]:g}"
            out.append({"kind": "power", "level": "warning", "ids": [nid],
                        "text": f"{me} needs {want} V but its supply is set to {s['volts']:g} V"})
    for n in nodes.values():                      # an injector's own inputs
        if n["part"]["type"] != INJECTOR:
            continue
        plus, gnd, _ = _has_pair(rig, n["id"])
        if (plus or gnd) and not (plus and gnd):
            miss = "GND" if plus else "+"
            out.append({"kind": "power", "level": "warning", "ids": [n["id"]],
                        "text": f"{_name(n, 'The bias-tee injector')} has no {miss} lead"})
        elif not plus and not gnd and not leads_into(rig, n["id"]) and n["id"] not in powered_by_inj:
            out.append({"kind": "power", "level": "note", "ids": [n["id"]],
                        "text": f"{_name(n, 'The bias-tee injector')} has no power in (VCC+ and GND− are free)"})
    return out


def report(rig: dict, path_ids: list[str], needs_power) -> list[str]:
    """The Output panel's power lines for one path: each powered part and the way its power comes."""
    nodes, st = _nodes(rig), status(rig, needs_power)
    lines = []
    for nid in path_ids:
        if nid not in st:
            continue
        s = st[nid]
        me = _name(nodes[nid], "a part")
        if s["powered"]:
            v = f", {s['volts']:g} V" if s.get("volts") is not None else ""
            lines.append(f"{me}: powered, {s['how']}{v}")
        else:
            lines.append(f"{me}: NOT powered, {s.get('problem') or s['how']}")
    return lines
