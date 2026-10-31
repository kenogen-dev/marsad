# Marsad tooling — where it has been, where it is, where it goes

The guiding aim: **run the station without Claude in the loop.** Claude is for designing
experiments and arguing about results, not for routine operation. Anything you would otherwise
have to ask it to compute belongs in a tool.

*Updated 2026-10-30. marsad 0.5.0 is released on GitHub (kenogen-dev/marsad) with a Windows zip;
everything below is in it.*

---

## Now

**Recording and analysing**

| Command | What it does |
|---|---|
| `marsad run <preset> <duration>` | Records a survey, writes the sidecar (typed chain and conditions plus measured weather, sun and PC state) and a build sheet, analyses, archives. Refuses a stale chain, a busy SDR, a gain the tuner does not have, a span rtl_power cannot plan. `--chain` and `--conditions` together skip the station-notes questions |
| two engines | **rtl_power** (the station's patched `rtl_power_gainfix`, the default since 2026-10-27: keeps the v5's gain, sums in 64 bits, no overflow NaNs) for surveys; **rtl_power_fftw** above 2147 MHz, for very narrow bins and single-hop work. The fingerprint names the engine, and v5 rtl_power runs end `| gainfix` |
| `marsad analyze` | Grid, floor, signals, spur tags, plots, peaks.csv, bins.csv, and an **Events** section with `events.csv`; reads either engine's file |
| `marsad events <tag>` **(new)** | Broadband steps and one-sweep transients from the per-sweep median of each 25 MHz block and the whole band; warm-up reported as warm-up, never as an event. Found V1-4n's 02:20-05:11 emitter and A3's two lifts unaided |
| `compare_runs.py` | The whole A/B in one command: fingerprint check, floor by block against the detection floor, the family statistic with error bars and a verdict |
| `spur_stats.py`, `drift.py` | The pre-registered harmonic statistic; warm-up and thermal drift from the per-sweep median |
| `adsb_check.py` | 1090 MHz positive control |
| `archive.py` | `occupancy.db`: every survey with fingerprint, signals and pooled bands |
| `marsad response` | Antenna return loss and SWR through the bridge, with honest trust flags |
| `marsad characterise` | Measures an IQ recording (bandwidth, pulses, PRIs and stagger, chirp shape, rotation, modulation family) and writes a recipe for the next capture |
| `marsad watch fixed / survey / list` | The triggered recorder. `fixed` parks one radio on a frequency; `survey` runs a normal `marsad run` on the XTR and records what it finds on the v5 (2 passes at 12 dB, ambush at 20 dB, clock lines and hop centres never). Cooldown and "same signal" go by the signal's occupied band, not 25 kHz. Private bands: fingerprint only, never IQ |
| `marsad web status / start / stop / restart` | The 8731 web app, in the background with no window, started by `marsad run` when it is down |

**Web app tabs** (http://127.0.0.1:8731/, the only address)

- **Scan**: live or archived, either engine; refused hops hatched; Stop works on any run; a
  **second-radio panel** shows the other recording or a running watch's spectrum.
- **Builder**: composes a scan for either engine, shows what the program will do, the time
  split, and a fingerprint made by marsad's own function (so it matches the run's).
- **Archive**: every survey, click to open.
- **Captures**: the recorder's catalogue grouped by signal, fingerprints, IQ waterfall and
  envelope, exports for SDR++ and Inspectrum, **archive to a verified zip** and restore;
  storage strip and live recorder status on every tab.
- **Chain**: the rig as a drawing; "Use for runs" makes it the default chain.
- **Probe**: near-field catalogue of room devices by state, **Hunt** mode, loudest-devices table.
- **Roadmap**: this file.

**Done since 2026-10-27:** gainfix as the default rtl_power; recorder phases 1-4 (characterise,
fixed watch, two-radio survey watch, Captures tab) and capture archiving; the first two-radio watch
on air (`watch2-4h`, 2026-10-30); the second-radio panel; cooldown by occupied band (after the
345 MHz hump took 9 captures); the event finder; the test suite from 2 min 12 s to ~40 s;
`station.local.json` for the location, kept out of git; marsad.exe rebuilt with all of it.

---

## Next

In order. Each step gets a pre-registered pass test before it is built (RECORDER_DESIGN.md).

1. **Does the 02:19-05:12 broadband emitter repeat?** It lifted 225-349 MHz by about +0.8 dB on
   the night of 2026-10-29, on for 2 h 52 min from 02:19, which fits a scheduled task. Tonight's
   12 h `watch survey milair` answers it: `marsad events` on the new run, same window or not.
