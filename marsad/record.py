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

from . import conditions, host
from .config import Station, source_root
from .rtlpower import (CROP, FIR, RTL_POWER_MAX_MHZ, TUNER_GAINS, TUNER_LABEL,
                       actual_bin_width_khz, find_tool, fingerprint, fmt_hms,
                       parse_duration, parse_freq_spec, rtl_power_max_span_mhz,
                       scan_geometry)


def progress_every(dur_s: float) -> float:
    """Seconds between progress lines: about ten per run, clamped.

    A 5 min check should not sit silent for minutes; a 6 h survey should not scroll.
    """
    return min(300.0, max(30.0, dur_s / 10.0))


def stop_child(proc: subprocess.Popen) -> None:
    """Ask rtl_power to finish and flush; insist if it does not within 10 s.

    rtl_power flushes its CSV after every interval, so even the insistent path
    loses nothing already written; the polite path lets it close the device.
    """
    try:
        host.ask_to_stop(proc)
        proc.wait(timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        proc.terminate()


def run_with_progress(cmd: list, dur_s: float, csv_path: Path, every: float | None = None,
                      stop_file: Path | None = None) -> int:
    """Run rtl_power, printing elapsed/remaining periodically.

    rtl_power prints its header and then nothing for the whole run, so a long survey
    looks hung. Each update is its own line rather than a redrawing bar, because
    rtl_power shares this console and its own output would break a \\r-updated line.

    `stop_file` is how the web app's Stop button reaches a run it started: when that
    file appears, rtl_power is asked to stop and KeyboardInterrupt is raised, so the
    rest of the run proceeds exactly as after a Ctrl-C. Such a run gets rtl_power in
    its own process group (that is what makes it addressable), which also means it no
    longer hears the console's Ctrl-C; the except branch covers that.
    """
    every = progress_every(dur_s) if every is None else every
    t0 = time.time()
    nxt = t0 + every
    own_group = stop_file is not None
    proc = subprocess.Popen(cmd, creationflags=host.popen_group_flags() if own_group else 0)
    try:
        while True:
            try:
                return proc.wait(timeout=0.5 if own_group else max(0.5, nxt - time.time()))
            except subprocess.TimeoutExpired:
                pass
            if own_group and stop_file.exists():
                stop_file.unlink(missing_ok=True)
                print("\n  Stop requested from the web app; asking rtl_power to finish.")
                stop_child(proc)
                raise KeyboardInterrupt
            now = time.time()
            if now < nxt:
                continue
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
        # In its own group it did not hear the Ctrl-C and must be asked.
        if own_group and proc.poll() is None:
            stop_child(proc)
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


def eeprom_listing(out: str) -> list[tuple[int, str]]:
    """The dongles rtl_eeprom lists before it opens one: [(index, name), ...]."""
    found = []
    m = re.search(r"Found (\d+) device\(s\):[ \t]*\n((?:[ \t]+\d+:.*\n?)*)", out)
    if m:
        for line in m.group(2).splitlines():
            mm = re.match(r"\s*(\d+):\s*(.*?)\s*$", line)
            if mm:
                found.append((int(mm.group(1)), mm.group(2)))
    return found


def open_failure_message(out: str, device: int, busy: list[str] | None) -> str:
    """Why `rtl_eeprom -d N` could not open the receiver, in the likeliest words.

    The numbers shift when a dongle is unplugged (2026-10-26: the XTR alone is
    device 0, but a remembered 1 kept coming back), so an index past the end of
    the list is a wrong number, not a busy dongle. Another program is only
    blamed when the SDR-in-use check did not already find none running.
    """
    listing = eeprom_listing(out)
    seen = ", ".join(f"{name or 'unnamed'} at {i}" for i, name in listing) or "none"
    if listing and device not in {i for i, _ in listing}:
        return (f"\n  No receiver at index {device}; detected: {seen}.\n"
                "  The index changes when a dongle is unplugged. Run again with the\n"
                "  right --device (on the Probe tab: Find receivers). Nothing was recorded.")
    if busy == []:
        return (f"\n  Receiver found but could not be opened on index {device} "
                f"(detected: {seen}).\n"
                "  No other radio program is running, so it is probably the wrong\n"
                "  index or a USB fault: check the device number, or unplug and\n"
                "  replug the dongle. Nothing was recorded.")
    return (f"\n  Receiver found but could not be opened on index {device}.\n"
            "  Another program (SDR++, SDR#, rtl_tcp...) probably has it.\n"
            "  Close it and try again. Nothing was recorded.")


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
        raise SystemExit(open_failure_message(out, device, host.running_sdr_programs()))
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
    p.add_argument("--keep-defaults", action="store_true",
                   help="do not remember this run's chain as the station default "
                        "(for probe runs and other one-offs)")
    p.add_argument("--stop-file", type=Path, metavar="PATH",
                   help="stop early, as Ctrl-C would, when this file appears "
                        "(the web app's Stop button)")
    p.add_argument("--chain-as-saved", action="store_true",
                   help="yes, the rig is still built as the saved default chain says "
                        "(needed once that chain has been used by a run and no --chain is given)")
    from . import rpf
    rpf.add_arguments(p)
    return p


def confirm_default_chain(station: Station, args) -> str | None:
    """Guard against recording a stale default chain. Returns the chain text to use.

    Nine command-line runs on 2026-10-20 all recorded the previous rig's chain
    because a run without --chain silently copies station.json's default. So:
    once a run has recorded the default (chain_used_at is set), the next run
    without --chain prints that chain and refuses, unless --chain-as-saved says
    the rig is unchanged or the Chain tab marks that chain static. On a terminal
    the question is asked instead of refused. Runs before any hardware is touched.
    """
    if args.chain:
        return args.chain
    d = station.defaults
    text = d.get("chain_order") or ""
    used = d.get("chain_used_at")
    if not text or not used or args.chain_as_saved:
        return text
    from . import chain as chain_mod
    if chain_mod.default_is_static(station, text):
        return text
    print(f"\n  The chain about to be recorded is the saved default, already used by a run "
          f"at {str(used).replace('T', ' ')}:\n    {text}")
    if not args.non_interactive and sys.stdin and sys.stdin.isatty():
        if input("  Is the rig still built exactly like this? [y/N] ").strip().lower() in ("y", "yes"):
            return text
        raise SystemExit("  Stopped. Pass --chain \"...\" for the rig as it is now. Nothing was recorded.")
    raise SystemExit(
        "  Is the rig still built like this? Re-run with --chain-as-saved to say yes, pass\n"
        "  --chain \"...\" for the rig as it is now, or tick 'static' on the Chain tab.\n"
        "  Nothing was recorded.")


def show_presets(station: Station) -> None:
    print("\n  Presets (gain and integration are the station defaults):\n")
    fftw = []
    for name, p in station.presets.items():
        if p.get("engine") == "rtl_power_fftw":
            fftw.append((name, p))
            continue
        lo, hi, b = parse_freq_spec(p["freq"])
        print(f"    {name:<7} {lo:>7.1f} - {hi:<7.1f} MHz  bin {b:>6.1f} kHz  "
              f"-g {p['gain']:<5g} -i {p['integration']:<3d}  {p['note']}")
    print(f"\n  Every survey also uses -c {CROP} -F {FIR}.\n")
    if fftw:
        from . import rpf
        print("  rtl_power_fftw presets (the engine is named in the preset; its dB scale is not rtl_power's):\n")
        for name, p in fftw:
            rate = p.get("rate", rpf.DEFAULT_RATE_HZ)
            lo, hi, c = rpf.parse_freq(p["freq"], rate)
            where = f"{lo:>7.1f} - {hi:<7.1f} MHz" if c is None else f"centre {c:<13.4f} MHz"
            print(f"    {name:<7} {where}  -b {p.get('bins', rpf.DEFAULT_BINS):<5} "
                  f"-g {p['gain']:<5g} -t {p.get('time', rpf.DEFAULT_TIME_S):<4g} "
                  f"{p.get('receiver')}  {p['note']}")
        print()


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
        f"| Conditions (typed) | {sidecar.get('conditions') or '(none recorded)'} |",
        *conditions.table_rows(sidecar.get("auto_conditions")), "",
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


VIEWER_PORT = 8731


def port_in_use(port: int = VIEWER_PORT) -> bool:
    """True if something is listening on `port` on this machine."""
    import socket
    with socket.socket() as sk:
        sk.settimeout(0.2)
        return sk.connect_ex(("127.0.0.1", port)) == 0


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


def start_live_view(station: Station) -> str:
    """Make sure the web app answers on 8731 before recording; start it if not.

    One address, always: http://127.0.0.1:8731/. The app follows whatever is
    recording, so it needs no --csv. It is started in the background with no
    window and no idle timeout (marsad/webapp.py), so it outlives this console
    and is still there between scans. Non-fatal: a page that will not start
    must never stop a survey recording.
    """
    from . import webapp
    return webapp.ensure_running(station, VIEWER_PORT)


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
    # A preset may name its recording engine; --engine overrides. rtl_power_fftw
    # has its own path (rpf.py) so nothing below changes for rtl_power runs.
    engine = args.engine or preset.get("engine", "rtl_power")
    if engine == "rtl_power_fftw":
        from . import rpf
        return rpf.record(args, station, preset)
    if preset.get("engine") not in (None, "rtl_power"):
        raise SystemExit(f"preset {args.preset!r} is for engine {preset['engine']!r}")
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
    max_span = rtl_power_max_span_mhz(args.crop)
    if effective_hi - lo > max_span:
        raise SystemExit(
            f"span {effective_hi - lo:g} MHz is wider than rtl_power can plan at -c {args.crop:g} "
            f"({max_span:.1f} MHz: the span divided by 1-crop must fit in 2^31 Hz). It would "
            f"plan one hop of 0 Hz and exit with 'Invalid sample rate'. Split it into two runs.")
    # Asking a little past the ceiling is the idiom for "give me everything you
    # can", so only complain when a meaningful slice would go missing.
    if hi > RTL_POWER_MAX_MHZ + 1.0:
        print(f"\n  Note: {hi:g} MHz is above rtl_power's {RTL_POWER_MAX_MHZ:.6f} MHz ceiling "
              f"(2^31 Hz).\n  It will quietly record only up to there; "
              f"{hi - RTL_POWER_MAX_MHZ:.1f} MHz will be missing.")

    # Looked up here so the summary can print its path; a missing rtl_power is
    # refused only after the chain guard below, which must speak first.
    rtl_power = find_tool("rtl_power", station.tool_dirs())

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
    chain_src = ("--chain" if args.chain else
                 "saved default" + (f", used by a run at {str(station.defaults.get('chain_used_at')).replace('T', ' ')}"
                                     if station.defaults.get("chain_used_at") else ""))
    print(f"    chain       ({chain_src}) {args.chain or station.defaults.get('chain_order') or '(none)'}")
    print(f"    writing     surveys/{csv_path.name}")
    free_gb = shutil.disk_usage(station.surveys).free / 1e9
    print(f"    disk free   {free_gb:.1f} GB")
    if geom['csv_mb'] / 1000.0 > free_gb * 0.8:
        raise SystemExit("  Not enough free disk for that estimate. Shorten the run.")
    if rtl_power is None:
        print("    rtl_power   NOT FOUND - a real run would refuse to start")

    print("\n  Command:\n    " + subprocess.list2cmdline(cmd))

    if args.dry_run:
        auto = conditions.new_block(station.data, station.surveys, csv_path.stem)
        print("\n  Measured conditions (stored in the sidecar as auto_conditions):\n    "
              + conditions.summary(auto["start"]))
        print("\n  --dry-run: nothing recorded.\n")
        return 0

    try:
        chain_text = confirm_default_chain(station, args)
    except (KeyboardInterrupt, EOFError):
        raise SystemExit("\n  Cancelled. Nothing was recorded.")
    if rtl_power is None:
        raise SystemExit(
            "rtl_power not found. Put the RTL-SDR Blog release in "
            f"{station.root / station.data.get('rtl_sdr_dir', 'rtl-sdr')} "
            "or on PATH, or set rtl_sdr_dir in station.json.")
    check_device_free(args.non_interactive)
    device_check(station, args.device)
    warn_long_run(dur_s)

    try:
        chain = ask_conditions(args, station.defaults)
        # An explicit chain always wins over whatever station.json remembers;
        # stale chain text is what mislabelled two earlier runs.
        chain["chain_order"] = chain_text
    except (KeyboardInterrupt, EOFError):
        raise SystemExit("\n  Cancelled at the questions. Nothing was recorded.")
    if not args.keep_defaults:
        # This run records this chain: from now on it needs confirming again.
        station.save_defaults({**chain, "chain_used_at": started.isoformat(timespec="seconds")})
        from . import chain as chain_mod
        chain_mod.mark_text_used(station, chain_text)

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
        "auto_conditions": conditions.new_block(station.data, station.surveys, csv_path.stem),
        "geometry": {k: (round(v, 6) if isinstance(v, float) else v)
                     for k, v in geom.items()
                     if k != "hop_centres_mhz" or geom["hops"] <= 16},
    }
    json_path.write_text(json.dumps(sidecar, indent=2) + "\n", encoding="utf-8")
    print(f"\n  Sidecar written: surveys/{json_path.name}")
    print("  Measured conditions: " + conditions.summary(sidecar["auto_conditions"]["start"]))

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
        rc = run_with_progress(cmd, dur_s, csv_path, stop_file=args.stop_file)
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
    sidecar["auto_conditions"]["end"] = conditions.snapshot(
        station.data, station.surveys, stem=csv_path.stem)
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
