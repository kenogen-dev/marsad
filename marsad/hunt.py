"""Hunt mode: find which bands a device raises, then track one line by ear.

Walking a near-field probe around a room while watching a waterfall does not work:
the eyes are on the screen and the hand is somewhere else. Hunt mode splits the
job in two.

1. Find candidates. Two short `wide` probe runs through the ordinary probe path,
   one with the probe in open air and one with it at the device. This module
   subtracts them bin by bin and pools the differences into blocks (10 MHz by
   default), ranked by how much the strongest bin in each block rose. Anything
   present in both runs - the SDR's own clock harmonics, the hop-centre DC
   spikes, the room's broadcast signals - cancels in the difference; what is
   left is what the device adds.

2. Track live. The page starts an ordinary `marsad run` on a tile around the
   chosen candidate (the 5 MHz hunt tile at -i 1, or a 100 MHz tile at -i 3)
   and turns the level of the strongest line in it into a tone, so the probe can
   be moved by ear. That half lives in the web page and in probe.compose; this
   module only supplies the candidates and the list of reports to choose from.

Why the difference uses `median_db` (median over sweeps) and not `max_db`: a
click during one hop lifts that whole hop's max-hold at once (the hop-wide
impulse finding of 2026-10-15), but a one-sweep event does not move a 10-sweep
median. A device's emission is steady while the probe is on it, so it survives
the median, and the impulses do not.

Stdlib only, like the rest of the recording path: the web server imports this.
"""
from __future__ import annotations

import csv
import json
import math
import re
from pathlib import Path

from .archive import median
from .config import Station
from .rtlpower import fingerprint

BLOCK_MHZ = 10.0
BLOCK_CHOICES_MHZ = [5.0, 10.0, 20.0]
TOP = 12

# The two tiles the tracking half can start, by the probe catalogue's range id.
TRACK_RANGES = ["tile", "hunt"]

# Probe runs carry the device state and the probe position in their conditions
# text (probe.compose writes "..., <probe> held <where>, device state <state>, ...").
STATE_RE = re.compile(r"device state ([^,;]+)")
HELD_RE = re.compile(r"probe(?: [AB])? held ([^,;]+)")
TARGET_RE = re.compile(r"near-field probe survey: (.+?), (?:large|medium|small|extra-small|E-field)")


# ---------------------------------------------------------------- reading reports
def load_bins(report_dir: Path) -> dict:
    """bins.csv as {kHz: (floor_db, median_db, max_db)}. Non-finite readings are
    skipped, so a masked or saturated bin simply has no entry."""
    out: dict = {}
    path = Path(report_dir) / "bins.csv"
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            try:
                f = float(r["freq_mhz"])
                vals = (float(r["floor_db"]), float(r["median_db"]), float(r["max_db"]))
            except (KeyError, TypeError, ValueError):
                continue
            if not all(math.isfinite(v) for v in vals):
                continue
            out[int(round(f * 1e3))] = vals
    return out


