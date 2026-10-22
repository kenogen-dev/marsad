"""Return loss and SWR of an antenna from three bridge runs.

A return-loss bridge with a broadband noise source on its input and the
receiver on its output reflects whatever is on its test port back to the
receiver. Three rtl_power runs with one fingerprint give the antenna's return
loss at every bin:

  open     nothing on the test port: total reflection, the 0 dB reference
  load     a 50 ohm load on the port: what leaks through the bridge with a
           perfect match, i.e. its directivity - the deepest return loss it
           can show at that frequency
  antenna  the antenna under test

    marsad response --open <tag|csv> --load <tag|csv> --antenna <tag|csv>
                    [--name "2130 GP v1"] [--f0 2130] [--smooth-mhz 1]
                    [--margin-db 6] [--rx-floor-db -37.5] [--skip-sweeps N]
                    [--out reports/response/<name>_<stamp>]

Per bin, on the median over sweeps (so a one-sweep click drops out):

    rl_db    = open_db - antenna_db        return loss, clipped at 0
    rho      = 10 ** (-rl_db / 20)         reflection coefficient
    swr      = (1 + rho) / (1 - rho)       capped at 99
    floor_db = open_db - load_db           bridge directivity (with --load)

Trust per bin: "ok"; "at-least" when the measured return loss is within
--margin-db of the bridge's directivity (the bridge cannot see deeper, so the
true value may be higher) or when the antenna reading is within --margin-db of
the receiver's own noise floor (--rx-floor-db); "dropped" within 25 kHz of a
hop centre (the E4000 DC spike) or inside the receiver's PLL gap.

Why --rx-floor-db is a number you give and not the run's floor column: with
the noise source on, the analyser's floor (20th percentile over time, rolling
median over frequency) sits ON the trace - it is the generator's level, not the
receiver's. The receiver's own floor comes from a terminated run with the
generator off (the station notes record -37.5 dB at g29 on the XTR).

Outputs: response.csv (every bin), response.md (the headline), atlas.json
(the smoothed curve for the Antenna Atlas import).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
from pathlib import Path

from .config import Station
from .rtlpower import fingerprint

HOP_EXCLUDE_KHZ = 25.0      # the DC spike's skirt, as hunt mode excludes it
VOUCH_DB = 15.0             # the bridge "vouches" for a bin when its directivity is this deep


# ---------------------------------------------------------------- loading

def resolve_csv(station: Station, token: str) -> Path:
    """A raw CSV path, or a tag matched against surveys/ (newest wins)."""
    from .drift import resolve
    return resolve(station, token)


def load_run(station: Station, token: str, skip_sweeps: int = 0) -> dict:
    """One bridge run: median over sweeps on the survey's own grid, plus its sidecar."""
    import numpy as np
    from .analyze import drop_sweeps, load_rtl_power, load_sidecar

    path = resolve_csv(station, token)
    s = load_rtl_power(path)
    drop_sweeps(s, skip_sweeps)
    with np.errstate(all="ignore"):
        med = np.nanmedian(s.power_db, axis=0)
    sc = load_sidecar(path) or {}
    fp = None
    if sc.get("rtl_power"):
        fp = fingerprint(sc, sc["rtl_power"].get("bin_khz_actual"))
    return {
        "tag": path.stem, "path": path, "sidecar": sc, "fingerprint": fp,
        "freqs_hz": s.freqs_hz, "median_db": med, "step_hz": s.step_hz,
        "hop_centres_hz": s.hop_centres_hz, "sweeps": len(s.times),
        "receiver": sc.get("receiver"),
    }


def check_same_fingerprint(runs: dict) -> None:
    """Refuse in a plain sentence when the runs cannot be subtracted."""
    fps = {k: r["fingerprint"] for k, r in runs.items()}
    known = {k: v for k, v in fps.items() if v}
    if len(set(known.values())) > 1:
        lines = [f"  {k:<8}{v}" for k, v in known.items()]
        raise SystemExit("The runs have different fingerprints, so their levels cannot be "
                         "subtracted bin for bin. Record all three with one range, bin width, "
                         "gain, integration, crop, FIR and receiver.\n" + "\n".join(lines))
    # Without a sidecar the fingerprint is unknown; the grid itself is the last check.
    grids = {k: (len(r["freqs_hz"]), round(r["step_hz"], 3),
                 round(float(r["freqs_hz"][0]), 1)) for k, r in runs.items()}
    if len(set(grids.values())) > 1:
        raise SystemExit("The runs are not on the same frequency grid (different range or bin "
                         "width), so their levels cannot be subtracted bin for bin.")


