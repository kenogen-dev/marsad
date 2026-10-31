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
| XTR range | **52–2187 MHz** and **gap 1095–1240 MHz**, both measured on this unit (SN 08701076) with `rtl_test -t` on 2026-10-13 - **cold figures**. Warm after an hour of streaming (2026-10-23): **52–2170, gap 1086–1230**; the whole PLL range slides down as it warms, see "Block H3". Nominal spec is ~52–2200 (some units ~2300) with a ~1105–1268 gap; the E4000 PLL varies per unit, so trust the measured figures. |
| XTR gains (dB) | -1, 1.5, 4, 6.5, 9, 11.5, 14, 16.5, 19, 21.5, 24, 29, 34, 42. Use these exact values with `-g`. |
| XTR bias-tee | **Always on** (hardware); SDR++ "Bias T" checkbox and `rtl_biast` have no effect. |
| Second receiver | Nooelec **NESDR SMArt v5** (R860 tuner, low-IF), 24–1766 MHz, no gap, HF direct sampling 0.1–25 MHz. Cleaner and more sensitive above ~700 MHz; planned as sniffer / L-band (1.3–1.7 GHz) receiver. |
| v5 bias-tee | **None.** Measured 2026-10-16: 0.1 mV at the SMA centre pin. It is a SMArt, not a SMArTee, so a plain DC-coupled 50 ohm load is safe directly on its SMA. The XTR still needs a DC block. |
| LNA | Nooelec **LaNA** (20–4000 MHz, ~16–25 dB gain, NF 0.9–1.6 dB), powered by the XTR's bias-tee, mounted at the antenna pigtail (immediately after the notch) and supported at the window so no weight hangs on the antenna's SMA. |
| Filter | Nooelec **Flamingo+ FM v2** notch (blocks 88–108 MHz; passes DC). Since 2026-10-14 it sits **at the antenna, ahead of the LaNA**, so broadcast FM is rejected before the amplifier instead of after it. |
| Antenna | Window suction-cup dual-whip dipole, whips adjustable ~50–125 mm each. **Standing config: 90° L, 60 mm each whip** (confirmed 2026-10-13) — quarter-wave at ~1250 MHz, i.e. near-resonant at the bottom of the LS band and electrically short toward 2147 MHz. Whip length in sidecars is **per whip**, not tip-to-tip. |
| Shielding | SDR sealed in a copper-taped plastic enclosure (built 2026-10-14): lid/body seam covered with conductive-adhesive copper tape, SMA and USB bulkheads bonded to the cage, dongle thermal-padded to a wall with perforated foam holding it against the pad. Ferrite chokes on the external cable segments only - **not inside the box**, measured 2026-10-18 to cost ~1.3 dB at 1670-1690 MHz. |

**One web address, always: http://127.0.0.1:8731/.** The Marsad web app (`marsad live`,
Scan / Builder / Archive / Chain / Probe / Roadmap) runs on port 8731 and nowhere else. It
follows the newest CSV on its own, `marsad run` reuses it, and since 2026-10-20 a second copy
cannot bind a port that is in use. **Any Claude session working in this folder:** the owner only ever uses 8731. A session may
start a test viewer on another port (8740-8749) so its work does not disrupt the owner's, but it
must start it with `--exit-after-idle 5` and **stop it when the test is done**, and it must say
so in its report. Never leave one running. Never start a second viewer on 8731 while one is
up; to put a page change in front of the owner, run **`marsad web restart`** (stops the listener
on 8731, starts it again in the background, confirms with a GET; refuses while a Probe-tab run
is recording, unless `--force`). **8731 never gets an idle timeout** - since 2026-10-27 it runs
in the background with no window, so it is there between scans and closing a PowerShell window
cannot take it down; `marsad run` and the launcher start it if it is not up. Stray viewers
on 8740/8741 left by session testing have twice had the owner looking at a stale page.

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

Analysis scripts were throwaway; the recording is in the SDR++ recordings folder, not this repo:
`C:\mydata\projects\sig_init\SDRplus\recordings\baseband_1265440000Hz_00-32-45_15-10-2026.wav`.

**Corrected 2026-10-27 by `marsad characterise` on that file** (RECORDER_DESIGN phase 1): rotation
12.000 s (9 passes, 1 ms scatter), PRIs 2744.5 / 3156.9 / 3472.7 us, pulse width 111.0 us at half
amplitude (107.3 at half power, 113.1 at -10 dB; the hand figure 113.8 sits between). **The chirp
is not linear**: S-shaped, sweeping ~3.5x faster at the ends of the pulse than in the middle,
-763 -> +853 kHz end to end (a straight-line fit gives -378 -> +649). **Each PRI repeats x5, not
x4** (the fifth ~2 us longer). The radar is also heard for ~3 s twice per rotation outside the
main beam, and an unexplained group of 275 us intervals may be an echo (untested).

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

### Noise generator characterisation of the XTR chain (2026-10-20/21): LaNA 25 -> 18 dB, coax under 1 dB, notch 15 dB and wider than FM

The first measurements of what each part of the chain *does to a signal*, as opposed to what it
adds to the floor. A broadband noise source ("Noise Source V2.1, 0.2-2000 MHz", 9 V barrel,
heatsink fitted) takes the antenna's place; each run removes one part, and the difference between
two runs is that part's response at every frequency. All runs `wide` (52-1660 MHz, 87 kHz bins),
5 min, 4 sweeps with 1 discarded, the dongle untouched throughout; only the outside of the cage
bulkhead and the window stack were handled.

**The generator is ~40 dB above the receiver's ceiling.** Through a 20 dB pad at g29 it read -1 dB
with 3044 bins at full scale; through 36 dB at g29 it read +1 to +3.6 with 8590 bins clipped,
i.e. *more* clipped than the first run showed. Working back from the clean runs the raw source
sits near +40 dB on rtl_power's scale at g29. Treat the listing's "flat, gentle" as relative to
a spectrum analyser. **Every characterisation run needs 36-39 dB of pad and the tuner at g14 or
g-1**, and the LaNA runs cannot be kept under the ceiling at any gain above -1.

**Bonus from the clipping: the 29 -> 14 gain step is nearly nominal.** `nsrc-D29` (clipped) vs
`nsrc-D14` (clean, plateau -10 to -12 below the gap): the plateau moved **13.0-14.4 dB, 13.5
overall**, for a nominal 15, flat across the range. So the E4000's lower steps deliver; it is only
the top step (29 -> 42, measured ~4 dB on 2026-10-19) that is hollow.

| Run | Gain | Pad | Where the source went | Plateau below / above the gap |
|---|---|---|---|---|
| `nsrc-Dm1-xtr-direct` | -1 | 36 | cage bulkhead, 3 ft coax off | -24.7 / -32 |
| `nsrc-Fm1-coax` | -1 | 36 | window end of the 3 ft coax | -25.3 / -32.7 |
| `nsrc-Em1-lana-rerun` | -1 | 39 | LaNA input, notch off | -4 / -17.8 |
| `nsrc-Am1-notch` | -1 | 39 | notch input, full stack | -5 / -18.5 |

(`nsrc-Em1-lana` 02:17 is **invalid**: a coax joint was found undone afterwards, 30 dB low; marked
in its sidecar.) Above the gap the direct and coax runs sit only ~6 dB over the receiver's -38
floor, so F-D is squeezed toward zero there; below the gap there is 13 dB of room.

**3 ft RG316 coax, F minus D:** -0.2 dB at 52-100 MHz rising smoothly to -0.9 at 700-900 and
-0.7 at 900-1095; scatter 0.37 dB bin to bin, no ripple. Textbook for a yard of RG316.
Prediction (-0.5 to -1 below 1 GHz) held.

**LaNA gain, E minus F plus the 3 dB extra pad:**

| Band | gain |
|---|---|
| 52-100 | +25.5 |
| 100-300 | +25 |
| 300-500 | +23.8 |
| 500-700 | +22.2 |
| 700-900 | +20.5 |
| 900-1095 | +18.7 |
| 1240-1660 | **+17.7, flat** |

The datasheet's 16-25 dB, measured on this unit: 25 at VHF sliding to 18 at L-band and flat from
1.24 to 1.66 GHz. **The amplifier is fine.** The cascade's 0.64 dB was the digital floor hiding a
working 18 dB amplifier, exactly as the gain ladder said. What it does past 1.66 GHz is the LS-range
repeat of D and E (not yet run), which is the 2 GHz tuner-vs-antenna answer.

**Flamingo+ notch, A minus E:** passband loss **-1.1 to -1.4 dB** from 150 MHz to 1.1 GHz (-0.6
to -0.8 above the gap, floor-squeezed). The stop band is **wider than 88-108**: -3 dB points at
**73.8 and 126.1 MHz**, -13 dB already at 80-85, -7.9 dB at 112-120. So it takes 5-8 dB off the
bottom of the aviation band (118-120 MHz) and 3 dB off 74-80 MHz, which matters if either is
ever a target.

**LS range (2026-10-21, 08:22-08:50): the LaNA holds 16-18 dB to 2.147 GHz; the receiver itself
is ~11 dB less sensitive at 2.1 GHz than at 1.3.** `nsrc-Dm1-LS` at g-1 fell on the floor
(plateau -36 to -38, nothing to subtract) and stands as a floor measurement only; the pair was
rerun at g14: `nsrc-D14-LS` (36 dB pad, bulkhead) and `nsrc-E14-LS` (39 dB pad, LaNA input,
notch off), 5 min each, 8 sweeps with 2 discarded, no bin near the ceiling (E max -7.2).

| Block | D14 plateau | E14 plateau | E - D + 3 (LaNA gain less ~1 dB coax) |
|---|---|---|---|
| 1241-1341 | -26.3 | -11.0 | +18.3 |
| 1441-1541 | -28.2 | -13.2 | +17.9 |
| 1641-1741 | -28.8 | -15.6 | +16.2 |
| 1741-1841 | -30.0 | -17.3 | +15.7 |
| 1841-1941 | -34.2 | -20.2 | +17.0 |
| 1941-2041 | -37.0 | -21.8 | +18.0 |
| 2041-2147 | -37.5 to -37.7 | -22.4 to -22.7 | +17.9 |

Above 1841 MHz the D plateau is at the receiver's floor, so the true direct level is lower and
the gain figures there are **lower bounds**. Either way: **the amplifier delivers at least 16-18
dB all the way to the top of the tuner's range. The LaNA is not the 2 GHz limit.**

What the D run shows instead: with the same generator on its input, the receiver reports **11 dB
less at 2.1 GHz than at 1.3 GHz** (-26 to -37.7, a smooth slide with a step at 1841). Part of that
may be the generator rolling off (its listing promises flatness only to 1.5 GHz), part is the
E4000's own front end weakening toward the top of its range. The two cannot be separated with
this source. Together with the whip's 0.84-wavelength mismatch at 2.1 GHz, that is the whole
"weak at 2 GHz" picture: a receiver down several dB, an amplifier that is fine, and an antenna of
unknown match. **The antenna is the only one of the three that can be changed**, so the UWB
antenna on the 2049-2147 tile against the whip's +5-6 dB plateau is the right next test, and the
return-loss bridge on the whip and on any home-built element is the one after it.

**The notch's depth is 15 dB, and the leakage check says that is real.** Inside 80-112 MHz
the A plateau is **-17.8 to -18.2 dB, flat to 0.4 dB across 32 MHz**, 17 dB above the chain's
floor, so it is not floor-limited. A flat shelf looked like a leakage floor, so it was tested
(`nsrc-leakcheck`, 2 min, g-1): generator stack powered but screwed to nothing, dangling where it
had been, LaNA input on the 50 ohm load through the DC block. **The FM band sat at -34.9 dB, the
floor, against -17.9 in the notch run.** Nothing radiates around the stack into the LaNA at a
level that matters, so the shelf is the filter's own ultimate rejection at this drive level, not
our set-up. **Use 15 dB as the Flamingo+'s rejection**, consistent with the floor-limited
"at least 16-18 dB" seen on the v5 on 2026-10-19. The flatness is then a property of the filter,
whose stop band bottoms out rather than following a curve. Incidental: the chain's floor at g-1
with the LaNA terminated is -35.0 dB, 2.5 dB *above* the g29 terminated figure; the tuner's
minimum-gain setting is not simply "less of the same" and should not be used for surveys.

### Standing chain restored after Block F (2026-10-21, 09:27): the gain path is unchanged to 0.04 dB

`chain-restored-check`, 30 min LS at g29 with the generator stack and pads removed and the
antenna back on the notch, against `window-A2-inside` (same fingerprint, 10 sweeps each).
Judged on the base-station block, which is always on and does not care about the hour:

| Block | A2 (2026-10-20 10:20) | restored (09:27) | change |
|---|---|---|---|
| 1941-2041 (PCS downlink) | -34.20 | -34.16 | **+0.04** |
| 2041-2141 (digital floor) | -37.94 | -37.79 | +0.15 |
| 1841-1941 | -37.64 | -36.94 | +0.70 |
| 1541-1841 | -36.96 to -37.42 | -36.20 to -36.90 | +0.5 to +0.85 |
| 1341-1541 | -33.58 to -34.67 | -31.98 to -33.11 | +1.6 |
| 1241-1341 | -31.36 | -27.64 | **+3.72** |
| 14.4 MHz family, paired, n=63 | +2.60 | +2.94 | +0.35, SE 0.18, not significant |

**The chain is back.** A loose joint, a missing part or a pad left in would show on the downlink
block first, and it moved 0.04 dB. The low blocks are higher, but uniformly: every 10 MHz slice
of 1241-1341 is up 3.5 +/- 0.3 dB with the same 1291-1321 hump as A2 and no new detection,
and the lift tapers smoothly to nothing by 1.9 GHz. That is the shape antenna-on received noise
had in cascade step 5 (+4.5, +3.5, +1.1, +0.5, 0.0), scaled up: the environment at 1.2-1.5 GHz
was louder this morning than during A2, as the 16 h diurnal survey says it can be. A remount of
the antenna in a slightly better spot would look the same and cannot be excluded; the next LS
run on this chain settles which. Nothing in it needs a repeat before the antenna work starts.

### Antenna outside the window, A/B/A (2026-10-20, 07:28-11:00): worth 3-5 dB, and the glass is not low-E

Block E. Three 30 min blocks at the LS fingerprint, g29, 10 analysed sweeps each after a 10 min
discard: `window-A1-inside` 07:28, `window-B2-outside` ~09:30, `window-A2-inside` ~10:30.
Whips 60 mm throughout, 90 deg L (mirror-flipped outside, so still one vertical and one
horizontal element), notch and LaNA on the sill in both positions. Outside needed a **3 ft coax
extension ahead of the notch**, about 1 dB of pre-amplifier loss, recorded before the run. A
first outside block (`window-B-outside`, 08:26) is contaminated: the suction cup let go on dusty
glass part way through and the antenna slid down the window; its whole-sweep level wandered
0.8 dB and it is superseded by B2, which held to 0.08 dB.

**The reference carrier failed as a yardstick.** 1799.98 MHz read -13.1 dB on 2026-10-19
evening, -18.8 during A1, -31.1 during B and -34.9 during B2, and it fell 3 dB *within* A1 with
nothing touched. After a week at -16.6 +/- 0.3 the emitter itself has faded; it is out of this
comparison and should not be used as a reference until it returns. Use the PCS downlink block
median (1930-1990 MHz, base stations, always on) and the 1241-1341 block instead.

**The control is the interesting half.** A2 came in 0.7-1.7 dB *below* A1 in every block from
1241 to 1841 MHz and within 0.4 dB above 1841: the morning quietening the 16 h diurnal survey
recorded (peak at 08:00, falling through late morning). So the hour was working *against* the
outside result - B2, sitting between A1 and A2 in time, should have read lower than A1 if nothing
else had changed. It read higher than both.

| Block | A1 inside | A2 inside | mean inside | B2 outside | outside minus inside |
|---|---|---|---|---|---|
| 1241-1341 | -30.42 | -31.36 | -30.89 | -25.98 | **+4.9** |
| 1341-1441 | -33.04 | -34.67 | -33.86 | -30.98 | +2.9 |
| 1441-1541 | -31.86 | -33.58 | -32.72 | -29.48 | +3.2 |
| 1541-1641 | -35.57 | -36.96 | -36.27 | -35.39 | +0.9 |
| 1641-1741 | -35.42 | -36.70 | -36.06 | -35.56 | +0.5 |
| 1741-1841 | -36.68 | -37.42 | -37.05 | -36.98 | +0.1 |
| 1841-1941 (digital floor) | -37.24 | -37.64 | -37.44 | -36.97 | +0.5 |
| 1941-2041 (PCS downlink) | -34.12 | -34.20 | -34.16 | -32.42 | +1.7 |
| 2041-2141 | -37.54 | -37.94 | -37.74 | -36.96 | +0.8 |
| 2141-2147 (AWS downlink) | -36.31 | -36.46 | -36.39 | -31.56 | **+4.8** |
| whole-band median | -34.86 | -36.06 | -35.46 | -34.02 | +1.4 |

