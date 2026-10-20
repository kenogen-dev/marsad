# SDR survey workflow (rtl_power → survey.py)

## One-time setup (Windows)

Dependencies live in a virtual environment at `C:\SDR\.venv` (already created):

```bat
py -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Station layout: `rtl-sdr\` holds the RTL-SDR Blog executables, `tools\` holds `survey.py`,
raw CSVs go in `surveys\`, analyser output in `reports\`.

## 1. Record a survey with rtl_power

Run from `C:\SDR`. Close SDR++ first: only one program can use the SDR at a time.

```bat
:: 1.24-1.7 GHz (above this unit's measured gap), 25 kHz bins, 10 s per sweep, 1 hour
rtl-sdr\rtl_power.exe -f 1241M:1700M:25k -g 34 -i 10 -c 0.25 -F 9 -e 1h survey_L_band.csv

:: Military air (UHF) 225-400 MHz, 12.5 kHz bins, 30 min
rtl-sdr\rtl_power.exe -f 225M:400M:12.5k -g 29 -i 10 -c 0.25 -F 9 -e 30m survey_milair.csv

:: 900 MHz ISM + paging 902-935 MHz, fine bins, 15 min
rtl-sdr\rtl_power.exe -f 902M:935M:5k -g 29 -i 5 -c 0.25 -F 9 -e 15m survey_900.csv

:: Wide overview 52-1700 MHz, coarse bins (skips nothing; mask the gap when analysing)
rtl-sdr\rtl_power.exe -f 52M:1700M:100k -g 29 -i 5 -c 0.25 -F 9 -e 1h survey_wide.csv
```

What the options do:

| Option | Meaning | Why |
|---|---|---|
| `-f lo:hi:bin` | Range and bin width | Smaller bins resolve narrow carriers but make bigger files |
| `-g` | Fixed gain (dB) | Never leave it on auto: auto gain makes surveys incomparable |
| `-i` | Seconds of averaging per sweep | Longer = steadier floor, fewer sweeps |
| `-c 0.25` | Crop 25% of each hop | **Important on the E4000**: removes hop edges and most of the zero-IF centre spike |
| `-F 9` | Better anti-alias filter | Fewer false signals folded in from outside each hop |
| `-e` | Stop after this long | `30m`, `1h`, `12h` |

The bias-tee on your SMArTee XTR is always on, so the LaNA works without any rtl_power flag.

## 2. Analyse

```bat
.venv\Scripts\python.exe tools\survey.py analyze survey_L_band.csv
.venv\Scripts\python.exe tools\survey.py analyze survey_wide.csv --receiver xtr        :: masks the 1095-1240 MHz gap
.venv\Scripts\python.exe tools\survey.py analyze survey_milair.csv --threshold 8       :: more sensitive detection
.venv\Scripts\python.exe tools\survey.py compare before.csv after.csv --threshold 10   :: e.g. before/after a station change
```

Output goes to `reports/<survey name>/`:

- `report.md` - summary, band table, real signals, spurs grouped by pattern
- `heatmap.png` - waterfall (time down, frequency across)
- `spectrum.png` - max hold, median, noise floor, threshold, signals (red) and likely spurs (purple x)
- `occupancy.png` - % of sweeps each frequency was busy
- `peaks.csv`, `bins.csv` - the numbers, for Excel or your own Python

`compare` also writes `reports/compare_A_vs_B/` with floor change and new / gone / changed signals.

## How detection works

- **Noise floor**: for each bin, the 20th percentile over time (ignores bursts), then a rolling median over 2 MHz (ignores carriers). Change the window with `--floor-window`.
- **Signal**: a local peak in max-hold at least `--threshold` dB above the floor, at least 25 kHz from the next.
- **Type**: steady (busy in at least 90% of sweeps), intermittent (5-90%), rare burst (under 5%).
- **Spur tags**: matches against 28.8 MHz clock harmonics, your 600 kHz comb, USB 480 MHz, 24/25/100 MHz PC clocks, and each hop's centre (E4000 DC spot). Edit `SPUR_FAMILIES_MHZ` and `RECEIVER_GAPS_MHZ` at the top of `tools/survey.py` as you learn your station.

Levels are relative dB, not calibrated dBm. Only compare surveys taken with the same gain, bin size, crop and integration time.
