# SDR Station – Claude Code briefing

This folder is the working environment for a hobby radio-monitoring station: an RTL-SDR receiver on a Windows PC, used for spectrum surveys, signal hunting, and (later) demodulation and satellite reception. Planning, RF physics and measurement design happen in a separate Claude chat project; **this repo is for code, tooling and data.**

The owner is learning as they go. Explain what commands do and why, keep changes small and reversible, and prefer plain-language summaries over jargon.

---

## Environment

- **OS:** Windows. **Shell:** Git Bash (paths look like `/c/SDR/...`). PowerShell is also available.
- **Python:** use the `py` launcher (`py -m pip ...`, `py script.py`). Use a virtual environment in `C:\SDR\.venv` and keep dependencies in `requirements.txt`.
- If an interactive Windows program hangs in Git Bash, prefix it with `winpty`.

### Target layout (create it if missing; move existing files in, don't duplicate)

```
C:\SDR\
├── CLAUDE.md            ← this file
├── RUNSHEET.md          ← the order to run things in; start here
├── BASELINE_PROTOCOL.md ← why each run is shaped the way it is
├── SHOPPING.md          ← what to buy and in what order, with the reasoning
├── requirements.txt     ← numpy, pandas, matplotlib, scipy (+ pillow if needed)
├── .venv\
├── rtl-sdr\             ← RTL-SDR Blog Windows release (rtl_power.exe, rtl_test.exe, rtl_tcp.exe, rtl_fm.exe, rtl_adsb.exe, rtl_biast.exe, rtl_sdr.exe)
├── station.json         ← last-used antenna/chain defaults (written by run_survey.py)
├── occupancy.db         ← SQLite archive of every survey (tools/archive.py)
├── tools\               ← survey.py, run_survey.py and future scripts
├── surveys\             ← raw rtl_power CSVs + a .json sidecar per survey (settings + conditions)
├── reports\             ← survey.py output, one folder per survey
├── recordings\          ← IQ / baseband recordings (SDR++ recorder, rtl_sdr)
└── decoders\            ← notes/configs for rtl_433, URH, SatDump
```

Never delete raw survey CSVs or recordings without asking.

---

## Hardware (facts that affect the code)

| Item | Details |
|---|---|
| **Main receiver** | Nooelec **NESDR SMArTee XTR** (shows as "SMArTee XTR v5ee"). RTL2832U + **Elonics E4000** tuner, zero-IF. 0.5 ppm TCXO (28.8 MHz). |
| XTR range | **52–2187 MHz** and **gap 1095–1240 MHz**, both measured on this unit (SN 08701076) with `rtl_test -t` on 2026-10-13. Nominal spec is ~52–2200 (some units ~2300) with a ~1105–1268 gap; the E4000 PLL varies per unit, so trust the measured figures. |
| XTR gains (dB) | -1, 1.5, 4, 6.5, 9, 11.5, 14, 16.5, 19, 21.5, 24, 29, 34, 42. Use these exact values with `-g`. |
| XTR bias-tee | **Always on** (hardware); SDR++ "Bias T" checkbox and `rtl_biast` have no effect. |
| Second receiver | Nooelec **NESDR SMArt v5** (R860 tuner, low-IF), 24–1766 MHz, no gap, HF direct sampling 0.1–25 MHz. Cleaner and more sensitive above ~700 MHz; planned as sniffer / L-band (1.3–1.7 GHz) receiver. |
| v5 bias-tee | **None.** Measured 2026-10-16: 0.1 mV at the SMA centre pin. It is a SMArt, not a SMArTee, so a plain DC-coupled 50 ohm load is safe directly on its SMA. The XTR still needs a DC block. |
| LNA | Nooelec **LaNA** (20–4000 MHz, ~16–25 dB gain, NF 0.9–1.6 dB), powered by the XTR's bias-tee, mounted at the antenna pigtail (immediately after the notch) and supported at the window so no weight hangs on the antenna's SMA. |
| Filter | Nooelec **Flamingo+ FM v2** notch (blocks 88–108 MHz; passes DC). Since 2026-10-14 it sits **at the antenna, ahead of the LaNA**, so broadcast FM is rejected before the amplifier instead of after it. |
| Antenna | Window suction-cup dual-whip dipole, whips adjustable ~50–125 mm each. **Standing config: 90° L, 60 mm each whip** (confirmed 2026-10-13) — quarter-wave at ~1250 MHz, i.e. near-resonant at the bottom of the LS band and electrically short toward 2147 MHz. Whip length in sidecars is **per whip**, not tip-to-tip. |
| Shielding | SDR sealed in a copper-taped plastic enclosure (built 2026-10-14): lid/body seam covered with conductive-adhesive copper tape, SMA and USB bulkheads bonded to the cage, dongle thermal-padded to a wall with perforated foam holding it against the pad. Ferrite chokes on the external cable segments only - **not inside the box**, measured 2026-10-18 to cost ~1.3 dB at 1670-1690 MHz. |

Only **one program can use an SDR at a time.** Before running `rtl_power` etc., SDR++ must be closed (check and warn).

### Signal chain (as built 2026-10-14)

```
antenna (90 deg L) -> pigtail -> Flamingo+ notch -> LaNA -> 8 ft coax
  -> [SMA bulkhead] -> 6" jumper -> SDR -> USB extension
  -> [USB bulkhead] -> USB M-M -> 16 ft USB -> PC
```

Ferrite chokes on the external segments. **Not on cables inside the enclosure** - measured
2026-10-18 to raise 1670-1690 MHz by ~1.3 dB. The notch is deliberately **before** the amplifier
so broadcast FM cannot compress the LaNA.

The notch+LaNA pair sits **at the antenna**, ahead of the 8 ft run, so that cable's loss
falls after the gain stage and costs essentially nothing in noise figure. (They were
briefly at the cage end during the 2026-10-14 build and were moved back the same day.)

---

## Known spurs at this station (auto-tag these, don't report them as discoveries)

- **600 kHz comb**: lines at exact multiples of 0.6 MHz across the whole range (e.g. 978.6, 1089.6/1090.2/1090.8, 1540.2/1540.8/1541.4, 1620.0/1620.6/1621.2, 2131.2; also the 240 MHz comb). Suspected switching power converter. Seen on both receivers.
  **Partly disproven in the LS band 2026-10-14** - see the terminated run below, and
  **shown to be a pure coincidence-match in 1095-1240 MHz 2026-10-16** (real spacing there
  is 604.2 kHz) - see the v5 PLL-gap survey. Do not trust this tag without measuring the
  spacing between adjacent lines.
- **14.4 MHz × n**: SDR clock harmonics. **Corrected 2026-10-16: the fundamental is 14.4 MHz,
  not 28.8.** On a terminated v5 run over 700-1700 MHz the odd multiples of 14.4 (the ones the
  28.8 family misses) are **exactly as strong** as the even ones: odd +4.91 dB vs even +4.92 dB
  above the whole-band median, n=35 each, difference -0.01 dB with SE 0.82. Every "28.8 MHz"
  tag in older notes is a subset that was missing half its members - e.g. 1022.4 = 14.4 x 71 was
  untagged and steady at 100% occupancy. Use `--family 14.4` with spur_stats.
- **Hop-centre DC spike**: E4000 zero-IF artefact at the centre of every rtl_power hop.
  `-c 0.25` does **not** remove these — crop discards the outer 25% of each hop (the
  anti-alias roll-off at the *edges*) and keeps the centre, where the DC spike lives.
  Measured 2026-10-13: 49 of 64 FFT bins kept per hop, and the 5 min LS survey still
  tagged hop-centre artefacts (strongest 1537.8655 MHz = dead centre of hop 141).
  Keep `-c 0.25` for the edge roll-off; `survey.py`'s hop-centre tag is what handles
  the DC spikes.
- **1499.73 + 1500.80 MHz pair**: appears intermittently, drifts together (warm-up), likely one local device.
- Possible PC clock families: 24, 25, 100 MHz × n; USB 480 MHz × n.

### Cage rebuild 2026-10-14: measured effect

Sealing the SDR in the bonded copper cage and moving the notch ahead of the LaNA left the
spur set essentially unchanged (1439.99 +0.8 dB, 1612.79 -0.2 dB, 1799.98 -2.1 dB) and the
median floor +0.3 dB.

Caveat on reading that: the pre-cage runs were **already inside a crude copper-taped cardboard
box**, so this measured "crude cage -> properly bonded cage with bulkheads", not "unshielded ->
shielded". Either the spurs are conducted (USB, power, coax shield), or the cardboard box was
already handling the radiated path. Both remain open.

A 50 ohm load at the LaNA input shows what is *not* arriving via the antenna, which separates
self-generated from received - it does not by itself separate conducted from radiated. Put the
load at the LaNA input, never at the SDR: the bias-tee is always on and a DC-coupled load shorts
it, and terminating there also removes the LNA gain, destroying comparability.

Thermal: per-sweep whole-band median std **0.151 dB** over 30 sweeps, total drift **-0.32 dB**
across the hour (pre-cage: 0.166 dB / -0.48 dB). The sealed enclosure did **not** run away and
needs no vents. Most of the drift is in the first ~8 sweeps, so pre-warm ~40 min before a long
run, or discard the first few sweeps.

### 1265.44 MHz: a periodic pulsed emitter (2026-10-14)

Found by the `burst23` preset (1 h, `-i 2`, 1800 sweeps). The LS survey could only ever see it
as a "rare burst" because it listens on any one frequency for ~14 s out of 2 h (0.19%).

| Property | Measured |
|---|---|
| Centre / width | 1265.44 MHz, ~737 kHz |
| Events in 1 h | **164** |
| Duration | **every single event fit in one 2 s sweep** - none spanned two |
| Gap between events | median **10 s**, min 10 s, max 82 s |
| Level | peak -21.0 dB vs -40.4 dB quiet nearby (~19 dB) |

**Confirmed by IQ analysis (105 s baseband, uint8, 2.4 MSPS, tuned 1265.44 MHz):**

| Parameter | Measured |
|---|---|
| Rotation | **12.000 s** (8 consecutive intervals, 11.90-12.10) = **5.00 RPM** |
| Pulse width | **113.8 us** |
| Modulation | **linear FM chirp, -606 -> +800 kHz** (~1 MHz sweep), straight to 55 kHz rms |
| Time-bandwidth product | ~114, so compressed to ~1 us = ~150 m range resolution |
| PRF | **staggered**: 2745 us (364 Hz) x4, 3157 us (317 Hz) x4, 3473 us (288 Hz) |
| Mainlobe hits per scan | ~19 pulses = ~57 ms = ~1.7 deg beamwidth at 30 deg/s |
| Dwell above threshold | ~420 ms (includes sidelobes) |

L-band, 5 RPM, pulse compression and PRF stagger together are the standard configuration of an
**air route surveillance radar** (ARSR class). The 23 cm amateur allocation is secondary to
radiolocation here, which is consistent. The exact installation is not identifiable from the
signal alone. Levels say nothing about distance: these are multi-megawatt sets and the nearest
could be tens of miles away.

Analysis scripts were throwaway; the recording is in the SDR++ recordings folder, not this repo.

**Watch the hop centres on narrow surveys.** The first burst23 run used 1264M:1268M, putting hop
centres at exactly 1265.000 and 1267.000 MHz; their zero-IF DC spikes sit ~24 dB above floor and
masked the target. The preset now starts at 1263M so the centres land at 1264.05 and 1266.15.
Also pass a `--floor-window` well under the span (0.3 MHz here) - the 2 MHz default is half of a
4 MHz survey and the rolling median stops being a floor.

### USB galvanic isolation 2026-10-15: no effect. The question is closed.

A high-speed USB isolator at the PC end (the only position where the dongle would still
enumerate). `rtl_test -s 2800000` first: four losses of 68 bytes, tally **0 samples per million**.
Terminated run, same settings as the other two, `--skip-sweeps 8` on both sides.

| Check | Result |
|---|---|
| Median floor | -37.5 dB vs -37.6 and -37.6 before - the LaNA is still powered, so the comparison stands |
| Whole-band median | -37.430 -> -37.400 dB (**+0.030**) |
| 28.8 MHz harmonics, n=31 | **+0.23 dB, standard error 0.26** |
| New spur families from the isolator's DC-DC | none |

That makes **three independent external interventions, all measuring zero**: a properly bonded
cage (0), a repaired USB shield bond (-0.14 +/- 0.24), and galvanic isolation of the USB ground
(+0.23 +/- 0.26). Enclosure, shield and ground path are all ruled out. The 28.8 MHz family is
coupled **on the dongle's own PCB**, where the RTL2832U's oscillator sits beside the tuner, and
no external measure can reach it. Do not spend more on this.

The isolator is RF-neutral: it removed nothing and added nothing. Keep it or remove it, but
decide once and record it in station.json, because it is part of the measurement chain either way.

Incidental: the whole-band median repeated to **0.03 dB** across a day, a hardware change and a
different time of day. That is the station's real stability figure.

### Diurnal behaviour, 16 h LS survey 2026-10-15 (01:16 - 16:58)

472 sweeps at `-i 120`, `--skip-sweeps 8`. The whole-band median is *not* flat over a day:

| Hours | Whole-band median |
|---|---|
| 01-04 (night) | -36.87 dB |
| 05-07 | -36.41 dB |
| 08 (peak) | **-35.65 dB** |
| 09-10 | -35.89 dB |
| 11-16 | -36.11 dB |

