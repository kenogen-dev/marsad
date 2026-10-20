"""The Probe tab composes a run; the record path does the rest.

Pure arithmetic and text, no station data needed: the hunt tile's hop geometry,
the composed `marsad run` arguments, the report summary and the ROOM_RF.md log.
"""
import csv
import datetime as dt
import re
import sys

import pytest

from conftest import PY, ROOT, run

sys.path.insert(0, str(ROOT))

from marsad import probe                      # noqa: E402
from marsad.config import Station             # noqa: E402
from marsad.rtlpower import parse_freq_spec   # noqa: E402

FORM = {"target": "PC power supply", "probe": "large-loop", "where": "against the PSU fan grille",
        "minutes": "5", "range": "wide", "device": "0", "notes": "monitors on, printer unplugged"}


@pytest.mark.parametrize("centre", [1265.44, 720.0, 433.92, 1633.47])
def test_hunt_tile_keeps_the_target_off_every_hop_centre(centre):
    h = probe.hunt_spec(centre)
    lo, hi, b = parse_freq_spec(h["spec"])
    assert hi - lo == pytest.approx(5.0, abs=1e-6) and b == 5.0
    assert lo < centre < hi
    # three hops of 1.667 MHz; the target a quarter hop (0.417 MHz) from the nearest centre
    assert len(h["hop_centres_mhz"]) == 3
    assert h["clearance_mhz"] == pytest.approx(h["hop_mhz"] / 4, abs=0.006)
    assert h["clearance_mhz"] >= 0.4


def test_compose_wide_is_the_preset_at_60_s_with_the_probe_chain():
    st = Station.load(ROOT)
    d = probe.compose(st, FORM)
    a = d["args"]
    assert a[:2] == ["wide", "5m"]
    assert d["tag"] == "probe-pc-power-supply-large-loop"
    assert a[a.index("--tag") + 1] == d["tag"]
    assert a[a.index("--integration") + 1] == "60"
    assert a[a.index("--receiver") + 1] == "xtr"
    assert "--freq" not in a and "--gain" not in a and "--device" not in a
    for flag in ("--non-interactive", "--no-live", "--keep-defaults"):
        assert flag in a
    chain = a[a.index("--chain") + 1]
    assert chain.startswith("large H-field loop probe, held against the PSU fan grille > SMA DC block > ")
    assert chain.endswith("> USB isolator > PC")
    cond = a[a.index("--conditions") + 1]
    assert "PC power supply" in cond and cond.endswith("; monitors on, printer unplugged")
    # 766 hops, not the 589 a crop-free estimate gives: 36 s of retuning per sweep
    assert d["geometry"]["hops"] == 766 and d["geometry"]["sweeps"] == 5
    assert d["geometry"]["retune_s_per_sweep"] == pytest.approx(36.0, abs=0.05)
    assert d["warnings"] == []
    assert d["rtl_power_line"].startswith("rtl_power -f 52M:1660M:100k -g 29 -i 60 -c 0.25 -F 9 -e 5m")


def test_compose_hunt_builds_the_offset_tile():
    st = Station.load(ROOT)
    d = probe.compose(st, {**FORM, "range": "hunt", "centre": "1265.44", "minutes": "2",
                           "device": "1"})
    a = d["args"]
    assert a[:2] == ["custom", "2m"]
    assert a[a.index("--freq") + 1] == "1262.52M:1267.52M:5k"
    assert a[a.index("--gain") + 1] == "42"
    assert a[a.index("--integration") + 1] == "1"
    assert a[a.index("--floor-window") + 1] == "0.3"
    assert a[a.index("--device") + 1] == "1"
    assert d["hunt"]["clearance_mhz"] >= 0.4
    assert d["warnings"] == []


def test_compose_on_the_v5_snaps_gain_and_uses_its_chain():
    st = Station.load(ROOT)
    d = probe.compose(st, {**FORM, "receiver": "v5", "device": "1"})
    a = d["args"]
    assert a[a.index("--receiver") + 1] == "v5"
    assert a[a.index("--gain") + 1] == "29.7"          # 29 is an E4000 step, not an R820T one
    assert a[a.index("--device") + 1] == "1"
    assert any("29.7" in w for w in d["warnings"])
    chain = a[a.index("--chain") + 1]
    assert "> v5 (" in chain and "LaNA (at the window" not in chain
    # an edited tail from the page is recorded verbatim
    e = probe.compose(st, {**FORM, "chain_tail": "SMA DC block > 1 ft coax > XTR > PC"})
    assert e["chain"].endswith("> SMA DC block > 1 ft coax > XTR > PC")
    with pytest.raises(ValueError, match="receiver"):
        probe.compose(st, {**FORM, "receiver": "hackrf"})


def test_receiver_detection_parses_rtl_eeprom():
    st = Station.load(ROOT)
    xtr = ("Found 1 device(s):\n  0:  Generic RTL2832U OEM\n\nUsing device 0: Generic RTL2832U OEM\n"
           "Found Elonics E4000 tuner\n\nCurrent configuration:\n__________\nVendor ID:\t\t0x0bda\n"
           "Manufacturer:\t\tNooelec\nProduct:\t\tSMArTee XTR v5ee\nSerial number:\t\t08701076\n")
    p = probe.parse_eeprom(xtr)
    assert (p["count"], p["product"], p["serial"], p["tuner"]) == (1, "SMArTee XTR v5ee", "08701076", "Elonics E4000")
    assert probe.match_receiver(st, p["product"], p["tuner"]) == "xtr"
    assert probe.match_receiver(st, "SMArt v5", "Rafael Micro R820T") == "v5"
    assert probe.parse_eeprom("Found 1 device(s):\n  0:  Generic\n\nUsing device 1: \n"
                              "Failed to open rtlsdr device #1.\n")["error"]
    assert probe.parse_eeprom("No supported devices found.\n")["error"]


