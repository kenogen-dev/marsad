"""Relay 021: a new station, its radios, and each radio's setup before any scan.

A station made by New Station carries `station_setup.json`. While it does, each radio has a checklist:

    1 add      the radio is in station.json, matched by its serial
    2 measure  its tuning range and blind spots, retune time, warm-up drift and terminated floor
    3 draw     the rig used for runs has a path to it (the Chain screen)
    4 confirm  the owner said the rig is built as drawn (cleared when the drawing changes)
    5 paths    an ADS-B receive proof through the rig, and a first baseline survey with the antenna

and scans on that radio stay refused until all five are done. A station without the file (every station made before
this, the owner's own among them) is not tracked and nothing is refused.

Every measurement is an existing command, launched like a scan (outside every process tree, with its log and exit
code): `rtl_test -t` for the E4000's range and gap, `marsad calibrate-retune --save`, `marsad run` from cold into a
50 ohm load read by `marsad drift`, a short terminated `marsad run` marked terminated in the archive, `marsad
adsb-check`, and a short `marsad run` with the rig's chain. Where no command exists (the range of an R820T/R860 has no
measuring tool here), the step says so and the owner can accept the tuner's typical figures, which stay marked "typical,
not measured". Results are kept, never replaced: each has its date, its step and its run, and the newest counts.

The location is written only to the new station's own `station.local.json`; nothing here reads one back.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import re
from contextlib import redirect_stdout
from pathlib import Path

from .config import DEFAULT_FILE, STATION_FILE, Station
from .rtlpower import TUNER_GAINS, TUNER_LABEL

SETUP_FILE = "station_setup.json"
LOCAL_FILE = "station.local.json"
STEP_ORDER = ["add", "measure", "draw", "confirm", "paths"]
STEP_TITLES = {"add": "Add the radio", "measure": "Measure the radio", "draw": "Draw the rig in Chain",
               "confirm": "Confirm the rig is built as drawn", "paths": "Path checks through the rig"}
# Relay 022: the guide's order. Warm-up comes straight after the 30 s range test, while the radio is still cold (it
# must start cold); retune (90 s of streaming) and the floor (taken warm) follow it.
MEASURES = ["range", "warmup", "retune", "floor"]
PATHS = ["adsb", "baseline"]
LOAD = "50 ohm load"
# Relay 022: what each step's result rests on. When one of these changes after the step was done, the step reads
# "needs re-checking" (and, being not done, keeps scanning locked) until it is run again.
#   add    the radio itself: its tuner (which sets the gain used) and its serial
#   range  the 100 MHz tile the load runs and the baseline record on, which the radio's range and gaps decide
#   rig    the drawn rig's text, which the receive proof and the baseline go through
DEPENDS = {"range": ("add",), "warmup": ("add", "range"), "retune": ("add",), "floor": ("add", "range"),
           "adsb": ("add", "rig"), "baseline": ("add", "range", "rig")}
BASIS_WORDS = {"add": "the radio (its tuner or serial)", "range": "the frequencies the measurement runs on (set by "
               "the radio's range)", "rig": "the rig"}

# What each measurement does, what to connect, how long, and what to expect (the window shows these before a run).
STEPS = {
    "range": {"title": "Tuning range and blind spots (PLL gap)",
              "what": "The tuner sweep (`marsad tuner-sweep`): tunes the radio across frequencies one at a time and "
                      "records where its synthesiser locks, then narrows each edge and each blind spot to 0.5 MHz. "
                      "This unit's own figures replace the tuner's typical ones.",
              "connect": "Nothing needs to be connected: lock is internal to the tuner. An antenna or a load may stay "
                         "on.",
              "duration": "about 1 to 2 minutes (short test about 20 s: a coarse pass, 100 MHz apart)",
              "expect": "Edges near the tuner's typical figures. Run it cold (before other steps) for the widest "
                        "figures: the edges move as the radio warms."},
    "retune": {"title": "Retune time per hop",
               "what": "Times how long rtl_power spends retuning between hops on this computer, which sets how much "
                       "of a sweep is spent listening. Saved into station.json (one figure for the station: the "
                       "engine does not keep one per radio).",
               "connect": "Nothing needs to be connected.", "duration": "90 s (short test 20 s)",
               "expect": "About 40 to 60 ms per hop on most PCs."},
    "warmup": {"title": "Warm-up drift",
               "what": "Records from cold for an hour into a 50 ohm load and reads the per-sweep whole-band median with "
                       "`marsad drift`: how long this radio takes to settle, which becomes its recommended warm-up.",
               "connect": "Fit the 50 ohm terminator on the radio's input. If the radio has a bias-tee that is always "
                          "on, put a DC block between the radio and the load, or the load shorts the bias-tee.",
               "duration": "60 min (short test 3 min). Start it with the radio cold: unplugged or idle for an hour.",
               "expect": "The floor falls a few tenths of a dB over the first 10 to 60 minutes, then holds within about "
                         "0.1 dB."},
    "floor": {"title": "Its own floor (terminated baseline)",
              "what": "A short run into the 50 ohm load at the starting settings: the receiver's own floor, with no "
                      "antenna. It is marked terminated in the archive, so the waterfall's internal layer can match "
                      "later runs against it.",
              "connect": "Fit the 50 ohm terminator on the radio's input (with a DC block if its bias-tee is always "
                         "on).",
              "duration": "10 min (short test 2 min). Warm the radio first (the warm-up step says how long).",
              "expect": "A flat floor with only the radio's own clock lines above it."},
    "adsb": {"title": "Receive proof (ADS-B)",
             "what": "Listens at 1090 MHz for 60 s through the whole rig and counts aircraft messages: proof the "
                     "antenna path receives at all before any survey relies on it.",
             "connect": "The rig as drawn, antenna on.", "duration": "60 s (short test 15 s)",
             "expect": "Hundreds of frames a minute with any traffic overhead; zero means the path is not receiving."},
    "baseline": {"title": "First baseline with the antenna",
                 "what": "A short survey at the starting settings with the rig's real chain: the first run of this "
                         "radio's comparison set.",
                 "connect": "The rig as drawn, antenna on.", "duration": "10 min (short test 2 min)",
                 "expect": "Signals above a floor a little higher than the terminated one."},
}


# Relay 024: the words that depend on the radio's tuner. Every step's text is chosen for the radio's own tuner.
TUNER_TEXT = {
    "e4000": {"range": {"expect": "Expect about 52 to 2200 MHz, with a blind spot (the PLL gap) somewhere near 1100 to "
                                  "1250 MHz; the gap's edges vary from unit to unit. Run it cold (before other steps) for "
                                  "the widest figures: the whole range slides down as the radio warms."}},
    "r820t": {"range": {"expect": "Expect about 24 to 1766 MHz, normally with no blind spots. Below about 24 MHz this "
                                  "driver switches to HF direct sampling, so the low edge found there is that switch, "
                                  "not a lock limit."}},
}


def step_text(step: str, tuner: str | None) -> dict:
    """One step's what / connect / how long / what to expect, for this radio's tuner (relay 024)."""
    key = "r820t" if (tuner or "").lower() in ("r820t", "r860", "r828d") else (tuner or "").lower()
    return dict(STEPS[step], **(TUNER_TEXT.get(key, {}).get(step) or {}))


def now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------- stations

def safe_name(name: str) -> tuple:
    """A station name as a folder name: characters Windows refuses (and trailing dots or spaces) replaced. Returns
    (folder name, whether anything was replaced)."""
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", name).strip(" .-") or "station"
    return safe, safe != (name or "").strip()


def default_folder(name: str, documents: Path | None = None) -> Path:
    """Documents\\Marsad\\Stations\\<name>."""
    docs = documents or Path(os.environ.get("USERPROFILE") or Path.home()) / "Documents"
    return docs / "Marsad" / "Stations" / safe_name(name)[0]


def free_default_folder(name: str, documents: Path | None = None) -> Path:
    """Relay 023: the default folder, or the next free one ("<name> 2", "<name> 3", ...) when that name is already
    used there, so a name alone is enough to create a station. Nothing is made here."""
    first = default_folder(name, documents)
    if folder_problem(first) is None:
        return first
    for n in range(2, 100):
        cand = first.with_name(f"{first.name} {n}")
        if folder_problem(cand) is None:
            return cand
    return first


def folder_problem(folder: Path) -> str | None:
    """A new station's folder must be new or empty, and never inside (or around) another station."""
    folder = Path(folder).resolve()
    if folder.exists() and (not folder.is_dir() or any(folder.iterdir())):
        return f"{folder} is not empty. Pick a new or empty folder."
    for p in [folder, *folder.parents]:
        if (p / STATION_FILE).exists():
            return f"{folder} is inside the station at {p}. Pick a folder outside every station."
    if folder.exists():
        for sub in folder.rglob(STATION_FILE):
            return f"{folder} holds a station at {sub.parent}."
    return None