PCS downlink 1930-1990 per sweep: A1 -28 to -30, B2 **-23 to -25**, A2 -28 to -30. Straight
up outside, straight back inside.

**Verdict against the predictions.** Low-E glass (9 dB or more): **refuted**. Plain glass
(1-2 dB): **exceeded**. Outside is worth **3-5 dB in the bands that carry signals**, 5-6 dB
once the extension's loss is added back, and essentially nothing where the floor is the
converter's. A thin metallic coating would have shown 10 dB and did not, so the loss is the
frame, the wall and the room, not a film on the glass.

**What 3-5 dB buys, honestly.** In bands where the floor is received noise (1241-1541, the
downlinks) signals and floor rise together and detection SNR does not improve, though more and
stronger signals appear (detections 14 -> 22, strongest -16.9 -> -9.6 dB). In bands sitting on
the digital floor (most of 1541-1941 and 2041-2110) a signal gains the full amount while the
floor stays put, which is a real sensitivity gain exactly where the station is weakest. The cost
is overload margin: the strongest signal outside reached -9.6 dB at g29.

**Decision.** A permanent outside mount is worth building, but not with 3 ft of cable ahead of
the amplifier: the LaNA and notch go outside with the antenna in a small weatherproof box, bias-tee
powered as now, and the 3 ft coax then carries amplified signal, where its loss costs nothing.
Suction cups on sun-warmed glass are not a mount. Until then the inside position stands.

Seen outside and not inside: 1435.92 MHz (aeronautical telemetry band, 1435-1525), -11.8 dB in
B and -20.0 in B2, 80-90% on, varying like a moving emitter; 1489.91 MHz, same band, -18.7.

### rtl_power_fftw characterised against rtl_power (2026-10-22, 07:40-07:51): the hops match, the scale is 3.3 dB off the model, and the E4000 stopped at 2170.6 MHz

First use of the second engine on the radio. XTR, the only dongle plugged in (device 0),
UWB biconical on the notch at the window, chain untouched between runs, g29, antenna on,
about 07:40 local. Yardstick: the PCS downlink block 1930-1990 MHz, always on. Six short
runs, files under `surveys/tooltests/rpfchar/`, nothing archived.

| Run | Command (device 0) | Passes / sweeps | Bins kept | Wall |
|---|---|---|---|---|
| `rp50k` | `rtl_power -f 1930M:1990M:50k -g 29 -i 10 -c 0.25 -F 9 -e 120` | 12 | 1393 (43.10 kHz, FFT 64) | 121.4 s |
| `rp25k` | same at `25k` | 12 | 2785 (21.55 kHz, FFT 128) | 121.6 s |
| `fftw64` | `rtl_power_fftw -f 1930M:1990M -g 290 -b 64 -r 2400000 -t 1 -o 25 -e 120` | 4 | 1584 (37.5 kHz, 33 hops) | 145.6 s |
| `fftw128` | same at `-b 128` | 4 | 3168 (18.75 kHz) | 145.5 s |
| `fftwfast` | `-b 64 -t 0.05 -e 60` | 17 | 1584 | 62.6 s |
| `fftwtop` | `-f 2147M:2187M -b 128 -t 1 -o 25 -e 60` | 5 | 1242 (13 hops, see below) | 73.2 s |

rtl_power_fftw runs are read with the analysis-time crop of 0.25 (the middle 75% of each
hop), so both engines are judged on the same part of their hops. No dropped samples and no
buffer underruns in any of the 893 hops the program recorded (`Buffer queue histogram`
first column 0 throughout).

**(a) The dB scale.** Pre-registered from the two programs' source: rtl_power_fftw writes
`10*log10(sum|X|^2 / repeats / N / rate)`, a power spectral density; rtl_power multiplies
each sample by 256 (its window coefficients are scaled by 256, rectangular by default),
runs a fixed-point FFT that divides by N, and writes `10*log10(sum|X/N|^2 / samples / rate)`.
So on the same input, **fftw minus rtl_power = 10*log10(N_rtl_power) - 48.2 dB +
10*log10(rate_rtl_power / rate_fftw)**: -29.5 dB against rtl_power's 64-point FFT (the 50k
presets) and -26.5 dB against its 128-point (25k), independent of rtl_power_fftw's own bin
count. Block medians (median over sweeps, then over the block):

| Block | rp50k | rp25k | fftw64 | fftw128 |
|---|---|---|---|---|
| 1930-1940 | -27.72 | -30.66 | -53.22 | -53.14 |
| 1940-1950 | -26.84 | -29.64 | -52.36 | -52.34 |
| 1950-1960 | -27.78 | -30.98 | -54.33 | -53.31 |
| 1960-1970 | -28.20 | -31.45 | -54.37 | -54.27 |
| 1970-1980 | -27.48 | -30.80 | -54.01 | -53.58 |
| 1980-1990 | -28.92 | -32.42 | -55.16 | -54.31 |
| **1930-1990** | **-27.78** | **-30.96** | **-53.94** | **-53.52** |

| Offset | Measured | Predicted |
|---|---|---|
| fftw64 - rp50k | **-26.16 dB** (10 MHz blocks -25.5 to -26.6, std 0.43) | -29.5 |
| fftw128 - rp25k | **-22.56 dB** | -26.5 |
| rp25k - rp50k | -3.18 | -3.01 |
| fftw128 - fftw64 | +0.42 | 0 |

**The shape of the prediction held and the constant did not.** rtl_power's own 64 -> 128
step moved it -3.18 dB (3.01 predicted), and rtl_power_fftw's reading is the same at 64 and
128 bins to within the +0.4 dB that two different bin grids make of a structured block, so
the per-Hz normalisation and the 10*log10(N) dependence are as the source says. But both
offsets sit **3.3 dB above** the model. The integer FFT is not the reason: rtl_power's
`fix_fft`, `FIX_MPY`, sine table and `real_conj` were compiled verbatim from its source into
a harness and fed the same quantised Gaussian samples as a float DFT, and they reproduce the
model to **0.01 dB at every noise level from 0.5 to 8 ADC counts** (the harness is
throwaway; the method is the record). So the two programs do what their source says, and the
difference is in what the **dongle delivers**: rtl_power ran the RTL2832U at 2.7586 MS/s (its
own choice for 29 hops over 60 MHz) and rtl_power_fftw at 2.4 MS/s, and the per-Hz level that
comes out differs by about 3.3 dB between those two rates - the decimation chain's gain, or
the E4000's IF filter, which librtlsdr sets from the sample rate, drooping across rtl_power's
wider kept region (+/-1.03 MHz against +/-0.90). The two cannot be told apart on a
structured block. **Pre-registered discriminator, two minutes of radio:** `rtl_power_fftw -f
1930M:1990M -b 64 -r 2758620 -t 1 -o 25 -e 120` beside `rp50k`. If the offset becomes
-30.1 dB the sample rate is the whole story; if it stays near -26 the rest is in the filter
shape. Best done with the noise generator on a pad, where the input is flat and the block
median is honest. Until then the working rule is: **an rtl_power_fftw reading at 2.4 MS/s
is about 26 dB below the same thing on rtl_power's 50k scale**, an empirical constant shown
only for 1930-1990 MHz at g29, and never a reason to compare the two engines' numbers
directly. The fingerprint carries the sample rate for exactly this reason.

**(b) Dwell and retune.** Timestamps are UTC to the second, so the per-hop figures come from
many hops: first acquisition start to last acquisition end, divided by the hops in between.

| Run | Hops | Span | Per hop | Listening per hop | Overhead per hop |
|---|---|---|---|---|---|
| `fftwfast` (-t 0.05) | 561 | 61 s | 0.1087 s | 0.0500 s | **58.7 ms** (+/- 2) |
| `fftw64` (-t 1) | 132 | 144 s | 1.091 s | 1.000 s | **91 ms** |
| `fftw128` (-t 1) | 132 | 144 s | 1.091 s | 1.000 s | 91 ms |

The overhead is not one number: a 0.05 s hop is one 240 KB read and costs 59 ms (tune, the
5 ms sleep the program keeps, buffer reset, USB start); a 1 s hop at 2.4 MS/s is 4.8 MB,
which the program reads as three 1.6 MB buffers with `rtlsdr_reset_buffer` before each, and
costs 91 ms, so **about 16 ms per extra buffer**. rtl_power's figure on this USB stack is 47
ms per hop. Listening fraction: 92% at `-t 1`, 46% at `-t 0.05`. The model `59 + 16 x (reads
- 1)` ms is now what `marsad run` uses for this engine (`rpf.geometry`, overridable in
station.json), and it predicts the measured 1.091 s per hop by construction. **The hop plan
is reproduced exactly:** all 33 centres the program tuned over 1930-1990 and all 13 it
reached over 2147-2187 equal `rpf.plan_hops()` to the hertz (first 1931.200000, then
+1.800000 MHz; first 2148.200000, then +1.790477 MHz).

**(c) Above rtl_power's ceiling.** `fftwtop` recorded **2147.302-2170.565 MHz, 1232 bins
above 2147.483648 MHz, all finite, five passes in 73 s** - the first data this station has
above 2^31 Hz. Median -60.8 dB on the engine's scale (about -34.6 on rtl_power's with the
empirical offset); 2147-2160 sits 0.9 dB above 2160-2170, consistent with the AWS downlink
ending at 2155; strongest bin 2158.915 MHz at -54.5; nothing within 10 dB of floor+10. **But
the E4000 would not lock above 2170.6 MHz.** Every planned centre from 2171.476 MHz up
failed three times with `[E4K] PLL not locked` and was dropped, nine hops in all; the
program carried on with the thirteen that worked, which is the behaviour wanted. So on this
morning, after the dongle had been running for ten hours, the XTR's usable top was between
2170.9 (top edge of the last locked hop) and 2171.5 MHz (first refused centre) - **16 MHz
short of the 2187 MHz `rtl_test -t` reported on 2026-10-13** and 23 MHz above rtl_power's
ceiling. Whether that is temperature (the E4000 PLL's top end is known to move with it), the
margin by which `rtl_test` judges a lock, or a drift in the unit is open: re-measure cold with
`rtl_test -t` and with `marsad run top 5m` back to back, and again after an hour warm. The
`top` preset stays at 2147-2187: it asks for everything, the program drops what will not
lock, and the `.log` names each dropped hop.

**The owner's first real run (2026-10-22, 18:45-19:37): the PLL ceiling descends as the dongle
warms.** `marsad run top 1h --device 0 --tag uwb-first --skip-warmup 5`, UWB biconical on the
notch at the window, g29. The Claude desktop app restarted at 19:37 and took the recorder with
it after 52 min; the `.dat` was analysed afterwards (`marsad analyze`, 178 passes, 2102 bins,
2147.30-2186.68 MHz) and archived with the antenna flag. Read pass by pass, the log and the
data say something the morning run could not: **all 22 hops locked at the start, up to
2185.8 MHz, and the top ones then dropped out one at a time** - 2185.80 after 3 passes,
2184.01 after 9, 2182.22 after 16, 2180.43 after 25, 2178.64 after 37, 2176.85 after 53,
2175.06 after 76, 2173.27 after 116 (35.6 min in; first written as "about 45", corrected 2026-10-23), while 2171.48, refused that morning
after ten hours running, held through all 177 passes. So the E4000's upper lock limit is not a
fixed 2170.6 or 2187: it is **above 2187 MHz cold and falls to about 2172 MHz as the dongle
warms**, which is why `rtl_test -t` on a cold unit said 2187 and the warm morning run said
2170.6. The top 15 MHz of the XTR's range are available for the first minutes of a run and
then gone. Consequences: the `top` preset stays at 2147-2187 and the program drops what will
not lock (the log names each hop; the Scan page hatches them); a survey that needs 2172-2187
MHz has to start cold and be short, or a 50-ohm-cooled dongle is the next experiment; and the
warm-up discard works against the top slice here, because the warm-up is when those hops
exist. Floor on the engine's scale: -59.4 dB at 2147.5-2160, -61.0 at 2160-2171, -60.1 at
2170-2180, -63.5 at 2180-2190 (from the early passes); the four detections (2164.30, 2166.08,
2180.44, 2182.23) are all hop centres of the plan, nothing else.

Incidental, for anyone reading the raw `.dat`: with `-e` the program writes the extra blank
line that marks the end of a pass **only once, at the very end**, not after every pass, so
passes are not separated in the file; the parser groups hops into passes by the frequency
wrapping back down, as it does for rtl_power, and does not care.

### The return-loss bridge arrived miswired (2026-10-22, 14:07-15:13): every reading so far is leakage

First radio use of the bridge, XTR at g14, no pad, generator warm, notch and LaNA off the window,
bridge OUT > DC block > 3 ft coax > bulkhead > XTR. Three short checks, nothing archived as a
measurement of anything but the bridge.

| Run | Bridge ports | Range | Block medians |
|---|---|---|---|
| `br-open-check` 14:07 | DUT open, REF on the load | 1850-2147, 25k | -39 to -41, i.e. the floor |
| `br-open-LScheck` 15:05 | DUT open, REF on the load | LS, 50k | -22.7 at 1241-1291, falling to -38.5 by 2041 |
| `br-test1-bothopen` 15:12 | DUT open, REF open | 1241-1541, 50k | -14.5 to -22.7 |

**Two signs of a broken bridge, both pre-registerable from how a bridge works:**

1. **The open reference is ~25 dB too low.** The same generator straight into the XTR through
   36 dB of pad at g14 read -26.3 at 1241-1341 (`nsrc-D14-LS`), so unpadded it is near +10. A
   working bridge costs ~6 dB each way, so DUT open should read about -2 dB at 1.3 GHz. It read
   -22.7 to -27.6.
2. **Both ports open is LOUDER than one port loaded, by 7.5-9.8 dB in every 50 MHz block.** With
   DUT and REF identical the bridge balances and OUT should fall 25 dB or more. It rose.

**Cause, found on the bench:** both white wires of the shielded pair and its braid are blobbed
together at both ends, and at the OUT end the blob spills onto the pad row beside the 0 ohm link.
OUT centre to OUT shell meters **0.3 ohm instead of about 100**. The detector's two leads are
shorted, so the bridge cannot see the DUT-REF difference and what reaches OUT is leakage. The
~100 MHz ripple in the open-port runs (peaks 1270-1280 and 1360-1380 MHz) is that leakage path,
not anything about the ports.

The fix is drawn in `decoders/rf_bridge_rewire.svg`: unsolder both ends, meter the bare pads,
then **coax-style** wiring if the bridge end has two pads, **balanced-pair** if it has three.
Pass check on the meter, nothing connected: OUT centre to shell ~100 ohm, Input to REF ~50,
Input to DUT ~50, REF to DUT ~100. **Pass check on the radio, pre-registered:** DUT open near
-2 dB at 1.3 GHz at g14, and both-open at least 25 dB below that. Until both pass, `marsad
response` has nothing valid to work on and no antenna has a return-loss figure. **The unit was
returned on 2026-10-22 without being rewired, and a replacement is ordered.** *Corrected
2026-10-23:* the replacement is a **three-port** "SWR Bridge 10-3000 MHz" (RFIN, RFOUT, DET, 50
ohm reference built in), so the four-port meter checks and the "both ports open" test above do
not apply to it. Its checks are in RUNSHEET Block G: meter RFIN-RFOUT ~50, RFIN-shell ~100,
DET-RFOUT ~0, DET centre-shell 50-100 and never 0; on the radio, RFOUT open near -2 dB at 1.3 GHz
at g14 and the load 25 dB or more below it. The rewire diagram stays in `decoders/` for the
record.

Incidental: 1850-2147 at 25k reads ~3 dB below the LS-range run at 50k in the overlap. That is
rtl_power's 128- against 64-point FFT, the -3.0 dB step in the fftw characterisation, not the
bridge.

### `top` on the UWB (2026-10-22, 18:45): this time the E4000 locked all the way to 2186.7 MHz

`uwb-first`, rtl_power_fftw `top` preset, 1 h requested, 178 passes over 51 min, standing chain
rebuilt after the bridge work with the UWB on the notch (**no DC block** in this chain; it had
gone to the bridge). Fingerprint `... | N128 | r2.4M | o25 | c0.25 | rtl_power_fftw | xtr`.

**All 22 planned hops appear in the data, 2147.302-2186.684 MHz, 2102 bins - but not in every
pass.** *Corrected 2026-10-23:* this paragraph first read the run as "all hops locked, so warmth
is not the explanation". That was a whole-run reading and it was wrong. Read pass by pass (see
"The owner's first real run" in the rtl_power_fftw characterisation section), every hop locked
at the start and the top ones dropped out one at a time as the dongle warmed, from 2185.80 after
3 passes to 2173.27 after 35.6 min. The upper lock limit falls with warm-up, which reconciles the
cold `rtl_test -t` (2187) with the warm morning run (2170.6). The 24 `PLL not locked` lines are
those drop-outs.

