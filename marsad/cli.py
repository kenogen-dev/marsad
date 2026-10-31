"""The `marsad` command.

    marsad                       open the web app (scan, builder, archive, roadmap)
    marsad run LS 1h             record a survey from a preset, then analyse it
    marsad analyze surveys/x.csv --receiver xtr
    marsad compare a.csv b.csv   the analyser's two-survey comparison
    marsad compare-runs A B      the pre-registered A/B statistics
    marsad spur-stats a/bins.csv b/bins.csv
    marsad drift <tag>           per-sweep whole-band median
    marsad events <tag>          broadband steps and one-sweep transients in a survey
    marsad adsb-check            1090 MHz positive control
    marsad archive list|add|new|trend|band|seen|mark
    marsad live                  the web app, without opening a browser
    marsad web start|stop|status|restart   keep the web app up on 8731, with no window
    marsad response --open A --load B --antenna C   return loss / SWR per bin
    marsad calibrate-retune      measure this machine's retune cost per hop
    marsad characterise <iq>     what a recording holds, and how to capture it next time
    marsad watch fixed|survey|list   the triggered recorder and its captures catalogue
    marsad init                  write a station.json to edit
    marsad config                show what station.json was loaded, from where

    marsad --station C:\\SDR run LS 1h    use another station folder (or set SDR_STATION)

Each command is a thin wrapper around one module; `marsad <command> --help` shows
that command's own options.
"""
from __future__ import annotations

import os
import sys
import time
import webbrowser

from . import __version__
from .config import Station
from .rtlpower import frozen

COMMANDS = {
    "run": "record a survey from a preset, then analyse it",
    "analyze": "analyse one or more rtl_power CSVs into reports/",
    "compare": "analyse two CSVs and write a comparison report",
    "compare-runs": "A/B statistics for two finished reports",
    "spur-stats": "harmonic-family statistic for one or two bins.csv",
    "drift": "per-sweep whole-band median of one survey",
    "events": "broadband steps and one-sweep transients in one survey (25 MHz blocks)",
    "adsb-check": "1090 MHz ADS-B receive check",
    "archive": "the occupancy.db archive (list, add, new, trend, band, seen, mark)",
    "live": "the local web app",
    "web": "start, stop, check or restart the web app on 8731 (no window)",
    "response": "antenna return loss and SWR from three bridge runs (open, load, antenna)",
    "calibrate-retune": "measure rtl_power's retune cost per hop on this PC",
    "characterise": "measure an IQ recording (bandwidth, pulses, chirp, period) and write a recipe",
    "watch": "the triggered recorder: fixed watch, survey + recorder, the captures catalogue",
    "init": "write a station.json into this folder to edit",
    "config": "show the loaded station configuration",
    "version": "print the version",
}


def usage() -> str:
    width = max(len(c) for c in COMMANDS)
    lines = [f"marsad {__version__} - rtl_power survey station", "",
             "usage: marsad [--station FOLDER] <command> [options]", "",
             "commands:"]
    for name, text in COMMANDS.items():
        lines.append(f"  {name:<{width}}  {text}")
    lines += ["", "With no command (a double-click on marsad.exe), starts the web app on",
              f"{HOME_URL} in the background (no window) and opens it in your browser;",
              "if it is already running, just opens it.",
              "`marsad <command> --help` lists that command's options."]
    return "\n".join(lines)


def split_station(argv: list) -> tuple:
    """Pull --station FOLDER out of the arguments wherever it appears."""
    root = None
    rest = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--station" and i + 1 < len(argv):
            root = argv[i + 1]
            i += 2
            continue
        if a.startswith("--station="):
            root = a.split("=", 1)[1]
            i += 1
            continue
        rest.append(a)
        i += 1
    return root, rest


HOME_PORT = 8731      # the one address the station's web app lives at
HOME_URL = f"http://127.0.0.1:{HOME_PORT}/"


def _linger(seconds: float = 8.0) -> None:
    """Keep a double-clicked console readable before it closes.

    Started from Explorer, the frozen exe owns its console window, which closes
    the instant the program exits; anything printed would vanish unread.
    """
    if frozen():
        print(f"  (this window closes in {seconds:g} s)")
        time.sleep(seconds)


