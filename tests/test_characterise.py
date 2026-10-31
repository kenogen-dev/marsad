"""The characteriser (marsad/characterise.py), RECORDER_DESIGN.md phase 1.

Synthetic signals with known answers pin each measurement: a scanning radar's
chirped, staggered pulse train (linear and S-shaped chirps), and continuous CW,
2-FSK, AM and FM carriers. The last test runs the real 2026-10-14 radar recording
when it is on this PC (it is never in git) and checks the pre-registered targets.
"""
from __future__ import annotations

import json
import os
import struct
from pathlib import Path

import numpy as np
import pytest

from marsad import characterise as C

RADAR_WAV = Path(os.environ.get(
    "MARSAD_RADAR_WAV",
    r"C:\mydata\projects\sig_init\SDRplus\recordings\baseband_1265440000Hz_00-32-45_15-10-2026.wav"))


def write_cu8(path: Path, x: np.ndarray, scale: float = 1.0) -> Path:
    """Complex samples in ADC counts -> rtl_sdr uint8 interleaved."""
    iq = np.empty(2 * len(x))
    iq[0::2], iq[1::2] = x.real * scale, x.imag * scale
    path.write_bytes(np.clip(np.round(127.5 + iq), 0, 255).astype(np.uint8).tobytes())
    return path


def write_wav_cs16(path: Path, x: np.ndarray, fs: int) -> Path:
    iq = np.empty(2 * len(x), np.int16)
    iq[0::2] = np.clip(x.real * 32767, -32768, 32767)
    iq[1::2] = np.clip(x.imag * 32767, -32768, 32767)
    data = iq.tobytes()
    hdr = (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
           + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 2, fs, fs * 4, 4, 16)
           + b"data" + struct.pack("<I", len(data)))
    path.write_bytes(hdr + data)
    return path


FS = 2.4e6
PRIS = [1000e-6] * 4 + [1200e-6] * 4 + [1400e-6]     # stagger pattern, repeated
WIDTH = 60e-6
F_START, F_END = -300e3, 400e3


def radar_train(nonlinear: bool = False, seconds: float = 2.2, period: float = 0.5,
                seed: int = 1) -> np.ndarray:
    """A scanning radar: chirped pulses at a staggered PRI, the beam (Gaussian,
    sigma 15 ms) passing every `period` seconds, in 2-count noise."""
    rng = np.random.default_rng(seed)
    n = int(seconds * FS)
    x = (rng.normal(0, 2, n) + 1j * rng.normal(0, 2, n)).astype(np.complex64)
    m = int(WIDTH * FS)
    u = np.arange(m) / m                          # 0..1 across the pulse
    if nonlinear:   # tangent-shaped sweep: fast at the ends, slow in the middle
        shape = np.tan((u - 0.5) * 2.0) / np.tan(1.0) / 2 + 0.5
    else:
        shape = u
    f = F_START + (F_END - F_START) * shape
    pulse = np.exp(2j * np.pi * np.cumsum(f) / FS).astype(np.complex64)
    t, k = 0.0, 0
    while True:
        t += PRIS[k % len(PRIS)]
        k += 1
        i = int(t * FS)
        if i + m >= n:
            break
        off = (t - 0.2) % period
        off = min(off, period - off)              # distance to the nearest beam centre
        amp = 60 * np.exp(-0.5 * (off / 0.015) ** 2)
        if amp > 1:
            x[i:i + m] += amp * pulse
    return x


@pytest.fixture(scope="module")
def radar_result(tmp_path_factory):
    p = write_cu8(tmp_path_factory.mktemp("radar") / "train.cu8", radar_train())
    return C.characterise(p, rate=FS, freq=1265.44e6)


@pytest.mark.xdist_group("radar_synth")      # one worker builds radar_result once
def test_radar_rotation(radar_result):
    rot = radar_result["pulses"]["rotation"]
    assert rot["period_s"] == pytest.approx(0.5, abs=0.002)
    assert radar_result["repeat"]["period_s"] == pytest.approx(0.5, abs=0.02)


@pytest.mark.xdist_group("radar_synth")      # one worker builds radar_result once
def test_radar_pulse_width(radar_result):
    assert radar_result["pulses"]["width_s"] == pytest.approx(WIDTH, abs=1.5e-6)


@pytest.mark.xdist_group("radar_synth")      # one worker builds radar_result once
def test_radar_staggered_pris(radar_result):
    p = radar_result["pulses"]
    assert p["staggered"]
    main = [L for L in p["pri"] if L["count"] >= 0.1 * sum(x["count"] for x in p["pri"])]
    got = sorted(L["pri_s"] for L in main)
    assert got == pytest.approx([1000e-6, 1200e-6, 1400e-6], abs=2e-6)
    runs = {round(L["pri_s"] * 1e6): L.get("run") for L in main}
    assert runs[1000] == 4 and runs[1200] == 4    # the pattern holds 4 intervals at each


