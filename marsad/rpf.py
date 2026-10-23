"""rtl_power_fftw: the station's second recording engine.

rtl_power_fftw (github.com/AD-Vega/rtl-power-fftw, built for Windows by
packaging/rtl_power_fftw/build.cmd) does what rtl_power does - tune, FFT,
average, hop - with three differences that matter here:

  - it keeps frequencies in 64-bit, so rtl_power's 2^31 Hz ceiling
    (2147.483648 MHz) does not exist and the XTR's top slice to 2187 MHz
    can be recorded;
  - the FFT is a floating-point FFTW transform of any even size, so bins can
    be as narrow as you like (1.2 kHz at 2048 bins) for hydrogen-line work;
  - `-B FILE` subtracts a baseline spectrum and `-t SECONDS` integrates for
    as long as you like per hop.

It has no crop, no FIR and no offset tuning. Its dB scale is a power spectral
density, 10*log10(sum|X|^2 / repeats / N / sample_rate), which is NOT
rtl_power's scale; the two differ by a constant that depends on rtl_power's
own FFT size (see CLAUDE.md). That is why the comparability fingerprint names
the engine: a run from one engine is never comparable with a run from the other.

What this module holds (stdlib only, like record.py):
  plan_hops()      the hop centres rtl_power_fftw will use, its own arithmetic
  geometry()       bins, dwell and timing for the pre-run summary
  fingerprint()    the comparability line for an rtl_power_fftw sidecar
  build_command()  the exact command line
  parse_log()      what the program said on stderr (actual rate, gain, hops)
  record()         the `marsad run --engine rtl_power_fftw` path

The output parser lives in analyze.py beside rtl_power's (it needs numpy).
"""
from __future__ import annotations

import datetime as dt
import json
import math
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import host
from .config import Station, source_root
from .rtlpower import TUNER_GAINS, TUNER_LABEL, find_tool, fmt_hms, parse_duration

ENGINE = "rtl_power_fftw"
TOOL = "rtl_power_fftw"
RAW_SUFFIX = ".dat"        # rtl_power_fftw's own convention; keeps these files out
                           # of the live viewer, which only knows rtl_power CSVs
LOG_SUFFIX = ".log"        # the program's stderr: actual rate, tuning, dropped samples

# Defaults for the engine. 2.4 MS/s is exact for the RTL2832U (28.8e6 * 2^22 /
# 50331648) and the rate the rtl-sdr community treats as safe over USB; the
# program's own default of 2.0 MS/s is not exact.
DEFAULT_RATE_HZ = 2_400_000
DEFAULT_BINS = 128
DEFAULT_TIME_S = 1.0
# Per-hop overhead, measured on the station PC 2026-10-22 (CLAUDE.md,
# "rtl_power_fftw characterised"): about 59 ms to retune and start the first
# read, plus about 16 ms for every further 1.6 MB buffer read within the hop
# (the program calls rtlsdr_reset_buffer before each read). station.json can
# override both: retune_ms_per_hop_fftw, read_ms_per_buffer_fftw.
RETUNE_MS_DEFAULT = 59.0
READ_MS_DEFAULT = 16.0
BASE_BUF = 16384                 # librtlsdr read granularity, bytes
DEFAULT_BUF_BYTES = 100 * BASE_BUF   # the program's buffer when more than that is needed

# Analysis-time crop, the counterpart of rtl_power's -c 0.25: keep the middle
# 75% of every hop. rtl_power_fftw has no crop, so the recorder asks it for a
# 25% overlap between hops (-o 25) and the parser drops 12.5% at each edge.
DEFAULT_CROP = 0.25


# ---------------------------------------------------------------------------
# requests
# ---------------------------------------------------------------------------
def parse_freq(spec: str, rate_hz: float) -> tuple[float, float, float | None]:
    """'2147M:2187M' -> (2147.0, 2187.0, None); '1420.4058M' -> (lo, hi, 1420.4058).

    A single frequency means one hop centred there, so the covered range is
    the sample rate wide around it. Returned in MHz.
    """
    def hz(tok: str) -> float:
        m = re.fullmatch(r"(\d+(?:\.\d+)?)([kKmMgG]?)", tok.strip())
        if not m:
            raise SystemExit(f"bad frequency token {tok!r} in {spec!r}")
        mult = {"": 1.0, "k": 1e3, "m": 1e6, "g": 1e9}[m.group(2).lower()]
        return float(m.group(1)) * mult

    parts = spec.split(":")
    if len(parts) == 1:
        c = hz(parts[0])
        return (c - rate_hz / 2) / 1e6, (c + rate_hz / 2) / 1e6, c / 1e6
    if len(parts) == 2:
        lo, hi = hz(parts[0]), hz(parts[1])
        if hi <= lo:
            raise SystemExit(f"bad -f spec {spec!r}: high must be above low")
        return lo / 1e6, hi / 1e6, None
    raise SystemExit(f"bad frequency spec {spec!r} for {ENGINE}: want LO:HI or a single "
                     "centre frequency (the bin count is -b, not part of the range)")