def launch(station: Station) -> int:
    """What a double-click on marsad.exe (or a bare `marsad`) does.

    One viewer per address, always: if the web app already serves 8731, just
    open the browser on it. Otherwise start it in the background with no
    window (marsad/webapp.py), so tidying the desktop can never close it, then
    open the browser. `marsad web stop` ends it. A station with no station.json
    (a fresh release) never takes 8731: it gets a test port (webapp.port_for).

    Two environment variables exist for testing a build without touching the
    owner's address: MARSAD_PORT moves the launcher to another port, and
    MARSAD_NO_BROWSER=1 skips opening the browser.
    """
    from . import webapp
    port = webapp.port_for(station, int(os.environ.get("MARSAD_PORT") or HOME_PORT))
    if port is None:                  # an empty station and no free test port
        _linger()
        return 1
    url = f"http://127.0.0.1:{port}/"
    use_browser = os.environ.get("MARSAD_NO_BROWSER") != "1"
    outcome = webapp.ensure_running(station, port)
    if outcome in ("up", "started", "starting"):
        print(f"  Opening {url} in your browser; this window can close."
              if use_browser else "  (browser not opened: MARSAD_NO_BROWSER is set)")
        if use_browser:
            webbrowser.open(url)
        _linger(6)
        return 0
    _linger()
    return 1


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    root, rest = split_station(argv)
    if not rest:
        return launch(Station.load(root))
    cmd, args = rest[0], rest[1:]
    if cmd in ("-h", "--help", "help"):
        print(usage())
        return 0
    if cmd in ("version", "--version", "-V"):
        print(__version__)
        return 0
    if cmd not in COMMANDS:
        print(f"marsad: unknown command {cmd!r}\n\n{usage()}", file=sys.stderr)
        return 2

    station = Station.load(root)
    prog = f"marsad {cmd}"
    if cmd == "run":
        from . import record
        return record.main(args, station, prog)
    if cmd in ("analyze", "compare"):
        from . import analyze
        return analyze.main([cmd, *args], station, "marsad") or 0
    if cmd == "compare-runs":
        from . import stats
        return stats.compare_runs_main(args, station, prog)
    if cmd == "spur-stats":
        from . import stats
        return stats.spur_stats_main(args, station, prog)
    if cmd == "drift":
        from . import drift
        return drift.main(args, station, prog)
    if cmd == "events":
        from . import events
        return events.main(args, station, prog)
    if cmd == "adsb-check":
        from . import adsb
        return adsb.main(args, station, prog)
    if cmd == "archive":
        from . import archive
        return archive.main(args, station, prog)
    if cmd == "live":
        from . import live
        return live.main(args, station, prog)
    if cmd == "web":
        from . import webapp
        return webapp.main(args, station, prog)
    if cmd == "response":
        from . import response
        return response.main(args, station, prog)
    if cmd == "calibrate-retune":
        from . import calibrate
        return calibrate.main(args, station, prog)
    if cmd == "characterise":
        from . import characterise
        return characterise.main(args, station, prog)
    if cmd == "watch":
        from . import watch
        return watch.main(args, station, prog)
    if cmd == "init":
        force = "--force" in args
        path = station.write_full(overwrite=force)
        print(f"  wrote {path}\n  Edit it to describe your receiver, then `marsad run --list`.")
        return 0
    if cmd == "config":
        import json
        print(f"  station folder   {station.root}")
        print(f"  station.json     {'found' if station.file_found else 'NOT found, using built-in defaults'}")
        local = station.root / "station.local.json"
        print(f"  local overlay    {'station.local.json found (git-ignored)' if local.exists() else 'none'}"
              + (", location set" if "location" in station.data else ", no location"))
        print(f"  surveys          {station.surveys}")
        print(f"  reports          {station.reports}")
        print(f"  rtl-sdr folders  {', '.join(str(d) for d in station.tool_dirs())}")
        from .rtlpower import build_label
        rp, rp_warning = station.rtl_power()
        print(f"  rtl_power        {f'{rp}  ({build_label(rp)} build)' if rp else 'NOT FOUND (install the RTL-SDR Blog release or set rtl_sdr_dir)'}")
        if rp_warning:
            print(f"                   WARNING: {rp_warning}")
        print(f"  receivers        {', '.join(station.receivers) or 'none'}")
        print(f"  presets          {', '.join(station.presets) or 'none'}")
        print(f"  retune ms/hop    {station.retune_ms_per_hop:g}")
        print(f"  spur families    {', '.join(f'{f:g}' for f, _ in station.spur_families)} MHz")
        if "--json" in args:
            print(json.dumps(station.data, indent=2))
        return 0
    return 2