@pytest.mark.xdist_group("radar_synth")      # one worker builds radar_result once
def test_radar_linear_chirp(radar_result):
    c = radar_result["pulses"]["chirp"]
    assert c["linear"]
    assert c["start_hz"] == pytest.approx(F_START, abs=40e3)
    assert c["end_hz"] == pytest.approx(F_END, abs=40e3)
    assert c["fit_sweep_hz"] == pytest.approx(F_END - F_START, rel=0.08)
    assert radar_result["family"] == "pulsed, linear FM chirp (radar-like)"


@pytest.mark.xdist_group("radar_synth")      # one worker builds radar_result once
def test_radar_occupied_band_and_recipe(radar_result):
    o = radar_result["occupied"]
    # the 99 % band spills a little past the sweep (the pulse's own edges)
    assert F_START - 100e3 < o["low_hz"] < F_START + 20e3
    assert F_END - 20e3 < o["high_hz"] < F_END + 100e3
    rc = radar_result["recipe"]
    assert rc["duration_s"] == pytest.approx(10.0)          # 7.5 x 0.5 s, floored at 10 s
    assert rc["gain"]["change_db"] == 0
    # ~0.8 MHz fits wholly on one side of DC at a stable rate, inside 80 % of the band
    obw = o["high_hz"] - o["low_hz"]
    assert rc["rtl_rate_hz"] <= 2.56e6 and not rc["notes"][:-1]
    assert rc["signal_above_tune_hz"] > obw / 2
    assert rc["signal_above_tune_hz"] + obw / 2 <= 0.4 * rc["rtl_rate_hz"]
    assert rc["store_rate_hz"] == pytest.approx(2.5 * obw, abs=1e3)


def test_nonlinear_chirp_is_flagged(tmp_path):
    p = write_cu8(tmp_path / "nlfm.cu8", radar_train(nonlinear=True))
    r = C.characterise(p, rate=FS, freq=1e9)
    c = r["pulses"]["chirp"]
    assert not c["linear"] and c["end_to_mid_slope"] > 1.5
    assert r["family"] == "pulsed, non-linear FM chirp (radar-like)"
    assert c["start_hz"] == pytest.approx(F_START, abs=60e3)
    assert c["end_hz"] == pytest.approx(F_END, abs=60e3)


def test_clipping_asks_for_less_gain(tmp_path):
    p = write_cu8(tmp_path / "hot.cu8", radar_train(), scale=3.0)   # pulses hit the rails
    r = C.characterise(p, rate=FS, freq=1e9)
    assert r["clipping"]["rail_fraction"] > 0
    assert r["recipe"]["gain"]["change_db"] < 0


# ---------------------------------------------------------------- continuous

CFS = 240_000


def _noise(n, sd, seed=2):
    rng = np.random.default_rng(seed)
    return (rng.normal(0, sd, n) + 1j * rng.normal(0, sd, n))


def _classify(tmp_path, x, name):
    p = write_wav_cs16(tmp_path / f"baseband_100000000Hz_{name}.wav", x, CFS)
    return C.characterise(p)


def test_cw(tmp_path):
    n = CFS
    t = np.arange(n) / CFS
    r = _classify(tmp_path, 0.3 * np.exp(2j * np.pi * 20e3 * t) + _noise(n, 0.01), "cw")
    assert r["family"] == "CW"
    assert r["tuned_hz"] == 100e6                 # read from the SDR++ style name
    assert r["continuous"]["centre_hz"] == pytest.approx(20e3, abs=200)
    assert r["recipe"]["duration_s"] == 10


