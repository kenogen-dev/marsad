"""`marsad watch report`: the review of one watch, without asking anybody.

    marsad watch report                  the newest watch in the catalogue
    marsad watch report <survey or tag>  one watch, by its survey name or part of it
    marsad watch report ... --md FILE    the same text, saved as Markdown too

Signal groups with their events, captures and GB; hourly counts by tier; grades; captures
that are likely empty; events the keep level refused although their peak was high; episodes;
possible harmonics; and pulse timing shared between signals, always as a question.

Read-only on occupancy.db. Private rows appear only as counts and occurrence: how many, in
which band, and when. Their frequencies and fingerprints are not listed.
"""
from __future__ import annotations

import datetime as dt
import json
from collections import Counter
from pathlib import Path

from . import capture as cap
from . import catalogue as cat
from .config import Station


def find_survey(con, query: str | None) -> str:
    """The survey a query names: exact, else the one name containing it, else refuse plainly."""
    names = [r[0] for r in con.execute("SELECT survey FROM captures WHERE survey IS NOT NULL "
                                       "GROUP BY survey ORDER BY MAX(started_epoch) DESC")]
    if not names:
        raise SystemExit("  No survey watch in the catalogue yet.")
    if not query:
        return names[0]
    if query in names:
        return query
    hits = [n for n in names if query in n]
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise SystemExit(f"  {query!r} matches {len(hits)} watches:\n    " + "\n    ".join(hits))
    raise SystemExit(f"  No watch matches {query!r}. Newest: {names[0]}")


def _hhmm(t: str | None) -> str:
    return (t or "")[5:16].replace("T", " ")


SAME_SIGNAL_HZ = 2.4e6
# The report says what a watch recorded, so IQ moved out of the pool afterwards still counts.
RECORDED = ("written", "archived", cat.SET_ASIDE)


def _recorded(r: dict) -> bool:
    return r.get("iq_status") in RECORDED


def timing_links(rows: list) -> list:
    """Pairs of open signals at different frequencies with the same pulse timing."""
    seen, out = [], []
    for r in rows:
        if r.get("private") or not r.get("timing"):
            continue
        try:
            t = json.loads(r["timing"])
        except (TypeError, ValueError):
            continue
        f = r.get("target_hz") or r["freq_hz"]
        for g in seen:
            if cap.timing_match(t, g["timing"]):
                # within one radio band it is the same signal, not another place with its timing
                if abs(f - g["f"]) > SAME_SIGNAL_HZ and not any(abs(f - x) <= SAME_SIGNAL_HZ for x in g["linked"]):
                    g["linked"].append(f)
                    out.append(tuple(sorted((round(f / 1e6, 2), round(g["f"] / 1e6, 2)))))
                break
        else:
            seen.append({"f": f, "timing": t, "linked": []})
    return out


def build(station: Station, query: str | None = None) -> dict:
    cfg = cap.settings(station)
    con = cat.open_ro(station)
    if con is None:
        raise SystemExit("  There is no captures catalogue yet.")
    try:
        name = find_survey(con, query)
        rows = [cat._row(r, cfg) for r in con.execute(
            "SELECT * FROM captures WHERE survey = ? ORDER BY started_epoch", (name,))]
        cat.sizes(con, station, cfg, rows)
    finally:
        con.close()
    private = [r for r in rows if r["private"]]
    open_rows = [r for r in rows if not r["private"]]

    hours: dict = {}
    for r in rows:
        h = (r.get("started") or "")[:13]
        hours.setdefault(h, Counter())["private" if r["private"] else "open"] += 1

    bands: dict = {}
    for r in private:
        b = bands.setdefault(r.get("band") or "private band", {"events": 0, "first": r["started"], "last": r["started"]})
        b["events"] += 1
        b["last"] = r["started"]

    iq = [r for r in open_rows if _recorded(r)]
    set_aside = [r for r in iq if r.get("iq_status") == cat.SET_ASIDE]
    empty =[r for r in iq if r.get("grade") == "empty"]
    refused = [r for r in open_rows if r.get("iq_status") == cap.BELOW_KEEP and (r.get("snr_peak_db") or 0) >=
               float(_settings(r).get("keep_min_snr_db") or 12.0)]
    harmonics: dict = {}
    for r in open_rows:
        if r.get("harmonic"):
            h = harmonics.setdefault(r["harmonic"], {"events": 0, "first": r["started"], "last": r["started"]})
            h["events"] += 1
            h["last"] = r["started"]
    return {"survey": name, "rows": rows, "events": len(rows), "open": len(open_rows), "private": len(private),
            "captures": len(iq), "iq_bytes": sum(r.get("iq_bytes") or 0 for r in iq),
            "set_aside": len(set_aside), "set_aside_bytes": sum(r.get("iq_bytes") or 0 for r in set_aside),
            "first": rows[0]["started"] if rows else None, "last": rows[-1]["started"] if rows else None,
            "groups": cat.group(open_rows, "size"), "bands": bands, "hours": hours,
            "grades": Counter(r["grade"] or "not graded" for r in open_rows),
            "statuses": Counter(r.get("iq_status") or "" for r in open_rows),
            "empty": empty, "refused": sorted(refused, key=lambda r: -(r.get("snr_peak_db") or 0)),
            "episodes": cat.episodes(open_rows), "harmonics": harmonics, "timing": timing_links(open_rows)}


