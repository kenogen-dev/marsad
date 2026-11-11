"""Relay 023: New Station's folder. The default follows the name, characters Windows refuses are replaced (and said),
a name already used there moves to the next free folder, and nothing is made before Create. Made-up folders only."""
from __future__ import annotations

from marsad import setup as S


def test_a_name_becomes_a_safe_folder_name():
    assert S.safe_name("Bench") == ("Bench", False)
    assert S.safe_name("a:b?") == ("a-b", True)
    assert S.safe_name("  trailing. ") == ("trailing", True)
    assert S.safe_name("") == ("station", True)


def test_the_default_follows_the_name_and_nothing_is_made(tmp_path):
    f = S.free_default_folder("Bench", tmp_path)
    assert f == tmp_path / "Marsad" / "Stations" / "Bench"
    assert not (tmp_path / "Marsad").exists()


def test_a_name_already_used_moves_to_the_next_free_folder(tmp_path):
    S.create_station(S.free_default_folder("Bench", tmp_path), "Bench")
    second = S.free_default_folder("Bench", tmp_path)
    assert second.name == "Bench 2" and S.folder_problem(second) is None
    S.create_station(second, "Bench")
    assert S.free_default_folder("Bench", tmp_path).name == "Bench 3"


def test_an_empty_existing_folder_is_still_the_default(tmp_path):
    first = S.default_folder("Bench", tmp_path)
    first.mkdir(parents=True)
    assert S.free_default_folder("Bench", tmp_path) == first
