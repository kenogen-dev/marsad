"""The Probe tab composes a run; the record path does the rest.

Pure arithmetic and text, no station data needed: the hunt tile's hop geometry,
the composed `marsad run` arguments, the report summary and the ROOM_RF.md log.
"""
import csv
import datetime as dt
import re
import sys

import pytest

from conftest import PY, ROOT, run, rig

sys.path.insert(0, str(ROOT))

from marsad import hunt, probe                # noqa: E402
from marsad.config import Station             # noqa: E402
from marsad.rtlpower import parse_freq_spec   # noqa: E402

FORM = {"target": "PC power supply", "probe": "large-loop", "where": "against the PSU fan grille",
        "minutes": "5", "range": "wide", "device": "0", "notes": "monitors on, heater unplugged"}


@pytest.mark.parametrize("centre", [1300.00, 720.0, 433.92, 1650.25])
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
    st = rig()
    d = probe.compose(st, FORM)
    a = d["args"]
    assert a[:2] == ["wide", "5m"]
    assert d["tag"] == "probe-pc-power-supply-large-loop"
    assert a[a.index("--tag") + 1] == d["tag"]
    assert a[a.index("--integration") + 1] == "60"
    assert a[a.index("--receiver") + 1] == "e4000"
    assert "--freq" not in a and "--gain" not in a and "--device" not in a
    for flag in ("--non-interactive", "--no-live", "--keep-defaults"):
        assert flag in a
    chain = a[a.index("--chain") + 1]
    assert chain.startswith("large H-field loop probe, held against the PSU fan grille > "
                            "SMA adapter > SMA DC block > 8 ft coax > FM notch > LNA (")
    assert chain.endswith("> USB isolator > PC")
    cond = a[a.index("--conditions") + 1]
    assert "PC power supply" in cond and cond.endswith("; monitors on, heater unplugged")
    # 766 hops, not the 589 a crop-free estimate gives: 36 s of retuning per sweep
    assert d["geometry"]["hops"] == 766 and d["geometry"]["sweeps"] == 5
    assert d["geometry"]["retune_s_per_sweep"] == pytest.approx(36.0, abs=0.05)
    assert d["warnings"] == []
    # rtl_power_gainfix where it is installed, the stock name where it is not (CI)
    assert re.match(r"rtl_power(_gainfix)? -f 52M:1660M:100k -g 29 -i 60 -c 0\.25 -F 9 -e 5m",
                    d["rtl_power_line"])


def test_compose_hunt_builds_the_offset_tile():
    st = rig()
    d = probe.compose(st, {**FORM, "range": "hunt", "centre": "1300.00", "minutes": "2",
                           "device": "1"})
    a = d["args"]
    assert a[:2] == ["custom", "2m"]
    assert a[a.index("--freq") + 1] == "1297.08M:1302.08M:5k"
    assert a[a.index("--gain") + 1] == "42"
    assert a[a.index("--integration") + 1] == "1"
    assert a[a.index("--floor-window") + 1] == "0.3"
    assert a[a.index("--device") + 1] == "1"
    assert d["hunt"]["clearance_mhz"] >= 0.4
    assert d["warnings"] == []


def test_attenuator_lands_between_the_notch_and_the_lna():
    st = rig()
    d = probe.compose(st, {**FORM, "attenuator": "10"})
    assert "> 8 ft coax > FM notch > SMA adapter > 10 dB SMA attenuator > LNA (" in d["chain"]
    assert ", 5m, 10 dB pad ahead of the LNA; monitors on" in d["conditions"]
    assert d["conditions"].count("10 dB pad") == 1
    assert d["range_label"].endswith(", 10 dB pad") and d["attenuator_db"] == 10
    none = probe.compose(st, FORM)
    assert "> FM notch > LNA (" in none["chain"]      # no pad: notch straight onto the LNA
    assert "attenuator" not in none["chain"] and ", 5m, no pad; monitors on" in none["conditions"]
    with pytest.raises(ValueError, match="kit has no 7 dB"):
        probe.compose(st, {**FORM, "attenuator": "7"})
    # the r820t chain has no LNA: the pad is refused with a warning, not silently placed
    r820t = probe.compose(st, {**FORM, "receiver": "r820t", "attenuator": "6"})
    assert "attenuator" not in r820t["chain"] and r820t["attenuator_db"] == 0
    assert any("no LNA" in w for w in r820t["warnings"])
    # a pad already written into the chain text is the record: the picker steps aside
    tail = ("SMA adapter > SMA DC block > 8 ft coax > Notch (input side) > 10dB Attenuator > "
            "LNA (at the antenna) > 3 ft RG316 coax > E4000 > PC")
    c = probe.compose(st, {**FORM, "chain_tail": tail})
    assert c["attenuator_db"] == 10 and c["attenuator_source"] == "chain"
    assert c["chain"].count("ttenuator") == 1 and "no pad" not in c["conditions"]
    assert ", 10 dB pad ahead of the LNA (in the chain)" in c["conditions"]
    assert c["range_label"].endswith(", 10 dB pad")
    same = probe.compose(st, {**FORM, "chain_tail": tail, "attenuator": "10"})
    assert same["chain"].count("ttenuator") == 1
    with pytest.raises(ValueError, match="already has a 10 dB attenuator"):
        probe.compose(st, {**FORM, "chain_tail": tail, "attenuator": "6"})
    # it reaches the room-map row through the range cell
    row = probe.log_row(dt.datetime(2031, 10, 20, 13, 0), d, "survey_x", probe.summarise_report(ROOT / "nope"), None, False)
    assert "| wide 52-1660 MHz (g29, i60), 10 dB pad |" in row