# ---------------------------------------------------------------- maths

def compute(open_run: dict, antenna_run: dict, load_run_: dict | None,
            gaps_mhz=(), smooth_mhz: float = 1.0, margin_db: float = 6.0,
            rx_floor_db: float | None = None):
    """The per-bin table. Pure arithmetic on the three median spectra."""
    import numpy as np
    import pandas as pd

    f = open_run["freqs_hz"]
    f_mhz = f / 1e6
    open_db = open_run["median_db"]
    ant_db = antenna_run["median_db"]
    load_db = load_run_["median_db"] if load_run_ else np.full_like(open_db, np.nan)

    rl = np.clip(open_db - ant_db, 0.0, None)
    floor = open_db - load_db                          # NaN without --load

    # trust flags
    dropped = np.zeros(len(f), dtype=bool)
    for hc in open_run["hop_centres_hz"]:
        dropped |= np.abs(f - hc) <= HOP_EXCLUDE_KHZ * 1e3
    for lo, hi in gaps_mhz:
        dropped |= (f_mhz >= lo) & (f_mhz <= hi)
    dropped |= np.isnan(open_db) | np.isnan(ant_db)
    at_least = np.zeros(len(f), dtype=bool)
    if load_run_ is not None:
        with np.errstate(invalid="ignore"):
            at_least |= rl >= (floor - margin_db)
    if rx_floor_db is not None:
        at_least |= ant_db <= rx_floor_db + margin_db

    # smoothing: a running median over --smooth-mhz, dropped bins left out
    win = max(1, int(round(smooth_mhz * 1e6 / open_run["step_hz"])) | 1)
    rl_in = pd.Series(np.where(dropped, np.nan, rl))
    rl_smooth = rl_in.rolling(win, center=True, min_periods=1).median().to_numpy()
    # a window holding any at-least bin is at-least
    al_in = pd.Series(np.where(dropped, 0.0, at_least.astype(float)))
    al_win = al_in.rolling(win, center=True, min_periods=1).max().to_numpy() > 0

    def swr_of(rl_db):
        rho = 10.0 ** (-rl_db / 20.0)
        with np.errstate(divide="ignore", invalid="ignore"):
            s = (1 + rho) / (1 - rho)
        return np.where(rho >= 0.999, 99.0, np.minimum(s, 99.0))

    swr = swr_of(rl)
    swr_smooth = swr_of(rl_smooth)

    flag = np.where(dropped, "dropped", np.where(al_win, "at-least", "ok"))
    return pd.DataFrame({
        "freq_mhz": np.round(f_mhz, 6),
        "open_db": np.round(open_db, 2),
        "load_db": np.round(load_db, 2),
        "antenna_db": np.round(ant_db, 2),
        "rl_db": np.round(rl, 2),
        "rl_smooth_db": np.round(rl_smooth, 2),
        "swr": np.round(swr, 3),
        "swr_smooth": np.round(swr_smooth, 3),
        "floor_db": np.round(floor, 2),
        "flag": flag,
    })


def headline(df, f0_mhz: float | None, has_load: bool) -> dict:
    """The numbers the report leads with."""
    import numpy as np
    ok = df["flag"] == "ok"
    out = {"n_bins": int(len(df)),
           "pct_at_least": round(100.0 * float((df["flag"] == "at-least").mean()), 1),
           "pct_dropped": round(100.0 * float((df["flag"] == "dropped").mean()), 1)}
    if not ok.any():
        out["dip"] = None
        return out
    sw = df["swr_smooth"].where(ok)
    i = int(sw.idxmin())
    out["dip"] = {"freq_mhz": float(df.loc[i, "freq_mhz"]),
                  "swr": float(df.loc[i, "swr_smooth"]),
                  "rl_db": float(df.loc[i, "rl_smooth_db"])}
    # 2:1 bandwidth: contiguous ok bins around the dip with smoothed SWR <= 2
    good = (ok & (df["swr_smooth"] <= 2.0)).to_numpy()
    lo = hi = i
    while lo > 0 and good[lo - 1]:
        lo -= 1
    while hi < len(df) - 1 and good[hi + 1]:
        hi += 1
    if good[i]:
        out["bw2"] = {"lo_mhz": float(df.loc[lo, "freq_mhz"]), "hi_mhz": float(df.loc[hi, "freq_mhz"]),
                      "width_mhz": float(df.loc[hi, "freq_mhz"] - df.loc[lo, "freq_mhz"])}
    else:
        out["bw2"] = None
    if has_load:
        vouch = df["floor_db"] >= VOUCH_DB
        out["vouch"] = ({"lo_mhz": float(df.loc[vouch, "freq_mhz"].min()),
                         "hi_mhz": float(df.loc[vouch, "freq_mhz"].max()),
                         "pct": round(100.0 * float(vouch.mean()), 1)}
                        if vouch.any() else None)
    else:
        out["vouch"] = None
    if f0_mhz:
        off = out["dip"]["freq_mhz"] - f0_mhz
        out["offset"] = {"mhz": round(off, 3), "pct": round(100.0 * off / f0_mhz, 2)}
    return out


