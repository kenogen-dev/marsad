"""Facts about rtl_power that are true on every machine.

Nothing in this module is particular to one station. The things that ARE -
the receiver's tuning gap, which spur families to tag, the measured retune
time - live in station.json and are loaded by `config.py`.
"""
from __future__ import annotations

import math
import os
import re
import shutil
import sys
from pathlib import Path

# rtl_power holds frequencies in a signed 32-bit int, so 2^31 Hz is a hard
# ceiling: above it the range is silently clamped, and a range entirely above
# it writes no file at all while still exiting 0. Measured 2026-10-13: asking
# for 2187M covered only up to 2147.483311 MHz, 337 Hz short of 2^31.
RTL_POWER_MAX_HZ = 2 ** 31
RTL_POWER_MAX_MHZ = RTL_POWER_MAX_HZ / 1e6  # 2147.483648

# The same 32-bit int bites the span: before dividing it into hops, rtl_power
# scales the WHOLE span by 1/(1 - crop) to undo the crop, and that product must
# also fit in 2^31. Measured 2026-10-20 at -c 0.25: 52M:1662.6M plans 767 hops,
# 52M:1662.7M plans "1 hop, 0 Hz bandwidth" and rtl_power exits with "Invalid
# sample rate: 0 Hz". At -c 0.1 the limit moves to 1932.7 MHz, as the formula says.
def rtl_power_max_span_mhz(crop: float) -> float:
    return RTL_POWER_MAX_MHZ * (1.0 - crop)


# rtl_power's ceiling on sample rate before it splits a span into hops.
RTL_MAX_SAMPLE_HZ = 2.8e6

# Fixed crop and FIR by convention, so surveys stay comparable.
CROP = 0.25
FIR = 9

# Tuner gain ladders. librtlsdr silently snaps an unlisted gain to the nearest
# step, so an unvalidated -g would be recorded in the sidecar as a value the
# hardware never used. These are properties of the tuner chip, not the station.
TUNER_GAINS = {
    "e4000": [-1.0, 1.5, 4.0, 6.5, 9.0, 11.5, 14.0, 16.5, 19.0, 21.5, 24.0, 29.0, 34.0, 42.0],
    "r820t": [0.0, 0.9, 1.4, 2.7, 3.7, 7.7, 8.7, 12.5, 14.4, 15.7, 16.6, 19.7, 20.7,
              22.9, 25.4, 28.0, 29.7, 32.8, 33.8, 36.4, 37.2, 38.6, 40.2, 42.1, 43.4,
              43.9, 44.5, 48.0, 49.6],
}
TUNER_GAINS["r860"] = TUNER_GAINS["r820t"]      # same ladder, newer silicon
TUNER_LABEL = {"e4000": "E4000", "r820t": "R820T/R860", "r860": "R820T/R860"}

# Programs that hold an RTL-SDR open. Any of these running makes rtl_power
# fail, or worse, half-work. Listed without .exe; the host module adds it.
SDR_PROGRAMS = [
    "sdrpp", "sdrplusplus", "sdrsharp", "sdruno", "sdrconnect",
    "cubicsdr", "sdrangel", "gqrx", "hdsdr",
    "rtl_tcp", "rtl_power", "rtl_power_gainfix", "rtl_fm", "rtl_test", "rtl_adsb",
    "rtl_sdr", "rtl_433", "satdump", "soapysdrserver",
]


# ---------------------------------------------------------------------------
# parsing
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


def fmt_hms(sec: float) -> str:
    sec = int(round(max(0.0, sec)))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ---------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------
