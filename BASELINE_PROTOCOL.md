# Station baseline protocol (started 2026-10-16)

Purpose: establish, for the first time, what each receiver and each element of each chain
contributes on its own. Everything measured before this date was measured through a chain
that had never been characterised.

**This does not replace the archive.** The sixteen existing surveys stay valid as surveys -
the whip-length study, the diurnal study and the 1265 MHz radar work are all sound. This adds
a foundation beneath them. The only run being redone is the v5 gap survey, which is retracted.

---

## Pre-registered analysis (decide now, not after)

Fix these before the first run. Choosing a statistic after seeing the data is fishing.

| Quantity | Value | Source |
|---|---|---|
| Primary statistic | **mean level of the 28.8 MHz harmonic family above the whole-band median** | the only metric with a measured error bar |
| Detection floor, hardware UNTOUCHED | **~0.05 dB** (std 0.16, SE 0.02, n=70) | overnight vs A2, 2026-10-17 |
| Detection floor, hardware HANDLED | **~0.5 dB** (std 3.4, SE 0.41, n=70) | A1 vs A2, 2026-10-17 |
| Detection floor, single line | **2.7 dB** (2 sigma of 1.36) | same |
| Secondary statistic | whole-band median (repeats to 0.01-0.03 dB) | terminated run pair + USB isolator |
| Never use | peak-finder detection counts; max-hold across runs | both grow with sweep count |

`py tools/spur_stats.py <bins.csv> [bins.csv] --family 28.8` computes this and, given two
files, prints the difference with its standard error and a significance call.

**Warm-up:** pass `--skip-warmup 16` to every run (16 minutes). Most thermal drift is in the
first ~8 sweeps, and the cage phase changes thermal behaviour by design.

---

## Predictions, recorded 2026-10-16 before any data

Written down in advance so they cannot be rationalised afterwards. Each says what is expected,
how strongly, and what result would overturn it.

| # | Prediction | Confidence | What would overturn it |
|---|---|---|---|
| P1 | Run 1a: the broadband pedestal at 1185-1232 MHz **vanishes** when terminated | high | pedestal still present at a similar level with no antenna |
| P2 | Run 1a: the 604.2 kHz comb at 1190-1210 MHz - **genuinely uncertain** | none | - see the confound below |
| P3 | Phase 2: caging moves the 28.8 MHz family mean by **less than 0.5 dB** | high | family mean moves by more than twice its standard error |
| P4 | Phase 2: seam tape (B1 vs B2) makes **no difference** | high | the taped block is measurably quieter |
| P5 | Phase 3: the v5 floor is set by the receiver itself, not by anything downstream | moderate | any single cascade step adds more than ~1 dB |
| P6 | Once the USB is solid, v5 + 46 mm with **no notch** still decodes ~zero ADS-B | moderate | frames appear - then USB was the cause and the overload diagnosis is wrong |

P6 is the one that discriminates between the two live explanations for the 2026-10-16 zero.
Run it before fitting the second notch, or the two variables change together and neither is
attributable.

**The confound in run 1a, on record before the run.** Terminating removes two things at once:
real external signals, *and* the FM overload this chain is known to suffer (it has no notch).
A compressed front end manufactures its own spurious products. So:

- comb **vanishes** -> ambiguous. Either it was external, or it was an artefact of the overload.
- comb **persists** -> conclusive. It is internal or conducted, and has nothing to do with the
  antenna.

Only the second outcome settles anything. The clean test needs the second FM notch: antenna
connected, notch in, no overload, real signals present. If the comb survives that, it is
genuinely external.

## What this programme will NOT establish

Whatever the numbers say, these claims are out of reach and must not be made from this data:

- **Nothing about sensitivity.** Terminated runs measure what each stage *adds* to the floor,
  never what it lets *through*. The v5 on 2026-10-16 had a perfectly ordinary noise floor and
  decoded zero ADS-B frames. Pair every configuration with `adsb_check.py`, or any sensitivity
  claim is unsupported.
- **Nothing about noise escaping toward the antenna.** No terminated run can see it. That needs
  Phase 3 step 7 with the antenna connected and A/B/A interleaved.
- **Nothing about absolute noise figure.** Every number here is relative dB. Absolute NF needs a
  calibrated noise source and the Y-factor method.
- **Nothing about antenna performance.** Antennas are graded against a known-present signal on
  the best-characterised chain available - never on a terminated baseline, and never on an
  uncharacterised chain. That error cost a day on 2026-10-16.

