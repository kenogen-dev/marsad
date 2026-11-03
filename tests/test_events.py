"""The event finder (marsad/events.py), on generated per-sweep block medians and on a
synthetic survey through the analyser and the CLI. The generated series mimic three things
the finder must tell apart: a broadband emitter that dips before it switches on and off, two
one-sweep lifts with no sustained change, and a slow warm-up followed by nothing.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from marsad import events as E
from marsad.analyze import Survey


BLOCKS = ["225-250", "250-275", "275-300", "300-325", "325-350", "350-375", "375-400"]
START = "2031-01-15 00:00:00"


def series(T, sweep_s, sigma=0.02, seed=5):
    """A flat series per block plus the whole band, with a little independent noise."""
    rng = np.random.default_rng(seed)
    names = BLOCKS + ["whole"]
    return names, -40 + sigma * rng.standard_normal((T, len(names))), pd.date_range(START, periods=T, freq=f"{sweep_s}s")


def emitter_case():
    """12 h at 31 s a sweep: +0.8 dB over 250-325 MHz from sweep 400 to 700, led in by a
    two-sweep dip, plus a -0.3 dB dip just before it goes off."""
    names, P, times = series(1400, 31)
    P[400:700, 1:4] += 1.0
    P[400:700, 7] += 0.8
    P[398:400, 1:4] -= 1.0
    P[398:400, 7] -= 0.8
    return names, P, times


def lifts_case():
    """80 sweeps of 30 s: two one-sweep lifts over most of the band, the first at sweep 9."""
    names, P, times = series(80, 30)
    for s in (9, 28):
        P[s, :5] += 2.5
        P[s, 7] += 2.2
    return names, P, times


def warmup_case():
    """8 h at 30 s a sweep, settling exponentially by about 1.9 dB over the first hour."""
    names, P, times = series(960, 30, sigma=0.012)
    fall = 2.0 * np.exp(-np.arange(960) / 40.0)
    return names, P + fall[:, None], times


def kind(res, k):
    return [f for f in res.found if f.kind == k]


def at(res, i):
    return E._t(res, i)[11:]


def test_emitter_is_dated_on_and_off_to_the_sweep():
    names, P, times = emitter_case()
    res = E.find_series(names, P, times, skip=100)
    ev = kind(res, "event")
    assert len(ev) == 1
    f = ev[0]
    assert (f.start, f.end) == (400, 700) and f.shape == "step"
    assert f.change["whole"] == pytest.approx(0.8, abs=0.03)
    assert set(f.moved) == {"250-275", "275-300", "300-325"}
    assert abs(f.change["375-400"]) < 0.1
    # the dip that leads into it is a transient, and the step's edge is not
    tr = [t.start for t in kind(res, "transient")]
    assert any(398 <= s <= 399 for s in tr) and 400 not in tr


def test_two_one_sweep_lifts_and_no_sustained_change():
    names, P, times = lifts_case()
    res = E.find_series(names, P, times, skip=10)
    assert not kind(res, "event") and not kind(res, "warm-up")
    tr = kind(res, "transient")
    assert [(t.start, t.shape) for t in tr] == [(9, "one-sweep lift"), (28, "one-sweep lift")]
    assert all(t.change["whole"] > 1.5 and len(t.moved) >= 3 for t in tr)   # the first is inside the skip


def test_warm_up_is_not_an_event():
    names, P, times = warmup_case()
    assert E.find_series(names, P, times, skip=300).found == []
    raw = E.find_series(names, P, times, skip=0)
    assert [f.kind for f in raw.found] == ["warm-up"]
    w = raw.found[0]
    assert w.start == 0 and 60 <= w.end <= 300
    assert w.change["whole"] > 1.0
    assert raw.sigma[-1] < 0.02


# ---------------------------------------------------------------- synthetic, through the analyser

def synthetic(T=240, step=(100, 160), lifts=(40, 200), seed=3):
    """225-400 MHz in 1 MHz bins, one sweep a minute: a +1 dB step over 225-350 for
    sweeps 100-159 and two one-sweep +2 dB lifts over 225-300."""
    rng = np.random.default_rng(seed)
    f = np.arange(225e6, 400e6 + 1, 1e6)
    P = -40 + 0.3 * rng.standard_normal((T, len(f)))
    P[step[0]:step[1], f < 350e6] += 1.0
    for t in lifts:
        P[t, f < 300e6] += 2.0
    times = pd.date_range("2031-10-29 00:00", periods=T, freq="60s")
    return Survey("syn", f, times, P, 1e6, np.array([]))


def test_synthetic_step_and_lifts():
    res = E.find(synthetic())
    ev = kind(res, "event")
    assert len(ev) == 1 and (ev[0].start, ev[0].end) == (100, 160) and ev[0].shape == "step"
    assert set(ev[0].moved) == {"225-250", "250-275", "275-300", "300-325", "325-350"}
    assert ev[0].change["375-400"] == pytest.approx(0, abs=0.1)
    assert [(t.start, t.shape) for t in kind(res, "transient")] == [(40, "one-sweep lift"), (200, "one-sweep lift")]


def test_one_block_changing_shape_is_not_an_event():
    s = synthetic(lifts=())
    s.power_db[100:160] -= np.where(s.freqs_hz < 350e6, 1.0, 0.0)       # undo the step
    f = s.freqs_hz
    s.power_db[100:160, (f >= 250e6) & (f < 275e6)] += 1.0             # one block up
    s.power_db[100:160, (f >= 275e6) & (f < 300e6)] -= 1.0             # its neighbour down
    assert kind(E.find(s), "event") == []


def write_csv(path: Path, s: Survey) -> None:
    """The survey as an rtl_power CSV, one hop per sweep."""
    with open(path, "w", newline="\n") as fh:
        for t, row in zip(s.times, s.power_db):
            vals = ", ".join(f"{v:.2f}" for v in row)
            fh.write(f"{t:%Y-%m-%d}, {t:%H:%M:%S}, {int(s.freqs_hz[0])}, {int(s.freqs_hz[-1])}, "
                     f"{s.step_hz:.2f}, 10, {vals}\n")


def test_analyze_writes_events_csv_and_the_report_section(tmp_path):
    from marsad import analyze as A
    csv = tmp_path / "survey_milair_4h_syn.csv"
    write_csv(csv, synthetic())
    A.main(["analyze", str(csv), "--out", str(tmp_path / "reports"), "--skip-sweeps", "10"])
    out = tmp_path / "reports" / csv.stem
    rep = (out / "report.md").read_text(encoding="utf-8")
    assert rep.index("## Events") < rep.index("## Images")
    assert "**Step** from 2031-10-29 01:40:00 until 2031-10-29 02:40:00" in rep
    df = pd.read_csv(out / "events.csv")
    assert list(df["kind"]) == ["transient", "event", "transient"]
    assert df.loc[1, "start_sweep"] == 101 and df.loc[1, "sweeps"] == 60


def test_cli_on_an_existing_report_uses_its_skip_and_replaces_the_section(tmp_path):
    from marsad import analyze as A
    from marsad.config import Station
    st = Station.load(tmp_path)
    st.surveys.mkdir(parents=True, exist_ok=True)
    csv = st.surveys / "survey_milair_4h_syn.csv"
    write_csv(csv, synthetic())
    A.main(["analyze", str(csv), "--out", str(st.reports), "--skip-sweeps", "120"])
    rep = st.reports / csv.stem / "report.md"
    assert E.report_skip(rep) == 120
    # the step began before the skip: with the report's skip it is dated from the first
    # analysed sweep and says so
    assert ("**Step** from 2031-10-29 02:00:00 until 2031-10-29 02:40:00 (40 min)"
            in rep.read_text(encoding="utf-8"))
    assert "already on when the analysed sweeps begin" in rep.read_text(encoding="utf-8")
    assert E.main(["syn", "--skip-sweeps", "0"], st) == 0
    text = rep.read_text(encoding="utf-8")
    assert text.count("## Events") == 1 and "**Step** from 2031-10-29 01:40:00" in text
    assert E.main(["syn"], st) == 0                       # default skip: what the report used (120)
    assert (st.reports / csv.stem / "events.csv").exists()