def scan_geometry(lo_mhz, hi_mhz, bin_khz, integration_s, duration_s, crop=CROP,
                  retune_ms_per_hop: float = 47.0):
    """Exactly what rtl_power will do with this request.

    rtl_power does not honour the requested bin width directly:
      1. it covers the span with as few hops as possible, each hop spanning
         sample_rate * (1 - crop), with sample_rate capped at 2.8 MS/s;
      2. it then *lowers* the sample rate so those hops tile the span exactly;
      3. it picks the smallest power-of-2 FFT whose bins are no wider than asked,
         so the real bin width is sample_rate / FFT.

    The retune cost per hop is the one station-specific input: it depends on
    the USB stack and should be measured (`marsad calibrate-retune`), not assumed.
    The 47 ms default is what one Windows PC measured in 2026.

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
    retune_s = hops * retune_ms_per_hop / 1000.0
    sweeps = max(1, int(duration_s // max(1, integration_s)))
    listen_each = integration_s / hops - retune_ms_per_hop / 1000.0
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


def csv_bin_khz(lo_mhz: float, hi_mhz: float, bin_khz: float, crop: float = CROP) -> float:
    """The bin width rtl_power will write in its CSV (column 5), in kHz to 4 places, as
    the sidecar's bin_khz_actual and the fingerprint carry it.

    scan_geometry works in floating point and can differ from it in the 4th decimal
    (LS: 43.7155 against the CSV's 43.7154), because rtl_power does this in integers:
    the span divided by the hop count with integer division, the rate truncated to an
    int, a top above the ceiling taken as 2^31 - 1, and the step printed to 0.01 Hz.
    Checked against all 17 distinct rtl_power ranges archived by 2026-10-30: 17 of 17.
    """
    lower = int(round(lo_mhz * 1e6))
    upper = min(int(round(hi_mhz * 1e6)), 2 ** 31 - 1)
    bw_used = 0
    for hops in range(1, 1500):
        bw_used = int(((upper - lower) // hops) / (1.0 - crop))
        if bw_used <= RTL_MAX_SAMPLE_HZ:
            break
    e = 0
    while bw_used / (1 << e) > bin_khz * 1e3:
        e += 1
    return round(round(bw_used / (1 << e), 2) / 1e3, 4)


def actual_bin_width_khz(csv_path) -> float | None:
    """Read the real bin width rtl_power used, from column 5 of the CSV."""
    try:
        with open(csv_path, "r") as fh:
            first = fh.readline()
        return float(first.split(",")[4]) / 1e3
    except (OSError, IndexError, ValueError):
        return None


def engine_of(sidecar: dict | None) -> str:
    """Which program recorded a survey. Sidecars before 2026-10-21 have no
    `engine` key and are all rtl_power."""
    return (sidecar or {}).get("engine") or "rtl_power"


def engine_section(sidecar: dict | None) -> dict:
    """The sidecar section holding the recording settings, whichever engine."""
    return (sidecar or {}).get(engine_of(sidecar), {}) or {}


def fingerprint(sidecar: dict, bin_khz_actual) -> str:
    """One line that two scans must share to be comparable.

    Gain, bin width, crop, FIR and integration all change measured levels or the
    frequency grid, so a difference in any of them invalidates a level or occupancy
    comparison. Range and receiver are in there too, because a different span
    changes the hop geometry and therefore the bins themselves.
    """
    if engine_of(sidecar) == "rtl_power_fftw":
        from .rpf import fingerprint as rpf_fingerprint
        return rpf_fingerprint(sidecar, bin_khz_actual)
    rp = sidecar["rtl_power"]
    b = f"{bin_khz_actual:.4f}" if bin_khz_actual else f"~{rp['bin_khz']:g}"
    # The earliest sidecars predate freq_high_mhz_effective; fall back to what was asked.
    hi = rp.get("freq_high_mhz_effective") or rp["freq_high_mhz"]
    build = build_token(sidecar)
    return (f"{rp['freq_low_mhz']:.3f}-{hi:.3f} MHz | {b} kHz | "
            f"g{rp['gain_db']:g} | "
            f"i{rp['integration_s']} | c{rp['crop']} | F{rp['fir']} | {sidecar['receiver']}"
            + (f" | {build}" if build else ""))


# ---------------------------------------------------------------------------
# finding the rtl-sdr executables
# ---------------------------------------------------------------------------
def exe_name(tool: str) -> str:
    return f"{tool}.exe" if os.name == "nt" else tool


def find_tool(tool: str, search_dirs: list[Path]) -> Path | None:
    """Locate rtl_power, rtl_adsb, rtl_eeprom etc.

    Looks in the configured folders first (the station's own rtl-sdr/ copy, the
    folder beside the executable), then on PATH. Returns None when it is not
    installed anywhere, so the caller can say so in plain words.
    """
    name = exe_name(tool)
    for d in search_dirs:
        if d and (Path(d) / name).exists():
            return Path(d) / name
    hit = shutil.which(tool)
    return Path(hit) if hit else None


# Which rtl_power marsad runs (2026-10-27). rtl_power_gainfix is rtl_power with
# two fixes (packaging/rtl_power_gainfix/BUILD.md): the R820T/R860 tuner gain is
# set again after the first tune, because the stock program set it before and a
# driver re-init threw it away, leaving the v5 on automatic gain whatever -g said;
# and the per-bin power sums are 64-bit, so strong carriers no longer overflow to
# -nan(ind). Measured: v5 matches rtl_sdr to 0.1 dB at four gains, XTR matches
# the stock build to 0.05 dB on the floor.
RTL_POWER_GAINFIX = "rtl_power_gainfix"
RTL_POWER_BUILDS = {RTL_POWER_GAINFIX: "gainfix", "rtl_power": "original"}
# Tuners whose gain the stock rtl_power lost. Their gainfix runs carry a build
# token in the fingerprint; the E4000 behaved the same under both builds.
GAIN_LOST_TUNERS = ("r820t", "r860")


def build_label(exe: Path | str | None) -> str:
    """'gainfix', 'original', or the bare file name for anything else."""
    stem = Path(str(exe or "rtl_power")).stem.lower()
    return RTL_POWER_BUILDS.get(stem, stem)


def choose_rtl_power(override: str | None, search_dirs: list[Path],
                     root: Path | None = None) -> tuple[Path | None, str | None]:
    """The rtl_power executable to run, and a one-line warning or None.

    `override` is station.json's `rtl_power_exe`: a name ("rtl_power" to choose
    the stock build on purpose) or a path, relative to the station folder. Without
    it, rtl_power_gainfix is used when it is installed and the stock rtl_power,
    with a warning, when it is not.
    """
    if override:
        p = Path(override)
        if p.parent != Path("."):
            p = p if p.is_absolute() or root is None else root / p
            if p.exists():
                return p, None
            return None, f"rtl_power_exe in station.json points at {p}, which does not exist"
        return find_tool(p.stem if p.suffix.lower() == ".exe" else p.name, search_dirs), None
    fixed = find_tool(RTL_POWER_GAINFIX, search_dirs)
    if fixed:
        return fixed, None
    stock = find_tool("rtl_power", search_dirs)
    if stock:
        return stock, (f"{exe_name(RTL_POWER_GAINFIX)} not found, using the stock "
                       f"{stock.name}: v5 (R820T) gain will be ignored and strong carriers "
                       "may read -nan. Build it with packaging/rtl_power_gainfix/build.cmd.")
    return None, None


def exe_record(exe: Path | None) -> dict:
    """What a sidecar keeps about the program that recorded it."""
    if exe is None:
        return {}
    import hashlib
    rec = {"exe": Path(exe).name, "build": build_label(exe)}
    try:
        rec["sha256"] = hashlib.sha256(Path(exe).read_bytes()).hexdigest()
    except OSError:
        rec["sha256"] = None
    return rec


def build_token(sidecar: dict) -> str:
    """The fingerprint's build field: 'gainfix' for an R820T/R860 run made with
    rtl_power_gainfix, else ''. Old sidecars have no build and get ''.

    v5 runs before the fix had their gain silently ignored, so they must not
    compare with v5 runs after it. XTR runs keep their fingerprint: the E4000
    read the same on both builds, and its history stays comparable.
    """
    rp = (sidecar or {}).get("rtl_power") or {}
    if rp.get("build") == "gainfix" and rp.get("tuner") in GAIN_LOST_TUNERS:
        return "gainfix"
    return ""


def program_line(rp: dict) -> str:
    """'rtl_power_gainfix.exe (gainfix build, sha256 1a2b3c4d...)' for scan_build.md
    and report.md. Sidecars from before 2026-10-27 name no program."""
    if not rp.get("exe"):
        return "rtl_power (build not recorded; before 2026-10-27 always the stock build)"
    sha = (rp.get("sha256") or "")[:16]
    return f"{rp['exe']} ({rp.get('build')} build" + (f", sha256 {sha}...)" if sha else ")")


def frozen() -> bool:
    """True inside a PyInstaller build."""
    return bool(getattr(sys, "frozen", False))
