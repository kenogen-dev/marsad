# Marsad roadmap

What the tool does today, and where it could go. The aim: anything you would otherwise have
to work out by hand, for a radio survey on a cheap dongle, belongs in a tool.

---

## Now

| Command | What it does |
|---|---|
| `marsad run <preset> <duration>` | Records a survey, writes the sidecar (typed chain and conditions plus measured weather, sun and PC state) and a build sheet, analyses, archives. Refuses a stale chain, a busy SDR, a gain the tuner does not have, a span rtl_power cannot plan |
| two engines | **rtl_power** (`rtl_power_gainfix` where built: keeps the R820T's gain, sums in 64 bits) for surveys; **rtl_power_fftw** above 2147 MHz, for very narrow bins and single-hop work. The fingerprint names the engine |
| `marsad analyze` | Grid, floor, signals, spur tags, plots, peaks.csv, bins.csv, and an Events section; reads either engine's file |
| `marsad events <tag>` | Broadband steps and one-sweep transients from the per-sweep median of each block and the whole band |
| `marsad compare-runs` | The whole A/B in one command: fingerprint check, floor by block against the detection floor, the family statistic with error bars and a verdict |
| `marsad spur-stats`, `marsad drift` | The harmonic-family statistic; warm-up and thermal drift |
| `marsad adsb-check` | 1090 MHz positive control |
| `marsad archive` | `occupancy.db`: every survey with fingerprint, signals and pooled bands |
| `marsad response` | Antenna return loss and SWR through a bridge, with trust flags |
| `marsad characterise` | Measures an IQ recording (bandwidth, pulses, PRIs, chirp shape, rotation, modulation family) and writes a recipe for the next capture |
| `marsad watch fixed / survey / list` | The triggered recorder. Private bands: fingerprint only, never IQ |
| `marsad web status / start / stop / restart` | The web app on 8731, in the background with no window |

**Web app tabs**: Scan, Builder, Archive, Captures, Chain, Probe, Roadmap.

---

## Next

- **Run from Linux and macOS end to end**: the OS-specific parts are few and live in `host.py`.
- **Log idle time through a run**, not only at its start and end, so a line that follows the
  display sleeping can be tested.
- **Per-radio frequency correction**: the setting exists (`recorder.ppm`); a command to measure a
  dongle's offset on a carrier it hears would finish it.
- **Band tables by region**: the private-band table follows North American allocations.
- **14.4 MHz family in the spur tagger**, and an odd/even half-harmonic test.
- **Trend plots** from `occupancy.db`: frequency versus time across the whole archive.

---

## Later

- **rtl_433, URH and SatDump workflows** for what the surveys find (your own devices only).
- **Resonance helper**: which bands moved between two antenna runs, and what length resonates there.

---

## Rules the tools exist to enforce

- **Fingerprints**: range, bin, gain, integration, crop, FIR, receiver, engine. Runs that do not
  match are not comparable, and the tools refuse.
- **Max-hold and detection counts grow with sweep count.** Never compare them across runs of
  different length. Medians and occupancy percentages are immune.
- **Handling between runs costs far more than elapsed time.** Error bars depend on what moved, not
  on how long the run was.
- **Warm up first.** Measure your dongle's settling with `marsad drift` and discard that much.
- **An effect cannot be seen through noise larger than itself.** Before believing a null, check
  what else was in the band, and put a known signal in the span.
- **A matching number is not evidence.** Only predictions that could fail settle anything.
- **A tool can silently ignore a setting.** When a setting matters, check that changing it moves
  the levels.
- **A run should describe its own rig.** `--chain` on every run.
- **One program per radio.**
- **Private communications are catalogued, never kept.** Cellular, pager and cordless bands get a
  fingerprint (shape and timing, not decodable) and a PRIVATE flag; never raw IQ.
- **Your location stays yours**: it lives in `station.local.json`, which git ignores.