Range -37.00 to -35.25, std 0.398 dB over the run - far above the 0.165 dB per-sweep stability,
so the swing is real. It peaks at 08:00 and then *falls* through the afternoon, which does not fit
a simple room-temperature ramp; that shape argues for human RF activity rather than thermal drift,
but the two are not separated. A terminated run at 03:00 and another at 08:00 would settle it.

**The uplink bands are what make day different from night:**

- **1760-1770 MHz** (AWS-3 uplink): absent all night (peaks 0.8-3.6 dB), appears from 09:00 with
  peaks of 20-36 dB. Strictly daytime - handsets near the window.
- **1850-1910 MHz** (PCS uplink): 0-7% at night, 20% at 09:00-10:00, 17% at midday.
- **1920-1930 MHz** (DECT): present all day and night, strongest at 01:00-02:00 (22 dB).
  Always-on base-station beacon rather than traffic.
- **1265.4-1266.3 MHz** (the radar): 0-17% per hour with peaks 21-26 dB, which is what sparse
  sampling of a steady emitter looks like at 0.19% listening per frequency.

Caution reading per-hour occupancy: the floor is a 20th percentile over the *whole* run, so a
quieter night floor lets more bins clear floor+10. Peak SNR per hour is the robust figure.

**No overload.** The six strongest sweeps peak at -1.0 to -6.7 dB, and the whole-band median
during them sits only 0.1-0.3 dB above that hour's mean - within the diurnal spread. Peaks that
close to full scale are worth one check at `-g 24` if certainty is ever needed.

### Hop-wide detections are impulses, not signals (survey.py, 2026-10-15)

rtl_power samples a hop as one block, so a click during that block lifts the whole hop's FFT at
once. `find_signals` now tags any detection at least 0.8 hop wide as
`hop-wide (N hops): impulse, not a signal`. On this 16 h survey that caught **34** of the 147
detections, including several of the strongest.

### 50 ohm terminated run 2026-10-14: what is actually ours

Method: 50 ohm load at the FM notch input, antenna disconnected, everything else untouched;
45 min at `-i 120`; both runs re-analysed with `--skip-sweeps 8` so the warm-up is excluded
from each.

- **The noise floor is internally set.** *(Superseded 2026-10-19 for the rebuilt chain: with
  3 ft coax and the notch at the antenna, antenna-on raises the 20th-percentile floor 3-4.5 dB
  below 1.55 GHz. The station is external-noise-limited there. See "XTR cascade step 5".)*
  Median floor -37.4 dB with the antenna, -37.6 dB
  terminated. The antenna adds no measurable noise at L/S band, so this station is
  *internal-noise-limited*: a better antenna would raise signals without raising the floor,
  which is a direct SNR gain. Antenna work pays here; more receiver gain does not.
- **Confirmed self-generated: the 28.8 MHz clock family.** Still present with no antenna at
  x44, 48, 50, 51, 56, 61, 62 = 1267.18, 1382.41, 1439.99, 1468.80, 1612.79, 1756.79,
  1785.60 MHz. Every one an exact multiple. This is the SDR's own TCXO and it is the real
  internal spur set in this band.
- **NOT self-generated: the "600 kHz comb" lines.** The strongest, 1799.9832 MHz at 21.1 dB
  SNR, **vanishes** when terminated, as do 1414.81 and 1728.60. They arrive through the
  antenna and are external signals, not our switching supply.
  Caution on the tag: with 43.7 kHz bins a 0.6 MHz comb has a line every ~13.7 bins, so about
  **7% of arbitrary signals land within half a bin of one by chance**. A lone 600 kHz comb tag
  is weak evidence. The 28.8 MHz tags are the trustworthy ones here.
- **Nothing unexplained is ours:** with the antenna off, zero untagged signals were detected.
- Confirmed external (vanish when terminated): the 1265.56-1266.22 MHz cluster, 1336.38,
  1397.15, 1414.81, 1728.60, 1731.09, 1799.98, 1861.67, 1926.36/1926.71.

### USB bulkhead bond repair 2026-10-14: no effect, and what that settles

Sanded the coating off the USB bulkhead body, copper-taped it to the cage, covered the unused
port. Re-ran terminated, same settings, `--skip-sweeps 8` on both.

Measured over all 31 28.8 MHz harmonics in band (level above the whole-band median):
**mean change -0.14 dB, std 1.36, n=31** (standard error 0.24 dB). Whole-band median moved
**0.01 dB**. No effect.

So the clock harmonics are **not** coupling through the USB shield or the enclosure. They are
almost certainly generated and coupled on the dongle's own PCB, where the RTL2832U's 28.8 MHz
oscillator sits beside the tuner - a known RTL-SDR trait that external shielding cannot fix.
**Stop chasing them with shielding.** They are a permanent, characterised, auto-tagged feature.
(Keep the repaired bond anyway: it is correct practice and may still matter for noise escaping
the box toward the antenna, which a terminated run cannot see.)

**Repeatability, now calibrated** (two back-to-back terminated runs, 15 sweeps each):

| Quantity | Run-to-run spread |
|---|---|
| Whole-band median | 0.01 dB |
| Individual harmonic level | std 1.36 dB |

So the overall level is extremely stable, but a single line's level is not. Treat a change in one
signal of less than ~2.7 dB (2 sigma) as noise; to detect anything smaller, average over many
lines as above. The peak-finder's threshold crossings are even noisier - lines sitting near
floor+10 dB flip in and out of the signal list between identical runs.

Strongest harmonics above the whole-band median: x51 (1468.8) +14.7 dB, x61 (1756.8) +14.6,
x50 (1440.0) +13.6.

### v5 in the XTR's PLL gap 2026-10-16: 1095-1240 MHz, first look

First survey of the band the XTR cannot tune, using the **v5** (R860, low-IF, no gap).
`1095M:1240M:25k -g 29.7 -i 30`, 1 h, 120 sweeps, first 32 discarded as warm-up; 21.58 kHz
bins, **45.8 s of listening per frequency**, 0.83% occupancy step. The chain was the most
spur-prone yet used: wire dipole 46 mm legs, **no LNA, no filter, v5 uncaged** on a USB
extension at the PC. Fingerprint
`1095.000-1240.000 MHz | 21.5774 kHz | g29.7 | i30 | c0.25 | F9 | v5` - nothing else shares
it, so this is a baseline, not a comparison.

**"1095-1175 MHz is empty" - RETRACTED the same day, see the ADS-B control below.** The raw
observation stands (3708 bins, not one crossed floor+10 dB in any of the 88 analysed sweeps),
but the receive path was not working, so it measures the chain and not the band.

**Everything sits in 1185-1232 MHz, and it is two separate things:**

| Feature | Measured |
|---|---|
| Broadband pedestal | floor -36.8 dB at 1100, rising to **-28.8 dB at 1205-1210**, back to -35.8 by 1240; ~45 MHz wide |
| Comb riding on it | **604.2 kHz spacing**, confined to 1190-1210 MHz |
| Strongest line | 1200.0044 MHz, 14.4 dB SNR, **100% occupancy**, steady |
| Time behaviour | dead steady - median ~ max-hold, vertical stripes in the waterfall, no structure across the hour |

The comb is real and localised: autocorrelation of the detrended median spectrum gives
**r = +0.885** at 604.2 kHz over 1190-1210, with its 2x harmonic (1208.4 kHz) also present.
Over 1100-1140, 1140-1180, 1180-1190, **1210-1232** and 1232-1240 it is absent (r between
-0.10 and -0.005). So the pedestal is *not* a comb - the comb merely sits on the pedestal's
peak, and they are probably two different sources.

The pedestal does **not** appear in the signal list, and that is correct: survey.py's 2 MHz
rolling-median floor tracks a 45 MHz-wide rise, so a pedestal is a floor by definition. Read
it off the per-band `floor_db` column, never off the detections.

**All six "600 kHz comb" tags on this survey are false positives.** The real spacing is
604.2 kHz, 0.7% off. The tagger matched 1200.0044 to 0.6 x 2000 because 1200 is a round
number - which is also why it simultaneously claims 24 MHz (x50), 25 MHz (x48) and 100 MHz
(x12) for the same line. Treat a 600 kHz tag as noise unless the *spacing between neighbouring
lines* has been measured; autocorrelating bins.csv does it in seconds.

**What it is not:** not GNSS (1164-1215 is the RNSS allocation, but GNSS is spread-spectrum
tens of dB below the noise floor and cannot appear as a peak in rtl_power); not DME/TACAN
(pulsed and bursty, this is constant); not a hop-centre artefact (hop centres are 1199.607 and
1201.679, the lines fall between them, and the R860 is low-IF so there is no centre DC spike
anyway). It also stops below 1240: three XTR LS runs show no 604.2 kHz periodicity over
1241-1300 MHz (r = -0.13 to -0.22), so it is not a receiver artefact that would continue.