# ---------------------------------------------------------------- outputs

ATLAS_MAX_POINTS = 800      # the Atlas stores a measurement with its design; 20,000 points is too big


def decimate(keep, max_points: int, dip: dict | None):
    """At most `max_points` rows, evenly spread over the span, the dip bin always kept.

    response.csv holds every bin; this only thins what goes into atlas.json.
    Even spacing means roughly one point per MHz on the LS range. The dip is
    re-inserted by frequency so the curve's minimum is never lost to the step.
    """
    if len(keep) <= max_points:
        return keep
    import numpy as np
    idx = np.unique(np.linspace(0, len(keep) - 1, max_points).round().astype(int))
    rows = keep.iloc[idx]
    if dip:
        hit = keep[np.isclose(keep["freq_mhz"], dip["freq_mhz"])]
        if not hit.empty and float(hit["freq_mhz"].iloc[0]) not in set(rows["freq_mhz"]):
            rows = (keep.loc[sorted(set(rows.index) | set(hit.index))])
    return rows


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-") or "response"


def run_lines(label: str, r: dict | None) -> list[str]:
    if r is None:
        return [f"- **{label}**: not given (no bridge-floor flag; every return loss is taken at face value)"]
    sc = r["sidecar"]
    chain = (sc.get("chain") or {}).get("chain_order") or "(not recorded)"
    cond = sc.get("conditions") or "(none recorded)"
    return [f"- **{label}**: `{r['tag']}`, {r['sweeps']} sweeps",
            f"  - fingerprint `{r['fingerprint'] or 'unknown (no sidecar)'}`",
            f"  - chain: {chain}", f"  - conditions: {cond}"]


