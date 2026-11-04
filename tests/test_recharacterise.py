"""Measuring older captures again: it adds reports and changes nothing that was there.
Synthetic captures and a real archive zip made by the program, no radio."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import zipfile
from pathlib import Path

import numpy as np
import pytest

from marsad import capture as cap
from marsad import capture_archive as CA
from marsad import catalogue as C
from marsad import characterise as ch
from marsad import recharacterise as RC
from marsad import watch_report as WR
from test_watch import station

RNG = np.random.default_rng(11)
RATE = 240e3


def pulsed() -> bytes:
    """Six seconds of noise with a 200 us pulse every 20 ms, as ci16."""
    n = int(RATE * 6)
    x = (RNG.standard_normal(n) + 1j * RNG.standard_normal(n)) * 0.002
    t = np.arange(n) / RATE
    x = x + 0.3 * ((t % 20e-3) < 200e-6)
    iq = np.empty(2 * n, np.int16)
    iq[0::2], iq[1::2] = x.real * 32768, x.imag * 32768
    return iq.tobytes()


def steady() -> bytes:
    """A steady carrier a little way off centre: not pulsed, so it reaches the continuous path."""
    n = int(RATE * 6)
    t = np.arange(n) / RATE
    x = (RNG.standard_normal(n) + 1j * RNG.standard_normal(n)) * 0.002 + 0.2 * np.exp(2j * np.pi * 20e3 * t)
    iq = np.empty(2 * n, np.int16)
    iq[0::2], iq[1::2] = x.real * 32768, x.imag * 32768
    return iq.tobytes()


def add_capture(st, con, freq: float, payload: bytes, name: str, new_recorder: bool = False, **row) -> int:
    pool = cap.pool_dir(st)
    day = pool / "2030-01-01"
    day.mkdir(parents=True, exist_ok=True)
    data = day / f"{name}.sigmf-data"
    data.write_bytes(payload)
    g = {"core:datatype": "ci16_le", "core:sample_rate": RATE}
    if new_recorder:
        g["marsad:cut_cutoff"] = cap.CUT_CUTOFF
    (day / f"{name}.sigmf-meta").write_text(json.dumps({"global": g, "captures": [{"core:frequency": freq}]}))
    md = day / f"{name}.characterise.md"
    md.write_text("old report")
    md.with_suffix(".json").write_text(json.dumps({"pulses": {"snr_db_max": 44.0}}))
    fp = day / f"{name}.fp.npz"
    fp.write_bytes(b"fp")
    return cap.add_row(con, {"started": "2030-01-01T00:00:00", "started_epoch": 1.9e9, "freq_hz": freq, "target_hz": freq,
                             "tier": "open", "survey": "survey_x", "receiver": "r820t", "iq_status": "written",
                             "captured": 1, "iq_path": str(data), "iq_bytes": len(payload), "family": "OFDM-like",
                             "characterised": str(md), "snr_peak_db": 30.0, "fingerprint": str(fp), **row})


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def tree(root: Path) -> dict:
    return {str(p): sha(p) for p in root.rglob("*") if p.is_file()}


def rows_of(st) -> list:
    con = sqlite3.connect(st.db)
    try:
        return [tuple(r) for r in con.execute("SELECT * FROM captures ORDER BY id")]
    finally:
        con.close()


def test_adds_reports_and_changes_no_row_report_or_capture(tmp_path):
    st = station(tmp_path)
    con = cap.connect(st)
    i = add_capture(st, con, 1500e6, pulsed(), "a")
    con.close()
    pool = cap.pool_dir(st)
    before_files, before_rows = tree(pool), rows_of(st)
    out = RC.run(st, say=lambda *_: None)
    assert out["done"] == 1 and not out["failed"]
    after = tree(pool)
    assert all(after[k] == v for k, v in before_files.items())        # everything that was there is unchanged
    assert rows_of(st) == before_rows
    new = sorted(Path(p).name for p in set(after) - set(before_files))
    assert new == ["a.characterise.v2.json", "a.characterise.v2.md", cap.REMEASURED_INDEX]
    entry = cap.remeasured_index(cap.settings(st))[str(i)]
    assert entry["family"].startswith("pulsed") and entry["old"]["family"] == "OFDM-like"
    assert entry["old"]["capture_snr_db"] == 44.0
    md = (pool / "2030-01-01" / "a.characterise.v2.md").read_text(encoding="utf-8")
    assert "not comparable" in md and "OFDM-like" in md


def test_a_second_run_overwrites_nothing(tmp_path):
    st = station(tmp_path)
    con = cap.connect(st)
    add_capture(st, con, 1500e6, pulsed(), "a")
    con.close()
    RC.run(st, say=lambda *_: None)
    md = cap.pool_dir(st) / "2030-01-01" / "a.characterise.v2.md"
    md.write_text("kept")
    assert RC.run(st, say=lambda *_: None)["done"] == 0
    assert md.read_text() == "kept"


def test_a_capture_from_the_current_recorder_is_left_alone(tmp_path):
    st = station(tmp_path)
    con = cap.connect(st)
    add_capture(st, con, 1500e6, pulsed(), "a", new_recorder=True)
    con.close()
    out = RC.run(st, say=lambda *_: None)
    assert out["done"] == 0 and out["current"] == 1
    assert not list(cap.pool_dir(st).rglob("*.v2.*"))


def test_a_private_row_is_never_opened(tmp_path, monkeypatch):
    st = station(tmp_path, {"band_tiers": [{"lo": 1490, "hi": 1510, "tier": "private", "band": "b"}]})
    con = cap.connect(st)
    add_capture(st, con, 1500e6, pulsed(), "a")      # a row that claims open in a band the table says is private
    con.close()
    monkeypatch.setattr(ch, "characterise", lambda *a, **k: pytest.fail("analysed a private capture"))
    out = RC.run(st, say=lambda *_: None)
    assert out["done"] == 0 and "private" in out["passed"][0][1]


def archived(st, i) -> Path:
    r = CA.archive(st, i, confirm=True)
    return Path(r["zip"])


def test_an_archived_capture_is_read_in_place_and_the_zip_is_untouched(tmp_path):
    st = station(tmp_path)
    payload = pulsed()
    con = cap.connect(st)
    i = add_capture(st, con, 1500e6, payload, "a")
    con.close()
    direct = ch.characterise(cap.pool_dir(st) / "2030-01-01" / "a.sigmf-meta")
    zp = archived(st, i)
    h, mtime = sha(zp), zp.stat().st_mtime_ns
    out = RC.run(st, say=lambda *_: None)
    assert out["done"] == 1
    assert sha(zp) == h and zp.stat().st_mtime_ns == mtime
    js = zp.parent / "recharacterised" / zp.stem / "a.characterise.v2.json"
    got = json.loads(js.read_text())
    assert got["remeasured"]["source"]["sha256_ok"] and "zip" in got["remeasured"]["source"]["read"]
    # the same answer as reading the capture from the pool: the offset is right
    assert got["result"]["family"] == direct["family"]
    assert got["result"]["pulses"]["width_s"] == pytest.approx(direct["pulses"]["width_s"], rel=1e-6)
    assert [p.name for p in zp.parent.glob("*.zip")] == [zp.name]


def test_a_damaged_or_compressed_member_is_refused(tmp_path):
    st = station(tmp_path)
    con = cap.connect(st)
    i = add_capture(st, con, 1500e6, pulsed(), "a")
    con.close()
    zp = archived(st, i)
    bad = bytearray(zp.read_bytes())
    with zipfile.ZipFile(zp) as z:
        info = z.getinfo("2030-01-01/a.sigmf-data")
    bad[info.header_offset + 2000] ^= 0xFF
    broken = tmp_path / "broken.zip"
    broken.write_bytes(bad)
    with pytest.raises(Exception):
        RC.open_member(broken, "a.sigmf-data")

    squeezed = tmp_path / "squeezed.zip"
    with zipfile.ZipFile(zp) as z, zipfile.ZipFile(squeezed, "w", zipfile.ZIP_DEFLATED) as out:
        for n in z.namelist():
            out.writestr(n, z.read(n))
    with pytest.raises(ValueError, match="compressed"):
        RC.open_member(squeezed, "a.sigmf-data")


def test_timing_only_band_gets_no_continuous_analysis(tmp_path, monkeypatch):
    st = station(tmp_path, {"timing_only_bands": [[1490, 1510]]})
    con = cap.connect(st)
    a = add_capture(st, con, 1500e6, steady(), "inside")
    b = add_capture(st, con, 1510.05e6, steady(), "touching")      # its band reaches into the listed one
    c = add_capture(st, con, 1600e6, steady(), "outside")
    con.close()
    real, called = ch.continuous_features, []

    def spy(src, *k, **kw):
        called.append(src.f0)
        return real(src, *k, **kw)
    monkeypatch.setattr(ch, "continuous_features", spy)
    assert RC.run(st, say=lambda *_: None)["done"] == 3
    assert called == [1600e6]
    idx = cap.remeasured_index(cap.settings(st))
    assert idx[str(a)]["family"] == ch.TIMING_ONLY_FAMILY and idx[str(b)]["family"] == ch.TIMING_ONLY_FAMILY
    assert idx[str(a)]["timing_only"] and not idx[str(c)]["timing_only"]
    report = json.loads((cap.pool_dir(st) / "2030-01-01" / "inside.characterise.v2.json").read_text())
    assert "continuous" not in report["result"]


def test_timing_only_touches_no_tier(tmp_path):
    cfg = {"timing_only_bands": [[1490, 1510]], "band_tiers": []}
    assert cap.timing_only(1500e6, 240e3, cfg) and not cap.timing_only(1520e6, 240e3, cfg)
    assert not cap.timing_only(None, 240e3, cfg) and cap.band_tier(1500e6, cfg)["tier"] == "open"


def test_the_list_and_event_page_show_the_new_figures_and_keep_the_old_label(tmp_path):
    st = station(tmp_path)
    con = cap.connect(st)
    i = add_capture(st, con, 1500e6, pulsed(), "a")
    con.close()
    RC.run(st, say=lambda *_: None)
    r = next(x for g in C.summary(st)["groups"] for x in g["rows"] if x["id"] == i)
    assert r["family"].startswith("pulsed") and r["family_old"] == "OFDM-like"
    assert "not comparable" in r["grade_source"]
    e = C.event(st, i)
    assert e["row"]["family_old"] == "OFDM-like" and "not comparable" in e["report_new"]
    assert e["report"] == "old report"


def test_the_report_finds_shared_timing_from_the_new_reports(tmp_path, monkeypatch):
    st = station(tmp_path)
    con = cap.connect(st)
    a = add_capture(st, con, 1300e6, pulsed(), "a")
    b = add_capture(st, con, 1500e6, pulsed(), "b")
    con.close()
    assert not WR.build(st)["timing"]                      # neither row stored any timing
    t = {"pri_us": [1000.0, 1200.0], "rotation_s": 7.0, "width_us": 90.0}
    cfg = cap.settings(st)
    RC.save_index(cfg, {str(i): {"family": "pulsed", "grade": "strong", "capture_snr_db": 30.0, "timing": t, "report": "x"}
                        for i in (a, b)})
    assert WR.build(st)["timing"]


def test_one_wide_signal_is_not_a_timing_link_with_itself():
    timing = json.dumps({"pri_us": [1000.0, 1200.0], "rotation_s": 7.0})
    rows = [{"freq_hz": f, "timing": timing} for f in (1300.0e6, 1300.4e6, 1500.0e6, 1500.2e6, 1300.1e6)]
    assert WR.timing_links(rows) == [(1300.0, 1500.0)]


# ---------------------------------------------------------------- the harmonic flag, out of sight

def test_harmonic_flag_is_hidden_and_its_code_kept(tmp_path):
    tiers = {"band_tiers": [{"lo": 200, "hi": 240, "tier": "private", "band": "some band"}]}
    assert cap.SHOW_HARMONIC is False
    assert cap.harmonic_note(433e6, tiers) == "possible harmonic of some band"     # kept for the test that will score it
    assert cap.shown_harmonic(433e6, tiers) is None
    assert cap.hide_harmonic("a; possible harmonic of some band; b") == "a; b"
    assert cap.hide_harmonic("possible harmonic of some band") is None

    st = station(tmp_path, tiers)
    con = cap.connect(st)
    i = add_capture(st, con, 433e6, pulsed(), "a", notes="possible harmonic of some band")
    con.close()
    assert not C.event(st, i)["row"]["harmonic"] and C.event(st, i)["row"]["notes"] is None
    assert "harmonic" not in WR.to_text(WR.build(st))
    assert "possible harmonic" not in json.dumps(C.summary(st), default=str)


def test_known_signal_limit_is_off_by_default(tmp_path):
    assert cap.settings(station(tmp_path))["known_cooldown_min"] == 0
