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


def radio_key(station: Station, receiver: str | None) -> str | None:
    """The key a radio's path is filed under in a split rig: its serial from
    recorder.serials (radios are found by serial, not position), else its name."""
    if not receiver:
        return None
    serial = ((station.data.get("recorder") or {}).get("serials") or {}).get(receiver)
    return str(serial) if serial else f"receiver {receiver}"


def use(station: Station, name: str, rig: list | None = None) -> dict:
    """Make `name` the current chain and write its text into station.json as the
    default chain_order, so a plain `marsad run` records it too.

    `rig` (the window's split rigs): every chain of one rig, one per radio. Then
    each run records the path ending at the radio it uses: station.json also gets
    chain_by_serial, {radio key: that path's text}. Without `rig` (one path, or the
    web page) chain_by_serial is cleared and everything is as it always was."""
    data = load(station)
    if name not in data["chains"]:
        raise ValueError(f"no chain called {name!r}")
    by_serial = None
    names = [str(n) for n in (rig or []) if n]
    if len(set(names)) > 1:
        by_serial = {}
        for n in names:
            if n not in data["chains"]:
                raise ValueError(f"no chain called {n!r}")
            rx = receiver_of(data["chains"][n]["parts"])
            key = radio_key(station, rx)
            if key is None:
                raise ValueError(f"the path {n!r} names no radio, so no run could pick it")
            if key in by_serial:
                raise ValueError(f"two paths of this rig end at the same radio ({rx})")
            by_serial[key] = chain_text(data["chains"][n]["parts"])
    data["current"] = name
    _write(path_of(station), data)
    # Choosing a chain is a fresh statement that the rig is built like this, so
    # the command line's "used, unconfirmed" stamp is cleared with it.
    defaults = {"chain_order": chain_text(data["chains"][name]["parts"]), "chain_used_at": None}
    if by_serial or "chain_by_serial" in station.defaults:
        defaults["chain_by_serial"] = by_serial
    station.save_defaults(defaults)
    return data


def rig_text_for(station: Station, receiver: str | None) -> str | None:
    """With a split rig current, the text of the path ending at this receiver's radio.

    None when no split rig is current (the run then does what it always did).
    SystemExit, in plain words, when one is current and none of its paths ends at
    this radio: a run must never quietly record another radio's rig."""
    by = station.defaults.get("chain_by_serial")
    if not by:
        return None
    key = radio_key(station, receiver)
    if key in by:
        return by[key]
    raise SystemExit(
        f"\n  The rig used for runs has no path ending at the {receiver or 'chosen'} radio "
        f"({key or 'no receiver named'}).\n  Its paths end at: {', '.join(by)}.\n"
        "  Draw that radio's path and press Use for runs, or pass --chain \"...\" for the rig "
        "as it is.\n  Nothing was recorded.")


def texts_are_static(station: Station, texts) -> bool:
    """True when every text belongs to a chain marked static (a split rig's paths)."""
    try:
        data = load(station)
    except ValueError:
        return False
    want = list(texts)
    return bool(want) and all(any(c.get("static") and chain_text(c["parts"]) == t
                                  for c in data["chains"].values()) for t in want)


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
    data["runs_record"] = station.defaults.get("chain_by_serial") or None
    data["file"] = str(path_of(station))
    return data


# ---------------------------------------------------------------- rig references (relay 012)
#
# The desktop window draws rigs in chain_layout.json beside chain.json; each Save keeps the drawing it
# replaced in the rig's history. A run records, beside its chain text (which stays the authority), a
# reference to the drawing that wrote that text: the rig's name, the path (the chain.json name the rig
# wrote), and a version, a short hash of the drawing itself. The hash needs no counter or clock, so the
# same drawing gives the same version wherever it is kept: current, or in history after later Saves,
# or under a new name after a rename (history moves with the rig). Never part of the fingerprint.

LAYOUT_FILE = "chain_layout.json"
DRAWING_KEYS = ("nodes", "wires", "enclosures", "groups", "power", "power_wires")


def drawing_version(rig: dict) -> str:
    """A drawing's version: 12 hex digits of the sha256 of its drawing keys (nodes, wires and any
    non-empty enclosures, groups, power, power wires), as the history keeps them."""
    import hashlib
    d = {k: rig.get(k) for k in DRAWING_KEYS if k in ("nodes", "wires") or rig.get(k)}
    raw = json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def read_layout(station: Station) -> dict | None:
    """chain_layout.json as saved, or None when there is none or it cannot be read. Reads only."""
    p = station.root / LAYOUT_FILE
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) and isinstance(d.get("rigs"), dict) else None


def rig_reference(station: Station, text: str) -> dict:
    """What a run records about the drawing behind its chain text: {"rig", "path", "version",
    "file"} when a drawn rig's path wrote exactly this text, else {"rig": None, "why": ...}.
    An archived rig is used only when no current one wrote the text."""
    text = (text or "").strip()
    lay = read_layout(station)
    if lay is None:
        return {"rig": None, "why": "no drawn rig: chain_layout.json is absent or unreadable"}
    found = []
    for name, r in lay["rigs"].items():
        if not isinstance(r, dict):
            continue
        for path, t in (r.get("chains") or {}).items():
            if (t or "").strip() == text and text:
                found.append((bool(r.get("archived")), str(name), str(path), r))
    if not found:
        return {"rig": None, "why": "the chain text matches no drawn rig (an old chain, or one typed with --chain)"}
    found.sort(key=lambda f: (f[0], f[1].lower(), f[2]))
    archived, name, path, r = found[0]
    out = {"rig": name, "path": path, "version": drawing_version(r), "file": LAYOUT_FILE}
    if archived:
        out["archived"] = True
    return out


def find_drawing(layout: dict | None, version: str, rig: str | None = None) -> dict | None:
    """The drawing with this version, looked for in the named rig first, then in every rig: its
    current drawing and every kept version. {"rig", "where": "current" | "history", "saved", "drawing",
    "chains"} or None when no kept drawing has it."""
    rigs = (layout or {}).get("rigs") or {}
    order = ([rig] if rig in rigs else []) + [n for n in rigs if n != rig]
    for name in order:
        r = rigs.get(name)
        if not isinstance(r, dict):
            continue
        if isinstance(r.get("nodes"), list) and drawing_version(r) == version:
            return {"rig": name, "where": "current", "saved": None, "drawing": copy.deepcopy(
                {k: r[k] for k in DRAWING_KEYS if k in r}), "chains": dict(r.get("chains") or {})}
        for h in r.get("history") or []:
            d = (h or {}).get("rig")
            if isinstance(d, dict) and drawing_version(d) == version:
                return {"rig": name, "where": "history", "saved": h.get("saved"), "drawing": copy.deepcopy(d),
                        "chains": dict(h.get("chains") or {})}
    return None


def rig_reference_rows(sidecar: dict | None) -> list[str]:
    """A Markdown row for report.md and scan_build.md; none for runs from before relay 012."""
    ref = (sidecar or {}).get("rig_reference")
    if not isinstance(ref, dict):
        return []
    if not ref.get("rig"):
        return [f"| Rig drawing | none recorded: {ref.get('why') or 'no drawn rig'} |"]
    arch = " (archived)" if ref.get("archived") else ""
    return [f"| Rig drawing | {ref['rig']}{arch}, path {ref.get('path')}, version {ref.get('version')} "
            f"(in {ref.get('file') or LAYOUT_FILE}; the chain text above is the authority) |"]
