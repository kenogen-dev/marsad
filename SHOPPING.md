# What to buy, in order

Prices are rough recollections, not checked today — treat them as "which bracket", not quotes.
The ranking is by **how much it changes what the station can measure**, not by cost.

---

## 1. Second Flamingo+ FM notch — the only item that fixes a known fault

Not an instrument, but it outranks every instrument here. The v5 chain has no notch, and
broadcast FM drives the R820T front end into compression: measured peaks at **+3.80 dB**, which
is essentially ADC full scale, through a 46 mm stub that is 0.015 wavelength at 98 MHz. The
consequence is already recorded — ADS-B at 1090 MHz decoded **1856 frames/min** on the XTR chain
and **0** on the v5 chain across a 9.4 dB gain range, same sky, same minutes.

Until this is in, every v5 number above the noise floor is suspect, and no antenna can be graded
on that chain.

---

## DECIDED 2026-10-18

Ordered: **noise source** ($22.57), **Nooelec attenuator kit** ($47.95), **near-field probe set**
($18.46). **VNA declined** - the sub-$50 units stop at 1.5 GHz and anything covering the full
52-2187 MHz band jumps to $130+.

Also ordered: **return-loss bridge** ($11.95, 0.1-3000 MHz SMA), **2x 3 ft RG316 jumpers**, and
an **SMA adapter kit**. Already on hand: 3x 6 in SMA jumpers, 9-12 V barrel supplies, multimeter.

**That completes the instrument set.** Everything on the measurement list below is now runnable.
Nothing further should be bought until each of these has been used at least once - the shortage
was never equipment, and adding more before using these would just be a different way of not
measuring. See "Replacing the VNA" below for what the bridge and noise source do together.

**Open item:** confirm the SMA **DC block** has arrived. It is the one safety part gating all of
this on the XTR, whose bias-tee is always live - see the warning under Consumables.

---

## 2. NanoVNA or LiteVNA, ~$50–80 — the instrument worth buying

Covers the band. This is the one that aims at the biggest target.

- The whip-length study took four hours across four lengths, judged *indirectly* through a
  distant signal at 1799.98 MHz. A VNA measures resonance and match **directly, in seconds**.
- It verifies the Flamingo+ notch actually does what its datasheet claims.
- It measures coax loss, and whether the window glass costs anything.

Antenna work is the 10 dB lever here — the station is internal-noise-limited, so a better
antenna raises signals without raising the floor. Everything else on this list moves fractions
of a dB.

---

## 3. SMA attenuator set, 3/6/10/20 dB, ~$10 — best value per dollar

Lets you **prove** the receiver is linear rather than assume it. That exact assumption is what
produced the retracted gap survey: an hour of data that measured the chain, not the band.

Method: insert 10 dB and confirm every level drops by 10 dB. Anything that drops by less is
compression. Anything that drops by more is a measurement problem.

---

## 4. Near-field / sniffer probe set, ~$15–30 — added 2026-10-18

New recommendation, from this session. The 1675 MHz hump cost **two days and five wrong
mechanism hypotheses**, and was finally solved by swapping one USB cable. A probe set plus an
RTL-SDR (which already is a spectrum analyser) turns "which part radiates?" into a two-minute
sweep over the open box instead of a 30-minute survey per guess.

The probes need no electronics — they are shielded loops. The receiver you already own is the
expensive half.

---

## 5. Noise source, BG7TBL type, ~$20 — relative dB becomes absolute

The cascade currently measures everything in *relative* dB. With a noise source and the Y-factor
method, the same runs yield **actual noise figure per stage**: Y is just the ON/OFF power ratio,
which rtl_power's dB differences give directly at fixed gain.

Accuracy is ±1–2 dB without a calibrated ENR, which is fine — the point is converting the
programme from comparative to absolute.

**Do this for free first.** Y-factor needs two *known* noise temperatures, and water supplies
both: a 50 Ω terminator in ice water (273 K) and in boiling water (373 K).

```
If the receiver's Te ~ 627 K (NF 5 dB):
Y = (373 + 627) / (273 + 627) = 1.111  ->  0.46 dB difference
```

