"""The `marsad` command.

    marsad                       open the web app (scan, builder, archive, roadmap)
    marsad run LS 1h             record a survey from a preset, then analyse it
    marsad analyze surveys/x.csv --receiver xtr
    marsad compare a.csv b.csv   the analyser's two-survey comparison
    marsad compare-runs A B      the pre-registered A/B statistics
    marsad spur-stats a/bins.csv b/bins.csv
    marsad drift <tag>           per-sweep whole-band median
    marsad adsb-check            1090 MHz positive control
    marsad archive list|add|new|trend|band|seen|mark
    marsad live                  the web app, without opening a browser
    marsad response --open A --load B --antenna C   return loss / SWR per bin
    marsad calibrate-retune      measure this machine's retune cost per hop
    marsad init                  write a station.json to edit
    marsad config                show what station.json was loaded, from where

    marsad --station C:\\SDR run LS 1h    use another station folder (or set SDR_STATION)

Each command is a thin wrapper around one module; `marsad <command> --help` shows
that command's own options.
"""
from __future__ import annotations

import sys

from . import __version__
from .config import Station

COMMANDS = {
    "run": "record a survey from a preset, then analyse it",
    "analyze": "analyse one or more rtl_power CSVs into reports/",
    "compare": "analyse two CSVs and write a comparison report",
    "compare-runs": "A/B statistics for two finished reports",
    "spur-stats": "harmonic-family statistic for one or two bins.csv",
    "drift": "per-sweep whole-band median of one survey",
    "adsb-check": "1090 MHz ADS-B receive check",
    "archive": "the occupancy.db archive (list, add, new, trend, band, seen, mark)",
    "live": "the local web app",
    "response": "antenna return loss and SWR from three bridge runs (open, load, antenna)",
    "calibrate-retune": "measure rtl_power's retune cost per hop on this PC",
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
    lines += ["", "With no command, opens the web app in your browser.",
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


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    root, rest = split_station(argv)
    if not rest:
        from . import live
        return live.main(["--open"], Station.load(root))
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
    if cmd == "adsb-check":
        from . import adsb
        return adsb.main(args, station, prog)
    if cmd == "archive":
        from . import archive
        return archive.main(args, station, prog)
    if cmd == "live":
        from . import live
        return live.main(args, station, prog)
    if cmd == "response":
        from . import response
        return response.main(args, station, prog)
    if cmd == "calibrate-retune":
        from . import calibrate
        return calibrate.main(args, station, prog)
    if cmd == "init":
        force = "--force" in args
        path = station.write_full(overwrite=force)
        print(f"  wrote {path}\n  Edit it to describe your receiver, then `marsad run --list`.")
        return 0
    if cmd == "config":
        import json
        print(f"  station folder   {station.root}")
        print(f"  station.json     {'found' if station.file_found else 'NOT found, using built-in defaults'}")
        print(f"  surveys          {station.surveys}")
        print(f"  reports          {station.reports}")
        print(f"  rtl-sdr folders  {', '.join(str(d) for d in station.tool_dirs())}")
        from .rtlpower import find_tool
        rp = find_tool("rtl_power", station.tool_dirs())
        print(f"  rtl_power        {rp or 'NOT FOUND (install the RTL-SDR Blog release or set rtl_sdr_dir)'}")
        print(f"  receivers        {', '.join(station.receivers) or 'none'}")
        print(f"  presets          {', '.join(station.presets) or 'none'}")
        print(f"  retune ms/hop    {station.retune_ms_per_hop:g}")
        print(f"  spur families    {', '.join(f'{f:g}' for f, _ in station.spur_families)} MHz")
        if "--json" in args:
            print(json.dumps(station.data, indent=2))
        return 0
    return 2
