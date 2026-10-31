"""station.local.json (2026-10-30): a git-ignored overlay for what must stay off GitHub,
today the station's location.

Pinned: its keys are laid over station.json (dicts merged, anything else replaced);
conditions see the merged location; and every write of station.json (save_defaults,
which `marsad run` and the Chain tab use, and set_value, which calibrate-retune uses)
leaves the local keys out of station.json and the local file untouched. The coordinates
here are made up.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from marsad import conditions
from marsad.config import Station

ROOT = Path(__file__).resolve().parent.parent
LOC = {"lat": 51.48, "lon": -0.01}


def make(tmp_path: Path, local: dict | None) -> Station:
    (tmp_path / "station.json").write_text(json.dumps({
        "_location": "note",
        "retune_ms_per_hop": 50.0,
        "defaults": {"lna": "LaNA", "chain_order": "antenna > PC"},
        "recorder": {"pool_gb": 100, "daily_gb": 20},
    }), encoding="utf-8")
    if local is not None:
        (tmp_path / "station.local.json").write_text(json.dumps(local), encoding="utf-8")
    return Station.load(tmp_path)


def test_local_keys_are_laid_over_station_json(tmp_path):
    st = make(tmp_path, {"location": LOC, "recorder": {"pool_gb": 30}, "_note": "x"})
    assert st.data["location"] == LOC
    assert st.data["recorder"]["pool_gb"] == 30 and st.data["recorder"]["daily_gb"] == 20   # merged, not replaced
    assert st.data["retune_ms_per_hop"] == 50.0 and "_note" not in st.data
    assert conditions.location_of(st.data) == (51.48, -0.01)


def test_no_local_file_means_no_location(tmp_path):
    st = make(tmp_path, None)
    assert "location" not in st.data and conditions.location_of(st.data) is None
    snap = conditions.snapshot(st.data)              # no location: no network call at all
    assert "station.local.json" in snap["weather"]["error"]


def test_writes_leave_local_keys_out_of_station_json(tmp_path):
    st = make(tmp_path, {"location": LOC, "recorder": {"pool_gb": 30}})
    local_before = (tmp_path / "station.local.json").read_bytes()
    st.save_defaults({"chain_order": "antenna > notch > PC", "chain_used_at": "2026-10-30T21:00"})
    st.set_value("retune_ms_per_hop", 48.0)
    on_disk = json.loads((tmp_path / "station.json").read_text(encoding="utf-8"))
    assert "location" not in on_disk
    assert on_disk["recorder"] == {"pool_gb": 100, "daily_gb": 20}          # the overlay's 30 not copied
    assert on_disk["defaults"]["chain_order"] == "antenna > notch > PC" and on_disk["retune_ms_per_hop"] == 48.0
    assert (tmp_path / "station.local.json").read_bytes() == local_before
    again = Station.load(tmp_path)
    assert again.data["location"] == LOC and again.data["recorder"]["pool_gb"] == 30


def test_git_ignores_station_local_json():
    r = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", "station.local.json"])
    assert r.returncode == 0
