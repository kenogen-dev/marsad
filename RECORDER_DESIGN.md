# Triggered recording for marsad: design (draft for review, 2026-10-27)

The surveys say *where and when* something transmits. Nothing keeps the signal itself, so a
find like 341.31 MHz (V1-4f) or the 1265.44 MHz radar can only be studied if someone happens to
tune SDR++ to it. This design adds a recorder that notices interesting signals during a survey,
captures them, and builds a **catalogue of everything the station hears**, so nothing unusual
goes unrecorded just because nobody was looking for it.

Built so far: phase 1 (`marsad characterise`, 2026-10-27) and phases 2 and 3 (`marsad watch`,
2026-10-28, tested offline; their radio tests are still owed). Section 9 records where the build
differs from this draft. Each phase has a pre-registered test, the same way RUNSHEET blocks do.

---

## 1. The shape

```
 XTR: survey (rtl_power, unchanged)           v5: recorder
        |                                           ^
        v                                           | "tune 341.31, record 30 s"
 live tailer (already exists) --> Watcher --> Trigger rules --> Recorder
                                                                  |
                                       Catalogue (occupancy.db) <-+-> capture files
                                              ^                       |
                                              +---- Characteriser <---+
                                                    (what is it? what would record it better?)
```

- **Watcher** reuses the live viewer's tailers (`live.py`), which already parse each pass of a
  CSV or `.dat` as it lands. It compares each pass against the run's floor.
- **Trigger rules** decide whether a detection is worth a capture (section 3).
- **Recorder** drives the second radio: `rtl_sdr` for N seconds at the target, then cuts the raw
  stream down to a narrow band around the signal and writes it with a SigMF description file.
- **Catalogue**: a new `captures` table in `occupancy.db`, one row per event, beside the
  existing `surveys`, `signals` and `bands`.
- **Characteriser** is the post-processor you asked for (section 5): it measures what was caught
  and writes the settings the *next* capture of that signal should use.

**Why the survey runs on one radio and recording on the other.** A dongle can only be used by one
program. With the XTR surveying and the v5 recording, **survey fingerprints do not change at all**:
the survey is the same rtl_power run it always was, so the whole archive stays comparable. That
follows the comparability principle without any trade.

## 2. Modes, in build order

| Mode | What | Needs |
|---|---|---|
| **Fixed watch** | One radio parked on a known frequency, recording whenever it is active | One radio; the simplest; proves the recorder and characteriser |
| **Two-radio trigger** (main mode) | XTR surveys, v5 records what the survey flags | Both radios on air at once (pinned roadmap item: second antenna or a splitter) |
| **Sweep and pounce** (later, maybe) | One radio sweeps, stops on a trigger, records, resumes | A third engine that drives the dongle directly; the survey gets holes |

### Two radios: what changes about timing (added 2026-10-27)

- **The recorder's tuning time doesn't matter.** It tunes once per capture (~0.1 s) and then
  listens for 30 s or more, so it spends over 99% of its time listening.
- **What sets "how fast we pounce" is the survey's revisit time**, not tuning. A signal is checked
  once per pass, and the trigger wants two passes, so the delay from first sight to recording is up
  to two pass times: about 1 min on milair at `-i 30`, 4 min on LS at `-i 120`. Faster tuning saves
  milliseconds; a shorter pass saves minutes. That is why the ambush rule exists.
- **Shark versus orca.** With one radio, the survey has to be both the finder and the measurer, so
  long dwell (`-i 120`, 83% listening) is right. With two, the survey only has to *find*, while the
  recorder does the long look. A spotter tuned for short revisit becomes worthwhile later, but it
  would be a new fingerprint. So the archive presets stay as they are, and any spotter preset is a
  separate, named comparison set.
- **Longer runs don't change the split.** A 12 or 24 h run has exactly the same
  listening/retuning split per pass as a 1 h run. It adds passes: better occupancy statistics and
  day/night coverage. Run length is chosen for the question, not for efficiency.
- **Telling the recorder where to tune: a per-radio frequency correction.** Both radios have
  0.5 ppm reference oscillators, but each is off by its own small amount: about 0.2 kHz at 385 MHz,
  growing with frequency. A tuning error stops mattering once the capture is cut 2-3x wider than the
  signal. Even so, the recorder applies a measured offset per radio, taken from steady carriers both
  radios hear (385.03, 299.999 MHz). It also places the signal off-centre (the v5 has no DC spike,
  but the XTR does, if roles ever swap). So the hand-off is: survey says "385.0320 on the XTR";
  the recorder tunes the v5 to that plus its correction and plus the offset, with no search needed.

