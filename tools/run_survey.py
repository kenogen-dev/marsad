#!/usr/bin/env python3
"""
Run an rtl_power survey from a named preset, then analyse it.

  py tools/run_survey.py LS 6h
  py tools/run_survey.py milair 30m --conditions "clear, heating off"
  py tools/run_survey.py --list
  py tools/run_survey.py LS 6h --dry-run

Does, in order:
  1. refuses to start if another program is holding the SDR (SDR++ etc.)
  2. builds the standard rtl_power command for the preset
  3. asks about the antenna/chain and conditions (Enter accepts last used)
  4. writes a surveys/<name>.json sidecar, then runs rtl_power
  5. runs survey.py analyze on the result

Standard command shape, per the station conventions:
  rtl_power -f LO:HI:BIN -g <fixed gain> -i <sec> -c 0.25 -F 9 -e <duration> out.csv

Stdlib only, so it works under the venv or a bare `py`.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RTL_POWER = ROOT / "rtl-sdr" / "rtl_power.exe"
SURVEY_PY = ROOT / "tools" / "survey.py"
SURVEYS = ROOT / "surveys"
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"


def analysis_python() -> str:
    """Interpreter to run survey.py with.

    survey.py needs pandas/numpy/matplotlib, which live in .venv. This script is
    stdlib-only and is normally launched with a bare `py`, so sys.executable is the
    system interpreter and has none of them. Prefer the venv when it exists.
    """
    return str(VENV_PY) if VENV_PY.exists() else sys.executable


def fmt_hms(sec: float) -> str:
    sec = int(round(max(0.0, sec)))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def progress_every(dur_s: float) -> float:
    """Seconds between progress lines: about ten per run, clamped.

    A 5 min check should not sit silent for minutes; a 6 h survey should not scroll.
    """
    return min(300.0, max(30.0, dur_s / 10.0))


def run_with_progress(cmd: list, dur_s: float, csv_path: Path, every: float | None = None) -> int:
    """Run rtl_power, printing elapsed/remaining periodically.

    rtl_power prints its header and then nothing for the whole run, so a long survey
    looks hung. Each update is its own line rather than a redrawing bar, because
    rtl_power shares this console and its own output would break a 