**What it is: untested.** No LNA, no filter and an uncaged dongle at the PC is the likeliest
source, but that is a guess. **A 50 ohm terminated v5 run settles it** in 45 min, the same way
the cage, shield-bond and isolator questions were settled. With no LNA in this chain the load
goes at the v5's own input - so check first whether the v5's bias-tee is on, which this file
does not record (the XTR's is always on, and a DC-coupled load would short it).

**Method note: this run had no positive control.** Starting at 1085 instead of 1095 would have
put **1090 MHz ADS-B** inside the span - continuous, strong, certain to be present - proving
the 46 mm legs with no LNA can hear anything at all. Without it, "the bottom 80 MHz is empty"
rests on an unverified receive path. Put a known-present signal inside the span of any survey
whose headline result is an *absence*. `rtl_adsb.exe` is in `rtl-sdr/`.

Incidental, from the same cross-check: the XTR's LS bins.csv is dominated by a **2.098 MHz**
periodicity (r = 0.90-0.95), which is exactly the LS hop width. Hop-to-hop response variation,
not a signal - but it is the largest spectral texture in those files, so any future
periodicity hunt on LS data must detrend per hop first.

### The v5 chain is FM-overloaded 2026-10-16: the gap survey measured nothing

An ADS-B positive control at 1090 MHz, run straight after the gap survey, with heavy traffic
overhead:

| Chain | Gain | Frames/min | Distinct ICAO |
|---|---|---|---|
| XTR + window dipole + notch + LaNA | 29 | **1856.4** | 344 (inflated, see below) |
| v5 + 46 mm wire dipole, no LNA, no notch | 40.2 | **0** | 0 |
| same, repeated after a tool fix | 40.2 | **0** | 0 |
| same, at the v5's maximum gain | 49.6 | **0** | 0 |

Same sky, same minutes, same tool, two dongles feet apart. Zero across a 9.4 dB gain range is
an open path, not a weak one - a signal merely below the decode threshold improves with gain.

**But the antenna and the dongle are both fine.** `rtl_power -f 88M:108M:100k -g 29.7 -d 0`
through the same 46 mm wire: **38 bins more than 10 dB above median, peaks at +3.80 dB**, i.e.
essentially at ADC full scale. Median level across 88-108 MHz was **-14.1 dB against -36.0 dB**
for the L-band survey at the *same* gain (about 16 dB after normalising the different bin
widths). A 46 mm stub is 0.015 wavelength at 98 MHz and still saturates the front end - that is
how strong local FM is here.

**So the v5 is not deaf, it is compressed.** The chain has **no FM notch** - the Flamingo+ is in
the XTR chain at the antenna - and broadcast FM is driving the R820T's fixed-gain front end into
compression, raising its noise floor and destroying sensitivity everywhere else. This is exactly
the failure the notch exists to prevent, and the v5 chain was built without one. The missing LNA
compounds it, and the 46 mm element is short for the band, but **the notch is the first fix.**

Consequences:

- **Every level in the v5 gap survey is suspect.** The band was not empty; the chain was not
  listening. It is consistent that the only identifiable steady features in it were internal
  clock harmonics (28.8 x 39, 28.8 x 40, 24 x 49, 100 x 11) - on-PCB spurs do not need an
  antenna, and they were all the v5 could hear.
- **The 1190-1210 comb and pedestal are almost certainly internal or conducted**, not received.
  A terminated run would still be worth doing, but the ADS-B result already argues for it.
- **Do not grade an antenna on that survey.** No length conclusion can be drawn from it.

**The LaNA cannot simply move to the v5 chain**: it is powered by the XTR's always-on bias-tee,
and the plain SMArt v5 is not a SMArTee, so it has no bias-tee to power one. A v5 LNA needs an
external injector.

**Never again run a survey whose headline result is an absence without a positive control inside
or beside the span.** `tools/adsb_check.py` does it in 60 seconds at 1090 MHz and prints a
pass/fail. It cost one minute and overturned an hour-long survey.

Two notes on the tool itself: `rtl_adsb` block-buffers its output file, so a hard kill discards
up to ~4 KB - most of a quiet run. `adsb_check.py` stops it with CTRL_BREAK so it flushes.
And `icao_distinct` is **inflated**: with `-e 5` allowed bit errors, errors landing in the
address field mint phantom aircraft (the control's DF counts include unassigned formats 19, 22
and 23, which are pure noise decodes). Use the DF17 count, not the ICAO count, to compare chains.

### v5 terminated baseline 2026-10-16: the first v5 reference, and both suspects vanish

50 ohm load directly at the v5's SMA (no bias-tee to worry about - measured 0.1 mV), nothing
else attached, open bench. `700M:1700M:50k -g 29.7 -i 120`, 1 h, 22 sweeps after a 16 min
warm-up discard. Fingerprint `700.000-1700.000 MHz | 43.6758 kHz | g29.7 | i120 | c0.25 | F9 | v5`.

| Quantity | Value |
|---|---|
| Whole-band median | **-35.890 dB** |
| 28.8 MHz family (n=35) | **+4.92 dB** above median, sd 3.73, SE 0.63 |
| 14.4 MHz family (n=70) | +4.92 dB above median, sd 3.40, SE 0.41 |
| Signals over floor+10 | 6, of which 5 are tagged spurs |

**Both features from the retracted gap survey are gone with the antenna off.**

| Band | Terminated | Antenna run | Delta |
|---|---|---|---|
| 1190-1200 | -36.07 | -32.01 | **-4.06** |
| 1200-1210 | -36.05 | -29.21 | **-6.83** |
| 1210-1220 | -36.17 | -31.86 | **-4.31** |
| 1220-1230 | -36.06 | -33.08 | **-2.98** |

The terminated floor is flat to within 0.25 dB across the whole 700-1700 MHz span. The
604.2 kHz comb is gone too: autocorrelation r = **+0.057** where it was **+0.885**.

**What that does and does not settle.** Recorded in BASELINE_PROTOCOL.md before the run:
removing the antenna removes real external signals *and* the FM overload together, so a feature
that vanishes is **ambiguous**. Only a feature that *persisted* would have been conclusive.

Against that, the shape argues for something real: simple front-end compression raises the floor
**broadly**, because the compressing signal (FM) is present whatever the tuner is set to. It does
not produce a 45 MHz hump centred on 1207 MHz, and it certainly does not produce a **periodic
604.2 kHz comb** - compression noise has no structure. One mechanism that could localise it is the
R820T's input tracking filter, whose FM rejection varies with tune frequency. Unresolved either way.

**The clean test is antenna + notch**, now that the second filter is on its way: real signals
present, no overload. If the comb survives that, it is genuinely external.

**Incidental: the sidecar's antenna/LNA/filter fields were stale.** report.md for this terminated
run says "Antenna | Wire dipole 46mm leg vertical, LNA | no LNA, Filter | No Filter" because
`--non-interactive` fills those from station.json. `chain_order` is correct and is the field that
matters, but do not read the header fields of a `--non-interactive` run as fact.

### Repeatability is set by HANDLING, not by time (2026-10-17)

A 6 h terminated run overnight, printer unplugged, dongle not touched since A2 finished. Same
fingerprint as A1/A2, 180 sweeps. It is effectively a third "cage off" block, and it recalibrates
what this station can resolve.

| Comparison | What differed | Per-line std | SE (n=70) |
|---|---|---|---|
| overnight vs **A2** | nothing - hardware untouched | **0.16 dB** | **0.02** |
| overnight vs **A1** | dongle removed, caged, removed again in between | 3.41 dB | 0.41 |
| A1 vs A2 | same handling | ~3.4 dB | 0.41 |

**Handling the dongle BETWEEN runs costs about 20x more than six hours of elapsed time.** The
dongle is never touched during a scan - what differs is what happened in the gap. A1 and A2 have
two handling events between them (into the cage, out again); A2 and the overnight run have none.
Two runs with nothing moved in between agree to 0.04 dB on the family mean; two runs with the
dongle lifted in and out between them scatter by 3.4 dB per line.

**Sweep count is not the explanation** - re-analysed with `--max-sweeps 30 --skip-sweeps 8` so the
overnight run has the same 22 analysed sweeps as A2, the scatter is **0.15 dB, SE 0.02**, unchanged
from 180 sweeps. The old 1.36 dB figure in these notes came from XTR runs that also involved
handling in between, so it was measuring this without knowing it.

Consequences for every future experiment here:

- **An experiment that needs no handling can resolve hundredths of a dB.** Switching something in
  the room, plugging or unplugging a device, changing the PC's state - all detectable at ~0.05 dB.
- **An experiment that needs handling is limited to a few tenths**, and needs bracketing or
  repeated in/out cycles to average the position variance down.
- The cage result (-1.07 dB, SE 0.23) survives this: it is still about 4.6 sigma. But its error
  bar is dominated by handling, not by anything about the cage, so the replication should do
  **several in/out cycles**, not one.

**Thermal stability, terminated, 6 h:** after the first sweep the whole-band median sits at
-35.85 dB with a standard deviation of **0.039 dB**. Discarding the warm-up sweep, the total range
across six hours is under 0.1 dB. The floor does not wander overnight.

### The printer is not the 240 MHz source (2026-10-17)

Unplugged for the whole 6 h run, against A2 with it powered and idle inches from the dongle:

| | A2, printer idle | overnight, printer unplugged |
|---|---|---|
| 720 MHz | -19.35 dB, 100% | **-19.33 dB, 100%** |
| whole-band median | -35.840 | **-35.840** |

Identical. The printer contributes nothing measurable, which also means it was never a usable
test signal for the cage.

**The real anomaly is that A1 is the outlier.** Something changed between A1 (21:38) and B1
(00:12) and then **stayed** changed through A2 and all six overnight hours: 720 MHz went from
-23.3 dB at 0% occupancy to -19.3 dB at 100%, and 1440 MHz went the other way, from -24.1 dB at
100% to -30 dB at 0%. Not the printer, and not the cage either - both later runs are cage-off.
A persistent state change in the room or the PC, still unidentified. It did not move the aggregate
statistics, which is why the A1/A2 control still passed.

### 1633.47 MHz deep tile (2026-10-19, 22:29): one bin, never moves, never off

`deep-1600-1633hunt`, `1600M:1701M:25k -g 29 -i 30`, 1 h, 120 sweeps, 21.5 kHz bins, 68 s of
listening per frequency. Hop 16 centre is at 1634.010, 25 bins away, so the DC spike is not
involved.

| Question | Answer |
|---|---|
| Width | **one bin**, 21.5 kHz. Neighbouring bins sit 15 dB down, symmetric: the FFT skirt, not sidebands |
| Family | **none** - no other line within 1600-1701 except the known clock harmonics at 1612.80, 1670.41, 1699.20 |
| Frequency drift | **zero**: the peak bin was 1633.4734 in all 120 of 120 sweeps |
| Occupancy | **100%** at 30 s resolution; no gaps at all |
| Level | wanders -17.2 to -23.5 dB over the hour, down ~5 dB from 22:30 to 23:00 then partly back |
| SNR | 18.1 dB median over the tile's -39.4 floor |

So it is an **unmodulated carrier that is crystal-locked in frequency but not steady in
amplitude**. Two readings fit, and they can be told apart with the attenuator kit:

1. **A nearby device leaking a fixed tone.** Nothing clean falls out of dividing 1633.4734 by
   small integers (not 24, 25, 26, 27, 19.2, 16.369, 14.4 or 12 MHz times anything), and this
   file's rule is that a matching number would not be evidence anyway.
2. **A third-order intermodulation product made in our own LaNA.** 2 x 1799.98 - 1966.49 =
   1633.47, and 1966.49 is inside the PCS downlink (1930-1995 MHz), which is strong and always
   on. An amplifier fed two strong signals makes 2f1-f2 and 2f2-f1 at low level. It would be
   absent on a terminated run, exactly as observed, and its amplitude would wander as the
   parents do, which also fits. It first appeared on 2026-10-15, after the chain was rebuilt
   with the notch ahead of the LaNA, and the g42 run raised it 4 dB, the same as a linear
   signal, which argues it is not made in the tuner. Whether it is made in the LaNA is open.

**The discriminating test, pre-registered:** a 10 dB pad at the LaNA's input, antenna on, 10
min, against the same 10 min with no pad. A real external carrier drops **10 dB**. An IMD3
product generated in the LaNA drops **30 dB**, because a third-order product falls three times
as fast as its parents. Tile `1600M:1701M:25k -g 29 -i 30` both times. If it is IMD, the LaNA
is being driven too hard by the downlink bands and the fix is a pad or a filter ahead of it,
and every other weak line in the L/S surveys needs the same suspicion.

### XTR cascade step 5 (2026-10-19): antenna on, at g29 and g42 - the chain is 3.5 dB better, the gain ladder was overstated

`xtr-casc5-antenna-g29` (18:47) and `xtr-casc5-antenna-g42` (19:48), back-to-back, antenna
straight onto the notch input, 90 deg L with 60 mm whips, evening, window in shade. Both 1 h,
22 sweeps analysed, **0% non-finite readings at either gain** - no clipping. The g42 run shares
no fingerprint with anything else and is read from its own report.

**Pre-registered for g29:** 1799.98 MHz carrier at **-16.6 dB median, within 1.5 dB**
(four old 60 mm runs); whole-band median in -36.1 to -37.0.
**Pre-registered for g42:** carrier rises **~13 dB**, floor ~1.6 dB, if the tuner delivers its
nominal gain.

| Quantity | notch, terminated | antenna g29 | antenna g42 |
|---|---|---|---|
| 1799.98 MHz, median over sweeps | - | **-13.1 dB** | **-9.3 dB** |
| 1799.98 SNR (median) | - | 22.9 dB | 23.9 dB |
| Whole-band median | -37.50 | -35.24 | -32.58 |
| 20th-pct floor 1241-1341 | -37.34 | -32.80 | -28.21 |
| 20th-pct floor 1541-1741 | -37.67 | -36.50 | -33.96 |
| 20th-pct floor 1941-2041 | -38.31 | -35.51 | -33.44 |
| 20th-pct floor 2041-2141 | -38.35 | **-38.35** | **-38.52** |
| Max over band | - | -1.39 | -3.23 |
| Detections / untagged | 5 / 0 | 16 / 9 | 32 / 16 |

**g29: the carrier beat its prediction by 3.5 dB.** -13.1 against -16.6 predicted, and the
archive's max-hold SNR for this line is 24.4 dB against 18.8-23.8 in every earlier run. The
rebuilt chain - 3 ft RG316 instead of 8 ft, bonded aluminium enclosure, notch at the antenna -
is the best this carrier has ever read. **New reference for 1799.98 at 60 mm: -13.1 dB median.**

**The floor is received noise below ~1.55 GHz, and the 2026-10-14 claim was wrong for this
chain.** Antenna on raises the 20th-percentile floor by **+4.5 dB at 1241-1341, +3.3 to +3.6 at
1341-1541**, ~+1.1 at 1541-1741, +0.5 at 1741-1941, +2.8 at 1941-2041 (PCS downlink) and
**0.00 at 2041-2141**. The 16 h survey of 2026-10-15 shows the same block pattern (its
1241-1341 median was -32.42 against -32.12 tonight), so this is the environment, not the new
chain. "The antenna adds no measurable noise at L/S band" (2026-10-14, 0.2 dB) was a property
of the old, lossier chain, not of the band. **Below ~1.55 GHz the station is now
external-noise-limited, which is what a receive chain should be.** From 1.55 to 1.95 GHz the
floor rises only 0.5-1 dB, and above 2.04 GHz nothing at all comes through - the only sign of
life up there is +2.3 dB at 2141-2147, the AWS downlink.

**g42: 13 dB of nominal gain delivered about 4 dB, and 1.0 dB of SNR.** The carrier rose
**+3.8 dB**, not 13. In the blocks where the floor is received noise it rose by the same
amount (+4.6, +4.3, +4.1 dB at 1241-1541), which is a gain change of **~4 dB** measured two
independent ways. The received-noise blocks rising with the carrier means SNR there is
unchanged by construction; only where the digital floor still dominates does g42 buy anything,
and on the carrier that is **+1.0 dB**. Above 2.04 GHz the floor did not move at all (-0.17).

**This corrects the gain ladder.** Its terminated numbers stand, but "roughly 10-13 dB of SNR
on the table" assumed the E4000 delivers its nominal steps, and it does not in this band:
29 -> 42 is worth ~4 dB of real gain at 1.3-1.8 GHz and nothing above 2 GHz. The digital floor
is real, but the lever to get above it is far shorter than the gain table says. **Reading 2
("the gain stages roll off") is the larger effect for the 29 -> 42 step.** What 14 -> 29 delivers
on a signal is unmeasured.

**Decision: stay at g29 for L/S surveys.** g42 buys 1 dB on one carrier, nothing in the busy
blocks, and would break the fingerprint of every antenna survey since 2026-10-13 for it. The
overload margin at g42 was fine for this hour (max -3.2 dB) but the 16 h survey saw -1.0 at
g29 in daytime with handsets at the window.

**Untagged lines worth knowing about:**

- **1633.47 MHz, steady, 91-100% occupancy, SNR 12-16 dB**: present in every antenna LS run
  since 2026-10-15 (6 of 37 surveys covering it), absent from every terminated run, so it is
  external. Inside the 1626.5-1660.5 MHz mobile-satellite uplink allocation, but a 100%-steady
  carrier is not what a satellite terminal transmits. Not a 14.4, 24, 25 or 100 MHz multiple.
  **Unidentified.** A `deep` tile over 1600-1700 would give it 20 kHz bins and 2 min dwell.
- **1859.22 MHz, intermittent, 59% at g29 and 86% at g42**: PCS uplink band, a handset near
  the window, as the diurnal section predicts for evening. First time detected; not new in kind.
- **1405.93 MHz, rare burst at g29, 23% at g42**: seen once before in the 16 h survey at 0.2%.
  Inside the 1400-1427 MHz passive band where nothing should transmit. Weak and unidentified.

**The XTR cascade is complete.** Bulkhead 0.00, coax +0.02, LaNA +0.64 (tilted), notch 0.00,
antenna +2.26 whole-band and +4.5 dB where the band is busy, with the reference carrier 3.5 dB
stronger than any earlier chain. Every element has a number and a prediction it was tested
against.

### XTR cascade step 4 (2026-10-19): the notch adds exactly nothing, as thermodynamics requires

`xtr-casc4-notch`. Flamingo+ notch on the LaNA's input, DC block and load moved to the notch
input, nothing else touched. Notch input centre pin **0.00 V** with the XTR powered, so the
LaNA blocks the bias-tee DC ahead of the notch. Sun on the window, glass not warm, chain at
room temperature. Started 17:11, so the run spanned sunset.

**Pre-registered: zero.** A passive filter at room temperature with a load on its input
delivers kTB to the LaNA whatever its insertion loss, so a terminated run cannot see the
notch's passband loss. Anything else would be a bad connector or pickup at the window.

| Quantity | step 3 | step 4 | change |
|---|---|---|---|
| Whole-band median | -37.500 dB | -37.500 dB | **+0.000 dB** |
| Largest 100 MHz block | | | -0.05 dB at 1441-1541 |
| 14.4 MHz family, paired, n=63 | +2.75 | +2.66 | -0.09 dB, SE 0.12, 0.8 sigma |
| Re-analysed, 18 warm-up sweeps discarded | -37.53 | -37.56 | -0.03 dB |

**Held.** The connectors are sound and the notch picks up nothing at the window. The terminated
XTR cascade is complete: bulkhead +0.00, coax +0.02, LaNA +0.64 (tilted), notch +0.00.
Everything a 50 ohm load can measure has been measured; the notch's insertion loss and the
LaNA's real gain need a signal, which is step 5.

**Warm-up grows along the cascade, and it does not matter.** `drift.py` total change including
warm-up: step 2 -0.32 dB (settled by sweep 11), step 3 -0.59 (sweep 14), step 4 -0.79
(sweep 18). All three curves have the identical smooth monotonic shape; only the amplitude
differs, and the step 4 run also spanned sunset with the LaNA on the window. The 16 min discard
leaves some of it in, but it is the same in both halves of each comparison and cancels: the
step 3 result was identical at 14 sweeps discarded and the step 4 result at 18. Unexplained.
For any run where the first sweeps matter, pre-warm 35 min or run back-to-back.

### Gain ladder 2026-10-19: the XTR's floor is a digital floor, and tuner gain barely moves it

**Corrected by cascade step 5 the same evening:** the terminated numbers below stand, but the
SNR arithmetic assumed the E4000 delivers its nominal gain steps. On a real carrier 29 -> 42
delivered **~4 dB, not 13**, and 1.0 dB of SNR. See "XTR cascade step 5".

Three 10 min runs, LaNA terminated at its input, chain untouched since `xtr-casc3-lana`, tuner
gain 14 / 29 / 42, five sweeps each with two discarded. Per-sweep std 0.013-0.034 dB, so three
sweeps are enough. Diagnostic fingerprints, not comparable to the cascade.

| Tuner gain | Whole-band median | 1241-1441 | 1441-1641 | 1641-1841 | 1841-2041 | 2041-2147 |
|---|---|---|---|---|---|---|
| 14 | -38.13 | -38.03 | -38.07 | -38.15 | -38.25 | -38.25 |
| 29 | **-37.46** | -37.04 | -37.24 | -37.52 | -38.08 | -38.24 |
| 42 | -35.88 | -34.79 | -35.32 | -36.08 | -37.62 | -38.13 |

**The g29 point reproduces `xtr-casc3-lana` (-37.50) to 0.04 dB** - the untouched-hardware
repeatability figure, again.

**28 dB of tuner gain raised the floor 2.25 dB overall and 0.12 dB at 2041-2147 MHz.** A
receiver whose floor was set by amplified input noise would rise a dB per dB. This one has a
fixed floor at about **-38.3 dB on rtl_power's scale** (the top block at every gain, and the
top blocks of the no-LaNA baseline) that nothing upstream of the ADC changes. Reading 1 from
step 3 holds: **the XTR's floor across 1241-2147 MHz is a digital floor, not thermal noise.**
Even with 20 dB of LaNA in front, amplified thermal noise only draws level with it at `-g 42`
and only below about 1.5 GHz. At g14 with the LaNA (-38.13) the floor equals g29 without it
(-38.14): the LaNA's whole contribution at g29 is simply amplified noise peeking over a fixed
floor.

