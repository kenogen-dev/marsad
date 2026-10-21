"""Near-field probe surveys from the web app: the back end of the Probe tab.

The room holds a dozen suspects (PC, monitors, chargers, the SDR's own box) and
the owner has a kit of near-field probes. A probe run is an ordinary `marsad run`
with three things composed for it - the tag, the chain and the conditions - so
its sidecar, scan_build.md, report and archive entry are exactly what a
command-line run produces. This module holds the catalogue of targets, probes
and ranges (one place to extend them), builds the command, runs it as a child
process, stops it the way Ctrl-C would, and appends one row per finished run to
ROOM_RF.md, the human-readable map of the room.

Stdlib only, like the rest of the recording path.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import re
import subprocess
import threading
import time
from collections import Counter
from pathlib import Path

from . import host
from .config import Station, source_root
from .rtlpower import (CROP, FIR, TUNER_GAINS, find_tool, fmt_hms, parse_freq_spec,
                       scan_geometry)

# ---------------------------------------------------------------- the catalogue
# Everything the page offers is listed here. station.json can override any of
# these under a "probe" key with the same names, so a new target or probe is one
# line in either place.

TARGETS = [
    "background (probe in open air)",
    "PC case",
    "PC power supply",
    "monitor 1",
    "monitor 2",
    "USB hub",
    "USB isolator",
    "TV",
    "printer",
    "LED lamp",
    "phone charger",
    "laptop charger",
    "router",
    "box fan",
    "cordless phone base",
    "SDR enclosure",
]

# id -> (label on the page, how the chain string names it)
PROBES = {
    "large-loop": ("large loop", "large H-field loop probe"),
    "medium-loop": ("medium loop", "medium H-field loop probe"),
    "small-loop": ("small loop", "small H-field loop probe"),
    "xs-loop": ("extra-small loop", "extra-small H-field loop probe"),
    "stub-a": ("stub A", "E-field stub probe A"),
    "stub-b": ("stub B", "E-field stub probe B"),
}

# The probe chain after the probe itself, per receiver. The DC block is not optional
# on the XTR: its bias-tee is always on and a shorted loop straight onto its SMA
# would short it. The v5 has no bias-tee, so it cannot power the LaNA and the probe
# goes to the cage without an amplifier. The page shows the tail and lets it be
# corrected before a run, so what is recorded is what was built.
PROBE_CHAIN_TAIL = {
    "xtr": (
        "SMA adapter > SMA DC block > 8 ft coax > Flamingo+ FM notch > LaNA (at the window, "
        "powered by the XTR bias-tee through "
        "the coax) > 3 ft RG316 coax > SMA bulkhead > 6 in SMA jumper (S-bend, no ferrite) > "
        "XTR (in aluminium enclosure, plugged direct into USB bulkhead, no internal USB cable) > "
        "USB bulkhead > 1 ft USB3 M:M > 16 ft USB3 > USB isolator > PC"
    ),
    "v5": (
        "SMA adapter > SMA DC block > 8 ft coax > cage SMA bulkhead > v5 (in copper cage, no jumper, "
        "no LNA: the v5 has no bias-tee to power the LaNA) > 6 in USB extension (white, no "
        "ferrites) > USB bulkhead > USB cable > PC"
    ),
}

# Range choices. "wide" is the station's overview preset; "hunt" is a 5 MHz tile
# around one typed frequency, for walking the probe around while watching one line.
#
# Why wide runs at -i 60 and not the preset's 5: rtl_power covers 52-1700 MHz in
# 785 hops (2.8 MS/s with the outer 25% cropped is 2.1 MHz per hop), and at 47 ms
# of retuning per hop a sweep spends 36.9 s doing nothing else. At -i 30 that is 0%
# listening and rtl_power's own help says it misbehaves; 60 s is the first round
# tier above the floor, with 39% of each sweep actually listening.
RANGES = {
    "wide": {
        "label": "wide: 52-1700 MHz overview (g29, 60 s sweeps)",
        "preset": "wide",
        "integration": 60,
    },
    "hunt": {
        "label": "hunt: 5 MHz around one frequency (5 kHz bins, g42, 1 s sweeps)",
        "preset": "custom",
        "span_mhz": 5.0,
        "bin_khz": 5.0,
        "gain": 42.0,
        "integration": 1,
        "floor_window": 0.3,     # the 2 MHz default is nearly half the span
    },
}

DURATIONS_MIN = [2, 5, 10]

# The attenuator kit, in dB. A pad goes between the notch's output and the LaNA's
# input when strong broadcast signals (UHF television at 560-590 MHz reached +4 dB
# through a loop) would otherwise drive the amplifier and the ADC into saturation.
ATTENUATORS_DB = [0, 3, 6, 10, 20]


def insert_pad(tail: str, db: int) -> str | None:
    """The chain tail with '> SMA adapter > N dB SMA attenuator' ahead of the LaNA,
    or None if the tail has no LaNA to put it in front of (the v5 chain).

    As built, the pad needs an adapter on the notch side; with no pad the notch
    goes straight onto the LaNA and the tail is returned unchanged."""
    if db <= 0:
        return tail
    parts = [p.strip() for p in tail.split(">")]
    for i, p in enumerate(parts):
        if p.startswith("LaNA"):
            parts[i:i] = ["SMA adapter", f"{db} dB SMA attenuator"]
            return " > ".join(parts)
    return None

LOG_NAME = "ROOM_RF.md"
ROOM_RF_HEADER = """# Room RF map

