"""Relay 019: setups for the two radios, what is possible with the radios enabled now, and each setup's "What if".

A setup is a choice of roles, stored with the roles in saved_scans.json. It starts nothing; the Start panels follow
it. Which setups the engine supports today (relay 012 made per-radio starts possible), each checked against the
engine's own start rules:

  survey_record  `marsad watch survey`: one radio surveys, the other records what it finds. Needs both radios free.
  both_survey    two `marsad run`s, one per radio: a run is refused only when its own radio is held (relay 012).
  both_record    two `marsad watch fixed`, one per radio, on different frequencies: a fixed watch needs only its own
                 radio free (owner, 2026-11-07); each writes its own status file (by serial) and the catalogue
                 database takes turns (sqlite's 5 s wait).
  one_survey     one radio surveys; the other idle or disabled.
  one_record     one radio parked on one frequency; the other idle or disabled.

Pure Python: no Qt, no service. The "What if" texts live in explanations.md (keys setup_<name>), so the design
project can review them; the station's own facts (splitter loss from the drawn rig, tuning ranges) are added here.
"""
from __future__ import annotations

import re
from pathlib import Path

ORDER = ("survey_record", "both_survey", "both_record", "one_survey", "one_record")
TITLES = {"survey_record": "Survey + record", "both_survey": "Both survey", "both_record": "Both record",
          "one_survey": "One radio: survey", "one_record": "One radio: fixed record"}
TWO = ("survey_record", "both_survey", "both_record")
# The engine supports all five today (see the module's note). A setup the engine could not do would be listed here
# with its reason, and offered greyed out.
NOT_SUPPORTED: dict = {}
SECTIONS = ("Good for", "Costs and limits", "Comparability", "Common mistakes")
FILE = Path(__file__).with_name("explanations.md")


def word(name) -> str:
    name = str(name or "")
    return name.upper() if name.isalpha() else name


def default_setup(d: dict) -> str:
    """With no setup chosen: survey + record when two radios are configured, else one radio surveying."""
    return "survey_record" if len((d or {}).get("radios") or []) >= 2 else "one_survey"


def chosen(d: dict) -> str:
    s = ((d or {}).get("roles") or {}).get("setup")
    return s if s in ORDER else default_setup(d)


def usable(d: dict) -> list:
    """The radios that can take new work: enabled, and not known to be unplugged."""
    return [r for r in (d or {}).get("radios") or [] if r.get("enabled", True) and r.get("present") is not False]


def availability(d: dict) -> dict:
    """{setup: {"ok", "why", "supported"}}: whether each setup can be used with the radios as they are now, and why
    not. Unsupported by the engine and not possible now are different reasons, said differently."""
    rs = (d or {}).get("radios") or []
    ok_radios = usable(d)
    off = [r for r in rs if not r.get("enabled", True)]
    out = {}
    for s in ORDER:
        if s in NOT_SUPPORTED:
            out[s] = {"ok": False, "supported": False, "why": "not supported yet: " + NOT_SUPPORTED[s]}
            continue
        if s in TWO:
            if len(rs) < 2:
                why = "needs two radios; station.json has one"
            elif len(ok_radios) < 2:
                bits = [f"the {word(r['name'])} is disabled" for r in off]
                bits += [f"the {word(r['name'])} is not plugged in" for r in rs
                         if r.get("enabled", True) and r.get("present") is False]
                why = "needs both radios enabled and plugged in: " + ", ".join(bits)
            else:
                why = ""
        else:
            why = "" if ok_radios else "needs one radio enabled and plugged in"
        out[s] = {"ok": not why, "supported": True, "why": why}
    return out


def possible_text(d: dict) -> str:
    """The Radios panel's top line: what can be done with the radios enabled now."""
    rs = (d or {}).get("radios") or []
    ok_radios = usable(d)
    off = [word(r["name"]) for r in rs if not r.get("enabled", True)]
    away = [word(r["name"]) for r in rs if r.get("enabled", True) and r.get("present") is False]
    note = ""
    if off:
        note += " " + " and ".join(off) + (" is" if len(off) == 1 else " are") + " disabled in this window."
    if away:
        note += " " + " and ".join(away) + (" is" if len(away) == 1 else " are") + " not plugged in."
    if len(ok_radios) >= 2:
        return ("Two radios: survey + record, both survey (two scans at once), or both record (two fixed "
                "recordings)." + note)
    if len(ok_radios) == 1:
        return (f"One radio ({word(ok_radios[0]['name'])}): survey, or a fixed recording. Survey + record and the "
                "two-radio setups need both radios enabled and plugged in." + note)
    if (d or {}).get("presence_source") is None and rs:
        return "Windows would not say which radios are plugged in." + note
    return "No radio can take new work now: none is enabled and plugged in." + note


