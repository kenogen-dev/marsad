"""The Builder's time split: listening kept / listening cropped away / retuning.

The arithmetic lives in the page's own JavaScript (marsad/web/builder.html), so the
test runs those functions in node rather than a Python copy that could drift from
them. Known cases are the measured ones in CLAUDE.md: LS at -i 120 listens 83% of
each sweep (47 ms x 432 hops), rtl_power_fftw at 1 s per hop listens 92% (91 ms
overhead: 59 ms plus 16 ms for each of two extra buffers).
"""
import json
import re
import shutil
import subprocess

import pytest

from conftest import ROOT

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

PAGE = ROOT / "marsad" / "web" / "builder.html"


def _function(src: str, name: str) -> str:
    """The source of `function name(...) { ... }`, found by matching braces."""
    start = src.index(f"function {name}(")
    depth, i = 0, src.index("{", start)
    while True:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
        i += 1


def _run(expr: str):
    src = PAGE.read_text(encoding="utf-8")
    consts = re.search(r"^let RTL_MAX.*$", src, re.M).group(0) + "\n" + \
        re.search(r"^let FFTW = .*$", src, re.M).group(0)
    body = "\n".join(_function(src, n) for n in ("geometry", "planHops", "geometryFftw", "timeSplit"))
    js = f"{consts}\n{body}\nprocess.stdout.write(JSON.stringify({expr}));"
    out = subprocess.run([NODE, "-e", js], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_ls_at_120_listens_83_percent_and_crops_a_quarter():
    # 1241M:2148M:50k (clamped to 2^31 Hz), -i 120, -c 0.25: 432 hops, FFT 64, 48 kept.
    sp = _run("timeSplit(geometry(1241, 2147.483648, 50, 120, 3600, 0.25), 120)")
    assert sp["kept"] + sp["cropped"] == pytest.approx(0.8308, abs=1e-4)
    assert sp["kept"] == pytest.approx(0.8308 * 48 / 64, abs=1e-4)
    assert sp["cropped"] == pytest.approx(0.8308 * 16 / 64, abs=1e-4)
    assert sp["retune"] == pytest.approx(0.1692, abs=1e-4)
    assert sum(sp.values()) == pytest.approx(1.0)


def test_fftw_one_second_hops_listen_92_percent():
    # The `top` preset: 2147-2187 MHz, 2.4 MS/s, 128 bins, 1 s, 25% overlap, crop 0.25.
    # 4.8 MB per hop = three reads, 59 + 2 x 16 = 91 ms; 96 of 128 bins kept.
    sp = _run("timeSplit(geometryFftw(2147, 2187, 2400000, 128, 1, 25, 0.25, 3600, null), 1)")
    listen = 1.0 / 1.091
    assert sp["kept"] == pytest.approx(listen * 0.75, abs=1e-4)
    assert sp["cropped"] == pytest.approx(listen * 0.25, abs=1e-4)
    assert sp["retune"] == pytest.approx(1 - listen, abs=1e-4)
    assert sum(sp.values()) == pytest.approx(1.0)


def test_sweep_that_cannot_finish_inside_the_interval_has_no_split():
    # LS needs 20.3 s just to retune its 432 hops, so -i 10 cannot finish a sweep.
    assert _run("timeSplit(geometry(1241, 2147.483648, 50, 10, 3600, 0.25), 10)") is None
