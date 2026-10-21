"""`marsad run` must not silently reuse a default chain that a run already recorded.

Nine command-line runs on 2026-10-20 recorded the previous rig's chain this way.
The guard runs before any hardware is touched, so these tests never open the SDR.
"""
import json
import subprocess
import sys

import pytest

from conftest import PY, ROOT

sys.path.insert(0, str(ROOT))

from marsad import chain                     # noqa: E402
from marsad.config import Station            # noqa: E402

CHAIN = "antenna (test) > XTR > PC"


def _station(tmp_path, used=True, static=False):
    (tmp_path / "station.json").write_text(json.dumps(
        {"defaults": {"chain_order": CHAIN,
                      "chain_used_at": "2026-10-20T23:00:00" if used else None}}), encoding="utf-8")
    st = Station.load(tmp_path)
    if static:
        d = chain.load(st)
        d["chains"]["t"] = {"static": True, "updated": None, "last_used": None,
                            "parts": [{"type": "antenna", "name": "antenna", "detail": "test"},
                                      {"type": "sdr", "name": "XTR", "rx": "xtr"},
                                      {"type": "pc", "name": "PC"}]}
        d["current"] = "t"
        chain.save(st, d)
        chain.use(st, "t")
        # use() clears the stamp as a fresh choice; put it back to test the static path alone
        st.save_defaults({"chain_used_at": "2026-10-20T23:00:00"})
    return st


def _run(tmp_path, *extra):
    r = subprocess.run([PY, "-m", "marsad", "--station", str(tmp_path), "run", "LS", "1m",
                        "--non-interactive", "--no-live", "--no-analyze", *extra],
                       cwd=str(ROOT), capture_output=True, text=True, timeout=120)
    return r.returncode, r.stdout + r.stderr


def test_used_default_chain_is_refused_before_any_hardware_check(tmp_path):
    _station(tmp_path)
    rc, out = _run(tmp_path)
    assert rc != 0
    assert "already used by a run at 2026-10-20 23:00:00" in out and CHAIN in out
    assert "--chain-as-saved" in out and "Nothing was recorded" in out
    assert "Receiver OK" not in out and "SDR is free" not in out       # stopped before the checks


def test_dry_run_still_works_and_names_the_chain_source(tmp_path):
    _station(tmp_path)
    rc, out = _run(tmp_path, "--dry-run")
    assert rc == 0 and "--dry-run: nothing recorded" in out
    assert "chain       (saved default, used by a run at 2026-10-20 23:00:00)" in out


def test_confirmation_paths_let_the_run_through(tmp_path):
    """Each path returns the chain text instead of raising; the real run would then
    go on to the hardware checks, which these tests stop short of."""
    from marsad.record import confirm_default_chain

    class A:
        chain = None; chain_as_saved = False; non_interactive = True
    st = _station(tmp_path, used=False)
    assert confirm_default_chain(st, A) == CHAIN                       # never used: fine
    st = _station(tmp_path, used=True)
    with pytest.raises(SystemExit, match="chain-as-saved"):
        confirm_default_chain(st, A)
    a = A(); a.chain_as_saved = True
    assert confirm_default_chain(st, a) == CHAIN                       # said yes
    a = A(); a.chain = "probe > PC"
    assert confirm_default_chain(st, a) == "probe > PC"                # explicit chain wins
    st = _station(tmp_path, used=True, static=True)
    assert confirm_default_chain(st, A) == CHAIN                       # Chain tab says static


def test_use_for_runs_clears_the_stamp_and_a_run_sets_it(tmp_path):
    st = _station(tmp_path, used=True)
    d = chain.load(st)
    chain.use(st, d["current"])
    assert Station.load(tmp_path).defaults.get("chain_used_at") is None
    st.save_defaults({"chain_order": CHAIN, "chain_used_at": "2026-10-21T01:02:03"})
    on_disk = json.loads((tmp_path / "station.json").read_text(encoding="utf-8"))
    assert on_disk["defaults"]["chain_used_at"] == "2026-10-21T01:02:03"