# ---------------------------------------------------------------- the "What if" texts
def parse_whatif(md: str) -> dict:
    """{setup: {section: text}} from explanations.md's setup_<name> entries: each section starts with its bold
    title ("**Good for:** ...")."""
    out = {}
    for m in re.finditer(r"^##\s+setup_(\w+)\s*$(.*?)(?=^##\s|\Z)", md, re.M | re.S):
        body = m.group(2)
        secs = {}
        for t in SECTIONS:
            mm = re.search(r"\*\*" + re.escape(t) + r":\*\*\s*(.*?)(?=\n\s*\*\*[A-Z][^*]*:\*\*|\Z)", body, re.S)
            if mm:
                secs[t] = " ".join(mm.group(1).split())
        tip = re.search(r"^tip:\s*(.*)$", body, re.M)
        secs["tip"] = tip.group(1).strip() if tip else ""
        out[m.group(1)] = secs
    return out


_WHATIF: dict | None = None


def whatif_texts() -> dict:
    global _WHATIF
    if _WHATIF is None:
        _WHATIF = parse_whatif(FILE.read_text(encoding="utf-8"))
    return _WHATIF


def splitter_facts(rig: dict | None) -> list[str]:
    """What the drawn rig used for runs says about a splitter feeding two radios: its nominal loss per output, as
    typed on the part, or that none is typed (not measured)."""
    if not rig:
        return ["No rig is used for runs, so the splitter's loss is not known."]
    nodes = rig.get("nodes") or []
    sp = [n for n in nodes if (n.get("part") or {}).get("type") == "splitter"]
    if not sp:
        return ["The rig used for runs has no splitter: each radio has its own antenna path."]
    out = []
    for n in sp:
        name = (n["part"].get("name") or "Splitter").strip()
        g = n.get("gain_db")
        if isinstance(g, (int, float)) and not isinstance(g, bool):
            out.append(f"{name} in the rig used for runs: {g:+g} dB on each output (typed on the part, nominal).")
        else:
            out.append(f"{name} in the rig used for runs: its loss is not typed on the part (not measured). A "
                       "resistive 2-way splitter costs about 6 dB on each output in theory.")
    return out


def station_facts(setup: str, d: dict, rig: dict | None) -> list[str]:
    """The station's own facts for a setup's What if: ranges from station.json (via the radio list), the splitter
    from the drawn rig. Where a figure is not measured, it says so."""
    rs = (d or {}).get("radios") or []
    out = []
    for r in rs:
        rng = r.get("range_mhz") or []
        gaps = r.get("gaps_mhz") or []
        if rng:
            out.append(f"{word(r['name'])} tunes {_mhz(rng[0])}–{_mhz(rng[1])} MHz"
                       + (" (blind " + ", ".join(f"{_mhz(a)}–{_mhz(b)}" for a, b in gaps) + ")" if gaps else "")
                       + " (station.json).")
    both = (d or {}).get("both_cover_mhz") or []
    if setup in TWO and len(rs) >= 2:
        out.append("Both can tune " + (", ".join(f"{_mhz(a)}–{_mhz(b)}" for a, b in both) or "nothing in common")
                   + " MHz.")
        out += splitter_facts(rig)
        out.append("How much two dongles side by side raise each other's floor is not measured on this station.")
    return out


def _mhz(v) -> str:
    v = float(v)
    return str(int(v)) if v.is_integer() else f"{v:g}"


def whatif(setup: str, d: dict, rig: dict | None) -> dict:
    """{"title", "sections": [(title, text)], "facts": [...], "available": {...}} for a setup."""
    t = whatif_texts().get(setup) or {}
    return {"title": TITLES.get(setup, setup), "tip": t.get("tip", ""),
            "sections": [(s, t[s]) for s in SECTIONS if t.get(s)],
            "facts": station_facts(setup, d, rig), "available": availability(d).get(setup)}