-updated line.
    """
    every = progress_every(dur_s) if every is None else every
    t0 = time.time()
    nxt = t0 + every
    proc = subprocess.Popen(cmd)
    try:
        while True:
            try:
                return proc.wait(timeout=max(0.5, nxt - time.time()))
            except subprocess.TimeoutExpired:
                pass
            now = time.time()
            nxt = now + every
            el, left = now - t0, max(0.0, dur_s - (now - t0))
            mb = csv_path.stat().st_size / 1e6 if csv_path.exists() else 0.0
            pct = min(100.0, 100.0 * el / dur_s) if dur_s else 0.0
            done = dt.datetime.now() + dt.timedelta(seconds=left)
            print(f"    {fmt_hms(el)} elapsed, {fmt_hms(left)} left ({pct:3.0f}%), "
                  f"{mb:.1f} MB, done ~{done:%H:%M}")
    except KeyboardInterrupt:
        # Windows delivers Ctrl-C to the whole console group, so rtl_power is already
        # stopping; wait for it so the CSV is flushed before we report on it.
        proc.wait()
        raise
REPORTS = ROOT / "reports"
DEFAULTS_FILE = ROOT / "station.json"

# Fixed crop and FIR on every survey, so surveys stay comparable.
CROP = 0.25
FIR = 9

# NESDR SMArTee XTR (E4000) gain steps. -g must be one of these exactly.
XTR_GAINS = [-1.0, 1.5, 4.0, 6.5, 9.0, 11.5, 14.0, 16.5, 19.0, 21.5, 24.0, 29.0, 34.0, 42.0]
# R820T/R860 in the SMArt v5. A different tuner means a different ladder, and 29.0 -
# the XTR's usual - is not on it. librtlsdr silently snaps to the nearest step, so an
# unvalidated gain would be recorded in the sidecar as a value the hardware never used.
V5_GAINS = [0.0, 0.9, 1.4, 2.7, 3.7, 7.7, 8.7, 12.5, 14.4, 15.7, 16.6, 19.7, 20.7,
            22.9, 25.4, 28.0, 29.7, 32.8, 33.8, 36.4, 37.2, 38.6, 40.2, 42.1, 43.4,
            43.9, 44.5, 48.0, 49.6]
TUNER_GAINS = {"xtr": ("E4000", XTR_GAINS), "v5": ("R820T/R860", V5_GAINS)}

# rtl_power holds frequencies in a signed 32-bit int, so 2^31 Hz is a hard
# ceiling: above it the range is silently clamped, and a range entirely above
# it writes no file at all while still exiting 0. Measured 2026-10-13: asking
# for 2187M covered only up to 2147.483311 MHz, 337 Hz short of 2^31.
RTL_POWER_MAX_HZ = 2 ** 31
RTL_POWER_MAX_MHZ = RTL_POWER_MAX_HZ / 1e6  # 2147.483648

# Ranges match the measured limits of this unit: 52-2187 MHz, PLL gap 1095-1240.
PRESETS = {
    "LS": dict(freq="1241M:2148M:50k", gain=29.0, integration=30, receiver="xtr",
               note="PLL gap to rtl_power's 2147.48 MHz ceiling"),
    "L": dict(freq="1241M:1700M:25k", gain=34.0, integration=10, receiver="xtr",
              note="L-band detail"),
    "milair": dict(freq="225M:400M:12.5k", gain=29.0, integration=10, receiver="xtr",
                   note="Military air UHF"),
    "900": dict(freq="902M:935M:5k", gain=29.0, integration=5, receiver="xtr",
                note="ISM + paging"),
    "wide": dict(freq="52M:1700M:100k", gain=29.0, integration=5, receiver="xtr",
                 note="Overview; sweeps through the gap, analyser masks it"),
    # Only 2 hops, so retuning costs ~94 ms per sweep instead of LS's 20.3 s. That buys
    # ~95% listening at -i 2 and 2 s time resolution, which is what characterising a short
    # burst needs - a wideband survey at -i 120 can detect one but cannot time it.
    # Range starts at 1263M so the two hop centres land at 1264.05 and 1266.15 MHz, clear of
    # the 1265.44 target: at 1264M they sat exactly on 1265.000/1267.000 and the zero-IF DC
    # spikes swamped it. floor_window is small because the default 2 MHz is half this span.
    "burst23": dict(freq="1263M:1267M:5k", gain=29.0, integration=2, receiver="xtr",
                    floor_window=0.3,
                    note="1265.44 MHz burst hunt; 2 hops, ~95% listening, hop centres offset"),
    # A range built by hand (the scan builder emits this): --freq carries the real
    # range, so the sidecar records "custom" instead of pretending to be a preset.
    # One 100 MHz tile of the LS band. 49 hops instead of 432, so -i 30 still leaves
    # 93% listening and 2 h buys ~136 s on every frequency against 13.8 s for the
    # full sweep, at twice the frequency resolution. --freq picks the tile.
    "deep": dict(freq="1241M:1342M:25k", gain=29.0, integration=30, receiver="xtr",
                 note="100 MHz deep tile; --freq picks which"),
    "custom": dict(freq="1241M:2148M:50k", gain=29.0, integration=120, receiver="xtr",
                   note="custom build; --freq sets the range"),
}

# Anything here holding the device makes rtl_power fail, or worse, half-work.
SDR_PROGRAMS = [
    "sdrpp.exe", "sdrplusplus.exe", "sdrsharp.exe", "sdruno.exe", "sdrconnect.exe",
    "cubicsdr.exe", "sdrangel.exe", "gqrx.exe", "hdsdr.exe",
    "rtl_tcp.exe", "rtl_power.exe", "rtl_fm.exe", "rtl_test.exe", "rtl_adsb.exe",
    "rtl_sdr.exe", "rtl_433.exe", "satdump.exe", "soapysdrserver.exe",
]

DEFAULTS = {
    "antenna_shape": "90 L",
    "whip_length_mm": 60,  # each whip, not tip-to-tip
    "lna": "Nooelec LaNA, bias-tee powered, at the antenna pigtail",
    "filter": "Nooelec Flamingo+ FM v2 notch (88-108 MHz)",
    "chain_order": "antenna > Flamingo+ FM notch > Nooelec LaNA > 8 ft coax > SMA bulkhead > SDR (caged) > USB extension > USB bulkhead > 16 ft USB > PC",
    "conditions": "",
}


# ---------------------------------------------------------------------------
# parsing helpers
# ---------------------------------------------------------------------------
def parse_duration(text: str) -> tuple[str, int]:
    """'6h' -> ('6h', 21600). Accepts an s/m/h/d suffix, or bare seconds."""
    t = text.strip().lower().replace(" ", "")
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd]?)", t)
    if not m:
        raise SystemExit(
            f"bad duration {text!r}: use e.g. 90s, 30m, 6h, 1d, or plain seconds")
    value, suffix = float(m.group(1)), m.group(2)
    seconds = int(value * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[suffix])
    if seconds < 1:
        raise SystemExit("duration must be at least 1 second")
    return t, seconds


def parse_freq_spec(spec: str) -> tuple[float, float, float]:
    """'1241M:2187M:50k' -> (1241.0, 2187.0, 50.0) as MHz, MHz, kHz."""
    def hz(tok: str) -> float:
        m = re.fullmatch(r"(\d+(?:\.\d+)?)([kKmMgG]?)", tok.strip())
        if not m:
            raise SystemExit(f"bad frequency token {tok!r} in {spec!r}")
        mult = {"": 1.0, "k": 1e3, "m": 1e6, "g": 1e9}[m.group(2).lower()]
        return float(m.group(1)) * mult

    parts = spec.split(":")
    if len(parts) != 3:
        raise SystemExit(f"bad -f spec {spec!r}: want LO:HI:BIN, e.g. 1241M:2187M:50k")
    lo, high, bin_hz = (hz(p) for p in parts)
    if high <= lo:
        raise SystemExit(f"bad -f spec {spec!r}: high must be above low")
    if bin_hz <= 0:
        raise SystemExit(f"bad -f spec {spec!r}: bin width must be positive")
    return lo / 1e6, high / 1e6, bin_hz / 1e3


RTL_MAX_SAMPLE_HZ = 2.8e6     # rtl_power's ceiling before it splits a span into hops
RETUNE_MS_PER_HOP = 47.0      # measured 2026-10-13, constant from 50 to 432 hops


def scan_geometry(lo_mhz, hi_mhz, bin_khz, integration_s, duration_s, crop=0.25):
    """Exactly what rtl_power will do with this request.

    rtl_power does not honour the requested bin width directly:
      1. it covers the span with as few hops as possible, each hop spanning
         sample_rate * (1 - crop), with sample_rate capped at 2.8 MS/s;
      2. it then *lowers* the sample rate so those hops tile the span exactly;
      3. it picks the smallest power-of-2 FFT whose bins are no wider than asked,
         so the real bin width is sample_rate / FFT.

    Verified against three runs. LS: span 906.484 MHz -> 432 hops -> 2797789 Hz
    (rtl_power reported 2797788.08) -> FFT 64 -> 43715 Hz bins, 20736 logged.
    burst23 at 1264M: 4 MHz -> 2 hops -> 2666670 Hz -> FFT 1024 -> 2604 Hz bins,
    1536 logged, hop centres 1265.000/1267.000 - exactly where the measured DC
    spikes sat. LS's first hop centre is 1242.049 MHz, the frequency rtl_power
    reported as "PLL not locked".
    """
    span_hz = (hi_mhz - lo_mhz) * 1e6
    hops = max(1, math.ceil(span_hz / (RTL_MAX_SAMPLE_HZ * (1 - crop))))
    hop_hz = span_hz / hops
    rate_hz = hop_hz / (1 - crop)
    fft = 1
    while rate_hz / fft > bin_khz * 1000.0:
        fft *= 2
    kept = int(round(fft * (1 - crop)))
    retune_s = hops * RETUNE_MS_PER_HOP / 1000.0
    sweeps = max(1, int(duration_s // max(1, integration_s)))
    listen_each = integration_s / hops - RETUNE_MS_PER_HOP / 1000.0
    return {
        "hops": hops,
        "hop_mhz": hop_hz / 1e6,
        "sample_rate_hz": rate_hz,
        "fft": fft,
        "bin_khz_predicted": rate_hz / fft / 1e3,
        "kept_bins_per_hop": kept,
        "bins": hops * kept,
        "sweeps": sweeps,
        "retune_s_per_sweep": retune_s,
        "listening_frac": max(0.0, 1.0 - retune_s / max(1e-9, integration_s)),
        "listen_per_freq_s": max(0.0, sweeps * listen_each),
        "occupancy_step_pct": 100.0 / sweeps,
        "csv_mb": sweeps * hops * (60 + kept * 8) / 1e6,
        "hop_centres_mhz": [lo_mhz + hop_hz / 1e6 * (i + 0.5) for i in range(hops)],
    }


def actual_bin_width_khz(csv_path: Path) -> float | None:
    """Read the real bin width rtl_power used, from column 5 of the CSV."""
    try:
        with open(csv_path, "r") as fh:
            first = fh.readline()
        return float(first.split(",")[4]) / 1e3
    except (OSError, IndexError, ValueError):
        return None


# ---------------------------------------------------------------------------
# environment checks
# ---------------------------------------------------------------------------
def running_sdr_programs() -> list[str] | None:
    """Names of SDR programs running now. [] means clear, None means can't tell."""
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    wanted = {p.lower() for p in SDR_PROGRAMS}
    found: list[str] = []
    for line in out.splitlines():
        if not line.startswith('"'):
            continue
        name = line.split('","')[0].strip('"').strip().lower()
        if name in wanted and name not in found:
            found.append(name)
    return found