Near-field probe surveys of the room, one row per completed run, appended by the Probe tab
of the marsad web app. occupancy.db and reports/<survey>/ hold the full data; this file is
the human-readable map of which device emits what.

Levels are relative dB on rtl_power's scale, not calibrated. "Strongest line" is the
detection with the highest max-hold SNR in peaks.csv. "Tagged families" counts detections
carrying each spur-family tag; a lone 600 kHz comb tag is weak evidence (CLAUDE.md) and the
spacing between neighbouring lines should be measured before it is believed.

| When | Target | Probe | Held | Range | Duration | Survey | Detections | Strongest line | Tagged families |
|---|---|---|---|---|---|---|---|---|---|
"""


def chain_tails(station: Station) -> dict:
    """Chain after the probe, by receiver id. station.json's probe.chain_tail wins."""
    over = station.data.get("probe", {}) if isinstance(station.data.get("probe"), dict) else {}
    tails = dict(PROBE_CHAIN_TAIL)
    user = over.get("chain_tail")
    if isinstance(user, dict):
        tails.update(user)
    elif isinstance(user, str):
        tails["xtr"] = user
    for rx in station.receivers:
        tails.setdefault(rx, f"SMA adapter > SMA DC block > coax > {rx} > USB > PC")
    return tails


def catalog(station: Station) -> dict:
    """The catalogue as the page sees it, with station.json's `probe` overrides applied."""
    over = station.data.get("probe", {}) if isinstance(station.data.get("probe"), dict) else {}
    probes = over.get("probes") or {k: list(v) for k, v in PROBES.items()}
    tails = chain_tails(station)
    receivers = [{"id": rx, "label": r.get("label", rx), "tuner": (r.get("tuner") or "").lower(),
                  "device": int(r.get("device", 0)), "chain_tail": tails[rx]}
                 for rx, r in station.receivers.items()]
    return {
        "targets": list(over.get("targets") or TARGETS),
        "probes": [{"id": k, "label": v[0], "desc": v[1]} for k, v in probes.items()],
        "ranges": [{"id": k, "label": v["label"]} for k, v in RANGES.items()],
        "receivers": receivers,
        "durations_min": list(over.get("durations_min") or DURATIONS_MIN),
        "attenuators_db": list(over.get("attenuators_db") or ATTENUATORS_DB),
        "chain_tail": tails,
        "log_file": str(station.root / LOG_NAME),
    }


# ---------------------------------------------------------------- which dongle is where
# rtl_test / rtl_power number the dongles by USB enumeration order, which changes when
# one is unplugged (station.json says the XTR is device 1, but with the v5 unplugged it
# is device 0). So the page asks each index what it is instead of trusting a number.
TUNER_WORDS = {"e4000": "E4000", "r820t": "R820T", "r860": "R860"}