| Block | median (fftw scale) |
|---|---|
| 2147-2155 | -59.41 |
| 2155-2160 | -59.15 |
| 2160-2170 | -60.91 |
| 2170-2180 | -59.36 |
| 2180-2187 | **-63.00** |

Whole-run median floor -60.8 dB, the same as the morning's `fftwtop`. 2147-2160 sits ~1.6 dB over
2160-2170, consistent with the AWS downlink ending at 2155, as in the morning. The top 7 MHz falls
3.6 dB below its neighbour, which looks like the tuner rolling off at the edge of its range rather
than anything received. **Nothing real above 2155**: all four detections are hop-centre artefacts
(2164.30, 2166.08, 2180.44, 2182.23 MHz).

Note: the report header says "90 L, whips 60 mm" for this run. It is the known stale-header
problem of `--non-interactive` runs; `chain_order` (UWB biconical) is the truth.

### Block H1, the fftw scale check (2026-10-22, 21:46-22:01): the 3.3 dB was the sample rate

Pre-registered in RUNSHEET Block H1. Four 2 min runs back to back, rig untouched, UWB on the
standing chain, g29, PCS downlink 1930-1990 MHz: A rtl_power 50k, B rtl_power_fftw `-b 64` at
**2.758620 MS/s** (the rate rtl_power picks for this span, giving **43.1034 kHz bins and 29 hops,
identical to A**), C rtl_power_fftw `-b 64` at 2.4 MS/s (this morning's setting), D = A again.
Logs confirm both rates; no dropped samples.

**The bracket failed over the whole block and held where it mattered.** D read 1.45 dB below A
overall, against a 0.3 dB limit. Sweep by sweep the drop is entirely in **1950-1990** (A -24.3
to -25.8, D -26.2 to -27.3, flat inside each run), while **1930-1950 did not move** (D-A -0.01
and -0.18 dB). The rig was untouched, the lower carriers held and the upper ones fell together:
the network shedding load at 22:00, not the station. So the verdict is read on 1930-1950, where
the bracket holds, and checked against the mean of A and D over the rest.

| 10 MHz block | D - A | B - A | C - A | C - B |
|---|---|---|---|---|
| **1930-1940** | **-0.01** | **-29.87** | **-26.16** | **+3.71** |
| **1940-1950** | **-0.18** | **-29.62** | **-26.30** | **+3.33** |
| 1950-1990, against mean(A,D) | -1.4 to -2.0 | -29.5 to -30.4 | -22.8 to -26.0 | +4.1 to +6.9 |
| whole block, against mean(A,D) | -1.45 | **-29.93** | **-25.48** | +4.46 |

**Against the predictions:**

- B - A, matched rate and identical bins: **-29.6 to -29.9 dB** in the stable blocks, model
  **-30.1**. Within 0.5 dB, and the 3.3 dB discrepancy is gone. **The two programs agree with
  their source code once the dongle runs at the same rate.**
- C - A reproduces this morning's **-26.2** to 0.1 dB (-26.16, -26.30). Nothing changed today.
- C - B, inside one engine where the model says 0: **+3.3 to +3.7 dB**. The dongle delivers about
  3.5 dB more per Hz at 2.4 MS/s than at 2.7586 MS/s, for the same signal. That is the whole of
  the morning's discrepancy, now measured directly rather than inferred.
- 1950-1990 is noisier (C - B up to +6.9 at 1980-1990) because that half was changing under the
  runs; it is consistent with the stable blocks once A and D are averaged, but it decides nothing.

**What it means.** The engines are fine; the **sample rate changes the level the dongle hands
over**, by about 3.5 dB between 2.4 and 2.7586 MS/s on the XTR at 1.93 GHz, g29. Somewhere in
the RTL2832U's decimation or the E4000's rate-dependent IF filter, which librtlsdr sets from the
sample rate - which one is not settled and does not need to be. The working rules:

- **rtl_power_fftw at 2.758620 MS/s reads rtl_power's 50k scale minus ~29.8 dB** (the model's
  10*log10(64) - 48.2 = -30.1, plus ~0.3 measured). At 2.4 MS/s it is minus ~26.2. Measured
  only at 1.93 GHz, g29, XTR.
- **Never compare two fftw runs at different sample rates on level**, even with the same bin
  width. The fingerprint already carries `r2.4M` / `r2.76M`, so `compare` refuses them; this is
  the measured reason it must.
- Where a run has to be laid beside rtl_power data, use **2.758620 MS/s and `-b 64`** over the
  same span: the hop plan and bin width then match rtl_power's exactly.

Untested: whether the 3.5 dB is the same at other frequencies or gains. A noise-generator pair at
2.4 and 2.7586 MS/s on a flat input would show the rate effect across the whole band in two runs.

### Block H2, 1633.47 MHz pad test (2026-10-22, 22:07-23:18): not made in our chain, and far less steady than thought

Pre-registered in RUNSHEET H2 (amended after A, before B). One rtl_power_fftw hop on 1633.000 MHz,
2048 bins = **1.17 kHz**, 2.4 MS/s, `-t 10`, g29, 10 min each, UWB on the standing chain. A: no pad
(22:07). B: 10 dB attenuator between the notch's SMA adapter and the LaNA input (22:47). C: pad out
again (23:09). The as-built stack without the pad is **notch > SMA adapter > LaNA**; A's chain text
omitted the adapter and its sidecar carries a `chain_correction`.

**The line is at 1633.4721 MHz in all three, one 1.17 kHz bin wide** (neighbours 3-4 dB down on
each side, nothing beyond), so it is narrower than ~2 kHz. No mirror image at 1632.53.

| Run | Line, median over 59 spectra | Range within the run | Excess over the local floor | Local floor |
|---|---|---|---|---|
| A, no pad | **-52.9** | -56.7 to -49.0 | 8.7 dB | -61.6 |
| B, 10 dB pad | **-55.4** | -59.9 to -52.6 | 8.9 dB | **-64.3** |
| C, no pad | **-47.3** | -51.4 to **-39.5** | 14.8 dB | -62.0 |

**Scored against the predictions:**

- **A and C agree within 3 dB: FAILED.** They differ by 5.5 dB, and C alone fell **8.7 dB in ten
  minutes** (-41.0 at 23:09 to -49.7 at 23:18). The line's wander is not the "6 dB over an hour"
  the deep tile suggested; it moves several dB within minutes.
- **Made in our chain (second order -20, third order -30): REFUTED, and this half survives the
  failed bracket.** Through the pad the line was present in every one of B's 59 spectra, 4.6 to
  11.5 dB over the floor. A product of the LaNA would have fallen 20-30 dB below A and vanished.
  The LaNA-intermod story for 1633.47 is closed, on top of the width argument (its supposed
  1966.49 parent was never a narrow line).
- **Real signal at the antenna, -10 dB: consistent but not shown.** B sits 2.5 dB below A, 8.1 dB
  below C, about **5 dB below their mean**. With the line swinging +/-4 dB on a minute scale, that
  is indistinguishable from -10. It also fits a signal partly entering **after** the pad (into the
  LaNA, the coax or the cage), which a pad at the LaNA input cannot attenuate. Not separated.

**Incidental, the floor:** the pad lowered the local floor at 1633 MHz by **2.4-2.7 dB**. So at 1.63
GHz the LaNA's amplified noise (antenna noise plus its own) still lifts the floor 2-3 dB above the
receiver's digital floor; cutting 10 dB ahead of it removes most of that. Consistent with cascade
step 3's LaNA tilt (+0.6 dB at 1641-1741 terminated) plus the received noise of step 5.

**What it now looks like:** a narrow (<2 kHz), frequency-locked, strongly fading tone that
reaches the antenna inside the window but vanished with the antenna outside (Block E). The
pattern points to **a device in the room or the building**, not the sky and not our chain. The
fading (several dB in minutes) suggests either the source's own duty or a path that changes
(people moving, a fan). The **Hunt mode** in the Probe tab exists for exactly this: walk a
near-field probe round the room on the 5 MHz `hunt` tile around 1633.47 and listen for the tone.

### Block H3, cold-start top edge (2026-10-23, 11:18-12:19): the whole PLL range slides down ~10-16 MHz as the XTR warms

Pre-registered in RUNSHEET H3. XTR idle (powered, not streaming) for 12 h, then cold `rtl_test
-t`, `top 1h` (rtl_power_fftw, 22 hops, g29, UWB on the standing chain), and `rtl_test -t` again
straight after, warm. Launched outside the desktop app's process tree; nothing touched.

| | cold, 11:18 | warm, 12:19 (after 1 h streaming) | shift |
|---|---|---|---|
| E4K top | **2186 MHz** | **2170 MHz** | **-16** |
| L-band gap | **1094-1239** | **1086-1230** | **-8 / -9** |
| bottom | 52 | 52 | 0 |

Hop by hop through the hour (last pass each hop centre appeared; no hop ever came back once gone):

| Hop centre | H3 today, cold start | `uwb-first`, 2026-10-22 evening |
|---|---|---|
| 2185.80 | 0.4 min | 1.2 min |
| 2184.01 | 2.3 | 3.5 |
| 2182.22 | 4.5 | 6.0 |
| 2180.43 | 7.6 | 9.1 |
| 2178.64 | 11.2 | 13.1 |
| 2176.85 | 15.6 | 18.0 |
| 2175.06 | 21.4 | 24.7 |
| 2173.27 | 29.9 | 35.6 |
| 2171.48 | **44.1** | held all 51 min |
| 2169.69 and below | held all 60 min | held |

**Against the predictions:** all 22 locked on the first pass (held); top-down, monotonic, no
re-lock (held - so a smooth thermal slide, not marginal locking); 2185.80 within 2 min (0.4,
held); 2180.43 by ~15 +/- 7.5 min (7.6, held at the edge); 2173.27 by ~45 +/- 22 min (29.9,
held); **2171.48 holds the whole hour: REFUTED**, it went at 44 min; warm `rtl_test -t` top
2175 or lower (2170, held).

**What it means.** The E4000's PLL tuning range is set by its VCO, and the whole range moves down
with temperature: the top by 16 MHz and both gap edges by 8-9 MHz over the first hour of
streaming. Today's curve ran ~15-25% faster than last night's and one hop further, so the
starting temperature and the room matter, but the shape repeats. Practical rules:

- **2172-2187 MHz is a cold-start-only band**: usable for the first ~5-40 minutes depending on
  the hop, gone after an hour. A survey of it must start cold and be short. Above ~2170 nothing
  is available on a warm XTR.
- **The PLL gap moves too, and that reaches 1090 MHz.** Warm, `rtl_test -t` puts the gap's lower
  edge at **1086 MHz, so 1090 (ADS-B) is inside the warm gap**. Cold it was 1094. rtl_power and
  rtl_power_fftw drop a hop that will not lock, but `rtl_adsb` keeps receiving through `PLL not
  locked`, so a warm XTR may decode ADS-B poorly or not at all with no clear error. Untested.
  **Pre-registered:** `adsb_check.py` on the XTR at g29, 10 min cold then 10 min after an hour of
  streaming, same hour of the day: a working-then-failing pattern means ADS-B belongs on the v5
  (R860, no gap) or on a cold XTR. The 2026-10-16 XTR control (1856 frames/min) did not record
  how warm the dongle was.
- The gap's top edge moving down (1239 -> 1230) only widens what LS (starting at 1241) can use.

### Block H4, UWB on the LS fingerprint (2026-10-23, 12:19-12:58): no better than the whip in its own spot

Pre-registered in RUNSHEET H4. `custom 2h --freq 1241M:2148M:50k -g 29 -i 120`, UWB on the
standing chain, dongle warm from H3. **Cut short at 12:57:48 (38 min, 19 sweeps)** by a Ctrl-C
reaching its console; the recorder died without finishing, so it was analysed afterwards with
the pre-registered `--skip-sweeps 8`, leaving 11 sweeps (the sidecar has a
`conditions_correction`). That is as long as the 30 min whip runs it is read against, so the
comparison stands.

Within-run plateaus (block median minus the run's own empty 2049-2099 block):

| Block | UWB H4 | whip casc5 (10-19 18:47) | whip A1 (10-20 07:28) | whip A2 (10-20 10:20) | whip restored (10-21 09:27) | UWB minus whip mean |
|---|---|---|---|---|---|---|
| 1241-1341 | 7.35 | 6.10 | 7.81 | 7.26 | 10.84 | -0.65 (whip spread 4.7) |
| 1441-1541 | 4.02 | 5.22 | 6.37 | 5.04 | 6.50 | -1.76 |
| 1930-1990 PCS downlink | **8.62** | 10.41 | 9.29 | 10.02 | 10.35 | **-1.39**, below all four |
| 2124-2147 AWS downlink | **3.36** | 3.06 | 3.47 | 2.82 | 3.08 | **+0.25**, inside the whip range |

**Against the predictions:** AWS at least +1.5 dB for the UWB: **REFUTED** (+0.25). PCS at least
+1.0: **REFUTED** (-1.39, the UWB is below every whip run). 1241-1341 within +/-2 dB: held.

**Why this does not contradict the 2049 deep tiles.** There the dipole was **moved to the UWB's
exact spot**, at g42, and the UWB won by +1.8 to +2.3 dB on the AWS plateau. Here the whip runs
sit in the dipole's own spot, 8 in left and a few inches higher, at g29. So H4 compares **antenna
plus position**, and the position seems to give back what the antenna gains. A second effect
works the same way: at g29 the AWS plateau is only ~3 dB above the digital floor, which squeezes
any difference (a real +2.3 dB at a 3.1 dB plateau would read about +1.3). **Reading: at its
current spot the UWB is no better than the whip across LS.** The UWB's advantage is real but
needs the better spot to show, so the dipole's outlined spot, or outside, is where it belongs.
The clean test is the UWB moved onto the dipole's outline, 30 min at this fingerprint.

**Incidentals, same run:**

- **1633.47 MHz is almost gone with the UWB**: +1.4 dB over the floor, against +7.6 to +10.9 in
  all four whip runs. *The inference first drawn here - "a near-field source close to the
  dipole's spot" - is WITHDRAWN by Block H5:* on the same spot, same evening, 1799.98 MHz (a
  distant carrier) reads ~8 dB weaker on the UWB than on the whips, and 1633.47 ~6 dB weaker.
  Both narrow carriers favour the whip by about the same margin, so the antenna, not proximity,
  is most of what moved.
- **1799.98 MHz is back** at +16.6 dB, steady; retired as a yardstick on 2026-10-20 and not
  reinstated on one run.
- **1435.92 MHz** (aeronautical telemetry, previously seen only with the antenna outside) at
  +7.9 dB median, against +1.1 to +3.6 in the whip runs. The UWB at its spot hears it; the whip
  did not.

### Block H5, whip / UWB / whip on the dipole's outline (2026-10-23, 21:40-00:17): the UWB wins the downlinks, the whip wins the carriers

Pre-registered (and revised before any run) in RUNSHEET H5. Three 30 min blocks at the LS
fingerprint, g29, `--skip-warmup 10` (5 of 10 sweeps analysed each), all three antennas on the
dipole's outlined spot, only the antenna swapped: A1 whips 21:40, B UWB 22:38, A2 whips 23:46.