def location_problem(loc) -> str | None:
    if loc is None:
        return None
    try:
        lat, lon = float(loc["lat"]), float(loc["lon"])
    except (KeyError, TypeError, ValueError):
        return "The location needs a latitude and a longitude in decimal degrees."
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return "Latitude runs -90 to 90 and longitude -180 to 180."
    return None


def create_station(folder, name: str, description: str = "", location: dict | None = None) -> dict:
    """New Station: the engine's own `marsad init` file, emptied of radios, plus the setup file that turns the
    checklists on. The location, if given, goes only into this station's station.local.json."""
    name = (name or "").strip()
    if not name:
        raise ValueError("A station needs a name.")
    folder = Path(folder)
    err = folder_problem(folder) or location_problem(location)
    if err:
        raise ValueError(err)
    folder.mkdir(parents=True, exist_ok=True)
    data = json.loads(DEFAULT_FILE.read_text(encoding="utf-8"))          # what `marsad init` writes
    data["station_name"] = name
    data["description"] = description or ""
    data["receivers"] = {}                                                # no radios until Add Radio
    data["default_receiver"] = None
    rec = data.get("recorder") if isinstance(data.get("recorder"), dict) else {}
    rec["serials"], rec["ppm"] = {}, {}
    rec["roles"] = {}
    data["recorder"] = rec
    d = data.get("defaults") if isinstance(data.get("defaults"), dict) else {}
    d["chain_order"] = ""                                                 # no rig until one is drawn for this station
    d.pop("chain_used_at", None)
    data["defaults"] = d
    (folder / STATION_FILE).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    # No rigs: without a chain.json the engine seeds two example chains ("antenna", "probe") and calls one current
    (folder / "chain.json").write_text(json.dumps({"schema": 1, "current": None, "chains": {}}, indent=1) + "\n",
                                       encoding="utf-8")
    _save(folder, {"schema": 1, "tracked": True, "created": now(), "name": name, "radios": {}, "jobs": []})
    if location is not None:
        (folder / LOCAL_FILE).write_text(json.dumps({"location": {"lat": float(location["lat"]),
                                                                  "lon": float(location["lon"])}}, indent=1),
                                         encoding="utf-8")
    for sub in ("surveys", "reports", "recordings", "logs"):
        (folder / sub).mkdir(exist_ok=True)
    return {"folder": str(folder.resolve()), "name": name}


def set_location(folder, location: dict | None) -> None:
    """Change or clear a station's location: written to its station.local.json only, never read back."""
    err = location_problem(location)
    if err:
        raise ValueError(err)
    p = Path(folder) / LOCAL_FILE
    if location is None:
        if p.exists():
            p.unlink()
        return
    p.write_text(json.dumps({"location": {"lat": float(location["lat"]), "lon": float(location["lon"])}}, indent=1),
                 encoding="utf-8")


def station_info(station: Station) -> dict:
    """What the window shows about a station. The location is "set" or "not set", never its value."""
    doc = load(station)
    raw = _raw(station)
    return {"root": str(station.root), "name": raw.get("station_name") or doc.get("name") or station.root.name,
            "description": raw.get("description") or "", "tracked": bool(doc.get("tracked")),
            "location_set": "location" in station.data}


# ---------------------------------------------------------------- the setup file

def _path(root) -> Path:
    return Path(root) / SETUP_FILE


def load(station_or_root) -> dict:
    root = station_or_root.root if isinstance(station_or_root, Station) else Path(station_or_root)
    p = _path(root)
    if not p.exists():
        return {"tracked": False, "radios": {}, "jobs": []}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"tracked": True, "radios": {}, "jobs": [], "unreadable": True}
    d.setdefault("radios", {})
    d.setdefault("jobs", [])
    return d


def _save(root, doc: dict) -> None:
    p = _path(root)
    tmp = p.with_suffix(".json.part")
    tmp.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    for attempt in range(20):           # Windows refuses the swap while another process is reading the file
        try:
            os.replace(tmp, p)
            return
        except PermissionError:
            if attempt == 19:
                raise
            import time as _t
            _t.sleep(0.05)


def tracked(station: Station) -> bool:
    return bool(load(station).get("tracked"))