def check_device_free(non_interactive: bool) -> None:
    busy = running_sdr_programs()
    if busy is None:
        print("  Could not check for running radio programs; make sure SDR++ is closed.")
        return
    if not busy:
        print("  SDR is free (no other radio program running).")
        return
    print("\n  *** Another program may be holding the SDR: " + ", ".join(busy))
    print("  Only one program can use the receiver at a time. Close it first")
    print("  (in SDR++: stop the radio, then quit) or this survey will fail,")
    print("  or record nothing useful.")
    # Without a terminal there is nobody to ask, so fail safe rather than
    # record six hours of nothing.
    if non_interactive or not sys.stdin or not sys.stdin.isatty():
        raise SystemExit("  Refusing to start. Re-run once it is closed.")
    if input("  Start anyway? [y/N] ").strip().lower() not in ("y", "yes"):
        raise SystemExit("  Stopped. Nothing was recorded.")


def device_check(device: int) -> None:
    """Prove the receiver is actually there and free, before asking anything.

    `rtl_eeprom -d N` with no write flags is a read-only probe that opens the
    device, so it catches both "unplugged" and "something else has it" in one
    go, in about 1.5 s. Doing this before the prompts means you never type out
    the conditions only to have rtl_power exit instantly.
    """
    eeprom = ROOT / "rtl-sdr" / "rtl_eeprom.exe"
    if not eeprom.exists():
        print("  (rtl_eeprom.exe not found, skipping the receiver check)")
        return
    try:
        r = subprocess.run([str(eeprom), "-d", str(device)],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"  (could not run the receiver check: {exc})")
        return

    out = (r.stdout or "") + (r.stderr or "")
    # rtl_eeprom exits 1 even on a successful read, so judge by its output.
    if "No supported devices found" in out:
        raise SystemExit(
            f"\n  No receiver found on device index {device}.\n"
            "  Check the USB cable is plugged in. Nothing was recorded.")
    if "usb_open error" in out or "Failed to open" in out:
        raise SystemExit(
            f"\n  Receiver found but could not be opened on index {device}.\n"
            "  Another program (SDR++, SDR#, rtl_tcp...) probably has it.\n"
            "  Close it and try again. Nothing was recorded.")
    if "Found" not in out:
        print("  (receiver check gave no clear answer, carrying on anyway)")
        return

    product = tuner = ""
    for line in out.splitlines():
        if line.startswith("Product:"):
            product = line.split(":", 1)[1].strip()
        elif "tuner" in line and line.startswith("Found"):
            tuner = line.replace("Found", "").strip()
    print(f"  Receiver OK: {product or 'unknown'}" + (f", {tuner}" if tuner else ""))


