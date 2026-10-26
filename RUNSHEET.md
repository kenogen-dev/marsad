# Run sheet

The order to do things in. Depth and reasoning live in `BASELINE_PROTOCOL.md`; this is the
checklist. Every run is 1 hour unless it says otherwise.

**Fingerprint for the whole v5 programme:** `700.000-1700.000 MHz | 43.6758 kHz | g29.7 | i120 | c0.25 | F9 | v5`
Anything that does not match it is not comparable.

---

## Done — the v5 terminated programme is CLOSED (2026-10-18)

**Reference run: `casc4-noferrite`.** Whole-band median **-35.76 dB**, floor flat within 0.3 dB
across 1620-1700, replicated against `cyc8-usb6-white` to **0.21 dB** in every 10 MHz block.
Compare all future v5 work against it.

| # | Run | Result |
|---|---|---|
| 0 | USB gate | clean, 0 lost/million, v5 = device 0 |
| 1 | **A1** `baseline-term` | median **-35.890**, family **+4.92** (n=35, SE 0.63) |
| 2 | **B1** `cage-on-untaped` | median -35.360. Read as a cage effect; later shown to be the faulty cable |
| 3 | **A2** `cage-off-2` | median -35.840. **Control passes** (+0.050 vs A1) |
| 4 | `overnight-noprinter` | printer exonerated; handling costs 20x more than time |
| 5-10 | `cyc1`…`cyc6` | three in/out cycles, stub removal, lid off, 12 in cable |
| 11 | `cyc8-usb6-white` | **faulty 6 in USB cable identified** (~5 dB at 1670-1680) |
| 12 | `casc2-jumper` | SMA bulkhead + jumper add **+0.010 dB** — nothing |
| 13 | `casc3-replicate` | jumper removed, hump stayed — jumper innocent |
| 14 | **`casc4-noferrite`** | **ferrites off: 1670-1690 drops 1.3 dB.** Baseline clean |

**Three build rules came out of it:**

1. **No ferrite chokes on cables inside the enclosure.** Measured +1.3 dB at 1670-1690.
   External chokes are unmeasured and stay.
2. **No interior copper lining.** It only increased coupling to the faulty cable.
3. **Fewer parts by default.** Five shielding interventions measured: three exactly zero, two
   actively worse, none beneficial. Each addition needs a measurement earning its place.

**Standing analysis rule:** exclude 700-800 MHz from every family statistic on this receiver.
720 MHz has ranged +8.3 to +32.7 dB above the band median across fourteen runs and produced one
spurious 3.2 sigma result. Use `compare_runs.py --exclude 700 800`, or read the block table.

---

## ~~Block A~~ - DONE, but superseded

> Ran 2026-10-17. Its headline cage effect was the faulty USB cable, not the cage.
> **B2 (seam taped) was never run** - that question is still open. Kept below for
> the audit trail.

### Block A as written

Load stays on the SMA throughout. Nothing changes but where the dongle sits.

### 2. B1 - cage on, seam NOT taped  &larr; next

Put the v5 in the cage. Lid screws only. Everything else untouched.

**Orientation: logo-up, matching A1** - rotated 180 about the SMA-USB axis from the normal
cage install. No v5 cage run has ever been done, so there is no legacy orientation to match;
the only requirement is that all four blocks agree. Check the foam still presses firmly on
the thermal pad in the flipped position before closing up.

```
py tools/run_survey.py custom 1h --freq 700M:1700M:50k --gain 29.7 --receiver v5 --device 0 --tag cage-on-untaped --chain "50 ohm load > v5 (in cage) > 6 in USB extension > USB bulkhead > 1 ft USB3 M:M > 16 ft USB3 > USB isolator > PC" --conditions "night, clear, 29C inside, 37% RH, v5 logo-up (rotated 180 about the SMA-USB axis from the normal cage install), printer powered but idle inches from dongle, monitors and PC on, TV on, box-fan on, BT headphones connected to PC playing audio" --skip-warmup 16 --non-interactive
```

### 3. A2 - cage off again

Dongle back out to the **taped spot, logo-up**, same as A1. Lid off, cage body stays put, the
internal extension stays plugged into the bulkhead and reaches out of the open box. Printer
unchanged. Run it straight after B1 - the bracket only works while drift is still roughly linear.

```
py tools/run_survey.py custom 1h --freq 700M:1700M:50k --gain 29.7 --receiver v5 --device 0 --tag cage-off-2 --chain "50 ohm load > v5 (open bench) > 6 in USB extension > USB bulkhead > 1 ft USB3 M:M > 16 ft USB3 > USB isolator > PC" --conditions "night, clear, 29C inside, 37% RH, v5 logo-up on the marked spot beside the cage, printer powered but idle inches from dongle, monitors and PC on, TV on, box-fan on" --skip-warmup 16 --non-interactive
```

### 4. B2 - cage on, seam taped

Back in the cage, tape over the seam. Chain `(in cage, seam taped)`, tag `cage-on-taped`.

**Reading it** (`py tools/spur_stats.py <A>/bins.csv <B>/bins.csv --family 14.4`):

- **A1 vs A2** within ~0.5 dB &rarr; drift and position are controlled, the rest is trustworthy.
  Further apart &rarr; the room or the placement is moving more than the effect we are hunting.
- **mean(A1,A2) vs B1** = the shielding effect. Never measured here before.
- **B1 vs B2** = does the seam leak. B2 is the upper bound on what any gasket could achieve.

---

---

## ~~Block A2~~ - DONE 2026-10-17

> Three cycles run. They did not settle the cage question, because the faulty cable
> was in the box for every `on` block. They did establish the handling error term,
> which turned out to matter more.

