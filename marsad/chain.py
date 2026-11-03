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
import re
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
    "filter": {"group": "RF", "label": "FM notch", "fields": []},
    "lna": {"group": "RF", "label": "LNA", "fields": []},
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


def part_types(station: Station) -> dict:
    """The palette, with station.json's chain_labels as the standard names."""
    over = station.data.get("chain_labels") or {}
    return {t: {**d, "label": over.get(t, d["label"])} for t, d in PART_TYPES.items()}


def _p(type_: str, name: str, detail: str = "", **fields) -> dict:
    return {"type": type_, "name": name, "detail": detail, **fields}


# Seeds, written the first time the tab is opened: the plainest rig for the station's
# default receiver and a near-field probe chain, for you to redraw as yours.
def seed_chains(rx: str = "r820t") -> dict:
    tail = [_p("coax", "Coax"), _p("sdr", rx, rx=rx), _p("usb", "USB cable"), _p("pc", "PC")]
    antenna = [_p("antenna", "antenna"), *copy.deepcopy(tail)]
    probe = [_p("probe", "near-field probe", "held where the Probe tab says"),
             _p("adapter", "SMA adapter"), _p("dcblock", "SMA DC block"), *copy.deepcopy(tail)]
    now = _now()
    mk = lambda parts: {"static": False, "updated": now, "last_used": None, "parts": parts}
    return {"schema": 1, "current": "antenna",
            "chains": {"antenna": mk(antenna), "probe": mk(probe)}}


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


# A hand-typed custom part that is plainly a known one becomes that part, so the
# recorded text stays searchable: "10dB Attenuator" -> 10 dB SMA attenuator,
# "Notch (input side)" -> FM notch (input side). Typed parts with a deliberate name
# (e.g. "rear SMA bulkhead") are left alone.
_PAD_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*dB\s*(?:SMA\s*)?(?:attenuator|pad)\s*$", re.I)
_CANON = [
    (re.compile(r"^\s*(fm\s*)?notch\s*$", re.I), "filter"),
    (re.compile(r"^\s*(l\w?na|low\s*noise\s*amp\w*)\s*$", re.I), "lna"),
    (re.compile(r"^\s*(sma\s*)?dc\s*block\s*$", re.I), "dcblock"),
    (re.compile(r"^\s*(sma\s*)?adapter\s*$", re.I), "adapter"),
    (re.compile(r"^\s*usb\s*isolator\s*$", re.I), "isolator"),
]


def canonical(p: dict, labels: dict | None = None) -> dict:
    """Normalise one part in place and return it (see above)."""
    labels = labels or PART_TYPES
    name = p.get("name") or ""
    if p["type"] == "attenuator":
        m = _PAD_RE.match(name)
        if m and p.get("db") in (None, ""):
            p["db"] = float(m.group(1)) if "." in m.group(1) else int(m.group(1))
        p["name"] = labels["attenuator"]["label"]
        return p
    if p["type"] != "custom":
        return p
    m = _PAD_RE.match(name)
    if m:
        p.update({"type": "attenuator", "name": labels["attenuator"]["label"],
                  "db": float(m.group(1)) if "." in m.group(1) else int(m.group(1))})
        return p
    for rx_, type_ in _CANON:
        if rx_.search(name):
            p["type"] = type_
            p["name"] = labels[type_]["label"]
            return p
    return p


# ---------------------------------------------------------------- storage
def validate(data, labels: dict | None = None) -> dict:
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
            parts.append(canonical(q, labels))
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
    data = seed_chains(station.default_receiver or "r820t")
    _write(p, data)
    return data


def _write(p: Path, data: dict) -> None:
    p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def save(station: Station, data) -> dict:
    """Store what the page sent. A chain whose parts changed gets a fresh
    'updated' stamp; untouched chains keep theirs, so staleness stays honest."""
    new = validate(data, part_types(station))
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
    # Choosing a chain is a fresh statement that the rig is built like this, so
    # the command line's "used, unconfirmed" stamp is cleared with it.
    station.save_defaults({"chain_order": chain_text(data["chains"][name]["parts"]),
                           "chain_used_at": None})
    return data


def default_is_static(station: Station, text: str) -> bool:
    """True when the Chain tab's current chain renders to `text` and is marked static."""
    try:
        data = load(station)
    except ValueError:
        return False
    cur = data["chains"].get(data.get("current") or "")
    return bool(cur and cur.get("static") and chain_text(cur["parts"]) == text)


def mark_text_used(station: Station, text: str) -> None:
    """A run just recorded `text`: stamp the Chain tab chain that renders to it, if any."""
    try:
        data = load(station)
    except ValueError:
        return
    for name, c in data["chains"].items():
        if chain_text(c["parts"]) == text:
            mark_used(station, name)
            return


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
    data["parts"] = part_types(station)
    data["receivers"] = list(station.receivers)
    data["file"] = str(path_of(station))
    return data
