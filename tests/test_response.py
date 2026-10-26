"""marsad response on a synthetic trio of bridge runs.

open flat at -10 dB, load flat at -40 dB (bridge directivity 30 dB), antenna
equal to open except a 20 dB dip at a known bin and a 27 dB dip at another,
which is inside the 6 dB margin of the directivity and must be flagged
at-least. A fourth run with a different gain in its sidecar must be refused.
"""
import json
import tempfile
from pathlib import Path
import sys

import numpy as np
import pandas as pd
import pytest

from conftest import ROOT

sys.path.insert(0, str(ROOT))

from marsad import response                      # noqa: E402
from marsad.analyze import Survey                # noqa: E402

STEP_HZ = 50e3
F0_HZ = 2100e6
N = 1201                                          # 2100.0 - 2160.0 MHz


def survey(level_db: float, dips=()) -> dict:
    """A run dict as load_run() would return it, three identical sweeps."""
    f = F0_HZ + np.arange(N) * STEP_HZ
    row = np.full(N, level_db, dtype=float)
    for i, depth in dips:
        row[i] -= depth
    P = np.vstack([row, row, row])
    s = Survey(name="synthetic", freqs_hz=f, times=pd.DatetimeIndex([pd.Timestamp("2026-10-21")] * 3),
               power_db=P, step_hz=STEP_HZ, hop_centres_hz=np.array([2130.0e6]))
    return {"tag": f"syn-{level_db:g}", "path": None, "sidecar": {}, "fingerprint": "fp",
            "freqs_hz": s.freqs_hz, "median_db": np.nanmedian(P, axis=0), "step_hz": STEP_HZ,
            "hop_centres_hz": s.hop_centres_hz, "sweeps": 3, "receiver": "xtr"}


DIP = 400          # 2120.0 MHz, 20 dB
DEEP = 1000        # 2150.0 MHz, 27 dB: within 6 dB of the 30 dB directivity
HOP = 600          # 2130.0 MHz: the hop centre, must be dropped


def test_return_loss_swr_and_flags():
    op = survey(-10.0)
    ld = survey(-40.0)
    an = survey(-10.0, dips=[(DIP, 20.0), (DEEP, 27.0)])
    df = response.compute(op, an, ld, gaps_mhz=[], smooth_mhz=0.0, margin_db=6.0)

    assert df.loc[DIP, "rl_db"] == pytest.approx(20.0)
    rho = 10 ** (-20 / 20)
    assert df.loc[DIP, "swr"] == pytest.approx((1 + rho) / (1 - rho), abs=1e-3)   # 1.222
    assert df.loc[DIP, "flag"] == "ok"
    assert df.loc[DIP, "floor_db"] == pytest.approx(30.0)

    assert df.loc[DEEP, "rl_db"] == pytest.approx(27.0)
    assert df.loc[DEEP, "flag"] == "at-least"            # 27 >= 30 - 6

    assert df.loc[0, "rl_db"] == 0.0 and df.loc[0, "swr"] == 99.0   # total reflection
    assert df.loc[HOP, "flag"] == "dropped"
    assert (df["flag"] == "dropped").sum() == 1           # +/-25 kHz = the centre bin only at 50 kHz


def test_smoothing_spreads_at_least_but_keeps_raw_columns():
    op, ld = survey(-10.0), survey(-40.0)
    an = survey(-10.0, dips=[(DEEP, 27.0)])
    df = response.compute(op, an, ld, gaps_mhz=[], smooth_mhz=0.5, margin_db=6.0)   # 11-bin window
    assert df.loc[DEEP, "rl_db"] == pytest.approx(27.0)              # raw untouched
    assert df.loc[DEEP, "rl_smooth_db"] == 0.0                       # median of one dip bin in 11 is 0
    assert df.loc[DEEP - 5, "flag"] == "at-least" and df.loc[DEEP - 6, "flag"] == "ok"