### Block A2 as written

**Why again:** the first A/B/A gave a significant cage effect (-1.07 dB family, +0.5 dB floor) but
rests on **one** in/out cycle, and handling between runs is the dominant error term (3.4 dB per-line
scatter vs 0.16 dB when nothing is touched). Three cycles triples that sample.

**Thermal is refuted** - B1's floor was already 0.8 dB high at sweep 1, before anything could warm
up, and every run settles *downward*. Do not modify the cage. Freeze the hardware for the series.

30-minute blocks, `--skip-warmup 10` (the settle is done by ~sweep 5). Same fingerprint as every
other v5 run, so these stay comparable to A1/B1/A2. Alternate strictly:

| # | Tag | Dongle |
|---|---|---|
| 1 | `cyc1-off` | bench, taped spot, logo-up |
| 2 | `cyc1-on` | in cage, seam untaped |
| 3 | `cyc2-off` | bench |
| 4 | `cyc2-on` | in cage |
| 5 | `cyc3-off` | bench |
| 6 | `cyc3-on` | in cage |

Only the tag and the `(open bench)` / `(in cage)` wording change between them:

```
py tools/run_survey.py custom 30m --freq 700M:1700M:50k --gain 29.7 --receiver v5 --device 0 --tag cyc1-off --chain "50 ohm load > v5 (open bench) > 6 in USB extension > USB bulkhead > 1 ft USB3 M:M > 16 ft USB3 > USB isolator > PC" --conditions "<daytime room state>" --skip-warmup 10 --non-interactive
```

```
py tools/run_survey.py custom 30m --freq 700M:1700M:50k --gain 29.7 --receiver v5 --device 0 --tag cyc1-on --chain "50 ohm load > v5 (in cage) > 6 in USB extension > USB bulkhead > 1 ft USB3 M:M > 16 ft USB3 > USB isolator > PC" --conditions "<daytime room state>" --skip-warmup 10 --non-interactive
```

Then repeat both with `cyc2-` and `cyc3-`.

**Reading it:** the three `off` blocks estimate the handling scatter directly; the three `on`
blocks give three independent cage measurements. The spread *between* the three cage numbers is
the real error bar - the across-line SE within any one comparison cannot see a shift that moves
all lines together.

## ~~Block B~~ - DONE for the v5, 2026-10-18

> Answered: **nothing measurable.** The SMA bulkhead and 6 in jumper together move
> the whole-band median by +0.010 dB. The open question below is resolved - the v5
> is standalone, so steps 7-9 do not exist for it until the antenna goes on.

### Block B as written

The load moves one element further from the dongle each time. Settings never change.

| # | Load sits at | Tag |
|---|---|---|
| 5 | v5 SMA, best enclosure from Block A | `casc-1-sdr` |
| 6 | far end of the 6 in extension | `casc-2-ext` |
| 7 | far end of the 8 ft coax | `casc-3-coax` |
| 8 | notch input *(once the second filter arrives)* | `casc-4-notch` |
| 9 | **antenna connected** | `casc-5-antenna` |

> **Open question before Block B starts:** is the v5 on the bench actually plumbed into the
> window run (8 ft coax, SMA bulkhead), or is it standalone with nothing on its SMA but the
> load? If standalone, steps 6-7 do not exist for the v5 and the cascade is shorter.

> **No LaNA step on the v5.** It is powered by the XTR's bias-tee and the v5 has none, so the
> v5 cascade has no amplifier stage. The XTR's cascade does.

---

## Block C - NEXT, blocked on the second FM notch

> Everything runnable without new parts is done. This block needs the antenna back
> on the v5, and step 11 needs the notch. **Run step 10 (P6) BEFORE fitting the
> notch** or the two variables move together.

### Block C as written

Only reachable once step 9 above is done, because everything before it is terminated.

### 10. P6 - ADS-B with NO notch

Both dongles connected. Run the control first so you know there is traffic.

```
py tools/adsb_check.py --seconds 60 --gain 29   --device 1 --label "XTR control"
py tools/adsb_check.py --seconds 60 --gain 40.2 --device 0 --label "v5 no-notch P6"
```

**Prediction:** v5 still reads ~zero. If frames appear, USB was the original cause and the
overload diagnosis was wrong. **Do this before the notch goes on** - the filter can wait in a
drawer as long as it takes.

### 11. Fit the notch, repeat

Same two commands. Frames appearing now = overload confirmed, and the chain is fixed.

### 12. The clean comb test

With antenna + notch (no overload, real signals present), re-run the survey and check
1190-1210 MHz. **If the 604.2 kHz comb is there, it is genuinely external.** This is the only
configuration that can settle it - the terminated run could not, because removing the antenna
removed the overload too.

---

## Block D - COMPLETE 2026-10-19

### Block D as written

Needs the DC block (its bias-tee is always on; a bare load shorts it). Same structure, with
an LaNA step added, and run at the **LS fingerprint** so it ties to the three existing
terminated runs.

**Fingerprint for the XTR cascade:** `1241.000-2147.484 MHz | 43.7154 kHz | g29 | i120 | c0.25 | F9 | xtr`
(the LS range at `-i 120`). Reference run: **`xtr-alu-baseline`**, whole-band median **-38.160 dB**.