**What follows for sensitivity.** Signals pass through the tuner gain; the digital floor does
not. Where the floor is fixed, every dB of tuner gain is a dB of SNR, so at g29 the station has
been leaving most of 13 dB on the table at the top of the band and about 10 dB at the bottom.
**Every L/S survey so far has been gain-limited**, and the 2026-10-14 "antenna adds no noise,
the station is internal-noise-limited" finding is this same fact seen from the other side.

**One caveat before acting on it.** The clock harmonics, which are injected on the PCB, rose
3-7 dB for the 13 dB step from g29 to g42 (1468.80: -24.7 -> -18.1; 1756.79: -26.8 -> -24.0),
less at higher frequency. That is consistent with the E4000's gain stages rolling off toward the
top of its range - the tuner may not actually deliver 13 dB more at 2 GHz. Noise alone cannot
separate "the ADC dominates" from "the gain setting does little up there". An **external steady
signal at two gains** separates them: the 1799.98 MHz carrier with the antenna on, at g29 and
g42, hour-matched. If it rises ~13 dB the tuner is delivering and the SNR gain is real.

**Decision deferred: which gain the antenna surveys should use.** The cascade stays at g29 to
keep its fingerprint; the antenna step should be run at both g29 and g42 so the two-gain test
above comes free. The cost of g42 is overload headroom: the 16 h LS survey peaked at -1.0 dB
full scale at g29 with DECT and PCS handsets near the window.

**The terminated notch step is pinned to zero by thermodynamics.** A passive filter at room
temperature with a load on its input delivers kTB to the LaNA whatever its insertion loss, so
step 4 cannot see the notch's passband loss. Only a signal can. Its value is as a control:
anything other than zero is a bad connector or radiated pickup at the window.

### XTR cascade step 3 (2026-10-19): the LaNA raises the floor 0.64 dB, and it is not flat

`xtr-casc3-lana`. LaNA fitted on the far end of the 3 ft coax, DC block and load moved to its
input, nothing else touched. Bias-tee measured **4.82 V** at the SMA bulkhead and at the coax
end, **0 V** at the LaNA input - the LaNA blocks DC on its input, so the DC block is kept for
consistency with step 2 rather than for protection. Same fingerprint, 1 h, 30 sweeps.

**Pre-registered:** floor rises 0.6-0.8 dB, broadband and flat, from the three old
LaNA-in-chain XTR runs at -37.4 to -37.6 dB against -38.16 without it.

**Measured, against step 2 (`compare_runs.py xtr-casc2-coax xtr-casc3-lana --family 14.4`):**

| Quantity | step 2 | step 3 | change |
|---|---|---|---|
| Whole-band median | -38.140 dB | -37.500 dB | **+0.640 dB** |
| 1241-1341 MHz | -38.11 | -37.08 | **+1.03** |
| 1441-1541 | -38.09 | -37.26 | +0.83 |
| 1641-1741 | -38.11 | -37.49 | +0.62 |
| 1841-1941 | -38.24 | -38.03 | +0.21 |
| 1941-2147 | -38.30 | -38.25 | **+0.02 to +0.08** - nothing |
| 14.4 MHz family, paired, n=63 | +3.21 dB | +2.75 dB | -0.46 dB, SE 0.06 |

Re-analysed with 14 warm-up sweeps discarded instead of 8: **+0.640 dB, identical**, every block
within 0.1 dB of the above. The rise is the amplifier, not warm-up.

**The magnitude matched the prediction; the shape did not.** The rise is a smooth tilt from
about +1.0 to +1.4 dB at 1241-1450 MHz down to zero above 1.95 GHz. Over the top 200 MHz the
LaNA adds nothing measurable at all. A ~100 MHz ripple rides on the tilt (peaks near 1240, 1340,
1440, 1540, 1610, 1710), which is the right period for a standing wave on roughly 1 m of
RG316 between two imperfect matches - and that is a dimension-match, which this file has learned
to distrust. The falsifiable version: swap to the 8 ft coax and the ripple period should fall to
roughly 35-40 MHz. Untested.

**The harmonics did not move.** 1468.80 median -24.3 -> -24.7 dB, 1756.79 -26.5 -> -26.8,
1670.41 -27.0 -> -27.2: all within handling scatter. The family statistic falls only because the
floor under them rose. That is what on-PCB spurs sitting *after* the amplifier must do, and it is
a second confirmation that they are not arriving through the antenna port.

**Thermal: PASS, but the LaNA adds its own warm-up.** `drift.py`: settled by sweep 14 (~25 min)
against 11 for step 2; total change including warm-up -0.59 dB against -0.32. After settling,
std 0.027 dB, range 0.090. The 16 min discard still gives the right median (see above), but
pre-warm the chain 30 min before any run where the first sweeps matter.

**Sun on the window (noted 2026-10-19).** The window the chain is mounted to faces the sun most
of the afternoon, so the LaNA, notch and load warm in direct sunlight. Bounded small: a load at
330 K instead of 300 K is 0.4 dB more noise, and the whole LaNA output only lifts the floor
0.64 dB, so the effect at the receiver is a few hundredths of a dB. Irrelevant for handled
experiments at the 0.5 dB floor; a real confound for untouched-hardware comparisons at 0.05 dB,
which should be run at night or hour-matched. Step 3's slower settling (25 min) may be partly
this. **Record the sun state in every conditions string.** A terminated run spanning sunset with
nothing touched would measure it with `drift.py`; not yet done.

**The open question this raises: 0.64 dB is far too small for a working 20 dB amplifier.** A
terminated LNA with ~20 dB gain and ~1 dB noise figure delivers noise roughly 21 dB above kTB to
the receiver. If the XTR's floor at `-g 29` were set by front-end thermal noise (E4000 noise
figure of order 6-8 dB) that should lift the floor by well over 10 dB, not 0.6. Two readings:

1. **The floor at `-g 29` is set by the ADC / digital path, not by thermal noise.** The LaNA's
   output noise is only just emerging above a fixed digital floor, most at the low end where its
   gain is highest and the coax loss lowest, not at all above 1.95 GHz. This also explains the
   station's earlier finding that the antenna adds no measurable noise: a digital floor hides
   it. If true, **every L/S-band survey so far has been gain-limited**, and more tuner gain would
   buy real sensitivity until the thermal floor appears - at the cost of overload headroom (the
   16 h survey peaked at -1.0 dB full scale).
2. **The LaNA is delivering far less than its rated gain** at 1.2-2.1 GHz through this chain.

**Discriminating test, no handling, 30 min:** leave the chain exactly as it is and run three
10 min checks at `-g 14`, `-g 29` and `-g 42`, tagged as a gain ladder (its own fingerprint,
not comparable to the cascade). If the floor climbs about a dB per dB of tuner gain, the floor is
input-noise-dominated and reading 2 holds. If it climbs much less than that between 14 and 29,
the receiver is digital-floor-limited at 29 and reading 1 holds. Either answer changes how the
antenna surveys should be run.

### XTR cascade step 2 (2026-10-19): the 3 ft coax to the window costs nothing

`xtr-casc2-coax`. The load and DC block moved from the SMA bulkhead to the far end of a **3 ft
coated RG316 coax** run to the window and secured in its permanent position, with an SMA
male-male adapter for gender. The 3 ft was chosen over the old 8 ft black coax for less slack
and less loss. Nothing else touched: same enclosure, same jumper, XTR still direct on the USB
bulkhead. Same fingerprint as `xtr-alu-baseline`, 1 h, 30 sweeps, 8 discarded as warm-up.

Compared with `compare_runs.py xtr-alu-baseline xtr-casc2-coax --family 14.4 --handling handled`:

| Quantity | step 1 (bulkhead) | step 2 (+ 3 ft coax) | change |
|---|---|---|---|
| Whole-band median | -38.160 dB | -38.140 dB | **+0.020 dB** |
| Largest 50 MHz block change | | | **-0.08 dB** at 1891-1941 (every other block within 0.07) |
| 14.4 MHz family, paired, n=59 | +2.99 dB | +3.09 dB | **+0.09 dB, SE 0.10, 0.9 sigma** |
| 28.8 MHz family, paired, n=31 | +5.46 dB | +5.54 dB | +0.08 dB, SE 0.31, 0.3 sigma |
| Signals over floor+10 | 7, all clock harmonics | 9, all tagged | |

**Nothing moved.** Every number is inside the 0.5 dB detection floor for a handled experiment,
and the family statistic is consistent with no change. The coax, the adapter and the bulkhead
pass-through add no measurable noise anywhere in 1241-2147 MHz. That is the cascade question
answered for this step, and it matches the v5 result for its bulkhead step (+0.010 dB).