def test_headline_finds_the_dip_bandwidth_and_offset():
    op, ld = survey(-10.0), survey(-40.0)
    # a 2 MHz wide 20 dB dip around 2120 MHz; smoothed SWR 1.22 inside it
    an = survey(-10.0, dips=[(i, 20.0) for i in range(DIP - 20, DIP + 21)])
    df = response.compute(op, an, ld, gaps_mhz=[], smooth_mhz=0.2, margin_db=6.0)
    head = response.headline(df, f0_mhz=2130.0, has_load=True)
    assert abs(head["dip"]["freq_mhz"] - 2120.0) <= 1.0
    assert head["dip"]["swr"] == pytest.approx(1.222, abs=0.01)
    assert head["bw2"]["width_mhz"] == pytest.approx(2.0, abs=0.2)
    assert head["offset"]["mhz"] == pytest.approx(-10.0, abs=1.0)
    assert head["vouch"]["pct"] == 100.0


def test_receiver_floor_flag_is_optional_and_explicit():
    op, ld = survey(-10.0), survey(-40.0)
    an = survey(-10.0, dips=[(DIP, 20.0)])
    df = response.compute(op, an, ld, gaps_mhz=[], smooth_mhz=0.0, rx_floor_db=-34.0)
    assert df.loc[DIP, "flag"] == "at-least"         # -30 dB is within 6 dB of -34
    df2 = response.compute(op, an, ld, gaps_mhz=[], smooth_mhz=0.0)
    assert df2.loc[DIP, "flag"] == "ok"


def test_outputs_and_atlas_json(tmp_path):
    op, ld = survey(-10.0), survey(-40.0)
    an = survey(-10.0, dips=[(i, 20.0) for i in range(DIP - 20, DIP + 21)])
    df = response.compute(op, an, ld, gaps_mhz=[], smooth_mhz=0.2)
    head = response.headline(df, 2130.0, True)

    class A:
        smooth_mhz = 0.2; margin_db = 6.0; rx_floor_db = None
    hl = response.write_outputs(tmp_path, "syn GP", {"open": op, "load": ld, "antenna": an},
                                df, head, 2130.0, A)
    assert any(line.startswith("dip") for line in hl)
    atlas = json.loads((tmp_path / "atlas.json").read_text())
    assert set(atlas) == {"name", "when", "z0", "f0_mhz", "source", "fingerprint", "points"}
    assert atlas["z0"] == 50 and atlas["f0_mhz"] == 2130.0
    assert set(atlas["points"][0]) == {"f", "swr", "rl", "flag"}
    assert all(p["flag"] in ("ok", "at-least") for p in atlas["points"])   # dropped omitted
    # 1200 non-dropped bins decimate to at most 800, and the dip bin itself survives
    assert len(atlas["points"]) <= 800
    dip_f = head["dip"]["freq_mhz"]
    assert any(abs(p["f"] - dip_f) < 1e-6 for p in atlas["points"])
    assert min(p["swr"] for p in atlas["points"]) == pytest.approx(head["dip"]["swr"], abs=0.01)
    (tmp_path / "response.csv").stat()
    assert sum(1 for _ in open(tmp_path / "response.csv")) == N + 1    # csv keeps every bin
    assert (tmp_path / "response.csv").exists() and "## Headline" in (tmp_path / "response.md").read_text()


def test_different_fingerprints_are_refused():
    op = survey(-10.0)
    an = survey(-10.0)
    an["fingerprint"] = "2100.000-2160.000 MHz | 50.0000 kHz | g42 | i10 | c0.25 | F9 | xtr"
    with pytest.raises(SystemExit, match="different fingerprints"):
        response.check_same_fingerprint({"open": op, "antenna": an})


