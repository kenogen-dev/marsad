# rtl_power_gainfix: rtl_power that applies tuner gain on the v5 (R820T/R860)

Built 2026-10-27 on the station PC. Lives beside the original as
`rtl-sdr\rtl_power_gainfix.exe`; **the original `rtl_power.exe` is untouched**.
Rebuild from scratch with `build.cmd` here (about 30 s, needs git and the
internet for one clone).

## The fault

The released `rtl_power.exe` (RTL-SDR Blog V1.4.0, the same source as
`rtlsdr.dll`'s `v0.6.0-131-gaed0`) sets the tuner gain **before** it sets the
sample rate and before it has ever tuned. On an R820T/R860 that order loses the
gain, through three steps of librtlsdr that are each reasonable on their own:

1. `rtlsdr_set_sample_rate` calls the tuner's `set_bw`, and `r820t_set_bw`
   ends with `rtlsdr_set_center_freq(dev, dev->freq)`. Nothing has been tuned
   yet, so `dev->freq` is **0**.
2. `rtlsdr_set_center_freq` has the Blog driver's automatic HF switch: below
   24 MHz on an R820T it turns on **direct sampling** (input 2). 0 Hz is below
   24 MHz, so it does, and puts the tuner in standby.
3. rtl_power's first real tune (385 MHz) turns direct sampling off again, and
   switching it off runs the tuner's **`init`**. `r82xx_init` reloads the
   R82xx's default register set, in which the LNA and mixer gains are on
   automatic (reg 0x05 = 0x83, reg 0x07 = 0x75). The gain rtl_power set and
   reported ("Tuner gain set to 7.70 dB") is gone; the tuner runs on its own AGC
   for the rest of the run.

rtl_power's own log shows it happening, in this order:

```
Tuner gain set to 7.70 dB.
Exact sample rate is: 1333333.015442 Hz
Enabled direct sampling mode, input 2
[R82XX] PLL not locked!
Disabled direct sampling mode
```

Why the others were fine: the automatic direct-sampling switch only exists for
the R820T, so the **XTR (E4000)** never takes this path. **rtl_sdr** sets rate,
then frequency, then gain, so its gain comes after the re-init.
**rtl_power_fftw** and **SDR++** were measured to apply gain normally (their
call order was not re-read for this note). Upstream
(rtlsdrblog/rtl-sdr-blog master) has no later change to `rtl_power.c` or
`librtlsdr.c`, so it is not fixed there either.

## The fix: `r82xx-gain-after-tune.patch`

rtl_power.c only: the gain block moves to after `rtlsdr_set_sample_rate`, with
one `rtlsdr_set_center_freq` to the first hop's centre in front of it. That
first tune does the direct-sampling off / re-init **before** the gain is set,
and every later hop is above 24 MHz, so nothing re-runs `init`. The library is
not rebuilt: the exe links against the station's existing `rtlsdr.dll` through
an import library made from its exports, exactly as for rtl_power_fftw. No
other behaviour changes: same hop plan, FFT, crop, FIR, output format.

**Limit:** a v5 scan whose hops cross 24 MHz (HF direct sampling) would still
switch the tuner off and on every sweep and lose the gain each time. No preset
does that, and gain means nothing in direct-sampling mode anyway.

## Build

| Part | Version |
|---|---|
| rtl_power.c, convenience.c, getopt.c, headers | rtlsdrblog/rtl-sdr-blog tag `V1.4.0` = commit `aed0ea19`, plus the patch |
| librtlsdr | the station's `rtl-sdr\rtlsdr.dll`, not rebuilt |
| Toolchain | VS Build Tools 2022, MSVC 14.44.35207 x64, `/O2 /W3 /MT` (static runtime) |

`pthread.h` and `libusb.h` are included by rtl_power.c but nothing from them is
used on Windows, so the build gives it empty stand-ins. Result 229,888 bytes,
depends on `rtlsdr.dll` and `KERNEL32.dll`. sha256 of the installed copy
(both patches):
`3f45d661bf569caf93affe91c5738b5b5d16b278fb3614a45e32172399e5c6b3`
(rebuilds are not byte-identical; MSVC embeds paths and timestamps).

## Second fix: `win64-accumulators.patch` (affects BOTH receivers)

rtl_power adds up each bin's power over the integration interval in a C
`long`. On Linux that is 64 bits; **on Windows it is 32 bits**, so a strong
carrier overflows past 2^31 and wraps. The result is either `-nan(ind)` (the
sum wrapped negative) or, worse, a **silently wrong, too-low value** (wrapped
round to positive again). Found while testing the gain fix: with the gain
working, the 385.04 MHz carrier bin read NaN in every sweep at g29.7 and a
wrong value at g40.2, while its neighbours were fine. The patch changes those
accumulators (`avg`, `rms_power`, `real_conj`, `remove_dc`, the swap temp) to
`int64_t`. Below 2^31 the arithmetic is identical, so ordinary levels do not
change; only bins that used to overflow now read their true value.

This is the real cause of the "NaN on the strongest carriers" the station has
seen since 2026-10-18 (CLAUDE.md, "FM overload re-confirmed ... and a parser
bug"), which was read as ADC saturation. It applies to the XTR too, because
it is in rtl_power, not the driver. The XTR was not on the PC for this test.

## Measured 2026-10-27, v5 (SN 27825674), device 0, antenna on, 385.04 MHz carrier

4 s captures, `-f 384.6M:385.6M:12.5k -i 1 -c 0.25 -F 9`, interleaved with
`rtl_sdr -f 385104166 -s 1333333` (same tune, same rate; levels from a
128-point numpy FFT of the raw IQ), two repeats agreeing to 0.15 dB. Change
from g7.7:

| gain | fixed rtl_power carrier | rtl_sdr carrier | fixed rtl_power floor | rtl_sdr floor |
|---|---|---|---|---|
| 29.7 | +18.0 | +17.9 | +3.4 | +3.6 |
| **40.2** | **+28.0** | **+28.0** | **+11.2** | **+11.5** |
| 49.6 | +35.7 | +35.6 | +18.1 | +18.5 |

No NaN bins in any of the eight files. Original `rtl_power.exe`, same test:
carrier NaN at both g7.7 and g40.2, floor -20.9 / -21.0 (the tuner on its own
AGC). Two-hop check `383M:387M:12.5k -i 2`: floor +10.4 dB on both hops,
carrier +26.8, so the gain survives retuning.

## Checked on the XTR (2026-10-27, 19:00)

The fix only has to leave the E4000 alone, since the XTR never had the gain bug.
XTR in the cage, DC block > coax > notch > window antenna, 385.04 MHz carrier,
`-f 383M:387M:12.5k -i 2 -e 6`, original and fixed build back to back:

| | original | fixed | rtl_sdr (reference) |
|---|---|---|---|
| carrier, g14 -> g29 | +14.80 dB | +14.09 dB | +14.53 dB |
| band median, g14 -> g29 | +2.24 | +2.20 | +2.39 |
| fixed minus original, median | | -0.01 (g14), -0.05 (g29) | |
| fixed minus original, carrier | | +0.77, -0.03, +0.07 (g14, three repeats); +0.06 (g29) | |
| non-finite readings | 0% | 0% | |

The first g14 carrier difference (+0.77) was single-bin scatter: the original
build alone moved 0.53 dB between three identical runs. **No change on the XTR.**