**Thermal: PASS.** `drift.py` settles by sweep 11 (~19 min), then std **0.026 dB**, range 0.090,
net drift -0.090 dB. Total change including warm-up -0.32 dB, the same shape as the baseline.

**The two extra detections are not signals.** 1250.44 and 1258.83 MHz are both hop-centre
tagged, 306 kHz wide, at 4.5% occupancy, i.e. one sweep out of 22 - an impulse during one hop
block. The rest of the list is the same 28.8 MHz family as step 1, with the usual per-line
reshuffle: 1756.79 went from 31.8% to 100% occupancy and 1295.99 dropped below threshold.
Per-line scatter here is 1.70 dB (28.8 family) - this station's handling figure, not an effect.

**Known single difference from the reference:** the printer was unplugged for this run and was
powered-idle inches from the enclosure during `xtr-alu-baseline` (see that sidecar's
`conditions_correction`). With a +0.02 dB result there is nothing for it to explain, which is a
second measurement of the 2026-10-17 finding that the printer contributes nothing.

**Next in the cascade:** the LaNA on the far end of the coax, where the load is now (the XTR's
bias-tee powers it through the coax, so the DC block stays with the load at the amplifier's
input, never at the XTR), then the notch ahead of the LaNA, then the antenna. Write the
prediction for each before running it; the old LaNA-in-chain XTR runs at -37.4 to -37.6 dB say
the amplifier should raise the floor by roughly 0.6-0.8 dB, broadband and flat.

### XTR alone, in the aluminium enclosure: the first clean XTR reference (2026-10-19)

`xtr-alu-baseline`. 50 ohm load + DC block on the **outside** of the SMA bulkhead, XTR plugged
**direct into the USB bulkhead** (no internal USB cable at all), one 6 in SMA jumper in an S-bend
inside, no ferrites in the box. `1241M:2148M:50k -g 29 -i 120`, 1 h, 30 sweeps, 16 min warm-up
discarded. Fingerprint `1241.000-2148.000 MHz | 43.6758 kHz | g29 | i120 | c0.25 | F9 | xtr`.

| Quantity | Value |
|---|---|
| Whole-band median | **-38.160 dB** |
| Floor 1250-2150, by 100 MHz block | **-38.11 to -38.31** - spread **0.20 dB across 900 MHz** |
| Signals over floor+10 | **7, every one a tagged 28.8 MHz clock harmonic** |
| Unexplained features | **none** |

**This is the XTR's first measurement of itself.** All three earlier XTR terminated runs
(-37.4, -37.5, -37.6) had the **LaNA in the chain and powered**, with the load at its input, so
they measured receiver + amplifier. Removing the LNA removes its noise contribution, and the
floor drops about 0.6-0.7 dB - the right direction and a plausible size. Those runs are **not**
comparable to this one; this is the new reference for every Block D cascade step.

**Thermal: PASS, and the direct-plug question is closed.** `drift.py` reports settling by sweep
10 (~17 min), then std **0.021 dB**, range 0.080, net drift -0.070 over the remaining 40 min.
Total change including warm-up -0.22 dB. The air gap under the dongle costs nothing measurable,
exactly as the plastic-box figures predicted. **No further thermal work is needed on this build.**

**The unbonded USB bulkhead shell is costing nothing visible.** The shell is a plastic coupler in
a zinc sleeve with no connection to the aluminium (measured open), and the floor is still flat to
0.20 dB across 900 MHz with zero unexplained features. Bonding it remains worth testing when a
metal-bodied bulkhead arrives, but nothing in this baseline demands it.

**Spur-tag caution, again.** Every one of the seven detections carries a "600 kHz comb" tag at
absurd multiples (x2160 to x2928) alongside its real 28.8 MHz identification. These are the
coincidence matches this file already warns about - with 43.7 kHz bins a 0.6 MHz comb has a line
every ~13.7 bins, so roughly 7% of arbitrary frequencies land on one. **Read the 28.8 tag, ignore
the comb tag** unless the spacing between neighbouring lines has actually been measured.

### The second notch works: FM overload removed from the v5 (2026-10-19)

Second Flamingo+ fitted directly on the cage SMA bulkhead with the 46 mm dipole on its input,
nothing else changed. `fm-check-withnotch` against `fm-check-p6`, same settings five hours apart.

| | no notch | with notch | change |
|---|---|---|---|
| FM median 88-108 | -15.60 dB | **-26.14 dB** | **-10.54** |
| 90.33 MHz | -7.56 | -23.98 | -16.42 |
| 95.5 MHz | -7.36 | -25.55 | **-18.19** |
| 98.7 MHz | -9.42 | -25.72 | -16.30 |
| Gap above the terminated L-band floor | 20.2 dB | **9.6 dB** | -10.5 |
| Non-finite readings | **7.4%** | **0%** | all 241 bins report |
| Signals over floor+10 | 12 | **0** | |

**The rejection figure is a lower bound.** After the notch the carriers sit at -21 to -26 dB
against a band median of -26.14, i.e. down in the noise, so the measurement is floor-limited.
Rejection is *at least* 16-18 dB and may be much more; this chain cannot see below its own floor.

**The hard evidence is the NaN count going to zero.** 7.4% of readings were previously driven
past what rtl_power's accumulator can represent, including 90.33 MHz which never produced a
finite value in 30 sweeps. Every bin now reports. Nothing in the FM band saturates any more.

**What is still open: whether this buys anything at 1090 MHz.** The overload was real and is now
largely gone, but P6 already showed the chain decodes ADS-B *while* overloaded, so the benefit is
not implied. That needs 10 min ADS-B runs - the no-notch baseline is only 188 DF17 frames over
two minutes (94/min) across two runs that disagreed by 6.5x.

### ADS-B frame counts are far too noisy for a 60 s A/B (2026-10-18)

Two runs four minutes apart, **nothing touched between them**:

| Run | DF17 | frames/min | distinct ICAO |
|---|---|---|---|
| `v5-P6-caged-46mm-nonotch` 23:54 | **163** | 329 | 84 |
| `v5-P6b-repeat-nonotch` 23:58 | **25** | 98 | 26 |

**A 6.5x swing with identical hardware.** A 60 s ADS-B count samples whatever aircraft happen to
be overhead, and that changes on a timescale of minutes. **Never compare chains on single 60 s
runs.** Use 10 min minimum, and bracket A/B/A if the expected effect is under about 2x.

This does not weaken the P6 refutation: zero is outside the 25-163 spread by construction, and
the 2026-10-16 chain read zero four times across a 9.4 dB gain range.

**For the notch question, prefer a steady signal.** Broadcast FM is always present and does not
come and go like aircraft, so `fm-check` measures the notch's actual rejection to a fraction of
a dB in five minutes, where ADS-B needs tens of minutes to resolve a factor of two. Measure what
the notch *does* on a steady signal first; measure what it *buys* at 1090 MHz second, and budget
real time for it.

### P6 REFUTED 2026-10-18: the v5 decodes ADS-B with no notch

Pre-registered 2026-10-16: *v5 + 46 mm, no notch, still decodes ~zero ADS-B.* Run tonight at
23:54 on the caged v5, 46 mm dipole on the cage SMA bulkhead, no notch, no LNA, gain 40.2:

| | 2026-10-16 | 2026-10-18 |
|---|---|---|
| DF17 frames / 60 s | **0** | **163** |
| All frames per minute | 0 | **323.5** |
| Distinct ICAO (inflated, see note) | 0 | 84 |
| Verdict | FAIL across a 9.4 dB gain range | **PASS** |

**The overload diagnosis survives; the explanation built on it does not.** `fm-check-p6`, one
hour earlier on this exact chain, put the FM median 20.2 dB above the terminated floor with
90.33 MHz unmeasurable in any of 30 sweeps. The front end **is** being driven hard, and it
decodes 84 aircraft anyway. So FM overload is real and was **not** the cause of the 2026-10-16
zero.

**What this corrects.** In "The v5 chain is FM-overloaded 2026-10-16" below, the measurements
stand but these claims are withdrawn:

- "the v5 is not deaf, it is compressed" - it was deaf, for a reason still unknown
- "broadband FM is ... destroying sensitivity everywhere else" - unsupported; the same overload
  is present now with the path working
- "**the notch is the first fix**" - it is an optimisation of unknown size, not a fix

**The gap survey stays retracted.** The 2026-10-16 ADS-B zero was genuine, so that chain really
was not receiving and the survey really does measure the chain rather than the band. Only the
*reason* changes, from "overloaded" to "unidentified".

**Leading suspect: the faulty black 6 in USB extension**, which was in the chain that day and is
the one component here with a measured defect. Unproven - its measured signature was at
1670-1690 MHz, not 1090 - and the chain differs in several other ways too (uncaged then vs caged
now, dipole on the dongle vs on the bulkhead, 17:00 vs 23:54). Do not promote this to a cause
without a test.

**For scale:** the XTR read 1856 frames/min on 2026-10-16. The v5's 323.5 is about 5x worse,
which is what a 46 mm element with no LNA and no filter should look like - a handicap, not a
fault. Use the DF17 count (163); the 84 ICAO figure is inflated by `-e 5` error tolerance, and
the unassigned downlink formats in the histogram (19, 22, 23, 26-31) are noise decodes.

**The notch question is now a clean A/B** and nothing blocks it: fit the second notch, change
nothing else, re-run the identical command. A large DF17 rise means the overload was costing
real sensitivity; no change means it is real but harmless at 1090 MHz.

### FM overload re-confirmed on the caged v5, and a parser bug that hid it (2026-10-18)

`fm-check-p6`, 5 min at `88M:108M:100k -g 29.7`, 46 mm dipole on the cage SMA bulkhead, no notch.
This is the positive control for P6 when the XTR is unavailable: it tests the exact chain the
ADS-B run uses, which the XTR never could.

| Quantity | 2026-10-16 (uncaged, dipole on dongle) | 2026-10-18 (caged, dipole on bulkhead) |
|---|---|---|
| FM median, 88-108 | -14.1 dB | **-15.60 dB** |
| v5 terminated L-band floor | -36.0 | -35.76 |
| **Gap** | **21.9 dB** | **20.2 dB** |

Two days apart, different enclosure, different antenna mounting, and the gap agrees to 1.7 dB.
**The front end is still being driven hard**, and the ADS-B run uses gain 40.2 against this
check's 29.7, so conditions during the decode attempt are 10.5 dB worse than these numbers show.

**90.33 MHz never produced a finite reading in any of 30 sweeps.** The strongest local station is
literally unmeasurable by this chain.

**The parser bug.** rtl_power emits non-finite values on its strongest bins, and the Windows MSVC
build spells them **`-nan(ind)`**, which was not in `load_rtl_power`'s accepted list of nan
spellings. `float()` raised, and the handler discarded the **entire line** - all 240 readings in
that hop. The survey analysed as **146 of 240 bins**; with the fix it is **241**, and 7.4% of
individual readings are flagged missing instead.

A bad *bin* must never cost the whole *hop*. `_reading()` now returns NaN for anything
non-finite however it is spelled, and only a broken timestamp or frequency field drops a line.

**The non-finite fraction is now reported, and it is a saturation diagnostic.** Those NaNs are
not random: they land on the strongest carriers (90.3, 93.3, 95.5, 94.5, 98.7 MHz, all reading
-7.6 to -11 dB on the sweeps where they parse), and bins adjacent to a NaN average 2 dB above the
band median. Above 1% the analyser now says so and warns that **peak levels are lower bounds**.

Consequences:

- **Every antenna survey before 2026-10-18 may have silently lost data** wherever a strong signal
  saturated a bin - the FM, milair and `wide` presets most of all. Terminated runs are unaffected
  (`casc4-noferrite` re-analyses to the identical 22,897 bins).
- A survey reporting far fewer bins than `scan_build.md` predicted was the symptom, and it was
  visible all along in the "skipping malformed line" spam. Read that output.

### The v5 terminated baseline is finished (2026-10-18)

The "start over with proper controls" programme has its result. **`casc4-noferrite` is the
reference run**: v5, 50 ohm load directly on the SMA, in the cage with the lid on, interior copper
stripped, sound USB extension, no ferrites inside the box, no jumper, bulkhead bare.

| Property | Value |
|---|---|
| Fingerprint | `700.000-1700.000 MHz \| 43.6758 kHz \| g29.7 \| i120 \| c0.25 \| F9 \| v5` |
| Whole-band median | -35.76 dB |
| Floor 1620-1700 | flat within 0.3 dB, no hump |
| Replication | agrees with `cyc8-usb6-white` to **0.21 dB** in every 10 MHz block 1620-1700 |

Everything that was contaminating it has been found and removed: a faulty USB cable (~5 dB),
ferrite chokes inside the box (~1.3 dB), and a sidecar's worth of mislabelled chains. Compare
future v5 work against this run.

**The cage: leave the v5 in it, and stop testing it with a load.** With a sound cable the cage
changes the floor by +0.16 dB in 1600-1700 and +0.09 dB elsewhere - it does not hurt. Whether it
*helps* cannot be answered by a terminated run at all, and this file has said so since the
pre-registration: with a 50 ohm load and a quiet room there is nothing for a cage to block, so a
perfect cage must measure zero. The cage's actual job is stopping the dongle's own emissions from
reaching the **antenna**, which a terminated run cannot see by construction.