def _raw(station: Station) -> dict:
    try:
        return json.loads(station.file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# ---------------------------------------------------------------- radios

def typical(tuner: str) -> dict:
    """The tuner's typical figures from the engine's own template, marked as not measured."""
    tuner = (tuner or "").lower()
    tpl = json.loads(DEFAULT_FILE.read_text(encoding="utf-8")).get("receivers") or {}
    key = "r820t" if tuner in ("r820t", "r860", "r828d") else tuner
    t = tpl.get(key) or {}
    return {"tuner": key or tuner, "label": t.get("label") or f"RTL-SDR ({tuner})",
            "range_mhz": t.get("range_mhz") or [24, 1766], "gaps_mhz": t.get("gaps_mhz") or [],
            "gains": TUNER_GAINS.get(key) or [], "tuner_label": TUNER_LABEL.get(key, tuner),
            "source": "typical for this tuner, not yet measured"}


def tuner_key(text: str | None) -> str | None:
    """rtl_eeprom's tuner words to the engine's tuner key."""
    t = (text or "").lower()
    if "e4000" in t or "elonics" in t:
        return "e4000"
    if "r820" in t or "r860" in t or "rafael" in t or "r828" in t:
        return "r820t"
    return None


def duplicate_serials(found: list) -> list:
    seen, dup = {}, set()
    for f in found:
        s = str(f.get("serial") or "")
        if s and s in seen:
            dup.add(s)
        seen[s] = True
    return sorted(dup)


DUP_HELP = ("Two radios report the same serial (many are sold as 00000001), so marsad cannot tell them apart. "
            "Give one a unique serial with rtl_eeprom (for example `rtl_eeprom -d 1 -s 00000102` with only that "
            "radio's index), then unplug and replug it. marsad does not do this for you: it writes to the radio.")


def add_radio(station: Station, serial: str, nickname: str, tuner: str, range_mhz=None, gaps_mhz=None,
              label: str = "", maker: str = "", product: str = "") -> dict:
    """Add Radio: a receiver in station.json, in the format the engine reads, matched by serial (rule 8)."""
    nick = (nickname or "").strip()
    serial = str(serial or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,23}", nick):
        raise ValueError("A nickname is 1 to 24 letters, digits, - or _, starting with a letter or digit.")
    if not serial:
        raise ValueError("The radio's serial is needed: marsad finds each radio by it.")
    raw = _raw(station)
    recs = raw.get("receivers") if isinstance(raw.get("receivers"), dict) else {}
    if nick in recs:
        raise ValueError(f"This station already has a radio called {nick}.")
    rec = raw.get("recorder") if isinstance(raw.get("recorder"), dict) else {}
    serials = rec.get("serials") if isinstance(rec.get("serials"), dict) else {}
    for other, s in serials.items():
        if str(s) == serial:
            raise ValueError(f"Serial {serial} is already this station's {other}.")
    key = tuner_key(tuner) or (tuner or "").lower()
    if key not in TUNER_GAINS:
        raise ValueError(f"Unknown tuner {tuner!r}: choose E4000 or R820T/R860.")
    typ = typical(key)
    rng = [float(v) for v in (range_mhz or typ["range_mhz"])]
    if len(rng) != 2 or not rng[0] < rng[1]:
        raise ValueError("The range is two frequencies in MHz, low then high.")
    # relay 024: blind spots are not measured yet; none is stored until the tuner sweep finds them (or the owner
    # types them). The range is the tuner's typical one, kept as an estimate.
    gaps = [[float(a), float(b)] for a, b in (gaps_mhz or [])]
    entry = {"label": label or typ["label"], "tuner": key, "range_mhz": rng, "gaps_mhz": gaps, "device": 0}
    if maker or product:
        entry["usb"] = {"maker": maker, "product": product}
    recs[nick] = entry
    raw["receivers"] = recs
    serials[nick] = serial
    rec["serials"] = serials
    raw["recorder"] = rec
    if not raw.get("default_receiver") or raw.get("default_receiver") not in recs:
        raw["default_receiver"] = nick
    station.file.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    doc = load(station)
    estimate = range_mhz is None or rng == [float(v) for v in typ["range_mhz"]]
    doc["radios"][nick] = {"serial": serial, "added": now(), "tuner": key,
                           "read": {"maker": maker, "product": product},
                           "facts": {"range_mhz": {"value": rng, "kind": "estimate" if estimate else "entered",
                                                   "source": f"estimate: typical for {TUNER_LABEL.get(key, key)}, not "
                                                             "measured on this unit" if estimate else
                                                             "entered by the owner"},
                                     "gaps_mhz": {"value": gaps, "kind": "not measured" if gaps_mhz is None else
                                                  "entered", "source": "not measured yet" if gaps_mhz is None else
                                                  "entered by the owner"}},
                           "results": {}}
    _save(station.root, doc)
    return {"radio": nick, "entry": entry}


# ---------------------------------------------------------------- commands for each step

def tile(rx: dict) -> tuple[float, float]:
    """The starting settings' range: 100 MHz a quarter of the way up the radio's range, clear of its gaps."""
    lo, hi = rx.get("range_mhz") or [24, 1766]
    a = round((lo + (hi - lo) * 0.25) / 10) * 10
    for g0, g1 in rx.get("gaps_mhz") or []:
        if a < g1 and a + 100 > g0:
            a = round(g1 / 10 + 1) * 10
    a = max(a, lo + 1)
    return float(a), float(min(a + 100, hi - 1))


def gain_for(tuner: str) -> float:
    ladder = TUNER_GAINS.get(tuner) or [29.0]
    return min(ladder, key=lambda g: abs(g - 29.0))


def step_command(station: Station, nick: str, step: str, short: bool, device: int, rig_chain: str = "",
                 stop_file: str | None = None) -> dict:
    """The existing command for one step, as engine arguments (kind "marsad") or a tool's (kind "tool"), or why
    there is none. `stop_file` (relay 025): for the steps that stop cleanly on one (STOP_FILE_STEPS), Cancel writes it."""
    cmd = _step_command(station, nick, step, short, device, rig_chain)
    if stop_file and step in STOP_FILE_STEPS and cmd.get("kind") == "marsad":
        cmd["args"] = cmd["args"] + ["--stop-file", str(stop_file)]
    return cmd


def _step_command(station: Station, nick: str, step: str, short: bool, device: int, rig_chain: str = "") -> dict:
    rx = station.receivers.get(nick) or {}
    tuner = rx.get("tuner") or ""
    g = gain_for(tuner)
    lo, hi = tile(rx)
    freq = f"{lo:g}M:{hi:g}M:50k"
    stamp_tag = f"setup-{step}-{nick}" + ("-short" if short else "")
    if step == "range":                                    # relay 024: the tuner sweep, for any tuner
        return {"kind": "marsad", "args": ["tuner-sweep", "--receiver", nick] + (["--short"] if short else [])}
    if step == "retune":
        return {"kind": "marsad", "args": ["calibrate-retune", "--device", str(device), "--gain", f"{g:g}", "--save",
                                           "--seconds", "20" if short else "90"]}
    if step in ("warmup", "floor", "baseline"):
        dur = {"warmup": "3m" if short else "60m", "floor": "2m" if short else "10m",
               "baseline": "2m" if short else "10m"}[step]
        chain = rig_chain if step == "baseline" else f"{LOAD} > {nick}"
        cond = {"warmup": "setup: warm-up drift from cold, 50 ohm load on the input",
                "floor": "setup: the radio's own floor, 50 ohm load on the input",
                "baseline": "setup: first baseline with the antenna, the rig as drawn"}[step]
        if step == "baseline" and not chain:
            return {"missing": "The rig has no path to this radio yet: draw it on the Chain screen first."}
        return {"kind": "marsad", "args": ["run", "custom", dur, "--freq", freq, "--gain", f"{g:g}", "--integration",
                                           "30", "--receiver", nick, "--device", str(device), "--tag", stamp_tag,
                                           "--chain", chain, "--conditions", cond, "--non-interactive"]}
    if step == "adsb":
        adsb_gain = 40.2 if tuner == "r820t" else gain_for(tuner)       # adsb-check's own default is an R820T step
        return {"kind": "marsad", "args": ["adsb-check", "--seconds", "15" if short else "60", "--device", str(device),
                                           "--gain", f"{adsb_gain:g}",
                                           "--label", (rig_chain or nick)[:200]]}
    raise ValueError(f"unknown step {step!r}")


# ---------------------------------------------------------------- results

_RANGE = re.compile(r"E4K range:\s*(-?\d+)\s*to\s*(\d+)\s*MHz", re.I)
_GAP = re.compile(r"E4K L-band gap:\s*(\d+)\s*to\s*(\d+)\s*MHz", re.I)
_RETUNE = re.compile(r"retune per hop\s+([\d.]+)\s*ms")
_SWEEP_RANGE = re.compile(r"tuner-sweep: range\s+([\d.]+)\s+to\s+([\d.]+)\s*MHz")
_SWEEP_GAP = re.compile(r"tuner-sweep: gap\s+([\d.]+)\s+to\s+([\d.]+)\s*MHz")
_SIDECAR_ADSB = re.compile(r"sidecar:\s*(\S+\.json)\s*$", re.M)


def _drift(station: Station, recording: str) -> dict:
    """`marsad drift` on a setup run's recording: its own printed verdict, read back."""
    from . import drift
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            drift.main([str(station.surveys / recording)], station)
    except SystemExit as exc:
        return {"error": str(exc).strip() or "drift could not read the run"}
    text = buf.getvalue()
    out = {"text": text}
    m = re.search(r"settled by sweep\s+(\d+) of (\d+)(?:\s+\(~(\d+) min in\))?", text)
    if m:
        out["settle_sweep"], out["sweeps"] = int(m.group(1)), int(m.group(2))
        out["settle_min"] = int(m.group(3)) if m.group(3) else None
    m = re.search(r"after settling:\s+mean\s+([+-][\d.]+) dB", text)
    if m:
        out["mean_db"] = float(m.group(1))
    m = re.search(r"total change\s+([+-][\d.]+) dB", text)
    if m:
        out["total_change_db"] = float(m.group(1))
    out["pass"] = "VERDICT: PASS" in text
    return out


def result_of(station: Station, job: dict, status: dict) -> dict:
    """What a finished step found, from its own output, with its end result in plain words (relay 025): `outcome`
    "done", "attention" or "failed", and `what`, `why` and `advice` for anything but done."""
    res = _result_of(station, job, status)
    if job.get("id"):
        res["job"] = job["id"]
    log = Path(job.get("log") or "")
    try:
        full = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
    except OSError:
        full = ""
    full = full or "\n".join(status.get("lines") or [])
    res.update(explain(station, job, res, full, status))
    return res


def _result_of(station: Station, job: dict, status: dict) -> dict:
    """What a finished step found, from its own output. ok False says why in plain words (never quietly accepted)."""
    step, text = job["step"], "\n".join(status.get("lines") or [])
    log = Path(job.get("log") or "")
    try:
        full = log.read_text(encoding="utf-8", errors="replace") if log.exists() else text
    except OSError:
        full = text
    code = status.get("exit_code")
    res = {"at": now(), "step": step, "short": bool(job.get("short")), "by": STEPS[step]["title"],
           "log": job.get("log"), "exit_code": code, "ok": False}
    if job.get("basis") is not None:
        res["basis"] = job["basis"]             # relay 022: what it rested on when it was started
    if step == "range" and "tuner-sweep:" in full:         # relay 024
        m = _SWEEP_RANGE.search(full)
        if not m:
            bad = re.search(r"tuner-sweep: (?:FAILED|REFUSED): (.*)", full)
            res["note"] = "the tuner sweep found no range: " + (bad.group(1) if bad else (status.get("reason")
                                                                                      or f"exit {code}"))
            return res
        lo, hi = float(m.group(1)), float(m.group(2))
        gaps = [[float(a), float(b)] for a, b in _SWEEP_GAP.findall(full)]
        res.update(range_mhz=[lo, hi], gaps_mhz=gaps, by="tuner sweep", method="tuner-sweep")
        edge_open = "true low edge is lower" in full or "true high edge is higher" in full
        low_direct = "where the driver switches to HF direct sampling" in full
        res["low_edge_kind"] = "direct sampling switch" if low_direct else "lock limit"
        odd = "UNEXPECTED: the driver reported HF direct sampling" in full
        far = _far_from_typical(station, job.get("radio"), lo, hi)       # relay 025: the 12-to-24 MHz kind of answer
        res["ok"] = 1 <= lo < hi <= 3000 and not edge_open and not odd and not far
        res["note"] = (f"locks {lo:g} to {hi:g} MHz" + ("".join(f", blind spot {a:g} to {b:g} MHz" for a, b in gaps)
                                                         if gaps else ", no blind spots")
                       + (f"; low edge {lo:g} MHz: where the driver switches to HF direct sampling"
                          if low_direct else "")
                       + ("" if not edge_open else ": an edge lay outside the sweep")
                       + ("" if not odd else ": the driver reported direct sampling inside the range")
                       + ("" if not far else f": {far}")
                       + (" (short test: a coarse pass, edges within about 100 MHz)" if job.get("short") else ""))
        return res
    if step == "range":
        m, g = _RANGE.search(full), _GAP.search(full)
        if not m:
            res["note"] = "rtl_test did not report a range: " + (status.get("reason") or "no output")
            return res
        lo, hi = float(m.group(1)), float(m.group(2))
        res.update(range_mhz=[lo, hi], gaps_mhz=[[float(g.group(1)), float(g.group(2))]] if g else [])
        res["ok"] = 20 <= lo < hi <= 2600
        res["note"] = (f"locks {lo:g} to {hi:g} MHz" + (f", gap {g.group(1)} to {g.group(2)} MHz" if g else "")
                       + ("" if res["ok"] else ": these figures look wrong for an RTL-SDR; run it again"))
        return res
    if step == "retune":
        m = _RETUNE.search(full)
        if not m:
            res["note"] = "calibrate-retune did not report a figure: " + (status.get("reason") or "no output")
            return res
        v = float(m.group(1))
        res.update(retune_ms_per_hop=v, ok=5 <= v <= 500)
        res["note"] = f"{v:g} ms per hop" + ("" if res["ok"] else ": outside what an RTL-SDR does; run it again")
        return res
    if step == "adsb":
        m = list(_SIDECAR_ADSB.finditer(full))
        try:
            side = json.loads(Path(m[-1].group(1)).read_text()) if m else {}
        except (OSError, ValueError):
            side = {}
        st = side.get("stats") or {}
        verdict = side.get("verdict") or "error"
        res.update(verdict=verdict, frames_per_min=st.get("frames_per_min"), aircraft=st.get("icao_distinct"),
                   df17=(st.get("df_counts") or {}).get("17") or (st.get("df_counts") or {}).get(17),
                   ok=verdict == "pass")
        res["note"] = (f"{verdict.upper()}: {st.get('frames_per_min', 0)} frames/min, {st.get('icao_distinct', 0)} "
                       f"aircraft" + ("" if verdict == "pass" else ". " + ADSB_ADVICE))
        return res
    rec = status.get("recording")
    if not rec or not (station.surveys / rec).exists():
        res["note"] = "the run wrote no recording: " + (status.get("reason") or f"exit {code}")
        return res
    res["recording"] = rec
    d = _drift(station, rec)
    if d.get("error"):
        res["note"] = d["error"]
        return res
    res.update({k: v for k, v in d.items() if k != "text"})
    if step == "warmup":
        mins = d.get("settle_min")
        res["warmup_min"] = mins
        res["ok"] = mins is not None and not job.get("short")
        res["note"] = (f"settled after about {mins} min (sweep {d.get('settle_sweep')} of {d.get('sweeps')}), total "
                       f"change {d.get('total_change_db', 0):+.2f} dB" if mins is not None else
                       "the run did not settle within its length: run the full hour")
        if job.get("short"):
            res["note"] += " (short test: too short to set a warm-up)"
    elif step == "floor":
        res["floor_db"] = d.get("mean_db")
        res["ok"] = d.get("mean_db") is not None
        marked = _mark_terminated(station, rec)
        res["marked_terminated"] = marked
        res["note"] = (f"floor {d['mean_db']:+.2f} dB (relative, rtl_power's scale), "
                       + ("marked terminated in the archive" if marked else
                          "not in the archive yet, so not marked terminated: `marsad archive mark <run> --terminated` "
                          "does it once the run is archived")
                       if d.get("mean_db") is not None else "no settled floor could be read")
    elif step == "baseline":
        res["ok"] = (d.get("sweeps") or 0) >= 2
        res["note"] = (f"{d.get('sweeps')} sweeps recorded: the first run of this radio's comparison set"
                       if res["ok"] else "too few sweeps to count as a baseline")
    return res


ADSB_ADVICE = ("Check the antenna is connected through the rig as drawn, that nothing else is using the radio, and that "
               "the gain is not at its lowest. With no aircraft overhead a quiet result can be honest: try again at a "
               "busier time before changing anything.")


def _mark_terminated(station: Station, recording: str) -> bool:
    """`marsad archive mark <run> --terminated`, so /internal can match later runs against it. False when the archive
    does not hold the run (yet); the measured floor stands either way."""
    from . import archive
    try:
        with redirect_stdout(io.StringIO()):
            return archive.main(["mark", Path(recording).stem, "--terminated"], station) == 0
    except (SystemExit, Exception):  # noqa: BLE001
        return False


def record(station: Station, nick: str, res: dict) -> None:
    """Keep a result (never replace one) and apply what the engine reads: a measured range and gap go into the
    radio's station.json entry; the retune figure was saved by calibrate-retune itself."""
    doc = load(station)
    r = doc["radios"].setdefault(nick, {"results": {}})
    r.setdefault("results", {}).setdefault(res["step"], []).append(res)
    if res["step"] == "range" and res.get("ok") and not res.get("short") and not res.get("accepted_typical"):
        raw = _raw(station)
        e = (raw.get("receivers") or {}).get(nick)
        if e is not None:
            e["range_mhz"], e["gaps_mhz"] = res["range_mhz"], res["gaps_mhz"]
            station.file.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
            how = res.get("by") or "Tuning range and blind spots"
            r.setdefault("facts", {})["range_mhz"] = {"value": res["range_mhz"], "kind": "measured",
                                                      "source": f"measured on {res['at'][:10]} by {how}"}
            r["facts"]["gaps_mhz"] = {"value": res["gaps_mhz"], "kind": "measured",
                                      "source": f"measured on {res['at'][:10]} by {how}"}
    if res["step"] == "warmup" and res.get("ok"):
        r.setdefault("facts", {})["warmup_min"] = {"value": res.get("warmup_min"),
                                                   "source": f"measured {res['at'][:10]} by Warm-up drift"}
    if res["step"] == "floor" and res.get("ok"):
        r.setdefault("facts", {})["floor_db"] = {"value": res.get("floor_db"), "recording": res.get("recording"),
                                                 "source": f"measured {res['at'][:10]} by Its own floor"}
    if res["step"] == "retune" and res.get("ok") and not res.get("short"):
        r.setdefault("facts", {})["retune_ms_per_hop"] = {"value": res.get("retune_ms_per_hop"),
                                                          "source": f"measured {res['at'][:10]} by Retune time per hop "
                                                                    "(saved for the whole station)"}
    _save(station.root, doc)


def basis(station: Station, nick: str, chain_text: str = "") -> dict:
    """Relay 022: what a step done now rests on (see DEPENDS), kept with its result."""
    rx = station.receivers.get(nick) or {}
    serial = ((station.data.get("recorder") or {}).get("serials") or {}).get(nick)
    lo, hi = tile(rx)
    return {"add": f"{rx.get('tuner') or ''}|{serial or ''}", "range": f"{lo:g}-{hi:g}", "rig": chain_text or ""}


def changed_since(step: str, res: dict, now_basis: dict) -> list:
    """Which of the step's foundations changed since its result was made (results from before relay 022 carry no
    basis, and are taken as they are)."""
    was = res.get("basis") or {}
    return [d for d in DEPENDS.get(step, ()) if d in was and was[d] != now_basis.get(d)]


def update_radio(station: Station, nick: str, serial=None, tuner=None, range_mhz=None, gaps_mhz=None, maker=None,
                 product=None) -> dict:
    """Relay 022: reopen step 1 and correct the radio (serial, tuner, range, gaps, maker, product). Its later steps
    that rest on what changed then read "needs re-checking"."""
    raw = _raw(station)
    recs = raw.get("receivers") if isinstance(raw.get("receivers"), dict) else {}
    if nick not in recs:
        raise ValueError(f"This station has no radio called {nick}.")
    e = recs[nick]
    rec = raw.setdefault("recorder", {})
    serials = rec.setdefault("serials", {})
    doc = load(station)
    r = doc["radios"].setdefault(nick, {"results": {}})
    facts = r.setdefault("facts", {})
    if serial is not None and str(serial).strip() and str(serial).strip() != str(serials.get(nick) or ""):
        serial = str(serial).strip()
        for other, s_ in serials.items():
            if other != nick and str(s_) == serial:
                raise ValueError(f"Serial {serial} is already this station's {other}.")
        serials[nick] = serial
        r["serial"] = serial
    if tuner is not None:
        key = tuner_key(tuner) or (tuner or "").lower()
        if key not in TUNER_GAINS:
            raise ValueError(f"Unknown tuner {tuner!r}: choose E4000 or R820T/R860.")
        e["tuner"] = key
        r["tuner"] = key
    if range_mhz is not None:
        rng = [float(v) for v in range_mhz]
        if len(rng) != 2 or not rng[0] < rng[1]:
            raise ValueError("The range is two frequencies in MHz, low then high.")
        if rng != [float(v) for v in e.get("range_mhz") or []]:
            e["range_mhz"] = rng
            facts["range_mhz"] = {"value": rng, "source": f"entered by the owner {now()[:10]}"}
    if gaps_mhz is not None:
        gaps = [[float(a), float(b)] for a, b in gaps_mhz]
        if gaps != [[float(a), float(b)] for a, b in e.get("gaps_mhz") or []]:
            e["gaps_mhz"] = gaps
            facts["gaps_mhz"] = {"value": gaps, "source": f"entered by the owner {now()[:10]}"}
    if maker is not None or product is not None:
        usb = e.setdefault("usb", {})
        if maker is not None:
            usb["maker"] = maker
        if product is not None:
            usb["product"] = product
    station.file.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    _save(station.root, doc)
    return {"radio": nick, "entry": e}


def accept_typical(station: Station, nick: str, chain_text: str = "") -> None:
    """The owner keeps the range as it is, an estimate (the tuner's typical figures, or entered by hand), instead of
    measuring it. Relay 024: the guide can go on, but no estimate unlocks scanning; and a range already measured,
    unchanged since, has no estimate left to keep."""
    rx = station.receivers.get(nick) or {}
    last = latest(load(station)["radios"].get(nick) or {}, "range")
    if last and last.get("ok") and not last.get("accepted_typical") and             [float(v) for v in last.get("range_mhz") or []] == [float(v) for v in rx.get("range_mhz") or []]:
        raise ValueError("This radio's range is already measured (by the tuner sweep), so there is no estimate to "
                         "keep. Run the sweep again to measure it anew.")
    record(station, nick, {"at": now(), "step": "range", "short": False, "ok": True, "accepted_typical": True,
                           "basis": basis(station, nick, chain_text),
                           "by": "the owner (no measuring command for this tuner)",
                           "range_mhz": rx.get("range_mhz"), "gaps_mhz": rx.get("gaps_mhz"),
                           "note": "the estimate kept: typical figures, not measured on this unit. Scanning stays "
                                   "locked until the tuner sweep measures them."})


def confirm_rig(station: Station, nick: str, chain_text: str) -> None:
    doc = load(station)
    r = doc["radios"].setdefault(nick, {"results": {}})
    r["rig_confirmed"] = {"at": now(), "text": chain_text}
    _save(station.root, doc)


# ---------------------------------------------------------------- the checklist

def latest(r: dict, step: str, full: bool = True) -> dict | None:
    rs = [x for x in (r.get("results") or {}).get(step) or [] if not (full and x.get("short"))]
    return rs[-1] if rs else None


def _sub_state(station: Station, nick: str, r: dict, m: str, running: set, now_basis: dict) -> tuple:
    """One measurement's or path check's state and words. Relay 022: a done step whose foundations changed since
    (DEPENDS) reads "needs re-checking"; so does a range changed by hand after it was measured."""
    last = latest(r, m)
    if (nick, m) in running:
        return "in progress", "running now: closing this guide or moving Back does not stop it"
    if last and last.get("ok"):
        moved = changed_since(m, last, now_basis)
        if moved:
            when = str(last.get("at", ""))[:16].replace("T", " ")
            words = " and ".join(BASIS_WORDS[d] for d in moved)
            return "needs re-checking", (words[0].upper() + words[1:] + f" changed since this was done ({when}): run "
                                         f"it again. Was: {last.get('note') or 'done'}")
        if m == "range" and last.get("accepted_typical"):
            return "estimate kept", ("An estimate, not measured on this unit. Scanning stays locked until the tuner "
                                     "sweep measures it: run it.")
        if m == "range":
            rx = station.receivers.get(nick) or {}
            same = ([float(v) for v in last.get("range_mhz") or []] == [float(v) for v in rx.get("range_mhz") or []]
                    and [[float(a), float(b)] for a, b in last.get("gaps_mhz") or []]
                    == [[float(a), float(b)] for a, b in rx.get("gaps_mhz") or []])
            if not same:
                return "needs re-checking", ("The range was changed by hand since it was "
                                             + ("kept" if last.get("accepted_typical") else "measured")
                                             + ": measure it again, or keep the figures as they are now.")
        return "done", last.get("note") or ""
    if last:
        return "needs attention", last.get("note") or ""
    return "not started", ""


def _agg(states: list) -> str:
    if all(s == "done" for s in states):
        return "done"
    for s in ("in progress", "needs attention", "needs re-checking", "estimate kept"):
        if s in states:
            return s
    return "in progress" if "done" in states else "not started"


def checklist(station: Station, nick: str, chain_text: str, running: set) -> dict:
    """Each step's state for one radio: "done", "in progress", "needs attention", "needs re-checking" (relay 022) or
    "not started", what is missing, and whether scans on it may start."""
    doc = load(station)
    r = doc["radios"].get(nick) or {}
    serials = (station.data.get("recorder") or {}).get("serials") or {}
    now_basis = basis(station, nick, chain_text)
    steps = []

    def add(key, state, detail, sub=None):
        steps.append({"step": key, "title": STEP_TITLES[key], "state": state, "detail": detail, "sub": sub or []})

    have = nick in station.receivers and bool(serials.get(nick))
    add("add", "done" if have else "not started",
        f"serial {serials.get(nick)}" if have else "Add the radio first.")
    sub = []
    for m in MEASURES:
        st, detail = _sub_state(station, nick, r, m, running, now_basis)
        sub.append({"step": m, "title": STEPS[m]["title"], "state": st, "detail": detail,
                    "results": len((r.get("results") or {}).get(m) or [])})
    states = [s["state"] for s in sub]
    add("measure", _agg(states), f"{states.count('done')} of {len(MEASURES)} measured", sub)
    add("draw", "done" if chain_text else "not started",
        chain_text if chain_text else "The rig used for runs has no path to this radio. Draw it on the Chain screen and "
                                      "use it for runs.")
    conf = r.get("rig_confirmed") or {}
    ok_conf = bool(chain_text) and conf.get("text") == chain_text
    add("confirm", "done" if ok_conf else "needs re-checking" if conf else "not started",
        f"confirmed {conf.get('at', '')[:16].replace('T', ' ')}" if ok_conf else
        "The drawing changed since it was confirmed: confirm it again." if conf else
        "Check the rig matches the drawing, then confirm.")
    sub = []
    for m in PATHS:
        st, detail = _sub_state(station, nick, r, m, running, now_basis)
        sub.append({"step": m, "title": STEPS[m]["title"], "state": st, "detail": detail,
                    "results": len((r.get("results") or {}).get(m) or [])})
    states = [s["state"] for s in sub]
    add("paths", _agg(states), f"{states.count('done')} of {len(PATHS)} passed", sub)
    missing = next((s for s in steps if s["state"] != "done"), None)
    return {"radio": nick, "steps": steps, "ready": missing is None, "facts": r.get("facts") or {},
            "missing": None if missing is None else {"step": missing["step"], "number": STEP_ORDER.index(missing["step"]) + 1,
                                                     "title": missing["title"], "detail": missing["detail"]}}


def lock_reason(station: Station, nick: str, chain_text: str = "", running: set | None = None) -> str | None:
    """Why a scan on this radio may not start yet (None: it may). Untracked stations never lock."""
    if not tracked(station):
        return None
    c = checklist(station, nick, chain_text, running or set())
    if c["ready"]:
        return None
    m = c["missing"]
    return (f"The {nick} is not set up yet: step {m['number']}, {m['title']} ({m['detail']}). "
            "Open Set up this station to finish it.")


# ---------------------------------------------------------------- relay 025: progress, Cancel, plain end results
#
# Every measurement and path check shows how far it has got, what it is doing now, the time gone and left, a Cancel,
# the last lines of its log, and at the end one result in plain words: done, needs attention (what went wrong, why it
# probably happened, what to do) or failed. Progress is read from each command's own output: the tuner sweep prints a
# line per tuning, `marsad run` prints its own clock; calibrate-retune and adsb-check print nothing while they work, so
# theirs is a time-based estimate and says so. Cancel stops only that run (rule 1): the sweep and `marsad run` stop
# cleanly on a stop file; the others are ended with their process tree. A cancelled run keeps no result.

STOP_FILE_STEPS = {"range", "warmup", "floor", "baseline"}
OUTCOME_MARK = {"done": "✓", "attention": "⚠", "failed": "✗"}

_SWEEP_PROGRESS = re.compile(r"tuner-sweep: progress (\d+) of (\d+): (.*)")
_RUN_PROGRESS = re.compile(r"(\d+(?::\d\d){1,2}) elapsed, (\d+(?::\d\d){1,2}) left \(\s*(\d+)%\)")
_ACTIVITY = {"warmup": "recording into the 50 ohm load from cold", "floor": "recording the radio's own floor into "
             "the 50 ohm load", "baseline": "recording the first baseline through the rig",
             "retune": "timing how long each retune takes (rtl_power)", "adsb": "listening at 1090 MHz for aircraft"}


def _far_from_typical(station: Station, nick, lo: float, hi: float) -> str | None:
    """Words when a measured range is far outside what any unit of this tuner does (relay 025: an R820T reported as
    "12 to 24 MHz"). A real unit's edges move by tens of MHz, never by most of its range."""
    if not nick:
        return None
    t = typical(station.tuner_of(nick) or "")
    tlo, thi = (t.get("range_mhz") or [None, None])[:2]
    if not tlo or not thi:
        return None
    if hi < thi * 0.6 or lo > tlo * 3 + 20 or hi - lo < (thi - tlo) * 0.5:
        return f"far from what this tuner normally does (about {tlo:g} to {thi:g} MHz)"
    return None


def _secs(text: str) -> float:
    s = 0
    for p in (int(x) for x in text.split(":")):
        s = s * 60 + p
    return float(s)


def _started_ts(job: dict) -> float | None:
    try:
        return dt.datetime.fromisoformat(str(job.get("started"))).timestamp()
    except (TypeError, ValueError):
        return None


def _arg(job: dict, name: str):
    a = [str(x) for x in job.get("argv") or []]
    return a[a.index(name) + 1] if name in a and a.index(name) + 1 < len(a) else None


def progress_of(job: dict, status: dict, now_ts: float | None = None) -> dict:
    """How a running step is doing: {"pct", "activity", "elapsed_s", "remaining_s", "basis"}. `basis` says where the
    figures come from: "the run's own clock", "the pace so far" (the sweep), or "estimated from the time" (commands
    that cannot report progress; pct then stops short of 100 until they end)."""
    import time as _t
    now_ts = now_ts if now_ts is not None else _t.time()
    t0 = _started_ts(job)
    el = max(0.0, now_ts - t0) if t0 else None
    lines = [ln for ln in status.get("lines") or [] if ln.strip()]
    step = job.get("step")
    out = {"pct": None, "activity": "starting: opening the radio", "elapsed_s": el, "remaining_s": None,
           "basis": None}
    if step == "range":
        for ln in reversed(lines):
            m = _SWEEP_PROGRESS.search(ln)
            if m:
                d, tot = int(m.group(1)), max(1, int(m.group(2)))
                out.update(pct=min(99.0, 100.0 * d / tot), activity=m.group(3).strip(), basis="the pace so far")
                if d > 0 and el:
                    out["remaining_s"] = el / d * max(0, tot - d)
                break
        if any(ln.startswith("tuner-sweep: range") for ln in lines):
            out.update(pct=99.0, activity="finishing: writing the result", remaining_s=0)
        return out
    if step in ("warmup", "floor", "baseline"):
        for ln in reversed(lines):
            m = _RUN_PROGRESS.search(ln)
            if m:
                pct = float(m.group(3))
                out.update(pct=min(99.0, pct), activity=_ACTIVITY[step], elapsed_s=_secs(m.group(1)),
                           remaining_s=_secs(m.group(2)), basis="the run's own clock")
                if pct >= 100:
                    out.update(activity="analysing the recording", remaining_s=None)
                break
        else:
            dur = _arg(job, "custom")
            if dur and el is not None:
                total = float(dur[:-1]) * (60 if dur.endswith("m") else 3600 if dur.endswith("h") else 1)
                out.update(pct=min(95.0, 100.0 * el / (total + 30)), remaining_s=max(0.0, total + 30 - el),
                           basis="estimated from the time", activity="starting the recording (rtl_power)")
        return out
    secs = _arg(job, "--seconds")
    if secs and el is not None:
        total = float(secs) + 10.0                     # the radio opening and the result being read
        out.update(pct=min(95.0, 100.0 * el / total), remaining_s=max(0.0, total - el), basis="estimated from the time",
                   activity=_ACTIVITY.get(step, "working"))
    return out


def explain(station: Station, job: dict, res: dict, full: str, status: dict) -> dict:
    """The end result of a finished run in plain words: outcome "done", "attention" (it ran, but what it found cannot
    be used: what went wrong, why it probably did, what to do) or "failed" (it could not run: the error and the log)."""
    step, low = res["step"], (full or "").lower()
    log_tail = [ln for ln in (full or "").splitlines() if ln.strip()][-6:]
    send = "If it happens again, open the full log and send it to Claude Code"

    def failed(what, why, advice):
        return {"outcome": "failed", "what": what, "why": why, "advice": advice, "error_lines": log_tail}

    def attention(what, why, advice):
        return {"outcome": "attention", "what": what, "why": why, "advice": advice}

    if "traceback (most recent call last)" in low:
        return failed("Marsad itself stopped with an error before the measurement finished.",
                      "A fault in Marsad, not in the radio or the rig.",
                      ["Open the full log", "Send the log to Claude Code"])
    refused = re.search(r"tuner-sweep: REFUSED: (.*)", full or "")
    busy = refused or any(w in low for w in ("usb_claim_interface", "another program has the radio open",
                                              "holds the radio"))
    if busy and not res.get("ok"):
        return failed("The radio was in use, so nothing was measured.",
                      (refused.group(1).strip() if refused else "Another program had the radio open.")
                      + " Only one program can use a radio at a time.",
                      ["Close other SDR programs (SDR++, a scan, a recorder) or wait for them to finish",
                       "Run it again"])
    if any(w in low for w in ("i2c wr failed", "i2c rd failed", "stopped answering over usb",
                              "rtlsdr_write_reg failed")) and not res.get("ok"):
        return failed("The radio stopped answering over USB part-way through.",
                      "A loose or long USB lead, a hub, or the USB isolator dropping the radio.",
                      ["Check the USB lead is pushed fully home",
                       "If the radio goes through the USB isolator, power-cycle the isolator", "Run it again"])
    if any(w in low for w in ("no supported devices", "no matching devices", "was not found",
                              "did not answer")) and not res.get("ok"):
        return failed("The radio could not be opened.",
                      "It is unplugged, Windows lost it, or it has not been found again after being moved.",
                      ["Check the radio is plugged in",
                       "If it goes through the USB isolator, power-cycle the isolator", "Run it again"])
    code = res.get("exit_code")
    if not res.get("ok") and code not in (0, None) and not any(
            k in res for k in ("range_mhz", "retune_ms_per_hop", "verdict", "recording")):
        return failed(f"The measurement ended with an error (exit {code}) before it had a result.",
                      (status.get("reason") or "The last lines of its log say what it was doing.").strip(),
                      ["Open the full log", "Run it again", send])
    if res.get("ok"):
        return {"outcome": "done", "what": res.get("note") or "done", "why": "", "advice": []}
    note = res.get("note") or ""
    if step == "range":
        lo, hi = (res.get("range_mhz") or [0, 0])[:2]
        t = typical(station.tuner_of(job.get("radio") or "") or "")
        tl, th = (t.get("range_mhz") or [0, 0])[:2]
        if "far from what this tuner normally does" in note:
            return attention(f"The sweep says the tuner locks only from {lo:g} to {hi:g} MHz. This kind of tuner "
                             f"({t.get('tuner_label') or 'its type'}) locks from about {tl:g} to {th:g} MHz.",
                             "Most likely the lock reading went wrong, not the radio: a radio's range does not "
                             "shrink by that much.",
                             ["Run it again", send + ": its log holds the driver's own words for every frequency"])
        if "direct sampling inside the range" in note:
            return attention("The driver said it was in HF direct sampling at frequencies the tuner should cover.",
                             "The driver's words did not match what this kind of radio normally says, so the sweep "
                             "could not trust its own reading.", ["Open the full log", send])
        if "outside the sweep" in note:
            return attention("The tuner still locked at the furthest frequency the sweep can reach, so an edge was "
                             "not found.",
                             "No RTL-SDR tuner locks that far: the driver is probably not reporting lock failures in "
                             "words this sweep recognises.", ["Open the full log", send])
        if "found no range" in note:
            return attention("The sweep found no frequency where the tuner locks.",
                             "Either the radio is not an RTL-SDR the sweep knows, or the driver answered in words it "
                             "does not recognise.", ["Open the full log", "Run it again", send])
    if step == "warmup":
        if res.get("short") and res.get("warmup_min") is not None:
            return {"outcome": "done", "what": note, "why": "", "advice": []}
        return attention("The floor had not settled by the end of the run, so no warm-up time could be set.",
                         "The radio was probably not cold at the start, or something in the room changed during it.",
                         ["Unplug the radio (or leave it idle) for an hour", "Run the full measurement again from cold",
                          "Keep the room steady while it runs"])
    if step == "retune":
        return attention(f"The retune time came out as {note}.",
                         "The PC was busy during the timing, or the radio dropped samples.",
                         ["Close heavy programs", "Run it again"])
    if step == "adsb":
        return attention(f"The receive proof did not pass: {note.split('. ')[0]}.",
                         "The antenna path is not receiving, or few aircraft were overhead.",
                         ["Check the antenna is on and connected through the rig as drawn",
                          "Check nothing else is using the radio",
                          "Try again at a busier time before changing anything"])
    if "no recording" in note:
        return failed("The run wrote no recording.", (status.get("reason") or "It ended before recording.").strip(),
                      ["Open the full log", "Run it again"])
    if step == "floor":
        return attention("No settled floor could be read from the run.", "The radio was probably still warming up.",
                         ["Warm the radio for as long as the warm-up step says", "Run it again"])
    if step == "baseline":
        return attention("The baseline recorded too few sweeps to count.", "The run stopped early.",
                         ["Run it again and let it finish"])
    return attention(note or "The measurement did not give a usable result.", "The full log says what it did.",
                     ["Open the full log", "Run it again"])


def latest_job(doc: dict, nick: str, step: str) -> dict | None:
    js = [j for j in doc.get("jobs") or [] if j.get("radio") == nick and j.get("step") == step]
    return js[-1] if js else None


def run_view(station: Station, nick: str, step: str, status_fn, now_ts: float | None = None) -> dict | None:
    """What the guide shows for this step's newest run: progress while it runs; its end result after."""
    doc = load(station)
    job = latest_job(doc, nick, step)
    if job is None:
        return None
    base = {"job": job.get("id"), "log": job.get("log"), "short": bool(job.get("short")),
            "started": job.get("started")}
    if not job.get("done"):
        st = status_fn(job["log"])
        lines = [ln for ln in st.get("lines") or [] if ln.strip()][-8:]
        return dict(base, state="cancelling" if job.get("cancel_requested") else "running", lines=lines,
                    progress=progress_of(job, st, now_ts))
    if job.get("cancelled"):
        return dict(base, state="ended", outcome="cancelled", what="Cancelled by you: nothing was kept.", why="",
                    advice=["Run it again when you are ready"])
    r = doc["radios"].get(nick) or {}
    res = next((x for x in reversed((r.get("results") or {}).get(step) or [])
                if x.get("job") == job.get("id") or (x.get("log") and x.get("log") == job.get("log"))), None)
    if res is None:
        return dict(base, state="ended", outcome="failed", what="This run left no result.", why="",
                    advice=["Open the full log", "Run it again"])
    if "outcome" not in res:                                   # kept before relay 025
        res = dict(res, outcome="done" if res.get("ok") else "attention", what=res.get("note") or "",
                   advice=[] if res.get("ok") else ["Run it again"])
    return dict(base, state="ended", outcome=res["outcome"], what=res.get("what") or res.get("note") or "",
                why=res.get("why") or "", advice=res.get("advice") or [], note=res.get("note") or "",
                at=res.get("at"), error_lines=res.get("error_lines") or [])


def request_cancel(station: Station, nick: str, step: str) -> dict:
    """Mark this step's running job for cancelling and write its stop file when it has one. Returns the job; the
    service ends a job with no stop file, or one that has not stopped in time, by its own process tree (rule 1)."""
    doc = load(station)
    job = next((j for j in reversed(doc.get("jobs") or []) if j.get("radio") == nick and j.get("step") == step
                and not j.get("done")), None)
    if job is None:
        raise ValueError("Nothing is running on this step.")
    job["cancel_requested"] = job.get("cancel_requested") or now()
    if job.get("stop_file"):
        try:
            Path(job["stop_file"]).write_text("cancel\n", encoding="utf-8")
        except OSError:
            pass
    _save(station.root, doc)
    return job