## Phase 0 - USB integrity gate (first thing, every session)

A dropped register write means a register was never set, so the gain may not be the gain
recorded in the sidecar. Nothing below is trustworthy until both dongles pass.

**First, confirm which index is which.** USB enumeration order is not guaranteed and can
change when you replug - today v5 = 0 and XTR = 1, but after rebuilding the bench that may
flip. Getting it wrong records one dongle's data under the other's name.

```
rtl-sdr/rtl_test.exe                     # prints the device list, then Ctrl-C
rtl-sdr/rtl_test.exe -s 2800000 -d 0     # v5   (whichever index it reported)
rtl-sdr/rtl_test.exe -s 2800000 -d 1     # XTR
```

Also confirm Windows sleep and USB selective suspend are off - Phases 1 and 2 are about six
hours of runs.

**Pass: 0 samples lost per million.** That is the figure recorded for the USB-isolator test,
so it is a like-for-like comparison. Any `rtlsdr_write_reg failed` line is an automatic fail -
fix the connector mechanically and re-run before going on.

---

## Phase 1 - v5 receiver alone

50 ohm load **directly on the v5 SMA**. Nothing else attached. No cage.

No DC block needed: the v5 is not a SMArTee and has no bias-tee. Confirm with a multimeter
first - the v5 SMA centre pin should read **0 V** to shell. If it reads ~4.5-5 V it does have
a bias-tee and needs a DC block like the XTR.

Run 1a, gap band, exact fingerprint of the retracted survey:

```
py tools/run_survey.py custom 1h --freq 1095M:1240M:25k --gain 29.7 --integration 30 --receiver v5 --device 0 --tag gap-probe-term --skip-warmup 16 --chain "50 ohm load at v5 SMA > v5 (open bench, no cage) > USB > PC"
```

Verified by `--dry-run`: 70 hops, **89% listening**, 45.8 s on each frequency, 120 sweeps.
Identical fingerprint to the retracted survey, so the comparison is direct.

Run 1b, first internal spur map of the v5:

```
py tools/run_survey.py custom 1h --freq 700M:1700M:50k --gain 29.7 --integration 120 --receiver v5 --device 0 --tag baseline-term --skip-warmup 16 --chain "50 ohm load at v5 SMA > v5 (open bench, no cage) > USB > PC"
```

**`-i 120` and a 50 kHz bin request are both load-bearing, not arbitrary.** At `-i 60` this
span retunes 22.4 s out of every 60 (63% listening); at `-i 120` that is 81%. And 100 kHz
requested yields a 32-point FFT with 87 kHz bins, which smears narrow spur lines - 50 kHz
gives FFT 64 and **43.68 kHz bins, the same grid as the proven LS terminated runs**. Result:
477 hops, 30 sweeps, 6 MB.

**Run 1a answers the open question from the retracted survey.** If the 604.2 kHz comb at
1190-1210 MHz and the broadband pedestal are still there with no antenna, they are internal
or conducted. If they vanish, they arrive through the antenna and are real external signals.
Same fingerprint as the gap survey, so the comparison is direct.

**Run 1b** covers 700-1700 MHz, which holds 35 harmonics of 28.8 MHz (x25 to x59) - enough
for the family statistic to have a usable error bar.

---

## Phase 2 - Does the cage do anything? (v5, terminated, 1 h per block)

**This comparison has never been made here.** The 2026-10-14 cage result compared a crude
copper-taped cardboard box against a bonded cage, changed the notch position in the same
rebuild, and ran its two halves on different days three hours apart on the clock - before the
1.4 dB diurnal swing was known. It concluded nothing about shielded versus unshielded.

Rules:

- **The USB chain never changes.** The bulkhead is threaded and copper-bonded into the
  enclosure and must not be disturbed. So the internal bulkhead-to-SDR extension stays plugged
  in for every block; for the A blocks it simply reaches out of the open box and the dongle
  rests outside. Same bulkhead, same extension, same cable, every time - nothing is added or
  removed between configurations.
- **Position is the residual confound, and it is unavoidable.** Testing "in a box" against
  "out of a box" necessarily moves the dongle, and near-field coupling is geometry. This cannot
  be designed out; it can only be bounded and recorded:
  - Put the dongle **beside** the cage, not on top of it. Resting it on a copper-clad lid puts
    it flat against a conductive plane, which is itself a partial shield and muddies what
    "unshielded" means.
  - **Mark the spot** - tape an outline on the bench - and keep the same face up and the same
    end pointing the same way in A1 and A2.
  - Keep the extension's slack and routing as similar as you can between configurations.
  - The cage body stays in the same place throughout, lid off for A and on for B, so its mere
    presence is constant and does not enter the comparison.
