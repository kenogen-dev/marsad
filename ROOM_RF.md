# Room RF map

Near-field probe surveys of the room, one row per completed run, appended by the Probe tab
of the marsad web app. occupancy.db and reports/<survey>/ hold the full data; this file is
the human-readable map of which device emits what.

Levels are relative dB on rtl_power's scale, not calibrated. "Strongest line" is the
detection with the highest max-hold SNR in peaks.csv. "Tagged families" counts detections
carrying each spur-family tag; a lone 600 kHz comb tag is weak evidence (CLAUDE.md) and the
spacing between neighbouring lines should be measured before it is believed.

| When | Target | Probe | Held | Range | Duration | Survey | Detections | Strongest line | Tagged families |
|---|---|---|---|---|---|---|---|---|---|
| 2026-10-20 12:20 | background (probe in open air) | large loop | near the background (probe in open air) | wide 52-1660 MHz (g29, i60) | 10m | survey_wide_10m_probe-background-probe-in-open-air-large-loop_20261020-1220 | 80 (32 untagged) | 860.5909 MHz, 1.6 dB (28.9 dB SNR, 10% on) | 600 kHz comb (switching converter?) x43; hop centre x8; 28.8 MHz SDR clock harmonic x6; 24 MHz crystal harmonic x6; hop-wide impulse x5 |
| 2026-10-20 12:59 | background (probe in open air) | large loop | in open air | wide 52-1660 MHz (g29, i60) | 10m | survey_wide_10m_probe-background-probe-in-open-air-large-loop_20261020-1259 | 93 (38 untagged) | 929.6026 MHz, 3.5 dB (30.4 dB SNR, 37.5% on) | 600 kHz comb (switching converter?) x52; 28.8 MHz SDR clock harmonic x8; hop centre x8; hop-wide impulse x6; 24 MHz crystal harmonic x4; 25 MHz clock harmonic x2; 100 MHz PCIe clock harmonic x2 |
| 2026-10-20 19:19 | background (probe in open air) | large loop | in open air | wide 52-1660 MHz (g29, i60) | 10m | survey_wide_10m_probe-background-probe-in-open-air-large-loop_20261020-1919 | 24 (6 untagged) | 922.4303 MHz, 1 dB (28.7 dB SNR, 10% on) | 600 kHz comb (switching converter?) x13; hop centre x5; 28.8 MHz SDR clock harmonic x5; hop-wide impulse x2; 24 MHz crystal harmonic x1 |
| 2026-10-20 20:59 | background (probe in open air) | large loop | in open air | wide 52-1660 MHz (g29, i60), 20 dB pad | 10m | survey_wide_10m_probe-background-probe-in-open-air-large-loop_20261020-2059 | 9 (1 untagged) | 940.8859 MHz, -6.1 dB (19.9 dB SNR, 20% on) [600 kHz comb (switching converter?) (x1568)] | 600 kHz comb (switching converter?) x7; 28.8 MHz SDR clock harmonic x4; hop-wide impulse x2; 24 MHz crystal harmonic x1; hop centre x1 |