Within-run plateaus (block median minus the run's own 2049-2099 block):

| Block | A1 whip | **B UWB** | A2 whip | A2 - A1 | B - mean(A) | B - best whip |
|---|---|---|---|---|---|---|
| 1241-1341 | 7.79 | 6.29 | 6.11 | -1.68 | -0.66 | -1.50 |
| 1341-1441 | 3.72 | 4.39 | 2.79 | -0.94 | +1.14 | +0.67 |
| 1441-1541 | 4.64 | 3.13 | 3.91 | -0.73 | -1.14 | -1.51 |
| 1930-1990 PCS downlink | 7.84 | **9.29** | 5.77 | **-2.06** | +2.49 | **+1.45** |
| 2110-2124 | 4.38 | 2.16 | 2.32 | -2.07 | -1.19 | -2.22 |
| 2124-2147 AWS downlink | 2.24 | **4.63** | 3.31 | +1.07 | +1.86 | **+1.32** |

| Line, peak over floor | A1 whip | B UWB | A2 whip |
|---|---|---|---|
| 1799.98 MHz | +29.1 | **+20.0** | +27.5 |
| 1633.47 MHz | +13.9 | **+5.7** | +9.9 |
| 1435.92 MHz | +0.8 | +1.3 | +1.3 |

**Against the predictions:** A1 and A2 within 1.0 dB on PCS and AWS: **FAILED** (PCS -2.06,
AWS +1.07; the late-evening network falls away, as in Block H1). But on both downlinks the UWB sits
**above both whip runs**, so the verdict is read against the better whip, the strict reading:
**PCS +1.45 and AWS +1.32 dB, both meeting "at least +1.0": held.** 1241-1341 within +/-2: held.
1633.47 at +7 or more in all three: **failed for the UWB** (+5.7).

**What it means.**

- **On the same spot, the UWB is the better antenna for the broadband downlinks by ~1.3-1.5 dB
  at g29**, matching the deep-tile result squeezed by the digital floor. H4's shortfall was the
  UWB's own spot: moving it 8 inches onto the dipole's outline turned PCS from -1.4 to +1.45.
- **The whips beat the UWB by 6-9 dB on narrow carriers** (1799.98, 1633.47), on the same spot.
  The likeliest reason is polarization: the 90 deg L has a horizontal element and the UWB is
  vertical only, while the base-station downlinks are usually slant +/-45 deg and reach both. That
  is a hypothesis; rotating the UWB to horizontal for one 10 min block would test it.
- **So H4's "1633.47 is a near-field source by the dipole's spot" is withdrawn**: 1633.47 behaves
  like the distant 1799.98 carrier. It still moved +4.3 dB when the UWB moved onto the outline
  (+1.4 -> +5.7), so the spot matters some, but the antenna matters more.
- **Which antenna is "better" depends on the target**: UWB for cellular-style broadband, the L
  dipole for whatever 1633/1800 are. The standing antenna should be chosen per survey, and the
  chain text will say which.

### UWB biconical vs the 60 mm whip at 2.1 GHz (2026-10-21): the antenna was the limit, worth +2.3 dB

The test the 2 GHz positive control asked for. `deep-2049-uwb-g42` (18:40) and
`deep-2049-whip-g42-day` (23:04), same tile, same fingerprint
`2049.0-2148.0 MHz | 25.0 kHz | g42.0 | i30 | c0.25 | F9 | xtr`, 1 h each, notch and LaNA
untouched. **The dipole was moved to the UWB's exact spot** so both antennas were compared in
one place rather than one being measured where the other had never sat - which is what makes
this comparable at all.

| 10 MHz block | whip | UWB | change |
|---|---|---|---|
| 2049-2059 | -41.24 | -40.64 | +0.60 |
| 2059-2069 | -41.41 | -40.64 | +0.77 |
| 2069-2079 | -41.36 | -40.64 | +0.72 |
| 2079-2089 | -41.34 | -40.65 | +0.69 |
| 2089-2099 | -41.32 | -40.64 | +0.68 |
| 2099-2109 | -41.52 | -40.72 | +0.79 |
| **2109-2119** | **-39.72** | **-34.56** | **+5.16** |
| **2119-2129** | **-34.96** | **-31.08** | **+3.88** |
| **2129-2139** | **-35.40** | **-31.71** | **+3.69** |
| 2139-2147 | -36.49 | -34.17 | +2.31 |

**The band edge is exactly where the allocation says.** Six consecutive empty blocks sit flat
within 0.3 dB in each run, then 2109-2119 jumps. The downlink starts at 2110 MHz and the data
finds it unaided.

**Read it on the within-run plateau, not the raw difference.** Downlink (2124-2147) minus the
empty reference (2049-2099), computed inside each run, so it cannot be moved by gain, drift or
the hour:

| Run | empty | downlink | plateau |
|---|---|---|---|
| `deep-2049-awscontrol-g42` whip, 10-19 night | -41.49 | -36.48 | **+5.01 dB** |
| `deep-2049-whip-g42-day` whip, 10-21 evening | -41.34 | -35.52 | **+5.82 dB** |
| `deep-2049-uwb-g42` UWB, 10-21 evening | -40.64 | -32.49 | **+8.15 dB** |

**The UWB beats the whip by +2.33 dB** on the statistic that cannot be gamed by conditions, and
the two whip runs two days apart agree to 0.81 dB, which is the repeatability figure for this
measurement. Raw signal is up 3.7-5.2 dB in the downlink blocks; the empty band rises a uniform
+0.7 dB, so some of the raw gain is more received noise, which is exactly why the plateau is the
honest number.

**This settles the open question from the 2 GHz positive control.** That section listed three
candidates for the weak 2.1 GHz performance - the tuner, the LaNA and the antenna - and noted the
antenna was the only one that could be changed. The noise generator had already cleared the LaNA
(16-18 dB to 2.147 GHz) and measured the receiver as ~11 dB less sensitive at 2.1 than at 1.3 GHz.
**The 60 mm whips, 0.84 wavelengths at 2.1 GHz and past their half-wave resonance, were costing
about 2.3 dB.** The receiver's own roll-off is the remaining limit and cannot be bought back.

**Caveats, honestly.** One run of each, 4.5 h apart, no A/B/A bracket, and the UWB ran first. The
plateau statistic removes most of what that could do, and the 0.81 dB whip-to-whip agreement
bounds the rest, but a bracket would make it airtight. The suction mount puts the UWB at the
inside face of the glass, so this is an inside-the-window figure; the outside result (+3-5 dB,
2026-10-20) stacks on top of it in principle and has not been measured together.

**Bracket closed (2026-10-22, 06:50): the UWB's margin survives the clock.** A second UWB leg in the
same spot, `deep-2049-uwb-g42-B`, cut to 30 min (41 sweeps analysed) by an app restart but intact.
Within-run plateau, downlink 2124-2147 over empty 2049-2099: UWB 18:40 **+8.00**, whip 23:04 **+5.78**,
UWB 06:50 **+7.15**; on the 2119-2134 core the three read +9.63, +6.45, +9.92. UWB/whip/UWB in one
spot across twelve hours: the UWB is **+1.8 dB on the plateau and +3.3 dB on the core** over the whip,
and the two UWB legs twelve hours apart agree to 0.85 dB on the plateau and 0.3 dB on the core, which
is the clock's whole contribution. The evening traffic was not the margin. Verdict unchanged: the
whip was the 2.1 GHz limit by 2-3 dB, and the UWB is the antenna for the top of the band.

### 2 GHz positive control (2026-10-19, 23:52): the XTR hears the cellular downlink at 2.1 GHz, weakly

`deep-2049-awscontrol-g42`, `2049M:2148M:25k -g 42 -i 30`, 1 h, 100 sweeps analysed, 21.8 kHz
bins, 71 s of listening per frequency, antenna on, night. **0% non-finite, max over band
-29.1 dB**: no clipping at g42 even with the downlink in the span.

**Pre-registered:** the 2110-2147 MHz cellular downlink shows as a plateau **at least 10 dB**
above the tile's own floor if the tuner has real sensitivity up there; **under 3 dB** means the
XTR is nearly deaf at 2.1 GHz and a 2 GHz antenna would be wasted.

| Block | 20th-pct floor | median | max-hold | median over the 2049-2100 reference |
|---|---|---|---|---|
| 2049-2109 (empty) | -41.6 to -41.75 | -41.1 to -41.75 | -31.5 to -33.2 | within 0.4 dB, flat |
| 2109-2114 | -40.24 | -40.08 | -32.57 | +1.42 |
| 2114-2119 | -39.17 | -38.74 | -31.14 | +2.76 |
| **2119-2134** | **-36.2 to -36.9** | **-35.7 to -36.1** | **-29.1 to -29.8** | **+5.4 to +5.8** |
| 2134-2144 | -37.9 to -38.4 | -37.4 to -38.2 | -30.3 to -30.6 | +3.4 to +4.1 |
| 2144-2147 | -36.97 | -36.16 | -29.41 | +5.34 |

**Result: between the two thresholds, nearer the good one.** The downlink is unmistakably there:
a 5-6 dB plateau in the median, 12 dB in max-hold, starting exactly at the 2110 MHz band edge
with the empty 2049-2109 MHz flat to 0.4 dB beside it. So the XTR is **not deaf at 2.1 GHz**.
But 5-6 dB for an always-on multi-carrier base-station band is weak; the PCS downlink at
1.95 GHz gave +4 dB over terminated in step 5 at g29, so 2.1 GHz is behaving like 1.95 GHz,
not falling off a cliff.

**What this does not separate: the tuner from the antenna.** The 60 mm whips, 120 mm tip to
tip, are 0.84 wavelengths at 2.1 GHz, past the half-wave resonance and toward the anti-resonance
where the 100 mm whips lost 7.8 dB at 1.8 GHz. A poor antenna match at 2.1 GHz would produce
exactly this picture. **The noise generator settles it**: source direct into the XTR, then through
the chain, gives the receiver's own response at 2.1 GHz with no antenna in the question. If the
receiver is flat up there, the antenna is the limit and a 2 GHz element is the right next build.

Reconciles with step 5: the 2041-2141 block "did not move with the antenna on" because the empty
2041-2109 MHz dominates a 100 MHz block; the downlink occupies only 2110-2155, and step 5's
2141-2147 block did rise +2.3 dB.

Incidental: 2091.82 MHz, a single-sweep 764 kHz burst at 1% occupancy. Nothing else in the tile.

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

### Milair 12 h map M1 (2026-10-26, 02:56-14:56): the E4000 has a 350 MHz edge and a near-deaf 315-350

First survey of 225-400 MHz. `milair` at **g19** (g29 hit full scale in the shakedown), `-i 30`,
1420 sweeps, long whips collapsed to 230 mm each in a straight vertical line, standing chain.
Fingerprint `225.000-400.000 MHz | 10.8507 kHz | g19 | i30 | c0.25 | F9 | xtr`. Full reading in
RUNSHEET M1.

- **Receiver features, not signals:** a fixed **10 dB step at exactly 350.0 MHz** (librtlsdr's
  E4000 VHF/UHF input switch) and a 10 dB roll-off over **310-315 MHz**. Between them, 316-349 MHz
  sits at -44 dB and does not move all day (per-sweep std 0.15 dB), while the bands on both sides
  move together (r = 0.91). Reading, unproven until the terminated run M1-T: **the XTR is nearly
  deaf across 315-350 MHz.** Never read a level across 350 MHz or 312 MHz as a change in the sky.
- **A broadband hump peaking near 270 MHz**, 15-25 dB over the 316-349 level, falling 5.4 dB
  from 03:00 to 14:00 with abrupt one-sweep drops (06:31, 12:29): a switching local source. The
  UHF satcom band 243-270 sits on top of it, so weak downlinks there are masked.
- **259.58, 262.65 and 296.93 MHz**, steady near -13 dB from 03:00, all left within 13:35-13:45:
  one local source, not satellites.
- **380-390 MHz:** a continuous carrier on 385.04 moved to 385.76 + 387.43 at 08:30 and back at
  13:22, with traffic on 386.50 / 386.98 - a trunked land-mobile system's control channel.
- **MUOS (360-380): no plateau.** Not an absence result: short antenna, through the window.

**M1-T, terminated twin (2026-10-26, 17:29):** same fingerprint, load on the notch input. The
antenna adds **+13 to +20 dB at 225-310, +7 dB at 351-380, and -0.08 dB at 316-349**: the XTR
hears nothing in 316-349 MHz on this chain, and its internal floor there sits ~3.5 dB below the
rest. A ~3.5 dB step near 315 and a 2.7 dB step at 350 survive with no antenna (tuner response);
the 270 MHz hump does not (received). **For 316-349 MHz (UHF milsat uplinks, part of the
milair band) use the v5, not the XTR.** The three lines 259.58 / 262.65 / 296.93 are absent
terminated; the display-sleep test M1-D is pending.

### V1-1b, the v5 alone in the aluminium cage (2026-10-27, 01:57-07:45): an hour to warm, 2 dB of drift

Cold start, 50 ohm load on the outside of the cage SMA bulkhead, `milair` at g40.2, `-i 30`, 697
sweeps. Fingerprint `225.000-400.000 MHz | ... | g40.2 | i30 | c0.25 | F9 | v5`. Full scoring in
RUNSHEET V1-1b.

- **Warm-up: settled at ~65 min (sweep 132), total fall -1.93 dB**, against the predicted 15 min
  and 0.5 dB. Then rock steady: std **0.016 dB**, net -0.07 over the next 4 h. The old v5 figures
  (sweep 4-6, -0.35 dB) were the copper cage; in the aluminium cage on foam it is six times worse.
  **Any v5 level comparison needs 65 min of streaming before its analysed part** (`--skip-warmup 65`),
  and twin runs back to back, because an idle-powered dongle cools.
- Settled whole-band median **-36.96 dB**. Floor not flat: block medians -35.8 to -38.4 (a dip at
  235-245, a slow tilt down toward 400). **No 350 MHz step** (0.02 dB), unlike the XTR's 2.7.
- All five detections are clock harmonics: 230.40, 259.20, 316.80 (28.8 x8, x9, x11) and 273.60,
  244.79 (14.4 x19, x17, which survey.py leaves untagged because it lacks the 14.4 family). Note
  259.20 is not M1's 259.58 line.

**V1-1 (2026-10-27 08:00-09:20): the v5's tuner gain has no effect through rtl_power.** With the
antenna on, `-g 7.7`, `22.9`, `29.7` and `49.6` all give the same levels to 0.1 dB (4 s captures at
383-387 MHz: median -23.6 at every gain; 385.04 MHz at +1.8 to +2.0, i.e. full scale), although
rtl_power prints "Tuner gain set to ..." each time. The same `rtl-sdr/rtlsdr.dll` sets the XTR's
gain normally. Until it is explained, **no v5 run's level depends on its `-g`**, and the 385.04 MHz
trunked carrier clips the v5 on the milair span. Next check: SDR++'s gain slider on the v5, AGC off.
Details in RUNSHEET V1-1.

*Resolved the same morning: the fault is rtl_power, not the dongle or the dll.* SDR++, `rtl_sdr` and
`rtl_power_fftw` all move the 385.04 carrier ~+29 dB for g7.7 -> g40.2; rtl_power +4.7 even on one
hop. Cause (tool session): rtl_power sets the gain, then its sample-rate call tunes to 0 Hz, which
switches the R820T into automatic HF direct sampling; the first real tune re-initialises the tuner
and leaves it on automatic gain. A second bug hits both radios: rtl_power's per-bin sums are 32-bit
on Windows and overflow on strong carriers, giving `-nan(ind)` or a silently low value - so the
**"NaN = saturation" readings above (FM checks 2026-10-16/18) are at least partly rtl_power
overflowing**, and peak levels near them may read low. Fixed build `rtl-sdr/rtl_power_gainfix.exe`
(+28.0 dB, matching rtl_sdr), source in `packaging/rtl_power_gainfix/`; on the XTR it matches the
stock build to 0.05 dB on the floor. **marsad runs it for every rtl_power scan since 2026-10-27**,
see "Which rtl_power marsad runs" under Existing code. Every archived v5 rtl_power run was at
g29.7, so none had tested gain.

**V1-1g (2026-10-27/28): the patched rtl_power keeps the v5's gain across the full milair span.**
A/B/A g29.7 / g40.2 / g29.7, 5 min each: 385.03 carrier **+10.88 dB** for a 10.5 dB step, every
25 MHz block +6.8 to +10.3, **zero non-finite readings**, A2-A1 -0.70 on the carrier (cold-start
drift). The v5's floor rises nearly dB for dB, so unlike the XTR it is input-noise-limited at
g29.7: extra gain buys little SNR. Standing v5 gain stays **g29.7**. RUNSHEET V1-1g.

**V1-2 / V1-3 (2026-10-28): the v5's milair twin, load then antenna, g29.7 gainfix** (5.4 h apart,
V1-3 cold: up to ~+1 dB bias). The antenna adds +9 to +10 dB at 250-300 (the 270 hump), +4.5 at
225-250, but only **+1.4 at 316-349** and +0.8 to +1.6 above 350: not deaf, but receiver-noise-limited
without an LNA. Load floor flat to **0.26 dB** (V1-1b's 2.6 dB shape was the stock build's AGC); no
350 MHz step; 385.04 control +24 dB. **240, 264, 288, 300 and 360 MHz vanish with the load**: they
are received from room/PC equipment, not v5 clock spurs; the v5's only own line here is 259.20
(28.8 x 9). RUNSHEET V1-2/V1-3.

**V1-4f, 7 h daytime milair map on the v5 (2026-10-27, 09:33-16:33, rtl_power_fftw g40.2).** The
v5 **hears 316-349 MHz** (+2.3 dB over its quietest 351-400 block, an intermittent ~200 kHz emitter
at 341.31 MHz, bursts at 319.51 and 303-316), no 350 MHz step (0.18 dB), 385.03 control steady
100%. 296.99 switched off once at 11:03:22; 259.58 stayed on all day; 262.66 was intermittent -
so M1's "one local source" reading does not repeat. Terminated twin still owed. RUNSHEET V1-4f.

