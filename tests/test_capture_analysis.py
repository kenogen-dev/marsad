"""The Captures tab's analysis panel.

A synthetic radar-like capture, characterised for real, with no radio. Pinned:
- the stored half reads the newer report over the old one and shows the old label;
- the IQ half finds the report's own pulses, with the times, the intervals and the sweep planted;
- it is worked out behind the page: `iq_view` answers at once, then has it when asked again;
- a private event gets no analysis and a refusal, also for a row claiming to be open;
- a timing-only capture gets timing, width and level, and nothing about how a pulse moves;
- an archived row and a set-aside row render from the report, reading no IQ;
- no report, no pulses and a report gone missing all say so rather than fail;
- the page lays the panel above the facts table, and names what is absent.
"""
from __future__ import annotations

import json
import time
import zipfile
from pathlib import Path

import numpy as np
import pytest

from marsad import capture as cap
from marsad import catalogue as C
from marsad import characterise as ch
from marsad.config import Station

FS = 1e6
PRI = 3e-3
WIDTH = 100e-6
SWEEP = 200e3            # -SWEEP to +SWEEP across each pulse
PASS_S = 1.0
PULSES_PER_PASS = 20


def iq_samples(seconds: float = 4.0) -> np.ndarray:
    """Passes of a chirped pulse train over quiet noise, as ci16."""
    rng = np.random.default_rng(3)
    n = int(seconds * FS)
    x = rng.normal(0, 30, n) + 1j * rng.normal(0, 30, n)
    m = int(WIDTH * FS)
    t = np.arange(m) / FS
    pulse = 8000 * np.exp(2j * np.pi * (-SWEEP * t + SWEEP / WIDTH * t ** 2))
    for k in range(int(seconds / PASS_S)):
        for j in range(PULSES_PER_PASS):
            s = int((0.2 + k * PASS_S + j * PRI) * FS)
            x[s:s + m] += pulse
    out = np.empty(2 * n, np.int16)
    out[0::2], out[1::2] = x.real.astype(np.int16), x.imag.astype(np.int16)
    return out


@pytest.fixture
def st(tmp_path):
    (tmp_path / "station.json").write_text(json.dumps({"recorder": {
        "band_tiers": [{"lo": 800, "hi": 820, "tier": "private", "band": "test private"}],
        "timing_only_bands": [[900, 910]]}}), encoding="utf-8")
    return Station.load(tmp_path)


def add(st, name: str, mhz: float = 500.0, measured: bool = True, **kw) -> tuple:
    day = cap.pool_dir(st) / "2030-01-02"
    day.mkdir(parents=True, exist_ok=True)
    data = day / f"cap_{name}.sigmf-data"
    iq_samples().tofile(data)
    day.joinpath(f"cap_{name}.sigmf-meta").write_text(json.dumps(
        {"global": {"core:sample_rate": FS, "core:datatype": "ci16_le"},
         "captures": [{"core:sample_start": 0, "core:frequency": mhz * 1e6, "core:datetime": "2030-01-02T03:04:05Z"}]}),
        encoding="utf-8")
    md = day / f"cap_{name}.characterise.md"
    row = {"started": "2030-01-02T03:04:05", "started_epoch": 1e9, "freq_hz": mhz * 1e6, "tuned_hz": mhz * 1e6,
           "receiver": "v5", "tier": "open", "iq_status": "written", "iq_path": str(data), "iq_bytes": data.stat().st_size,
           "store_rate_hz": FS, "snr_peak_db": 30.0, "family": None, **kw}
    if measured:
        res = ch.characterise(data)
        md.write_text("# the report", encoding="utf-8")
        md.with_suffix(".json").write_text(json.dumps(ch._clean(res)), encoding="utf-8")
        row.update(characterised=str(md), family=res["family"], capture_snr_db=cap.capture_snr(res))
    con = cap.connect(st)
    i = cap.add_row(con, row)
    con.close()
    return i, data


def analysis_when_ready(st, i: int, wait_s: float = 60.0) -> dict:
    end = time.time() + wait_s
    while time.time() < end:
        a = C.iq_view(st, i)["analysis"]
        if not a.get("pending"):
            return a
        time.sleep(0.05)
    raise AssertionError("the analysis never finished")


