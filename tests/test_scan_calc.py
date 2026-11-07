"""The desktop window's Live Scan calculations against the web page's own output (relay 009).

tests/data/scan_page_fixtures.json is written by running the page's functions under Node
(tests/data/make_scan_fixtures.mjs) on made-up inputs. Rerun that script after changing the
page; these tests then say whether marsad_desktop/scan_calc.py still gives the same answers.
"""
import json
import math
from pathlib import Path

import numpy as np
import pytest

from marsad_desktop import scan_calc as S

FIX = json.loads((Path(__file__).parent / "data" / "scan_page_fixtures.json").read_text(encoding="utf-8"))


def _arr(xs):
    return np.array([math.nan if v is None else v for v in xs], dtype=float)


def _same(got, want, tol=1e-4):
    want = _arr(want)
    got = np.asarray(got, dtype=float)
    assert got.shape == want.shape
    both_nan = np.isnan(got) & np.isnan(want)
    assert np.array_equal(np.isnan(got), np.isnan(want)), np.flatnonzero(np.isnan(got) != np.isnan(want))[:10]
    d = np.abs(np.where(both_nan, 0, got - want))
    assert d.max() <= tol, (d.argmax(), got[d.argmax()], want[d.argmax()])


@pytest.mark.parametrize("sc", FIX["scenarios"], ids=lambda s: s["name"])
def test_spur_mask_matches_the_page(sc):
    got = S.build_mask(sc["lo"], sc["hi"], 1200, sc["step_k"], [14.4], sc["hop_centres"])
    assert got.tolist() == sc["mask"]


@pytest.mark.parametrize("sc", FIX["scenarios"], ids=lambda s: s["name"])
def test_sweep_floor_matches_the_page(sc):
    _same(S.sweep_floor(_arr(sc["rows"][1]), sc["lo"], sc["hi"]), sc["floor"])


@pytest.mark.parametrize("sc", FIX["scenarios"], ids=lambda s: s["name"])
def test_display_floor_matches_the_page(sc):
    rows = [_arr(r) for r in sc["rows"]]
    assert list(S.display_floor(rows)) == pytest.approx(sc["display_floor"])


@pytest.mark.parametrize("sc", FIX["scenarios"], ids=lambda s: s["name"])
def test_layer_split_matches_the_page(sc):
    row = _arr(sc["rows"][1])
    mask = S.build_mask(sc["lo"], sc["hi"], 1200, sc["step_k"], [14.4], sc["hop_centres"])
    floor = S.sweep_floor(row, sc["lo"], sc["hi"])
    term = _arr(sc["term"])
    assert len(sc["layers"]) == 24
    for case in sc["layers"]:
        got = S.layer_row(row, floor, mask, term if case["term"] else None,
                          case["noise"], case["signal"], case["internal"], case["thr"])
        _same(got, case["row"])


def test_layers_together_give_back_the_measurement():
    sc = FIX["scenarios"][0]
    row = _arr(sc["rows"][1])
    mask = S.build_mask(sc["lo"], sc["hi"], 1200, sc["step_k"], [14.4], sc["hop_centres"])
    floor = S.sweep_floor(row, sc["lo"], sc["hi"])
    _same(S.layer_row(row, floor, mask, _arr(sc["term"])), row.tolist(), tol=1e-3)


@pytest.mark.parametrize("g", FIX["grid"], ids=lambda g: f"{g['lo']}-{g['hi']} x{g['z']}")
def test_family_grid_matches_the_page(g):
    fams = [14.4, 28.8, 24, 25, 100]
    lo, hi, W = g["lo"], g["hi"], g["W"]
    vlo = (g["f_lo"] - lo) / (hi - lo)
    vhi = (g["f_hi"] - lo) / (hi - lo)
    ticks = S.family_ticks(fams, g["f_lo"], g["f_hi"], W / (g["f_hi"] - g["f_lo"]))
    got =[[t["colour"], S.jsround((((t["mhz"] - lo) / (hi - lo)) - vlo) / (vhi - vlo) * W) + 0.5] for t in ticks]
    assert len(got) == len(g["ticks"])
    for (c1, x1), (c2, x2) in zip(got, g["ticks"]):
        assert c1 == c2 and x1 == pytest.approx(x2)
    labels = [f"{t['base']:g}×{t['n']}" for t in ticks if t["labelled"]]
    assert labels == [t for t, _ in g["labels"]]


def test_family_grid_hides_dense_families_and_28_8():
    t = S.family_ticks([14.4, 28.8, 100], 52, 1660, 1100 / 1608)
    assert {x["base"] for x in t} == {100}          # 14.4 would be 10 px apart, 28.8 is every second 14.4


@pytest.mark.parametrize("case", FIX["nearest"], ids=lambda c: str(c["f"]))
def test_nearest_family_matches_the_page(case):
    got = S.nearest_family(case["f"], case["families"])
    assert (list(got) if got else None) == case["near"]


@pytest.mark.parametrize("case", FIX["detections"], ids=lambda c: str(len(c["peaks"])))
def test_detection_line_matches_the_page(case):
    assert S.detection_line(case["peaks"], case["thr"]) == case["text"]