**Wall mount, A/B/A (2026-10-28, 20:34-23:52): equivalent, the comparison set stands.** v5 in its
aluminium cage moved from the desk into the 3D-printed wall slide-in mount (rotated, ~2 cm higher),
nothing else changed; `milair` g29.7 gainfix. A1 desk 95 min (last 30 min used), B wall 40 min, A2
desk 40 min (5 min skipped each), and the first 35 min after skip of the night map as B2 (wall).
B started after an isolator power-cycle (the move dropped the dongle off USB, 21 min after A1
ended); A2 2.5 min after B, the map 16 min after A2. In the blocks the antenna barely reaches
(351-380, 380-400 MHz), **B minus mean(A1, A2) +0.12 / +0.06 dB and B2 minus A2 +0.02 / +0.09 dB**,
inside the 0.5 dB handled floor and opposite in sign between pairs. The received blocks 225-349
fell 0.6-1.7 dB from A1 to A2 with the radio on the desk both times (the evening environment), and
B sits between A1 and A2 in every one of them (B - meanA -0.35 to +0.35). Lines, including the v5's
own 259.20 spur and the 385.03 control, moved within 1-2 dB with mixed signs, inside the 3.4 dB
per-line handling scatter. **The mount costs nothing measurable**: the wall is the v5's standing
position from 2026-10-29, chain text "in the 3D-printed wall-mounted slide-in mount*", with the
asterisk meaning "equivalence-tested 2026-10-28 against the desk, same comparison set".

**V1-4n, 12 h overnight milair map on the wall (2026-10-29, 00:08-12:08, g29.7 gainfix), scored
against the predictions written before it:**

| Prediction | Result |
|---|---|
| 385.03 MHz in >= 95% of sweeps | **failed as worded, 91.4%**; in every one of the 127 sweeps it was absent, 385.76 was on (median +23 dB). The trunked control channel hops, as M1 saw; the system was present 100% |
| 259.58 MHz on all night | **inconclusive**: a weak line, 4-6 dB over its +/-1 MHz median (empty controls nearby -0.8 to +0.8), lowest 02:20-05:10 |
| 296.99 MHz on, off only after sunrise | **held**: on all night, one switch-off at **11:44**; V1-4f saw it go at 11:03 the day before, so it is a late-morning off, not sunrise |
| 270 MHz hump lowest 01:00-05:00, then rising | **refuted**: 250-300 was at its **highest** 02:20-05:10 |
| room lines step with display sleep | **not testable**: `user_idle_s` is recorded only at start and end |
| 316-349 1-1.5 dB over the load floor (V1-2) | **exceeded**: +2.09 dB (V1-3 read +1.4 by day); 351-380 +1.53, 380-400 +0.54, 250-300 +8.7 |

**The night's one event: a broadband emitter on 02:19:45-05:12**, a clean step of +1.0 to +1.4 dB
across 225-349 MHz, +0.2 at 351-380 and nothing at 380-400. It switched on with a one-sweep dip
just before the rise and went off through two dips and a brief overshoot between 05:07 and 05:12,
the signature of something with its own power stage cycling. The weak "lines going off" in that
window (259.58, 262.66, 300.0) are the floor rising under them, not the lines stopping. Source
unidentified; 2 h 52 min starting at 02:19 fits a scheduled task (the PC's automatic maintenance,
a heating cycle, an appliance timer) but nothing has been tested. Another night with the same
window would say whether it repeats on a schedule.

**USB power on the v5 path (2026-10-29), inline USB meter, idle / streaming (`rtl_test -s 2400000`):**

| Meter position | Idle | Streaming |
|---|---|---|
| PC port -> isolator | 4.975 V, 0.276 A | 4.975 V, 0.386 A |
| isolator output -> 16 ft cable | 5.041 V, 0.055 A | 5.049 V, 0.126 A |
| 1 ft M:M -> cage USB bulkhead | 5.000 V, (0.000 A) | **4.941 V**, (0.000 A) |

The zero currents at the bulkhead are the meter mounted backwards (power enters its socket side,
and it measures one direction only); position 2 is the dongle's current, since the cable draws
nothing. **The v5 gets 4.94 V streaming, inside USB's 4.75-5.25 V.** It draws only **0.126 A**
streaming (predicted 0.25-0.35, refuted). The isolator is the big consumer: 0.64 W out for 1.92 W
in, about 33% efficient, ~1.1-1.3 W of its own. The 17 ft from isolator to bulkhead drops 0.108 V
at 0.126 A, **about 0.86 ohm of loop resistance**, so a dongle drawing 0.3 A would see ~4.79 V and
0.4 A ~4.71 V, below spec. The XTR (E4000 plus an always-on bias-tee feeding the LaNA) draws more
than the v5, so **its path is the one that can sag**; meter it at position 2 when it is reconnected.
Two `rtl_test -s 2400000` runs through the meter: **0 samples per million lost** each, so the
meter passes high-speed data cleanly and the v5 path is sound on both power and data. Incidentals:
`rtl_eeprom` shows the v5's **"Bias Tee always on: yes"** flag set, which does nothing on a board
with no bias-tee circuit (0.1 mV measured 2026-10-16; leave the EEPROM alone); and rtl_test's
"Enabled direct sampling mode, input 2" is the R820T's automatic HF mode at 0 Hz, harmless here.

**XTR USB path (2026-10-29), its own route with no isolator: PC > 16 ft USB > new 1 ft M:M > cage
USB bulkhead, ~5.9 m.** Meter at the PC port (the only joint on this route where it reads current
the right way round): idle **5.000 V, 0.114 A**, streaming **4.966 V, 0.198 A**; `rtl_test -s
2400000` 60 s, **0 samples per million lost**. XTR SMA input open (nothing attached). Bias-tee feeding nothing (in the two-radio plan the
LaNA runs from its own USB-C and the XTR arm has a DC block), so 0.198 A is the operating figure;
even at the v5 path's 0.86 ohm the dongle would see ~4.80 V, inside spec. The meter reads current
in one direction only: plug into the side power comes from, socket toward the dongle.
**Both radios streaming together** (v5 on the isolator as device 0, XTR direct as device 1, two
`rtl_test -s 2400000` at once, ~60 s): **0 samples per million lost on each**, so the PC carries
two 2.4 MS/s streams. The second window listed device 0 as `, , SN: Ç`: a dongle already open in
another program cannot have its USB strings read, so it shows garbage. Normal, not a fault.

**Splitter A/B/A on the XTR (2026-10-29/30), `milair` g19, 230 mm vertical dipole on the right
window, XTR on foam at its future wall spot.** A1 `xtr-dipole-baseline-wallspot` 19:13 (60 min),
B `xtr-split-B-before-lana` 21:21 (40 min), A2 `xtr-split-A2-direct` 00:02 (40 min), 10 sweeps
skipped each. B put a resistive 2-way divider (3 x 16.7 ohm, 6 dB per arm) between the antenna and
the XTR's notch, the other arm to the v5 (connected, not streaming) through a DC block. **The LaNA
cannot be powered through this divider** (the bias-tee voltage collapses across the resistors and
its LED goes out), so in this test it sits after the divider on the XTR arm.

| Block (median over sweeps) | A1 | B | A2 | A2 - A1 | B - mean(A) | predicted |
|---|---|---|---|---|---|---|
| 225-310 | -23.59 | -28.78 | -23.34 | +0.25 | **-5.32** (floor -6.44) | -4 to -6: held |
| 250-300 (270 hump) | -22.40 | -30.01 | -21.88 | +0.52 | **-7.87** (floor -8.78) | more than the divider can cost |
| 316-349 | -40.47 | -44.32 | -40.58 | -0.11 | **-3.80** | none |
| 351-380 | -31.52 | -35.53 | -31.20 | +0.32 | **-4.17** (floor -4.58) | -2 to -4: held at the edge |
| 380-400 | -36.30 | -38.14 | -35.58 | +0.73 | -2.20 | none |
| 385.0 carrier, per-sweep max | -8.9 | -9.7 | -5.3 | **+3.6** | -2.6 | -6.0 to -6.5: **bracket failed** |

**The bracket holds on the floor** (A2 - A1 within 0.75 dB in every block) and **fails on the
carrier**: steady within each run (p10-p90 under 2.5 dB) but 3.6 dB higher at midnight than at
19:00, and only 0.8 dB lower in B than in A1. The trunked transmitter changed its own level
between runs, so the carrier says nothing about the divider. Read the divider on the floor:
**it costs 4-5 dB of received noise below 400 MHz on the XTR arm**, the size expected from a 6 dB
divider feeding a chain whose floor is partly the LaNA's own noise.

**250-300 fell 7.9-8.8 dB, more than a 6 dB divider can take off a power sum.** Not explained.
Two candidates, untested: the 270 MHz hump is a switching local source that M1 saw step down
abruptly, and it may have been low during B only; or the short dipole's mismatch transfers
differently into the divider's resistive 50 ohm than into the LaNA directly.

**New: at the wall spot the XTR hears 316-349 MHz.** M1 (2026-10-26, desk position, old USB route)
read -43.96 there, equal to its terminated twin M1-T (-43.88), and that was the basis for "the
XTR is nearly deaf across 315-350". A1 and A2 read -40.5, **3.5 dB over that terminated level**,
and B drops 3.8 dB back onto it, so the excess arrives through the antenna port. 351-380 is also
2.8 dB above M1. What changed since M1: the XTR's position (wall spot), its USB route (no isolator,
16 ft direct), and the room (v5 on the wall, the 02:19 broadband emitter of V1-4n also covered
225-349).

**Terminated twin at the wall spot (`xtr-wallspot-term-g19`, 2026-10-30, load > DC block on the
notch input, 40 min): matches M1-T to 0.2 dB in every block** (225-250 -39.98 vs -39.78, 250-300
-40.18 vs -40.00, 316-349 **-44.06 vs -43.88**, 351-380 -41.62 vs -41.44, 380-400 -41.72 vs
-41.56). The new position and the direct USB route without the isolator add nothing conducted.
**So the 3.5 dB at 316-349 is received through the antenna**: the XTR is not deaf there, it
just had nothing to hear on 2026-10-26. Something in 316-349 (and +1.5 to +2.8 dB at 351-400) is
present now that was not during M1, in both A1 (19:13) and A2 (00:02), so steady rather than the
02:19 event. A terminated run cannot see emissions that reach the antenna through the air, so a
local source is not excluded: candidates are the v5's isolator (a ~1.2 W DC-DC converter, now on
the wall), the XTR or its new USB cable radiating from the wall spot, or a new source outside.
Next, antenna on with one candidate unplugged at a time, 40 min each, against A2.

**Isolator pair (2026-10-30, 07:33 and 08:21): the isolator is not the source, and both runs were
through the splitter.** `xtr-wallspot-noisolator` (isolator and v5 unpowered) and
`xtr-wallspot-isolator-A3` (both back on) agree to 0.6 dB in every block, so switching the isolator
changes nothing. Both read 6-9 dB below A2, which looked like an environmental drop until it turned
out the antenna had gone back **through the splitter** after the terminated twin (v5 arm without its
DC block, harmless: the LaNA blocks the bias-tee DC). Their levels match splitter run B within about
1 dB, so the "drop" was the divider. Both sidecars carry a `chain_correction` and
`chain_order_actual`. The 316-349 finding is unchanged: direct, the XTR hears ~3.5 dB there; through
the divider it falls back to the terminated level. Source still unidentified; the isolator is cleared.
A3 also spanned a thunderstorm (two broadband one-sweep lifts, 08:26:55 and 08:36:26, cause not
established).

**v5 arm of the splitter (`v5-split-arm`, 2026-10-30 09:13-11:13, `milair` g29.7 gainfix, 65 min
warm-up discarded, 110 sweeps analysed; XTR on the other arm powered, not streaming).** Scored
against V1-2 (load, warm) and V1-3 (antenna direct, cold):

| Block | split arm over load | direct over load | predicted for the arm |
|---|---|---|---|
| 225-250 | +1.17 | +4.50 | (power sum: +1.6) |
| 250-300 | **+3.07** | +9.21 | +3 to +4: held |
| 316-349 | **+0.89** | +1.43 | within 0.3: **failed** |
| 351-380 | +0.21 | +1.58 | within 0.5: held |
| 380-400 | -0.06 | +0.67 | within 0.5: held |
| 385.03 carrier over local floor | **+21.1**, on in 100% of sweeps | +23.9, 80% | +17 to +18: exceeded |

The 316-349 excess is not spread across the block: it is +1.0 to +2.0 dB at 316-331, falling to
+0.1 at 340-349. The bottom of the block is the skirt of the received energy below 316, and the
top 10 MHz is on the load floor. So the v5 arm hears **no new 316-349 source**, unlike the XTR
direct at the wall spot. The 270 hump fell about 7.6 dB through the divider (power-sum
prediction +4.6, measured +3.1), the same over-6 dB loss the XTR arm showed at 250-300 (7.9-8.8),
now on both arms on different days, so it looks like the divider and the short dipole, not the
hump switching. The carrier lost only 2.7 dB, but the trunked transmitter's level moves 3-4 dB
between days, so it says nothing about the divider. The settled whole-band median was flat from
10:04 (-41.92 to -41.95), so the 65 min discard was enough. **Verdict: the v5 arm works as a
splitter arm should, about 6 dB down with no LNA, adequate for trigger-and-record on what the XTR
finds, not for surveying weak signals itself.**
**For the two-radio plan:** the divider costs the XTR 4-5 dB of floor, plus whatever the LaNA's
position costs, and the LaNA cannot sit ahead of it on bias-tee power. Two antennas (dipole on the
v5, UWB on the XTR) remain the better setup for independent surveys; the divider is for
trigger-and-record, where the v5 only needs to hear what the XTR already found.

**First two-radio watch, `watch2-4h` (2026-10-30, 13:12-17:12, `marsad watch survey milair 4h`,
XTR g19 through the splitter, v5 recorder g29.7 on the other arm, room untouched).** 480 survey
sweeps, not interrupted. **67 events, 33 IQ captures written, 2.0 GB.** Against the predictions:
296.99 and 259.58 MHz triggered and were recorded (4 times each, AM-like): **held**. Every
380-400 MHz event, 385.03 included, was fingerprint-only, 30 private rows and **zero IQ files**:
**held**. No trigger on a clock line or hop centre: **held** (299.999 is the exempt reference;
the nearest triggers to hop centres, ~17-22 kHz off, were confirmed by the v5, which has no DC
spike, e.g. 246.90 MHz at 23 dB). Storage under 10 GB: **held**, but **1.94 GB of the 2.0 came
from one signal recorded 9 times in 6 min**: a ~650 kHz-wide hump at **345.2-345.8 MHz** from
16:57 to ~17:07 (survey: 345.55 MHz, rare burst, one sweep over floor+10; v5: sparse ~200 us
pulses, 0.002% duty, groupings near 10 Hz and 1 Hz). Each pass its peak bin landed somewhere
else in the hump, and the 30 min cooldown is keyed per frequency at 25 kHz, so every pass looked
like a new signal and got a 216 MB capture at 2.4 MS/s. **Fixed in b77319c the same evening:**
two triggers are one signal when their occupied bands overlap, |df| <= max(25 kHz, (w1+w2)/2),
for the cooldown (in a run and after a restart), the 2-pass counter and the dispatcher; pinned
in `tests/test_watch.py` on a 651 kHz hump wandering +/-300 kHz (one capture in 30 min). The hump sits inside 316-349 MHz, the band the XTR hears ~3.5 dB
over its terminated floor at the wall spot; whether it is that source is untested (it was
present ~10 min of 4 h, and the excess is steady). Other open-band repeaters: 313.30 (x5),
307.36 (pulsed / OFDM-like, x3), 368.99, 370.89, 371.89, 360.64, 237.36, 298.90, 246.90.

### Printer room catalogue P1 (2026-10-26, 21:04-22:48): nothing in 52-1660 MHz, in any state

Six wide runs, stub A (blade) taped at the back middle of the case: unplugged, off, idle,
printing, idle, unplugged. Full table in RUNSHEET P1. **Off, idle and printing add nothing**
against the closing unplugged run. The 8-13 dB TV-band "rises" the catalogue first reported came
from the **opening** unplugged run: the broadcast environment changed between 21:04 and 21:30.
The bracket caught it. A probe on an 8 ft coax hears the room's broadcasters, so a probe
catalogue must always close with a repeat of its background state, and the catalogue should use
the later one. Printer motor and supply noise below 52 MHz is untested (v5 HF direct sampling
with a loop probe would cover it).

### Tool limit, not a station effect

- **rtl_power stops at 2147.483648 MHz (2^31 Hz)** — it holds frequencies in a signed
  32-bit int. Measured 2026-10-13: `-f 1241M:2187M:50k` covered only to 2147.483311 MHz
  (337 Hz short of 2^31), and a range *entirely* above the ceiling (2150M:2180M) wrote
  **no file while still exiting 0**. Requesting ~2148M gets the maximum. `run_survey.py`
  refuses a range above it and warns when a top would be clamped. For 2147–2187 MHz use
  SDR++ or rtl_power_fftw, not rtl_power.
