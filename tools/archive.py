"""Long-term occupancy archive.

Every survey produces report.md, peaks.csv and bins.csv and then sits there as an
island. This appends them to a SQLite database so the station can answer the
questions a single run cannot:

    what is here that was not here last week?
    how has this frequency behaved over the last month?
    which signals are permanent, which are daily, which were once?

Three tables:

  surveys  one row per recording, with the comparability fingerprint
  signals  one row per detection (the peaks.csv rows)
  bands    bins.csv resampled onto a fixed 1 MHz grid

The resampling matters. rtl_power picks its bin width from the span, so a
100 MHz tile and a 907 MHz sweep have different grids and their bins.csv rows do
not line up. Detections are points and compare fine; bins do not, so they are
pooled into 1 MHz buckets that any survey can be compared on.

Stdlib only, so it runs under a bare `py` like run_survey.py.

    py tools/archive.py add reports/<name>          add one report
    py tools/archive.py add --all                   add every report not yet in
    py tools/archive.py list                        what is archived
    py tools/archive.py new                         signals in the newest survey only
    py tools/archive.py trend 1265.44               one frequency through time
    py tools/archive.py band 1920 1930              one band through time
    py tools/archive.py seen --min 3                the persistent set
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from run_survey import fingerprint  # noqa: E402  same wording as scan_build.md

DB = ROOT / "occupancy.db"
SURVEYS = ROOT / "surveys"
REPORTS = ROOT / "reports"
BUCKET_MHZ = 1.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS surveys (
  id              INTEGER PRIMARY KEY,
  name            TEXT UNIQUE NOT NULL,
  started         TEXT,
  started_epoch   REAL,
  duration_s      REAL,
  preset          TEXT,
  lo_mhz          REAL,
  hi_mhz          REAL,
  bin_khz         REAL,
  gain_db         REAL,
  integration_s   REAL,
  crop            REAL,
  fir             INTEGER,
  receiver        TEXT,
  fingerprint     TEXT,
  sweeps          INTEGER,
  skipped         INTEGER,
  median_floor_db REAL,
  antenna         TEXT,
  lna             TEXT,
  filt            TEXT,
  conditions      TEXT,
  terminated      INTEGER
);
CREATE TABLE IF NOT EXISTS signals (
  survey_id       INTEGER NOT NULL,
  freq_mhz        REAL, width_khz REAL,
  max_db          REAL, median_db REAL, floor_db REAL,
  snr_max_db      REAL, snr_median_db REAL,
  occupancy_pct   REAL, type TEXT, spur_tags TEXT
);
CREATE TABLE IF NOT EXISTS bands (
  survey_id       INTEGER NOT NULL,
  mhz             INTEGER,
  floor_db        REAL, median_db REAL, max_db REAL,
  occ_max_pct     REAL, occ_mean_pct REAL, n_bins INTEGER
);
CREATE INDEX IF NOT EXISTS ix_sig_freq ON signals(freq_mhz);
CREATE INDEX IF NOT EXISTS ix_sig_sv   ON signals(survey_id);
CREATE INDEX IF NOT EXISTS ix_band_mhz ON bands(mhz);
CREATE INDEX IF NOT EXISTS ix_sv_time  ON surveys(started_epoch);
"""


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if not n:
        return float("nan")
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def looks_terminated(sc: dict) -> int:
    """A 50 ohm load run has no antenna, so it must not count as 'this was not here'.

    Judged from the text the operator typed, which on this station always says
    '50 ohm load' when the antenna is off.
    """
    # The filename is included because the station's own tagging convention puts
    # "terminated" in the tag, and that is often more reliable than the chain text.
    blob = " ".join(str(v) for v in list(sc.get("chain", {}).values())
                    + [sc.get("conditions", ""), sc.get("csv", "")]).lower()
    # station.json carries the previous run's wording until it is answered again, so a
    # sidecar can hold both the stale "50 ohm load" text and the correction that says the
    # antenna is back. An explicit denial wins; otherwise fall back to the positive phrases.
    if any(w in blob for w in ("reconnected", "anymore", "no 50 ohm", "no 50ohm")):
        return 0
    return int("antenna disconnected" in blob or "50 ohm load" in blob
               or "50ohm load" in blob or "terminated" in blob)