- **A1 vs A2 is now doing double duty.** Because A2 follows a full remove-insert-remove cycle,
  it measures position repeatability as well as time drift. If A1 and A2 agree within ~0.5 dB,
  position control is good enough for the shielding result to mean something. If they do not,
  the position effect is larger than the effect being hunted and the cage test cannot resolve
  it - that is itself a finding, and an honest one.
- **Finalise the thermal mounting before starting.** A cage changes temperature, so otherwise
  "caged" and "warmer" are the same variable.
- **Run A/B/A, not A then B.** Diurnal swing reaches 1.4 dB against a 0.15 dB per-sweep
  stability, so consecutive blocks confound shielding with time of day.

Same settings as run 1b (`700M:1700M:50k -g 29.7 -i 120`, 1 h) for every block - about
four hours in total. **Leave the dongle powered between blocks** so only the first needs a
real warm-up; `--skip-warmup 16` then covers the rest.

| Block | Tag | Configuration |
|---|---|---|
| A1 | `v5-cage-off-1` | no cage; bulkhead loose on bench, extension inline |
| B1 | `v5-cage-on-untaped` | cage closed, lid screws only, seam **not** taped |
| A2 | `v5-cage-off-2` | no cage again, same loose bulkhead and extension |
| B2 | `v5-cage-on-taped` | cage closed, seam taped over |

`--chain` strings, so the sidecars describe themselves:

- A blocks: `50 ohm load at v5 SMA > v5 (outside cage, beside it, marked spot) > internal extension > cage USB bulkhead > 1ft USB3 M:M > 16ft USB3 > isolator > PC`
- B blocks: `50 ohm load at v5 SMA > v5 (inside closed cage) > internal extension > cage USB bulkhead > 1ft USB3 M:M > 16ft USB3 > isolator > PC`

The two strings differ only in where the dongle sits, which is the point.

Reading it:

- **A1 vs A2** is the control. If they differ by more than ~0.5 dB on the family mean, time
  drift is contaminating the run - use the A1/A2 mean as the reference, or re-run at a
  quieter hour.
- **mean(A1,A2) vs B1** is the shielding effect, which has never been measured here.
- **B1 vs B2** answers the seam-tape question: is taping the seam each time worth doing, or
  does the tongue-and-groove joint already do the work?

A terminated run cannot see noise **escaping** toward the antenna. That needs Phase 3 step 7
with the antenna connected, A/B/A interleaved, and it is a separate question.

---

## Phase 3 - Cascade: move the load outward one element at a time

Each step adds exactly one physical element. Same settings throughout
(`700M:1700M:50k -g 29.7 -i 120`, 1 h). Record `--chain` honestly on every run: two runs
have already been mislabelled because station.json held the previous run's wording.

| Step | Load goes here | Isolates | Tag |
|---|---|---|---|
| 1 | v5 SMA | receiver alone | `casc-1-sdr` |
| 2 | v5 SMA, cage on | shielding | `casc-2-cage` |
| 3 | far end of 6 in jumper | jumper + connectors | `casc-3-jumper` |
| 4 | far end of 8 ft coax | cable run | `casc-4-coax` |
| 5 | LaNA input | amplifier noise contribution | `casc-5-lna` |
| 6 | notch input | filter | `casc-6-notch` |
| 7 | (antenna connected) | the real world | `casc-7-antenna` |

**What the cascade cannot tell you.** It measures what each stage *adds* to the noise floor.
It says nothing about what each stage lets *through*. Those are different failure modes - the
v5 on 2026-10-16 had a perfectly ordinary noise floor and still decoded zero ADS-B frames.

Sensitivity needs a known-present signal. Pair every configuration with:

```
py tools/adsb_check.py --seconds 60 --gain 40.2 --device 0 --label "<same tag>"
```

The terminated run gives the floor; ADS-B gives the sensitivity. Together they are a complete
characterisation. Compare on the **DF17 frame count**, not the ICAO count - with `-e 5`
allowed bit errors, errors landing in the address field mint phantom aircraft, so a noisier
chain reports *more* "aircraft".

---

## Phase 4 - XTR, once the DC block arrives

Identical structure. One difference, and it matters:

> **The XTR bias-tee is always on and DC-coupled. An inner-conductor SMA DC block goes between
> its SMA and the 50 ohm load, every time.** A bare terminator shorts the bias-tee.

