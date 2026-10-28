# Marsad tooling — where it has been, where it is, where it goes

The guiding aim: **run the station without Claude in the loop.** Claude is for designing
experiments and arguing about results, not for routine operation. Anything you would otherwise
have to ask it to compute belongs in a tool.

*Updated 2026-10-27. marsad 0.3.0 is released on GitHub (kenogen-dev/marsad) with a Windows zip;
everything below is on `main`.*

---

## Now

**Recording and analysing**

| Command | What it does |
|---|---|
| `marsad run <preset> <duration>` | Records a survey, writes the sidecar (typed chain and conditions plus measured weather, sun and PC state) and a build sheet, analyses, archives. Refuses a stale chain, a busy SDR, a gain the tuner does not have, a span rtl_power cannot plan |
| two engines | **rtl_power** (the station's patched `rtl_power_gainfix`: keeps the v5's gain, no overflow NaNs) for surveys; **rtl_power_fftw** above 2147 MHz, for very narrow bins and single-hop work. The fingerprint names the engine, and v5 rtl_power runs carry `gainfix` |
| `marsad analyze` | Grid, floor, signals, spur tags, plots, peaks.csv, bins.csv; reads either engine's file |
| `compare_runs.py` | The whole A/B in one command: fingerprint check, floor by block against the detection floor, the family statistic with error bars and a verdict |
| `spur_stats.py`, `drift.py` | The pre-registered harmonic statistic; warm-up and thermal drift from the per-sweep median |
| `adsb_check.py` | 1090 MHz positive control |
| `archive.py` | `occupancy.db`: every survey with fingerprint, signals and pooled bands |
| `marsad response` | Antenna return loss and SWR through the bridge, with honest trust flags |
| `marsad characterise` **(new)** | Measures an IQ recording (bandwidth, pulses, PRIs and stagger, chirp shape, rotation, modulation family) and writes a recipe for the next capture. Phase 1 of the recorder; it rediscovered the 1265 radar to 1 ms and found its chirp is S-shaped |
| `marsad web status / start / stop / restart` | The 8731 web app, which now runs in the background with no window and is started by `marsad run` when it is down |

**Web app tabs** (http://127.0.0.1:8731/, the only address)

- **Scan**: live or archived, either engine; refused hops hatched; Stop works on any run.
- **Builder**: composes a scan for either engine, shows what the program will do, and now the
  **time split**: listening kept / listening cropped away / retuning, live as settings change.
- **Archive**: every survey, click to open.
- **Chain**: the rig as a drawing; "Use for runs" makes it the default chain.
- **Probe**: near-field catalogue of room devices by state, **Hunt** mode (live single-hop
  readings ~6 a second, a line follower with lock, tone and big visual meter), loudest-devices table.
- **Roadmap**: this file.

---

## Next

In order. Each step gets a pre-registered pass test before it is built (RECORDER_DESIGN.md).

1. **Recorder phase 2: fixed watch** on 1265.44 MHz with the v5, 1 h. Pass: at least 250 of ~300
   scans captured, under 2 GB, and the recipe narrows the rate after the first capture.
2. **Per-radio frequency correction**: measure each radio's offset on carriers both hear
   (385.03, 299.999 MHz) so the recorder tunes straight to what the survey saw.
3. **Recorder phase 3: two-radio trigger**: XTR surveys, v5 records. Needs both radios on air at
   once (second antenna or a splitter). Every event catalogued; private bands fingerprint-only.
4. **Recorder phase 4: Captures tab**: the catalogue, each event's spectrogram and flags, play
   or open in SDR++ for open-band captures.
5. **Retune measurement**: where rtl_power's ~47 ms per hop goes. A second patch is kept only if
   it saves at least 15 ms per hop (about 83% -> 88-91% listening on long LS runs, same fingerprint).
6. **14.4 MHz family in the spur tagger**: the analyser still tags only the 28.8 MHz half, so
   odd multiples (244.79, 273.60 on 2026-10-27) come out untagged. Then the odd/even
   half-harmonic test as a command.
7. **Station location in station.json** (owner) so measured conditions include weather and sun.

---

## Later

- **Spotter preset** for the two-radio setup: short revisit for finding, the recorder does the
  long look. A new fingerprint, so a new comparison set by rule.
- **Trend plots** from `occupancy.db`: frequency versus time across the whole archive.
- **Resonance hunter**: which bands moved between two runs, and what length would resonate there.
- **Upstream report** of the rtl_power gain and overflow bugs to RTL-SDR Blog (the owner posts).

Done since the last version of this file: packaging (marsad, 0.3.0 release), git, the
rtl_power_fftw engine and adapter, the always-up web app.

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