- **rtl_power cannot plan a span wider than 2^31 x (1 - crop) Hz = 1610.6 MHz at `-c 0.25`.**
  The same 32-bit int: before dividing the span into hops it scales the whole span by
  1/(1 - crop), and that product overflows. Measured 2026-10-20: `52M:1662.6M` plans 767 hops,
  `52M:1662.7M` plans "1 hop, 0 Hz bandwidth" and exits with `Invalid sample rate: 0 Hz`; at
  `-c 0.1` the limit moves to 1932.7 MHz as the formula predicts. **The `wide` preset
  (52M:1700M, 1648 MHz) could never have run** and is now `52M:1660M:100k`. `marsad run`
  refuses a wider span. Also measured on the way: wide is **766 hops** (not the 589 a crop-free
  estimate gives), so its retune floor is 36 s per sweep; at the old `-i 5` it would listen 0% of
  the time. The preset is now `-i 60` (40% listening). `L` (219 hops, 10.3 s floor) has the same
  problem at `-i 10` and has not been changed; use `-i 30` or more there.

---

## Existing code

**Packaged as `marsad` since 2026-10-20** (المرصد, al-marsad: the observatory; GitHub
kenogen-dev/marsad). The code lives in the `marsad/` package; `marsad run LS 1h` is the
command, and `sdr` is a second entry point for the same thing, kept so every command in the
notes and RUNSHEET still works. The `tools/*.py` scripts below are thin wrappers over the
package and keep their original command lines. `.venv\Scripts\python.exe -m pytest` runs 33
regression tests that pin the recorded figures; the frozen Windows build is
`packaging/marsad.spec` -> `dist/marsad/marsad.exe`. **A double-click on marsad.exe (or a bare
`marsad`) is the launcher (2026-10-22):** it starts the web app on 8731 and opens the browser;
if 8731 is already served it only opens the browser on it and lingers a few seconds so the
message can be read. *Changed 2026-10-27:* it no longer runs the app inside its own console
(the old "close this window to stop" banner); see "The web app stays up" below. Before this the busy-port refusal
made a double-click flash a console and exit. `Start marsad.cmd` beside the exe keeps the
window open on an error. `tests/test_launch.py` pins the dispatch. For testing a build without
touching the owner's address: `MARSAD_PORT=8745` moves the launcher and `MARSAD_NO_BROWSER=1`
keeps the browser shut. Found on the way, same day: `marsad live` had refused to start at all
since the rtl_power_fftw presets landed, because the Builder tab's config parsed every preset's
range with the rtl_power parser and `top` (`2147M:2187M`, no bin width) failed it; the builder
now skips presets for another engine. The viewer `marsad run` starts was dying the same way.

**The web app stays up (2026-10-27), `marsad/webapp.py`.** On 2026-10-26/27 the page kept
vanishing: it ran inside a console window that got closed when the desktop was tidied (which
also killed a Probe-tab scan, a child of the app), and sessions and `marsad run` started it
with a 5 min idle timeout, so there was no page between scans. Now `marsad run` (both
engines, so also `tools/run_survey.py`) and the launcher check 8731 before recording and print
one line: "web app: already up on 8731" or "web app: started on 8731, pid N". If nothing
answers they start `marsad live --port 8731` **in the background with no window and no idle
timeout**, working folder the station root, output appended to `reports/live_view.log`, and
confirm with a GET; if it will not start, the scan goes on and the line says so. A port held by
something that is not the web app is left alone. `--no-live` (which Probe-tab runs pass) skips
the check. The window is hidden with `CREATE_NEW_CONSOLE` + `SW_HIDE`, **not**
`DETACHED_PROCESS`: tested on this PC, a process with no console at all cannot send Ctrl-Break,
so Probe-tab scans could not stop rtl_power cleanly; with a hidden console they can, and no
window or Windows Terminal tab appears. Any other port started this way (a test viewer, e.g.
the launcher under `MARSAD_PORT=8745`) gets `--exit-after-idle 5` automatically.
`marsad web status` (up?, listener pid from netstat, follows newest?, Probe-tab state),
`marsad web start`, `marsad web stop` (kills the listener on the port; refuses during a
Probe-tab run unless `--force`), `marsad web restart [--force]`; all take `--port`.
`tests/test_webapp.py` pins the check-and-start decision, the launch flags and the refusal.

**Probe tab (2026-10-20)** in the web app (`marsad live`, port 8731): near-field probe surveys
of the room from the browser, one hand on the probe. `marsad/probe.py` holds the catalogue
(targets, the four H-field loops and two E-field stubs, the two ranges) and composes the
**tag, chain and conditions only** - the run itself is `marsad run` as a child process with
`--non-interactive --no-live --keep-defaults --stop-file`, so the sidecar, scan_build.md,
report and archive entry are identical to a command-line run. Ranges: `wide` (the preset) and
`hunt`, a 5 MHz tile at 5 kHz bins, `-g 42 -i 1`, shifted so the typed centre sits a quarter
hop above a hop centre (420 kHz clear of the DC spike). Stop writes a flag file the child polls
twice a second; the child asks rtl_power to finish, then proceeds exactly as after Ctrl-C
(partial CSV kept, analysed, archived). Each finished run appends a row to **`ROOM_RF.md`**
(when, target, probe, where held, range, duration, survey, detections, strongest non-artefact
line, tagged families) - the human-readable map of the room; the archive holds the data.
The Receiver picker (XTR / v5) asks each plugged-in dongle what it is with `rtl_eeprom`
and fills the device index from that, because USB numbering moves when a dongle is unplugged;
the chain after the probe follows the receiver and can be edited on the page before a run.
`--keep-defaults` exists so probe runs do not overwrite the antenna chain that a plain
`marsad run LS` falls back on.

**Two fixes from the printer catalogue (2026-10-26).** *Device index:* station.json's
remembered `device: 1` for the XTR kept coming back into the form after every run, and with
only the XTR plugged in (device 0) the run failed on `-d 1` blaming "another program". The page
now detects receivers (rtl_eeprom) when the tab opens, again before Start and before a Hunt
start, and again whenever a run lets go of the SDR; the device box takes only what detection
found or what was typed by hand since the page opened (kept until reload). station.json's
number and the browser's saved form are never used for it. If detection fails the box is left
empty, the page says why, and Start refuses; `compose` also refuses an empty device rather than
defaulting to 0. When rtl_eeprom cannot open the index, the message now names the cause: "No
receiver at index 1; detected: ... at 0" when the index is past the list, and it blames another
program only when the SDR-in-use check found one (`record.open_failure_message`). *Duplicate
probe:* `probe.clean_tail` drops any leading probe part from the chain after the probe (the
Chain tab's "near-field probe (held where the Probe tab says)" placeholder, or one typed in the
box) and every empty segment, so the recorded chain names one probe and never contains `> >`
or a leading `>`. `tests/test_probe.py` pins both, with rtl_eeprom mocked.

**Which physical probe is which (2026-10-26, owner's kit, six SMA probes):** large loop = the
big ring; medium loop = the hexagon; small loop = the small round loop; extra-small loop = the
tiny spiral; **stub A = the straight blade tip** (pure E-field, the "tipped probe"); **stub B =
the tip with the concentric arcs**. Names in ROOM_RF.md and the tags follow this map. The
Probe tab's labels say it too ("stub A (straight blade)", "stub B (concentric-arc tip)",
"extra-small loop (tiny spiral)"); the ids, tag slugs and chain wording are unchanged, so old
runs still match and hunt.py still reads their conditions text.

**Measured conditions in every sidecar (2026-10-26).** The typed `--conditions` text had gone
stale through copy-paste, so `marsad/conditions.py` (stdlib) now writes an `auto_conditions`
block into every sidecar from both engines (`marsad run`, the Probe tab, `tools/run_survey.py`),
with a `start` snapshot when the sidecar is first written and an `end` snapshot after recording.
Each holds: **weather** from Open-Meteo's free no-key API (temperature, humidity, surface
pressure, cloud, precipitation, wind, is_day, weather code; one request, 5 s timeout), **sun**
elevation/azimuth and day / civil twilight / twilight / night computed locally (NOAA formula,
~0.1 deg, no network), **pc** (Windows uptime, `user_idle_s` = seconds since the last keyboard
or mouse input, the cheap proxy for whether the displays have slept, which Windows will not
report cheaply; and `previous_recording`: the newest earlier sidecar's end time and run length,
because the XTR's PLL range drifts as it warms), and `indoor: null` until there is a room
thermometer. Weather and sun need **`"location": {"lat": .., "lon": ..}` in station.json**
(decimal degrees, west/south negative; the `_location` note there has an example); without it,
or offline, each part records the reason and the run carries on: nothing in it can fail a run.
`--dry-run` prints the start snapshot. report.md's settings table and scan_build.md show it as
"Conditions, measured at start/end" beside the typed text, which is unchanged (scan_build.md
labels it "Conditions (typed)"). Sidecars from before 2026-10-26 have no block and show no rows.
`tests/test_conditions.py` pins it with the network mocked.

**Chain tab (2026-10-20)**: the rig as a drawing. `marsad/chain.py` keeps named chains in
`chain.json` beside station.json, each a line of parts (source, adapter, DC block, coax, notch,
LaNA, attenuator, bulkheads, jumper, SDR, USB, isolator, PC, custom) with a name and a detail;
the page draws a chain as a rail you drag palette parts onto, drag to reorder and click to edit,
and renders the `--chain` text live. The three seeds reproduce the recorded chain text word for
word. "Use for runs" makes a chain current **and writes its text into station.json's default
chain_order**, so a plain `marsad run` records it; the Probe tab takes its chain after the probe
from the current chain when the receiver matches. Each chain carries `updated` and `last_used`
stamps: a probe run started from the page stamps the current chain, saving a change clears the
stamp, and starting a run on a chain that is unchanged since its last run asks "still built as
drawn?" unless the chain is ticked **static**.

**The command line has the same guard (2026-10-21).** Nine `nsrc-*` runs on 2026-10-20 recorded
a stale chain because a run without `--chain` silently copies station.json's default. Now a run
that records the default chain stamps `defaults.chain_used_at`, and the next `marsad run` (or
`tools/run_survey.py`) without `--chain` prints that chain and stops, before any hardware check,
unless `--chain-as-saved` says the rig is unchanged, `--chain "..."` gives the new rig, or the
Chain tab's current chain renders to that text and is ticked static. On a terminal it asks
y/N instead of stopping. "Use for runs" on the Chain tab clears the stamp (choosing a chain is
a fresh statement). `--dry-run` shows the chain and its source without the guard. Header fields (antenna, whips, LNA, filter) in a probe sidecar
come from station.json defaults, as for any `--non-interactive` run; `chain_order` is the truth.
The probe chain goes through the DC block into the LaNA at the window, never a loop straight onto
the XTR's SMA (bias-tee always on).

**Hunt mode (2026-10-21)**, a panel inside the Probe tab, for walking a near-field probe
around without watching the screen. `marsad/hunt.py` (stdlib) does step 1: two finished
**wide** probe runs with the same fingerprint - open air, then at the device - are subtracted
**bin by bin on `median_db`** (median over sweeps, so a one-sweep click drops out) and the
differences pooled into blocks (5/10/20 MHz), ranked by the bin that rose most; the block's
median rise is shown beside it, since a broadband emitter lifts that and a single line does
not. Whatever both runs share - clock harmonics, hop-centre DC spikes, the room's broadcasters
- cancels. Two open-air backgrounds 100 min apart still differed by **13 dB** in the UHF TV
and 700 MHz LTE blocks (measured 2026-10-21), so **run the pair back to back**. Step 2 starts
an ordinary `marsad run` through the probe path on the new `tile` range (**101 MHz, 25 kHz
bins, g29, `-i 3`**: 49 hops, 2.3 s retune floor, 23% listening) or the 5 MHz `hunt` tile,
tagged `hunt-<target>-<probe>`; the page reads the run's sweeps from `/data` and turns the
strongest line in the tile (or the typed centre) into sound with the Web Audio API: **one
octave per 10 dB** over the sweep's floor (110 Hz at the floor), or Geiger clicks doubling
every 6 dB. Columns within +/-25 kHz (at least two columns) of every hop centre are excluded
from "strongest", because the E4000 DC spike's skirt is ~+/-20 kHz wide at 8 dB over the
floor and would win otherwise. Step 3, "Narrow", stops the tile and restarts the 5 MHz hunt
tile on the followed line once the tile's analysis has finished (the child process holds the
SDR until then). Endpoints: `GET /hunt/reports`, `GET /hunt/candidates?bg&dev&block`; a
fingerprint mismatch is refused in plain words. While a run started from the page records,
the server reads its CSV twice a second instead of every 2 s.

**Hunt mode after its first shakedown (2026-10-26).** The owner's first hand-held hunt
(`survey_deep_10m_hunt-printer-unplugged-stub-a_20261026-2305` tile, `..._custom_10m_..._2316`
5 MHz hunt; sidecars carry a `conditions_correction`) found four faults, all fixed:

- *Too slow to follow by hand* (a reading every 3 s on the tile, 1 s on the 5 MHz hunt, which is
  rtl_power's minimum). New range **`live`**: one rtl_power_fftw hop, 2.4 MS/s, 512 bins
  (4.69 kHz), `-t 0.1`, g29, tuned **0.6 MHz (a quarter hop) below the target** so the line sits
  between the DC spike and the cropped edge (`probe.live_spec`). One hop never retunes between
  spectra, so a pass is 0.1 s + the 59 ms overhead = 0.159 s. **Measured on the radio
  2026-10-26 23:40** (XTR, 855.6 MHz, scratch station, test viewer on 8745): **5.84 spectra a
  second reach the page**, a new reading every 160 ms (max 334), none skipped; ~4.2 MB of
  `.dat` per minute. `DatTailer` closes a one-hop pass the moment its block ends instead of
  waiting for the next block to wrap it. The page asks `GET /hunt/reading` every 150 ms on
  `live` (500 ms on the tiles) and shows the measured rate beside the planned one
  (`updates_per_s`, from when sweeps reached the server, 5 s window). The tailer's file
  reading now holds `poll_lock`, so the request handler and the background loop can both read.
- *The strongest-line follower jumped between unrelated carriers* (855.6, 806.4, 864.0, 888.0,
  891.0, 903.8 MHz). `hunt.Follower`, server side, stepped once per new sweep however often the
  page asks: it stays on its line until another is **6 dB stronger for 3 sweeps in a row**, or
  its own line has been under 3 dB over the floor for 3 sweeps; it tracks a drift of up to
  max(2 bins, 15 kHz) but never onto a noise bin. **🔒 Lock line** pins it (a faded locked line
  keeps its frequency). Bins within max(1.5 bins, 25 kHz) of a hop centre never win.
- *Broadband emitters were invisible* (blade on Bluetooth headphone cases and Wi-Fi bulb bases:
  the tile median jumped from -31 to -15/-18 dB, so line-over-floor barely moved or fell).
  Follow mode **floor level** turns the span's median, against the quietest median of this run,
  into the tone. The HUD always shows both: line over the floor, and floor level with its rise.
- *Hunt runs inherited the catalogue form's target, state and held text.* Every Hunt-panel start
  opens a box showing the target, state and held text the run will record, held defaulting to
  **"hand-held, moved around"** (`probe.HUNT_HELD_DEFAULT`, also compose's default for any tile
  or live run with no held text); "Narrow" reuses what was confirmed. `ProbeRunner.start`
  refuses a tile/hunt/live run without `confirmed` in the form; the catalogue form's own Start
  sends it, being pressed beside those fields.

**The visual meter (2026-10-26, same night)**, for reading the Hunt panel at arm's length with the
probe in hand. It is drawn in the same call that sets the tone, so sight and sound always agree,
and it uses the tone's scale (0-40 dB of the driving value). **⛶ Big meter** (Esc to leave) fills
the window: a huge number (line over the floor, or floor level in floor mode), the followed
frequency with 🔒 when locked, a thick bar, and a background that runs cool to hot (blue at 0 dB
to red at 40) so it reads from the corner of the eye. A **peak-hold mark** on both bars and a
**best so far** value with **Reset best** (also reset by a new run or a new follow mode) show
where the maximum was during a sweep of the probe. A **45 s history strip** shows the driving
value as a trace coloured on the same scale, with the best as a dashed line: "warmer / colder"
as the probe moves. A **mini spectrum** of the latest sweep (360 columns, max per column, from
`hunt.spectrum_strip`, sent in `/hunt/reading`) marks the followed line (orange when locked),
the challenger waiting to take over (dashed), the floor (dotted) and the DC-spike zones the
follower never picks (grey), so a jump to another line is obvious. Plain canvas, no libraries;
colours come from the page's CSS variables (the page has only its dark theme). Held text quick
choices on the confirm box (`probe.HUNT_HELD_CHOICES`): "hand-held, moved around", **"hand-held on
a wand, moved around"**, "hand-held on a wand, held still at one spot" (the non-conductive wand
keeps the hand and arm out of the near field). Checked end to end on the radio through a test
viewer on 8745 against a scratch station: 5.9 updates a second on the page, line locked at
855.638 MHz. That check also found that the run's status never named a `.dat` (a stray control
character in the file-name pattern), now fixed and pinned.