def test_swr_bounds_from_the_bridge_leak():
    # 20 dB return loss through a 30 dB bridge: rho 0.1 +/- 0.0316
    op, ld = survey(-10.0), survey(-40.0)
    an = survey(-10.0, dips=[(DIP, 20.0)])
    df = response.compute(op, an, ld, gaps_mhz=[], smooth_mhz=0.0)
    lo, hi = 0.1 - 10 ** -1.5, 0.1 + 10 ** -1.5
    assert df.loc[DIP, "swr_lo"] == pytest.approx((1 + lo) / (1 - lo), abs=1e-3)   # 1.141
    assert df.loc[DIP, "swr_hi"] == pytest.approx((1 + hi) / (1 - hi), abs=1e-3)   # 1.303
    # return loss deeper than the directivity: the bridge cannot rule out a perfect match
    an2 = survey(-10.0, dips=[(DIP, 35.0)])
    df2 = response.compute(op, an2, ld, gaps_mhz=[], smooth_mhz=0.0)
    assert df2.loc[DIP, "swr_lo"] == 1.0
    # without a load run the range is unknown, not invented
    df3 = response.compute(op, an, None, gaps_mhz=[], smooth_mhz=0.0)
    assert df3["swr_lo"].isna().all() and df3["swr_hi"].isna().all()


def test_weak_bridge_says_dip_not_resolvable():
    # 12 dB directivity everywhere, as the 3-port bridge measured at 1.25 GHz. The
    # antenna reads 11 dB return loss at one end (within the margin: at-least) and
    # 4 dB elsewhere (trusted). The old headline named the best trusted bin, a 4 dB
    # "dip"; the honest one says the bridge cannot resolve it and points at the
    # at-the-ceiling end instead.
    op, ld = survey(-10.0), survey(-22.0)
    an = survey(-14.0, dips=[(i, 7.0) for i in range(0, 40)])
    df = response.compute(op, an, ld, gaps_mhz=[], smooth_mhz=0.2, margin_db=6.0)
    head = response.headline(df, f0_mhz=2101.0, has_load=True)
    assert head["dip"] is None and head["bw2"] is None
    assert "vouches nowhere" in head["unresolved"]
    assert head["ceiling"]["freq_mhz"] < 2102.0
    assert head["ceiling"]["rl_db"] == pytest.approx(11.0)
    assert head["ceiling"]["swr_lo"] == pytest.approx(1.06, abs=0.01)    # rho 0.282 - 0.251
    assert "offset" not in head and head["at_f0"]["freq_mhz"] == pytest.approx(2101.0, abs=0.05)

    class A:
        smooth_mhz = 0.2; margin_db = 6.0; rx_floor_db = None
    hl = response.write_outputs(Path(tempfile.mkdtemp()), "weak", {"open": op, "load": ld, "antenna": an},
                                df, head, 2101.0, A)
    assert any("not resolvable with this bridge" in line for line in hl)
    assert any("AT THE BRIDGE'S CEILING" in line for line in hl)


def test_few_trusted_bins_is_not_a_dip():
    # a good bridge (30 dB) but the receiver floor flags almost everything
    op, ld = survey(-10.0), survey(-40.0)
    an = survey(-10.0, dips=[(DIP, 20.0)])
    df = response.compute(op, an, ld, gaps_mhz=[], smooth_mhz=0.0, rx_floor_db=-9.0)
    head = response.headline(df, None, has_load=True)
    assert head["pct_ok"] < 5.0
    assert head["dip"] is None and "trusted" in head["unresolved"]


GP = ("br3-gp-open", "br3-gp-load", "br3-gp1265-cut0")


@pytest.mark.skipif(not all(any((ROOT / "surveys").glob(f"*{t}_*.csv")) for t in GP),
                    reason="the 2026-10-25 1265 GP bridge runs are not on this machine")
def test_real_1265_gp_trio_is_unresolved_with_bounds():
    from marsad.config import Station
    st = Station.load()
    runs = {k: response.load_run(st, t) for k, t in zip(("open", "load", "antenna"), GP)}
    df = response.compute(runs["open"], runs["antenna"], runs["load"], gaps_mhz=[],
                          smooth_mhz=5.0, margin_db=6.0, rx_floor_db=-38.1)
    head = response.headline(df, 1265.0, has_load=True)
    assert head["dip"] is None and head["unresolved"]          # not "dip 1462.8 MHz, SWR 4.30"
    assert head["ceiling"]["freq_mhz"] < 1245.0
    row = df.iloc[(df["freq_mhz"] - 1241.0).abs().argsort()[:1]].iloc[0]
    assert row["swr_lo"] == pytest.approx(1.0, abs=0.05)
    assert 2.7 <= row["swr_hi"] <= 3.2