def test_compose_on_the_v5_snaps_gain_and_uses_its_chain():
    st = rig()
    d = probe.compose(st, {**FORM, "receiver": "r820t", "device": "1"})
    a = d["args"]
    assert a[a.index("--receiver") + 1] == "r820t"
    assert a[a.index("--gain") + 1] == "29.7"          # 29 is an E4000 step, not an R820T one
    assert a[a.index("--device") + 1] == "1"
    assert any("29.7" in w for w in d["warnings"])
    chain = a[a.index("--chain") + 1]
    assert "> r820t (" in chain and "LNA (" not in chain
    # an edited tail from the page is recorded verbatim
    e = probe.compose(st, {**FORM, "chain_tail": "SMA DC block > 1 ft coax > E4000 > PC"})
    assert e["chain"].endswith("> SMA DC block > 1 ft coax > E4000 > PC")
    with pytest.raises(ValueError, match="receiver"):
        probe.compose(st, {**FORM, "receiver": "hackrf"})


def test_receiver_detection_parses_rtl_eeprom():
    st = rig()
    e4000 = ("Found 1 device(s):\n  0:  Generic RTL2832U OEM\n\nUsing device 0: Generic RTL2832U OEM\n"
           "Found Elonics E4000 tuner\n\nCurrent configuration:\n__________\nVendor ID:\t\t0x0bda\n"
           "Manufacturer:\t\tAcme\nProduct:\t\tGeneric E4000\nSerial number:\t\t00000101\n")
    p = probe.parse_eeprom(e4000)
    assert (p["count"], p["product"], p["serial"], p["tuner"]) == (1, "Generic E4000", "00000101", "Elonics E4000")
    assert probe.match_receiver(st, p["product"], p["tuner"]) == "e4000"
    assert probe.match_receiver(st, "Generic R820T", "Rafael Micro R820T") == "r820t"
    assert probe.parse_eeprom("Found 1 device(s):\n  0:  Generic\n\nUsing device 1: \n"
                              "Failed to open rtlsdr device #1.\n")["error"]
    assert probe.parse_eeprom("No supported devices found.\n")["error"]


def test_compose_refuses_a_bad_form_in_plain_words():
    st = rig()
    with pytest.raises(ValueError, match="target"):
        probe.compose(st, {**FORM, "target": "other", "target_other": ""})
    with pytest.raises(ValueError, match="centre"):
        probe.compose(st, {**FORM, "range": "hunt"})
    with pytest.raises(ValueError, match="minutes"):
        probe.compose(st, {**FORM, "minutes": "0"})
    with pytest.raises(ValueError, match="where the probe is held"):
        probe.compose(st, {**FORM, "where": ""})
    bg = probe.compose(st, {**FORM, "target": "background (probe in open air)", "where": ""})
    assert bg["held"] == "in open air" and ", held in open air > " in bg["chain"]
    gap = probe.compose(st, {**FORM, "range": "hunt", "centre": "1150"})
    assert any("PLL gap" in w for w in gap["warnings"])
    other = probe.compose(st, {**FORM, "target": "other", "target_other": "Desk Lamp #2"})
    assert other["tag"] == "probe-desk-lamp-2-large-loop"


