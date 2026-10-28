# Marsad

*المرصد, al-marsad: the observatory.*

Marsad (المرصد, "the observatory"): an RTL-SDR spectrum monitoring station with a
comparability fingerprint for every survey, pre-registered A/B statistics, thermal drift and
spur-family tools, an occupancy archive, and a live waterfall. Built around rtl_power on
Windows.

Marsad records, analyses and compares radio spectrum surveys made with an RTL-SDR dongle
and `rtl_power`, with a **comparability fingerprint** on every run so you can only ever
compare like with like.

It grew out of one hobby monitoring station (a Nooelec NESDR SMArTee XTR and a SMArt v5
on a Windows PC) and a measurement programme that kept finding its own mistakes. The
tools exist to stop those mistakes recurring: every survey records how it was made,
every comparison checks the settings match first, and the statistics come with error
bars measured on real hardware.

## What you get

One command, `marsad`, and a local web app. `sdr` is installed as a short alias for the
same command, so `sdr run LS 1h` and `marsad run LS 1h` are identical.

| Command | What it does |
|---|---|
| `marsad` | opens the web app: live scan, scan builder, archive, roadmap |
| `marsad run LS 1h` | records a survey from a preset, writes a sidecar with every setting, analyses it, archives it |
| `marsad run LS 1h --dry-run` | shows exactly what rtl_power will do (hops, FFT, real bin width, dwell) and records nothing |
| `marsad analyze surveys/x.csv --receiver xtr` | report, plots, `peaks.csv`, `bins.csv` |
| `marsad compare-runs before after` | the A/B statistics: whole-band median, floor by block, harmonic family with its error bar, a verdict |
| `marsad spur-stats a/bins.csv b/bins.csv` | the harmonic-family statistic on its own |
| `marsad drift <tag>` | did the receiver drift as it warmed? |
| `marsad adsb-check` | proves the receive path works, in 60 s, by counting aircraft at 1090 MHz |
| `marsad archive list / new / trend / band / seen` | the long-term occupancy database |
| `marsad response --open A --load B --antenna C` | an antenna's return loss and SWR at every bin, from three runs through a return-loss bridge |
| `marsad calibrate-retune` | measures how long *your* PC loses to retuning between hops |
| `marsad init` | writes a `station.json` for you to edit |
| `marsad config` | shows what was loaded and from where |

`marsad <command> --help` lists each command's options.

## Install

