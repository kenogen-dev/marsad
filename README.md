# Marsad

*المرصد, al-marsad: the observatory.*

Marsad (المرصد, "the observatory"): an RTL-SDR spectrum monitoring station with a
comparability fingerprint for every survey, pre-registered A/B statistics, thermal drift and
spur-family tools, an occupancy archive, and a live waterfall. Built around rtl_power on
Windows.

Marsad records, analyses and compares radio spectrum surveys made with an RTL-SDR dongle
and `rtl_power`, with a **comparability fingerprint** on every run so you can only ever
compare like with like.

The tools exist to stop the usual mistakes of amateur measurement: every survey records how
it was made, every comparison checks the settings match first, and the statistics come with
error bars.

## What you get

One command, `marsad`, and a local web app. `sdr` is installed as a short alias for the
same command, so `sdr run wide 1h` and `marsad run wide 1h` are identical.

| Command | What it does |
|---|---|
| `marsad` | opens the web app: live scan, scan builder, archive, roadmap |
| `marsad run wide 1h` | records a survey from a preset, writes a sidecar with every setting, analyses it, archives it |
| `marsad run wide 1h --dry-run` | shows exactly what rtl_power will do (hops, FFT, real bin width, dwell) and records nothing |
| `marsad analyze surveys/x.csv --receiver r820t` | report, plots, `peaks.csv`, `bins.csv` |
| `marsad compare-runs before after` | the A/B statistics: whole-band median, floor by block, harmonic family with its error bar, a verdict |
| `marsad spur-stats a/bins.csv b/bins.csv` | the harmonic-family statistic on its own |
| `marsad drift <tag>` | did the receiver drift as it warmed? |
| `marsad adsb-check` | proves the receive path works, in 60 s, by counting aircraft at 1090 MHz |
| `marsad archive list / new / trend / band / seen` | the long-term occupancy database |
| `marsad response --open A --load B --antenna C` | an antenna's return loss and SWR at every bin, from three runs through a return-loss bridge |
| `marsad backup export <folder>` | copies the station to a verified zip on another drive; `backup list`, `verify` and `import` restore it |
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
and double-click `marsad.exe`. It starts the web app on http://127.0.0.1:8731/ in the
background, with no window, and opens it in your browser; it stays up between scans, and
`marsad web stop` stops it (`marsad web status` says whether it is up). If the app is already
running (one copy per address, always), the double-click just opens the browser on it. To keep
a second unzipped copy from taking the address while your working folder's app is down, put that
folder's path in `~/.marsad_home`.
`Start marsad.cmd` beside the exe keeps a window open if anything goes wrong, so an error can
be read. For the commands, open a terminal in that folder.

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
marsad run fm 5m --tag test --dry-run
marsad run fm 5m --tag test
```

Do a short run before a long one: rtl_power silently stops at 2147.48 MHz (a 32-bit limit),
for one, and five minutes will show you that before a night is wasted on it.

The shipped `station.json` names two receivers, `r820t` (most RTL-SDRs; the default) and
`e4000`, with generic figures. Rename them, and set what you measure on yours.

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
- **retune_ms_per_hop**: how long rtl_power loses between hops. 47 ms is a starting
  figure; run `marsad calibrate-retune --save` to measure yours. It sets the "listening fraction"
  the dry run reports.
- **detection_floor_db**: the smallest change `compare-runs` will believe, by whether you
  touched the hardware between runs.
- **known_mhz**, **builder_notes** and **chain_modules**: what the scan builder shows for a
  range, warnings of your own for when a scan matches, and the physical rig it offers to record.
- **probe**: the Probe tab's device list and the chain after the probe.
- **recorder**: `marsad watch`'s pool, caps, trigger settings, which receiver does what and
  the private-band table.
- **defaults**: the last answers to the sidecar questions. `marsad run` rewrites this section
  and leaves the rest alone.

Keys starting with `_` are notes to yourself and are ignored.

**`station.local.json`**, beside it, is laid over `station.json` when marsad loads (dicts merged
key by key) and is for what should not be shared: today your **location**,
`{"location": {"lat": 51.48, "lon": -0.01}}` in decimal degrees, which lets every run record the
outdoor weather (Open-Meteo, no key) and the sun's elevation beside your typed conditions. The
repository's `.gitignore` keeps it out of git, and nothing marsad writes ever copies it into
`station.json`.

## How a survey is kept comparable

Every run writes `surveys/<name>.json` beside the CSV with the full command, the gain, bin
width, integration, crop, FIR, receiver, the physical signal chain and the conditions, and
`reports/<name>/scan_build.md` with the geometry rtl_power derived and a one-line
fingerprint:

```
1270.000-1700.000 MHz | 43.7154 kHz | g29.7 | i120 | c0.25 | F9 | r820t | gainfix
```

Two surveys can be compared directly only if their fingerprints match. `compare-runs`
refuses to pretend otherwise, and `archive new` only looks for new signals among surveys
with the same fingerprint.

Built in:

- rtl_power picks its own bin width (the next power-of-two FFT), so the fingerprint uses
  the *actual* width read back from the CSV.
- Max-hold and detection counts grow with sweep count. Compare steady signals on their
  median over sweeps and intermittent ones on occupancy; use `--max-sweeps` to compare a
  long run fairly with a short one.
- A single spur line can move by over a dB between runs. The family statistic
  averages across every harmonic and pairs them run to run, which is what makes sub-dB
  changes measurable.
- The analyser treats rtl_power's non-finite readings (`-nan(ind)` on Windows) as missing
  bins, not as broken lines, and reports their fraction: above 1% the front end is
  saturating and peak levels are lower bounds.
- Retuning costs tens of milliseconds per hop, so a short integration on a wide span
  spends most of its time not listening. The dry run shows the split.
- Every report has an **Events** section (and `events.csv`): broadband steps and one-sweep
  lifts found in the per-sweep median of each 25 MHz block, with warm-up reported as warm-up.
  `marsad events <tag>` runs it on any survey.

## Recording what a survey finds

`marsad watch` is a triggered recorder:

```bash
marsad watch fixed --freq 433.92M --duration 1h                     # one radio parked on a signal
marsad watch survey milair 12h --chain "..." --conditions "..."     # two radios: one surveys, the other records
marsad watch list                                                   # the catalogue
marsad watch report                                                 # the review of the newest watch
```

With one dongle, `watch fixed` finds it by itself. With two, it tells them apart by tuner;
two of the same kind need their serial numbers in `recorder.serials` in `station.json`
(`rtl_eeprom` shows them), and say which does what in `recorder.roles`.

Each event gets a fingerprint (a 20 ms x 1 kHz power spectrogram) and a row in
`occupancy.db`; in open bands the first event of a signal also starts an IQ capture (SigMF),
at most once per 30 min per signal, which `marsad characterise` measures (bandwidth, pulses,
chirp, rotation, modulation family) into a recipe for the next one. **Private bands (cellular,
pagers, DECT and cordless, private land-mobile, from a table in station.json) get the
fingerprint only: their raw IQ never touches disk.** The table shipped follows North American
allocations, so outside North America add your own bands before recording. Storage has a pool and a daily cap, and
nothing is ever deleted; the Captures tab can move a good capture into a verified zip archive.

Before the first capture of a signal the recorder takes a **wide look**: a few seconds at the radio's full
rate, held in memory and dropped, to see whether the signal is pulsed or continuous and how wide it really is.
What it saw sets the capture's width; if it saw nothing (a beam that comes round every few seconds is easily
missed), the whole band is kept, because a survey's width is only a guess. Private bands get no look. `--look 0`
or `recorder.look_s: 0` turns it off.

With a keep level (`--keep-min-snr`, off by default), the first `recorder.keep_check_s` seconds are held in
memory until the recorder's own SNR reaches it. If it does not, nothing is written, but the recorder keeps
listening: a burst later in the same event still starts a capture, with the second before it.

`recorder.known_cooldown_min` (off by default) limits a signal the catalogue already knows well: once
`recorder.known_after` captures of it have a strong or medium grade and a meaningful label, it records at most
that often, and its events stay catalogued. `--dry-run` says whether this is on.

`marsad watch report [survey or part of its name] [--md file]` is the review of a watch, read-only: signals with
their events, captures and GB, events per hour by tier, grades, captures that look empty, events the keep level
refused despite a high peak, and episodes (three or more strong events on two or more signals, close in time and
frequency). Private rows appear only as counts per band and when they happened.

Two open signals with the same pulse spacings and rotation get "same emitter?", a question it does not answer for
you, and it changes no tier, nothing kept and nothing exported.

`marsad watch recharacterise [--survey S] [--ids 1,2] [--limit N] [--dry-run]` measures older captures again after the
characteriser improves. It only adds: `<capture>.characterise.v2.md` and `.json` beside the capture, or for a capture
in an archive zip in `recharacterised/<zip name>/` beside the zip, which is read in place, checked against its
manifest and left untouched. Catalogue rows and old reports stay as they were; the Captures tab and the watch report
show the newer figures with the old label beside them. SNR from before and after is not comparable. It skips private
rows, a capture still being written and captures already made by the current recorder, runs at low priority, and
`recorder.timing_only_bands` (MHz pairs) keeps captures touching those bands to timing, width and level.

`marsad watch tidy [--exclude-survey S] [--before YYYY-MM-DD]` lists the captures graded empty and what each takes,
with a token, and moves nothing. `marsad watch tidy --confirm TOKEN` moves exactly that list, `.sigmf-data` and
`.sigmf-meta` only, to a holding folder outside the pool (`recorder.set_aside`, default `recordings/set_aside/<date>/`),
checked by size and sha256. Rows, fingerprints and characterise reports stay, the row reads "set aside", and the page
offers no IQ or Listen for it. `marsad watch tidy --put-back [--ids 1,2] [--confirm]` returns them with their old
status. **Nothing is deleted**: the holding folder is yours to delete when you are sure, and is not in a backup. Held back and said why:
archived captures, a capture a recorder is writing or that other events are filed inside, anything touching
`recorder.timing_only_bands`, and surveys in `recorder.tidy_exclude_surveys` (a list of names or parts of names).

## Backing up

Your surveys, captures and notes live on one drive until you copy them.

```bash
marsad backup export E:\backups --dry-run      # sizes, free space, warnings; writes nothing
marsad backup export E:\backups                # all groups
marsad backup export E:\backups --include settings,surveys,reports,db
marsad backup verify E:\backups\marsad-backup_20310115-1200.zip
marsad backup import E:\backups\marsad-backup_20310115-1200.zip --into D:\radio
```

An export copies and changes nothing in the station. The groups are `settings` (`station.json`,
`chain.json`, your notes and other files of yours in the folder, not program files, `rtl-sdr`, a
venv or git), `surveys`, `reports`, `db` (`occupancy.db`, taken through sqlite's backup API so a
recorder writing to it cannot tear the copy), `captures`, `archive` and `recordings` (anything else
in `recordings/`). The zip is written as `.part`, every member is read back and hashed against
`manifest.json`, and only then is it renamed. A destination inside the station is refused, and one
on the same physical drive gets a plain warning, because that does not protect against a drive
failure. `station.local.json` (your location) stays out unless you add `--include-location`. Files a
running recorder is writing are left out and named, and raw IQ of a private band can never be
exported. Each export is a full copy: there is no incremental mode yet. `rtl-sdr/` is not copied;
reinstall it after a restore.

An import verifies the whole zip first, restores each file with its hash checked, and refuses, listing
them, if a file already there differs. Into an empty folder it gives a working station: `marsad
--station <folder>` opens it with its archive, captures and settings. It refuses a backup made by a
newer version. The Archive tab has the same export and verify in a Backup panel.

## The web app

`marsad` (or `marsad live` without opening a browser) serves `http://127.0.0.1:8731/`:

- **Scan**: the survey being recorded, live: spectrum with max-hold, waterfall, per-sweep
  median, hover readout naming the nearest harmonic family. Or any finished survey from the
  dropdown. Wheel zooms, drag pans, shift+wheel scrolls history.
- **Builder**: compose a scan. Slider on the tuner range, the geometry rtl_power will use,
  the dwell split, the signal chain and conditions, and the complete `marsad run` command to
  copy. It warns about the 2^31 ceiling, a PLL gap, a hop centre landing on your target,
  and a 50 ohm load run whose tag does not say so.
- **Archive**: every survey, and what `occupancy.db` holds.
- **Captures**: the recorder's catalogue grouped by signal, each event's fingerprint, IQ
  waterfall and envelope, exports for SDR++ (WAV) and Inspectrum / URH (.cf32 + SigMF), and
  archiving; a "recorder running" bar shows on every tab while a watch runs.
- **Chain**: the rig as a drawing; "Use for runs" makes it the default `--chain`.
- **Probe**: near-field probe surveys of the room's devices by state, and a Hunt mode that
  turns the strongest line into sound and a big meter while you move the probe.
- **Roadmap**: where the tool is going (a `TOOL_ROADMAP.md` in your station folder replaces it).

Starting a survey with `marsad run` reuses an open viewer; it follows the newest CSV.

