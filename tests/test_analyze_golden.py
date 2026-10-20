"""survey.py analyze regenerates the archived reports byte for byte.

bins.csv holds floor/median/max/occupancy for every bin and peaks.csv every
detection with its tags, so any change to parsing, NaN handling, the floor
estimator, the peak finder or the spur tagger shows up as a diff. The command
lines are the ones recorded in each report's scan_build.md (casc4-noferrite
was re-analysed by hand with no warm-up discard, which is what its report.md
header shows).
"""
import filecmp

import pytest

from conftest import PY, TOOLS, run, survey_csv, report_dir

CASES = [
    # (survey name, extra analyser arguments)
    ("survey_custom_5m_fm-check-withnotch_20261019-0024", ["--receiver", "v5"]),
    ("survey_custom_30m_casc4-noferrite_20261018-1147", ["--receiver", "v5"]),
    ("survey_custom_1h_baseline-term_20261016-2138", ["--receiver", "v5", "--skip-sweeps", "8"]),
    ("survey_custom_1h_cage-off-2_20261017-0121", ["--receiver", "v5", "--skip-sweeps", "8"]),
]


@pytest.mark.parametrize("name,extra", CASES, ids=[c[0] for c in CASES])
def test_analyze_reproduces_archived_report(tmp_path, name, extra):
    csv = survey_csv(name)
    ref = report_dir(name)
    run([PY, TOOLS / "survey.py", "analyze", csv, "--out", tmp_path, *extra])
    out = tmp_path / name
    for f in ("bins.csv", "peaks.csv"):
        assert filecmp.cmp(out / f, ref / f, shallow=False), f"{name}/{f} differs"