def parse_eeprom(text: str) -> dict:
    """The useful fields of one `rtl_eeprom -d N` report, or an `error`."""
    out = {"product": "", "serial": "", "tuner": "", "count": 0, "error": None}
    m = re.search(r"Found (\d+) device", text)
    if m:
        out["count"] = int(m.group(1))
    if "No supported devices found" in text:
        out["error"] = "no RTL-SDR devices found"
        return out
    if "Failed to open" in text or "usb_open error" in text:
        out["error"] = "could not open (no device at that index, or another program holds it)"
        return out
    for line in text.splitlines():
        if line.startswith("Product:"):
            out["product"] = line.split(":", 1)[1].strip()
        elif line.startswith("Serial number:"):
            out["serial"] = line.split(":", 1)[1].strip()
        elif line.startswith("Found") and "tuner" in line:
            t = line.replace("Found", "").replace("tuner", "").strip()
            out["tuner"] = t
    return out


def match_receiver(station: Station, product: str, tuner_text: str) -> str | None:
    """Which station receiver a dongle is, from its tuner chip and product name."""
    tuner_text = tuner_text.upper()
    by_tuner = [rx for rx, r in station.receivers.items()
                if TUNER_WORDS.get((r.get("tuner") or "").lower(), "").upper() in tuner_text
                and tuner_text]
    if len(by_tuner) == 1:
        return by_tuner[0]
    cands = by_tuner or list(station.receivers)
    p = product.lower()
    for rx in cands:
        label = (station.receivers[rx].get("label") or "").lower()
        if rx.lower() in p or any(w in p for w in label.split() if len(w) > 2 and w in p):
            return rx
    return None


def detect_receivers(station: Station, max_index: int = 4) -> dict:
    """Ask each USB index what dongle it holds. Read-only; ~1.5 s per device.

    Must not run while a recording holds the SDR: the open would fail and be
    reported as "no device", which is the wrong answer.
    """
    eeprom = find_tool("rtl_eeprom", station.tool_dirs())
    if eeprom is None:
        return {"found": [], "error": "rtl_eeprom not found in rtl-sdr/ or on PATH"}
    found = []
    count = None
    for d in range(max_index):
        if count is not None and d >= count:
            break
        try:
            r = subprocess.run([str(eeprom), "-d", str(d)], capture_output=True, text=True,
                               timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            return {"found": found, "error": f"could not run rtl_eeprom: {exc}"}
        info = parse_eeprom((r.stdout or "") + (r.stderr or ""))
        if count is None:
            count = info["count"]
        if info["error"]:
            if d == 0 and count == 0:
                return {"found": [], "error": info["error"]}
            continue
        found.append({"device": d, "product": info["product"], "serial": info["serial"],
                      "tuner": info["tuner"],
                      "receiver": match_receiver(station, info["product"], info["tuner"])})
    return {"found": found, "error": None if found else "no RTL-SDR devices found"}


def slug(text: str, max_len: int = 40) -> str:
    """'PC power supply' -> 'pc-power-supply': safe inside a survey filename."""
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:max_len].rstrip("-") or "x"


# ---------------------------------------------------------------- the hunt tile
def hunt_spec(centre_mhz: float, span_mhz: float = 5.0, bin_khz: float = 5.0) -> dict:
    """The -f spec for a hunt tile, with the target kept off every hop centre.

    rtl_power puts the E4000's zero-IF DC spike at the centre of each hop, and a
    symmetric span lands one of those centres exactly on the target (the first
    burst23 run found this at 1265 MHz). The tile is shifted so the target sits a
    quarter hop above a hop centre: as far from the DC spike as from the hop edge,
    where the anti-alias roll-off dips.
    """
    lo = centre_mhz - span_mhz / 2
    g = scan_geometry(lo, lo + span_mhz, bin_khz, 1, 60)
    hop = g["hop_mhz"]
    nearest = min(g["hop_centres_mhz"], key=lambda c: abs(c - centre_mhz))
    lo = round(lo + (centre_mhz - hop / 4) - nearest, 2)      # 10 kHz grid reads cleanly
    hi = round(lo + span_mhz, 2)
    g = scan_geometry(lo, hi, bin_khz, 1, 60)
    return {
        "spec": f"{lo:g}M:{hi:g}M:{bin_khz:g}k",
        "lo": lo, "hi": hi,
        "hop_mhz": g["hop_mhz"],
        "hop_centres_mhz": [round(c, 4) for c in g["hop_centres_mhz"]],
        "clearance_mhz": round(min(abs(c - centre_mhz) for c in g["hop_centres_mhz"]), 4),
    }


