"""Record an rtl_power survey from a named preset, then analyse it.

  marsad run LS 6h
  marsad run milair 30m --conditions "clear, heating off"
  marsad run --list
  marsad run LS 6h --dry-run

Does, in order:
  1. refuses to start if another program is holding the SDR (SDR++ etc.)
  2. builds the standard rtl_power command for the preset
  3. asks about the antenna/chain and conditions (Enter accepts last used)
  4. writes a surveys/<name>.json sidecar, then runs rtl_power
  5. runs the analyser on the result, writes scan_build.md, archives it

Standard command shape, per the station conventions:
  rtl_power -f LO:HI:BIN -g <fixed gain> -i <sec> -c 0.25 -F 9 -e <duration> out.csv

Stdlib only, so it works under a bare system Python; the analyser (which needs
numpy) is launched as a separate process.
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

from . import host
from .config import Station, source_root
from .rtlpower import (CROP, FIR, RTL_POWER_MAX_MHZ, TUNER_GAINS, TUNER_LABEL,
                       actual_bin_width_khz, find_tool, fingerprint, fmt_hms,
                       parse_duration, parse_freq_spec, scan_geometry)


def progress_every(dur_s: float) -> float:
    """Seconds between progress lines: about ten per run, clamped.

    A 5 min check should not sit silent for minutes; a 6 h survey should not scroll.
    """
    return min(300.0, max(30.0, dur_s / 10.0))


def run_with_progress(cmd: list, dur_s: float, csv_path: Path, every: float | None = None) -> int:
    """Run rtl_power, printing elapsed/remaining periodically.

    rtl_power prints its header and then nothing for the whole run, so a long survey
    looks hung. Each update is its own line rather than a redrawing bar, because
    rtl_power shares this console and its own output would break a \\r-updated line.
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
        # Ctrl-C reaches the whole console group, so rtl_power is already
        # stopping; wait for it so the CSV is flushed before we report on it.
        proc.wait()
        raise


# ---------------------------------------------------------------------------
# environment checks
# ---------------------------------------------------------------------------
def check_device_free(non_interactive: bool) -> None:
    busy = host.running_sdr_programs()
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


def device_check(station: Station, device: int) -> None:
    """Prove the receiver is actually there and free, before asking anything.

    `rtl_eeprom -d N` with no write flags is a read-only probe that opens the
    device, so it catches both "unplugged" and "something else has it" in one
    go, in about 1.5 s. Doing this before the prompts means you never type out
    the conditions only to have rtl_power exit instantly.
    """
    eeprom = find_tool("rtl_eeprom", station.tool_dirs())
    if eeprom is None:
        print("  (rtl_eeprom not found, skipping the receiver check)")
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
        print("    - sleep and hibernate off")
        print("    - USB selective suspend off for this hub (Windows)")
        print("    - lid-close action set to 'do nothing', on a laptop")
        print("  (Operating-system settings; change them yourself, this program will not.)")


# ---------------------------------------------------------------------------
# conditions prompt
# ---------------------------------------------------------------------------
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
def build_parser(station: Station, prog: str = "marsad run") -> argparse.ArgumentParser:
    presets = station.presets
    p = argparse.ArgumentParser(
        prog=prog, description="Record an rtl_power survey from a preset, then analyse it.")
    p.add_argument("preset", nargs="?", help="preset name: " + ", ".join(presets))
    p.add_argument("duration", nargs="?", help="how long to record, e.g. 30m, 6h")
    p.add_argument("--list", action="store_true", help="show the presets and exit")
    p.add_argument("--gain", type=float, help="override the preset gain (dB)")
    p.add_argument("--integration", type=int, help="override seconds per sweep")
    p.add_argument("--freq", help="override the LO:HI:BIN spec")
    p.add_argument("--device", type=int, default=0, help="rtl_power device index (default 0)")
    p.add_argument("--receiver", choices=list(station.receivers) or None,
                   help="receiver, for gap masking")
    p.add_argument("--chain", metavar="TEXT",
                   help="the physical signal chain, source first, separated by ' > '. "
                        "The scan builder emits this; it is what makes a 50 ohm load run "
                        "self-describing instead of relying on stale station.json text.")
    p.add_argument("--conditions", help="free-text conditions, skips that prompt")
    p.add_argument("--tag", help="extra word in the filename, e.g. lna-off")
    p.add_argument("--threshold", type=float, help="passed through to the analyser")
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
    p.add_argument("--no-notify", action="store_true",
                   help="skip the beeps and the desktop notification when the scan ends")
    p.add_argument("--no-live", action="store_true",
                   help="do not open the live view for this run")
    p.add_argument("--no-archive", action="store_true",
                   help="skip folding the finished report into occupancy.db")
    return p