The keep-it reasons are mechanical, and they are good ones: fixed geometry, strain off the SMA,
and a dongle that does not move between runs. **Handling between runs is this station's dominant
error source** - 3.4 dB per line versus 0.16 dB for six hours of elapsed time - so a box that
holds the dongle in one place is attacking the largest term in the error budget directly.

**Do not put the interior copper back.** It was only ever shown to increase coupling to the faulty
cable. Fewer parts is the default.

**The test that could show a cage benefit is blocked on the FM notch.** It needs a real signal:
ADS-B frame counts or a known carrier's SNR, caged versus uncaged. On the v5 that returns 0 either
way until the notch is in, because the chain is overload-limited. Park it until then.

### CONFIRMED 2026-10-18: ferrite chokes on the internal USB cable cost ~1.3 dB at 1675 MHz

`casc4-noferrite` took both ferrites off the 6 in USB extension and changed nothing else.
Pre-registered: 1670-1690 would return to `cyc8`'s value if the chokes were the cause.

**It did, exactly.**

| 10 MHz block | casc3, chokes on | casc4, chokes off | change |
|---|---|---|---|
| 1620-1630 | -36.80 | -36.80 | 0.00 |
| 1640-1650 | -36.41 | -36.69 | -0.28 |
| 1660-1670 | -36.72 | -36.96 | -0.24 |
| **1670-1680** | **-35.36** | **-36.71** | **-1.35** |
| **1680-1690** | **-34.98** | **-36.26** | **-1.28** |
| 1690-1700 | -36.69 | -36.88 | -0.19 |

Whole-band median moved -0.090 dB, inside the 0.1 dB validity gate registered beforehand.

**And `casc4` is a true replication of `cyc8`** - identical hardware, no jumper, bare bulkhead,
same cable, no chokes, cage on, runs ten hours apart. Every 10 MHz block from 1620 to 1700 agrees
to within **0.21 dB**, and the two key blocks to 0.13 and 0.18 dB. That is the first replication
`cyc8` has ever had, and it is tight.

So the tally is **two runs with chokes (casc2, casc3), two without (cyc8, casc4)**, the choked
pair elevated by ~1.1-1.3 dB in the same 20 MHz, the unchoked pair agreeing to 0.18 dB.

**Build rule reversed: do not put ferrite chokes on cables inside the enclosure.** The earlier
advice that they "cost nothing" was wrong. Ferrite permeability collapses well below 1.7 GHz, so
at L-band a choke is not an absorber; it is a reactive discontinuity. The mechanism remains
untested - what is established is the effect and its size. Chokes on the *external* USB run are
untouched by this and can stay.

**The 14.4 MHz family statistic said +0.99 dB at 3.2 sigma here and that was an artefact.**
Excluding 700-800 MHz it falls to +0.41 dB at 1.9 sigma, not significant. The 720 MHz region
swung **+9.6 dB** between these two runs, as it has all along (+8.3 to +32.7 dB above the band
median across fourteen runs), and it drags any family mean that includes it. **Exclude 700-800
from every family statistic on this receiver**, or use the block table, which is unaffected.

### The jumper is innocent - and the ferrite chokes are the suspect (2026-10-18)

`casc3-replicate` removed the SMA jumper from the box entirely, put the 50 ohm load back
directly on the v5's SMA and left everything else as it was. Pre-registered: 1670-1690 MHz would
fall by ~0.8 dB if the jumper caused the rise.

**It did not fall.** 1670-1680 went **+0.43 dB** and 1680-1690 **+0.08 dB**. Whole-band median
moved +0.080 dB, inside the 0.1 dB gate registered beforehand, so the comparison is valid and the
answer is clean: **the jumper contributes nothing, and the 2026-10-17 stub refutation stands after
all.** The family statistic agrees - paired difference +0.10 dB, SE 0.40, n=70, 0.2 sigma.

**That leaves exactly one difference between `casc3-replicate` and `cyc8-usb6-white`: the ferrite
chokes on the USB extension.** Same cage, same lid, same dongle position, same cable, no jumper in
either, bulkhead bare in both. The only thing added is a choke at each end of the 6 in USB
extension.

| 10 MHz block | cyc8 (no chokes) | casc3 (chokes) | change |
|---|---|---|---|
| 1620-1630 | -36.80 | -36.80 | 0.00 |
| 1640-1650 | -36.48 | -36.41 | +0.07 |
| 1660-1670 | -36.89 | -36.72 | +0.17 |
| **1670-1680** | **-36.58** | **-35.36** | **+1.22** |
| **1680-1690** | **-36.08** | **-34.98** | **+1.10** |
| 1690-1700 | -36.80 | -36.69 | +0.11 |

Whole-band median agrees to **0.060 dB** and every block outside 1670-1690 is within 0.2 dB, so
this is not drift. And it replicates: `casc2-jumper`, the other choked run, sits +0.79 and
+1.02 dB above `cyc8` in the same two blocks. **Two choked runs, one unchoked, the same ~1 dB in
the same 20 MHz.**

**This inverts the standing advice.** The chokes were added on the reasoning that they cost
nothing and the one unchoked cable in the box had been the faulty one. Ferrite permeability
collapses well below 1.7 GHz, so at this frequency a choke is not an absorber - it is a reactive
discontinuity at each end of a 150 mm cable. Whether that is the mechanism is **untested, and a
mechanism that merely fits the frequency has been wrong five times here.**

**The decisive test is to take the two ferrites off the USB extension and change nothing else.**
If 1670-1690 drops ~1 dB, the chokes are confirmed and "ferrites on every segment" has to come out
of the build rules for this band. Until then, do not add ferrites inside the box as a default.

### Cascade step 2 (2026-10-18): the bulkhead costs nothing, but 1675 came back a little

`casc2-jumper` put the 50 ohm load on the **outside** of the SMA bulkhead and reconnected the
internal 6 in SMA jumper, so the signal path now runs bulkhead -> jumper -> dongle. Ferrite
chokes were also added to both ends of the replacement USB extension in the same step, so this
run carries two changes; the chokes are bounded by `cyc8-usb6-white`, which measured that cable
clean with no choke at all.

**Broadband: nothing.** Whole-band median -35.760 -> -35.750 dB, a change of **+0.010 dB**. The
bulkhead and the jumper add no measurable noise anywhere in 700-1700 MHz. That is the cascade
question answered for this step.

**1670-1690 MHz rose by about 0.8 dB**, and it is the only band that moved. Tracked across every
700-1700 run on this receiver (median of `median_db` in that block):

| Configuration | Runs | 1670-1690 MHz |
|---|---|---|
| cage off | baseline-term, cage-off-2, overnight, cyc1-off, cyc2-off | -36.1 to -36.6 |
| cage on, **faulty black cable** | cage-on-untaped, cyc1-on, cyc2-on, cyc3, cyc4, cyc5 | -29.2 to -31.8 |
| cage on, sound cable, no jumper | cyc6-usb12, cyc8-usb6-white | -36.9, -36.3 |
| cage on, sound cable, **+ jumper** | **casc2-jumper** | **-35.50** |

So the hump is back at roughly a sixth of its original size, and the peak has shifted up a block:
1670-1680 is +0.79 dB and 1680-1690 is +1.02 dB, where the cable's hump peaked at 1670-1680.

**This does not resurrect the dangling-stub hypothesis - it shows that test was underpowered.**
The 2026-10-17 stub removal measured "no change" while the faulty cable was contributing +5 dB in
the same band. An effect of 0.8 dB cannot be seen through noise six times its size. With the bad
cable gone the floor is finally quiet enough for it. **Unproven**: one cycle, handling in between,
0.8 dB against a 0.5 dB detection floor, and two things changed at once.

**`cyc8-usb6-white` had a one-off low-band pedestal, and it was not the jumper that removed it.**
700-750 MHz read **-18.52 dB** in that run against -28.0 to -31.5 in all thirteen others, with
750-800 and 900-1000 lifted to match - about 12 dB of excess across 300 MHz, present in exactly
one run and gone in the next. Compared against the open-bench baseline instead, `casc2-jumper` sits
+1.79 dB at 700-750, +0.74 at 750-800 and within 0.2 dB from 800 MHz up, i.e. normal. Do not read
the large negative numbers in a cyc8 -> casc2 comparison as the jumper doing anything; they are
that one run returning to baseline. Source of the transient unidentified.

**Refuted: the 720/1440 MHz see-saw.** The A1 -> B1 anomaly looked like one source alternating
between 720 and 1440 MHz. Across all fourteen runs the anti-correlation is weak (1440 averages
+6.6 dB when 720 is at 100% occupancy, +9.1 dB when it is at 0%, against several dB of scatter)
and `casc2-jumper` has **both at 100%**, which the hypothesis forbids. Dropped. Both lines are
simply unstable: 720 ranges +8.3 to +32.7 dB above the band median across runs, far beyond the
3.4 dB handling scatter, so neither is usable as a reference.

### RESOLVED 2026-10-18: the 1675 MHz hump was one defective USB cable

Everything below about the cage raising the noise floor is **superseded**. The 1630-1700 MHz
hump, chased for two days through five mechanism hypotheses, was the internal 6 in USB 2.0
extension - and specifically *that cable*, not its length.

**The test that settled it.** A different 6 in USB 2.0 extension, same length, same L-bend
routing, same position, everything else untouched:

| Band | original black 6 in | replacement 6 in |
|---|---|---|
| 1650-1660 | +3.74 dB | **-0.36 dB** |
| 1660-1670 | +3.46 dB | **-0.51 dB** |
| **1670-1680** | **+5.20 dB** | **-1.03 dB** |
| 1600-1700 overall | +2.31 dB | **+0.16 dB** |
| 700-1600 | +0.07 dB | +0.09 dB |

**So the cage is essentially neutral.** With a sound cable it changes the floor by under 0.2 dB
anywhere in 700-1700. The earlier "cage raises the floor by +0.5 dB" result was this cable's
resonance being counted as a cage effect, because the cable was only inside the box when the
cage was on.

**What this corrects, in order of how wrong it was:**

1. **The cable-length table is wrong.** A 12 in cable removed the hump and I read that as the
   resonance relocating with length. It was not - swapping to *any* sound cable removes it. The
   predicted "2 in clears the LS band" arithmetic was built on a false premise. **Do not design
   cable lengths around it.**
2. **The lid/base laminate patch (TM22 at 1683 MHz) was also wrong**, despite stripping the
   lining halving the hump. Removing interior copper reduced coupling to the bad cable, not a
   patch resonance. The dimensional match was coincidence, like the other four.
3. **"The cage raises the broadband floor" is withdrawn.** It raises it by +0.16 dB, which is
   inside the noise.

**The one thing that survived: the family statistic.** Bench to cage, with the good cable, the
14.4 MHz family mean moves **+1.20 dB (SE 0.55, n=70)** - marginally significant at 2.2 sigma,
one cycle, handling-dominated error. That is the *direction originally pre-registered* for a
working cage (harmonics unchanged, floor drops, so the ratio rises), but at this error bar it
needs the multi-cycle replication that was never finished. Treat it as unproven.

**The methodological lesson, now five for five.** Every mechanism hypothesis in this
investigation was proposed because a dimension matched the observed frequency: air-cavity modes,
the wall laminate, the dangling SMA stub, the dongle assembly length, the cable length. All five
were wrong. The hypothesis that held tested **component identity**, not geometry. In an
enclosure full of structures, something always matches your frequency - **a matching number
carries no information.** Only a prediction that can fail does.

**Practically:** the black 6 in USB 2.0 extension is a known-faulty part with a measured 5 dB
signature. Keep it labelled and out of the chain. It is also the only component that can
reproduce the effect, so do not discard it.

### The cage's 1675 MHz hump is its own lid and base (2026-10-17)

The cage raises the floor **only** between about 1630 and 1700 MHz, peaking at 1670-1680. Every
other 100 MHz block moves by less than 0.9 dB with no consistent sign. The whole-band median rise
is this one band dragging the average.

**Mechanism: the enclosure's large faces are dielectric-loaded patch resonators.** Copper tape
lines the inside, the outside wrap is the other plate, and the ~2.5 mm plastic between them is the
dielectric. For a rectangular patch,

    f(m,n) = 86.6 * sqrt((m/L)^2 + (n/W)^2)   GHz, L and W in mm, for plastic eps_r ~ 3

The lid and base are both **200 x 120 mm**, and their **TM22 mode lands at 1683 MHz** - within half
a percent of the measured peak. The side faces do not fit: 200x75 gives 1234 and 1738 MHz,
120x75 gives 1362 and 1849 MHz.

**Tested by stripping the lid's inside lining:**

| | lid lined | lid stripped |
|---|---|---|
| 1600-1700 floor rise | +3.48 dB | **+1.66 dB** |
| peak 1670-1680 | +7.46 dB | **+5.36 dB** |
| whole-band median rise | +0.880 dB | **+0.430 dB** |

**Half the effect removed, and the peak frequency did not move.**

**Correction:** the strip removed ALL inside copper from the lid AND the body - only a 5-10 mm edge
wrap remains for the mating faces. So the laminate patch accounts for about half the hump and
something else, at the same 1675 MHz, accounts for the rest. The base lining was never a candidate
for the remainder because it was already gone.