## 3. Trigger rules

A detection triggers a capture when **all** of these hold. Every number is a station.json setting.

- **SNR at least 12 dB** over the pass's local floor (the survey's own detection threshold is 10).
- **Seen on 2 consecutive passes**, so a single click does not fire it. A separate "burst" rule
  with a lower bar can be added once fixed watch shows what short signals look like.
- **Not a known artefact**: clock harmonics (14.4 / 24 / 25 / 28.8 MHz families), hop-centre DC
  spikes and hop-wide impulses are already tagged by `analyze.py` and never trigger.
- **Cooldown per frequency** (default 30 min), so a steady carrier is captured once, not every
  pass. Steady known carriers (385.03, 299.999) get **one reference capture a day**.
- **Capture length** default 30 s, at most 120 s.
- **Storage** (revised 2026-10-27, owner's offer of a 500 GB drive): a recorder pool of **100 GB**
  in its own folder, with a soft daily cap of **20 GB**. Nothing is ever deleted automatically
  (CLAUDE.md: never delete recordings without asking). When the pool fills, the catalogue keeps
  logging events and fingerprints, IQ writing stops, and the web app says so. The owner reviews
  and frees space. Both numbers are station.json settings. A typical day is under 1 GB, so the cap
  is a guard against a stuck trigger, not a working limit.

**A burst rule ("ambush")**: a signal seen on only one pass but strong (default 20 dB) does not get
a capture of itself, because it will be gone by the time the recorder tunes. Instead the recorder
**parks on that frequency for a while** (default 10 min, or until something with a higher priority
triggers), recording if it comes back. Short repeating emitters like the radar are caught this way;
a pure one-off is still catalogued from the survey pass alone.

## 4. Every event catalogued; two levels of record

This is where your "keep it all, flag it" idea and the station's ground rule meet. **Every
trigger, in every band, gets a catalogue row.** Nothing is filtered out of the catalogue, so an
anomaly anywhere in the spectrum is always on record. What differs is how much of the signal is
kept:

| Band tier | Catalogue row | Fingerprint | Raw IQ (listenable, decodable) |
|---|---|---|---|
| **open**: radar, satellites, telemetry, aviation navigation, ISM / own devices, unidentified | yes | yes | **yes** |
| **private**: cellular up/downlink (700, 850, AWS, PCS), pagers, cordless / DECT, private land-mobile | yes, flagged **PRIVATE: do not decode** | yes | **no** |

The **fingerprint** is what makes an anomaly inside a private band still catchable without holding
anyone's communication. It is kept for every event:

- frequency, occupied bandwidth, peak and median SNR, start, duration, how often it repeats;
- a **power spectrogram** at 20 ms time resolution and about 1 kHz frequency resolution. That shows
  shape, timing, pulses, chirps and hopping, and lets the characteriser tell "LTE-shaped" from
  "a CW carrier that has no business being in an LTE band". At 20 ms per frame it **cannot be
  demodulated**: pager symbols are 0.16-0.8 ms and voice needs phase, which a power spectrogram
  throws away.

If a private-band event's fingerprint does **not** look like the service that owns the band (a CW
carrier, a radar-like pulse train), the catalogue flags it **ANOMALY: review**. You can then decide,
case by case, to aim a fixed-watch capture at it.

**Why not record the private bands too and just flag them?** I considered it, as you suggested,
and I'd recommend against it, for three reasons:

1. **A flag doesn't change what is on the disk.** Raw IQ from a pager channel *is* the messages:
   paging is unencrypted, so anyone with the file can decode it. "Do not decode" written beside it
   doesn't make the file different.
2. **The law cares about capture, not decoding.** In the US, intercepting cellular and paging
   traffic is restricted by federal law (ECPA) regardless of whether you ever play it back.
   Recording raw IQ of those bands is the part that is restricted.
3. **The fingerprint already covers your real worry.** The anomalies you don't want to miss are
   things that *don't* belong in a band. Their shape and timing are exactly what the fingerprint
   keeps, and the ANOMALY flag brings them to you.