def plan_hops(lo_hz: float, hi_hz: float, rate_hz: int, overlap_pct: float,
              centre_hz: float | None = None) -> list[int]:
    """The centre frequencies rtl_power_fftw will tune to, in its own arithmetic.

    From Plan::Plan in acquisition.cxx: the minimum overhang is overlap% of the
    sample rate; the hop count is the smallest that covers the span with at
    least that much overlap; the overhang is then recomputed (integer) so the
    hops tile the span exactly, first centre half a sample rate above the low
    edge. A single frequency is one hop at that frequency.
    """
    if centre_hz is not None:
        return [int(centre_hz)]
    start, stop, rate = int(lo_hz), int(hi_hz), int(rate_hz)
    min_overhang = rate * overlap_pct / 100.0
    hops = math.ceil(((stop - start) - min_overhang) / (rate - min_overhang))
    if hops <= 1:
        return [(start + stop) // 2]
    overhang = int((hops * rate - (stop - start)) / (hops - 1))
    centres = [int(start + rate / 2.0)]
    for _ in range(1, hops):
        centres.append(centres[-1] + rate - overhang)
    return centres


def reads_per_hop(bins: int, repeats: int) -> int:
    """How many rtlsdr_read_sync calls one hop takes, from Plan::Plan's buffer
    sizing: up to 1.6 MB the buffer is the data rounded up to 16 KiB and one
    read does it; beyond that the buffer is 1,638,400 bytes and the hop needs
    ceil(data / that) reads."""
    data = 2 * bins * repeats
    mult = math.ceil(data / BASE_BUF)
    if mult <= 100:
        return 1
    return math.ceil(data / DEFAULT_BUF_BYTES)


def geometry(lo_mhz: float, hi_mhz: float, rate_hz: int, bins: int, time_s: float,
             overlap_pct: float, crop: float, duration_s: float,
             retune_ms_per_hop: float = RETUNE_MS_DEFAULT, centre_mhz: float | None = None,
             read_ms_per_buffer: float = READ_MS_DEFAULT) -> dict:
    """Everything the pre-run summary and scan_build.md say about a request.

    `repeats` and the listening time per hop are the program's own figures
    (repeats = ceil(rate * t / N)); the overhead per hop (retune plus one read
    start, then one more read cost per extra buffer) was measured on this PC
    and can be overridden from station.json.
    The program ends after the pass during which the requested duration
    elapses, so the pass count is rounded up.
    """
    centres = plan_hops(lo_mhz * 1e6, hi_mhz * 1e6, rate_hz, overlap_pct,
                        None if centre_mhz is None else centre_mhz * 1e6)
    hops = len(centres)
    repeats = math.ceil(rate_hz * time_s / bins)
    listen_s = bins * repeats / rate_hz
    reads = reads_per_hop(bins, repeats)
    retune_s = (retune_ms_per_hop + read_ms_per_buffer * (reads - 1)) / 1000.0
    pass_s = hops * (listen_s + retune_s)
    passes = max(1, math.ceil(duration_s / pass_s)) if pass_s > 0 else 1
    kept = bins - 2 * int(bins * crop * 0.5)
    bin_hz = rate_hz / bins
    # One text line per bin per hop per pass, about 24 bytes each, plus headers.
    raw_mb = passes * hops * (bins * 24 + 160) / 1e6
    return {
        "hops": hops,
        "hop_mhz": rate_hz / 1e6,
        "hop_spacing_mhz": (centres[1] - centres[0]) / 1e6 if hops > 1 else rate_hz / 1e6,
        "sample_rate_hz": rate_hz,
        "fft": bins,
        "bin_khz_predicted": bin_hz / 1e3,
        "kept_bins_per_hop": kept,
        "bins": hops * kept,
        "repeats_per_hop": repeats,
        "reads_per_hop": reads,
        "overhead_per_hop_s": retune_s,
        "listen_per_hop_s": listen_s,
        "retune_s_per_sweep": hops * retune_s,
        "sweep_s": pass_s,
        "sweeps": passes,
        "listening_frac": listen_s / (listen_s + retune_s) if (listen_s + retune_s) > 0 else 0.0,
        "listen_per_freq_s": passes * listen_s * (1 + (overlap_pct / 100.0 if hops > 1 else 0)),
        "occupancy_step_pct": 100.0 / passes,
        "csv_mb": raw_mb,
        "hop_centres_mhz": [c / 1e6 for c in centres],
        "covered_mhz": [lo_mhz + bin_hz * int(bins * crop * 0.5) / 1e6,
                        hi_mhz - bin_hz * int(bins * crop * 0.5) / 1e6],
    }


def fingerprint(sidecar: dict, bin_khz_actual=None) -> str:
    """One line two rtl_power_fftw runs must share to be comparable.

    Same role as rtlpower.fingerprint for rtl_power sidecars, and deliberately
    incomparable with it: the engine name is part of the line, so no rtl_power
    run can ever match an rtl_power_fftw run.
    """
    rp = sidecar[ENGINE]
    b = bin_khz_actual or rp.get("bin_khz_actual")
    bins = f"{b:.4f}" if b else f"~{rp['bin_khz']:.4f}"
    return (f"{rp['freq_low_mhz']:.3f}-{rp['freq_high_mhz']:.3f} MHz | {bins} kHz | "
            f"g{rp['gain_db']:g} | t{rp['time_per_hop_s']:g} | N{rp['bins']} | "
            f"r{rp['rate_hz'] / 1e6:g}M | o{rp['overlap_pct']:g} | c{rp['crop']} | "
            f"{ENGINE} | {sidecar['receiver']}")


def build_command(exe: str, freq_spec: str, gain_db: float, bins: int, rate_hz: int,
                  time_s: float, overlap_pct: float, duration_s: int, device: int,
                  baseline: str | None = None, window: str | None = None,
                  ppm: int = 0) -> list[str]:
    """The rtl_power_fftw command line. Gain is in tenths of a dB for this program."""
    cmd = [exe, "-f", freq_spec, "-g", str(int(round(gain_db * 10))), "-b", str(bins),
           "-r", str(rate_hz), "-t", f"{time_s:g}", "-e", str(duration_s), "-q"]
    if overlap_pct:
        cmd += ["-o", f"{overlap_pct:g}"]
    if device:
        cmd += ["-d", str(device)]
    if ppm:
        cmd += ["-p", str(ppm)]
    if baseline:
        cmd += ["-B", str(baseline)]
    if window:
        cmd += ["-w", str(window)]
    return cmd


# ---------------------------------------------------------------------------
# what the program said
# ---------------------------------------------------------------------------
def parse_log(text: str) -> dict:
    """Facts from rtl_power_fftw's stderr.

    With -q the program prints the device set-up once and then only errors, so
    the log is short even for a long run. What matters: the sample rate the
    device actually used (the bin width is rate/N), the gain it snapped to, the
    frequencies it tuned to on the first pass, how many hops it dropped as
    untunable, and whether any readout dropped samples.
    """
    out = {"rate_hz_actual": None, "gain_tenths": None, "tuned_hz": [], "dropped_hops": [],
           "dropped_sample_errors": 0, "buffer_underruns": 0, "estimated_hop_s": None,
           "repeats": None}
    for line in text.splitlines():
        m = re.search(r"Actual sample rate:\s*(\d+)\s*Hz", line)
        if m:
            out["rate_hz_actual"] = int(m.group(1))
        m = re.search(r"Selected nearest available gain:\s*(-?\d+)", line)
        if m:
            out["gain_tenths"] = int(m.group(1))
        m = re.search(r"Device tuned to:\s*(\d+)\s*Hz", line)
        if m:
            out["tuned_hz"].append(int(m.group(1)))
        m = re.search(r"Unable to tune to\s*(\d+)", line)
        if m:
            out["dropped_hops"].append(int(m.group(1)))
        if "dropped samples" in line:
            out["dropped_sample_errors"] += 1
        m = re.search(r"Estimated time of measurements:\s*([\d.]+)", line)
        if m:
            out["estimated_hop_s"] = float(m.group(1))
        m = re.search(r"Number of averaged spectra:\s*(\d+)", line)
        if m:
            out["repeats"] = int(m.group(1))
        m = re.search(r"Buffer queue histogram:\s*(\d+)", line)
        if m and int(m.group(1)) > 0:
            out["buffer_underruns"] += int(m.group(1))
    return out


def actual_bin_width_khz(sidecar_section: dict) -> float | None:
    rate = sidecar_section.get("rate_hz_actual")
    bins = sidecar_section.get("bins")
    if rate and bins:
        return rate / bins / 1e3
    return None


# ---------------------------------------------------------------------------
# the build sheet
# ---------------------------------------------------------------------------
def write_scan_build(raw_path: Path, sidecar: dict, geom: dict, analyze_cmd,
                     out_dir: Path, retune_ms: float) -> Path:
    """scan_build.md for an rtl_power_fftw run: the same sheet record.py writes
    for rtl_power, with this engine's own settings and geometry."""
    out_dir.mkdir(parents=True, exist_ok=True)
    rp, ch = sidecar[ENGINE], sidecar["chain"]
    actual = rp.get("bin_khz_actual")
    c = geom.get("hop_centres_mhz") or []
    centres = (", ".join(f"{x:.4f}" for x in c[:4]) + " ... " + ", ".join(f"{x:.4f}" for x in c[-2:])
               if len(c) > 8 else ", ".join(f"{x:.4f}" for x in c) or "n/a")
    rows = [
        f"# Scan build: {raw_path.stem}", "",
        "Everything this scan was built from. Two scans can be compared directly only if",
        "their fingerprints match. **Engine: rtl_power_fftw** - its dB scale is a power",
        "spectral density and is not rtl_power's scale; no rtl_power run matches this one.", "",
        "## Comparability fingerprint", "", "```", fingerprint(sidecar, actual), "```", "",
        "## Command", "", "```", sidecar["command_line"], "```", "",
        "## Requested", "", "| Setting | Value |", "|---|---|",
        f"| Preset | {sidecar['preset']} |",
        f"| Range | {rp['freq_low_mhz']:g} - {rp['freq_high_mhz']:g} MHz"
        + (f" (one hop centred on {rp['centre_mhz']:g} MHz) |" if rp.get("centre_mhz") else " |"),
        f"| FFT bins per hop | {rp['bins']} |",
        f"| Sample rate | {rp['rate_hz'] / 1e6:g} MS/s requested"
        + (f", {rp['rate_hz_actual'] / 1e6:.6f} actual |" if rp.get("rate_hz_actual") else " |"),
        f"| Gain | {rp['gain_db']:g} dB fixed (-g {int(round(rp['gain_db'] * 10))}) |",
        f"| Integration | {rp['time_per_hop_s']:g} s per hop (-t) |",
        f"| Overlap / crop | -o {rp['overlap_pct']:g}, middle {100 * (1 - rp['crop']):g}% of each hop kept at analysis |",
        f"| Baseline | {rp.get('baseline') or 'none'} |",
        f"| Window | {rp.get('window') or 'none (rectangular)'} |",
        f"| Duration | {sidecar['duration_requested']} requested, "
        f"{(sidecar.get('duration_actual_s') or 0.0) / 60:.1f} min actual |",
        f"| Receiver | {sidecar['receiver']} |", "",
        "## Geometry rtl_power_fftw derived", "", "| Quantity | Value |", "|---|---|",
        f"| Hops | {geom['hops']} x {geom['hop_mhz']:.4f} MHz, spaced {geom['hop_spacing_mhz']:.4f} MHz |",
        f"| Bin width | {geom['bin_khz_predicted']:.4f} kHz predicted"
        + (f", {actual:.4f} kHz actual |" if actual else " |"),
        f"| Bins kept per hop | {geom['kept_bins_per_hop']} of {geom['fft']} |",
        f"| Bins logged | {geom['bins']:,} |",
        f"| Covered after crop | {geom['covered_mhz'][0]:.4f} - {geom['covered_mhz'][1]:.4f} MHz |",
        f"| Spectra averaged per hop | {geom['repeats_per_hop']} |",
        f"| Hop centres (MHz) | {centres} |", "",
        "Hop centres carry the E4000's zero-IF DC spike; rtl_power_fftw replaces the centre",
        "bin by its neighbours' mean, but the spike's skirt is wider than one bin.", "",
        "## Dwell", "", "| Quantity | Value |", "|---|---|",
        f"| Listening per hop | {geom['listen_per_hop_s']:.3f} s |",
        f"| Overhead per pass | {geom['retune_s_per_sweep']:.2f} s "
        f"({geom['hops']} hops x {geom['overhead_per_hop_s'] * 1000:.0f} ms: {retune_ms:g} ms retune "
        f"+ {geom['reads_per_hop'] - 1} extra buffer reads) |",
        f"| One pass | {geom['sweep_s']:.1f} s |",
        f"| Listening | {geom['listening_frac'] * 100:.1f}% |",
        f"| Passes (sweeps) | {geom['sweeps']:,} planned; the run ends after the pass in which the duration elapses |",
        f"| Total listening per frequency | {geom['listen_per_freq_s']:.1f} s |",
        f"| Smallest occupancy step | {geom['occupancy_step_pct']:.2f}% (one pass) |", "",
        "## Station", "", "| Item | Value |", "|---|---|",
        f"| Antenna | {ch.get('antenna_shape')}, {ch.get('whip_length_mm')} mm each whip |",
        f"| LNA | {ch.get('lna')} |",
        f"| Filter | {ch.get('filter')} |",
        f"| Chain | {ch.get('chain_order') or '(not recorded)'} |",
        f"| Conditions | {sidecar.get('conditions') or '(none recorded)'} |", "",
        "## Analysis", "", "```",
        subprocess.list2cmdline(analyze_cmd) if analyze_cmd else "(not analysed at record time)",
        "```", "",
        f"Recorded {(sidecar.get('csv_bytes') or 0) / 1e6:.1f} MB, rtl_power_fftw exit "
        f"{sidecar.get('exit_code')}, interrupted={sidecar.get('interrupted')}.",
    ]
    log = sidecar.get("log") or {}
    if log:
        rows += ["", f"The program reported: actual sample rate {log.get('rate_hz_actual')} Hz, "
                     f"gain {log.get('gain_tenths')} tenths, {len(log.get('tuned_hz') or [])} hops "
                     f"tuned on the first pass, {len(log.get('dropped_hops') or [])} dropped as "
                     f"untunable, {log.get('dropped_sample_errors')} dropped-sample errors, "
                     f"{log.get('buffer_underruns')} buffer underruns."]
    rows.append("")
    for note in sidecar.get("corrections", []):
        rows += [f"> **Corrected after the fact:** {note}", ""]
    path = out_dir / "scan_build.md"
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# recording
# ---------------------------------------------------------------------------
def run_recording(cmd: list, dur_s: float, raw_path: Path, log_path: Path,
                  stop_file: Path | None = None) -> int:
    """Run rtl_power_fftw with stdout into the .dat file and stderr into the .log.

    Mirrors record.run_with_progress: a progress line now and then, and the
    web app's stop file asks the program to finish its pass (Ctrl-Break, which
    the Windows build is patched to honour like Ctrl-C).
    """
    from .record import progress_every, stop_child
    every = progress_every(dur_s)
    t0 = time.time()
    nxt = t0 + every
    own_group = stop_file is not None
    with open(raw_path, "w", encoding="ascii", newline="\n") as raw, \
            open(log_path, "w", encoding="utf-8") as log:
        proc = subprocess.Popen(cmd, stdout=raw, stderr=log,
                                creationflags=host.popen_group_flags() if own_group else 0)
        try:
            while True:
                try:
                    return proc.wait(timeout=0.5 if own_group else max(0.5, nxt - time.time()))
                except subprocess.TimeoutExpired:
                    pass
                if own_group and stop_file.exists():
                    stop_file.unlink(missing_ok=True)
                    print("\n  Stop requested from the web app; asking rtl_power_fftw to finish its pass.")
                    stop_child(proc)
                    raise KeyboardInterrupt
                now = time.time()
                if now < nxt:
                    continue
                nxt = now + every
                el, left = now - t0, max(0.0, dur_s - (now - t0))
                mb = raw_path.stat().st_size / 1e6 if raw_path.exists() else 0.0
                pct = min(100.0, 100.0 * el / dur_s) if dur_s else 0.0
                done = dt.datetime.now() + dt.timedelta(seconds=left)
                print(f"    {fmt_hms(el)} elapsed, {fmt_hms(left)} left ({pct:3.0f}%), "
                      f"{mb:.1f} MB, done ~{done:%H:%M}")
        except KeyboardInterrupt:
            if own_group and proc.poll() is None:
                stop_child(proc)
            proc.wait()
            raise


def add_arguments(p) -> None:
    """The engine's own options on `marsad run`. Harmless for rtl_power presets."""
    p.add_argument("--engine", choices=["rtl_power", ENGINE],
                   help="recording program; a preset may name one (default rtl_power)")
    p.add_argument("--bins", type=int, metavar="N",
                   help=f"{ENGINE}: FFT bins per hop (even; preset or {DEFAULT_BINS})")
    p.add_argument("--time", type=float, metavar="S",
                   help=f"{ENGINE}: integration seconds per hop, -t (preset or {DEFAULT_TIME_S:g})")
    p.add_argument("--rate", type=int, metavar="HZ",
                   help=f"{ENGINE}: sample rate (preset or {DEFAULT_RATE_HZ})")
    p.add_argument("--overlap", type=float, metavar="PCT",
                   help=f"{ENGINE}: minimum hop overlap, -o (default 100 x crop)")
    p.add_argument("--baseline", metavar="FILE",
                   help=f"{ENGINE}: subtract this baseline spectrum, -B (single-hop runs)")
    p.add_argument("--window", metavar="FILE",
                   help=f"{ENGINE}: window function values, -w (N lines)")


def record(args, station: Station, preset: dict) -> int:
    """`marsad run <preset> <duration> --engine rtl_power_fftw`, start to finish.

    Same shape as record.main for rtl_power: checks, questions, sidecar before
    recording, the run, the sidecar again, analysis, build sheet, archive.
    """
    from . import record as rec

    dur_text, dur_s = parse_duration(args.duration)
    rate = args.rate or preset.get("rate") or DEFAULT_RATE_HZ
    bins = args.bins or preset.get("bins") or DEFAULT_BINS
    time_s = args.time if args.time is not None else float(preset.get("time", DEFAULT_TIME_S))
    crop = args.crop if args.crop is not None else float(preset.get("crop", DEFAULT_CROP))
    overlap = (args.overlap if args.overlap is not None
               else float(preset.get("overlap", 100.0 * crop)))
    gain = preset["gain"] if args.gain is None else args.gain
    receiver = args.receiver or preset["receiver"]
    freq = args.freq or preset["freq"]
    baseline = args.baseline or preset.get("baseline")
    window = args.window or preset.get("window")

    if bins % 2:
        raise SystemExit(f"--bins {bins}: {ENGINE} wants an even number of bins")
    if bins < 2 or time_s <= 0 or rate <= 0:
        raise SystemExit("bins, --time and --rate must all be positive")
    if not (0 <= crop < 0.5):
        raise SystemExit("--crop must be between 0 and 0.5")
    lo, hi, centre = parse_freq(freq, rate)
    if centre is None and overlap < 100.0 * crop - 1e-9:
        raise SystemExit(f"--overlap {overlap:g}% is less than the crop needs: with --crop "
                         f"{crop:g} the hops must overlap by at least {100 * crop:g}% or the "
                         "cropped edges leave gaps between hops")
    tuner = station.tuner_of(receiver)
    valid = TUNER_GAINS.get(tuner or "")
    if valid and gain not in valid:
        nearest = min(valid, key=lambda g: abs(g - gain))
        raise SystemExit(f"gain {gain:g} dB is not a {TUNER_LABEL.get(tuner, tuner)} step. "
                         f"Nearest is {nearest:g}. Valid: {', '.join(f'{g:g}' for g in valid)}")
    if args.fir != 9 or (args.crop is not None and args.crop != DEFAULT_CROP):
        print(f"\n  Note: {ENGINE} has no FIR; --crop is applied at analysis time.")
    if baseline and centre is None:
        print("\n  Note: --baseline subtracts the same N values from EVERY hop (by bin index),"
              "\n  which only means something for a single-hop run. Recorded as asked.")
    for b in (baseline, window):
        if b and not Path(b).exists():
            raise SystemExit(f"{b}: file not found")

    exe = find_tool(TOOL, station.tool_dirs())
    if exe is None and not args.dry_run:
        raise SystemExit(
            f"{TOOL} not found. Build it with packaging\\rtl_power_fftw\\build.cmd and put "
            f"the exe in {station.root / station.data.get('rtl_sdr_dir', 'rtl-sdr')}.")

    station.surveys.mkdir(parents=True, exist_ok=True)
    started = dt.datetime.now()
    tag = f"_{args.tag}" if args.tag else ""
    stem = f"survey_{args.preset}_{dur_text}{tag}_{started:%Y%m%d-%H%M}"
    raw_path = station.surveys / f"{stem}{RAW_SUFFIX}"
    log_path = station.surveys / f"{stem}{LOG_SUFFIX}"
    json_path = station.surveys / f"{stem}.json"
    if raw_path.exists():
        raise SystemExit(f"{raw_path.name} already exists. Use --tag to distinguish this run.")

    cmd = build_command(str(exe or TOOL), freq, gain, bins, rate, time_s, overlap, dur_s,
                        args.device, baseline, window)
    # Every run of this engine can be stopped from the Scan page: the recorder polls
    # this file and sends Ctrl-Break, which the Windows build is patched to treat
    # like Ctrl-C (finish the pass, write it, exit). --stop-file (the Probe tab) wins.
    stop_file = args.stop_file or (station.reports / f"{stem}.stop")
    retune_ms = float(station.data.get("retune_ms_per_hop_fftw", RETUNE_MS_DEFAULT))
    read_ms = float(station.data.get("read_ms_per_buffer_fftw", READ_MS_DEFAULT))
    geom = geometry(lo, hi, rate, bins, time_s, overlap, crop, dur_s, retune_ms, centre, read_ms)

    print()
    print(f"  Survey: {args.preset}  ({preset.get('note', '')})   engine: {ENGINE}")
    if centre is not None:
        print(f"    range       one hop centred on {centre:g} MHz, {lo:.4f} - {hi:.4f} MHz")
    else:
        print(f"    range       {lo:g} - {hi:g} MHz")
    print(f"    geometry    {geom['hops']} hops x {geom['hop_mhz']:.4f} MHz (spaced "
          f"{geom['hop_spacing_mhz']:.4f}), {bins} bins, {geom['bin_khz_predicted']:.4f} kHz bins, "
          f"{geom['bins']:,} logged after crop")
    print(f"    gain        {gain:g} dB fixed   integration {time_s:g} s per hop "
          f"({geom['repeats_per_hop']} spectra averaged)")
    print(f"    dwell       one pass {geom['sweep_s']:.1f} s, {geom['listening_frac'] * 100:.0f}% listening "
          f"({geom['overhead_per_hop_s'] * 1000:.0f} ms overhead per hop: {retune_ms:g} ms retune "
          f"+ {geom['reads_per_hop'] - 1} extra reads x {read_ms:g} ms)")
    print(f"    duration    {dur_text}  (~{geom['sweeps']:,} passes, ~{geom['csv_mb']:.0f} MB, "
          f"occupancy step {geom['occupancy_step_pct']:.2f}%)")
    print(f"    crop        {crop:g} at analysis (-o {overlap:g} overlap recorded)"
          + ("" if crop == DEFAULT_CROP else "   NOT standard"))
    if baseline:
        print(f"    baseline    {baseline}")
    if window:
        print(f"    window      {window}")
    print(f"    receiver    {receiver}")
    chain_src = ("--chain" if args.chain else "saved default")
    print(f"    chain       ({chain_src}) {args.chain or station.defaults.get('chain_order') or '(none)'}")
    print(f"    writing     surveys/{raw_path.name}  (+ .log with the program's own messages)")
    free_gb = shutil.disk_usage(station.surveys).free / 1e9
    print(f"    disk free   {free_gb:.1f} GB")
    if geom["csv_mb"] / 1000.0 > free_gb * 0.8:
        raise SystemExit("  Not enough free disk for that estimate. Shorten the run.")
    if exe is None:
        print(f"    {TOOL}   NOT FOUND - a real run would refuse to start")
    print("\n  Command:\n    " + subprocess.list2cmdline(cmd))
    if args.dry_run:
        print("\n  --dry-run: nothing recorded.\n")
        return 0

    try:
        chain_text = rec.confirm_default_chain(station, args)
    except (KeyboardInterrupt, EOFError):
        raise SystemExit("\n  Cancelled. Nothing was recorded.")
    rec.check_device_free(args.non_interactive)
    rec.device_check(station, args.device)
    rec.warn_long_run(dur_s)
    try:
        chain = rec.ask_conditions(args, station.defaults)
        chain["chain_order"] = chain_text
    except (KeyboardInterrupt, EOFError):
        raise SystemExit("\n  Cancelled at the questions. Nothing was recorded.")
    if not args.keep_defaults:
        station.save_defaults({**chain, "chain_used_at": started.isoformat(timespec="seconds")})
        from . import chain as chain_mod
        chain_mod.mark_text_used(station, chain_text)

    sidecar = {
        "schema": 1,
        "engine": ENGINE,
        "csv": raw_path.name,
        "log": None,
        "preset": args.preset,
        "preset_note": preset.get("note", ""),
        "started_local": started.isoformat(timespec="seconds"),
        "duration_requested": dur_text,
        "duration_requested_s": dur_s,
        "command_line": subprocess.list2cmdline(cmd),
        "command": cmd,
        "stop_file": str(stop_file),
        ENGINE: {
            "freq_spec": freq,
            "freq_low_mhz": round(lo, 6),
            "freq_high_mhz": round(hi, 6),
            "freq_high_mhz_effective": round(hi, 6),
            "centre_mhz": centre,
            "bins": bins,
            "rate_hz": rate,
            "time_per_hop_s": time_s,
            "overlap_pct": overlap,
            "crop": crop,
            "gain_db": gain,
            "bin_khz": geom["bin_khz_predicted"],
            "device": args.device,
            "baseline": baseline,
            "window": window,
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
    print()
    print(f"  Recording until about {ends:%H:%M} (the run ends after the pass in progress).")
    print(f"  Ctrl-C finishes the current pass and keeps it; a second Ctrl-C stops at once.")
    if not args.no_live:
        print("  Live view: not available for this engine (the Scan page reads rtl_power CSVs).")
    print()
    t0 = time.time()
    interrupted = False
    stop_file.parent.mkdir(parents=True, exist_ok=True)
    stop_file.unlink(missing_ok=True)       # a stale flag must not stop this run at once
    try:
        rc = run_recording(cmd, dur_s, raw_path, log_path, stop_file=stop_file)
    except KeyboardInterrupt:
        interrupted, rc = True, 130
        print("\n  Interrupted. Keeping what was written.")
    elapsed = time.time() - t0
    stop_file.unlink(missing_ok=True)

    log = {}
    try:
        log = parse_log(log_path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        pass
    sec = sidecar[ENGINE]
    if log.get("rate_hz_actual"):
        sec["rate_hz_actual"] = log["rate_hz_actual"]
        sec["bin_khz_actual"] = round(log["rate_hz_actual"] / bins / 1e3, 4)
    if log.get("gain_tenths") is not None:
        sec["gain_db_actual"] = log["gain_tenths"] / 10.0
    sidecar["log"] = log
    sidecar.update({
        "finished_local": dt.datetime.now().isoformat(timespec="seconds"),
        "duration_actual_s": round(elapsed, 1),
        "exit_code": rc,
        "interrupted": interrupted,
        "csv_bytes": raw_path.stat().st_size if raw_path.exists() else 0,
    })
    json_path.write_text(json.dumps(sidecar, indent=2) + "\n", encoding="utf-8")

    if not raw_path.exists() or raw_path.stat().st_size == 0:
        json_path.unlink(missing_ok=True)
        if raw_path.exists():
            raw_path.unlink()
        print(f"\n  {TOOL} wrote no data, so nothing was kept. Its messages are in "
              f"surveys/{log_path.name}:")
        try:
            print("    " + "\n    ".join(log_path.read_text(errors="replace").splitlines()[-8:]))
        except OSError:
            pass
        if chain.get("conditions"):
            print(f"  Your conditions text, to save retyping:\n    {chain['conditions']}")
        return rc or 1

    print(f"\n  Recorded {raw_path.stat().st_size / 1e6:.1f} MB in {elapsed / 60:.1f} min "
          f"({TOOL} exit {rc}).")
    if log.get("dropped_hops"):
        print(f"  {len(log['dropped_hops'])} hop(s) could not be tuned and were dropped: "
              + ", ".join(f"{h / 1e6:.3f}" for h in log["dropped_hops"]) + " MHz")
    if log.get("dropped_sample_errors"):
        print(f"  {log['dropped_sample_errors']} dropped-sample error(s) reported; see the .log")

    tag_text = f" {args.tag}" if args.tag else ""
    if not args.no_notify:
        state = "interrupted" if interrupted else ("finished" if rc == 0 else f"ended with exit code {rc}")
        rec.notify(f"Scan{tag_text} {state}",
                   f"{raw_path.name}  -  {elapsed / 60:.0f} min recorded, analysing now")

    if args.no_analyze:
        print("  Build sheet: " + str(write_scan_build(
            raw_path, sidecar, geom, None, station.reports / raw_path.stem, retune_ms)))
        print("  Skipping analysis. Run it later with:\n"
              f"    marsad analyze surveys/{raw_path.name} --receiver {receiver}\n")
        return 0

    analyze = host.self_command("analyze", "--station", str(station.root), str(raw_path),
                                "--receiver", receiver, "--out", str(station.reports),
                                prefer_venv=station.venv_python)
    if args.threshold is not None:
        analyze += ["--threshold", str(args.threshold)]
    fw = args.floor_window if args.floor_window is not None else preset.get("floor_window")
    if fw:
        analyze += ["--floor-window", str(fw)]
    if args.skip_warmup > 0:
        skip = max(0, min(geom["sweeps"] - 1, math.ceil(args.skip_warmup * 60 / geom["sweep_s"])))
        if skip:
            analyze += ["--skip-sweeps", str(skip)]
    print("\n  Analysing:\n    " + subprocess.list2cmdline(analyze) + "\n")
    arc = subprocess.run(analyze, cwd=source_root() or None).returncode
    build = write_scan_build(raw_path, sidecar, geom, analyze,
                             station.reports / raw_path.stem, retune_ms)
    if not args.no_notify:
        rec.notify(f"Report ready{tag_text}" if arc == 0 else f"Analysis failed{tag_text}",
                   f"reports/{raw_path.stem}/report.md" if arc == 0 else f"analyser exit code {arc}")
    if arc == 0:
        print(f"\n  Done. Report: reports/{raw_path.stem}/report.md\n")
        print(f"        Build:  reports/{raw_path.stem}/{build.name}")
        if not args.no_archive:
            rec.archive_run(station, raw_path.stem, chain.get("chain_order"))
    return arc
