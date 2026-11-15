# marsad_desktop: the Marsad window

A native Windows program (PySide6 + pyqtgraph, drawn through OpenGL) for Marsad. It replaces the browser page served
by `marsad live`, screen by screen. **The engine is unchanged**: recording, analysis, the recorder, the catalogue and
the archive all stay in the `marsad` package. The window only looks at a station and asks its service to do things.

## How the pieces fit

```
  the window (this package)  --HTTP-->  a station's service (marsad live)  --starts-->  marsad run / watch / setup
       .venv-desktop                         .venv                               (outside every process tree)
```

- **The window** draws and asks. It never opens a radio and never runs a scan itself. Closing it never stops a scan.
- **The service** is `marsad live` for one station folder. It answers the window, checks that a radio is free (one
  program per radio, radios found by serial), and launches runs outside its own process tree, so neither the window
  nor the service closing can stop them.
- **A station** is a folder with a `station.json` (receivers, presets, roles), `chain.json` (recorded chain texts),
  `chain_layout.json` (the Chain screen's drawings), and its surveys, reports and logs. Private values belong in
  `station.local.json` beside it, which the window never opens.
- **Version handshake:** each service reports a build stamp of the engine's code on `/ping`. A window started on
  newer code restarts the services it started itself (ports 8750-8759) as soon as nothing runs on them; for any other
  service it shows a bar saying what to do. A home station's service is never restarted without a click.

## Screens

| Screen | What it does |
|---|---|
| Live Scan (home) | Follows the newest recording: spectrum, waterfall, hover readout, refused hops, recorder panel, radio strip, Start panels |
| Chain | The rig as a drawing (parts, wires, cables, enclosures, power); writes the same recorded text as the engine's `chain.chain_text` |
| Radios / setups | Roles, Swap, coverage, and the radio setups the engine supports |
| Set up this station | A step-by-step guide: add a radio, measure it, draw and confirm the rig, path checks |
| Stations | New / Open / Switch / Close station; the program never deletes or re-creates a station folder |

## Running it

Two environments, kept apart: `.venv` (the engine, `requirements.txt`) and `.venv-desktop` (this window,
`marsad_desktop/requirements.txt`).

```
py -m venv .venv-desktop
.venv-desktop\Scripts\python.exe -m pip install -r marsad_desktop\requirements.txt
.venv-desktop\Scripts\pythonw.exe -m marsad_desktop.app
```

With no station chosen yet it opens a welcome dialog (New Station, Open Station). Useful settings:

| Variable | Effect |
|---|---|
| `MARSAD_SERVICE` | Point the window at a service already running (e.g. `http://127.0.0.1:8742`) |
| `MARSAD_START_SCREEN` | Open on a given screen (`Chain`, ...) |
| `MARSAD_PRACTICE=1` | Label everything as a practice station with simulated radios |
| `MARSAD_DESKTOP_INI` | Keep the window's preferences in another file (tests use this) |
| `MARSAD_APPDATA` | Keep the station list somewhere else (tests use this) |
| `MARSAD_NO_GL=1` | Draw without OpenGL |
| `MARSAD_TRACE=1` | Write a timing trace (also Settings > Diagnostics) |

Ports: 8731 is a home station's own service; 8740-8749 are test services started by launchers or tests (always with
`--exit-after-idle 5`); 8750-8759 are services the window starts for other stations.

## Where things are kept

- The window's preferences: `~/.marsad_desktop.ini` (never the registry, never a station).
- The list of known stations: `%LOCALAPPDATA%\Marsad\stations.json`.
- New stations: `Documents\Marsad\Stations\<name>` unless another folder is chosen.
- Service logs: `<station>\logs\window_service_<port>_<time>.log`. The optional timing trace:
  `marsad_window_trace.log` beside the preferences, started afresh past 5 MB (the previous one kept as `.old.log`).

## Testing

- `pytest` (in `.venv`) runs the public suite, including the window's Qt-free modules (`rig`, `geometry`,
  `setup_words`, `theme`, `scan_calc`, `setups`).
- `prototypes/*check*.py` drive the real window off-screen on made-up stations with simulated radios, through their
  own services on test ports, and stop every service they start. `prototypes/window_smoke_020.py` times everyday
  journeys against a 100 ms target.

## Rules the window keeps

Closing the window never stops a scan. Private bands are enforced by the engine, not the screen. Nothing the program
offers deletes recorded data. A run's fingerprint is never changed silently. Every run records its own rig. One
program per radio, radios by serial. A recorder whose status is more than 15 s old is shown as stopped. Levels are
relative dB, and the two engines' scales are never drawn as one.