Verify before trusting it: XTR SMA centre pin to shell should read **~4.5-5 V**; measured
through the DC block it should read **0 V**.

Run the XTR cascade at the **LS fingerprint** (`1241M:2147.484M:50k -g 29 -i 120`) so that
step 5 is directly comparable to the three existing terminated runs. The difference between
"load at the SDR" and "load at the LaNA input" is exactly what the LaNA, coax and notch
contribute - a number the station has never had.

---

## Results

Fill in as runs complete. One line per block, family mean from `spur_stats.py`.

**Programme closed 2026-10-18.** The reference run is `casc4-noferrite`. Fourteen 700-1700 MHz
v5 runs were made; the ones carrying a conclusion are listed here. Family means are 14.4 MHz,
n=70, from `compare_runs.py`.

| Date | Phase | Tag | Family mean | Whole-band median | What it established |
|---|---|---|---|---|---|
| 10-16 21:38 | 1b | `baseline-term` | +4.92 (SE 0.63) | -35.890 | First v5 reference. Pedestal and comb both gone with the antenna off (P1, P2). |
| 10-17 00:12 | 2 / B1 | `cage-on-untaped` | +3.78 (SE 0.50) | -35.360 | Cage on. Read at the time as a real cage effect; **later shown to be the faulty cable**, which was only in the box when caged. |
| 10-17 01:21 | 2 / A2 | `cage-off-2` | +4.78 (SE 0.41) | -35.840 | **Control passes**: vs A1 median +0.050 dB, family -0.14 dB (SE 0.41). Drift and position are controlled. |
| 10-17 02:36 | — | `overnight-noprinter` | — | -35.840 | Printer exonerated (identical to A2). Thermal flat: median sd **0.039 dB** over 6 h. Established that **handling costs 20x more than time**. |
| 10-17 12:16–22:46 | A2 | `cyc1`…`cyc5` | — | — | Three in/out cycles plus stub and lid-off tests. Every mechanism hypothesis refuted. |
| 10-17 23:54 | — | `cyc6-usb12` | — | -35.550 | 12 in cable: hump gone. Misread as length-dependent resonance. |
| 10-18 01:19 | — | `cyc8-usb6-white` | +5.53 | -35.760 | **Different 6 in cable, same length: hump gone.** The faulty cable identified. |
| 10-18 03:35 | B / casc2 | `casc2-jumper` | +4.34 | -35.750 | SMA bulkhead + jumper add **+0.010 dB** broadband — nothing. |
| 10-18 10:47 | B / casc3 | `casc3-replicate` | +4.44 | -35.670 | Jumper removed: 1670-1690 did **not** fall. Jumper innocent; stub refutation stands. |
| 10-18 11:47 | B / casc4 | **`casc4-noferrite`** | +5.43 | **-35.760** | **Reference run.** Ferrites off: 1670-1690 drops **1.3 dB**. Replicates `cyc8` to **0.21 dB** in every block. |

### What the programme established

1. **A clean, replicated v5 terminated baseline** — flat within 0.3 dB across 1620-1700, two
   independent runs agreeing to 0.21 dB. This did not exist before.
2. **The 1675 MHz hump had two causes, both found**: one faulty 6 in USB cable (~5 dB) and
   ferrite chokes inside the enclosure (~1.3 dB). Neither was a resonance of the cage.
3. **Handling, not time, sets repeatability** — 3.4 dB per line versus 0.16 dB over six hours.
   This reshaped every error bar in the programme and is its most portable result.
4. **The cage is RF-neutral and mechanically valuable.** +0.16 dB in 1600-1700, +0.09 elsewhere.
   Keep it for fixed geometry, which attacks finding 3 directly.
5. **Five shielding interventions, none beneficial.** Bonded cage (0), USB shield bond
   (-0.14 ± 0.24), galvanic isolation (+0.23 ± 0.26), interior copper (worse), internal ferrites
   (+1.3 dB, worse). The default for the XTR build is **fewer parts, not more**.
6. **720-1000 MHz is unstable and must be excluded from family statistics** — 720 MHz has ranged
   +8.3 to +32.7 dB above the band median across fourteen runs. Including it produced a spurious
   3.2 sigma result on 10-18. Cause unidentified; this is the largest open question left.

### Verdicts on the pre-registered predictions

