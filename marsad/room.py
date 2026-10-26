"""The room's equipment catalogue: what each device adds, state by state.

A device is surveyed several times with the probe fixed in place (marked corners,
sub-millimetre), once per state: unplugged, off, idle, active. Hunt mode's
difference ranking (hunt.rank) answers "what does this run add over that one";
this module applies it to every state of one device against a background - its
own unplugged run by default, the device's absence with the probe exactly where
it was - and says the answer in a sentence:

    "printing adds 14.2 dB at 120.31 MHz over unplugged; 3 blocks rise 6 dB or more"

and ranks the devices against each other on their active state, which is the
"loudest devices in the room" list.

What is compared is the median over sweeps (`median_db`), as in hunt mode, so a
one-sweep click does not count as the device. Two runs are differenced only when
their fingerprints match; otherwise the pair is refused in a sentence and the
rest of the table still shows.

Stdlib only, like hunt.py: the web server imports this.
"""
from __future__ import annotations

from .config import Station
from . import hunt
from .probe import QUIET_STATES

LOUD_DB = 6.0            # a block "rises" when its strongest bin is up this much or more
NO_STATE = "(no state recorded)"


def device_runs(station: Station) -> dict:
    """Probe runs grouped by target, then by state, newest first within a state.
    Hunt tiles are left out: a 5 or 100 MHz tile cannot be compared with a wide run."""
    out: dict = {}
    for info in hunt.report_list(station):
        if not info.get("probe_run") or not info.get("target"):
            continue
        if (info.get("tag") or "").startswith("hunt-"):
            continue
        state = info.get("state") or NO_STATE
        out.setdefault(info["target"], {}).setdefault(state, []).append(info)
    return out


def targets(station: Station) -> list:
    """Each catalogued target with its states and run counts, for the page's picker."""
    runs = device_runs(station)
    return [{"target": t, "states": {s: len(v) for s, v in by_state.items()},
             "runs": sum(len(v) for v in by_state.values()),
             "background": t.lower().startswith("background")}
            for t, by_state in sorted(runs.items(), key=lambda kv: kv[0].lower())]


def _sentence(state: str, bg_label: str, r: dict) -> str:
    """The comparison in plain words, from one hunt.rank result over all blocks."""
    blocks = r["all"]
    if not blocks:
        return f"{state}: nothing to compare"
    top = blocks[0]
    loud = [b for b in blocks if b["peak_rise_db"] >= LOUD_DB]
    if top["peak_rise_db"] < LOUD_DB:
        return (f"{state} adds nothing over {bg_label} beyond {LOUD_DB:g} dB "
                f"(largest rise {top['peak_rise_db']:+.1f} dB at {top['peak_mhz']:.2f} MHz)")
    where = ", ".join(f"{b['peak_mhz']:.2f}" for b in loud[:5])
    more = f" and {len(loud) - 5} more" if len(loud) > 5 else ""
    return (f"{state} adds {top['peak_rise_db']:.1f} dB at {top['peak_mhz']:.2f} MHz over "
            f"{bg_label}; {len(loud)} block{'s' if len(loud) != 1 else ''} rise "
            f"{LOUD_DB:g} dB or more, at {where} MHz{more}")


def _difference(station: Station, bg: dict, run: dict, block_mhz: float, top: int) -> dict:
    """One run against the background, or a refusal in a sentence."""
    row = {"run": run["name"], "started": run["started"], "held": run.get("held"),
           "fingerprint": run.get("fingerprint")}
    if run["name"] == bg["name"]:
        row["is_background"] = True
        return row
    if bg.get("fingerprint") and run.get("fingerprint") and bg["fingerprint"] != run["fingerprint"]:
        row["refused"] = ("different fingerprint from the background, so the two cannot be "
                          f"subtracted: background {bg['fingerprint']}, this run {run['fingerprint']}")
        return row
    try:
        r = hunt.candidates(station, bg["name"], run["name"], block_mhz, top=10 ** 6)
    except ValueError as exc:
        row["refused"] = str(exc)
        return row
    blocks = r["candidates"]
    row.update({
        "all": blocks,
        "candidates": blocks[:top],
        "blocks": r["blocks"],
        "loud_blocks": sum(1 for b in blocks if b["peak_rise_db"] >= LOUD_DB),
        "strongest": blocks[0] if blocks else None,
        "same_position": (None if not (bg.get("held") and run.get("held"))
                          else bg["held"].lower() == run["held"].lower()),
    })
    return row