def test_summary_and_room_log_row(tmp_path):
    rep = tmp_path / "survey_wide_5m_probe-x_20311020-1200"
    rep.mkdir()
    cols = ["freq_mhz", "width_khz", "max_db", "median_db", "floor_db", "snr_max_db",
            "snr_median_db", "occupancy_pct", "type", "spur_tags"]
    rows = [
        [1468.8, 43.7, -24.7, -25.0, -37.5, 12.8, 12.5, 100.0, "steady",
         "28.8 MHz SDR clock harmonic (x51); 600 kHz comb (switching converter?) (x2448)"],
        [720.0, 87.4, -19.3, -19.4, -36.0, 16.7, 16.6, 100.0, "steady", "24 MHz crystal harmonic (x30)"],
        [1650.25, 21.5, -17.2, -20.1, -39.4, 22.2, 19.3, 100.0, "steady", ""],
        # the receiver's own DC spike outranks everything on SNR and must not be "strongest"
        [1500.01, 4.3, -3.5, -20.0, -28.3, 23.8, 8.3, 55.3, "intermittent",
         "hop centre: zero-IF DC artefact, likely not real"],
    ]
    rows.sort(key=lambda r: -r[5])
    with open(rep / "peaks.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        w.writerows(rows)
    s = probe.summarise_report(rep)
    assert s["detections"] == 4 and s["untagged"] == 1
    assert s["strongest"]["f"] == 1650.25 and s["strongest"]["snr"] == 22.2
    assert s["families"] == {"28.8 MHz SDR clock harmonic": 1, "600 kHz comb (switching converter?)": 1,
                             "24 MHz crystal harmonic": 1, "hop centre": 1}

    st = rig()
    plan = probe.compose(st, {**FORM, "where": "on the lid | near the fan"})
    when = dt.datetime(2031, 10, 20, 12, 0)
    row = probe.log_row(when, plan, rep.name, s, 97.3, True)
    assert row.startswith("| 2031-10-20 12:00 | PC power supply |  | large loop | on the lid \\| near the fan |")
    assert "5m (stopped at 1.6 min)" in row
    assert "1650.2500 MHz, -17.2 dB (22.2 dB SNR, 100% on)" in row
    assert "4 (1 untagged)" in row

    class Fake:
        root = tmp_path
    path = probe.append_log(Fake(), row)
    probe.append_log(Fake(), row.replace("12:00", "12:10"))
    text = path.read_text(encoding="utf-8")
    assert text.count("# Room RF map") == 1
    assert text.count("| 2031-10-20 12:") == 2
    assert text.index("| When |") < text.index("| 2031-10-20 12:00")


def test_record_path_offers_the_web_app_flags():
    out = run([PY, "-m", "marsad", "run", "--help"])
    assert "--stop-file" in out and "--keep-defaults" in out
    # the dry run must still print the same summary with the new options present
    out = run([PY, "-m", "marsad", "run", "wide", "5m", "--tag", "pytest", "--integration", "60",
               "--non-interactive", "--no-live", "--keep-defaults", "--dry-run"])
    assert re.search(r"766 hops x 2\.0992 MHz, FFT 32", out)
    assert "-f 52M:1660M:100k -g 29.7 -i 60 -c 0.25 -F 9 -e 5m" in out
    assert "WARNING" not in out


def test_record_refuses_a_span_rtl_power_cannot_plan():
    """rtl_power scales the whole span by 1/(1-crop) in a 32-bit
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


def test_device_state_reaches_the_tag_the_conditions_and_the_room_map(tmp_path):
    st = rig()
    d = probe.compose(st, {**FORM, "target": "heater", "state": "idle"})
    assert d["state"] == "idle" and d["tag"] == "probe-heater-idle-large-loop"
    assert ", device state idle, " in d["conditions"]
    # the hunt module reads it back out of a sidecar's conditions, target intact
    assert hunt.STATE_RE.search(d["conditions"]).group(1) == "idle"
    assert hunt.TARGET_RE.search(d["conditions"]).group(1) == "heater"
    other = probe.compose(st, {**FORM, "target": "heater", "state": "other",
                               "state_other": "heating, duplex; 20 pages"})
    assert other["state"] == "heating duplex 20 pages"        # no separators left in it
    assert other["tag"] == "probe-heater-heating-duplex-20-pa-large-loop"   # state slug capped at 20
    with pytest.raises(ValueError, match="device state"):
        probe.compose(st, {**FORM, "state": "other", "state_other": " "})
    none = probe.compose(st, FORM)
    assert none["state"] == "" and "device state" not in none["conditions"]
    assert "states" in probe.catalog(st) and "unplugged" in probe.catalog(st)["states"]
    row = probe.log_row(dt.datetime(2031, 10, 26, 9, 0), d, "survey_x",
                        probe.summarise_report(ROOT / "nope"), None, False)
    assert row.startswith("| 2031-10-26 09:00 | heater | idle | large loop |")


def test_room_map_gains_the_state_column_and_old_rows_stay_valid(tmp_path):
    old = ("# Room RF map\n\nintro\n\n"
           "| When | Target | Probe | Held | Range | Duration | Survey | Detections | Strongest line | Tagged families |\n"
           "|---|---|---|---|---|---|---|---|---|---|\n"
           "| 2031-10-20 12:20 | background | large loop | on the lid \\| near the fan | wide | 10m | s1 | 80 | x | y |\n")
    (tmp_path / "ROOM_RF.md").write_text(old, encoding="utf-8")

    class Fake:
        root = tmp_path
    new_row = "| 2031-10-26 09:00 | heater | idle | large loop | h | wide | 5m | s2 | 1 | x | y |"
    probe.append_log(Fake(), new_row)
    lines = (tmp_path / "ROOM_RF.md").read_text(encoding="utf-8").splitlines()
    table = [ln for ln in lines if ln.startswith("|")]
    assert table[0].startswith("| When | Target | State | Probe |")
    assert table[1].count("|") == 12
    assert table[2] == ("| 2031-10-20 12:20 | background | | large loop | on the lid \\| near the fan "
                        "| wide | 10m | s1 | 80 | x | y |")
    assert table[3] == new_row
    assert all(len(probe.CELL_SPLIT.split(t)) == 13 for t in table)    # one shape throughout
    assert lines[:4] == ["# Room RF map", "", "intro", ""]
    # a second append does not migrate twice
    probe.append_log(Fake(), new_row)
    assert (tmp_path / "ROOM_RF.md").read_text(encoding="utf-8").count("| When |") == 1


# ---------------------------------------------------------------- device index and duplicate probe
def test_chain_tail_drops_a_leading_probe_and_empty_segments():
    # the Chain tab's placeholder, or a probe typed into the box, must not make a second probe
    tail = ("near-field probe (held where the Probe tab says) > SMA DC block > 8 ft coax > "
            "LNA > E4000 > PC")
    assert probe.clean_tail(tail) == "SMA DC block > 8 ft coax > LNA > E4000 > PC"
    # what deleting the placeholder by hand left behind
    assert probe.clean_tail(" > > SMA DC block >  > E4000 > PC > ") == "SMA DC block > E4000 > PC"
    assert probe.clean_tail("") == ""
    st = rig()
    d = probe.compose(st, {**FORM, "probe": "stub-a", "chain_tail": " > " + tail})
    assert d["chain"].count("probe") == 1
    assert d["chain"].startswith("E-field stub probe A, held against the PSU fan grille > SMA DC block")
    assert not re.search(r">\s*>", d["chain"]) and not d["chain"].strip().endswith(">")


def test_chain_tab_probe_chain_tail_has_no_probe():
    from marsad import chain
    parts = [{"type": "probe", "name": "near-field probe", "detail": "held where the Probe tab says"},
             {"type": "dcblock", "name": "SMA DC block", "detail": ""},
             {"type": "sdr", "name": "E4000", "detail": "", "rx": "e4000"}]
    assert chain.tail_text(parts) == "SMA DC block > E4000"


def test_compose_refuses_an_unknown_device_instead_of_guessing():
    st = rig()
    with pytest.raises(ValueError, match="device number unknown"):
        probe.compose(st, {**FORM, "device": ""})
    assert probe.compose(st, {**FORM, "device": "0"})["device"] == 0


def test_detect_receivers_with_only_the_xtr_plugged_in(monkeypatch):
    # rtl_eeprom mocked: one dongle, the E4000, at index 0
    st = rig()
    e4000 = ("Found 1 device(s):\n  0:  Acme, Generic E4000, SN: 00000101\n\n"
           "Using device 0: Generic RTL2832U OEM\nFound Elonics E4000 tuner\n\n"
           "Product:\t\tGeneric E4000\nSerial number:\t\t00000101\n")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return type("R", (), {"stdout": e4000, "stderr": ""})()
    monkeypatch.setattr(probe, "find_tool", lambda *a, **k: "rtl_eeprom")
    monkeypatch.setattr(probe.subprocess, "run", fake_run)
    out = probe.detect_receivers(st)
    assert out["error"] is None
    assert [(f["device"], f["receiver"]) for f in out["found"]] == [(0, "e4000")]
    assert len(calls) == 1               # stops at the count rtl_eeprom reported


def test_open_failure_names_a_wrong_index_not_another_program():
    from marsad import record
    out = ("Found 1 device(s):\n  0:  Acme, Generic E4000, SN: 00000101\n\n"
           "Using device 1: \nFailed to open rtlsdr device #1.\n")
    assert record.eeprom_listing(out) == [(0, "Acme, Generic E4000, SN: 00000101")]
    msg = record.open_failure_message(out, 1, [])
    assert "No receiver at index 1" in msg and "Generic E4000, SN: 00000101 at 0" in msg
    assert "Another program" not in msg
    # index present but will not open: blame another program only when one is running
    busy = ("Found 1 device(s):\n  0:  Acme E4000\n\nUsing device 0: x\nusb_open error -3\n")
    assert "Another program" in record.open_failure_message(busy, 0, ["SDR++.exe"])
    assert "Another program" not in record.open_failure_message(busy, 0, [])
