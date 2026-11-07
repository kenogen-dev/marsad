"""The Live Scan screen's rules that are not drawing (relay 009), and the service route it reads.

Pinned:
- a recorder whose status has not changed for 15 s is silent, whatever its file says, timed on the
  window's own forward-only clock: a change of the PC's clock can neither silence a live recorder
  nor keep a dead one alive; "Mark as seen" quiets it until it changes again;
- the survey's position: "sweep N of M in progress · S s left", moving on with the clock between
  answers and never past the end; rtl_power_fftw's pass and planned hops; finished;
- refused hops in the page's words, "T of P hops tuned, K refused";
- /live/progress: the run's facts and position with no sweep rows.
"""
from __future__ import annotations

import pytest

from marsad import live
from marsad_desktop import live_data as L

from test_second_radio import write_run
from test_watch import station


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def rec(updated, state="running", job=True):
    return {"serial": "00000002", "receiver": "radio-b", "state": state, "updated": updated,
            "job": {"kind": "survey pass", "target_mhz": 433.92} if job else None}


def test_silent_after_15_s_without_a_change_whatever_the_file_says():
    clk = Clock()
    w = L.RecorderWatch(clock=clk)
    assert w.update([rec(1.0)])[0]["view"] == "running"
    clk.t += 14.9
    assert w.update([rec(1.0)])[0]["view"] == "running"
    clk.t += 0.2
    v = w.update([rec(1.0)])[0]
    assert v["view"] == "silent" and v["state"] == "running" and v["silent_s"] == pytest.approx(15.1)
    assert L.silent_text(v["silent_s"]) == "silent 15 s"
    clk.t += 300
    assert L.silent_text(w.update([rec(1.0)])[0]["silent_s"]) == "silent 5 min"


def test_a_rewrite_keeps_it_running_and_a_new_rewrite_wakes_a_silent_one():
    clk = Clock()
    w = L.RecorderWatch(clock=clk)
    for k in range(20):                                  # rewritten every 2 s for 40 s
        clk.t += 2
        assert w.update([rec(float(k))])[0]["view"] == "running"
    clk.t += 16
    assert w.update([rec(19.0)])[0]["view"] == "silent"
    clk.t += 1
    assert w.update([rec(20.0)])[0]["view"] == "running"


def test_the_pc_clock_changing_neither_silences_nor_revives():
    """The status's own timestamps jump back an hour (summer time) and forward a day: only
    whether they change counts, on the window's clock."""
    clk = Clock()
    w = L.RecorderWatch(clock=clk)
    w.update([rec(1_800_000_000.0)])
    clk.t += 2
    assert w.update([rec(1_800_000_000.0 - 3600)])[0]["view"] == "running"     # clock set back: still a change
    clk.t += 2
    assert w.update([rec(1_800_086_400.0)])[0]["view"] == "running"            # set forward a day
    clk.t += 15.5                                        # then nothing changes: silent, future stamp or not
    assert w.update([rec(1_800_086_400.0)])[0]["view"] == "silent"


def test_mark_as_seen_quiets_it_until_it_changes():
    clk = Clock()
    w = L.RecorderWatch(clock=clk)
    w.update([rec(5.0)])
    clk.t += 20
    assert w.update([rec(5.0)])[0]["view"] == "silent"
    w.mark_seen("00000002")
    assert w.update([rec(5.0)])[0]["view"] == "seen"
    clk.t += 1
    assert w.update([rec(6.0)])[0]["view"] == "running"
    clk.t += 20
    assert w.update([rec(6.0)])[0]["view"] == "silent"    # a new silence is shown again


def test_stopped_and_starting():
    w = L.RecorderWatch(clock=Clock())
    assert w.update([rec(1.0, state="stopped")])[0]["view"] == "stopped"
    assert w.update([{**rec(2.0, job=False), "serial": "x"}])[0]["view"] == "starting"


def test_the_sweep_bar_moves_with_the_clock_between_answers():
    p = {"file": "a.csv", "total": 84, "expected": 900, "integration_s": 20, "cursor_pct": 0.25}
    a = L.progress(p, fetched_at=100.0, now=100.0)
    assert a["kind"] == "sweep" and a["fraction"] == pytest.approx(0.25)
    assert a["text"] == "sweep 85 of 900 in progress · 15 s left"
    b = L.progress(p, fetched_at=100.0, now=106.0)
    assert b["fraction"] == pytest.approx(0.55) and b["text"].endswith("9 s left")
    c = L.progress(p, fetched_at=100.0, now=200.0)                 # an answer late: never past the end
    assert c["fraction"] == 1.0 and c["text"].endswith("0 s left")
    assert L.progress({**p, "expected": None}, 100.0, 100.0)["text"] == "sweep 85 in progress · 15 s left"


def test_progress_for_fftw_finished_and_nothing():
    f = L.progress({"file": "b.dat", "engine": "rtl_power_fftw", "total": 3, "planned_hops": 22,
                    "eta_s": 125, "cursor_pct": 0.4}, 0, 0)
    assert f == {"fraction": 0.4, "kind": "hop", "text": "pass 4 in progress · 22 hops planned · 2 min 05 s left in the run"}
    d = L.progress({"file": "a.csv", "total": 300, "expected": 300, "finished": True}, 0, 0)
    assert d["kind"] == "done" and d["text"] == "finished · 300 sweeps of 300"
    assert L.progress({}, 0, 0)["kind"] == "waiting"


def test_refused_hops_in_the_pages_words():
    assert L.hops_text(22, 22, [[2171.5, 2173.3]] * 8) == "22 of 22 hops tuned, 8 refused"
    assert L.hops_text(12, 10, []) == "10 of 12 hops tuned"
    assert L.hops_text(12, 12, 2) == "12 of 12 hops tuned, 2 refused"
    assert L.hops_text(None, None, []) is None                     # rtl_power says nothing about hops


def test_current_hop_is_the_one_after_the_cursor():
    cs = [401.0, 403.0, 405.0]
    assert L.current_hop({"cursor": 402.1}, cs, None) == (402.0, 404.0)
    assert L.current_hop({"cursor": 406.0}, cs, None) == (400.0, 402.0)      # past the top: the next pass
    assert L.current_hop({"cursor": None}, cs, None) is None


def test_live_progress_carries_facts_and_no_rows(tmp_path):
    st = station(tmp_path)
    path = write_run(st, "survey_milair_10m_bench-test_20311029-2000.csv", "v5", sweeps=4)
    t = live.get_tailer(st, path.name, 1200, 2000)
    t.poll()
    p = live.progress_payload(st, t, path.name)
    for k in ("rows", "stamps", "medians", "partial", "hop_centres_mhz"):
        assert k not in p
    assert p["total"] == 4 and p["engine"] == "rtl_power" and p["integration_s"] == 10.0
    assert p["finished"] is False and p["live"] == path.name
    f = p["facts"]
    assert f["receiver"] == "v5" and f["started_local"] == "2031-10-29T20:00:00" and f["engine"] == "rtl_power"
    assert f["preset"] == "milair"
    side = path.with_suffix(".json")
    side.write_text(side.read_text().replace('"geometry"', '"finished_local": "2031-10-29T20:10:00", "geometry"'))
    t.poll()
    assert live.run_facts(st, path)["finished_local"] == "2031-10-29T20:10:00"     # the cache follows the sidecar
    assert live.progress_payload(st, t, path.name)["finished"] is True               # an rtl_power run says it is over