def _settings(r: dict) -> dict:
    s = r.get("settings")
    if isinstance(s, dict):
        return s
    try:
        return json.loads(s or "{}")
    except ValueError:
        return {}


def to_text(rep: dict) -> str:
    moved = (f" ({rep['set_aside']} of them, {rep['set_aside_bytes'] / 1e9:.2f} GB, set aside since)"
             if rep["set_aside"] else "")
    L = [f"# Watch report: {rep['survey']}", "",
         f"{_hhmm(rep['first'])} to {_hhmm(rep['last'])}: {rep['events']} events ({rep['open']} open, "
         f"{rep['private']} private), {rep['captures']} IQ captures, {rep['iq_bytes'] / 1e9:.2f} GB of IQ{moved}", ""]

    L += ["## Open signals, largest first", "",
          "| MHz | what it was labelled | events | captures | GB | peak dB | best grade |",
          "|---:|---|---:|---:|---:|---:|---|"]
    for g in rep["groups"]:
        gb = sum(r.get("iq_bytes") or 0 for r in g["rows"]) / 1e9
        L.append(f"| {g['f_mhz']:.3f} | {g['family'] or '-'} | {g['events']} | {sum(map(_recorded, g['rows']))} | "
                 f"{gb:.2f} | {g['peak_db']:.1f} | {g['grade'] or '-'} |")

    L += ["", "## Events per hour", "", "| hour | open | private |", "|---|---:|---:|"]
    for h, c in sorted(rep["hours"].items()):
        L.append(f"| {h[5:].replace('T', ' ')}:00 | {c['open']} | {c['private']} |")

    L += ["", "## Open events: grades and what happened to their IQ", ""]
    L.append("Grades: " + (", ".join(f"{n} {k}" for k, n in rep["grades"].most_common()) or "none"))
    L.append("IQ: " + (", ".join(f"{n} {k}" for k, n in rep["statuses"].most_common()) or "none"))

    L += ["", "## Private bands: counts and occurrence only", ""]
    if rep["bands"]:
        L += ["| band | events | first | last |", "|---|---:|---|---|"]
        for b, v in sorted(rep["bands"].items(), key=lambda kv: -kv[1]["events"]):
            L.append(f"| {b} | {v['events']} | {_hhmm(v['first'])} | {_hhmm(v['last'])} |")
    else:
        L.append("None.")

    L += ["", "## Likely empty captures", ""]
    if rep["empty"]:
        gb = sum(r.get("iq_bytes") or 0 for r in rep["empty"]) / 1e9
        L.append(f"{len(rep['empty'])} of {rep['captures']} captures, {gb:.2f} GB: nothing the characteriser could "
                 "use, or a width that was only the capture's own filter.")
        L.append("")
        L += ["| event | MHz | when | MB | why |", "|---:|---:|---|---:|---|"]
        for r in rep["empty"]:
            why = "only the capture's filter shape" if r.get("filter_shaped") else (r.get("family") or "")
            if r.get("iq_status") == cat.SET_ASIDE:
                why += "; set aside since"
            L.append(f"| {r['id']} | {r['freq_hz'] / 1e6:.3f} | {_hhmm(r['started'])} | "
                     f"{(r.get('iq_bytes') or 0) / 1e6:.0f} | {why} |")
    else:
        L.append("None.")

    L += ["", "## Refused by the keep level although the peak was high", ""]
    if rep["refused"]:
        L += ["| event | MHz | when | peak dB | note |", "|---:|---:|---|---:|---|"]
        for r in rep["refused"][:30]:
            L.append(f"| {r['id']} | {r['freq_hz'] / 1e6:.3f} | {_hhmm(r['started'])} | {r['snr_peak_db']:.1f} | "
                     f"{(r.get('notes') or '').split(';')[0]} |")
        if len(rep["refused"]) > 30:
            L.append(f"\n...and {len(rep['refused']) - 30} more.")
    else:
        L.append("None.")

    L += ["", "## Episodes: several signals close together in time and frequency", ""]
    if rep["episodes"]:
        L += ["| from | to | MHz | events | signals | events' ids |", "|---|---|---|---:|---:|---|"]
        for e in rep["episodes"]:
            L.append(f"| {_hhmm(e['first'])} | {_hhmm(e['last'])} | {e['lo_mhz']:.1f}-{e['hi_mhz']:.1f} | "
                     f"{e['events']} | {e['signals']} | {', '.join(map(str, e['ids'][:12]))}"
                     f"{' ...' if len(e['ids']) > 12 else ''} |")
    else:
        L.append("None.")

    L += ["", "## Questions, not findings", ""]
    if not rep["harmonics"] and not rep["timing"]:
        L.append("None.")
    for h, v in sorted(rep["harmonics"].items()):
        L.append(f"- {h}: {v['events']} events, {_hhmm(v['first'])} to {_hhmm(v['last'])}. A flag only: untested.")
    for a, b in rep["timing"]:
        L.append(f"- {a:.2f} and {b:.2f} MHz have the same pulse timing: same emitter?")
    return "\n".join(L) + "\n"


def main(station: Station, query: str | None, md: str | None = None) -> int:
    text = to_text(build(station, query))
    print(text)
    if md:
        Path(md).write_text(text, encoding="utf-8")
        print(f"  wrote {md}")
    return 0