| # | Load sits at | Tag | Result |
|---|---|---|---|
| 1 | outside of the SMA bulkhead, XTR direct on USB bulkhead | `xtr-alu-baseline` | median **-38.160**, floor flat to 0.20 dB over 900 MHz, 7 detections all clock harmonics. **Reference.** |
| 2 | far end of the 3 ft RG316 coax, at the window | `xtr-casc2-coax` | **+0.020 dB**, largest block -0.08, family +0.09 (SE 0.10). Nothing. Drift PASS |
| 3 | LaNA input, LaNA on the far end of the coax *(DC block stays with the load)* | `xtr-casc3-lana` | **+0.640 dB**, predicted 0.6-0.8. But tilted: +1.0 at 1241-1341 to **zero above 1.95 GHz**. Harmonics unmoved. Drift PASS, settles ~25 min |
| 4 | notch input, notch ahead of the LaNA | `xtr-casc4-notch` | **+0.000 dB**, predicted zero. Largest block -0.05, family -0.09 (SE 0.12). Holds at 18 sweeps discarded |
| 5 | **antenna connected**, at g29 then g42 back-to-back | `xtr-casc5-antenna-g29`, `xtr-casc5-antenna-g42` | g29: 1799.98 at **-13.1 dB**, 3.5 dB better than predicted; floor +4.5 dB at 1241-1341, 0.00 above 2.04 GHz. g42: carrier **+3.8 dB** for 13 nominal, SNR +1.0. **Stay at g29.** No clipping either run |

Printer **unplugged** from step 2 onward; it was powered-idle for step 1 (see that sidecar's
`conditions_correction`). Known single difference, measured twice now to contribute nothing.

Step 2's command, for the shape of the next ones (change only the tag, chain and conditions):

```
py tools/run_survey.py custom 1h --freq 1241M:2148M:50k --gain 29 --receiver xtr --device 0 --tag xtr-casc2-coax --chain "50 ohm load > SMA DC block > SMA M:M adapter > 3 ft RG316 coax (far end, at the window) > SMA bulkhead > 6 in SMA jumper (S-bend, no ferrite) > XTR (in aluminium enclosure, plugged direct into USB bulkhead, no internal USB cable) > USB bulkhead > 1 ft USB3 M:M > 16 ft USB3 > USB isolator > PC" --conditions "cascade step 2, printer unplugged, ..." --skip-warmup 16 --non-interactive
```

Then `py tools/compare_runs.py xtr-alu-baseline <tag> --family 14.4 --handling handled` and
`py tools/drift.py <tag>`.

**Step 3 result:** magnitude as predicted, shape not - the LaNA's rise falls to nothing above
1.95 GHz. 0.64 dB is far too small for a working 20 dB amplifier if the receiver were
thermal-noise-limited, so either the XTR's floor at `-g 29` is a digital floor or the LaNA is
under-delivering. See CLAUDE.md "XTR cascade step 3".

**Gain ladder DONE 2026-10-19:** g14 -38.13, g29 -37.46, g42 -35.88. The floor is a digital
floor at ~-38.3 dB; 28 dB of tuner gain moved it 2.25 dB overall and 0.12 dB above 2 GHz. The
g29 point reproduced step 3 to 0.04 dB. See CLAUDE.md "Gain ladder 2026-10-19". The cascade
stays at g29 for its fingerprint; **run the antenna step at both g29 and g42.**

**Step 4 result:** +0.000 dB, exactly as pre-registered. The terminated cascade is complete.

### Step 5 result

Both predictions were wrong in instructive directions: the carrier came in **3.5 dB stronger**
than the old chain ever gave (-13.1 against -16.6 predicted), and the 13 dB nominal gain step
delivered **~4 dB** on the carrier and on the received-noise floor alike, for 1.0 dB of SNR.
Below ~1.55 GHz the station is external-noise-limited; above 2.04 GHz nothing comes through at
either gain. **L/S surveys stay at g29.** Full numbers in CLAUDE.md "XTR cascade step 5".

Standing config for antenna surveys is now: antenna > notch > LaNA > 3 ft coax > bulkhead >
jumper > XTR in the aluminium enclosure, direct on the USB bulkhead. station.json carries it.

**Open items from the step:** 1633.47 MHz - the deep tile (2026-10-19 22:29) shows one
21.5 kHz bin, frequency fixed in 120 of 120 sweeps, 100% on, amplitude wandering 6 dB. Either a
device's leaked tone or an IMD3 product of 1799.98 and the PCS downlink made in the LaNA.
**Pre-registered test:** 10 dB pad at the LaNA input, same tile, 10 min each way: a real
carrier drops 10 dB, LaNA IMD drops 30 dB. And 1405.93 MHz (weak, in the passive band).

**2 GHz control done (2026-10-19 23:52):** the XTR hears the 2110-2147 MHz cellular downlink
at 2.1 GHz as a 5-6 dB plateau, 12 dB in max-hold, no clipping at g42. Not deaf, but weak, and
the 60 mm whips are 0.84 wavelengths there. Whether the tuner or the antenna is the limit is
for the noise generator to settle before any 2 GHz antenna is built. See CLAUDE.md.

---

## Block E - antenna outside the window, A/B/A (2026-10-20)

**DONE 2026-10-20.** Outside is worth **3-5 dB** in the bands that carry signals (5-6 with the
extension's loss added back), nothing on the digital floor. Low-E glass **refuted** (would have
been 9+ dB). The control ran the right way: A2 came in 0.7-1.7 dB below A1 (morning
quietening), and B2 still beat both. The 1799.98 MHz reference carrier faded from -13 to -35 dB
across the morning on its own and is **retired as a yardstick** until it returns; use the
1930-1990 downlink block median and the 1241-1341 block. Decision: build a permanent outside
mount with the notch and LaNA outside in a weatherproof box, no cable ahead of the amplifier;
until then the inside position stands. Full table in CLAUDE.md.

The antenna has always sat on the inside face of the glass. Modern retrofit windows often carry
a low-E coating, a metallic oxide film that attenuates radio by 10-30 dB at GHz frequencies. If
this window has one, moving the antenna outside is the largest gain available to the station. The
nylon or fiberglass screen is an insulator and invisible to radio; check it with a multimeter on
continuity across the mesh (open = insulator) before blaming it for anything.