The floor repeats to 0.03 dB, so 0.46 dB is a comfortable measurement. Seal the terminator in a
bag, keep the cable short and identical between readings.

---

## 6. ADF4351 module, ~$20 — a known signal anywhere in band

35 MHz – 4.4 GHz CW source. The recurring methodological failure at this station is surveys whose
headline result is an **absence**, run without a positive control. `adsb_check.py` covers
1090 MHz; this covers everywhere else.

---

## Replacing the VNA with the noise source — 2026-10-18

A VNA does two jobs. The noise source does one of them for free and the other for ~$23.

**S21, "what comes through" — free, needs nothing extra.** Noise source -> device -> SDR, then
noise source -> SDR with the device removed. The dB difference versus frequency *is* the device's
insertion loss. Covers the whole band, no ceiling. This measures: the Flamingo+ notch's actual
rejection, coax loss, LaNA gain, and whether each attenuator really is the value stamped on it.

**S11, "what bounces back" — needs a return-loss bridge.** A resistive bridge with three ports:
IN from the noise source, DUT to the antenna, REFLECTED to the SDR. When the DUT is exactly
50 ohm the bridge balances and nothing reaches the reflected port; the worse the match, the more
comes back. Procedure: leave DUT open (total reflection) and record - that is the 0 dB reference
- then connect the antenna and record again. The difference is return loss, and the dips are
resonances. **This is the whip-length study done in minutes instead of four hours**, and unlike
the NanoVNA-H it reaches 2147 MHz.

What it does not give: phase, or a Smith chart. Scalar magnitude only. For "where does this
antenna resonate and how well is it matched", that is sufficient.

**The DUT side must be clean; the other two sides barely matter.** A bridge measures reflection,
so any adapter between the DUT port and the antenna adds its own mismatch, and nothing in the
measurement can separate that from the antenna's. Adapters on the IN and OUT sides only add loss,
and loss cancels when you subtract the open-DUT reference. So spend the connector budget on
getting the antenna onto the DUT port directly, and stop worrying about the rest.

Watch for: a cheap bridge's **directivity** (~25-35 dB, worse above 1 GHz) sets the deepest null
it can resolve. Finding a 10-20 dB return loss dip is comfortable; measuring a 40 dB match is not.
The bridge also costs ~6 dB each way, so budget ~12 dB of round-trip loss against the noise
source's output.

---

## Consumables and build parts

| Item | Why |
|---|---|
| **50 Ω SMA terminators ×3** | the cascade moves the load to five points with varying genders |
| **SMA M-M and F-F adapters** | so one terminator fits anywhere — but keep the stack short |
| **SMA DC block, inner-conductor** | the XTR's bias-tee is always on; a DC-coupled load shorts it |
| **Conductive fabric-over-foam EMI gasket** | compresses, continuous contact, reusable — unlike tape on a lid that opens |
| **Thermal pads + small aluminium heatsinks** | a hot part should not be wrapped in insulating foam |
| **Ferrite chokes** | standing build rule: both ends of every cable inside the box |
| **Second enclosure + USB and SMA bulkheads** | buy for *mechanics*, not RF — fixed repeatable geometry matters when chasing half-dB effects |

---

## Do not buy

- **Bias-tee injector / second LaNA.** Let the cascade decide whether the v5 needs an LNA.
- **USB logic analyser (~24 MHz) or USB scope (~100 MHz).** Five orders of magnitude below
  700–1700 MHz. There is no path from one to the other.
- **Phone tone-generator apps.** Audio, up to ~20 kHz, usually as actual sound.
- **A benchtop spectrum analyser.** You own two already. The gap has never been the ability to
  measure; it is having something *known* to measure, which is what items 3–6 supply.

---

## The pattern worth noticing

Every instrument above except the VNA exists to supply a **known** quantity — a known
attenuation, a known noise temperature, a known tone, a known radiator location. That is the
station's actual shortage. Of the things that have gone wrong here, almost none were failures of
sensitivity; they were failures of having a reference to check against.