## Layout of a station folder

```
station.json      your station (see above)
station.local.json  your location (optional, kept out of git)
rtl-sdr/          rtl_power.exe and friends (optional; PATH works too)
surveys/          raw rtl_power CSVs and their .json sidecars - never delete these
reports/          one folder per survey: report.md, scan_build.md, plots, peaks.csv, bins.csv
recordings/       the recorder's captures (pool) and archive zips
occupancy.db      the archive
```

## For developers

```bash
.venv\Scripts\python.exe -m pytest           # regression suite, in parallel (pytest-xdist); -n 0 for serial
.venv\Scripts\pyinstaller.exe --noconfirm packaging\marsad.spec   # dist\marsad\marsad.exe
```

The tests run on synthetic data with no radio and no station folder: a `scan_geometry` that
reproduces rtl_power's own arithmetic, a replayed radio through the recorder, generated surveys
through the analyser and the statistics. **A change that moves a pinned figure is a bug, not a
refactor.**

`tools/*.py` are thin wrappers that keep the original command lines working; the code is in
`marsad/`. Universal rtl_power facts live in `rtlpower.py`, station facts in
`station.json`, and the two OS-specific operations (finding running SDR programs, stopping a
child cleanly so it flushes its output) in `host.py`.

Levels are relative dB from rtl_power, never calibrated dBm.