def test_compose_refuses_a_bad_form_in_plain_words():
    st = Station.load(ROOT)
    with pytest.raises(ValueError, match="target"):
        probe.compose(st, {**FORM, "target": "other", "target_other": ""})
    with pytest.raises(ValueError, match="centre"):
        probe.compose(st, {**FORM, "range": "hunt"})
    with pytest.raises(ValueError, match="minutes"):
        probe.compose(st, {**FORM, "minutes": "0"})
    gap = probe.compose(st, {**FORM, "range": "hunt", "centre": "1150"})
    assert any("PLL gap" in w for w in gap["warnings"])
    other = probe.compose(st, {**FORM, "target": "other", "target_other": "Desk Lamp #2"})
    assert other["tag"] == "probe-desk-lamp-2-large-loop"


def test_summary_and_room_log_row(tmp_path):
    rep = tmp_path / "survey_wide_5m_probe-x_20261020-1200"
    rep.mkdir()
    cols = ["freq_mhz", "width_khz", "max_db", "median_db", "floor_db", "snr_max_db",
            "snr_median_db", "occupancy_pct", "type", "spur_tags"]
    rows = [
        [1468.8, 43.7, -24.7, -25.0, -37.5, 12.8, 12.5, 100.0, "steady",
         "28.8 MHz SDR clock harmonic (x51); 600 kHz comb (switching converter?) (x2448)"],
        [720.0, 87.4, -19.3, -19.4, -36.0, 16.7, 16.6, 100.0, "steady", "24 MHz crystal harmonic (x30)"],
        [1633.47, 21.5, -17.2, -20.1, -39.4, 22.2, 19.3, 100.0, "steady", ""],
        # the receiver's own DC spike outranks everything on SNR and must not be "strongest"
        [1265.01, 4.3, -3.5, -20.0, -28.3, 23.8, 8.3, 55.3, "intermittent",
         "hop centre: zero-IF DC artefact, likely not real"],
    ]
    rows.sort(key=lambda r: -r[5])
    with open(rep / "peaks.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        w.writerows(rows)
    s = probe.summarise_report(rep)
    assert s["detections"] == 4 and s["untagged"] == 1
    assert s["strongest"]["f"] == 1633.47 and s["strongest"]["snr"] == 22.2
    assert s["families"] == {"28.8 MHz SDR clock harmonic": 1, "600 kHz comb (switching converter?)": 1,
                             "24 MHz crystal harmonic": 1, "hop centre": 1}

    st = Station.load(ROOT)
    plan = probe.compose(st, {**FORM, "where": "on the lid | near the fan"})
    when = dt.datetime(2026, 10, 20, 12, 0)
    row = probe.log_row(when, plan, rep.name, s, 97.3, True)
    assert row.startswith("| 2026-10-20 12:00 | PC power supply | large loop | on the lid \\| near the fan |")
    assert "5m (stopped at 1.6 min)" in row
    assert "1633.4700 MHz, -17.2 dB (22.2 dB SNR, 100% on)" in row
    assert "4 (1 untagged)" in row

    class Fake:
        root = tmp_path
    path = probe.append_log(Fake(), row)
    probe.append_log(Fake(), row.replace("12:00", "12:10"))
    text = path.read_text(encoding="utf-8")
    assert text.count("# Room RF map") == 1
    assert text.count("| 2026-10-20 12:") == 2
    assert text.index("| When |") < text.index("| 2026-10-20 12:00")


def test_record_path_offers_the_web_app_flags():
    out = run([PY, "-m", "marsad", "run", "--help"])
    assert "--stop-file" in out and "--keep-defaults" in out
    # the dry run must still print the same summary with the new options present
    out = run([PY, "-m", "marsad", "run", "wide", "5m", "--tag", "pytest", "--integration", "60",
               "--non-interactive", "--no-live", "--keep-defaults", "--dry-run"])
    assert re.search(r"766 hops x 2\.0992 MHz, FFT 32", out)
    assert "-f 52M:1660M:100k -g 29 -i 60 -c 0.25 -F 9 -e 5m" in out
    assert "WARNING" not in out


def test_record_refuses_a_span_rtl_power_cannot_plan():
    """Measured 2026-10-20: rtl_power scales the whole span by 1/(1-crop) in a 32-bit
    int before splitting it into hops, so 52M:1700M (1648 MHz) plans as one hop of 0 Hz."""
    import subprocess
    from marsad.rtlpower import rtl_power_max_span_mhz
    assert rtl_power_max_span_mhz(0.25) == pytest.approx(1610.612736)
    r = subprocess.run([PY, "-m", "marsad", "run", "custom", "5m", "--freq", "52M:1700M:100k",
                        "--tag", "pytest", "--dry-run"], cwd=str(ROOT),
                       capture_output=True, text=True, timeout=120)
    assert r.returncode != 0
    assert "1610.6 MHz" in r.stderr and "Invalid sample rate" in r.stderr
    run([PY, "-m", "marsad", "run", "custom", "5m", "--freq", "52M:1662.6M:100k",
         "--tag", "pytest", "--dry-run"])