def test_the_report_found_what_was_planted(st):
    i, _ = add(st, "radar")
    a = C.event(st, i)["analysis"]
    assert a["grade"] == "strong" and a["family"].startswith("pulsed") and a["source"] == "characterise report"
    p = a["pulses"]
    assert p["count"] == 4 * PULSES_PER_PASS
    assert p["width_s"] == pytest.approx(WIDTH, rel=0.1)
    assert p["pri"][0]["pri_s"] == pytest.approx(PRI, rel=0.01)
    assert p["rotation"]["period_s"] == pytest.approx(PASS_S, rel=0.01)
    assert a["chirp"]["swept"] and a["has_iq"] and a["iq_note"] is None


def test_the_iq_half_gives_each_pulse_every_interval_and_the_sweep(st):
    i, _ = add(st, "radar")
    v = analysis_when_ready(st, i)
    assert v["count"] == len(v["t"]) == 4 * PULSES_PER_PASS
    assert v["t"][0] == pytest.approx(0.2, abs=1e-3)
    assert np.median(np.diff(v["t"][:PULSES_PER_PASS])) == pytest.approx(PRI, rel=0.01)
    pri = v["pri"]
    assert pri["intervals"] == 4 * (PULSES_PER_PASS - 1) and sum(pri["counts"]) == pri["intervals"]
    edges = np.geomspace(pri["lo_s"], pri["hi_s"], len(pri["counts"]) + 1)
    k = int(np.argmax(pri["counts"]))
    assert edges[k] * 0.98 <= PRI <= edges[k + 1] * 1.02
    c = v["curve"]["median_hz"]
    assert len(c) == 48 and v["curve"]["pulses"] == 4 * PULSES_PER_PASS
    # a straight sweep: rising end to end, near its planted edges inside the 50 % extent
    assert np.all(np.diff(c[4:-4]) > 0)
    assert c[4] == pytest.approx(-SWEEP, abs=0.3 * SWEEP) and c[-5] == pytest.approx(SWEEP, abs=0.3 * SWEEP)


def test_the_waterfall_does_not_wait_for_the_pulses(st, monkeypatch):
    i, _ = add(st, "radar")
    gate = []
    real = C.analysis_from_iq

    def slow(*a):
        while not gate:
            time.sleep(0.01)
        return real(*a)
    monkeypatch.setattr(C, "analysis_from_iq", slow)
    first = C.iq_view(st, i)
    assert first["analysis"] == {"pending": True} and first["img"]
    assert C.iq_view(st, i)["analysis"] == {"pending": True}      # asked again: not started twice
    gate.append(1)
    assert analysis_when_ready(st, i)["count"] == 4 * PULSES_PER_PASS


def test_a_failure_costs_the_figure_not_the_waterfall(st, monkeypatch):
    i, _ = add(st, "radar")

    def boom(*a):
        raise ValueError("broken")
    monkeypatch.setattr(C, "analysis_from_iq", boom)
    assert "broken" in analysis_when_ready(st, i)["note"] and C.iq_view(st, i)["img"]


def test_the_newer_report_wins_and_the_old_label_is_kept(st):
    i, data = add(st, "radar")
    con = cap.connect(st)
    con.execute("UPDATE captures SET family = 'OFDM-like' WHERE id = ?", (i,))
    con.commit()
    con.close()
    cfg = cap.settings(st)
    res = ch.characterise(data)
    new = data.with_name(data.name.replace(".sigmf-data", ".characterise.v2.md"))
    new.write_text("# new", encoding="utf-8")
    new.with_suffix(".json").write_text(json.dumps({"result": ch._clean(res)}), encoding="utf-8")
    Path(cfg["_remeasured_index"]).write_text(json.dumps({"rows": {str(i): {
        "family": res["family"], "grade": "strong", "capture_snr_db": 30.0, "report": str(new), "timing_only": False}}}),
        encoding="utf-8")
    a = C.event(st, i)["analysis"]
    assert a["source"] == "re-measured report" and a["family_old"] == "OFDM-like" and a["family"] == res["family"]


def test_a_private_event_gets_nothing(st):
    i, _ = add(st, "secret", mhz=810.0, tier="private", measured=False)
    assert C.event(st, i)["analysis"] is None
    with pytest.raises(C.Refused, match="private"):
        C.iq_view(st, i)