def write_outputs(out: Path, name: str, runs: dict, df, head: dict, f0_mhz, args) -> list[str]:
    out.mkdir(parents=True, exist_ok=True)
    df.to_csv(out / "response.csv", index=False)

    has_load = runs.get("load") is not None
    lines = [f"# Antenna response: {name}", "",
             f"Three bridge runs, return loss per bin from `open - antenna`, "
             f"smoothed over {args.smooth_mhz:g} MHz. Margin {args.margin_db:g} dB.", ""]
    lines += run_lines("open", runs["open"]) + run_lines("load", runs.get("load")) \
        + run_lines("antenna", runs["antenna"]) + [""]
    if args.rx_floor_db is None:
        lines += ["Receiver floor: not given, so no bin is flagged for sitting on the receiver's "
                  "own noise. Pass `--rx-floor-db` (a terminated run's whole-band median) to "
                  "enable that flag.", ""]
    else:
        lines += [f"Receiver floor taken as {args.rx_floor_db:g} dB.", ""]
    lines += ["## Headline", ""]
    hl = []
    if head.get("dip"):
        d = head["dip"]
        hl.append(f"dip            {d['freq_mhz']:.3f} MHz   SWR {d['swr']:.2f}   "
                  f"return loss {d['rl_db']:.1f} dB")
        if head.get("bw2"):
            b = head["bw2"]
            hl.append(f"2:1 bandwidth  {b['lo_mhz']:.3f} - {b['hi_mhz']:.3f} MHz   "
                      f"({b['width_mhz']:.3f} MHz wide)")
        else:
            hl.append("2:1 bandwidth  none: the smoothed SWR never reaches 2:1 on a trusted bin")
        if head.get("offset"):
            o = head["offset"]
            hl.append(f"offset from f0 {o['mhz']:+.3f} MHz ({o['pct']:+.2f}%) from {f0_mhz:g} MHz")
    else:
        hl.append("dip            none: no bin is trusted (all at-least or dropped)")
    if has_load:
        v = head.get("vouch")
        hl.append(f"bridge vouches {v['lo_mhz']:.3f} - {v['hi_mhz']:.3f} MHz "
                  f"({v['pct']:g}% of bins, directivity >= {VOUCH_DB:g} dB)"
                  if v else f"bridge vouches nowhere: directivity under {VOUCH_DB:g} dB at every bin")
    hl.append(f"bins           {head['n_bins']}: {head['pct_at_least']:g}% at-least, "
              f"{head['pct_dropped']:g}% dropped")
    lines += ["```", *hl, "```", "",
              "`at-least`: the bridge or the receiver cannot see deeper there; the true return "
              "loss may be higher. `dropped`: hop-centre DC spike or PLL gap.", ""]
    (out / "response.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    keep = df[df["flag"] != "dropped"]
    keep = decimate(keep, ATLAS_MAX_POINTS, head.get("dip"))
    atlas = {
        "name": name,
        "when": dt.datetime.now().isoformat(timespec="seconds"),
        "z0": 50,
        "f0_mhz": f0_mhz,
        "source": {"open": runs["open"]["tag"],
                   "load": runs["load"]["tag"] if has_load else None,
                   "antenna": runs["antenna"]["tag"]},
        "fingerprint": runs["open"]["fingerprint"] or runs["antenna"]["fingerprint"],
        "points": [{"f": float(r.freq_mhz), "swr": round(float(r.swr_smooth), 2),
                    "rl": round(float(r.rl_smooth_db), 1), "flag": str(r.flag)}
                   for r in keep.itertuples()],
    }
    (out / "atlas.json").write_text(json.dumps(atlas, indent=1), encoding="utf-8")
    return hl


# ---------------------------------------------------------------- CLI

def main(argv=None, station: Station | None = None, prog: str = "marsad response") -> int:
    station = station or Station.load()
    ap = argparse.ArgumentParser(prog=prog,
                                 description="Return loss and SWR from three bridge runs")
    ap.add_argument("--open", required=True, metavar="TAG|CSV", help="nothing on the test port")
    ap.add_argument("--antenna", required=True, metavar="TAG|CSV", help="the antenna under test")
    ap.add_argument("--load", metavar="TAG|CSV", help="50 ohm load on the test port (bridge floor)")
    ap.add_argument("--name", default=None, help='e.g. "2130 GP v1" (default: the antenna tag)')
    ap.add_argument("--f0", type=float, metavar="MHZ", help="design frequency, for the offset")
    ap.add_argument("--smooth-mhz", type=float, default=1.0, help="running-median width (1)")
    ap.add_argument("--margin-db", type=float, default=6.0,
                    help="how close to a floor counts as at-least (6)")
    ap.add_argument("--rx-floor-db", type=float, default=None,
                    help="the receiver's own noise floor from a terminated run, e.g. -37.5")
    ap.add_argument("--skip-sweeps", type=int, default=0, help="discard warm-up sweeps (0)")
    ap.add_argument("--out", help="output folder (default reports/response/<name>_<stamp>)")
    args = ap.parse_args(argv)

    runs = {"open": load_run(station, args.open, args.skip_sweeps),
            "antenna": load_run(station, args.antenna, args.skip_sweeps)}
    if args.load:
        runs["load"] = load_run(station, args.load, args.skip_sweeps)
    check_same_fingerprint(runs)

    receiver = next((r["receiver"] for r in runs.values() if r["receiver"]), None)
    gaps = station.receiver_gaps().get(receiver or "", [])
    df = compute(runs["open"], runs["antenna"], runs.get("load"), gaps,
                 args.smooth_mhz, args.margin_db, args.rx_floor_db)
    head = headline(df, args.f0, "load" in runs)

    name = args.name or runs["antenna"]["tag"]
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
    out = Path(args.out) if args.out else station.reports / "response" / f"{slug(name)}_{stamp}"
    hl = write_outputs(out, name, runs, df, head, args.f0, args)

    print(f"\n  RESPONSE  {name}")
    for line in hl:
        print("  " + line)
    print(f"\n  written: {out}\n")
    return 0