2. **The 316-349 MHz source at the wall spot.** Direct at the wall spot the XTR hears ~3.5 dB over
   its terminated floor there (the terminated floor matches M1-T, so it is received), and it
   was not there on 2026-10-26. The isolator is cleared. Next: antenna on, one candidate unplugged
   at a time (the XTR's new USB cable, the v5 on the wall), 40 min each.
3. **The 345.2-345.8 MHz pulsed hump**: ~650 kHz wide, sparse ~200 us pulses near 10 Hz and 1 Hz,
   on 16:57-17:07 on 2026-10-30, inside 316-349. Whether it is the same source as item 2 is
   untested. The recorder now catches it once per 30 min.
4. **Log `user_idle_s` through a run**, not only at start and end, so "room lines step with
   display sleep" can be tested (V1-4n could not).
5. **ADS-B on a warm XTR (H3).** Warm, its PLL gap starts at 1086 MHz, so 1090 is inside it and
   rtl_adsb carries on through "PLL not locked". `adsb_check` at g29, 10 min cold then 10 min
   after an hour of streaming, same hour of day; working-then-failing puts ADS-B on the v5.
6. **Per-radio frequency correction**: the setting exists (`recorder.ppm` by serial); still to
   measure each radio's offset on carriers both hear (385.03, 299.999 MHz).
7. **Recorder radio test on 1265.44 MHz** (phase 2's pre-registered pass): the v5, 1 h, at least
   250 of ~300 scans produce an event, under 2 GB.
8. **14.4 MHz family in the spur tagger**: the analyser still tags only the 28.8 MHz half, so
   odd multiples (244.79, 273.60) come out untagged. Then the odd/even half-harmonic test.
9. **Retune measurement**: where rtl_power's ~47 ms per hop goes. A second patch is kept only if
   it saves at least 15 ms per hop.

---

## Later

- **The XTR wall mount**: the XTR to its wall spot like the v5 (equivalence-tested A/B/A against
  the foam before it becomes the standing position, as the v5's was on 2026-10-28).
- **The outdoor box** (pinned 2026-10-22): antenna, notch and LaNA together outside the window in
  a small weatherproof box, LaNA still bias-tee powered, so the coax carries amplified signal.
  Outside measured +3-5 dB in the signal-carrying bands; the largest gain not yet banked.
- **A horn or dish for the hydrogen line** (pinned 2026-10-22): the `h1` preset exists, but the
  21 cm line needs a directional antenna aimed at the sky; research horn and small-dish designs
  before building.
- **Both receivers at once, properly** (pinned 2026-10-27): the splitter works for
  trigger-and-record but costs the XTR arm 4-5 dB; two antennas (dipole on the v5, UWB on the
  XTR) for independent surveys.
- **Spotter preset** for the two-radio setup: short revisit for finding, the recorder does the
  long look. A new fingerprint, so a new comparison set by rule.
- **Trend plots** from `occupancy.db`: frequency versus time across the whole archive.
- **Resonance hunter**: which bands moved between two runs, and what length would resonate there.
- **Upstream report** of the rtl_power gain and overflow bugs to RTL-SDR Blog (the owner posts).

---

## Rules the tools exist to enforce

These came out of being wrong, each one more than once.

- **Fingerprints**: range, bin, gain, integration, crop, FIR, receiver, engine (and the rtl_power
  build on the v5). Runs that do not match are not comparable, and the tools refuse.
- **Keep runs comparable.** Change a setting or tool that breaks the fingerprint only when it is
  worth starting a new comparison set, then collect fresh data under it.
- **Max-hold and detection counts grow with sweep count.** Never compare them across runs of
  different length. Medians and occupancy percentages are immune.
- **Handling between runs costs ~0.4 dB; touching nothing costs 0.05 dB.** Error bars depend on
  what moved, not on how long the run was.
- **The v5 needs 65 minutes to warm** (V1-1b): `--skip-warmup 65` for any level comparison.
- **An effect cannot be seen through noise larger than itself.** Before believing a null, check
  what else was in the band.
- **A matching number is not evidence.** Only predictions that could fail have settled anything.
- **A tool can silently ignore a setting.** rtl_power printed "Tuner gain set" on the v5 for ten
  days while ignoring it. When a setting matters, check that changing it moves the levels.
- **A run should describe its own rig.** `--chain` on every run.
- **One program per radio, and sessions take turns.** A check that the SDR is free cannot see the
  gap between two planned runs; agree who has the radio before starting.
- **Private communications are catalogued, never kept.** Cellular, pager and cordless bands get a
  fingerprint (shape and timing, not decodable) and a PRIVATE flag; never raw IQ.
- **The station's location stays off GitHub**: it lives in `station.local.json`, which git ignores.