**The XTR dropped off USB during that check (2026-10-26 23:51:57)**, 88 s into the third live
run of the evening (two earlier runs of 1 and 2 min were clean). libusb error -4 (no device);
afterwards Windows listed the XTR as not present and detection found no RTL-SDR. Cause not
known: a USB/isolator dropout under continuous 2.4 MS/s streaming is the obvious suspect and is
untested. **rtl_power_fftw does not exit when its dongle vanishes**: it loops on `rtlsdr_write_reg
failed with -4` / `dropped samples`, ignores `-e`, and wrote **580 MB of .log in three minutes**.
`rpf.run_recording` now checks the log's tail at least every 2 s (`rpf.device_lost`: 20 failed
register writes in the last 4 KB, or a log over 20 MB), stops the program, keeps what was
recorded and exits with code 3 and a message to replug. Pinned in `tests/test_hunt_follow.py`.

`tests/test_hunt_follow.py` pins the live geometry, the confirmation, the follower's hysteresis,
lock and floor mode (on the shakedown's -31 -> -15 dB numbers), the one-hop pass close and the
once-per-sweep stepping.

**Device state and the room catalogue (2026-10-26).** The Probe form has a **State** picker
(unplugged / off / idle / active, or free text such as "printing"). It goes into the tag
(`probe-printer-idle-stub-a`), the conditions (`device state idle`, which `hunt.report_info`
reads back) and a **State** column in ROOM_RF.md; rows from before the column carry an empty
cell. `marsad/room.py` (stdlib, on `hunt.rank`) backs the "Room equipment catalogue" panel:
pick a device and every state is differenced bin by bin on `median_db` against the device's
own newest **unplugged** run (or any chosen background), in a sentence ("printing adds 15.0 dB
at 120.31 MHz over unplugged; 3 blocks rise 6 dB or more"), with a warning when the probe was
held somewhere else. "Loudest devices" ranks each device's active state (anything but
unplugged/off/idle) against its own unplugged run of the same fingerprint, and lists devices
still missing a run. A fingerprint mismatch is refused per run in a sentence. Hunt tiles are
left out. Endpoints `GET /room/targets`, `/room/compare?target&bg&block`, `/room/loudest?block`;
`tests/test_room.py` pins it on a synthetic printer.

`marsad response` (2026-10-21) - an antenna's return loss and SWR at every bin from three
runs through the return-loss bridge with the noise generator on its input: `open` (nothing on
the test port, total reflection, the 0 dB reference), `load` (50 ohm on the port, the bridge's
own leakage, i.e. its directivity and the deepest return loss it can show there) and `antenna`.
Per bin on the **median over sweeps**: `rl = open - antenna` (clipped at 0), `rho = 10^(-rl/20)`,
`SWR = (1+rho)/(1-rho)`, `floor = open - load`. Each bin carries a trust flag: `at-least` when
the return loss is within `--margin-db` (6) of the bridge's directivity or the antenna reading
is within that margin of `--rx-floor-db`; `dropped` within 25 kHz of a hop centre or in the
PLL gap. A running median over `--smooth-mhz` (1) gives the smoothed columns; raw stays in
`response.csv`. The three fingerprints must match, as for compare-runs, or it refuses in a
sentence. Writes `response.csv` (every bin), `response.md` (dip, best SWR, 2:1 bandwidth over
trusted bins, the range the bridge vouches for at >= 15 dB directivity, flagged fractions,
offset from `--f0`) and `atlas.json` for the Antenna Atlas import, thinned to 800 points
with the dip bin always kept. `tests/test_response.py` pins the arithmetic on a synthetic trio.

**Weak bridge (2026-10-26).** When the bridge vouches nowhere (directivity under 15 dB at every
bin) or under 5% of bins are trusted, response.md no longer names a dip: it says "dip not
resolvable with this bridge" and gives where the raw smoothed return loss is highest, marked
AT THE BRIDGE'S CEILING. (Before, it named the best *trusted* bin, i.e. the best of the
worst-matched bins: the 1265 GP's "dip 1462.8 MHz, SWR 4.30".) Every bin also carries the true
SWR's range, `swr_lo` / `swr_hi`: the leak adds with unknown phase, so the true reflection is
`rho_m +/- 10^(-floor/20)`, clipped to 0..1. response.md shows it at the dip or ceiling and at
`--f0`. On the GP trio, 1241 MHz reads SWR 1.00 to 2.9-3.0: the bridge cannot tell that
antenna from a perfect match there.

**`--rx-floor-db` is a number you supply, not the run's floor column**, because with the
generator on the analyser's floor (20th percentile over time, rolling median over frequency)
sits on the trace itself; it is the generator, not the receiver. Take it from a terminated run
with the generator off (-37.5 dB at g29 on the XTR). Without it that flag is simply off, and
response.md says so.

```
py -m marsad response --open <tag|csv> --load <tag|csv> --antenna <tag|csv>
    [--name "2130 GP v1"] [--f0 2130] [--smooth-mhz 1] [--margin-db 6]
    [--rx-floor-db -37.5] [--skip-sweeps N] [--out reports/response/<name>_<stamp>]
```

**Which rtl_power marsad runs (2026-10-27).** `rtl-sdr/rtl_power_gainfix.exe`, the patched
build that keeps the v5's gain and sums power in 64 bits (see V1-1 above), for every rtl_power
scan: `marsad run`, the Probe tab, `tools/run_survey.py` and `marsad calibrate`. If it is not
installed, marsad runs the stock `rtl_power.exe` and prints one WARNING line. To choose the stock
build on purpose, put `"rtl_power_exe": "rtl_power"` in station.json (a name, or a path relative
to the station folder). `marsad config` and `--dry-run` name the program and its build. Every
rtl_power sidecar now records `exe`, `build` (`gainfix` or `original`), `sha256` and `tuner` in
its `rtl_power` section; scan_build.md and report.md show it as a "Program" row. Sidecars from
before this say "build not recorded" and were all the stock build. A running
`rtl_power_gainfix.exe` counts as holding the SDR.

**The fingerprint rule:** a **v5 (R820T/R860) run made with the gainfix build gets `| gainfix`
on the end of its fingerprint**, e.g. `225.000-400.000 MHz | 10.8507 kHz | g40.2 | i30 | c0.25 |
F9 | v5 | gainfix`, because every older v5 rtl_power run had its gain silently ignored and must
not be compared with a new one; `compare`, `archive new` and response refuse the mix. **XTR
runs keep their fingerprint unchanged** (`... | F9 | xtr`): the E4000 read the same under both
builds, so its history stays comparable, and the build is still in the sidecar. A v5 run made
deliberately with the stock build gets no token, so it matches the old runs, which is correct:
both ignored the gain. `tests/test_gainfix.py` pins the choice, the fallback, the sidecar fields
and the rule. **Not in the release zip:** `rtl-sdr/` is not in git and the frozen build ships no
rtl-sdr executables at all; a release user unzips the RTL-SDR Blog release and builds the gainfix
exe with `packaging/rtl_power_gainfix/build.cmd` (README says so), or lives with the warning.

**The triggered recorder (2026-10-28), `marsad watch`** (RECORDER_DESIGN.md phases 2 and 3;
`marsad/capture.py`, `marsad/watch.py`). Phase 1 is `marsad characterise <iq>` (2026-10-27): it
measures a recording (bandwidth, pulses, PRIs, chirp, rotation, modulation family) and writes a
recipe for the next capture. The recorder feeds it every capture.

```
marsad watch fixed --freq 1265.44M --receiver v5 --duration 1h [--width 50k] [--gain G] [--capture S] [--chain "..."] [--dry-run]
marsad watch survey <preset> <duration> --chain "..." --conditions "..." [--recorder-chain "..."] [--recorder-gain G] [--dry-run]
marsad watch list [--last 30]
```

- **fixed**: parks one radio on one frequency with `rtl_sdr` streaming to memory. Every time the
  band rises 12 dB over its own floor (judged per ~0.85 ms frame, so a radar pulse counts) an
  **event** is catalogued with a **fingerprint**: a power spectrogram at 20 ms x ~1 kHz
  (`.fp.npz`). In an open band the first event also starts an **IQ capture**: from 1 s before
  the event, the recipe's duration (30 s without one, 120 s at most), shifted so the signal sits
  at 0 Hz, filtered and decimated, written as SigMF (`.sigmf-data` complex int16 + `.sigmf-meta`
  with frequency, rate, gain, serial, ppm, chain, UTC start). Then at most one capture per 30 min
  per frequency. Each capture is characterised (`.characterise.md/.json` beside it) and the
  recipe is stored with its row; the watch then retunes from it (rate, offset off DC, duration,
  gain nudge). Offline on the saved radar file: 9 main-beam events in 104 s at 12.0 s, one 30 s
  capture (298 MB), recipe 1.79 MHz occupied, rotation 12.00 s, 90 s, 2.4 MS/s.
- **survey**: the XTR surveys with an ordinary `marsad run` started as a child (`--device` = the
  XTR's index today, found by serial, plus `--non-interactive --stop-file`; everything else you
  type is passed through, so give `--chain`). The survey and its fingerprint are exactly a plain
  run's. The watcher follows its CSV or .dat with the live viewer's tailer and fires the v5 on
  each completed pass: **12 dB over the pass's local floor on 2 consecutive passes**; never a
  clock family (14.4, 24, 25, 28.8, 100, 480 MHz), a hop centre or a hop-wide impulse; 30 min
  cooldown per frequency; the reference carriers 385.03 and 299.999 MHz once a day (exempt from
  the family tags); **ambush**: one pass at 20 dB or more parks the v5 there 10 min and records if
  it returns, pre-empted by any two-pass trigger. Ctrl-C asks the survey to stop via its stop file.
- **Private bands** (`recorder.band_tiers` in station.json: cellular 600/700/850/AWS/PCS, pagers,
  DECT and old cordless, private land-mobile including 380-400 MHz; anything not listed is open):
  **raw IQ never touches disk, not even a temp file**. There is no IQ writer for them at all; the
  stream lives in memory and only the 20 ms fingerprint is saved. The row is flagged
  `PRIVATE: do not decode`, plus `ANOMALY: review` when the fingerprint does not look like the
  band's service (a CW carrier of 3 kHz or less, a regular pulse train, or far narrower than the
  service). `tests/test_watch.py` pins this by watching every file opened for writing.
- **Catalogue**: a `captures` table in occupancy.db beside surveys/signals/bands, one row per
  event, open or private (time, frequency, SNR, bandwidth, duty, repeat, tier, flags, iq_status,
  file paths, rate, gain, ppm, chain, family, recipe). `marsad watch list` prints it.
- **Storage**: `recorder.pool` (default `recordings/captures`, relative to the station or an
  absolute path such as another drive), `pool_gb` 100, `daily_gb` 20. **Nothing is ever deleted**:
  when either is reached, events are still catalogued and fingerprinted, IQ stops, the console
  says so once and each row says "pool full" or "daily cap". Files go in one folder per day.
- **Radios by serial**: `recorder.serials` (XTR 08701076, v5 27825674); detection is rtl_eeprom,
  and rtl_sdr is given `-d <serial>`, never an index. `recorder.ppm` holds each radio's frequency
  error by serial (reported minus true, per million), 0 until measured; the survey's is taken out
  and the recorder's put in when tuning.
- Tests run with no radio: `tests/fake_rtl_sdr.py` replays a file to stdout in place of rtl_sdr,
  rtl_eeprom and the `marsad run` child are canned. The radar test uses the saved WAV when present.

- **Recipes carry over between runs (fixed 2026-10-28 afternoon).** Rows were matched on the
  measured centre within 20 kHz, and the radar's chirp centre sits 57 kHz above 1265.44, so the
  11:35 watch started from the defaults although the morning's capture had a recipe. Rows now
  carry `target_hz` (the frequency the recorder was asked for; added to old catalogues the first
  time a recorder opens them), older rows match inside their own occupied band, and with a recipe
  the radio tunes on the measured centre. Pinned in `tests/test_watch.py`. **The same bug hit
  within a run** (radio, 2026-10-28: the 12:06 capture of the 11:35 watch was again 30 s at
  1265.440): a new recipe restarts the radio and re-plans through the same lookup, which missed.
  Two more fixes found by the two-captures test: the measured centre was adopted only within
  max(20 kHz, half the occupied band) of the asked-for frequency, so a narrow signal 40 kHz off
  was retuned beside (now: within a quarter of the measuring capture's radio band); and the
  characteriser's repeat period could run away (-2.8e12 s on gated bursts, a parabolic
  refinement dividing by a near-zero curvature), giving a 10 s duration; now refined only on a
  true peak and clamped.

**Captures tab (2026-10-28), `marsad/catalogue.py`** (RECORDER_DESIGN.md phase 4), in the web
app between Archive and Probe. Read-only on occupancy.db (opened `mode=ro`, so it never migrates
or writes it) and on the pool. **There is no delete control anywhere.**

- **Strip**: pool used of `pool_gb`, today's GB of `daily_gb`, and every recorder's live status.
- **Live recorder status (required, 2026-10-28 afternoon).** The owner looked at the web app
  during a running `watch fixed` and concluded nothing was scanning: a fixed watch writes no
  survey file, so the Scan tab looked idle. Now every `marsad watch` (fixed or survey, however it
  was started) writes `recorder_status_<serial>.json` in the pool at every change and rewrites it
  every **2 s** from a small thread that never touches the radio or the stream: mode, receiver and
  serial, the current job (fixed watch / capture / ambush, asked-for and tuned frequency, rate,
  gain, tier), events so far and the last one (time, MHz, SNR), the capture in progress and when
  it ends, the next capture allowed (cooldown) or "next event may record", the ambush parking,
  survey passes, and when the watch ends. The page polls `/captures/recorders` every 3 s and shows
  a **"recorder running: ..." bar under the tabs on every tab, the Scan tab included**, and the
  full line in the Captures strip. **Freshness decides, not the state field**: a file not
  rewritten for 15 s means the watch was closed or killed (Ctrl-C, a power cut), and the page
  says "stopped reporting N s ago" instead of "running". A watch that ends normally writes
  `state: stopped` with its totals. Checked on a test viewer with a throttled fake watch, killed
  mid-run: the bar went away and the strip said why within 15 s.
- **List**: every event, **grouped by signal** (the asked-for frequency, else the measured centre;
  events within 25 kHz or half the occupied bandwidth go together), each group with its family
  from the characteriser, event and capture counts, peak and badges (OPEN, PRIVATE: do not decode,
  ANOMALY: review); click to open its events (time, MHz, SNR, bandwidth, duration, mode, iq
  status). Filters: date, MHz range, OPEN / PRIVATE / ANOMALY / with IQ written.
