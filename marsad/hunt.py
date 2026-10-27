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


# ---------------------------------------------------------------- following one line
# The first shakedown (2026-10-26) showed two faults in "follow the strongest line":
# on a 100 MHz tile it hopped between unrelated carriers (855.6, 806.4, 864.0, 888.0,
# 891.0, 903.8 MHz: cellular and land-mobile coming in through the probe and coax),
# because whichever was a dB higher this sweep won; and a broadband emitter (the
# blade on a Bluetooth headphone case, a Wi-Fi bulb base) lifted the whole tile's
# median by 12-16 dB, so "line over the tile's floor" barely moved. Follower fixes
# the first with hysteresis and a lock, and reports the floor itself for the second.
SWITCH_DB = 6.0      # a challenger must be this much stronger than the followed line ...
SWITCH_N = 3         # ... for this many updates in a row before the follower moves
LOST_DB = 3.0        # the followed line has gone when it is under this over the floor ...
LOST_N = 3           # ... for this many updates; then the strongest line is taken again
DC_SKIP_KHZ = 25.0   # the E4000's DC spike skirt is about +/-20 kHz at 8 dB over the floor
FOLLOW_MODES = ("strongest", "typed", "floor")


class Follower:
    """Which line a hunt run is following, update by update.

    Fed the latest complete sweep at full resolution ({kHz: dB}) and the hop centres
    (Hz). Modes: `strongest` follows a line with hysteresis (or stays on a locked
    one), `typed` reads the typed centre frequency, `floor` reads the sweep's median
    level itself, which is what a broadband source raises. Every answer carries both
    numbers - the line over the floor, and the floor against the quietest it has been
    this run - so the page can show both whichever drives the tone.
    """

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self.cur_khz = None          # the line being followed
        self.chal_khz = None         # a stronger line waiting to take over
        self.chal_n = 0
        self.lost_n = 0
        self.quiet_db = None         # lowest sweep median seen this run
        self.updates = 0
        self.switches = 0

    @staticmethod
    def _near(latest: dict, keys: list, khz: float, tol: float):
        """The strongest bin within tol kHz of khz: (kHz, dB) or (None, None)."""
        best = (None, None)
        for k in keys:
            if abs(k - khz) <= tol:
                v = latest[k]
                if best[1] is None or v > best[1]:
                    best = (k, v)
        return best

    def update(self, latest: dict, hop_centres_hz=(), step_khz: float | None = None,
               mode: str = "strongest", centre_mhz: float | None = None,
               lock_mhz: float | None = None) -> dict | None:
        keys = sorted(k for k, v in latest.items() if v == v)        # finite bins only
        if not keys:
            return None
        if step_khz is None:
            step_khz = median([b - a for a, b in zip(keys, keys[1:])]) if len(keys) > 1 else 1.0
        tol = max(2.0 * step_khz, 15.0)                               # what counts as "the same line"
        floor = median([latest[k] for k in keys])
        self.updates += 1
        if self.quiet_db is None or floor < self.quiet_db:
            self.quiet_db = floor
        skip = max(1.5 * step_khz, DC_SKIP_KHZ)
        dc = [h / 1e3 for h in hop_centres_hz]
        usable = [k for k in keys if all(abs(k - c) > skip for c in dc)] or keys
        strongest = max(usable, key=lambda k: latest[k])
        locked = False
        switched = False
        if mode == "typed" and centre_mhz:
            k, v = self._near(latest, keys, centre_mhz * 1e3, tol)
            self.cur_khz = k
        elif lock_mhz:
            # Locked: stay on this line whatever else appears, following it if it drifts
            # within the tolerance, so a crystal warming up does not lose the lock.
            locked = True
            want = lock_mhz * 1e3
            k, v = self._near(latest, usable, want, tol)
            if k is not None and v - floor >= LOST_DB:
                self.cur_khz = k
            else:                           # faded: hold the lock, read the bin under it
                self.cur_khz = want
                k = min(keys, key=lambda x: abs(x - want))
                v = latest[k] if abs(k - want) <= tol else None
            self.chal_khz, self.chal_n, self.lost_n = None, 0, 0
        else:
            # Hysteresis: start on the strongest, then only move when another line has
            # been SWITCH_DB stronger for SWITCH_N updates in a row, or the followed one
            # has sunk into the floor for LOST_N updates.
            if self.cur_khz is None:
                self.cur_khz = strongest
            k, v = self._near(latest, usable, self.cur_khz, tol)
            if v is None or v - floor < LOST_DB:
                self.lost_n += 1
            else:
                self.lost_n = 0
                self.cur_khz = k            # track a small drift, never onto a noise bin
            far = abs(strongest - (self.cur_khz or 0)) > tol
            if far and (v is None or latest[strongest] >= v + SWITCH_DB):
                if self.chal_khz is not None and abs(strongest - self.chal_khz) <= tol:
                    self.chal_n += 1
                else:
                    self.chal_khz, self.chal_n = strongest, 1
            else:
                self.chal_khz, self.chal_n = None, 0
            if self.chal_n >= SWITCH_N or self.lost_n >= LOST_N:
                self.cur_khz = strongest
                self.chal_khz, self.chal_n, self.lost_n = None, 0, 0
                switched = True
                self.switches += 1
            k, v = self._near(latest, usable, self.cur_khz, tol)
        over = None if v is None else v - floor
        rise = floor - self.quiet_db
        value = rise if mode == "floor" else over
        return {
            "mode": mode, "locked": locked, "switched": switched,
            "f_mhz": None if self.cur_khz is None else round(self.cur_khz / 1e3, 4),
            "level_db": None if v is None else round(v, 2),
            "over_db": None if over is None else round(over, 2),
            "floor_db": round(floor, 2),
            "quiet_db": round(self.quiet_db, 2),
            "floor_rise_db": round(rise, 2),
            "value_db": None if value is None else round(value, 2),
            "strongest_mhz": round(strongest / 1e3, 4),
            "strongest_over_db": round(latest[strongest] - floor, 2),
            "challenger_mhz": None if self.chal_khz is None else round(self.chal_khz / 1e3, 4),
            "challenger_n": self.chal_n,
            "switches": self.switches,
            "tol_khz": round(tol, 2),
        }


def spectrum_strip(latest: dict, hop_centres_hz=(), step_khz: float | None = None,
                   cols: int = 360) -> dict | None:
    """The latest sweep squeezed to `cols` points for the Hunt panel's mini spectrum:
    max per column (a narrow line must survive the squeeze), plus the stretches the
    follower never picks (around every hop centre's DC spike), in MHz."""
    keys = sorted(k for k, v in latest.items() if v == v)
    if len(keys) < 2:
        return None
    lo, hi = keys[0], keys[-1]
    span = max(hi - lo, 1e-9)
    row = [None] * cols
    for k in keys:
        c = min(cols - 1, int((k - lo) / span * cols))
        v = latest[k]
        if row[c] is None or v > row[c]:
            row[c] = round(v, 1)
    if step_khz is None:
        step_khz = median([b - a for a, b in zip(keys, keys[1:])])
    skip = max(1.5 * step_khz, DC_SKIP_KHZ)
    excl = [[round((h / 1e3 - skip) / 1e3, 4), round((h / 1e3 + skip) / 1e3, 4)]
            for h in hop_centres_hz if lo - skip <= h / 1e3 <= hi + skip]
    return {"lo_mhz": round(lo / 1e3, 4), "hi_mhz": round(hi / 1e3, 4), "db": row, "excluded_mhz": excl}
