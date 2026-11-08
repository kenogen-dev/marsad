"""Rule 7: one program per radio, not one per station.

Which radio each running radio program holds, from marsad's own records: a recording still
being written names its radio in its sidecar, and a running `marsad watch` names its radio
(by serial) in its status file. Every SDR program process is then accounted for against those
records; one that no record explains (SDR++ opened by hand, rtl_tcp, a second rtl_power that
marsad did not start) cannot be placed on a radio, and a start on either radio is refused
while it runs, because a process name says nothing about which dongle it opened.

`check` is the one rule. `marsad run` (both engines, so also the old web page's starts and the
window's) asks it before recording; the window asks it to say which radio is free. Reads small
files and the process list only: no radio is opened."""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import host
from .config import Station

STILL_WRITING_S = 300.0      # a recording untouched this long is not being written any more
FFTW = "rtl_power_fftw"      # not in rtlpower.SDR_PROGRAMS; counted here so its runs are placed too
RTL_POWER_NAMES = ("rtl_power", "rtl_power_gainfix")


def recording_active(path: Path, now: float | None = None) -> bool:
    """Is this recording still being written? Its sidecar has no `finished_local` (the
    recorder stamps that when the program exits), and the file grew recently: within
    STILL_WRITING_S, or three of its write intervals for a slow scan (rtl_power writes
    once per -i, rtl_power_fftw once per pass). A run killed without warning never gets
    `finished_local`, so the clock is what retires it."""
    now = time.time() if now is None else now
    try:
        age = now - path.stat().st_mtime
    except OSError:
        return False
    every = 0.0
    try:
        d = json.loads(path.with_suffix(".json").read_text())
        if d.get("finished_local"):
            return False
        eng = d.get(d.get("engine") or "rtl_power") or {}
        every = float(eng.get("integration_s") or (d.get("geometry") or {}).get("sweep_s") or 0)
    except (OSError, ValueError, TypeError):
        pass
    return age <= max(STILL_WRITING_S, 3 * every)


def _word(rx) -> str:
    """As live.radio_word: an all-letter name in capitals, any other as written."""
    name = str(rx or "")
    return (name.upper() if name.isalpha() else name) or "a radio"


def _stem(program: str) -> str:
    p = str(program).lower()
    return p[:-4] if p.endswith(".exe") else p


def users(station: Station, programs: dict | None | bool = False, now: float | None = None) -> dict:
    """What holds a radio now. `known`: one entry per marsad recording or recorder, each
    {radio, serial, doing, program, what}. `unknown`: program names running that no record
    explains. `programs`: the process counts used (None when the process list could not be
    read). Pass `programs` to use given counts (tests); False reads the real list."""
    from . import capture as cap
    cfg = cap.settings(station)
    now = time.time() if now is None else now
    known = []
    if station.surveys.exists():
        files = list(station.surveys.glob("*.csv")) + list(station.surveys.glob("*.dat"))
        for f in sorted(files):
            if not recording_active(f, now):
                continue
            try:
                d = json.loads(f.with_suffix(".json").read_text())
            except (OSError, ValueError):
                d = {}
            rx = d.get("receiver")
            fftw = (d.get("engine") == FFTW) or f.suffix.lower() == ".dat"
            known.append({"radio": rx, "serial": cap.serial_of(cfg, rx) if rx else None, "doing": "surveying",
                          "program": FFTW if fftw else "rtl_power", "what": f"marsad run, {f.name}"})
    try:
        from . import catalogue
        recs = catalogue.live_recorders(station, cfg, now=now)
    except Exception:  # noqa: BLE001 - a broken status file must not hide the process check
        recs = []
    for r in recs:
        if not r.get("running"):
            continue
        job = r.get("job") or {}
        what = f"marsad watch {r.get('mode') or 'recorder'}"
        if isinstance(job.get("target_mhz"), (int, float)):
            what += f" on {job['target_mhz']:.4f} MHz"
        known.append({"radio": r.get("receiver"), "serial": str(r.get("serial") or "") or None,
                      "doing": "recording", "program": "rtl_sdr", "what": what})
    if programs is False:
        programs = host.sdr_program_counts((FFTW,))
    unknown: list[str] = []
    if programs is not None:
        budget = {"rtl_power": sum(k["program"] == "rtl_power" for k in known),
                  FFTW: sum(k["program"] == FFTW for k in known),
                  "rtl_sdr": sum(k["program"] == "rtl_sdr" for k in known)}
        for name, n in sorted(programs.items()):
            s = _stem(name)
            fam = "rtl_power" if s in RTL_POWER_NAMES else s
            take = min(int(n), budget.get(fam, 0))
            if fam in budget:
                budget[fam] -= take
            unknown += [name] * (int(n) - take)
    return {"known": known, "unknown": unknown, "programs": programs}