# ---------------------------------------------------------------- composing a run
def compose(station: Station, form: dict) -> dict:
    """Turn the page's form into the `marsad run` arguments, plus what they will do.

    Raises ValueError with a plain-language message for anything the form got
    wrong, so the page can show it instead of starting a doomed run.
    """
    cat = catalog(station)
    target = str(form.get("target") or "").strip()
    if target == "other":
        target = str(form.get("target_other") or "").strip()
    if not target:
        raise ValueError("pick a target, or type one under 'other'")
    probes = {p["id"]: p for p in cat["probes"]}
    probe = probes.get(str(form.get("probe") or ""))
    if probe is None:
        raise ValueError("pick a probe")
    where = str(form.get("where") or "").strip()
    try:
        minutes = float(form.get("minutes") or 0)
    except (TypeError, ValueError):
        raise ValueError("duration must be a number of minutes")
    if minutes <= 0:
        raise ValueError("duration must be more than 0 minutes")
    rng_id = str(form.get("range") or "wide")
    rng = RANGES.get(rng_id)
    if rng is None:
        raise ValueError(f"unknown range {rng_id!r}")
    try:
        device = int(form.get("device") or 0)
    except (TypeError, ValueError):
        raise ValueError("device index must be a whole number")
    notes = str(form.get("notes") or "").strip()

    preset = station.presets.get(rng["preset"])
    if preset is None:
        raise ValueError(f"station.json has no {rng['preset']!r} preset")
    receiver = str(form.get("receiver") or preset["receiver"])
    if receiver not in station.receivers:
        raise ValueError(f"unknown receiver {receiver!r}; station.json knows "
                         + ", ".join(station.receivers))
    gain = float(rng.get("gain", preset["gain"]))
    integration = int(rng.get("integration", preset["integration"]))
    warnings: list[str] = []
    extra: list[str] = ["--receiver", receiver]
    # The ranges are written for the XTR's E4000 ladder; on another tuner snap the
    # gain to its nearest real step, which is what librtlsdr would do silently.
    valid = TUNER_GAINS.get(station.tuner_of(receiver) or "")
    if valid and gain not in valid:
        nearest = min(valid, key=lambda g: abs(g - gain))
        warnings.append(f"gain {gain:g} dB is not a step on the {receiver}'s tuner; "
                        f"using the nearest, {nearest:g} dB")
        gain = nearest
    if gain != float(preset["gain"]) and rng_id != "hunt":
        extra += ["--gain", f"{gain:g}"]
    hunt = None
    if rng_id == "hunt":
        try:
            centre = float(form.get("centre") or 0)
        except (TypeError, ValueError):
            raise ValueError("centre frequency must be a number, in MHz")
        if centre <= 0:
            raise ValueError("type the centre frequency of the hunt tile, in MHz")
        hunt = hunt_spec(centre, rng["span_mhz"], rng["bin_khz"])
        freq = hunt["spec"]
        rx = station.receivers.get(receiver, {})
        r_lo, r_hi = rx.get("range_mhz", [0, 1e9])
        if not (r_lo <= hunt["lo"] and hunt["hi"] <= r_hi):
            warnings.append(f"{hunt['lo']:g}-{hunt['hi']:g} MHz is outside the {receiver} "
                            f"tuning range ({r_lo:g}-{r_hi:g})")
        for g_lo, g_hi in rx.get("gaps_mhz", []):
            if hunt["lo"] < g_hi and hunt["hi"] > g_lo:
                warnings.append(f"the tile overlaps the {receiver} PLL gap "
                                f"({g_lo:g}-{g_hi:g} MHz): rtl_power cannot tune there")
        extra += ["--freq", freq, "--gain", f"{gain:g}", "--integration", str(integration),
                  "--floor-window", f"{rng['floor_window']:g}"]
        range_label = f"hunt {centre:g} MHz ({freq}, g{gain:g}, i{integration})"
    else:
        freq = preset["freq"]
        extra += ["--integration", str(integration)]
        lo_, hi_, _ = parse_freq_spec(freq)
        range_label = f"wide {lo_:g}-{hi_:g} MHz (g{gain:g}, i{integration})"
    if device:
        extra += ["--device", str(device)]

    lo, hi, bin_khz = parse_freq_spec(freq)
    dur_s = int(round(minutes * 60))
    dur_text = f"{minutes:g}m"
    geom = scan_geometry(lo, hi, bin_khz, integration, dur_s, CROP,
                         retune_ms_per_hop=station.retune_ms_per_hop)
    if integration < geom["retune_s_per_sweep"]:
        warnings.append(f"-i {integration} is under the {geom['retune_s_per_sweep']:.1f} s "
                        f"retune floor for {geom['hops']} hops; rtl_power misbehaves there")
    if geom["sweeps"] < 3:
        warnings.append(f"only {geom['sweeps']} sweep(s) at -i {integration}: the floor "
                        "percentile and occupancy will be crude")

    # Where the probe was held is part of the measurement: a loop against a PSU
    # grille and one a hand's width away are different runs. Only the open-air
    # background has a sensible default.
    if where:
        held = where
    elif target.lower().startswith("background"):
        held = "in open air"
    else:
        raise ValueError("say where the probe is held (for example 'against the PSU fan grille')")
    tag = f"probe-{slug(target)}-{slug(probe['id'], 20)}"
    tail = str(form.get("chain_tail") or "").strip() or cat["chain_tail"][receiver]
    try:
        pad_db = int(form.get("attenuator") or 0)
    except (TypeError, ValueError):
        raise ValueError("attenuator must be one of " + ", ".join(map(str, cat["attenuators_db"])))
    if pad_db not in cat["attenuators_db"]:
        raise ValueError(f"the kit has no {pad_db} dB pad; choose from "
                         + ", ".join(map(str, cat["attenuators_db"])))
    padded = insert_pad(tail, pad_db)
    if padded is None:
        warnings.append(f"the {receiver} chain has no LaNA to put a {pad_db} dB pad ahead of; "
                        "the pad was NOT added to the chain")
        pad_db = 0
        padded = tail
    pad_text = f"{pad_db} dB pad ahead of the LaNA" if pad_db else "no pad"
    chain = f"{probe['desc']}, held {held} > {padded}"
    conditions = (f"near-field probe survey: {target}, {probe['desc']} held {held}, "
                  f"{range_label}, {dur_text}, {pad_text}")
    if pad_db:
        range_label += f", {pad_db} dB pad"      # the room-map row's Range cell carries it
    if notes:
        conditions += f"; {notes}"

    args = [rng["preset"], dur_text, "--tag", tag, "--chain", chain,
            "--conditions", conditions, *extra,
            "--non-interactive", "--no-live", "--keep-defaults"]
    rtl = ["rtl_power", "-f", freq, "-g", f"{gain:g}", "-i", str(integration),
           "-c", str(CROP), "-F", str(FIR), "-e", dur_text]
    if device:
        rtl += ["-d", str(device)]
    return {
        "args": args,
        "command_line": subprocess.list2cmdline(["marsad", "run", *args]),
        "rtl_power_line": " ".join(rtl) + " surveys/<name>.csv",
        "tag": tag, "chain": chain, "conditions": conditions,
        "target": target, "probe": probe["label"], "probe_id": probe["id"], "held": held,
        "range": rng_id, "range_label": range_label,
        "preset": rng["preset"], "receiver": receiver, "freq": freq,
        "gain": gain, "integration": integration, "device": device, "attenuator_db": pad_db,
        "minutes": minutes, "duration_text": dur_text, "duration_s": dur_s,
        "hunt": hunt,
        "geometry": {k: (round(v, 4) if isinstance(v, float) else v)
                     for k, v in geom.items() if k != "hop_centres_mhz"},
        "warnings": warnings,
    }