def warn_long_run(duration_s: int) -> None:
    if duration_s >= 2 * 3600:
        print("\n  This is a long run. Check these before leaving it:")
        print("    - Windows sleep and hibernate off")
        print("    - USB selective suspend off for this hub")
        print("    - lid-close action set to 'do nothing', on a laptop")
        print("  (Windows settings; change them yourself, this script will not.)")


# ---------------------------------------------------------------------------
# conditions prompt
# ---------------------------------------------------------------------------
def load_defaults() -> dict:
    d = dict(DEFAULTS)
    if DEFAULTS_FILE.exists():
        try:
            saved = json.loads(DEFAULTS_FILE.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                d.update(saved)
        except (OSError, ValueError) as exc:
            print(f"  (ignoring unreadable {DEFAULTS_FILE.name}: {exc})")
    return d


def save_defaults(d: dict) -> None:
    keep = {k: d[k] for k in ("antenna_shape", "whip_length_mm", "lna", "filter",
                              "chain_order") if k in d}
    try:
        DEFAULTS_FILE.write_text(json.dumps(keep, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"  (could not save defaults: {exc})")


def ask_conditions(args, defaults: dict) -> dict:
    """Ask about the physical setup. Enter accepts the shown default."""
    chain = dict(defaults)
    # No terminal (scheduled run, piped input) means nobody can answer prompts.
    if args.non_interactive or not sys.stdin or not sys.stdin.isatty():
        if not args.non_interactive:
            print("  No terminal for questions; using saved station.json defaults.")
        chain["conditions"] = args.conditions or ""
        if args.chain:
            chain["chain_order"] = args.chain
        return chain

    print("\n  Station notes for the sidecar (Enter keeps the [default]):")

    def ask(label, key, cast=str):
        current = defaults.get(key, "")
        raw = input(f"    {label} [{current}]: ").strip()
        if not raw:
            return current
        try:
            return cast(raw)
        except ValueError:
            print(f"      not a number, keeping {current!r}")
            return current

    chain["antenna_shape"] = ask("antenna shape (e.g. '120 V', 'L')", "antenna_shape")
    chain["whip_length_mm"] = ask("whip length mm (each whip, not tip-to-tip)",
                                  "whip_length_mm", int)
    chain["lna"] = ask("LNA in chain", "lna")
    chain["filter"] = ask("filter in chain", "filter")
    chain["conditions"] = (args.conditions or "").strip() or input(
        "    conditions (weather, what is switched on in the room): ").strip()
    return chain


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Record an rtl_power survey from a preset, then analyse it.")
    p.add_argument("preset", nargs="?", help="preset name: " + ", ".join(PRESETS))
    p.add_argument("duration", nargs="?", help="how long to record, e.g. 30m, 6h")
    p.add_argument("--list", action="store_true", help="show the presets and exit")
    p.add_argument("--gain", type=float, help="override the preset gain (dB)")
    p.add_argument("--integration", type=int, help="override seconds per sweep")
    p.add_argument("--freq", help="override the LO:HI:BIN spec")
    p.add_argument("--device", type=int, default=0, help="rtl_power device index (default 0)")
    p.add_argument("--receiver", choices=["xtr", "v5"], help="receiver, for gap masking")
    p.add_argument("--chain", metavar="TEXT",
                   help="the physical signal chain, source first, separated by ' > '. "
                        "The scan builder emits this; it is what makes a 50 ohm load run "
                        "self-describing instead of relying on stale station.json text.")
    p.add_argument("--conditions", help="free-text conditions, skips that prompt")
    p.add_argument("--tag", help="extra word in the filename, e.g. lna-off")
    p.add_argument("--threshold", type=float, help="passed through to survey.py analyze")
    p.add_argument("--non-interactive", action="store_true",
                   help="ask nothing, use saved defaults (for scheduled runs)")
    p.add_argument("--dry-run", action="store_true", help="show what would run, record nothing")
    p.add_argument("--crop", type=float, default=CROP, metavar="F",
                   help="edge crop per hop (default 0.25; changing it breaks comparability)")
    p.add_argument("--fir", type=int, default=FIR, metavar="N",
                   help="FIR filter size (default 9; changing it breaks comparability)")
    p.add_argument("--floor-window", type=float, metavar="MHZ",
                   help="analyser floor window; the 2 MHz default is too wide "
                        "for a narrow span (overrides the preset)")
    p.add_argument("--skip-warmup", type=float, default=0.0, metavar="MIN",
                    help="discard the first MIN minutes when analysing (receiver warm-up drift)")
    p.add_argument("--no-analyze", action="store_true", help="record only, skip the analyser")
    p.add_argument("--no-live", action="store_true",
                   help="do not open the live view for this run")
    p.add_argument("--no-archive", action="store_true",
                   help="skip folding the finished report into occupancy.db")
    return p


def show_presets() -> None:
    print("\n  Presets (gain and integration are the station defaults):\n")
    for name, p in PRESETS.items():
        lo, hi, b = parse_freq_spec(p["freq"])
        print(f"    {name:<7} {lo:>7.1f} - {hi:<7.1f} MHz  bin {b:>6.1f} kHz  "
              f"-g {p['gain']:<5g} -i {p['integration']:<3d}  {p['note']}")
    print(f"\n  Every survey also uses -c {CROP} -F {FIR}.\n")


def fingerprint(sidecar, bin_khz_actual) -> str:
    """One line that two scans must share to be comparable.

    Gain, bin width, crop, FIR and integration all change measured levels or the
    frequency grid, so a difference in any of them invalidates a level or occupancy
    comparison. Range and receiver are in there too, because a different span
    changes the hop geometry and therefore the bins themselves.
    """
    rp = sidecar["rtl_power"]
    b = f"{bin_khz_actual:.4f}" if bin_khz_actual else f"~{rp['bin_khz']:g}"
    # The earliest sidecars predate freq_high_mhz_effective; fall back to what was asked.
    hi = rp.get("freq_high_mhz_effective") or rp["freq_high_mhz"]
    return (f"{rp['freq_low_mhz']:.3f}-{hi:.3f} MHz | {b} kHz | "
            f"g{rp['gain_db']:g} | "
            f"i{rp['integration_s']} | c{rp['crop']} | F{rp['fir']} | {sidecar['receiver']}")


def write_scan_build(csv_path: Path, sidecar: dict, geom: dict,
                     analyze_cmd, out_dir: Path) -> Path:
    """Write scan_build.md beside the report: how this scan was built, in full.

    The sidecar is the machine-readable record and lives with the CSV. This is the
    human sheet that travels with the report, so two scans can be checked for
    comparability without opening any JSON.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    rp, ch = sidecar["rtl_power"], sidecar["chain"]
    actual = actual_bin_width_khz(csv_path)
    c = geom.get("hop_centres_mhz") or []
    centres = (", ".join(f"{x:.4f}" for x in c[:4]) + " ... " + ", ".join(f"{x:.4f}" for x in c[-2:])
               if len(c) > 8 else ", ".join(f"{x:.4f}" for x in c) or "n/a")
    rows = [
        f"# Scan build: {csv_path.stem}", "",
        "Everything this scan was built from. Two scans can be compared directly only if",
        "their fingerprints match.", "",
        "## Comparability fingerprint", "", "```", fingerprint(sidecar, actual), "```", "",
        "## Command", "", "```", sidecar["command_line"], "```", "",
        "## Requested", "", "| Setting | Value |", "|---|---|",
        f"| Preset | {sidecar['preset']} |",
        f"| Range | {rp['freq_low_mhz']:g} - {rp['freq_high_mhz_effective']:g} MHz |",
        f"| Bin width requested | {rp['bin_khz']:g} kHz |",
        f"| Gain | {rp['gain_db']:g} dB fixed |",
        f"| Integration | {rp['integration_s']} s/sweep |",
        f"| Crop / FIR | -c {rp['crop']}  -F {rp['fir']} |",
        f"| Duration | {sidecar['duration_requested']} requested, "
        f"{(sidecar.get('duration_actual_s') or 0.0) / 60:.1f} min actual |",
        f"| Receiver | {sidecar['receiver']} |", "",
        "## Geometry rtl_power derived", "", "| Quantity | Value |", "|---|---|",
        f"| Hops | {geom['hops']} x {geom['hop_mhz']:.4f} MHz |",
        f"| Sample rate | {geom['sample_rate_hz']/1e6:.6f} MS/s |",
        f"| FFT size | {geom['fft']} ({geom['kept_bins_per_hop']} bins kept per hop after crop) |",
        f"| Bin width | {geom['bin_khz_predicted']:.4f} kHz predicted"
        + (f", {actual:.4f} kHz actual |" if actual else " |"),
        f"| Bins logged | {geom['bins']:,} |",
        f"| Hop centres (MHz) | {centres} |", "",
        "Hop centres carry the E4000's zero-IF DC spike. A target sitting on one is masked;",
        "move the range start to shift them.", "",
        "## Dwell", "", "| Quantity | Value |", "|---|---|",
        f"| Retuning per sweep | {geom['retune_s_per_sweep']:.2f} s "
        f"({geom['hops']} hops x {RETUNE_MS_PER_HOP:g} ms) |",
        f"| Listening | {geom['listening_frac']*100:.1f}% of each sweep |",
        f"| Sweeps | {geom['sweeps']:,} |",
        f"| Total listening per frequency | {geom['listen_per_freq_s']:.1f} s |",
        f"| Smallest occupancy step | {geom['occupancy_step_pct']:.2f}% (one sweep) |", "",
        "## Station", "", "| Item | Value |", "|---|---|",
        f"| Antenna | {ch.get('antenna_shape')}, {ch.get('whip_length_mm')} mm each whip |",
        f"| LNA | {ch.get('lna')} |",
        f"| Filter | {ch.get('filter')} |",
        f"| Chain | {ch.get('chain_order') or '(not recorded)'} |",
        f"| Conditions | {sidecar.get('conditions') or '(none recorded)'} |", "",
        "## Analysis", "", "```",
        subprocess.list2cmdline(analyze_cmd) if analyze_cmd else "(not analysed at record time)",
        "```", "",
        f"Recorded {(sidecar.get('csv_bytes') or 0)/1e6:.1f} MB, rtl_power exit "
        f"{sidecar.get('exit_code')}, interrupted={sidecar.get('interrupted')}.", "",
    ]
    for note in sidecar.get("corrections", []):
        rows += [f"> **Corrected after the fact:** {note}", ""]
    path = out_dir / "scan_build.md"
    path.write_text(chr(10).join(rows) + chr(10), encoding="utf-8")
    return path



def free_port(start: int = 8731, tries: int = 10) -> int | None:
    """First port in a small range nothing is listening on.

    A viewer may already be running - started by hand, or left over from the
    previous block - and it would be watching the PREVIOUS survey's file. Rather
    than reuse it and show stale data, take the next port and point the new one
    at this run.
    """
    import socket
    for port in range(start, start + tries):
        with socket.socket() as sk:
            sk.settimeout(0.2)
            if sk.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return None


def viewer_follows(port: int = 8731) -> bool:
    """True if a live viewer is already serving on `port` AND follows the newest CSV.

    An older viewer (pinned to one file) answers without the "follow" flag, and
    gets a fresh neighbour on the next port as before.
    """
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/surveys", timeout=1) as r:
            return bool(json.load(r).get("follow"))
    except Exception:
        return False


def start_live_view(csv_path: Path, idle_min: float = 5.0):
    """Reuse the viewer on 8731 if it follows the newest CSV, else launch one.

    The viewer switches to this run's CSV the moment rtl_power creates it, so it
    needs no --csv. Non-fatal: a viewer that will not start must never stop a
    survey recording.
    """
    script = Path(__file__).parent / "live_view.py"
    if not script.exists():
        return None
    if viewer_follows():
        print("  Live view: http://127.0.0.1:8731/   (already running; it follows the newest scan)")
        return None
    port = free_port()
    if port is None:
        print("  Live view: ports 8731-8740 are all busy, skipping.")
        return None
    cmd = [sys.executable, str(script),
           "--port", str(port), "--exit-after-idle", str(idle_min)]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"  Live view could not start ({exc}); recording is unaffected.")
        return None
    print(f"  Live view: http://127.0.0.1:{port}/   "
          f"(closes {idle_min:g} min after the scan ends)")
    return proc


def source_is_load(chain_order: str | None) -> bool | None:
    """Terminated or antenna, decided from the chain this run actually recorded.

    archive.py otherwise infers this from sidecar text, which CLAUDE.md records as
    often wrong because station.json carried the previous run's wording. Here the
    source is the first element of the chain the user passed for THIS run, so the
    flag is a statement of fact rather than a guess. Returns None when there is no
    chain to read, in which case the flag is left alone.
    """
    if not chain_order:
        return None
    source = chain_order.split(">")[0].strip().lower()
    if re.search(r"50\s*(ohm|[Ωω])|terminat|dummy\s*load", source):
        return True
    if "antenna" in source or "dipole" in source or "whip" in source:
        return False
    return None


def archive(stem: str, chain_order: str | None) -> None:
    """Fold the finished report into occupancy.db and record the source flag.

    Non-fatal throughout: the survey and its report already exist on disk, so an
    archive hiccup must not make the run look failed.
    """
    report_dir = REPORTS / stem
    add = [sys.executable, str(Path(__file__).parent / "archive.py"), "add", str(report_dir)]
    print("\n  Archiving:\n    " + subprocess.list2cmdline(add))
    try:
        if subprocess.run(add).returncode != 0:
            print("  Archive add failed - run it by hand; the report is unaffected.")
            return
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"  Archive add could not run ({exc}); the report is unaffected.")
        return

    load = source_is_load(chain_order)
    if load is None:
        print("  Source flag not set: the chain does not name an antenna or a 50 ohm load.\n"
              f"  Set it by hand:  py tools/archive.py mark {stem} --terminated|--antenna")
        return
    flag = "--terminated" if load else "--antenna"
    mark = [sys.executable, str(Path(__file__).parent / "archive.py"), "mark", stem, flag]
    print("    " + subprocess.list2cmdline(mark))
    try:
        subprocess.run(mark)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"  Archive mark could not run ({exc}); set it by hand.")


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.list:
        show_presets()
        return 0
    if not args.preset or not args.duration:
        parser.print_help()
        return 2
    if args.preset not in PRESETS:
        raise SystemExit(f"unknown preset {args.preset!r}. "
                         f"Known: {', '.join(PRESETS)} (--list for detail)")

    preset = PRESETS[args.preset]
    dur_text, dur_s = parse_duration(args.duration)
    freq = args.freq or preset["freq"]
    # `is None` not `or`, so --gain 0 / --integration 0 are rejected, not ignored.
    gain = preset["gain"] if args.gain is None else args.gain
    integration = preset["integration"] if args.integration is None else args.integration
    receiver = args.receiver or preset["receiver"]
    lo, hi, bin_khz = parse_freq_spec(freq)

    if integration < 1:
        raise SystemExit("--integration must be at least 1 second")
    tuner, valid = TUNER_GAINS.get(receiver, (None, None))
    if valid and gain not in valid:
        nearest = min(valid, key=lambda g: abs(g - gain))
        raise SystemExit(f"gain {gain:g} dB is not a {tuner} step. Nearest is {nearest:g}. "
                         f"Valid: {', '.join(f'{g:g}' for g in valid)}")
    # rtl_power's 32-bit ceiling. Catch it here: it fails silently, and a
    # six-hour run that writes nothing is an expensive way to find out.
    if lo >= RTL_POWER_MAX_MHZ:
        raise SystemExit(
            f"low edge {lo:g} MHz is at or above rtl_power's {RTL_POWER_MAX_MHZ:.6f} MHz "
            f"ceiling (2^31 Hz). It would write no file and still report success. "
            f"Use SDR++ or rtl_power_fftw above that.")
    effective_hi = min(hi, RTL_POWER_MAX_MHZ)
    # Asking a little past the ceiling is the idiom for "give me everything you
    # can", so only complain when a meaningful slice would go missing.
    if hi > RTL_POWER_MAX_MHZ + 1.0:
        print(f"\n  Note: {hi:g} MHz is above rtl_power's {RTL_POWER_MAX_MHZ:.6f} MHz ceiling "
              f"(2^31 Hz).\n  It will quietly record only up to there; "
              f"{hi - RTL_POWER_MAX_MHZ:.1f} MHz will be missing.")

    if not RTL_POWER.exists():
        raise SystemExit(f"rtl_power.exe not found at {RTL_POWER}")
    if not SURVEY_PY.exists() and not args.no_analyze:
        raise SystemExit(f"survey.py not found at {SURVEY_PY}")

    SURVEYS.mkdir(exist_ok=True)
    started = dt.datetime.now()
    tag = f"_{args.tag}" if args.tag else ""
    stem = f"survey_{args.preset}_{dur_text}{tag}_{started:%Y%m%d-%H%M}"
    csv_path = SURVEYS / f"{stem}.csv"
    json_path = SURVEYS / f"{stem}.json"
    if csv_path.exists():
        raise SystemExit(f"{csv_path.name} already exists. Use --tag to distinguish this run.")

    cmd = [str(RTL_POWER), "-f", freq, "-g", f"{gain:g}", "-i", str(integration),
           "-c", str(args.crop), "-F", str(args.fir), "-e", dur_text]
    if args.device:
        cmd += ["-d", str(args.device)]
    cmd.append(str(csv_path))

    geom = scan_geometry(lo, effective_hi, bin_khz, integration, dur_s, args.crop)
    sweeps = geom["sweeps"]
    print()
    print(f"  Survey: {args.preset}  ({preset['note']})")
    print(f"    range       {lo:g} - {effective_hi:g} MHz, {bin_khz:g} kHz bins requested")
    print(f"    geometry    {geom['hops']} hops x {geom['hop_mhz']:.4f} MHz, FFT {geom['fft']}, "
          f"{geom['bin_khz_predicted']:.4f} kHz actual bins, {geom['bins']:,} logged")
    print(f"    gain        {gain:g} dB fixed   integration {integration} s/sweep")
    print(f"    dwell       {geom['listening_frac']*100:.0f}% listening "
          f"({geom['retune_s_per_sweep']:.1f} s of every sweep retunes), "
          f"{geom['listen_per_freq_s']:.1f} s total on each frequency")
    print(f"    duration    {dur_text}  ({sweeps:,} sweeps, ~{geom['csv_mb']:.0f} MB CSV, "
          f"occupancy step {geom['occupancy_step_pct']:.2f}%)")
    if integration < geom["retune_s_per_sweep"] * 1.2:
        print(f"    WARNING     -i {integration} is under the {geom['retune_s_per_sweep']:.1f} s "
              f"retune floor for {geom['hops']} hops; rtl_power misbehaves there.")
    print(f"    crop/FIR    -c {args.crop}  -F {args.fir}" + ("" if (args.crop, args.fir) == (CROP, FIR) else "   NOT standard - not comparable with other surveys"))
    print(f"    receiver    {receiver}")
    print(f"    writing     surveys/{csv_path.name}")
    free_gb = shutil.disk_usage(SURVEYS).free / 1e9
    print(f"    disk free   {free_gb:.1f} GB")
    if geom['csv_mb'] / 1000.0 > free_gb * 0.8:
        raise SystemExit("  Not enough free disk for that estimate. Shorten the run.")

    print("\n  Command:\n    " + subprocess.list2cmdline(cmd))

    if args.dry_run:
        print("\n  --dry-run: nothing recorded.\n")
        return 0

    check_device_free(args.non_interactive)
    device_check(args.device)
    warn_long_run(dur_s)

    try:
        chain = ask_conditions(args, load_defaults())
        if args.chain:
            # An explicit chain always wins over whatever station.json remembers;
            # stale chain text is what mislabelled two earlier runs.
            chain["chain_order"] = args.chain
    except (KeyboardInterrupt, EOFError):
        raise SystemExit("\n  Cancelled at the questions. Nothing was recorded.")
    save_defaults(chain)

    sidecar = {
        "schema": 1,
        "csv": csv_path.name,
        "preset": args.preset,
        "preset_note": preset["note"],
        "started_local": started.isoformat(timespec="seconds"),
        "duration_requested": dur_text,
        "duration_requested_s": dur_s,
        "command_line": subprocess.list2cmdline(cmd),
        "command": cmd,
        "rtl_power": {
            "freq_spec": freq,
            "freq_low_mhz": lo,
            "freq_high_mhz": hi,
            "freq_high_mhz_effective": effective_hi,
            "bin_khz": bin_khz,
            "gain_db": gain,
            "integration_s": integration,
            "crop": args.crop,
            "fir": args.fir,
            "device": args.device,
        },
        "receiver": receiver,
        "chain": {k: chain.get(k) for k in ("antenna_shape", "whip_length_mm", "lna",
                                            "filter", "chain_order")},
        "conditions": chain.get("conditions", ""),
        "geometry": {k: (round(v, 6) if isinstance(v, float) else v)
                     for k, v in geom.items()
                     if k != "hop_centres_mhz" or geom["hops"] <= 16},
    }
    json_path.write_text(json.dumps(sidecar, indent=2) + "\n", encoding="utf-8")
    print(f"\n  Sidecar written: surveys/{json_path.name}")

    ends = started + dt.timedelta(seconds=dur_s)
    first = started + dt.timedelta(seconds=integration)
    print()
    print(f"  Recording until about {ends:%H:%M}. Ctrl-C stops early and keeps what it has,")
    print(f"  but nothing is written until the first {integration} s sweep completes at "
          f"{first:%H:%M:%S}.")
    if not args.no_live:
        start_live_view(csv_path)
    print()
    t0 = time.time()
    interrupted = False
    try:
        rc = run_with_progress(cmd, dur_s, csv_path)
    except KeyboardInterrupt:
        interrupted, rc = True, 130
        print("\n  Interrupted. Keeping the partial CSV.")
    elapsed = time.time() - t0

    sidecar.update({
        "finished_local": dt.datetime.now().isoformat(timespec="seconds"),
        "duration_actual_s": round(elapsed, 1),
        "exit_code": rc,
        "interrupted": interrupted,
        "csv_bytes": csv_path.stat().st_size if csv_path.exists() else 0,
    })
    # What rtl_power actually used, which is what matters when comparing surveys.
    actual_khz = actual_bin_width_khz(csv_path)
    if actual_khz is not None:
        sidecar["rtl_power"]["bin_khz_actual"] = round(actual_khz, 4)
    json_path.write_text(json.dumps(sidecar, indent=2) + "\n", encoding="utf-8")

    if not csv_path.exists() or csv_path.stat().st_size == 0:
        # No data means the sidecar describes a survey that never happened, so
        # bin it rather than leave an orphan JSON in surveys/ to puzzle over.
        json_path.unlink(missing_ok=True)
        if csv_path.exists():
            csv_path.unlink()
        if interrupted:
            # rtl_power accumulates for a whole interval before writing anything, so a
            # cancel inside the first sweep leaves a genuinely empty file. That is not a
            # fault, and the hardware questions below would only mislead.
            print(f"{chr(10)}  Stopped after {elapsed:.0f} s. At -i {integration} rtl_power "
                  "writes nothing until the first")
            print(f"  {integration} s sweep completes, so there was no data to keep. "
                  "Nothing is wrong;")
            print(f"  let the next run reach {integration} s before cancelling if you want "
                  "to keep a partial file.")
        else:
            print(f"{chr(10)}  rtl_power wrote no data, so nothing was kept.")
            print("  Is the receiver plugged in, and not in use by another program?")
        if chain.get("conditions"):
            print(f"  Your conditions text, to save retyping:\n    {chain['conditions']}")
        return rc or 1

    print(f"\n  Recorded {csv_path.stat().st_size / 1e6:.1f} MB in {elapsed / 60:.1f} min "
          f"(rtl_power exit {rc}).")

    if args.no_analyze:
        print("  Build sheet: " + str(write_scan_build(
            csv_path, sidecar, geom, None, REPORTS / csv_path.stem)))
        print("  Skipping analysis. Run it later with:\n"
              f"    {analysis_python()} tools/survey.py analyze surveys/{csv_path.name} --receiver {receiver}\n")
        return 0

    analyze = [analysis_python(), str(SURVEY_PY), "analyze", str(csv_path),
               "--receiver", receiver, "--out", str(REPORTS)]
    if args.threshold is not None:
        analyze += ["--threshold", str(args.threshold)]
    fw = args.floor_window if args.floor_window is not None else preset.get("floor_window")
    if fw:
        analyze += ["--floor-window", str(fw)]
    skip = 0
    if args.skip_warmup > 0:
        # Warm-up is a wall-clock effect, so convert minutes to whole sweeps here
        # rather than making the caller redo the arithmetic every time -i changes.
        skip = max(0, min(geom["sweeps"] - 1,
                          math.ceil(args.skip_warmup * 60 / integration)))
        if skip:
            analyze += ["--skip-sweeps", str(skip)]
    print("\n  Analysing:\n    " + subprocess.list2cmdline(analyze) + "\n")
    arc = subprocess.run(analyze).returncode
    build = write_scan_build(csv_path, sidecar, geom, analyze,
                             REPORTS / csv_path.stem)
    if arc == 0:
        print(f"\n  Done. Report: reports/{csv_path.stem}/report.md\n")
        print(f"        Build:  reports/{csv_path.stem}/{build.name}")
        if not args.no_archive:
            archive(csv_path.stem, chain.get("chain_order"))
    return arc


if __name__ == "__main__":
    sys.exit(main())