- **P1 CONFIRMED.** The 1185-1232 MHz pedestal is gone. Terminated floor flat at -35.94 to
  -36.17 dB across 1140-1250 MHz; the antenna run read -29.21 at 1200-1210. Rise within the
  terminated run **-0.18 dB**, against about +8 dB with the antenna connected.
- **P2 RESOLVED AS FAR AS IT CAN BE.** The 604.2 kHz comb is gone: r = +0.057 against the
  original +0.885. Per the reading registered in advance, **a comb that vanishes is ambiguous** -
  external, or an artefact of the FM overload. Only the notch test separates them.
- **P3 UNRESOLVED. The earlier "REFUTED" is withdrawn.** It rested on a -1.07 dB cage effect that
  is now known to be the faulty USB cable, which was only inside the box when the cage was on.
  With a sound cable the best estimate is **+1.20 dB (SE 0.55, n=70)** - 2.2 sigma, one cycle,
  handling-dominated. That technically exceeds the registered overturn threshold, but not by
  enough to believe from a single cycle. **Neither confirmed nor refuted.**
- **P4 NOT RUN.** B2, the seam-taped block, was never executed. The question stands.
- **P5 CONFIRMED for the steps actually run.** No cascade step added more than ~1 dB to the
  broadband floor: the SMA bulkhead and 6 in jumper together moved the whole-band median by
  **+0.010 dB**. One caveat the prediction did not anticipate: a **component** that is not a
  cascade step - a ferrite choke - added 1.3 dB in a 20 MHz band while adding nothing broadband.
  The prediction holds for the floor; it does not cover band-localised effects.
- **P6 REFUTED, 2026-10-18.** Predicted ~zero ADS-B frames on the v5 with no notch. Measured
  **163 DF17 frames in 60 s, 323.5 frames/min, 84 distinct ICAO - PASS.** By the overturn
  condition registered on 2026-10-16 ("frames appear -> the overload diagnosis is wrong"), the
  FM-overload explanation for the 2026-10-16 zero is withdrawn. The overload itself is still
  measured and real (`fm-check-p6`: FM median 20.2 dB above the terminated floor, 90.33 MHz
  unmeasurable) - it simply does not prevent decoding. Cause of the original zero: unknown,
  with the faulty black USB extension the leading untested suspect.
  **This is the single clearest return on pre-registration in the programme.** Had the notch
  gone on first, it would have been credited with a fix it did not make.

### Note on how the wrong answers were reached

Five mechanism hypotheses were proposed and all five were wrong: air-cavity modes, the wall
laminate patch, the dangling SMA stub, the dongle assembly length, and cable length. Every one
was proposed because a **dimension matched the observed frequency**. The two hypotheses that held
tested **component identity** instead - swap the cable, remove the ferrites. In an enclosure full
of structures, something always matches your frequency. A matching number carries no information;
only a prediction that can fail does.

Second lesson, from 10-18: **an effect cannot be seen through noise larger than itself.** The
stub test correctly measured "no change" while the faulty cable was adding 5 dB to the same band.
Its real contribution was never more than a fraction of a dB. Before believing a null, check what
else is in the band.

### Registered 2026-10-16, before B1 runs

**Two statistics, not one.** The family mean is `harmonic - whole-band median`, so a common-mode
gain shift (thermal, mains) subtracts out exactly - it is blind to drift by construction. But the
cage does not change on-PCB clock harmonics; it changes the FLOOR.

| Statistic | Measures | Weakness |
|---|---|---|
| **Whole-band median** | the floor directly - what a cage actually moves | absorbs gain drift |
| **Family mean (14.4 MHz)** | harmonics vs floor; drift-immune | a floor drop shows up as a mean *rise* |

Report both for every cage block. If the cage works, expect the **median to fall** and the family
mean to **rise** by a similar amount. A1 vs A2 says which statistic to trust on the night.

**Corollary: a quiet room cannot detect a working cage.** With no antenna, external RF reaches the
dongle only through the case or down the USB, and blocking the case path is the cage's whole job.
With nothing to block, a perfect cage measures zero. Room activity here is not purely a nuisance -
it is partly the test signal.

**Printer correction.** A1's recorded conditions say "printer on and printing". It was powered but
**idle**; it had been printing earlier in the evening. The sidecar is left exactly as typed - this
note is the correction, so the audit trail stays intact. The printer sits **inches from the
dongle** and is normally kept unplugged, which makes it a strong, local, radiated source. For the
cage blocks it must be in the **same state throughout**: powered, idle, not moved. Testing the
printer itself is a separate single-variable experiment, not something to change mid-series.
