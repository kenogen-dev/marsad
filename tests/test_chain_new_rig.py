"""Relay 014: a new rig is kept as a draft drawing from the start (named, in the picker, no chains yet), its
chains reach chain.json on the first Save that has a path, and a taken name is refused. No Qt."""
from __future__ import annotations

import pytest

from marsad_desktop import rig as R

DOC = {"schema": 1, "current": None, "chains": {}}
PARTS = [{"type": "antenna", "name": "Whip"}, {"type": "sdr", "name": "radio-a", "rx": "radio-a"},
         {"type": "pc", "name": "PC"}]


def test_a_draft_is_listed_with_no_chains():
    lay = R.add_draft(R.empty_layout(), "Bench", R.new_rig(), DOC)
    assert lay["rigs"]["Bench"]["chains"] == {}
    d = R.rigs_for_display(DOC, lay)
    assert d["Bench"]["draft"] is True and d["Bench"]["chains"] == {}


def test_a_taken_name_is_refused():
    doc, lay = R.apply_rig(DOC, R.empty_layout(), "Old", R.rig_from_parts(PARTS))
    with pytest.raises(ValueError, match="already a rig or chain"):
        R.add_draft(lay, "Old", R.new_rig(), doc)
    with pytest.raises(ValueError, match="name"):
        R.add_draft(lay, "  ", R.new_rig(), doc)


def test_the_first_save_with_a_path_writes_its_chains_and_keeps_the_draft_in_history():
    lay = R.add_draft(R.empty_layout(), "Bench", R.new_rig(), DOC)
    doc, lay = R.apply_rig(DOC, lay, "Bench", R.rig_from_parts(PARTS))
    assert "Bench" in doc["chains"] and lay["rigs"]["Bench"]["chains"]
    assert len(lay["rigs"]["Bench"]["history"]) == 1                 # the empty draft it replaced
    assert not R.rigs_for_display(doc, lay)["Bench"].get("draft")


def test_a_template_draft_stays_a_draft_until_saved():
    lay = R.add_draft(R.empty_layout(), "T", R.rig_from_parts(PARTS), DOC)
    assert R.rigs_for_display(DOC, lay)["T"]["draft"] is True