Air-cavity modes of the copper-bounded box (~205 x 125 x 80 mm) land at **1405 and 1893 MHz** -
straddling the peak, fitting nothing. The edge wrap is far too small; lambda/2 at 1675 MHz is
89.5 mm in air. Remaining candidates, untested: the cage coupling real external emissions in (the
lines that rise are genuine - 1679.99 MHz is 24 x 70, a clock harmonic), or a ~90 mm conductor
inside the box, which is roughly the length of the dongle with its terminator and connectors.

**Dangling SMA stub: REFUTED 2026-10-17.** A 6 in jumper left in the box, shorted at the bulkhead
and open at the far end, is a quarter-wave resonator whose 5*lambda/4 mode lands at 1624-1710 MHz
depending on velocity factor - an apparently perfect fit for the 1670-1680 peak. Removing it
changed nothing: 1650-1660 went +3.75 -> +3.74, 1660-1670 +3.49 -> +3.46, 1670-1680 +5.36 -> +5.20.

That makes **two refuted mechanism hypotheses whose dimensions matched beautifully** (air-cavity
modes, then the stub) plus one partially confirmed (the wall laminate, about half the effect). The
recurring error is treating a matching dimension as evidence: in a box full of structures, one of
them always lands on your frequency. The same trap as the thermal hypothesis, where 20 C predicted
exactly the observed +0.28 dB and was still wrong. **Only structural predictions - what must change
if the mechanism is real - have settled anything here.**

What is solid: the effect needs the dongle **inside the enclosure**, is confined to 1620-1700 MHz,
peaks at 1670-1680 at about +5 dB, leaves 700-1600 untouched (+0.07 dB), and survives stripping all
interior copper and removing the stub.

Next test: dongle in the **body with the lid off**. Hump vanishes = it needs a closed conductive
volume; hump persists = the body alone does it and this is coupling, not enclosure resonance.

Note the earlier air-filled cavity calculation was the wrong model - it gave a lowest mode of
1458 MHz with nothing in 1600-1700, and correctly ruled itself out. The resonator is not the box
interior, it is the wall laminate, where the plastic slows the wave by sqrt(eps_r).

**Bench repeatability during this series:** two cage-off blocks 3 h apart agreed to **+0.05 dB**
on the median and -0.14 dB on the family mean. The control is solid.

**The 14.4 MHz family mean is the wrong statistic for this question.** It gave -1.07 dB then
-0.46 dB across two cycles, with the harmonics moving -0.57 then +0.42 dB in absolute terms -
opposite directions. Individual spur levels reshuffle by ~3.4 dB whenever anything physical
changes (measured on the cable reroute, dongle untouched). The **whole-band median** is the
reliable instrument here: it repeats to 0.05 dB and moved +0.88 and +0.43 dB.

### The cage does something, and it is not what a cage is for (2026-10-17)

The **first properly controlled shielded-vs-unshielded test** this station has ever run. The
2026-10-14 cage result compared crude cardboard against a bonded cage, moved the notch in the
same rebuild, and ran its halves on different days - it concluded nothing. This one is A/B/A
bracketed, pre-registered, single-variable, v5, terminated, 1 h per block, one settings profile.

| Block | Tag | Whole-band median | 14.4 family (n=70) |
|---|---|---|---|
| A1 cage off | `baseline-term` | -35.890 | +4.92 |
| B1 cage on, seam untaped | `cage-on-untaped` | **-35.360** | **+3.78** |
| A2 cage off | `cage-off-2` | -35.840 | +4.78 |

**The control passes emphatically.** A1 vs A2, three hours apart with the dongle removed, caged
and removed again: median **+0.050 dB**, family mean **-0.14 dB (SE 0.41)**. Drift, room and
position repeatability are all inside the 0.5 dB detection floor, so B1's difference is real.

**The cage effect, bracketed:**

| | Full span | Excluding 1600-1700 MHz |
|---|---|---|
| Family mean | **-1.07 dB** (SE 0.23) | **-1.13 dB** (SE 0.21, n=63) |
| Whole-band median | **+0.505 dB** | **+0.275 dB** |

Decomposed: the clock harmonics fall about **0.86 dB in absolute terms** while the broadband
floor **rises** about 0.28 dB. Opposite directions, so this is not a gain shift.

**Read plainly: the cage attenuates the dongle's own radiated harmonics - which is the cage
working - while simultaneously raising the broadband noise floor.** The net effect on the family
statistic is negative. Both pre-registered directions were wrong, which is why they were written
down first.

This also sits against the station's earlier position that the clock family is purely on-PCB and
beyond the reach of shielding. That position came from three XTR interventions, one of which was
the confounded cage test. **A 0.86 dB drop says some of the harmonic energy reaching the tuner is
radiated, not conducted** - at least on the v5.

**Cavity resonance is ruled out.** The enclosure is 200 x 120 x 75 mm external, whose lowest mode
is 1458 MHz (1525 internal) with **zero modes in 1600-1700 MHz** where the floor rise is largest,
and essentially no change (-0.06 dB) in the band that does contain the one mode.

**Caveats.** One bracket, not a replication. A 240 MHz comb switched on between A1 and B1 - new
100% occupancy lines at 720, 960 and 1680 MHz - and 720 was still up in A2, so the room was not
static. The exclusion test shows the result does not depend on it. The printer sits inches from
the dongle and was powered but idle; it is the prime suspect and should be **unplugged, not
idled**, for the replication. B2 (seam taped) was not run: it would discriminate aperture
resonance from a volume effect, since taping should change the former and not the latter.

### Tool limit, not a station effect

- **rtl_power stops at 2147.483648 MHz (2^31 Hz)** — it holds frequencies in a signed
  32-bit int. Measured 2026-10-13: `-f 1241M:2187M:50k` covered only to 2147.483311 MHz
  (337 Hz short of 2^31), and a range *entirely* above the ceiling (2150M:2180M) wrote
  **no file while still exiting 0**. Requesting ~2148M gets the maximum. `run_survey.py`
  refuses a range above it and warns when a top would be clamped. For 2147–2187 MHz use
  SDR++ or rtl_power_fftw, not rtl_power.

---

## Existing code

**Packaged as `marsad` since 2026-10-20** (المرصد, al-marsad: the observatory; GitHub
kenogen-dev/marsad). The code lives in the `marsad/` package; `marsad run LS 1h` is the
command, and `sdr` is a second entry point for the same thing, kept so every command in the
notes and RUNSHEET still works. The `tools/*.py` scripts below are thin wrappers over the
package and keep their original command lines. `py -m pytest` runs 23 regression tests that
pin the recorded figures; the frozen Windows build is `packaging/marsad.spec` ->
`dist/marsad/marsad.exe`.

`tools/run_survey.py` — records a survey from a preset, then analyses it:

```
py tools/run_survey.py <preset> <duration> [--tag x] [--gain N] [--integration N]
                       [--freq LO:HI:BIN] [--conditions "..."] [--threshold N]
                       [--skip-warmup MIN] [--floor-window MHZ]
                       [--chain "src > ... > PC"] [--crop F] [--fir N]
                       [--dry-run] [--no-analyze] [--non-interactive] [--list]
```

`scan_geometry()` reproduces rtl_power's own arithmetic exactly, so the pre-run summary is not an
estimate: rtl_power covers the span with as few hops as it can (sample rate capped at 2.8 MS/s),
*lowers* the sample rate so those hops tile the span exactly, then picks the smallest power-of-2
FFT no wider than the requested bin. Verified against three runs; it predicts LS's first hop
centre at 1242.049 MHz, which is exactly the frequency rtl_power reported as "PLL not locked".

`--skip-warmup MIN` converts minutes to whole sweeps and passes `--skip-sweeps` to the analyser,
so the warm-up discard stays constant in wall-clock terms when `-i` changes. `--crop`/`--fir`
exist for completeness and print a warning: changing either breaks comparability with every other
survey. The `custom` preset exists so a hand-built range is recorded as `custom` rather than
masquerading as a preset.

`--chain` records the physical signal chain in the sidecar as `chain_order`, remembers it in
station.json, prints it in scan_build.md and is compared by `survey.py` as a station field. Use
it: two runs were mislabelled because station.json still held the previous run's chain text, and
`archive.py`'s terminated flag guessed wrong off it. A run should describe its own rig.

Every run writes `reports/<name>/scan_build.md`: the command, what was requested, the derived
geometry including hop centres, the dwell split, the station chain, the conditions, the analysis
command, and a one-line **comparability fingerprint**. Two scans can be compared directly only if
their fingerprints match.

Refuses to start if another program holds the SDR; validates `-g` against the E4000
gain steps; names the CSV per the convention; prompts for antenna/chain/conditions
(remembering answers in `station.json`); writes the JSON sidecar before recording so
an interrupted run still has one; then runs `survey.py analyze`. Size estimates are a
range because rtl_power rounds bin width down to a power-of-2 FFT, giving up to twice
the requested bins — the sidecar records the actual width as `bin_khz_actual`.

`tools/scan_builder.html` - interactive scan builder, published as an Artifact at
https://claude.ai/artifact/E3RgHzawowNVfdH7jRtm4g . Redeploy by publishing that file to the same
URL. It is the intended way to compose a scan:

- Dual-thumb slider on the tuner range, sitting on the frequency map's top axis. The map is three
  stacked strips, each a zoom into the one above (tuner -> scan -> one hop with its bins, crop
  zones and DC spike), because one bin is ~1/20000 of a full sweep and cannot be drawn to scale.
- Settings split by whether they **lock comparability** (range, bin, gain, integration, crop,
  FIR - with the live fingerprint shown) or are **free** (duration, tag, device, analysis, run
  options). "What rtl_power will do" sits under both so the effect of a change is visible.
- A **signal chain builder**: modules in order, checkbox per module, reorder, add custom ones,
  and an antenna / 50 ohm load toggle on the source. Emits `--chain`.
- A **conditions** panel (temp, humidity, inside/outside, sky, notes) that composes
  `--conditions`.
- Live facts for whatever span is selected, drawn from this file's own findings.
- Warns on the 2^31 ceiling, the PLL gap, a sub-retune-floor `-i`, a hop centre landing on a
  target frequency, non-standard crop/FIR, and a terminated run whose tag does not say so.

`tools/spur_stats.py` - the pre-registered statistic for comparing configurations: mean level
of a harmonic family above the whole-band median, and for two runs the **paired** difference
with its standard error. Pairing cancels the large line-to-line spread and leaves only what
changed. Validated against the USB bulkhead repair: reproduces -0.14 dB, std 1.36, n=31,
SE 0.24 exactly. Use this, not eyeballed single lines, for any before/after claim.
See `BASELINE_PROTOCOL.md` for the measurement programme it serves.

**Bug found and fixed 2026-10-18 the same day it was written:** bin width was taken as
`freqs[1] - freqs[0]`, which after `--band` or `--exclude` can be the width of the *gap* the
filter left rather than of a bin. That inflated the harmonic tolerance to tens of MHz, so every
harmonic matched something and `--exclude` silently returned the unfiltered n. It now uses the
median spacing between adjacent bins, which no number of gaps can fool. Unfiltered comparisons
were never affected and the A1/A2 regression still reproduces exactly.

`tools/compare_runs.py` - the whole A/B analysis in one command, which is what every block of
this programme has needed by hand. Takes two report dirs, two bins.csv paths, or just two survey
tags, and prints: the comparability fingerprint for both (refusing to pretend a mismatch is a
comparison), each run's chain and conditions, the whole-band median shift, the floor block by
block with every block at or beyond the detection floor starred, the harmonic-family statistic
with its paired difference and a verdict, and optionally the autocorrelation of each run with hop
multiples flagged as instrument artefacts. `--handling handled|untouched` picks which measured
detection floor to judge against (0.5 dB if the hardware was touched between runs, 0.05 dB if it
was not) - that is a fact about the experiment, so the tool makes you state it. Validated: it
reproduces the recorded A1/A2 control exactly (+0.050 dB median, -0.14 dB family, SE 0.41, n=70).

```
py tools/compare_runs.py <before> <after> [--family 14.4] [--block 100]
    [--band LO HI] [--exclude LO HI] [--handling handled|untouched]
    [--periodicity] [--verbose]
```

`tools/drift.py` - thermal / time drift, from the **per-sweep whole-band median**. You cannot
measure an enclosure's temperature with an SDR and do not need to: what matters is whether the
receiver's output moves as it warms, and a thermal gain shift moves the whole floor together,
which a median over every bin in one sweep detects. The whole-band median repeats to 0.03-0.05 dB
here, so it is the most sensitive instrument the station has.

Reads the **raw CSV** and never skips warm-up sweeps - `--skip-warmup` exists to keep drift *out*
of a comparison, this tool exists to look straight at it. Reports first/last sweep, total change
including warm-up, the sweep from which the remainder stays inside the budget, and the settled
mean, std, range and net drift, with a pass/fail against `--threshold` (default 0.1 dB). A FAIL
distinguishes *trending* (still climbing at the end - an inadequate heat path) from *wandering*
(more likely the room or the PC).

Validated against both recorded figures: the 6 h terminated run settles by sweep 4 (~6 min) with
total change -0.35 dB and settled std 0.025 dB; `casc4-noferrite` settles by sweep 6 of 15.

```
py tools/drift.py <tag-or-csv> [--threshold 0.1] [--plot-text]
```

