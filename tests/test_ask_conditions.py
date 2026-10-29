"""`marsad run` with --chain and --conditions must not stop at the station-notes prompts.

2026-10-28: `marsad watch fixed ...; marsad run milair 10h --chain ... --conditions ...`
was left running with the owner away; the run sat at the antenna-shape question and the
10 h map never started. These tests never open the SDR.
"""
import sys
from types import SimpleNamespace

import pytest

from conftest import ROOT

sys.path.insert(0, str(ROOT))

from marsad import record                    # noqa: E402

DEFAULTS = {"antenna_shape": "90 L", "whip_length_mm": 60, "lna": "LaNA",
            "filter": "Flamingo+", "chain_order": "old chain"}


class Tty:
    def isatty(self):
        return True


def args(chain=None, conditions=None, non_interactive=False):
    return SimpleNamespace(chain=chain, conditions=conditions, non_interactive=non_interactive)


def test_chain_and_conditions_skip_every_prompt(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", Tty())

    def no_input(prompt=""):
        raise AssertionError(f"input() called: {prompt!r}")
    monkeypatch.setattr("builtins.input", no_input)
    got = record.ask_conditions(args("ant > XTR > PC", "night, fans on"), DEFAULTS)
    assert got["chain_order"] == "ant > XTR > PC" and got["conditions"] == "night, fans on"
    assert got["antenna_shape"] == "90 L" and got["whip_length_mm"] == 60   # header from station.json
    assert "station-notes questions skipped" in capsys.readouterr().out


@pytest.mark.parametrize("a", [args(chain="ant > XTR > PC"), args(conditions="night"),
                               args(chain="ant > XTR > PC", conditions="   ")])
def test_one_flag_alone_still_asks(monkeypatch, a):
    monkeypatch.setattr(sys, "stdin", Tty())
    asked = []

    def answer(prompt=""):
        asked.append(prompt)
        return "typed"
    monkeypatch.setattr("builtins.input", answer)
    record.ask_conditions(a, DEFAULTS)
    assert any("antenna shape" in p for p in asked) and len(asked) >= 4