- **Event page**: the fingerprint for **every** event, private included; the row's facts; for an
  open event whose IQ exists (its own capture, or an earlier capture it fell inside) the IQ
  waterfall (max-hold per row so a pulse survives, dB over each bin's median) and the envelope
  (peak and mean dBFS), computed by reading the file one slice at a time (a 295 MB capture: 2.4 s,
  cached), the recipe, the characterise report, and chain / gain / serial / hardware / UTC start
  from the SigMF meta.
- **Exports**, open events only: **WAV for SDR++** (16-bit stereo I/Q, which is the ci16 data byte
  for byte behind a 44-byte header; named `baseband_<Hz>Hz_<HH-MM-SS>_<DD-MM-YYYY>_v5.wav`,
  SDR++'s own convention: its file source, checked in this PC's `file_source.dll`, opens `*.wav`
  and takes the centre frequency from the first `[0-9]+Hz` in the name; a non-whole cut rate is
  rounded in the header and the page says so) and **`.cf32` + SigMF meta** (`cf32_le`,
  `core:dataset` naming the .cf32) for Inspectrum, URH and GNU Radio. Copies go to an `exports`
  folder beside the capture; the SigMF capture stays the master and is never touched. **A private
  event gets no export or IQ controls, and `/captures/export` and `/captures/iq` refuse it on the
  server (403)**, also for a row that claims to be open in a band the tier table says is private.
- Endpoints: `GET /captures?date&lo&hi&badge`, `/captures/event?id`, `/captures/fp?id`,
  `/captures/iq?id`, `/captures/recorders`, `POST /captures/export {id, format: wav|cf32}`. `tests/test_catalogue.py`
  pins the list, the event pages, the slice-at-a-time read, the exports byte for byte and the
  private refusals, including through HTTP.

**Archiving captures (2026-10-30), `marsad/capture_archive.py`.** On the Captures tab every event
page has **Archive…**: keep a good capture for good without it counting against `pool_gb`. The dialog
asks what to take (this event; this capture and every event inside it; the whole signal group; an
event that owns a capture always brings the events inside it, so none is left pointing at nothing),
whether fingerprints also stay in the pool (default yes: small, quick to view) and whether exports go
too (default no: they can be made again from the SigMF; left out, they stay in the pool), plus a label
and a note. **Show what would move** lists every file and the MB it frees; nothing changes until the
amber **Archive: move N file(s) ...** button and a second confirm. One zip goes to
`recorder.archive` (default `recordings/archive`; absolute paths allowed; **refused if it is inside
the pool**), named `arch_<MHz>_<UTC>_<label>.zip`: the files at their pool-relative paths,
`manifest.json` (the catalogue rows as they were, sha256 and size per file, receivers, gains, chains,
the note) and `README.txt`. **Order, the only path by which anything leaves the pool:** write
`.zip.part`, re-open it and hash every member against the hash taken while writing, rename it, and only
then remove the originals, each first checked to be unchanged in size and age since it was zipped. A
failed check removes nothing from the pool and deletes only the half-written zip. A capture a running
recorder is writing (its status file's `capture.file`, while fresh) is refused, as is one with no
`.sigmf-meta` yet. Private-tier events archive as fingerprints and rows only; an IQ-like file of a
private row is never zipped, and the export / IQ 403 rules are unchanged. **The rows stay in
occupancy.db** with `iq_status` "archived" and a new `archive_path` column (added the first time an
archive is made); the list shows an ARCHIVED badge and an "archived" filter; a fingerprint that left
the pool is read back out of the zip. **Restore to the pool** verifies the zip, unzips each file
through a `.part` with its hash checked, refuses rather than overwrite a different file already in the
pool, gives the rows their old status from the manifest, and keeps the zip ("also archived").
The strip shows "archive N GB, not counted in the pool"; the pool figure drops by what left.
`today` (the daily cap) still counts what was written today, archived or not. Endpoints
`POST /captures/archive/plan`, `/captures/archive` (needs `"confirm": true`), `/captures/restore`.
`tests/test_capture_archive.py` pins it with no radio. (`marsad/archive.py` is the occupancy archive
of surveys, a different thing.)

**Second engine: rtl_power_fftw (2026-10-21).** `rtl-sdr/rtl_power_fftw.exe` is a native
Windows build of github.com/AD-Vega/rtl-power-fftw 1.0-beta2, made on this PC with the
Visual Studio Build Tools that were already installed (static FFTW 3.3.10, the same
rtlsdr.dll rtl_power uses); `packaging/rtl_power_fftw/BUILD.md` records every version and
hash, `build.cmd` there rebuilds it from scratch, and the one patch (four lines) makes
Ctrl-Break finish the pass the way Ctrl-C does, so marsad's Stop button works on it. It
exists for three things rtl_power cannot do: **record above 2147.48 MHz** (frequencies are
64-bit), **bins as narrow as you like** (any even FFT size, 1.17 kHz at 2048 bins) and
**baseline subtraction** (`-B FILE`, N values by bin index, so single-hop runs only). What it
does not have: crop, FIR, a built-in window (`-w FILE` takes N values you supply), offset
tuning, bias-tee control. `marsad run <preset> <duration> --engine rtl_power_fftw`, or a
preset that names the engine: `top` (2147-2187 MHz, 22 hops of 2.4 MHz at 25% overlap,
128 bins = 18.75 kHz, 1 s per hop) and `h1` (one hop on 1420.4058 MHz on the v5, 2048 bins,
60 s per spectrum). Options `--bins --time --rate --overlap --baseline --window`; gain is
validated against the tuner ladder as before and passed in tenths of a dB (`-g 290`). The
raw output is `surveys/<stem>.dat` (the live viewer only reads rtl_power CSVs and never sees
it) with the program's stderr in `<stem>.log`; the sidecar carries `"engine"` and an
`rtl_power_fftw` section with the actual sample rate parsed from that log. **Crop is applied
at analysis time**: the recorder asks for `-o 100*crop` overlap and the parser drops
`int(N*crop/2)` bins off each edge of every hop, rtl_power's own arithmetic, so hops still
tile. `marsad analyze` reads either format (judged by the file's first line) and takes
`--crop` for a .dat without a sidecar. **The fingerprint names the engine**
(`... | N128 | r2.4M | o25 | c0.25 | rtl_power_fftw | xtr`), so an rtl_power_fftw run never
matches an rtl_power run, `compare` refuses the mix, and `archive new` never compares across
engines. Its dB are a power spectral density, `10*log10(sum|X|^2 / repeats / N / rate)`, not
rtl_power's scale; see the characterisation section for the measured offset.
`tests/test_rpf.py` pins the hop plan (the program's own Plan::Plan arithmetic), geometry,
fingerprint, command line, log parser and the parser on a sample file.

**Second-radio panel on the Scan tab (2026-10-29), `live.other_payload`, `GET /other`.** With
two radios running, the main view follows one recording and the panel under its spectrum shows
the other: (1) the newest recording **still being written** that is not the main one (sidecar
has no `finished_local` and the file grew within 5 min, or three write intervals for a slow
scan; a killed run retires itself by the clock), drawn as latest pass, median and max-hold
over its last 120 passes plus a small waterfall, labelled receiver, tag, file, pass count and
fingerprint (its own 600-column tailers, so the main view keeps its width); (2) every running
`marsad watch`, drawn from its status file: the session now puts a **power spectrum of its
tuning** in `recorder_status_<serial>.json` (`spectrum`: 256 columns, mean and max-hold over the
last ~2 s, the floor and the trigger level, floor + 12 dB, in the watched window, tuned
frequency) plus `recent_events` (last 12) for markers. Power per column only, the same for
every tier: a private band still gets no IQ of any kind. The status thread reads a dict the
session replaces whole and never touches the stream. With neither, the panel is one line.
**"Newest" now means newest STARTED** (creation time), for the main view's follow too: two
runs flush in turn, and newest-modified flipped the view between them. Polled every 2 s while
the Scan tab is open. `tests/test_second_radio.py` pins the choice, the endpoint and the field.

**The web app shows rtl_power_fftw runs live (2026-10-22).** `marsad live` on 8731 follows
the newest recording of either engine, `surveys/*.csv` or `surveys/*.dat`, and the Scan tab's
survey list and Archive table carry both, with an `fftw` tag. A `.dat` is read by
`live.DatTailer`, a subclass of the CSV tailer that keeps the same spectrum, waterfall,
median trend, peaks and payload: it remembers its byte offset between polls, reads one block
per hop (five `#` header lines, N `frequency power` lines, a blank line), converts the
program's UTC stamps to local time, applies the sidecar's crop as each block lands
(`int(N*crop/2)` bins off each edge, the analyser's cut, so the live picture is the report's),
and groups hops into passes by the frequency wrapping down - the only exact rule, because
hops drop out as the tuner refuses them and `-e` writes the pass-ending blank line only once,
at the very end. The pass in progress is the partial trace; the last pass closes when the
`.log` says `Session duration elapsed`. The `.log` is tailed too: every `Unable to tune to`
becomes a **hatched gap** on the spectrum and the waterfall ("hop the tuner refused: no data
there"), never a silent hole, and the header says `22 of 22 hops tuned, 8 refused`, passes
done against planned, and the engine fingerprint. The dB axis carries the note that the
scale is rtl_power_fftw's own (about 26 dB below rtl_power's 50 kHz scale at 2.4 MS/s), so
nobody reads the two as one. **Stop works on any rtl_power_fftw run**, not only ones started
from the page: `rpf.record` now always writes a stop file (`reports/<stem>.stop`, named in
the sidecar) and polls it; the Scan tab's Stop button posts `/scan/stop`, the recorder sends
Ctrl-Break, the patched program finishes its pass, and analysis and archiving follow as
usual. The **Builder tab** has an engine choice: for rtl_power_fftw it offers FFT bins,
seconds per hop, sample rate and overlap, reproduces the program's hop plan and the measured
overhead model, previews the engine's fingerprint, flags the ceiling as fine (with the
warm-dongle caveat) and emits `--engine rtl_power_fftw --bins --time --rate --overlap`; the
trade-off table varies seconds per hop instead of `-i`. `tests/test_live_dat.py` pins the
tailer on the real PCS capture (one read against two reads that cut mid-line), the refused
hops on the log of the owner's first top-slice run, and the newest-of-either-engine follow.
The live viewer does not auto-start for a `.dat` run from `marsad run` yet (the 8731 copy
follows it on its own when it is up).

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
(remembering answers in `station.json`), except that **`--chain` and `--conditions` both
given skip the station-notes questions** (header fields from station.json, as for
`--non-interactive`; since 2026-10-28, when a chained 10 h run sat at the first prompt with
the owner away; the "Start anyway?" device prompt still asks); writes the JSON sidecar before recording so
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

`tools/antenna_bench.html` - the Antenna Bench, published as an Artifact at
https://claude.ai/artifact/7R4r7UGerUnrjahnaUHqCJ (2026-10-22; it grew out of the Ground Plane Trim
Bench the same day). Redeploy by publishing that file to the same URL. One page, eleven antenna
types behind a picker: quarter-wave ground plane (the default, model unchanged), discone (no
radials, no reflector; a 1000 MHz cutoff is a 75 mm cone that covers 1090, 1265 and 2130 together;
its cut guide trims the first dip against 1.12 x cutoff, not the target), J-pole (half-wave
radiator on a quarter-wave stub, tap at 0.04 lambda for 50 ohm), coaxial collinear, biquad with
reflector, axial-mode helix, quadrifilar helix (two half-turn loops split +/-2.5 %, loop =
2 sqrt((pi D/2)^2 + H^2) + 2D, cardioid up, RHCP/LHCP), 137 MHz V-dipole, can antenna, 5-element
Yagi and the station's own 60 mm whips. The knobs, the to-scale drawing and the predicted readouts share the first screen
(owner's request 2026-10-22), with the SWR chart and cut guide directly under them. For each: dimensions from the target frequency, a to-scale drawing, a
closed-form SWR model (said plainly in "How the bench thinks"), elevation pattern with azimuth
beamwidth for the directional ones, the station bands within 15 percent of the target, a cut guide
that names the dimension to trim and its mm-per-MHz, and the `marsad run` / `marsad response`
commands with full `--chain` and `--conditions`. **The receiver follows the frequency:** above
1766 MHz the XTR at gain 14 behind the DC block (`--rx-floor-db -38.1`); at or below it the v5 at
gain 15.7 with a 10 dB pad between generator and RFIN and no DC block (`--rx-floor-db -35.9`). The
open and load references are tagged by receiver and sweep (`br-open-xtr-1811-2148`) because they
serve every antenna measured on that sweep. A "how the station will score it" line per target:
ADS-B by `adsb_check.py` DF17/min against the whip's 1856, 2.1 GHz by the within-run plateau on the
2049 tile (whip +5.8, UWB +8.0), the helix by Iridium occupancy on a 1600-1701 tile, the Yagi by the
radar on `burst23`, the V-dipole by a 137 MHz survey. The cut log (artifact db collection `cuts`)
carries an `antenna` field and a `dims` snapshot; entries without one read as ground plane; the view
filters by the selected type. Measured overlay from `marsad response` atlas.json works for every
type. The Antenna Atlas is a separate artifact and is not touched by this page.

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

**Event finder (2026-10-30), `marsad/events.py`.** The broadband emitter of V1-4n (02:19-05:12)
was found by eye; now every analysis looks for such things. It works on the **per-sweep median of
each 25 MHz block and of the whole band** (one number per block per sweep; a single line cannot move
it, a broadband emitter steps it cleanly, and it repeats to a few hundredths of a dB). Each series is
cut at its own change points (median of the next 6 sweeps minus median of the previous 6, at least
max(0.5 dB, 5 x the settled sweep-to-sweep scatter)); each cut is moved to the earliest sweep from
which the series stays on the new side, so a switch-on after a dip, or a switch-off that dips and
overshoots, dates to where the new level begins. A segment that sits above (or below) **both
neighbours** by that much is an **event**: its neighbours are the baseline, because a night has no
single quiet level (V1-4n: -0.43 before, +0.45 during, -0.41 after, ~0 once the morning starts, so a
long rolling median lands between them and a 4-8 h one was tried and failed). An edge segment counts
against its one neighbour when it is the shorter and the neighbour is not itself an event. Events
seen in several series (runs overlapping for half the shorter) are one event, dated by the whole
band when it saw it. **Broadband rule:** the whole band moved, or at least two blocks moved and all
the same way; one block up while its neighbour goes down is a hump changing shape, not an emitter.
Shape: **step** (at most 2 sweeps part-way across the change) or **ramp**. **Warm-up** is looked for
directly (a slow settling seldom makes a 6-sweep step): the smoothed whole band moving one way (80 %
of its moves one sign) by the threshold before it holds within half of it for a quarter of the run;
reported as "warm-up / trend", never as an event, and the search starts after it. **Transients:** a
sweep (or two) off the median of the 4 sweeps before AND the 4 after, same direction, by max(0.5 dB,
6 x scatter), in the whole band or at least two blocks; the smaller of the two differences is used,
so a step's edge never counts. Sustained changes are searched after the run's warm-up skip;
transients in **every** sweep, skip included (a sweep judged against its own neighbours does not care
that the level is settling; A3's first lift is sweep 10 of a 10-sweep skip).

Output: an **"## Events"** section in report.md (above Images) and **`events.csv`** in the report
folder (kind, shape, start, end, start_sweep, sweeps, duration_min, whole_db, blocks_moved, note, one
`d_<block>` column each), from `marsad analyze` / `marsad run` for both engines. On any existing
survey:

```
marsad events <tag|report|csv|dat> [--block 25] [--min-db 0.5] [--min-sweeps 6] [--skip-sweeps N] [--out DIR]
```

`--skip-sweeps` defaults to what the report used ("first N discarded"), else 0; it writes events.csv
and replaces (or adds) the report.md section. Levels are relative dB on the engine's own scale.

Validated, scored against predictions written first (commit message): **V1-4n** (skip 130) one
emitter, **step 02:20:16 -> 05:11:47**, whole band **+0.76 dB**, 250-275 +0.59, 275-300 +1.11,
300-325 +0.81 (225-250 and 325-350 under 0.5, 375-400 0.0), the two-sweep dip at 02:19:13-45 a
transient; plus 8 smaller events 06:04-09:46, all the 270 MHz hump (225-300 only, whole band under
0.5) switching through the morning, as M1 saw it. **A3** (skip 10) no sustained change, one-sweep
lifts at **08:26:55** (+2.4 dB whole band) and **08:36:26** (+1.8). **V1-1b** with its skip of 130:
nothing; with no skip: warm-up, settled from sweep 83, and nothing else. The per-25 MHz block
changes on V1-4n are smaller and less even than the "+1.0 to +1.4 dB across 225-349" read by eye;
the whole band's +0.76 is the clean figure. `tests/test_events.py` pins all three on their per-sweep
block medians (`tests/data/events_*_blocks.csv`, derived; the raw CSVs are untouched) and a synthetic
survey through the analyser and the CLI.

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
| wide | 52M:1660M:100k, -g 29, -i 60 | Overview, 766 hops, 36 s retune floor; analyse with `--receiver xtr`. 52M:1700M overflows rtl_power's planner (see tool limits) |
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

- **Pinned 2026-10-22: the outdoor box.** Antenna, Flamingo+ notch and LaNA together outside
  the window in a small weatherproof box, LaNA still powered through the coax by the XTR's
  bias-tee, so the 3 ft coax carries amplified signal and its loss costs nothing. Block E
  measured outside at +3-5 dB in the signal-carrying bands; this is the largest measured gain
  not yet banked. Needs a real mount, not suction cups on sun-warmed glass.
- **Pinned 2026-10-22: a dish or horn for the hydrogen line.** The `h1` preset (one hop on
  1420.4058 MHz, 2048 bins, 60 s, v5) exists, but the 21 cm line needs a directional antenna
  aimed at the sky; an omni such as the UWB will almost certainly show nothing. Research
  horn and small-dish designs before building.
- **Pinned 2026-10-27: both receivers at once.** The owner has two Flamingo+ notches, one per
  radio. Running the XTR and the v5 together needs either two antennas (or one antenna through a
  splitter after the LaNA, ~3.5 dB per arm) and a second SMA path into its own enclosure. Until
  then one notch is used, at the window, ahead of whichever receiver is on the antenna.
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
