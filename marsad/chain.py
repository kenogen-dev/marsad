"""The Chain tab's back end: the station's signal chains as drawn parts.

A chain is a line of parts from the source (antenna, probe or load) to the PC.
The web app draws it as a rail of boxes you drag parts onto; this module stores
those drawings in chain.json beside station.json, turns a drawing into the
`--chain` text every run records, and remembers when each chain was last changed
and last used so the Probe tab can ask "still built like this?" before a run,
unless the chain is marked static.

Stdlib only.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
from pathlib import Path

from .config import Station

FILE = "chain.json"

# What the palette offers. `fields` are the extra boxes the editor shows; every
# part also has a name (what the chain text says) and a detail (in brackets).
PART_TYPES = {
    "antenna": {"group": "Source", "label": "Antenna", "source": True, "fields": []},
    "probe": {"group": "Source", "label": "Near-field probe", "source": True, "fields": [],
              "note": "the Probe tab replaces this part with the probe you pick there"},
    "load": {"group": "Source", "label": "50 ohm load", "source": True, "fields": []},
    "adapter": {"group": "RF", "label": "SMA adapter", "fields": []},
    "dcblock": {"group": "RF", "label": "SMA DC block", "fields": []},
    "filter": {"group": "RF", "label": "Flamingo+ FM notch", "fields": []},
    "lna": {"group": "RF", "label": "LaNA", "fields": []},
    "attenuator": {"group": "RF", "label": "SMA attenuator", "fields": ["db"]},
    "bulkhead_sma": {"group": "RF", "label": "SMA bulkhead", "fields": []},
    "coax": {"group": "Cables", "label": "Coax", "fields": ["length"]},
    "jumper": {"group": "Cables", "label": "SMA jumper", "fields": ["length"]},
    "sdr": {"group": "Receiver", "label": "SDR", "fields": ["rx"]},
    "usb": {"group": "USB", "label": "USB cable", "fields": ["length"]},
    "bulkhead_usb": {"group": "USB", "label": "USB bulkhead", "fields": []},
    "isolator": {"group": "USB", "label": "USB isolator", "fields": []},
    "pc": {"group": "USB", "label": "PC", "fields": []},
    "custom": {"group": "Other", "label": "Custom part", "fields": []},
}


def _p(type_: str, name: str, detail: str = "", **fields) -> dict:
    return {"type": type_, "name": name, "detail": detail, **fields}


# Seeds, written the first time the tab is opened. They reproduce the chain text the
# station has been recording, word for word, so the first drawing is the real rig.
def seed_chains() -> dict:
    xtr_tail = [
        _p("filter", "Flamingo+ FM notch"),
        _p("lna", "LaNA", "at the window, powered by the XTR bias-tee through the coax"),
        _p("coax", "3 ft RG316 coax", length="3 ft"),
        _p("bulkhead_sma", "SMA bulkhead"),
        _p("jumper", "6 in SMA jumper", "S-bend, no ferrite", length="6 in"),
        _p("sdr", "XTR", "in aluminium enclosure, plugged direct into USB bulkhead, no internal USB cable", rx="xtr"),
        _p("bulkhead_usb", "USB bulkhead"),
        _p("usb", "1 ft USB3 M:M", length="1 ft"),
        _p("usb", "16 ft USB3", length="16 ft"),
        _p("isolator", "USB isolator"),
        _p("pc", "PC"),
    ]
    antenna = [_p("antenna", "antenna",
                  "90 deg L dipole, 60 mm each whip, suction-mounted on the inside face of the window glass"),
               _p("filter", "Flamingo+ FM notch"),
               _p("lna", "LaNA", "at the window, on the far end of the coax, powered by the XTR bias-tee through the coax"),
               *copy.deepcopy(xtr_tail[2:])]
    probe_xtr = [_p("probe", "near-field probe", "held where the Probe tab says"),
                 _p("adapter", "SMA adapter"), _p("dcblock", "SMA DC block"),
                 _p("coax", "8 ft coax", length="8 ft"), *copy.deepcopy(xtr_tail)]
    probe_v5 = [_p("probe", "near-field probe", "held where the Probe tab says"),
                _p("adapter", "SMA adapter"), _p("dcblock", "SMA DC block"),
                _p("coax", "8 ft coax", length="8 ft"),
                _p("bulkhead_sma", "cage SMA bulkhead"),
                _p("sdr", "v5", "in copper cage, no jumper, no LNA: the v5 has no bias-tee to power the LaNA", rx="v5"),
                _p("usb", "6 in USB extension", "white, no ferrites", length="6 in"),
                _p("bulkhead_usb", "USB bulkhead"),
                _p("usb", "USB cable"),
                _p("pc", "PC")]
    now = _now()
    mk = lambda parts: {"static": False, "updated": now, "last_used": None, "parts": parts}
    return {"schema": 1, "current": "antenna-xtr",
            "chains": {"antenna-xtr": mk(antenna), "probe-xtr": mk(probe_xtr),
                       "probe-v5": mk(probe_v5)}}


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------- text
def part_text(p: dict) -> str:
    """One part as the chain text says it: 'name (detail)'."""
    name = str(p.get("name") or PART_TYPES.get(p.get("type", ""), {}).get("label") or "part").strip()
    if p.get("type") == "attenuator" and p.get("db") not in (None, "", 0, "0"):
        name = f"{p['db']} dB SMA attenuator"
    detail = str(p.get("detail") or "").strip()
    return f"{name} ({detail})" if detail else name


def chain_text(parts: list) -> str:
    return " > ".join(part_text(p) for p in parts)


def tail_text(parts: list) -> str:
    """The chain after its source part(s): what the Probe tab puts after the probe."""
    i = 0
    while i < len(parts) and PART_TYPES.get(parts[i].get("type", ""), {}).get("source"):
        i += 1
    return chain_text(parts[i:])


def receiver_of(parts: list) -> str | None:
    for p in parts:
        if p.get("type") == "sdr" and p.get("rx"):
            return str(p["rx"])
    return None


# ---------------------------------------------------------------- storage
def validate(data) -> dict:
    """A chain.json document the page sent, checked and normalised. Raises ValueError."""
    if not isinstance(data, dict) or not isinstance(data.get("chains"), dict):
        raise ValueError("chain data must have a 'chains' object")
    out = {"schema": 1, "current": None, "chains": {}}
    for name, c in data["chains"].items():
        name = str(name).strip()
        if not name:
            raise ValueError("a chain needs a name")
        if not isinstance(c, dict) or not isinstance(c.get("parts"), list):
            raise ValueError(f"chain {name!r} needs a 'parts' list")
        parts = []
        for p in c["parts"]:
            if not isinstance(p, dict) or p.get("type") not in PART_TYPES:
                raise ValueError(f"chain {name!r} has a part of unknown type {p.get('type') if isinstance(p, dict) else p!r}")
            q = {"type": p["type"], "name": str(p.get("name") or "").strip(),
                 "detail": str(p.get("detail") or "").strip()}
            for f in ("length", "db", "rx"):
                if p.get(f) not in (None, ""):
                    q[f] = p[f]
            parts.append(q)
        out["chains"][name] = {"static": bool(c.get("static")),
                               "updated": c.get("updated") or _now(),
                               "last_used": c.get("last_used") or None,
                               "parts": parts}
    cur = data.get("current")
    out["current"] = cur if cur in out["chains"] else (next(iter(out["chains"]), None))
    return out


def path_of(station: Station) -> Path:
    return station.root / FILE


def load(station: Station) -> dict:
    """chain.json, seeded with the station's known chains the first time."""
    p = path_of(station)
    if p.exists():
        try:
            return validate(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            raise ValueError(f"{p.name} is unreadable: {exc}")
    data = seed_chains()
    _write(p, data)
    return data


def _write(p: Path, data: dict) -> None:
    p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def save(station: Station, data) -> dict:
    """Store what the page sent. A chain whose parts changed gets a fresh
    'updated' stamp; untouched chains keep theirs, so staleness stays honest."""
    new = validate(data)
    p = path_of(station)
    old = {}
    if p.exists():
        try:
            old = validate(json.loads(p.read_text(encoding="utf-8")))["chains"]
        except (OSError, ValueError):
            old = {}
    for name, c in new["chains"].items():
        was = old.get(name)
        if was is None or was["parts"] != c["parts"]:
            # a changed chain has not yet been used in its new form
            c["updated"] = _now()
            c["last_used"] = None
        else:
            c["updated"] = was["updated"]
            c["last_used"] = was["last_used"]
    _write(p, new)
    return new


def use(station: Station, name: str) -> dict:
    """Make `name` the current chain and write its text into station.json as the
    default chain_order, so a plain `marsad run` records it too."""
    data = load(station)
    if name not in data["chains"]:
        raise ValueError(f"no chain called {name!r}")
    data["current"] = name
    _write(path_of(station), data)
    station.save_defaults({"chain_order": chain_text(data["chains"][name]["parts"])})
    return data


def mark_used(station: Station, name: str | None = None) -> None:
    """A run just started on this chain: stamp it. Never fails a run."""
    try:
        data = load(station)
        name = name or data["current"]
        if name in data["chains"]:
            data["chains"][name]["last_used"] = _now()
            _write(path_of(station), data)
    except (OSError, ValueError):
        pass


def needs_confirm(chain: dict) -> bool:
    """True when a run should ask 'still built like this?': the chain is not
    marked static and has been used as drawn (save() clears last_used on a change,
    so a stamp means 'untouched since that run')."""
    return not chain.get("static") and bool(chain.get("last_used"))


def summary(station: Station) -> dict:
    """What the page needs: the document plus derived text per chain."""
    data = load(station)
    for name, c in data["chains"].items():
        c["text"] = chain_text(c["parts"])
        c["tail"] = tail_text(c["parts"])
        c["receiver"] = receiver_of(c["parts"])
        c["source"] = c["parts"][0]["type"] if c["parts"] else None
        c["needs_confirm"] = needs_confirm(c)
    data["parts"] = PART_TYPES
    data["file"] = str(path_of(station))
    return data