# ---------------------------------------------------------------- the room log
def summarise_report(report_dir: Path) -> dict:
    """What one finished report says, in the few numbers the room map carries."""
    peaks = Path(report_dir) / "peaks.csv"
    out = {"detections": 0, "untagged": 0, "strongest": None, "families": {}}
    if not peaks.exists():
        return out
    fams: Counter = Counter()
    with open(peaks, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    out["detections"] = len(rows)
    for r in rows:
        tags = [t.strip() for t in (r.get("spur_tags") or "").split(";") if t.strip()]
        if not tags:
            out["untagged"] += 1
        for t in tags:
            if t.startswith("hop-wide"):
                fams["hop-wide impulse"] += 1
            elif t.startswith("hop centre"):
                fams["hop centre"] += 1
            else:
                fams[t.split(" (x")[0]] += 1
    if rows:
        # peaks.csv is sorted by max-hold SNR, so the first row is the strongest line.
        # Skip the receiver's own artefacts (hop-centre DC spikes, hop-wide impulses):
        # a spur-family tag stays eligible, since a device's comb is what a probe hunts.
        def artefact(r):
            return any(t.strip().startswith(("hop centre", "hop-wide"))
                       for t in (r.get("spur_tags") or "").split(";"))
        r = next((r for r in rows if not artefact(r)), rows[0])
        out["strongest"] = {"f": float(r["freq_mhz"]), "max_db": float(r["max_db"]),
                            "snr": float(r["snr_max_db"]), "occ": float(r["occupancy_pct"]),
                            "tags": r.get("spur_tags") or "",
                            "artefact_only": artefact(r)}
    out["families"] = dict(fams)
    return out


def _cell(text) -> str:
    return str(text if text is not None else "").replace("|", "\\|").replace("\n", " ")


def log_row(when: dt.datetime, plan: dict, stem: str, summary: dict,
            actual_s: float | None, interrupted: bool) -> str:
    dur = plan["duration_text"]
    if interrupted and actual_s:
        dur += f" (stopped at {actual_s / 60:.1f} min)"
    s = summary.get("strongest")
    strongest = (f"{s['f']:.4f} MHz, {s['max_db']:g} dB ({s['snr']:g} dB SNR, "
                 f"{s['occ']:g}% on)" + (f" [{s['tags']}]" if s["tags"] else "")) if s else "none"
    fams = "; ".join(f"{k} x{v}" for k, v in sorted(summary.get("families", {}).items(),
                                                      key=lambda kv: -kv[1])) or "none"
    det = f"{summary['detections']} ({summary['untagged']} untagged)"
    cells = [f"{when:%Y-%m-%d %H:%M}", plan["target"], plan["probe"], plan["held"],
             plan["range_label"], dur, stem, det, strongest, fams]
    return "| " + " | ".join(_cell(c) for c in cells) + " |"


def append_log(station: Station, row: str) -> Path:
    """Append one row to ROOM_RF.md, writing the header the first time."""
    path = station.root / LOG_NAME
    new = not path.exists() or path.stat().st_size == 0
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        if new:
            fh.write(ROOM_RF_HEADER)
        fh.write(row + "\n")
    return path


def read_log(station: Station) -> str:
    path = station.root / LOG_NAME
    return path.read_text(encoding="utf-8") if path.exists() else ""


# ---------------------------------------------------------------- running one
class ProbeRunner:
    """One probe run at a time, as a `marsad run` child process.

    The child's console output goes to reports/probe_run.log (a file, not a pipe,
    so the child survives the server being closed and still finishes its analysis).
    Stop is a flag file the child polls; see record.run_with_progress.
    """

    STOP_FLAG = "probe_stop.flag"
    LOG = "probe_run.log"

    def __init__(self, station: Station):
        self.station = station
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.job: dict | None = None

    @property
    def log_path(self) -> Path:
        return self.station.reports / self.LOG

    @property
    def stop_path(self) -> Path:
        return self.station.reports / self.STOP_FLAG

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, form: dict) -> dict:
        with self.lock:
            if self.running():
                raise RuntimeError("a probe run is already recording; stop it first")
            plan = compose(self.station, form)       # ValueError on a bad form
            self.station.reports.mkdir(parents=True, exist_ok=True)
            self.stop_path.unlink(missing_ok=True)
            cmd = host.self_command("run", "--station", str(self.station.root), *plan["args"],
                                    "--stop-file", str(self.stop_path),
                                    prefer_venv=self.station.venv_python)
            # Python block-buffers stdout when it is a file, which would hold the
            # child's "Recording until" and "Recorded" lines back until it exits and
            # leave the page stuck on "starting". Unbuffered, they land as printed.
            env = {**os.environ, "PYTHONUNBUFFERED": "1"}
            with open(self.log_path, "w", encoding="utf-8") as log:
                log.write("  " + subprocess.list2cmdline(cmd) + "\n\n")
                log.flush()
                try:
                    self.proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                                                 stdin=subprocess.DEVNULL, env=env,
                                                 cwd=source_root() or None)
                except (OSError, subprocess.SubprocessError) as exc:
                    raise RuntimeError(f"could not start marsad run: {exc}")
            # The Chain tab's current drawing is what this run is believed to use.
            from . import chain as chain_mod
            chain_mod.mark_used(self.station)
            self.job = {"plan": plan, "started": time.time(), "rec_t0": None,
                        "started_local": dt.datetime.now().isoformat(timespec="seconds"),
                        "stem": None, "stopped": False, "logged": False,
                        "summary": None, "row": None, "log_error": None}
            return self.status()

    def stop(self) -> dict:
        with self.lock:
            if not self.running() or self.job is None:
                raise RuntimeError("nothing is recording")
            if "Recorded " in self._log_text():
                raise RuntimeError("the recording has already finished; the analysis is running")
            self.job["stopped"] = True
            self.stop_path.write_text("stop\n", encoding="utf-8")
            return self.status()

    def shutdown(self) -> None:
        """Server closing: ask a live recording to stop, do not wait for it."""
        if self.running() and self.job is not None:
            try:
                self.stop()
            except RuntimeError:
                pass
            print(f"  A probe run is still finishing in the background; its log is "
                  f"{self.log_path}")

    def _log_text(self) -> str:
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def status(self) -> dict:
        proc, job = self.proc, self.job
        if job is None:
            return {"state": "idle"}
        text = self._log_text()
        plan = job["plan"]
        if job["stem"] is None:
            m = re.search(r"surveys/(survey_\S+)\.csv", text)
            if m:
                job["stem"] = m.group(1)
        if job["rec_t0"] is None and "Recording until" in text:
            job["rec_t0"] = time.time()
        alive = proc is not None and proc.poll() is None
        if alive:
            if "Recorded " in text or "Interrupted" in text:
                state = "analysing"
            elif job["stopped"]:
                state = "stopping"
            elif job["rec_t0"] is not None:
                state = "recording"
            else:
                state = "starting"
        else:
            report = (self.station.reports / job["stem"] / "report.md") if job["stem"] else None
            if report is not None and report.exists() and "Done. Report" in text:
                state = "done"
                self._finish(job)
            else:
                state = "failed"
        now = time.time()
        rec_elapsed = (now - job["rec_t0"]) if job["rec_t0"] else 0.0
        if state in ("done", "failed") and job.get("ended") is None:
            job["ended"] = now
        if job.get("ended"):
            rec_elapsed = min(rec_elapsed, job["ended"] - (job["rec_t0"] or job["ended"]))
        remaining = max(0.0, plan["duration_s"] - rec_elapsed) if state == "recording" else None
        lines = [ln for ln in text.splitlines() if ln.strip()]
        message = None
        if state == "failed":
            message = "\n".join(lines[-6:]) if lines else "the run produced no output"
        return {
            "state": state,
            "tag": plan["tag"], "target": plan["target"], "probe": plan["probe"],
            "held": plan["held"], "range_label": plan["range_label"],
            "started_local": job["started_local"],
            "duration_s": plan["duration_s"],
            "elapsed_s": round(rec_elapsed, 1),
            "remaining_s": None if remaining is None else round(remaining, 1),
            "elapsed": fmt_hms(rec_elapsed),
            "remaining": fmt_hms(remaining) if remaining is not None else None,
            "stem": job["stem"],
            "csv": f"{job['stem']}.csv" if job["stem"] else None,
            "report_url": f"/reports/{job['stem']}/report.md" if state == "done" else None,
            "report_dir": str(self.station.reports / job["stem"]) if job["stem"] else None,
            "exit_code": None if alive else proc.returncode,
            "summary": job["summary"],
            "row": job["row"],
            "log_error": job["log_error"],
            "message": message,
            "log_tail": "\n".join(lines[-12:]),
        }

    def _finish(self, job: dict) -> None:
        """Once per finished run: summarise the report and append the room-map row."""
        if job["logged"]:
            return
        job["logged"] = True
        stem = job["stem"]
        try:
            summary = summarise_report(self.station.reports / stem)
            actual = None
            interrupted = False
            try:
                sc = json.loads((self.station.surveys / f"{stem}.json").read_text(encoding="utf-8"))
                actual = sc.get("duration_actual_s")
                interrupted = bool(sc.get("interrupted"))
            except (OSError, ValueError):
                pass
            when = dt.datetime.fromisoformat(job["started_local"])
            row = log_row(when, job["plan"], stem, summary, actual, interrupted)
            append_log(self.station, row)
            job["summary"] = summary
            job["row"] = row
        except Exception as exc:  # noqa: BLE001 - the report exists; the map row is a bonus
            job["log_error"] = f"could not write the ROOM_RF.md row: {exc}"