def _same(entry: dict, rx: str | None, serial: str | None) -> bool:
    if serial and entry.get("serial"):
        return str(entry["serial"]) == str(serial)
    return bool(rx) and entry.get("radio") == rx


def check(station: Station, rx: str | None, programs: dict | None | bool = False,
          now: float | None = None) -> dict:
    """May a run start on radio `rx`? ok False when its radio is held (by a marsad run or
    recorder, named), or when a program runs that no record places on a radio. ok True with
    `others` listing what holds the other radio, which the run records."""
    from . import capture as cap
    u = users(station, programs, now)
    serial = cap.serial_of(cap.settings(station), rx) if rx else None
    mine = [k for k in u["known"] if _same(k, rx, serial)]
    unclear = [k for k in u["known"] if k not in mine and not k.get("radio") and not k.get("serial")]
    others = [k for k in u["known"] if k not in mine and k not in unclear]
    out = {"rx": rx, "serial": serial, "mine": mine, "others": others, "unknown": list(u["unknown"]),
           "unclear": unclear, "could_not_check": u["programs"] is None}
    if mine:
        held = "; ".join(f"{k['program']} ({k['what']})" for k in mine)
        out.update(ok=False, reason=f"The {_word(rx)} is in use: {held}. Only one program can use a radio "
                                    "at a time. Wait for it to finish, or stop it.")
    elif u["unknown"] or unclear:
        names = sorted(set(u["unknown"]) | {k["program"] for k in unclear})
        out.update(ok=False, reason=", ".join(names) + (" is" if len(names) == 1 else " are")
                   + " running, and marsad cannot tell which radio it holds (a program it did not start "
                     "leaves no record saying so). So a start on either radio is refused. Close it first.")
    else:
        out.update(ok=True, reason="")
    if out["ok"] and others:
        out["text"] = (f"The {_word(rx)} is free. The other radio is busy: "
                       + "; ".join(f"{_word(k['radio'])} {k['doing']} ({k['what']})" for k in others)
                       + ". The run records that, because two dongles side by side can raise each other's floor.")
    elif out["ok"]:
        out["text"] = ("Could not check for running radio programs; make sure SDR++ and other radio programs "
                       "are closed." if out["could_not_check"] else "No other radio program is running.")
    else:
        out["text"] = out["reason"]
    return out


def busy_record(result: dict, forced: bool = False) -> list:
    """The run's record of what else held a radio when it started: the field
    `other_radios_busy` in its sidecar (empty, and then left out, when nothing did). Not part
    of the fingerprint. With `forced` (someone answered "Start anyway"), what held this radio
    and any unplaced program are recorded too."""
    keep = ("radio", "serial", "doing", "program", "what")
    out = [{k: e.get(k) for k in keep} for e in result.get("others") or []]
    if forced:
        out += [{**{k: e.get(k) for k in keep}, "same_radio": True} for e in result.get("mine") or []]
        out += [{"radio": None, "serial": None, "doing": "unknown program", "program": _stem(p),
                 "what": "a program marsad did not start; which radio it held is not known"}
                for p in (result.get("unknown") or [])]
    return out


def describe(entries: list | None) -> str:
    """One line for a run's `other_radios_busy`, e.g. "radio-b recording (marsad watch fixed on
    300.5000 MHz)". Empty when the field is absent: runs from before relay 012 say nothing."""
    out = []
    for e in entries or []:
        who = _word(e.get("radio")) if e.get("radio") else "a program"
        what = f" ({e['what']})" if e.get("what") else ""
        out.append(f"{who} {e.get('doing') or 'busy'}{what}" + (" on this same radio" if e.get("same_radio") else ""))
    return "; ".join(out)


def table_rows(sidecar: dict | None) -> list[str]:
    """Markdown rows for report.md and scan_build.md; none when nothing else held a radio."""
    text = describe((sidecar or {}).get("other_radios_busy"))
    return [f"| Other radio busy at start | {text} (two dongles side by side can raise each other's floor) |"] if text else []