def pick_background(by_state: dict, background: str | None, station: Station) -> dict:
    """The named report, or the target's newest unplugged run."""
    if background:
        for runs in by_state.values():
            for r in runs:
                if r["name"] == background:
                    return r
        if not (station.reports / background / "bins.csv").exists():
            raise ValueError(f"no bins.csv for the background run {background!r} under reports/")
        return hunt.report_info(station, background)
    unplugged = by_state.get("unplugged")
    if not unplugged:
        raise ValueError("this target has no unplugged run to use as the background; "
                         "record one, or pick a background run")
    return unplugged[0]


def compare(station: Station, target: str, background: str | None = None,
            block_mhz: float = hunt.BLOCK_MHZ, top: int = 8) -> dict:
    """Every run of one target, grouped by state, each differenced against the background."""
    by_state = device_runs(station).get(target)
    if not by_state:
        raise ValueError(f"no probe runs of {target!r} with a report yet")
    if block_mhz <= 0:
        raise ValueError("block width must be more than 0 MHz")
    bg = pick_background(by_state, background, station)
    bg_label = bg.get("state") if bg.get("target") == target and bg.get("state") else \
        (bg.get("target") or bg["name"])
    order = {s: i for i, s in enumerate(["unplugged", "off", "idle", "active"])}
    states = []
    for state in sorted(by_state, key=lambda s: (order.get(s, 4), s)):
        rows = [_difference(station, bg, run, block_mhz, top) for run in by_state[state]]
        newest = next((r for r in rows if "all" in r), None)
        states.append({
            "state": state,
            "runs": [{k: v for k, v in r.items() if k != "all"} for r in rows],
            "sentence": _sentence(state, bg_label, newest) if newest else None,
        })
    return {"target": target, "background": bg, "background_label": bg_label,
            "block_mhz": block_mhz, "loud_db": LOUD_DB, "states": states}


def loudest(station: Station, block_mhz: float = hunt.BLOCK_MHZ) -> dict:
    """Targets ranked by what their active state adds over their own unplugged run.

    "Active" is any state that is not unplugged, off or idle: the page offers
    `active`, and free text such as "printing" counts too. Where a target has
    several active runs the loudest one stands for it. A target without an
    unplugged run and an active run of the same fingerprint is listed under
    `skipped` with the reason, so the catalogue shows what is still to record."""
    ranked, skipped = [], []
    for target, by_state in device_runs(station).items():
        if target.lower().startswith("background"):
            continue
        active = [(s, r) for s, runs in by_state.items()
                  if s not in QUIET_STATES and s != NO_STATE for r in runs]
        if not by_state.get("unplugged"):
            skipped.append({"target": target, "reason": "no unplugged run yet"})
            continue
        if not active:
            skipped.append({"target": target, "reason": "no active run yet"})
            continue
        best = None
        reasons = []
        for state, run in active:
            # the newest unplugged run on the same grid as this active run
            bg = next((u for u in by_state["unplugged"]
                       if not (u.get("fingerprint") and run.get("fingerprint"))
                       or u["fingerprint"] == run["fingerprint"]), None)
            if bg is None:
                reasons.append(f"{state}: no unplugged run with the same fingerprint")
                continue
            d = _difference(station, bg, run, block_mhz, 1)
            if "refused" in d:
                reasons.append(f"{state}: {d['refused']}")
                continue
            if d["strongest"] and (best is None or
                                   d["strongest"]["peak_rise_db"] > best["peak_rise_db"]):
                s = d["strongest"]
                best = {"target": target, "state": state, "run": run["name"],
                        "background": bg["name"], "peak_mhz": s["peak_mhz"],
                        "peak_rise_db": s["peak_rise_db"], "tag": s["tag"],
                        "loud_blocks": d["loud_blocks"], "blocks": d["blocks"],
                        "same_position": d["same_position"]}
        if best:
            ranked.append(best)
        else:
            skipped.append({"target": target, "reason": "; ".join(reasons) or "nothing comparable"})
    ranked.sort(key=lambda b: -b["peak_rise_db"])
    for i, b in enumerate(ranked, 1):
        b["rank"] = i
    return {"ranked": ranked, "skipped": sorted(skipped, key=lambda s: s["target"].lower()),
            "block_mhz": block_mhz, "loud_db": LOUD_DB}