`tools/adsb_check.py` - 1090 MHz ADS-B positive control. Fixed gain, counts frames and
distinct ICAO, prints pass/weak/fail. The sensitivity half of a characterisation, which a
terminated run cannot provide. Compare chains on the DF17 count, not the ICAO count.

`tools/archive.py` - long-term occupancy archive in `occupancy.db`. Stdlib only.

```
py tools/archive.py add --all | add reports/<name> [--replace]
py tools/archive.py list
py tools/archive.py new [survey] [--last 5] [--min-occ N] [--min-snr N] [--no-spurs]
py tools/archive.py trend <MHz> [--tol-khz 100]
py tools/archive.py band <lo> <hi>
py tools/archive.py seen [--min 2]
py tools/archive.py mark <survey> --terminated | --antenna
```

Three tables: `surveys` (one per recording, with the fingerprint), `signals` (peaks.csv rows),
`bands` (bins.csv pooled into 1 MHz buckets). The pooling is the point: rtl_power picks its bin
width from the span, so a 100 MHz tile and a 907 MHz sweep have different grids and their bins
do not line up - detections are points and compare fine, bins do not.

`new` compares only against surveys with the **same fingerprint** that cover the frequency, and
never against terminated runs. It warns when the survey is much longer than its comparison set,
because a longer run hears more of everything and every rare burst then looks new - filter with
`--min-occ`. The terminated flag is guessed from the sidecar text and is often wrong, because
station.json carries the previous run's wording; `mark` is the record of fact.

### Comparing runs: pick the right metric (2026-10-16)

**Max-hold grows with sweep count by construction** - it is extreme-value statistics, so the more
often you look the higher the peak you happen to catch. Measured on 1799.98 MHz, a steady external
carrier, same antenna, same station: max-hold minus median-over-sweeps was **0.15 dB at 22 sweeps
and 1.18 dB at 472**. Detection counts inflate the same way - the 16 h survey reports 147 signals
at 472 sweeps and **28** when truncated to its first 22.

| Signal type | Use | Why |
|---|---|---|
| steady | **median over sweeps** (`snr_median_db` in peaks.csv) | immune to sweep count |
| intermittent | **occupancy %** | a fraction, so also immune |
| anything | max-hold, **within one run only** | never across runs of different length |

`survey.py --max-sweeps N` truncates a run to its first N sweeps, so a long recording can be
compared fairly with a short one without re-recording. The report states when it has been applied.

Duration and sweeps are the same knob: `sweeps = duration / integration`, exactly. Specifying one
rather than the other changes nothing, as long as integration is held constant - which the
fingerprint already enforces. The scan builder offers both fields as a convenience.

### Whip length: 60 mm is the measured optimum (2026-10-16)

Four lengths, all 1 h LS runs at `-i 120`, 22-30 sweeps, **same chain**, judged on the absolute
level of 1799.98 MHz (median over sweeps, no normalisation - the floor moves, so normalised
numbers mislead):

| Whips (each) | Total | 1799.98 absolute | vs 60 mm |
|---|---|---|---|
| 50 mm | 100 mm | -20.66 dB | -4.0 |
| **60 mm** | **120 mm** | **-16.6 to -16.7 dB** | **peak** |
| 75 mm | 150 mm | -17.62 dB | -0.9 |
| 100 mm | 200 mm | -24.46 dB | -7.8 |

60 mm measured four times across different hours and days, spread **0.33 dB**. The 75 mm run at
10:32-11:33 brackets exactly between the 16 h run's 10:00 and 11:00 blocks, so that comparison is
hour-matched and chain-matched. A plateau from 60 to 75, sharp falloff either side.

**Keep the standing 60 mm config.** It was right from the start; now it is measured.

Secondary evidence, same direction: the floor and the internal 1612.79 spur both *rise* with
longer whips (-27.7 dB at 60 mm, -26.0 at 100 mm). As the antenna approaches anti-resonance the
match degrades, more internally generated noise reflects back into the front end, and less
external signal gets through.

**Method warning.** An earlier "~75 mm" run (2026-10-13 11:55) read -14.10 dB, better than
everything, and that bad point drove a wrong prediction that longer would keep improving. It had
a **different chain** - cardboard-box shielding, filter after the amp, no isolator - and its whip
length was a retrospective guess, never measured. `compare` reports chain differences as station
changes for exactly this reason. Run points through the check instead of eyeballing the number.

Quarter-wave arithmetic is still the wrong model for *why*: resonance governs impedance match,
which matters less with an LNA at the feedpoint, and a dipole's behaviour past half-wave is about
pattern and anti-resonance rather than resonance. But the prediction it made here (shorter is
better-centred) and the prediction aperture reasoning made (longer is better) were **both wrong**.
The measurement is the authority.

### Deep tiles beat wide sweeps for investigating

Measured, 2 h each:

| | hops | bin | listening per frequency | sweeps | occupancy step | CSV |
|---|---|---|---|---|---|---|
| full LS, `-i 120` | 432 | 43.72 kHz | 13.8 s | 60 | 1.67% | 12 MB |
| 100 MHz tile, `-i 30` | 49 | 21.47 kHz | **135.7 s** | 240 | **0.42%** | 10 MB |

Ten times the dwell, twice the frequency resolution, four times the sweeps, for less disk. Use
the `deep` preset and `--freq` to pick the tile; nine tiles of ~101 MHz cover 1241-2147.48
(1241, 1342, 1443, 1544, 1645, 1746, 1847, 1948, 2049). Tiles are internally comparable and each
repeat builds a time series in the archive; tiles are *not* comparable with each other or with a
full sweep, which is what the fingerprint is for.

`tools/survey.py` (already written; keep its CLI stable):

```
py tools/survey.py analyze surveys/<file>.csv [--threshold 10] [--receiver xtr] [--mask 1095-1240] [--floor-window 2] [--skip-sweeps N] [--max-sweeps N] [--out reports]
py tools/survey.py compare before.csv after.csv [same options]
```

- Parses rtl_power CSV (`date, time, Hz low, Hz high, Hz step, samples, dB...`), groups hops into sweeps, builds a time × frequency grid.
- Noise floor = 20th percentile over time, then rolling median over frequency. Signals = max-hold peaks ≥ threshold above floor. Occupancy = % of sweeps above threshold. Types: steady (≥90%), intermittent (5–90%), rare burst (<5%).
- Tags spurs using `SPUR_FAMILIES_MHZ` and hop centres; masks gaps via `RECEIVER_GAPS_MHZ`.
- Outputs per survey: `report.md`, `heatmap.png`, `spectrum.png`, `occupancy.png`, `peaks.csv`, `bins.csv`; compare adds `compare.png`, `compare.md`, `compare_signals.csv`.
- Levels are relative dB, not calibrated dBm.

---

## Survey conventions

- Standard command shape: `rtl_power -f LO:HI:BIN -g <fixed gain> -i <sec> -c 0.25 -F 9 -e <duration> <file>.csv`
  - Always a **fixed** gain (never auto), always `-c 0.25` and `-F 9`.
- File naming: `survey_<band>_<duration>_<YYYYMMDD-HHMM>.csv`, e.g. `survey_LS_6h_20261013-0100.csv`.
- Each survey gets a sidecar `<same name>.json` with: command line, gain, bin, integration, crop, receiver, antenna shape + whip length, LNA/filter in chain, and free-text conditions (weather, what's switched on in the room).
- Compare surveys only when gain, bin, crop and integration match. `compare` should warn if sidecars differ.
- Long runs: Windows sleep and USB selective suspend must be off.

### Dwell time: retuning dominates short integrations

Measured 2026-10-13 on this station: **retuning costs ~47-48 ms per hop**, constant across
50, 200 and 432 hops (floor sweep times 2.40 s / 9.67 s / 20.33 s). The sample buffer itself
is only 2.93 ms, so the rest is PLL settling, USB round-trip and buffer resync.

LS has 432 hops, so its physical floor is **20.3 s per sweep** - never set `-i` below ~25 on LS
(rtl_power's own help: "buggy if a full sweep takes longer than the interval").

Listening fraction = 1 - (47 ms / (interval / hops)):

| `-i` | ms/hop | listening | sweeps in 1 h | total listen per frequency |
|---|---|---|---|---|
| 30 | 69 | 32% | 120 | ~2.7 s |
| 60 | 139 | 66% | 60 | ~5.5 s |
| 120 | 278 | 83% | 30 | ~6.9 s |
| 180 | 417 | 89% | 20 | ~7.4 s |

Two tiers, each internally comparable: **short checks (5-10 min) at `-i 30`** (keeps 10-20 sweeps
for a usable floor percentile), **long surveys (1 h+) at `-i 120`** (83% listening and still 30+
sweeps). Gains flatten past 120 while sweep count keeps halving, and sweeps are what occupancy
percentages and the 20th-percentile floor depend on.

Ready-made ranges:

| Name | Range / bin | Notes |
|---|---|---|
| LS | 1241M:2148M:50k, -g 29, -i 30 | Gap top (+1) to rtl_power's 2147.48 MHz ceiling; first 6 h run |
| L | 1241M:1700M:25k, -g 34, -i 10 | L-band detail |
| milair | 225M:400M:12.5k, -g 29, -i 10 | Military air UHF |
| 900 | 902M:935M:5k, -g 29, -i 5 | ISM + paging |
| wide | 52M:1700M:100k, -g 29, -i 5 | Overview; analyse with `--receiver xtr` |
| burst23 | 1263M:1267M:5k, -g 29, -i 2 | 1265.44 MHz burst hunt; 2 hops, ~95% listening, 2 s resolution, hop centres offset off-target |

---

## First tasks

1. **Done** (2026-10-13). Folder layout, `.venv`, `requirements.txt`; `survey.py` moved to `tools/`;
   x64 RTL-SDR executables copied into `rtl-sdr/` (the Downloads copy was left in place).
2. **Done** (2026-10-13). `rtl_test -t`: Nooelec SMArTee XTR v5ee, SN 08701076, Elonics E4000,
   all 14 gain steps as documented, no errors. Measured 52-2187 MHz with the PLL gap at
   1095-1240 MHz, so `RECEIVER_GAPS_MHZ` and the LS/L presets were corrected to match.
3. **Done** (2026-10-13). `tools/run_survey.py`, see above.
4. **Done** (2026-10-13). `survey.py` reads the sidecar into a "Survey settings" section of
   `report.md`; `compare` warns on screen and in `compare.md`, separating settings mismatches
   (which invalidate the comparison) from station changes (usually the point of it). Bin width
   is cross-checked from the CSVs themselves, so that check works without sidecars.
5. Help run the first 6-hour LS survey and summarise the report.

## Later roadmap (don't start unless asked)

- rtl_433 config/logging for 915 MHz sensors.
- URH workflow for bursts found in surveys (own devices only).
- SatDump for Meteor-M LRPT (137.9 MHz; needs a 137 MHz V-dipole), later GOES/HRPT with a dish.
- `rtl_power_fftw` adapter in `survey.py` for hydrogen-line (1420.4 MHz) work on the v5.
- Long-term occupancy: append each survey's `peaks.csv` / `bins.csv` to a SQLite database; trend plots.
- **Run the station without Claude in the loop** (pinned 2026-10-17, not started). The goal is
  independence from Claude for routine operation, NOT distribution - so the priority is promoting
  the throwaway analysis scripts into permanent commands, not packaging. Highest value first:
  a single `compare_runs.py` that takes two bins.csv and prints both registered statistics with
  error bars, floor by band, a significance verdict against the 0.5 dB detection floor, and any
  periodicity found - i.e. everything done by hand for A1 vs B1. Then an autocorrelation/comb
  detector and an odd/even half-harmonic test, both of which have been written as throwaways
  three times. About half a day. The test for whether something belongs here: *would I otherwise
  have had to ask Claude to do it?*
- **Package as a distributable project** (lower priority, only if sharing it ever matters). Surveyed: ~2,965
  lines of Python, only **54 station/platform-specific lines** and **19 config declarations** to
  extract. `spur_stats.py` and `live_view.py` are already portable and stdlib-only; only
  `survey.py` needs numpy/pandas/matplotlib. Work, in order: (1) split universal rtl_power facts
  from station facts - the 2^31 ceiling and the 2.8 MS/s cap and the E4000/R820T gain ladders are
  universal, while the PLL gap, the spur families and the presets are ours, and **`RETUNE_MS_PER_HOP
  = 47.0` was measured on this USB stack and a shipped tool should measure it, not assume it**;
  (2) find rtl_power on PATH instead of `ROOT/rtl-sdr/*.exe`; (3) POSIX branches for the
  Ctrl-Break shutdown and the `tasklist` SDR-in-use check; (4) one CLI with a `pyproject.toml`
  entry point; (5) scan_builder emits the packaged command; (6) regression tests - `scan_geometry`
  and `spur_stats` both have known-good answers to pin. Roughly one to two days.
  **Do not start it mid-programme**: the fingerprint system assumes the tools behave identically
  across a comparison set, so reshaping run_survey.py between blocks would put a variable inside
  the experiment. The novel part is the measurement discipline, not the plotting.

---

## Ground rules

- Receive-only hobby station. **Do not decode or store the content of private communications** (pager messages, cellular, cordless phones); identifying signal types, frequencies and occupancy is fine.
- URH/replay-style analysis only on devices the owner owns.
- Don't install system-wide software or change Windows settings without asking.
