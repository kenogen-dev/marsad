"""The event finder (marsad/events.py, 2026-10-30), pinned on the three cases it was
pre-registered against and on a synthetic survey through the analyser and the CLI.

The real cases are the per-sweep block medians of three surveys (tests/data/events_*_blocks.csv,
made with events.block_series from the raw CSVs, which stay where they are):

- V1-4n, v5 12 h milair night 2026-10-29: a broadband emitter, on 02:20:16 (after a two-sweep
  dip from 02:19:13), off 05:11:47 (after dips and an overshoot from 05:07:45);
- A3, XTR 40 min 2026-10-30: two one-sweep broadband lifts, 08:26:55 and 08:36:26, the first
  inside the report's 10-sweep warm-up skip;
- V1-1b, v5 terminated 8 h 2026-10-27: a 65 min warm-up, then nothing.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from marsad import events as E
from marsad.analyze import Survey

DATA = Path(__file__).resolve().parent / "data"


def case(name: str, skip: int):
    df = pd.read_csv(DATA / f"events_{name}_blocks.csv", comment="#")
    times = pd.to_datetime(df.pop("time"))
    return E.find_series(list(df.columns), df.to_numpy(float), times, skip=skip)


def kind(res, k):
    return [f for f in res.found if f.kind == k]


def at(res, i):
    return E._t(res, i)[11:]


def test_v1_4n_finds_the_night_emitter_on_and_off_to_the_sweep():
    res = case("v1-4n", 130)                 # the report's own warm-up skip
    ev = kind(res, "event")
    main = [f for f in ev if f.change["whole"] >= 0.5]
    assert len(main) == 1
    f = main[0]
    assert at(res, f.start) == "02:20:16" and at(res, f.end) == "05:11:47"
    assert f.shape == "step"
    assert f.change["whole"] == pytest.approx(0.76, abs=0.02)
    assert set(f.moved) == {"250-275", "275-300", "300-325"}
    assert f.change["325-350"] < 0.5 and abs(f.change["375-400"]) < 0.1     # nothing at 375-400
    # the two-sweep dip that leads into it is a transient, and the step's edge is not
    tr = {at(res, t.start): t for t in kind(res, "transient")}
    assert tr["02:19:45"].shape == "one-sweep dip"
    assert "02:20:16" not in tr
    # everything else sustained is the 270 MHz hump reshaping in the morning: only the
    # low blocks, never the whole band by its threshold
    for g in ev:
        if g is not f:
            assert abs(g.change["whole"]) < 0.5 and set(g.moved) <= {"225-250", "250-275", "275-300"}
            assert res.times[g.start].hour >= 6
    # 296.99 MHz switching off at 11:44 is one line: no event there
    assert not [g for g in ev if res.times[g.start].hour == 11]


def test_a3_two_one_sweep_lifts_and_no_sustained_change():
    res = case("a3", 10)
    assert not kind(res, "event") and not kind(res, "warm-up")
    tr = kind(res, "transient")
    assert [(at(res, t.start), t.shape) for t in tr] == [("08:26:55", "one-sweep lift"),
                                                       ("08:36:26", "one-sweep lift")]
    assert all(t.change["whole"] > 1.5 and len(t.moved) >= 3 for t in tr)
    assert tr[0].start == 9                  # inside the skip: transients are looked for there too


def test_v1_1b_warm_up_is_not_an_event():
    quiet = case("v1-1b", 130)
    assert quiet.found == []
    raw = case("v1-1b", 0)
    assert [f.kind for f in raw.found] == ["warm-up"]
    w = raw.found[0]
    assert w.start == 0 and 60 <= w.end <= 135                 # settles in the first ~65 min
    assert w.change["whole"] > 1.0                              # the level fell as it warmed
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
    times = pd.date_range("2026-10-29 00:00", periods=T, freq="60s")
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
    assert "**Step** from 2026-10-29 01:40:00 until 2026-10-29 02:40:00" in rep
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
    assert ("**Step** from 2026-10-29 02:00:00 until 2026-10-29 02:40:00 (40 min)"
            in rep.read_text(encoding="utf-8"))
    assert "already on when the analysed sweeps begin" in rep.read_text(encoding="utf-8")
    assert E.main(["syn", "--skip-sweeps", "0"], st) == 0
    text = rep.read_text(encoding="utf-8")
    assert text.count("## Events") == 1 and "**Step** from 2026-10-29 01:40:00" in text
    assert E.main(["syn"], st) == 0                       # default skip: what the report used (120)
    assert (st.reports / csv.stem / "events.csv").exists()