Three 30 min blocks at the LS fingerprint, g29, `--skip-warmup 10`, whips at 60 mm throughout,
notch and LaNA staying inside on the sill. Only the antenna's position changes between blocks.
A/B/A because the busy bands are diurnal and the morning is their peak; the bracket separates
"moved outside" from "the hour changed".

| Block | Antenna | Tag |
|---|---|---|
| A1 | inside, on the glass as always | `window-A1-inside` |
| B | outside the sash, short coax through the gap to the notch | `window-B-outside` |
| A2 | back inside, same spot | `window-A2-inside` |

**Predictions, on record:**

- **1799.98 MHz carrier, median over sweeps.** Low-E glass: B rises **10 dB or more** over A1.
  Plain glass: **1-2 dB**, from clearing the frame and the wall.
- **Busy-band floors** (1241-1541, 1941-2041): rise with the carrier, because those bands are
  external-noise-limited. **Digital-floor bands** (1841-1941, 2041-2109): do not move.
- **A2 returns to within 0.5 dB of A1** on the carrier (the handling floor; the antenna is
  remounted). If A1 and A2 disagree by more, the bracket failed and B is uninterpretable.

**Amendment before B ran:** block B needed a 3 ft coax extension between the antenna and the
notch so the notch and LaNA could stay secured at the window. That is loss AHEAD of the
amplifier, about 1 dB at 1.8 GHz, which comes straight off the carrier. Amended: low-E glass
still shows as **9 dB or more**; plain glass now lands **within 1 dB of A1 either way**, which
would be ambiguous and need a repeat with a 1 ft pigtail.

Read with `compare_runs.py window-A1-inside window-B-outside --family 14.4 --handling handled`,
the same for A1 vs A2, and `archive.py trend 1799.98`.

---

## Block F - noise generator characterisation (2026-10-20/21)

**Done on the wide range.** LaNA gain 25 dB at VHF to 17.7 dB flat across 1.24-1.66 GHz; 3 ft
coax under 1 dB; notch passband loss 1.1-1.4 dB, stop band 74-126 MHz at -3 dB, depth 15 dB,
confirmed real by the leakage check. Full numbers in CLAUDE.md "Noise generator
characterisation". Facts for every future run with the source: **36 dB of pad direct into the
receiver, 39 dB ahead of the LaNA, tuner at g-1**; anything less clips.

**Still to run:**

1. ~~Leakage check~~ **done 07:45:** FM band at the floor (-34.9) with no cable path, so the
   notch's 15 dB is real. Use 15 dB as the Flamingo+'s rejection.
2. ~~LS-range D and E~~ **done 08:50 at g14** (the g-1 pair fell on the floor): LaNA gain
   16-18 dB all the way to 2.147 GHz, lower bounds above 1.84 GHz. **The amplifier is not the
   2 GHz limit.** The receiver itself reads 11 dB less at 2.1 GHz than at 1.3 with the same
   source on its input, part generator roll-off, part E4000. The antenna is the only movable
   piece: UWB antenna on the 2049-2147 tile next, then the bridge.

**Block F is complete. The standing chain is restored and checked** (`chain-restored-check`,
2026-10-21 09:27, 30 min LS at g29, against `window-A2-inside`): the PCS downlink block
1941-2041 agrees to **+0.04 dB** and the digital-floor block 2041-2141 to +0.15, so the
gain path is exactly as it was. 1241-1341 read +3.7 dB and 1341-1541 +1.6, lifted uniformly
across every 10 MHz slice with no new line and tapering to zero by 1.9 GHz: the received-noise
pattern of step 5, i.e. the morning environment, not the chain. The next LS run will show
whether it was a one-off. station.json carries the standing chain again.

---

## Block G - return-loss bridge (started 2026-10-22, BLOCKED on the replacement)

The bridge as shipped had its detector leads shorted (OUT centre to shell 0.3 ohm, should be
~100). Every run on 2026-10-22 measured leakage: DUT open read ~25 dB too low, and both ports
open read 8-10 dB *louder* than one loaded. **Returned 2026-10-22, not rewired; a replacement is
ordered.** *Corrected 2026-10-23:* the replacement is a **different, three-port board**
("SWR Bridge 10-3000 MHz": RFIN, RFOUT, DET, 50 ohm reference built in), due 2026-10-24. The
four-port checks written here first (OUT/REF/DUT, "both ports open") do not apply to it. When it
arrives, before it touches the radio:

1. **Meter check, nothing connected:** RFIN centre to RFOUT centre ~50 ohm; RFIN centre to shell
   ~100; DET centre to RFOUT centre ~0; DET centre to DET shell 50-100, **never 0** (0 is the
   first board's short). A new unit can arrive with the same fault, so this is not optional.
2. **Rig:** generator (on 10 min first) on RFIN; RFOUT open, loaded or on the antenna; DET > DC
   block > 3 ft coax > cage. XTR g14, no pad, no notch or LaNA.
3. **Radio check:** RFOUT open reads near **-2 dB at 1.3 GHz** on the full LS span at g14 (same
   fingerprint as `nsrc-D14-LS`); RFOUT on the 50 ohm load reads **25 dB or more below** open
   (that gap is the bridge's directivity). Fail either and stop.
**Replacement arrived; first checks 2026-10-25.** It is a different layout from the one the meter
values above assumed: the board is cut into separate copper islands (shells are not common:
RFIN-RFOUT shell 30.7 ohm, DET shell open to both), and DET centre-shell, RFOUT centre-shell and
DET-RFOUT centre all read ~0.1 ohm, with R7 (49R9) reading 0.8-1.2 ohm in circuit against
25.1 for R5 and R6. Workmanship matches the factory photos with no bridges; the low readings
look like the layout (a link wire and coax braids at the junctions), not a fault. DC block on
the coax end read 0.001 V before DET went on.

Radio check 14:20/14:29, LS at g14, no pad: **open and load read the same** (every 50 MHz block
within -1.6 to +1.2 dB) instead of 25 dB apart, so the pass mark failed. But both runs sit at the
receiver's ceiling below 1.8 GHz (block medians +0.3 to -3, peaks +2.8), so compression could be
flattening both. **Next: the same pair with a 10 dB pad between the generator and RFIN**
(pre-registered: open about -12 dB at 1.3 GHz, load at least 20 dB below it or on the -38 floor).
If open and load still match off the ceiling, the bridge does not balance.

**Padded pair done (14:57 load, 15:02 open; the first was started as "open" with the load left
on and was relabelled, see its `label_correction`).** Off the ceiling now (peaks -4.1/-4.4, the
pad took 6-8 dB) and **open and load still match**: every 50 MHz block within -2.2 to +0.6 dB.

**Reading, before calling the board bad: the ports may be the other way round.** In a bridge with
a floating (balun-fed) source, the *detector* is the port that needs the second balun, because it
sits across the two bridge midpoints, while the *test port* is ground-referenced. On this board
the second ferrite pigtail goes to **RFOUT** and DETECK is wired straight to the junction. So RFOUT
is probably the **detector output** and **DETECK the test (DUT) port** - and these runs loaded and
unloaded the detector while the XTR's own 50 ohm input sat on the test port, which would change
very little, exactly as measured.

**Pre-registered swap test:** generator > 10 dB pad > RFIN; **RFOUT > DC block > XTR**; DETECK
open, then DETECK on the 50 ohm load; g14, LS 2 min each. If the swap is right: open vs load
**at least 15 dB apart** across most of 1241-2147. If they still match: the board does not
balance either way round, and it goes back.

**Swap test done (15:07 open, 15:09 load). The ports were the other way round: DETECK is the test
port, RFOUT is the detector output.** Open minus load, by 50 MHz block:

| Band | open | load | open - load |
|---|---|---|---|
| 1241-1541 | -12.3 to -16.6 | -24.7 to -27.8 | **+10.7 to +13.1** |
| 1541-1841 | -15.5 to -22.7 | -22.5 to -27.0 | +4.3 to +9.1 |
| 1841-2041 | -28.4 to -31.9 | -29.6 to -33.2 | +0.2 to +2.4 |
| 2041-2147 | -31.7 to -32.1 | -35.1 to -36.4 | +3.4 to +4.3 |

Against the pre-registered mark (at least 15 dB across most of the band): **not met**, but the
orientation question is settled - the same pad and gain gave ~0 dB the other way round. Two
limits are visible: above ~1.85 GHz the open reading is only 6-9 dB over the g14 floor (-38), so
the gap there is floor-limited, not a bridge figure; and at 1.24-1.54 GHz the load sits 11-13 dB
*over* the floor, so ~12 dB is a real directivity there, far short of the listing's 30 dB. The
usual cause is the detector port not seeing 50 ohm (the XTR's input through DC block, coax and
bulkhead is not a clean 50 ohm). **Next, pre-registered:** move the 10 dB pad from RFIN to
between RFOUT and the DC block, same everything else. If the detector match is the limit,
open - load at 1241-1541 rises to **at least 20 dB**; if it stays ~12, that is the board's own
directivity at these frequencies.

**Pad-on-detector done (15:18 open, 15:27 load): REFUTED, the detector match is not the limit.**
Open - load at 1241-1541 is **+9.3 to +11.8 dB**, against +10.7 to +13.1 with the pad at RFIN -
the same within a dB. 1541-1841 +4 to +9; 1891-2041 the load reads **2.5-2.8 dB louder than open**,
which is what poor directivity does (leakage and reflection add with a phase that turns with
frequency); above 2041 floor-limited. So **~11 dB directivity at 1.2-1.5 GHz, ~5 at 1.6-1.8, none
usable above 1.85 GHz**, against the listing's 30 dB. One cheap suspect left before blaming the
board: the 50 ohm load itself. **Next:** repeat the load run with each of the other terminators,
pad back at RFIN. A load that reads 10 dB+ lower than this one means the load was the limit; all
loads within ~2 dB means it is the bridge.

**Load test done, 16:16-16:23, all joints wrench-snugged first (the generator-side adapter had
been loose enough to twist), pad still on the detector side.** Open - load B / open - load A:
**+11.8 / +12.3 dB at 1241-1291**, +10.7 / +11.0 at 1441-1491, +6 to +8 at 1591-1741, about 0
or negative at 1841-2041, +2.4 to +4.9 at 2041-2147 (floor-limited). The two loads agree within
~1 dB everywhere that matters, and the tightened open reads within 1 dB of the earlier one.
**Verdict, as pre-registered: it is the bridge, not the load or the joints.** Measured
directivity ~12 dB at 1.25 GHz, falling to ~6 by 1.7 GHz and nothing usable above ~1.85 GHz,
against the listing's 30 dB. Not usable for the discone (1.5-2.2 GHz) or anything in LS above
~1.6 GHz. Untested: how it does below 1 GHz, where boards like this are usually designed to work.

4. Only then `marsad response` (`open`, `load`, `antenna`) using the commands the Antenna Bench
   generates, `--rx-floor-db -38.1` on the XTR. Antennas below 1766 MHz go on the v5 (g15.7, 10
   dB pad, no DC block, `--rx-floor-db -35.9`).

Also from 2026-10-22: `top` on the UWB locked all 22 hops to 2186.7 MHz in the evening, where
the morning run dropped everything above 2171.5. The cold/warm PLL test is still owed.

---

## Block H - rtl_power_fftw scans (started 2026-10-22)

### H1. Scale check: is the 3.3 dB mismatch the sample rate? (pre-registered 2026-10-22, 21:35)

This morning rtl_power_fftw at 2.4 MS/s read **-26.16 dB** against rtl_power's 50k scale on the
PCS downlink block, where the programs' own arithmetic predicts -29.5. rtl_power ran the dongle
at 2.7586 MS/s. Four 2 min runs back to back, same chain, g29, PCS downlink 1930-1990 MHz:

| # | Tag | Engine | Rate | Bins |
|---|---|---|---|---|
| A | `scale-A-rp50k` | rtl_power, 50k | 2.7586 MS/s (its own choice) | 43.10 kHz, FFT 64 |
| B | `scale-B-fftw-r2759k` | rtl_power_fftw, `-b 64` | **2.758620 MS/s** | 43.10 kHz, **identical bin width to A** |
| C | `scale-C-fftw-r2400k` | rtl_power_fftw, `-b 64` | 2.4 MS/s | 37.5 kHz, this morning's setting |
| D | `scale-D-rp50k` | rtl_power, 50k | 2.7586 MS/s | repeat of A, the bracket |

**Predictions, on the 1930-1990 block median (median over sweeps, then over the block):**

- **A vs D within 0.3 dB**, or the band moved during the block and B/C cannot be read.
- **C - A reproduces this morning's -26.2 within 0.5 dB**, or something else changed today.
- **B - A is the answer.** Near **-30.1 dB** (the model at matched rate): the sample rate is the
  whole story, and every fftw run at 2.4 MS/s is 3.3 dB "hot" relative to the model. Stays near
  **-26.2**: the rate is not it, and the difference lives in the E4000's IF filter shape. In
  between: both, split by where it lands.
- **C - B** is the same question read inside one engine: the model says 0 dB (both are per-Hz);
  +3.3 dB means the dongle delivers more per Hz at 2.4 MS/s than at 2.7586.

Read with block medians by hand (`compare-runs` refuses a cross-engine pair by design).

**DONE 21:46-22:01. Answer: the sample rate.** Read on 1930-1950, where A and D agree to 0.2 dB
(1950-1990 fell 1.4-2.0 dB between A and D as the network shed load at 22:00):
**B - A -29.6 to -29.9** (model -30.1), **C - A -26.2/-26.3** (morning -26.2), **C - B +3.3 to
+3.7**. The dongle hands over ~3.5 dB more per Hz at 2.4 MS/s than at 2.7586. Never compare fftw
levels across sample rates; to sit beside rtl_power data use `-b 64 --rate 2758620`. Full table
in CLAUDE.md "Block H1".

### H2. 1633.47 MHz at 1.17 kHz bins, then the 10 dB pad - NEXT (pre-registered 2026-10-22, 22:10)

One hop centred on **1633.000 MHz** (line at +470 kHz, clear of the DC spike; its zero-IF mirror
image would land at 1632.53), 2048 bins = **1.17 kHz**, 2.4 MS/s, `-t 10`, g29, 10 min each,
`--floor-window 0.1`. The narrow bins should lift a single-frequency line ~12.6 dB above what rtl_power's
21.5 kHz bins showed (18 dB on the deep tile), which is what lets a 20 or 30 dB drop be read
instead of vanishing.

| # | Tag | Pad |
|---|---|---|
| A | `h2-A-1633-nopad` | none - also the check that the line is there at all on the UWB |
| B | `h2-B-1633-pad10` | 10 dB between the notch and the LaNA input |
| C | `h2-C-1633-nopad` | none again - the bracket; the line wandered 6 dB in an hour on 2026-10-19 |

**What the archive already says (read before the run, 2026-10-22):**

- **The PCS-IMD story has a width problem.** 2 x 1799.98 - 1966.49 = 1633.47 only works if
  there is a narrow line at 1966.49. In every antenna run it sits **+0.8 to +1.2 dB** over the
  floor, i.e. no line, just the broadband LTE downlink. A third-order product of a wideband
  parent is at least that wide; 1633.47 is **one bin**. Its partner 2 x 1966.49 - 1799.98 =
  2133.00 is also absent (-0.7 to +0.5). On paper that specific product is ruled out.
- **But it does track something.** In Block E the line **vanished with the antenna outside**
  (B +0.1, B2 +2.2 dB over floor) and returned inside (A1 +9.7, A2 +10.7). 1799.98 also faded
  during B/B2, so that fits a product of 1799.98 and fits a source inside the room equally well.
  It does not track 1799.98 dB for dB (A1 vs casc5: 1799.98 -5.3 dB, 1633.47 -1.2).

**Predictions, on the line's median level, A/C mean against B:**

- **-10 dB (+/-2):** a real signal arriving at the antenna (a room device or outside).
- **-20 dB:** a second-order product made in our chain (e.g. 1799.98 - 166.51 MHz).
- **-30 dB or gone:** a third-order product made in our chain.
- A and C must agree within 3 dB (the line's own wander over half an hour), or B is unreadable.
- If A shows no line at all: stop, and look at 1799.98 before anything else.

**Amendment after A, before B (22:25).** A found the line at **1633.4721 MHz**, essentially one
1.17 kHz bin, median **9.1 dB** over floor, 32% occupancy at floor+10. That is ~16 dB weaker than
its history implies (8-11 dB in 43 kHz bins should be ~25 dB at 1.17 kHz): the UWB in its new spot
hears it far less than the dipole did, or it has faded. No mirror at 1632.53 (too weak to show).
At 9 dB a 10 dB drop does not leave a line, so the predictions are restated on the **peak bin's
median excess over floor**, background per bin 0.28 +/- 0.42 dB:

- real signal: **~2.6 dB** (1.2-4.6 allowing the line's own +/-3 dB wander) - about 5 sigma
- second- or third-order product made in the chain: **0.3 dB or less**, i.e. background

So B now separates **real vs made-in-chain**, not second- vs third-order. Noted, not hidden.

**DONE 23:18. Not made in our chain; real-at-the-antenna consistent but not shown.** Line medians
A -52.9, B (pad) -55.4, C -47.3. The bracket failed (A vs C 5.5 dB; C alone fell 8.7 dB in ten
minutes), but B held the line in all 59 spectra at 4.6-11.5 dB over the floor, which no 20-30 dB
intermod drop allows. B is ~5 dB below mean(A,C): fits -10 inside the wander, and also fits partial
entry after the pad. The pad also dropped the floor 2.5 dB. Next: hunt it in the room with a
probe. Full table in CLAUDE.md "Block H2".

### G-GP. 1265 MHz ground plane on the bridge (pre-registered 2026-10-25, before the antenna run)

Owner-built quarter-wave GP: vertical **52.0 mm**, four radials **59.0 mm** at **45 deg** droop,
1.63 mm wire, 3 mm feed stub. The Antenna Bench's model at those dimensions: effective length
(55 + 0.5 x 59) / 1.5 = 56.3 mm, end-effect 0.95, so **predicted dip 1264 MHz**. Bridge:
generator > RFIN, GP on DETECK, RFOUT > 10 dB pad > DC block > XTR, g14, `1241M:1541M:50k -i 10`,
open / GP / load, 2 min each.

- **Dip between 1241 and ~1290 MHz** if the build matches the model (+/-2% for the end-effect
  factor, +/-1 mm of cutting).
- About even odds the true dip sits **at or just below 1241**, inside the XTR's PLL gap: then
  the curve falls toward the bottom edge with no visible bottom, which reads as "slightly long".
- Only the dip's **frequency** is graded; with ~12 dB directivity its depth is not.

## H6. Does a warm XTR lose 1090 MHz? (pre-registered 2026-10-24, 10:00) - NEXT

H3 put the warm XTR's PLL gap at 1086-1230 MHz, so 1090 (ADS-B) sits inside it once warm; cold it
was 1094-1239, with 1090 just below. XTR idle since 00:17 (10 h). Whips on the dipole's outline
as left by H5-A2, nothing touched.

1. **Cold:** `adsb_check.py --seconds 600 --gain 29 --device 0`, straight away.
2. **Warm it usefully:** 1 h LS at the H5 fingerprint, whips on the outline - a morning whip
   reference beside H5's evening blocks.
3. **Warm:** the same `adsb_check.py`, straight after.
4. `rtl_test -t`, warm, to read the gap edge at that moment.

Predictions, on the DF17 count (not ICAO, which is inflated):

- **Gap reaches 1090 when warm:** warm DF17 falls **more than 5x** below cold (or to near zero),
  and the warm `rtl_test -t` puts the gap's lower edge at or below 1090.
- **It does not:** warm and cold DF17 within **2x** of each other (10 min counts swing, so 2x is
  the noise band for a single pair an hour apart).
- Between 2x and 5x: unreadable on one pair; repeat with the v5 running beside it as a traffic
  control.
- The cold run warms the dongle itself for 10 min, but the gap edge needs most of an hour to
  move 4 MHz past 1090, so the cold count stands.

**First pass, 2026-10-24 (read 23:10): no loss seen, but the warm half was not warm.** Cold 11:16:
**28,037 DF17** (3,793 frames/min). Warm-up survey 11:26-12:27. Warm check **12:57**, i.e. **30 min
after streaming stopped**, not "straight after": **19,552 DF17** (2,656/min). Ratio **1.43x**,
inside the 2x no-effect band, but the dongle had half an hour idle to cool back toward the cold
gap edge, and no warm `rtl_test -t` was recorded. So it does not answer the question. **Rerun with
the three steps chained, no gap between streaming and the warm check.**

### H5. Whip / UWB / whip on the dipole's outlined spot, same day (pre-registered 2026-10-23, 13:40) - NEXT

*Revised before any run, at the owner's prompt:* the four whip runs H4 was scored against are 2-4
days old and from other hours, so H5 brings its own same-day whip reference. Three 30 min blocks
back to back at H4's fingerprint (`custom 30m --freq 1241M:2148M:50k -g 29 -i 120
--skip-warmup 10`), **all on the dipole's outlined spot**, nothing else touched:

| Block | Antenna on the dipole's outline | Tag |
|---|---|---|
| A1 | 60 mm whips, 90 deg L (the standing dipole) | `h5-A1-whip` |
| B | UWB biconical | `h5-B-uwb` |
| A2 | 60 mm whips again (the bracket) | `h5-A2-whip` |

Read on within-run plateaus (block median minus the run's own 2049-2099 block); B against the
mean of A1 and A2.

- **A1 and A2 agree within 1.0 dB** on the PCS and AWS plateaus, or the hour moved under the
  block and B is not readable there.
- **PCS 1930-1990: UWB at least +1.0 dB over the whips.** **AWS 2124-2147: at least +1.0 dB**
  (same-spot deep tile at g42 gave +1.8 to +2.3; g29's squeeze toward the digital floor shrinks
  that to about +1.3).
- **1241-1341: within +/-2 dB.**
- **1633.47 MHz: +7 dB or more over the floor in all three blocks** if it is a nearby source at
  the spot (H4 at the UWB's own spot: +1.4). If it is strong with the whip and weak with the UWB
  on the same spot, the antenna, not the spot, is what hears it (polarization: the whip's L has
  a horizontal element, the UWB is vertical only).

If the UWB does not beat the whips here, the 2049 deep-tile win was specific to g42 or that
evening, and H4 is not about the spot.

**DONE 00:17.** Bracket failed (A2-A1: PCS -2.06, AWS +1.07, the late-evening network), but B sat
above both whips on both downlinks: **PCS +1.45, AWS +1.32 dB against the better whip - held.**
1241-1341 within +/-2: held. 1633.47 +7 in all three: failed for the UWB (+5.7). On the same spot
the **whips beat the UWB by 6-9 dB on the narrow carriers** 1799.98 and 1633.47 - likely
polarization - which withdraws H4's "near-field source by the dipole spot". Details in CLAUDE.md
"Block H5". Possible follow-up: UWB rotated horizontal, 10 min, same spot.

### H3. Cold-start top edge (pre-registered 2026-10-23, 11:20)

XTR idle (powered, not streaming) since 23:18 the night before, 12 h. Cold `rtl_test -t` at
11:18: **E4K range 52 to 2186 MHz, gap 1094-1239** - the 2026-10-13 cold figure (2187) to within
`rtl_test`'s 1 MHz search step. Then straight into `top 1h` (rtl_power_fftw, 22 hops, 2147-2187,
g29), the same settings as `uwb-first`, then `rtl_test -t` again, warm.

Predictions, from the pass-by-pass reading of `uwb-first` (2026-10-22 18:45):

- **All 22 hops lock on the first pass**, top centre 2185.80 MHz.
- **The top hops drop out from the top down and do not come back**: 2185.80 within the first
  ~5 passes (2 min), 2180.43 by ~15 min, 2173.27 by ~45 min (+/- 50% on each time).
- **2171.48 MHz holds for the whole hour.**
- **Warm `rtl_test -t` afterwards reports a top of 2175 MHz or lower**, against 2186 cold.

A hop that drops and later re-locks refutes "a smooth thermal slide" and points at marginal
locking. If the curve starts already lower than `uwb-first`'s, idle-powered is not "cold".

**DONE 12:19. Four of five held; one refuted.** All 22 locked at first; drop-outs strictly top-down
with no re-lock; 2185.80 gone at 0.4 min, 2180.43 at 7.6, 2173.27 at 29.9; **2171.48 did NOT hold
the hour (gone at 44.1 min)**; warm `rtl_test -t` read **52-2170, gap 1086-1230** against cold
52-2186, gap 1094-1239. The whole PLL range slides down as the dongle warms. **New open item:
warm, 1090 MHz sits inside the gap** - an ADS-B cold/warm check on the XTR is pre-registered in
CLAUDE.md "Block H3".

### H4. UWB reference on the LS fingerprint, 2 h (pre-registered 2026-10-23, 11:20)

`custom 2h --freq 1241M:2148M:50k --gain 29 --integration 120 --skip-warmup 16`, the standard
LS antenna fingerprint (`1241.000-2147.484 MHz | 43.7154 kHz | g29 | i120 | ...`), so the UWB
sits beside 21 archived runs including the 60 mm whip on today's chain (`xtr-casc5-antenna-g29`
2026-10-19 evening, `window-A1/A2-inside` 2026-10-20 morning). Chosen over a 1847-2148 tile
because a tile would match nothing.

Read on **within-run plateaus** (block median minus the run's own empty 2049-2099 block, the
digital floor), because the runs are at different hours:

- **AWS downlink 2124-2147: UWB at least 1.5 dB above the whip runs** (the 2049 deep tile gave
  +1.8 to +2.3 at g42).
- **PCS downlink 1930-1990: UWB at least 1.0 dB above** the whip.
- **1241-1341: within +/-2 dB of the whip** - the 60 mm whips are near quarter-wave resonance
  there, so the UWB has no advantage to show.
- 1799.98 MHz is not used (retired as a yardstick 2026-10-20).

**DONE (cut to 38 min by a Ctrl-C at 12:57; 11 sweeps analysed). Two of three refuted.** AWS
plateau UWB +0.25 dB over the whip mean (predicted at least +1.5); PCS **-1.39** (predicted at
least +1.0); 1241-1341 within +/-2 (held). The whip runs sit in the dipole's own spot, the UWB
8 in away, so this measures antenna plus position; the same-spot deep tiles still stand. **Next:
UWB onto the dipole's outline, 30 min at this fingerprint.** Bonus: 1633.47 MHz nearly vanished
(+1.4 dB) with the UWB, pointing at a source close to the dipole's spot - start the probe hunt
there. Details in CLAUDE.md "Block H4".

---

## Rules that apply to every run

- Check the device index first. USB order is not guaranteed: `.\rtl-sdr\rtl_test.exe`
- Hold the room steady across a comparison block, and write down what it was doing.
- Never change range, bin, gain, integration, crop or FIR. That is the fingerprint.
- Archiving and the terminated/antenna flag are automatic; no follow-up commands.
- Compare with `--family 14.4`, not 28.8 - the odd half-harmonics are just as strong.
- Use `py tools/compare_runs.py <before> <after>` for any A/B. It does the whole
  analysis: fingerprint check, median, floor by block, family statistic with a
  verdict against the measured detection floor, periodicity.
- **Write the prediction down before the run.** Every result this programme trusts
  came from a prediction that could have failed; every one it had to retract came
  from a number that matched after the fact.