def report_facts(report_dir: Path) -> tuple[int | None, int]:
    """Sweeps analysed and warm-up sweeps discarded, read back from report.md."""
    md = report_dir / "report.md"
    if not md.exists():
        return None, 0
    head = md.read_text(encoding="utf-8")[:2000]
    m = re.search(r"- Sweeps: ([\d,]+)", head)
    sweeps = int(m.group(1).replace(",", "")) if m else None
    m2 = re.search(r"first (\d+) discarded", head)
    return sweeps, int(m2.group(1)) if m2 else 0


def add_report(con: sqlite3.Connection, report_dir: Path, replace: bool = False) -> str:
    name = report_dir.name
    peaks, bins = report_dir / "peaks.csv", report_dir / "bins.csv"
    side = SURVEYS / f"{name}.json"
    if not peaks.exists() or not bins.exists():
        return f"  skip {name}: no peaks.csv/bins.csv"
    if not side.exists():
        return f"  skip {name}: no sidecar at surveys/{name}.json"

    row = con.execute("SELECT id FROM surveys WHERE name=?", (name,)).fetchone()
    if row and not replace:
        return f"  have {name}"
    if row:
        con.execute("DELETE FROM signals WHERE survey_id=?", (row["id"],))
        con.execute("DELETE FROM bands WHERE survey_id=?", (row["id"],))
        con.execute("DELETE FROM surveys WHERE id=?", (row["id"],))

    sc = json.loads(side.read_text(encoding="utf-8"))
    rp, ch = sc["rtl_power"], sc.get("chain", {})
    sweeps, skipped = report_facts(report_dir)

    band_rows = list(csv.DictReader(bins.open(encoding="utf-8")))
    floors = [float(r["floor_db"]) for r in band_rows if r["floor_db"] not in ("", "nan")]

    import datetime as dt
    started = sc.get("started_local") or ""
    try:
        epoch = dt.datetime.fromisoformat(started).timestamp()
    except ValueError:
        epoch = 0.0

    cur = con.execute(
        "INSERT INTO surveys (name, started, started_epoch, duration_s, preset, lo_mhz,"
        " hi_mhz, bin_khz, gain_db, integration_s, crop, fir, receiver, fingerprint,"
        " sweeps, skipped, median_floor_db, antenna, lna, filt, conditions, terminated)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (name, started, epoch, sc.get("duration_actual_s"), sc.get("preset"),
         rp.get("freq_low_mhz"), rp.get("freq_high_mhz_effective"),
         rp.get("bin_khz_actual") or rp.get("bin_khz"), rp.get("gain_db"),
         rp.get("integration_s"), rp.get("crop"), rp.get("fir"), sc.get("receiver"),
         fingerprint(sc, rp.get("bin_khz_actual")), sweeps, skipped,
         round(median(floors), 2) if floors else None,
         ch.get("antenna_shape"), ch.get("lna"), ch.get("filter"),
         sc.get("conditions", ""), looks_terminated(sc)))
    sid = cur.lastrowid

    with peaks.open(encoding="utf-8") as fh:
        con.executemany(
            "INSERT INTO signals VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [(sid, float(r["freq_mhz"]), float(r["width_khz"]), float(r["max_db"]),
              float(r["median_db"]), float(r["floor_db"]), float(r["snr_max_db"]),
              float(r["snr_median_db"]), float(r["occupancy_pct"]), r["type"],
              r["spur_tags"]) for r in csv.DictReader(fh)])

    buckets: dict[int, list[tuple[float, float, float, float]]] = {}
    for r in band_rows:
        try:
            f = float(r["freq_mhz"])
            vals = (float(r["floor_db"]), float(r["median_db"]),
                    float(r["max_db"]), float(r["occupancy_pct"]))
        except ValueError:
            continue
        buckets.setdefault(int(f // BUCKET_MHZ), []).append(vals)
    con.executemany(
        "INSERT INTO bands VALUES (?,?,?,?,?,?,?,?)",
        [(sid, k, round(median([v[0] for v in vs]), 2),
          round(median([v[1] for v in vs]), 2), round(max(v[2] for v in vs), 2),
          round(max(v[3] for v in vs), 2),
          round(sum(v[3] for v in vs) / len(vs), 3), len(vs))
         for k, vs in buckets.items()])
    con.commit()
    flag = " [terminated]" if looks_terminated(sc) else ""
    return (f"  added {name}: {len(list(csv.DictReader(peaks.open(encoding='utf-8'))))} "
            f"signals, {len(buckets)} MHz buckets{flag}")


def cmd_add(args) -> int:
    con = connect()
    dirs = ([d for d in sorted(REPORTS.iterdir())
             if d.is_dir() and not d.name.startswith("compare_")]
            if args.all else [Path(p) for p in args.report])
    for d in dirs:
        print(add_report(con, d, args.replace))
    n = con.execute("SELECT COUNT(*) c FROM surveys").fetchone()["c"]
    print(f"\n  {n} surveys archived in {DB.name}")
    return 0


def cmd_list(args) -> int:
    con = connect()
    rows = con.execute(
        "SELECT s.*, (SELECT COUNT(*) FROM signals g WHERE g.survey_id=s.id) n"
        " FROM surveys s ORDER BY started_epoch").fetchall()
    if not rows:
        print("  nothing archived yet; run: py tools/archive.py add --all")
        return 0
    print(f"\n  {'started':<17}{'name':<44}{'sweeps':>7}{'sigs':>6}{'floor':>8}  fingerprint")
    for r in rows:
        print(f"  {(r['started'] or '')[:16]:<17}{r['name'][:43]:<44}"
              f"{r['sweeps'] or 0:>7}{r['n']:>6}{r['median_floor_db'] or 0:>8.1f}"
              f"  {r['fingerprint']}{'  [terminated]' if r['terminated'] else ''}")
    print()
    return 0


def comparable(con, sv: sqlite3.Row, any_settings: bool) -> list[sqlite3.Row]:
    """Earlier surveys this one can honestly be measured against.

    Same fingerprint by default: a different gain or bin width changes what clears
    the threshold, so 'new' would just be reporting the settings change.
    Terminated runs are always excluded - with no antenna, everything looks absent.
    """
    q = ("SELECT * FROM surveys WHERE id != ? AND terminated = 0"
         " AND started_epoch < ? AND lo_mhz <= ? AND hi_mhz >= ?")
    p = [sv["id"], sv["started_epoch"], sv["lo_mhz"], sv["hi_mhz"]]
    if not any_settings:
        q += " AND fingerprint = ?"
        p.append(sv["fingerprint"])
    return con.execute(q + " ORDER BY started_epoch DESC", p).fetchall()


def cmd_new(args) -> int:
    con = connect()
    sv = (con.execute("SELECT * FROM surveys WHERE name=?", (args.survey,)).fetchone()
          if args.survey else
          con.execute("SELECT * FROM surveys ORDER BY started_epoch DESC LIMIT 1").fetchone())
    if not sv:
        print("  no such survey")
        return 1
    prev = comparable(con, sv, args.any_settings)[:args.last]
    print(f"\n  {sv['name']}  ({sv['started'][:16]})")
    print(f"  compared against {len(prev)} earlier survey(s) with "
          f"{'any settings' if args.any_settings else 'the same fingerprint'}")
    if not prev:
        print("  nothing to compare with yet - archive more runs of the same build.\n")
        return 0
    # A longer run hears more of everything, so every rare burst in it looks "new"
    # against shorter runs. Say so rather than letting the list be misread.
    base = median([float(p["sweeps"] or 0) for p in prev])
    if base and (sv["sweeps"] or 0) > 2 * base:
        print(f"  CAUTION: this run has {sv['sweeps']} sweeps against a median of {base:.0f}"
              f" in the comparison set.")
        print("  It simply listened longer, so rare bursts will look new. Use --min-occ to"
              " filter,")
        print("  or compare runs of similar length.")
    ids = tuple(p["id"] for p in prev)
    tol = args.tol_khz / 1000.0
    rows = con.execute(
        "SELECT * FROM signals WHERE survey_id=? AND occupancy_pct >= ?"
        " AND snr_max_db >= ? ORDER BY snr_max_db DESC", (sv["id"], args.min_occ, args.min_snr)
    ).fetchall()
    out = []
    for r in rows:
        if args.no_spurs and r["spur_tags"]:
            continue
        hit = con.execute(
            f"SELECT COUNT(*) c FROM signals WHERE survey_id IN "
            f"({','.join('?' * len(ids))}) AND ABS(freq_mhz - ?) <= ?",
            (*ids, r["freq_mhz"], tol)).fetchone()["c"]
        if hit == 0:
            out.append(r)
    if not out:
        print("  nothing new.\n")
        return 0
    print(f"\n  {len(out)} signal(s) not seen in those runs (within {args.tol_khz:g} kHz):\n")
    print(f"  {'freq MHz':>11}{'SNR':>7}{'occ %':>8}{'width kHz':>11}  type / tags")
    for r in out:
        print(f"  {r['freq_mhz']:>11.4f}{r['snr_max_db']:>7.1f}{r['occupancy_pct']:>8.1f}"
              f"{r['width_khz']:>11.1f}  {r['type']}"
              f"{'  | ' + r['spur_tags'] if r['spur_tags'] else ''}")
    print()
    return 0


def cmd_trend(args) -> int:
    con = connect()
    tol = args.tol_khz / 1000.0
    rows = con.execute(
        "SELECT v.name, v.started, v.terminated, v.fingerprint, g.* FROM signals g"
        " JOIN surveys v ON v.id = g.survey_id WHERE ABS(g.freq_mhz - ?) <= ?"
        " ORDER BY v.started_epoch", (args.mhz, tol)).fetchall()
    cov = con.execute(
        "SELECT COUNT(*) c FROM surveys WHERE lo_mhz <= ? AND hi_mhz >= ?",
        (args.mhz, args.mhz)).fetchone()["c"]
    print(f"\n  {args.mhz:g} MHz +/- {args.tol_khz:g} kHz")
    print(f"  detected in {len(rows)} of {cov} survey(s) that covered it\n")
    if not rows:
        print("  never above threshold there.\n")
        return 0
    print(f"  {'started':<17}{'freq MHz':>11}{'SNR':>7}{'occ %':>8}  survey")
    for r in rows:
        print(f"  {(r['started'] or '')[:16]:<17}{r['freq_mhz']:>11.4f}"
              f"{r['snr_max_db']:>7.1f}{r['occupancy_pct']:>8.1f}  {r['name'][:40]}"
              f"{'  [terminated]' if r['terminated'] else ''}")
    print()
    return 0


def cmd_band(args) -> int:
    con = connect()
    rows = con.execute(
        "SELECT v.name, v.started, v.terminated, COUNT(*) n,"
        " ROUND(AVG(b.floor_db),2) floor, ROUND(MAX(b.max_db),2) peak,"
        " ROUND(MAX(b.occ_max_pct),1) occ_max, ROUND(AVG(b.occ_mean_pct),3) occ_mean"
        " FROM bands b JOIN surveys v ON v.id = b.survey_id"
        " WHERE b.mhz >= ? AND b.mhz < ? GROUP BY v.id ORDER BY v.started_epoch",
        (int(args.lo), int(args.hi))).fetchall()
    print(f"\n  {args.lo:g}-{args.hi:g} MHz, one row per survey\n")
    if not rows:
        print("  no archived survey covers that band.\n")
        return 0
    print(f"  {'started':<17}{'floor':>8}{'peak':>8}{'occ max':>9}{'occ mean':>10}  survey")
    for r in rows:
        print(f"  {(r['started'] or '')[:16]:<17}{r['floor']:>8.2f}{r['peak']:>8.2f}"
              f"{r['occ_max']:>9.1f}{r['occ_mean']:>10.3f}  {r['name'][:38]}"
              f"{'  [terminated]' if r['terminated'] else ''}")
    print()
    return 0


def cmd_seen(args) -> int:
    con = connect()
    rows = con.execute(
        "SELECT ROUND(freq_mhz, 2) f, COUNT(DISTINCT survey_id) n,"
        " ROUND(AVG(snr_max_db),1) snr, ROUND(AVG(occupancy_pct),1) occ,"
        " MAX(spur_tags) tags FROM signals g"
        " JOIN surveys v ON v.id = g.survey_id WHERE v.terminated = 0"
        " GROUP BY f HAVING n >= ? ORDER BY n DESC, snr DESC", (args.min,)).fetchall()
    print(f"\n  frequencies detected in at least {args.min} antenna survey(s)\n")
    if not rows:
        print("  nothing yet.\n")
        return 0
    print(f"  {'freq MHz':>11}{'runs':>6}{'avg SNR':>9}{'avg occ':>9}  tags")
    for r in rows[:args.limit]:
        print(f"  {r['f']:>11.2f}{r['n']:>6}{r['snr']:>9.1f}{r['occ']:>9.1f}"
              f"  {(r['tags'] or '')[:60]}")
    print()
    return 0


def cmd_mark(args) -> int:
    """The text heuristic is a guess; this is the record of fact."""
    con = connect()
    n = con.execute("UPDATE surveys SET terminated=? WHERE name=?",
                    (int(args.term), args.survey)).rowcount
    con.commit()
    print(f"  {args.survey}: {'no antenna (terminated)' if args.term else 'antenna connected'}"
          if n else f"  no archived survey named {args.survey}")
    return 0 if n else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="archive one or more report folders")
    a.add_argument("report", nargs="*")
    a.add_argument("--all", action="store_true", help="every folder in reports/")
    a.add_argument("--replace", action="store_true", help="re-ingest ones already present")
    a.set_defaults(func=cmd_add)

    sub.add_parser("list", help="what is archived").set_defaults(func=cmd_list)

    n = sub.add_parser("new", help="signals in a survey that earlier comparable runs lack")
    n.add_argument("survey", nargs="?", help="default: the most recent archived survey")
    n.add_argument("--last", type=int, default=5, help="how many earlier runs to check (5)")
    n.add_argument("--tol-khz", type=float, default=100.0, help="match window (100 kHz)")
    n.add_argument("--min-snr", type=float, default=0.0)
    n.add_argument("--min-occ", type=float, default=0.0)
    n.add_argument("--no-spurs", action="store_true", help="hide anything already tagged")
    n.add_argument("--any-settings", action="store_true",
                   help="compare across different gain/bin/integration too")
    n.set_defaults(func=cmd_new)

    t = sub.add_parser("trend", help="one frequency across every survey")
    t.add_argument("mhz", type=float)
    t.add_argument("--tol-khz", type=float, default=100.0)
    t.set_defaults(func=cmd_trend)

    b = sub.add_parser("band", help="one band across every survey")
    b.add_argument("lo", type=float)
    b.add_argument("hi", type=float)
    b.set_defaults(func=cmd_band)

    m = sub.add_parser("mark", help="correct the terminated/antenna flag by hand")
    m.add_argument("survey")
    g = m.add_mutually_exclusive_group(required=True)
    g.add_argument("--terminated", dest="term", action="store_true")
    g.add_argument("--antenna", dest="term", action="store_false")
    m.set_defaults(func=cmd_mark)

    s = sub.add_parser("seen", help="frequencies that keep coming back")
    s.add_argument("--min", type=int, default=2, help="minimum surveys (2)")
    s.add_argument("--limit", type=int, default=40)
    s.set_defaults(func=cmd_seen)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