def show_presets(station: Station) -> None:
    print("\n  Presets (gain and integration are the station defaults):\n")
    for name, p in station.presets.items():
        lo, hi, b = parse_freq_spec(p["freq"])
        print(f"    {name:<7} {lo:>7.1f} - {hi:<7.1f} MHz  bin {b:>6.1f} kHz  "
              f"-g {p['gain']:<5g} -i {p['integration']:<3d}  {p['note']}")
    print(f"\n  Every survey also uses -c {CROP} -F {FIR}.\n")


def write_scan_build(csv_path: Path, sidecar: dict, geom: dict,
                     analyze_cmd, out_dir: Path, retune_ms: float) -> Path:
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
        f"({geom['hops']} hops x {retune_ms:g} ms) |",
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
    """First port in a small range nothing is listening on."""
    import socket
    for port in range(start, start + tries):
        with socket.socket() as sk:
            sk.settimeout(0.2)
            if sk.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return None


def notify(title: str, body: str) -> None:
    """Tell the owner a scan is over: three beeps and a desktop notification.

    The scan is watched from the live view or not at all, so a finish needs to
    reach someone who is in another window. Both channels are best-effort: a
    notification that fails must never make the run look failed.

    On Windows the toast goes through classic Windows PowerShell 5.1, because
    PowerShell 7 cannot load the WinRT notification types. Nothing is
    installed; this is the OS's own API. Elsewhere `notify-send` is tried.
    """
    if host.IS_WINDOWS:
        try:
            import winsound
            for _ in range(3):
                winsound.Beep(880, 180)
                time.sleep(0.08)
        except Exception:
            pass
        ps = shutil.which("powershell.exe") or \
            r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
        q = lambda t: str(t).replace("'", "''")      # PowerShell single-quote escaping
        script = (
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
            "ContentType = WindowsRuntime] | Out-Null\n"
            "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
            "[Windows.UI.Notifications.ToastTemplateType]::ToastText02)\n"
            "$n = $t.GetElementsByTagName('text')\n"
            f"$n.Item(0).AppendChild($t.CreateTextNode('{q(title)}')) | Out-Null\n"
            f"$n.Item(1).AppendChild($t.CreateTextNode('{q(body)}')) | Out-Null\n"
            "$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe'\n"
            "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show("
            "[Windows.UI.Notifications.ToastNotification]::new($t))\n"
        )
        try:
            import tempfile
            with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False,
                                             encoding="utf-8") as fh:
                fh.write(script)
                path = fh.name
            subprocess.run([ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        except Exception:
            pass
        return
    print("\a" * 3, end="", flush=True)
    if shutil.which("notify-send"):
        try:
            subprocess.run(["notify-send", title, body], timeout=15)
        except Exception:
            pass


def viewer_follows(port: int = 8731) -> bool:
    """True if a live viewer is already serving on `port` AND follows the newest CSV."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/surveys", timeout=1) as r:
            return bool(json.load(r).get("follow"))
    except Exception:
        return False


def start_live_view(station: Station, idle_min: float = 5.0):
    """Reuse the viewer on 8731 if it follows the newest CSV, else launch one.

    The viewer switches to this run's CSV the moment rtl_power creates it, so it
    needs no --csv. Non-fatal: a viewer that will not start must never stop a
    survey recording.
    """
    if viewer_follows():
        print("  Live view: http://127.0.0.1:8731/   (already running; it follows the newest scan)")
        return None
    port = free_port()
    if port is None:
        print("  Live view: ports 8731-8740 are all busy, skipping.")
        return None
    cmd = host.self_command("live", "--station", str(station.root),
                            "--port", str(port), "--exit-after-idle", str(idle_min))
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                cwd=source_root() or None)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"  Live view could not start ({exc}); recording is unaffected.")
        return None
    print(f"  Live view: http://127.0.0.1:{port}/   "
          f"(closes {idle_min:g} min after the scan ends)")
    return proc


def source_is_load(chain_order: str | None) -> bool | None:
    """Terminated or antenna, decided from the chain this run actually recorded.

    The archive otherwise infers this from sidecar text, which is often wrong
    because station.json carried the previous run's wording. Here the source is
    the first element of the chain the user passed for THIS run, so the flag is
    a statement of fact rather than a guess. Returns None when there is no
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


def archive_run(station: Station, stem: str, chain_order: str | None) -> None:
    """Fold the finished report into occupancy.db and record the source flag.

    Non-fatal throughout: the survey and its report already exist on disk, so an
    archive hiccup must not make the run look failed.
    """
    from . import archive as arch
    report_dir = station.reports / stem
    print(f"\n  Archiving:\n    marsad archive add {report_dir}")
    try:
        con = arch.connect(station)
        print(arch.add_report(station, con, report_dir))
    except Exception as exc:  # noqa: BLE001 - never fail the run over the archive
        print(f"  Archive add failed ({exc}) - run it by hand; the report is unaffected.")
        return

    load = source_is_load(chain_order)
    if load is None:
        print("  Source flag not set: the chain does not name an antenna or a 50 ohm load.\n"
              f"  Set it by hand:  marsad archive mark {stem} --terminated|--antenna")
        return
    flag = "--terminated" if load else "--antenna"
    print(f"    marsad archive mark {stem} {flag}")
    try:
        arch.mark(con, stem, load)
    except Exception as exc:  # noqa: BLE001
        print(f"  Archive mark failed ({exc}); set it by hand.")


def main(argv=None, station: Station | None = None, prog: str = "marsad run") -> int:
    station = station or Station.load()
    parser = build_parser(station, prog)
    args = parser.parse_args(argv)
    presets = station.presets

    if args.list:
        show_presets(station)
        return 0
    if not args.preset or not args.duration:
        parser.print_help()
        return 2
    if args.preset not in presets:
        raise SystemExit(f"unknown preset {args.preset!r}. "
                         f"Known: {', '.join(presets)} (--list for detail)")

    preset = presets[args.preset]
    dur_text, dur_s = parse_duration(args.duration)
    freq = args.freq or preset["freq"]
    # `is None` not `or`, so --gain 0 / --integration 0 are rejected, not ignored.
    gain = preset["gain"] if args.gain is None else args.gain
    integration = preset["integration"] if args.integration is None else args.integration
    receiver = args.receiver or preset["receiver"]
    lo, hi, bin_khz = parse_freq_spec(freq)

    if integration < 1:
        raise SystemExit("--integration must be at least 1 second")
    tuner = station.tuner_of(receiver)
    valid = TUNER_GAINS.get(tuner or "")
    if valid and gain not in valid:
        nearest = min(valid, key=lambda g: abs(g - gain))
        raise SystemExit(f"gain {gain:g} dB is not a {TUNER_LABEL.get(tuner, tuner)} step. "
                         f"Nearest is {nearest:g}. "
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

    rtl_power = find_tool("rtl_power", station.tool_dirs())
    if rtl_power is None and not args.dry_run:
        raise SystemExit(
            "rtl_power not found. Put the RTL-SDR Blog release in "
            f"{station.root / station.data.get('rtl_sdr_dir', 'rtl-sdr')} "
            "or on PATH, or set rtl_sdr_dir in station.json.")

    station.surveys.mkdir(parents=True, exist_ok=True)
    started = dt.datetime.now()
    tag = f"_{args.tag}" if args.tag else ""
    stem = f"survey_{args.preset}_{dur_text}{tag}_{started:%Y%m%d-%H%M}"
    csv_path = station.surveys / f"{stem}.csv"
    json_path = station.surveys / f"{stem}.json"
    if csv_path.exists():
        raise SystemExit(f"{csv_path.name} already exists. Use --tag to distinguish this run.")

    cmd = [str(rtl_power or "rtl_power"), "-f", freq, "-g", f"{gain:g}", "-i", str(integration),
           "-c", str(args.crop), "-F", str(args.fir), "-e", dur_text]
    if args.device:
        cmd += ["-d", str(args.device)]
    cmd.append(str(csv_path))

    retune_ms = station.retune_ms_per_hop
    geom = scan_geometry(lo, effective_hi, bin_khz, integration, dur_s, args.crop,
                         retune_ms_per_hop=retune_ms)
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
    print(f"    crop/FIR    -c {args.crop}  -F {args.fir}"
          + ("" if (args.crop, args.fir) == (CROP, FIR)
             else "   NOT standard - not comparable with other surveys"))
    print(f"    receiver    {receiver}")
    print(f"    writing     surveys/{csv_path.name}")
    free_gb = shutil.disk_usage(station.surveys).free / 1e9
    print(f"    disk free   {free_gb:.1f} GB")
    if geom['csv_mb'] / 1000.0 > free_gb * 0.8:
        raise SystemExit("  Not enough free disk for that estimate. Shorten the run.")
    if rtl_power is None:
        print("    rtl_power   NOT FOUND - a real run would refuse to start")

    print("\n  Command:\n    " + subprocess.list2cmdline(cmd))

    if args.dry_run:
        print("\n  --dry-run: nothing recorded.\n")
        return 0

    check_device_free(args.non_interactive)
    device_check(station, args.device)
    warn_long_run(dur_s)

    try:
        chain = ask_conditions(args, station.defaults)
        if args.chain:
            # An explicit chain always wins over whatever station.json remembers;
            # stale chain text is what mislabelled two earlier runs.
            chain["chain_order"] = args.chain
    except (KeyboardInterrupt, EOFError):
        raise SystemExit("\n  Cancelled at the questions. Nothing was recorded.")
    station.save_defaults(chain)

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
        start_live_view(station)
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

    tag = f" {args.tag}" if args.tag else ""
    if not args.no_notify:
        state = "interrupted" if interrupted else ("finished" if rc == 0 else f"ended with exit code {rc}")
        notify(f"Scan{tag} {state}",
               f"{csv_path.name}  -  {elapsed/60:.0f} min recorded, analysing now")

    if args.no_analyze:
        print("  Build sheet: " + str(write_scan_build(
            csv_path, sidecar, geom, None, station.reports / csv_path.stem, retune_ms)))
        print("  Skipping analysis. Run it later with:\n"
              f"    marsad analyze surveys/{csv_path.name} --receiver {receiver}\n")
        return 0

    analyze = host.self_command("analyze", "--station", str(station.root), str(csv_path),
                                "--receiver", receiver, "--out", str(station.reports),
                                prefer_venv=station.venv_python)
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
    # From source, `-m marsad` must be importable: run from the checkout.
    arc = subprocess.run(analyze, cwd=source_root() or None).returncode
    build = write_scan_build(csv_path, sidecar, geom, analyze,
                             station.reports / csv_path.stem, retune_ms)
    if not args.no_notify:
        notify(f"Report ready{tag}" if arc == 0 else f"Analysis failed{tag}",
               f"reports/{csv_path.stem}/report.md" if arc == 0 else f"analyser exit code {arc}")
    if arc == 0:
        print(f"\n  Done. Report: reports/{csv_path.stem}/report.md\n")
        print(f"        Build:  reports/{csv_path.stem}/{build.name}")
        if not args.no_archive:
            archive_run(station, csv_path.stem, chain.get("chain_order"))
    return arc