def test_a_row_claiming_open_in_a_private_band_gets_nothing(st):
    i, _ = add(st, "claims", mhz=810.0)            # the row says open; the tier table says private
    assert C.event(st, i)["analysis"] is None
    with pytest.raises(C.Refused, match="private"):
        C.iq_view(st, i)


def test_timing_only_shows_timing_width_and_level_and_nothing_else(st):
    i, _ = add(st, "lead", mhz=905.0)
    a = C.event(st, i)["analysis"]
    assert a["timing_only"] and a["chirp"] is None and a["family"] == "pulsed" and a["family_old"] is None
    assert a["pulses"]["width_s"] and a["pulses"]["pri"] and a["pulses"]["snr_db_max"]
    v = analysis_when_ready(st, i)
    assert v["curve"] is None and v["t"] and v["pri"]
    blob = json.dumps([a, v])
    for word in ("sweep", "start_hz", "fit_start", "median_hz", "FM", '"shape"', "occupied"):
        assert word not in blob, word


def test_an_event_touching_a_timing_only_band_by_its_own_width_counts(st):
    i, _ = add(st, "edge", mhz=910.4)              # 1 MS/s: its band reaches down past 910
    assert C.event(st, i)["analysis"]["timing_only"]


def test_a_set_aside_row_renders_from_its_report(st):
    from marsad import capture_tidy as T
    i, data = add(st, "gone")
    con = cap.connect(st)
    T.ensure_columns(con)
    con.execute("UPDATE captures SET iq_status = 'set aside', set_aside_path = 'elsewhere' WHERE id = ?", (i,))
    con.commit()
    con.close()
    data.unlink()
    a = C.event(st, i)["analysis"]
    assert not a["has_iq"] and "set aside" in a["iq_note"] and a["pulses"]["count"] == 4 * PULSES_PER_PASS
    with pytest.raises(C.Refused, match="set aside"):
        C.iq_view(st, i)


def test_an_archived_row_reads_its_report_from_the_zip(st, tmp_path):
    from marsad import capture_archive as A
    i, data = add(st, "kept")
    js = data.with_name(data.name.replace(".sigmf-data", ".characterise.json"))
    z = tmp_path / "a.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.write(js, "2030-01-02/" + js.name)
    js.unlink()
    data.unlink()
    con = cap.connect(st)
    A.ensure_columns(con)
    con.execute("UPDATE captures SET iq_status = 'archived', archive_path = ? WHERE id = ?", (str(z), i))
    con.commit()
    con.close()
    a = C.event(st, i)["analysis"]
    assert not a["has_iq"] and "archive zip" in a["iq_note"]
    assert a["source"].endswith("zip") and a["pulses"]["count"] == 4 * PULSES_PER_PASS


def test_no_report_or_no_pulses_say_so(st):
    i, _ = add(st, "bare", measured=False)
    a = C.event(st, i)["analysis"]
    assert a["source"] is None and a["pulses"] is None and a["has_iq"]
    assert "no pulse train" in analysis_when_ready(st, i)["note"]


def test_a_report_that_went_missing_does_not_fail(st):
    i, data = add(st, "lost")
    data.with_name(data.name.replace(".sigmf-data", ".characterise.json")).unlink()
    assert C.event(st, i)["analysis"]["source"] is None


def test_an_event_without_iq_says_why(st):
    con = cap.connect(st)
    i = cap.add_row(con, {"started": "2030-01-02T03:04:05", "started_epoch": 1e9, "freq_hz": 5e8, "receiver": "v5",
                          "tier": "open", "iq_status": "below keep level", "snr_peak_db": 7.0})
    con.close()
    a = C.event(st, i)["analysis"]
    assert not a["has_iq"] and "below keep level" in a["iq_note"]


def test_the_page_puts_the_panel_above_the_facts_and_names_what_is_absent():
    html = (Path(C.__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
    assert html.index('id="cs-an"') < html.index('id="cs-facts"') < html.index('id="cs-fp"')
    for said in ("Not shown: this capture touches a timing-only band", "No rotation measured",
                 "The report found no pulse train", "Pulse times are not in the report"):
        assert said in html