def report_info(station: Station, name: str) -> dict:
    """What one report is, for the page's picker: when, tag, target, fingerprint."""
    rep = station.reports / name
    side = station.surveys / f"{name}.json"
    info = {"name": name, "has_bins": (rep / "bins.csv").exists(), "started": None,
            "preset": None, "tag": None, "target": None, "probe_run": False,
            "fingerprint": None, "conditions": "", "chain": "", "state": None, "held": None}
    try:
        sc = json.loads(side.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return info
    rp = sc.get("rtl_power", {})
    info["started"] = sc.get("started_local")
    info["preset"] = sc.get("preset")
    info["conditions"] = sc.get("conditions", "") or ""
    info["chain"] = (sc.get("chain") or {}).get("chain_order", "") or ""
    try:
        info["fingerprint"] = fingerprint(sc, rp.get("bin_khz_actual"))
    except (KeyError, TypeError, ValueError):
        pass
    parts = name.split("_")
    if len(parts) >= 5:
        info["tag"] = "_".join(parts[3:-1])
    m = TARGET_RE.search(info["conditions"])
    if m:
        info["target"] = m.group(1)
        info["probe_run"] = True
    m = STATE_RE.search(info["conditions"])
    if m:
        info["state"] = m.group(1).strip()
    m = HELD_RE.search(info["conditions"])
    if m:
        info["held"] = m.group(1).strip()
    info["background"] = bool(info["target"] and info["target"].lower().startswith("background"))
    return info


def report_list(station: Station) -> list:
    """Every report with a bins.csv and a sidecar, newest first, probe runs flagged.
    The page offers all of them: a hand-run `wide` survey of the room is a fair
    background too, as long as its fingerprint matches the device run."""
    out = []
    if not station.reports.exists():
        return out
    for d in station.reports.iterdir():
        if not (d / "bins.csv").exists() or not (station.surveys / f"{d.name}.json").exists():
            continue
        out.append(report_info(station, d.name))
    out.sort(key=lambda i: i["started"] or "", reverse=True)
    return out


# ---------------------------------------------------------------- the ranking
def spur_tag(f_mhz: float, tol_khz: float, families) -> str | None:
    """The first spur family a frequency lands on, within tol_khz, or None.

    A sub-MHz comb (the 600 kHz family) has a line every ~7 bins at this bin width,
    so about 7% of arbitrary frequencies land on one by chance: the tag is kept,
    marked weak, as CLAUDE.md asks."""
    for base, label in families:
        m = round(f_mhz / base)
        if m >= 1 and abs(f_mhz - m * base) * 1e3 <= tol_khz:
            return f"{label} (x{m})" + (", weak evidence" if base < 1.0 else "")
    return None


def rank(bg: dict, dev: dict, block_mhz: float = BLOCK_MHZ, top: int = TOP,
         families=(), gaps=(), min_common: float = 0.5) -> dict:
    """Blocks of the span ranked by how much the device run rose over the background.

    bg and dev are load_bins() dicts on the same frequency grid. For every bin in
    both, rise = device median_db - background median_db. Per block: the largest
    rise and where it is (`peak_mhz`, `peak_rise_db`), the median rise over the
    block (`floor_rise_db`, what a broadband emitter does), and the device's own
    level at the peak. Ranked by peak rise, which is what a tone can follow.
    """
    common = sorted(set(bg) & set(dev))
    need = min_common * min(len(bg), len(dev))
    if len(common) < max(10, need):
        raise ValueError(f"the two runs share only {len(common)} of their bins: they are not on "
                         "the same frequency grid, so their levels cannot be subtracted. Use two "
                         "runs with the same range and bin width (the same fingerprint).")
    step_khz = median([b - a for a, b in zip(common, common[1:])]) if len(common) > 1 else 0.0
    tol_khz = max(1.5 * step_khz, 10.0)
    blocks: dict = {}
    for k in common:
        f = k / 1e3
        if any(lo <= f < hi for lo, hi in gaps):
            continue
        rise = dev[k][1] - bg[k][1]
        blocks.setdefault(int(f // block_mhz), []).append((rise, k))
    out = []
    for b, items in blocks.items():
        rises = [r for r, _ in items]
        peak_rise, k = max(items)
        f = k / 1e3
        out.append({
            "lo_mhz": round(b * block_mhz, 3), "hi_mhz": round((b + 1) * block_mhz, 3),
            "n_bins": len(items),
            "peak_mhz": round(f, 4),
            "peak_rise_db": round(peak_rise, 2),
            "floor_rise_db": round(median(rises), 2),
            "device_db": round(dev[k][1], 2), "background_db": round(bg[k][1], 2),
            "device_max_db": round(dev[k][2], 2),
            "tag": spur_tag(f, tol_khz, families),
        })
    out.sort(key=lambda c: -c["peak_rise_db"])
    for i, c in enumerate(out, 1):
        c["rank"] = i
    return {"candidates": out[:top], "blocks": len(out), "n_bins_common": len(common),
            "step_khz": round(step_khz, 4), "block_mhz": block_mhz}


def candidates(station: Station, background: str, device: str, block_mhz: float = BLOCK_MHZ,
               top: int = TOP) -> dict:
    """The ranked list for two report names, refusing a pair whose fingerprints differ."""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", background or "") or not re.fullmatch(r"[A-Za-z0-9._-]+", device or ""):
        raise ValueError("pick a background run and a device run")
    if background == device:
        raise ValueError("the background and the device run are the same report")
    if block_mhz <= 0:
        raise ValueError("block width must be more than 0 MHz")
    infos = {}
    for role, name in (("background", background), ("device", device)):
        if not (station.reports / name / "bins.csv").exists():
            raise ValueError(f"no bins.csv for the {role} run {name!r} under reports/")
        infos[role] = report_info(station, name)
    fa, fb = infos["background"]["fingerprint"], infos["device"]["fingerprint"]
    if fa and fb and fa != fb:
        raise ValueError("the two runs have different fingerprints and cannot be subtracted:\n"
                         f"  background  {fa}\n  device      {fb}")
    bg = load_bins(station.reports / background)
    dev = load_bins(station.reports / device)
    receiver = None
    try:
        sc = json.loads((station.surveys / f"{device}.json").read_text(encoding="utf-8"))
        receiver = sc.get("receiver")
    except (OSError, ValueError):
        pass
    gaps = station.receiver_gaps().get(receiver or "", [])
    r = rank(bg, dev, block_mhz, top, station.spur_families, gaps)
    r.update({"background": infos["background"], "device": infos["device"],
              "fingerprint": fa or fb, "receiver": receiver})
    return r