def test_fsk_symbol_rate(tmp_path):
    rng = np.random.default_rng(3)
    baud, dev = 1200, 5e3
    bits = rng.integers(0, 2, int(CFS / CFS * baud) + 1)
    sym = np.repeat(bits * 2 - 1, CFS // baud)[:CFS]
    f = 30e3 + dev * sym
    x = 0.3 * np.exp(2j * np.pi * np.cumsum(f) / CFS) + _noise(len(f), 0.01)
    r = _classify(tmp_path, x, "fsk")
    assert r["family"] == "FSK (2-level)"
    c = r["continuous"]
    assert c["symbol_rate_hz"] == pytest.approx(baud, rel=0.05)
    assert c["shift_hz"] == pytest.approx(2 * dev, rel=0.15)


def test_am_and_fm(tmp_path):
    n = CFS
    t = np.arange(n) / CFS
    am = 0.3 * (1 + 0.6 * np.sin(2 * np.pi * 1e3 * t)) * np.exp(2j * np.pi * -15e3 * t)
    assert _classify(tmp_path, am + _noise(n, 0.005), "am")["family"] == "AM-like"
    rng = np.random.default_rng(4)
    msg = np.convolve(rng.normal(0, 1, n), np.ones(80) / 80, "same")   # ~3 kHz audio-like
    msg /= np.abs(msg).max()
    fm = 0.3 * np.exp(2j * np.pi * np.cumsum(10e3 + 5e3 * msg) / CFS)
    assert _classify(tmp_path, fm + _noise(n, 0.005), "fm")["family"] == "FM-like"


# ---------------------------------------------------------------- inputs and output

def test_raw_needs_rate_and_freq(tmp_path):
    p = write_cu8(tmp_path / "x.cu8", _noise(10000, 2))
    with pytest.raises(SystemExit, match="--rate and --freq"):
        C.characterise(p)


def test_sigmf_cf32(tmp_path):
    n = CFS // 2
    t = np.arange(n) / CFS
    x = (0.3 * np.exp(2j * np.pi * -25e3 * t) + _noise(n, 0.01)).astype(np.complex64)
    (tmp_path / "cap.sigmf-data").write_bytes(x.tobytes())
    (tmp_path / "cap.sigmf-meta").write_text(json.dumps({
        "global": {"core:datatype": "cf32_le", "core:sample_rate": CFS},
        "captures": [{"core:sample_start": 0, "core:frequency": 433.92e6}]}))
    r = C.characterise(tmp_path / "cap.sigmf-meta")
    assert r["family"] == "CW" and r["tuned_hz"] == 433.92e6
    assert r["recipe"]["signal_hz"] == pytest.approx(433.92e6 - 25e3, abs=300)


def test_cli_writes_md_and_json_beside_recording(tmp_path):
    p = write_cu8(tmp_path / "train.cu8", radar_train(seconds=1.2))
    assert C.main([str(p), "--rate", str(FS), "--freq", "1265.44e6"]) == 0
    md = (tmp_path / "train.characterise.md").read_text(encoding="utf-8")
    js = json.loads((tmp_path / "train.characterise.json").read_text(encoding="utf-8"))
    assert "Recipe for the next capture" in md
    assert js["pulses"]["pri"] and "_y" not in json.dumps(js)


# ---------------------------------------------------------------- the real recording

@pytest.mark.skipif(not RADAR_WAV.exists(), reason="2026-10-14 radar recording not on this PC")
def test_real_radar_recording_preregistered():
    """RECORDER_DESIGN.md phase 1: rotation 12.0 +/- 0.1 s, pulse width 114 +/- 5 us,
    PRIs near 2745 / 3157 / 3473 us. Three rotations (36 s) keep it to ~12 s.
    The chirp target ("linear, about 1.4 MHz") is pinned to what the data showed:
    a NON-linear (S-shaped) chirp, ~1.6 MHz end to end, ~1.0 MHz by straight-line fit."""
    r = C.characterise(RADAR_WAV, duration_s=36)
    p = r["pulses"]
    assert p["rotation"]["period_s"] == pytest.approx(12.0, abs=0.1)
    assert p["width_s"] == pytest.approx(114e-6, abs=5e-6)
    main = sorted(L["pri_s"] for L in p["pri"] if L["count"] >= 0.1 * sum(x["count"] for x in p["pri"]))
    assert main == pytest.approx([2745e-6, 3157e-6, 3473e-6], abs=3e-6)
    c = p["chirp"]
    assert 1.3e6 < c["sweep_hz"] < 1.9e6
    assert c["end_to_mid_slope"] > 2
    assert r["family"] == "pulsed, non-linear FM chirp (radar-like)"


def test_repeat_period_never_runs_away_on_a_flat_topped_autocorrelation():
    """2026-10-28: gated FSK bursts (0.4 s on every 2.5 s) gave a repeat period of
    -2.8e12 s, because the parabolic refinement divided by a near-zero curvature on a
    flat-topped peak, and the recipe then said "~-0.0 repeats". The period must come out
    near the true one, or not at all."""
    dt = 0.01
    t = np.arange(0, 30, dt)
    gate = (((t - 1.0) % 2.5) < 0.4).astype(float)
    r = C._period_autocorr(gate, dt, 0.1)
    assert r is not None and r["period_s"] == pytest.approx(2.5, abs=dt)
    flat = np.r_[np.ones(400), np.zeros(400)]          # one block: a decay, not a repeat
    r2 = C._period_autocorr(flat, dt, 0.1)
    assert r2 is None or r2["period_s"] > 0