You need `rtl_power` from the [RTL-SDR Blog Windows release](https://github.com/rtlsdrblog/rtl-sdr-blog/releases)
(or your distribution's `rtl-sdr` package on Linux). Unzip it into a folder called
`rtl-sdr` next to `station.json`, or put it on your PATH.

marsad prefers `rtl_power_gainfix`, a patched rtl_power that keeps the gain on R820T/R860
dongles (the stock program silently runs them on automatic gain) and does not overflow to
`-nan` on strong carriers. It is not in the RTL-SDR Blog release and not in the marsad zip:
build it with `packaging/rtl_power_gainfix/build.cmd` (see `BUILD.md` there) and put
`rtl_power_gainfix.exe` beside `rtl_power.exe`. Without it marsad runs the stock rtl_power
and warns once per run; on an E4000 dongle that costs nothing measurable.

**Windows, no Python:** download the `marsad` folder from the releases page, put it anywhere,
and double-click `marsad.exe`. It starts the web app on http://127.0.0.1:8731/, opens it in
your browser, and keeps a console window open that says so; close that window to stop. If the
app is already running (one copy per address, always), the double-click just opens the browser
on it. `Start marsad.cmd` beside the exe does the same and keeps the window open if anything
goes wrong, so an error can be read. For the commands, open a terminal in that folder.

**From source (any OS):**

```bash
py -m venv .venv
.venv\Scripts\python.exe -m pip install -e .
.venv\Scripts\marsad.exe --help
```

On Linux or macOS use `python3 -m venv .venv`, `.venv/bin/pip install -e .` and `marsad`.

## First run

```bash
marsad init                 # writes station.json; open it and describe your receiver
marsad run --list           # the presets it defines
marsad run LS 5m --tag test --dry-run
marsad run LS 5m --tag test
```

Do a short run before a long one. A 5 minute test once caught that rtl_power silently
stops at 2147.48 MHz (a 32-bit limit) before a 6 hour survey wasted the night on it.

## The station file

`station.json` holds everything that is particular to *your* station, and nothing that is
true of every RTL-SDR. Edit it:

- **receivers**: each dongle's tuner (`e4000` or `r820t`, which decides the gain ladder),
  its tuning range, any PLL gap it cannot tune (the E4000 has one; measure it with
  `rtl_test -t`), and its USB device index.
- **spur_families_mhz**: harmonic families the analyser tags automatically, such as the
  28.8 MHz clock. Order matters, and changing the list changes every `peaks.csv`, so do it
  between comparison sets.
- **presets**: named ranges for `marsad run`.
- **retune_ms_per_hop**: how long rtl_power loses between hops. 47 ms was measured on one
  PC; run `marsad calibrate-retune --save` to measure yours. It sets the "listening fraction"
  the dry run reports.
- **detection_floor_db**: the smallest change `compare-runs` will believe, by whether you
  touched the hardware between runs.
- **known_mhz** and **chain_modules**: what the scan builder shows for a range, and the
  physical rig it offers to record.
- **defaults**: the last answers to the sidecar questions. `marsad run` rewrites this section
  and leaves the rest alone.

Keys starting with `_` are notes to yourself and are ignored.

## How a survey is kept comparable

Every run writes `surveys/<name>.json` beside the CSV with the full command, the gain, bin
width, integration, crop, FIR, receiver, the physical signal chain and the conditions, and
`reports/<name>/scan_build.md` with the geometry rtl_power derived and a one-line
fingerprint:

```
1241.000-2147.484 MHz | 43.7154 kHz | g29 | i120 | c0.25 | F9 | xtr
```

Two surveys can be compared directly only if their fingerprints match. `compare-runs`
refuses to pretend otherwise, and `archive new` only looks for new signals among surveys
with the same fingerprint.

Things the tools learned the hard way, now built in:

- rtl_power picks its own bin width (the next power-of-two FFT), so the fingerprint uses
  the *actual* width read back from the CSV.
- Max-hold and detection counts grow with sweep count. Compare steady signals on their
  median over sweeps and intermittent ones on occupancy; use `--max-sweeps` to compare a
  long run fairly with a short one.
- A single spur line moves by over a dB between identical runs. The family statistic
  averages across every harmonic and pairs them run to run, which is what makes sub-dB
  changes measurable.
- The analyser treats rtl_power's non-finite readings (`-nan(ind)` on Windows) as missing
  bins, not as broken lines, and reports their fraction: above 1% the front end is
  saturating and peak levels are lower bounds.
- Retuning costs tens of milliseconds per hop, so a short integration on a wide span
  spends most of its time not listening. The dry run shows the split.

## The web app

`marsad` (or `marsad live` without opening a browser) serves `http://127.0.0.1:8731/`:

- **Scan**: the survey being recorded, live: spectrum with max-hold, waterfall, per-sweep
  median, hover readout naming the nearest harmonic family. Or any finished survey from the
  dropdown. Wheel zooms, drag pans, shift+wheel scrolls history.
- **Builder**: compose a scan. Slider on the tuner range, the geometry rtl_power will use,
  the dwell split, the signal chain and conditions, and the complete `marsad run` command to
  copy. It warns about the 2^31 ceiling, a PLL gap, a hop centre landing on your target,
  and a 50 ohm load run whose tag does not say so.
- **Archive**: every CSV, and what `occupancy.db` holds.
- **Roadmap**: `TOOL_ROADMAP.md` from the station folder.

Starting a survey with `marsad run` reuses an open viewer; it follows the newest CSV.

## Layout of a station folder

```
station.json      your station (see above)
rtl-sdr/          rtl_power.exe and friends (optional; PATH works too)
surveys/          raw rtl_power CSVs and their .json sidecars - never delete these
reports/          one folder per survey: report.md, scan_build.md, plots, peaks.csv, bins.csv
recordings/       ADS-B captures
occupancy.db      the archive
```

## For developers

```bash
.venv\Scripts\python.exe -m pytest           # regression suite
.venv\Scripts\pyinstaller.exe --noconfirm packaging\marsad.spec   # dist\marsad\marsad.exe
```

The tests pin the numbers the measurement programme depends on: a `scan_geometry` that
reproduces rtl_power's arithmetic for three measured runs, four archived reports
regenerated byte for byte, and the recorded statistics (`-0.14 dB, std 1.36, n=31`;
`+0.050 dB median`; `settled by sweep 4`). Tests that need the station's raw data skip
elsewhere. **A change that moves any of those figures is a bug, not a refactor.**

`tools/*.py` are thin wrappers that keep the original command lines working; the code is in
`marsad/`. Universal rtl_power facts live in `rtlpower.py`, station facts in
`station.json`, and the two OS-specific operations (finding running SDR programs, stopping a
child cleanly so it flushes its output) in `host.py`.

Levels are relative dB from rtl_power, never calibrated dBm.
