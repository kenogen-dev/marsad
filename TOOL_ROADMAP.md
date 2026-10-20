# Marsad tooling — where it has been, where it is, where it goes

The guiding aim: **run the station without Claude in the loop.** Claude is for designing
experiments and arguing about results, not for routine operation. Anything you would otherwise
have to ask it to compute belongs in a tool.

---

## Now

| Tool | What it does |
|---|---|
| `run_survey.py` | Records from a preset, derives the exact rtl_power geometry, writes a sidecar and a build sheet, analyses, archives, and sets the terminated/antenna flag from the chain. Beeps and raises a Windows toast when the recording ends and again when the report is ready (`--no-notify` to silence) |
| `survey.py` | Parses the CSV, builds the time x frequency grid, finds signals, tags spurs, plots |
| `spur_stats.py` | The pre-registered statistic: harmonic family mean above the whole-band median, paired difference with a real error bar |
| `compare_runs.py` | **The whole A/B analysis in one command**: fingerprint check, whole-band median, floor block by block against the measured detection floor, the family statistic with error bars and a verdict, periodicity. Validated against the recorded A1/A2 control |
| `adsb_check.py` | 1090 MHz positive control. Pass/fail on whether the chain hears anything at all |
| `archive.py` | `occupancy.db` — every survey, with fingerprints, signals and pooled bands |
| `drift.py` | Per-sweep whole-band median: warm-up, settling point, and a pass/fail on thermal drift. Validated against both recorded figures |
| `live_view.py` | Watches a running scan: spectrum, waterfall, per-sweep median trend, hop cursor, hover readout. Also browses finished surveys. One viewer on port 8731 **follows the newest CSV** (since 2026-10-19), so a tab left open shows the next run; `run_survey.py` reuses it instead of starting another. `--pin` stays on one file. Wheel zooms the spectrum and waterfall together, drag pans, shift+wheel scrolls waterfall history one sweep per pixel, double-click resets; the header says when the scan finished |
| `scan_builder.html` | Composes a scan. Published as an Artifact; works offline |

**Live view tabs**

- **Scan** — live or archived, picked from the dropdown. Frequency grid on both charts, sweep
  numbering on the waterfall, hover readout with harmonic identification.
- **Archive** — every CSV in `surveys/` with receiver, gain, integration, sweeps and range.
  Click a row to open it.
- **Roadmap** — this file.

---

## Next

Ordered by how much Claude-dependence each removes.

1. **Odd/even half-harmonic test** — confirms whether a spur family's fundamental is what it is
   assumed to be. This is how 14.4 MHz was found to be the real fundamental, not 28.8.
2. **Resonance hunter** — given a before/after pair, report which bands moved and what physical
   length would resonate there. The cage investigation was this, done manually, for a day.

---

## Later

- **Packaging for distribution** — `pyproject.toml`, one CLI, config split from code, POSIX
  branches, regression tests. Only matters if the tools are ever shared; it buys no independence
  on its own. ~54 station-specific lines and 19 config declarations stand in the way.
- **Git** — worth doing before the tools get much larger, independently of publishing.
- **rtl_power_fftw adapter** in `survey.py`, for hydrogen-line work.
- **Trend plots** from `occupancy.db` — frequency versus time across the whole archive.

---

## Rules the tools exist to enforce

These came out of being wrong, each one more than once.

- **Fingerprints**: range, bin, gain, integration, crop, FIR, receiver. Runs that do not match
  are not comparable, and the tools should refuse rather than let you.
- **Max-hold and detection counts grow with sweep count.** Never compare them across runs of
  different length. Medians and occupancy percentages are immune.
- **Handling between runs costs ~0.4 dB; touching nothing costs 0.05 dB.** Error bars depend on
  what moved, not on how long the run was.
- **An effect cannot be seen through noise larger than itself.** The dangling-stub test measured
  "no change" at a time when a faulty cable was adding 5 dB to the same band; the stub's real
  contribution is about 0.8 dB. Before believing a null, check what else was in the band.
- **A matching number is not evidence.** Four mechanism hypotheses matched the observed
  frequency or magnitude; three were wrong. Only predictions that could fail have settled
  anything.
- **A run should describe its own rig.** `--chain` on every run, because `station.json` carries
  the last run's wording and has mislabelled runs before.