So the catalogue is complete across the whole spectrum, and the station's ground rule (CLAUDE.md:
identify signal types and occupancy, never store the content of private communications) holds.

The band tier table lives in station.json, so it can be refined. The default for **anything not in
the table is open**: unidentified signals are exactly what we want recorded in full.

## 5. The characteriser (post-processor)

It runs on each finished capture and does two jobs.

**What did we catch?** It measures, all from techniques already used by hand on this station:

- occupied bandwidth and centre offset;
- duty cycle, pulse width, pulse repetition interval and stagger (the 1265 radar analysis);
- chirp: start/end frequency and sweep rate;
- rotation or repeat period from pulse-train envelopes (the radar's 12.000 s);
- modulation family by simple features: CW, AM-like, FM-like, FSK (symbol rate from the envelope),
  OFDM-like (flat-topped, steep edges), noise-like / broadband;
- for fingerprint-only events, the same measurements where a power spectrogram allows them.

**How should the next capture be set?** From those measurements it writes a per-frequency
**recipe** into the catalogue that the recorder uses next time:

- sample rate about 2.5x the occupied bandwidth (no wider than needed, so files stay small);
- centre placed so the signal sits clear of the tuner's DC spike;
- duration long enough for several repeats (a 12 s rotation gets 90 s, a CW carrier 10 s);
- gain lowered if the capture came close to clipping, raised if the SNR was marginal.

So the second capture of anything is better than the first, automatically. That loop works for
open-band signals; for private-band events the recipe only improves the fingerprint.

## 6. Disk sizes

| What | Rate | 30 s capture |
|---|---|---|
| Raw stream, 2.4 MS/s, 8-bit | 4.8 MB/s | 144 MB (never kept) |
| Narrow cut, 50 kHz complex 16-bit | 0.2 MB/s | **6 MB** |
| Narrow cut, 1.4 MHz (radar chirp) | 5.6 MB/s | 168 MB |
| Fingerprint spectrogram | ~10 KB/s | 0.3 MB |

A busy day of 100 narrow captures is under 1 GB. Wideband captures like the radar are what the
daily budget is for.

## 7. Phases and their pre-registered tests

1. **Characteriser on old data.** Feed it the 105 s radar recording from 2026-10-14 (SDR++
   recordings folder). *Pass:* it reports a rotation of 12.0 +/- 0.1 s, pulse width 114 +/- 5 us,
   a linear chirp of about 1.4 MHz, and staggered PRIs near 2745 / 3157 / 3473 us. Needs no radio.
2. **Fixed watch on 1265.44 MHz**, 1 h, v5. *Pass:* at least 250 of the ~300 expected scans
   produce an event, captured files total under 2 GB, and the recipe after the first capture
   narrows the rate to cover the chirp without excess.
3. **Two-radio trigger**, XTR on milair, v5 recording, 2 h. *Pass:* 341.31 MHz and the
   385-388 MHz trunked carriers produce events; no clock harmonic or hop-centre spike triggers;
   cellular-band events are fingerprint-only with the PRIVATE flag; disk use within budget.
4. **The web app**: a Captures tab listing the catalogue, with each event's spectrogram, its
   flags, and a play / open-in-SDR++ button for open-band captures.

## 8. Not in this design

- **Retune speed-up**: a separate experiment. First measure where rtl_power's ~47 ms per hop goes
  (tuner lock, USB restart, buffer flush). Then decide whether a second rtl_power patch that keeps
  the stream running is worth it. Keep it only if it saves at least 15 ms per hop; at `-i 120` on
  LS that is roughly 83% -> 88-91% listening, with no fingerprint change.
- **Decoding** anything. The recorder captures and characterises; decoders (rtl_433, SatDump, URH
  on own devices) stay separate tools, as now.

## 9. As built (phases 2 and 3, 2026-10-28)

Code: `marsad/capture.py` (the engine: stream, events, cutter, SigMF, fingerprints, catalogue)
and `marsad/watch.py` (the commands, the trigger rules, the dispatcher). Settings: the `recorder`
section of station.json (defaults in `marsad/station.default.json`). Tests: `tests/test_watch.py`,
with `tests/fake_rtl_sdr.py` standing in for the radio.

```
marsad watch fixed --freq 1265.44M --receiver v5 --duration 1h
marsad watch survey milair 2h --chain "..." --conditions "..." --recorder-chain "..."
marsad watch list
```

Where the build differs from the draft above, and why:

- **An event is the band rising 12 dB over its own floor**, judged per ~0.85 ms FFT frame inside
  each 20 ms block (so a 114 us pulse is not averaged away), in sub-bands of at least 4 bins (so
  noise alone cannot reach 12 dB), and closed after 1 s of quiet. The floor is the median of quiet
  rows, capped at 3 dB over the band's typical bin so a steady carrier cannot become its own floor.
- **Fixed watch catalogues every event but writes IQ once per cooldown** (30 min per frequency).
  Writing every radar scan at 2.4 MS/s would be ~6 GB an hour. One capture covers the recipe's
  duration (90 s = 7.5 rotations for the radar), which is what the characteriser needs anyway;
  the events in between are rows with a fingerprint and `iq_status` "cooldown".
- **The radar gives two events per rotation**: the main beam (~40 dB) and a ~13 dB lobe ~6 s
  later (the "heard twice per rotation outside the main beam" of the characteriser). Count scans
  on events of 25 dB or more.
- **The first capture of anything with no recipe keeps the full 2.4 MS/s** (on the v5 centred, on
  the XTR a quarter band above DC), because its bandwidth is not yet known. A survey trigger
  already knows the line's width and is planned from it.
- **The cut rate is the radio rate divided by a whole number**, at least 2.5x the occupied
  bandwidth (so slightly more, e.g. 60 kS/s for a 20 kHz signal). A whole-number decimation keeps
  the filter continuous across blocks, which a test pins bit for bit.
- **The recipe carries two extra fields**: `gain_db_used` (the gain nudge is applied to the gain
  the capture was made at, not to the default, or the gain would oscillate) and `occupied_hz` (so
  the watched band follows the signal and not the capped cut rate).
- **Phase 2's "recipe narrows the rate" cannot hold for the radar.** On the saved recording the
  recipe says 1.79 MHz occupied, rotation 12.00 s, duration 90 s, and keeps 2.4 MS/s: 2.5x of
  1.79 MHz is more than the radio can sample stably. The test should read "the recipe keeps the
  chirp whole and sets the duration from the rotation". Offline: 9 main-beam events in 104 s at
  12.0 s spacing, one 30 s capture of 298 MB; an hour projects to ~1.2 GB.
- **385-388 MHz is private** in the default tier table ("private land-mobile", the trunked system
  found in M1). Its events, including the daily reference capture of 385.03, are fingerprint-only.
  The fingerprint keeps the carrier's peak frequency (1 kHz bins, interpolated), which is enough to
  start the per-radio frequency correction without holding any IQ. 299.999 MHz is open.
  **Owner's call** whether 380-400 should stay private; it is one row in station.json.
- **Reference carriers are exempt from the artefact tags.** 299.999 MHz sits on the 100 MHz and
  25 MHz families and would otherwise never trigger. The 600 kHz comb is not an artefact tag for
  the recorder at all (a coincidence match ~7 % of the time); 14.4 MHz was added (cheap).
- **The survey radio's own error is taken out before the recorder's is put in**: true frequency
  = survey frequency / (1 + ppm_xtr), asked of the v5 as true / (1 + ppm_v5). Both 0 until
  measured.
- **Survey mode is a wrapper**: `marsad watch survey` finds both radios by serial, starts an
  ordinary `marsad run ... --device <XTR index> --non-interactive --stop-file ...` as a child (so
  the survey, its fingerprint and its files are byte-identical to a plain run), and follows its
  CSV or .dat with the live viewer's tailer, every completed pass. `marsad run` itself is
  unchanged. Ctrl-C asks the survey to finish through its stop file and lets the current capture
  close.
- **The ambush leaves a row of its own** (mode "survey pass") from the survey's measurement, so a
  pure one-off is catalogued even when it never comes back. A two-pass trigger pre-empts it.
- **"captured" means "the recorder dealt with it"**: a capture, a fingerprint-only session, or a
  frequency outside the recorder's range (row with `iq_status` "outside the recorder's range").
  The cooldown is read from these rows, so a restart does not re-capture.
- **The rtl_sdr log** (`rtl_sdr.log`, its stderr: tuning, gain, dropped samples) sits in the pool.
