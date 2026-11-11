"""Relay 026: Add Radio fills itself in from Detect, radios can be edited (the serial never), a radio's setup can be
redone with its history kept, and setup items can be removed into an archive and restored. Recorded data is never
deleted (rule 4 as amended 2026-11-11), proved here by running every new action on a station full of made-up
recordings and comparing every file before and after, and by searching the new actions' code for anything that
deletes.

Made-up stations only; no radio is opened.
"""
from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path

import pytest

from marsad import live, setup as S
from marsad.config import Station
from marsad_desktop import rig as R


def new_station(tmp_path) -> Station:
    folder = tmp_path / "Stations" / "Bench"
    S.create_station(folder, "Bench", "a made-up test station")
    st = Station.load(folder)
    S.add_radio(st, "90000001", "bench-a", "e4000")
    S.add_radio(Station.load(folder), "90000002", "bench-r", "r820t")
    return Station.load(folder)


@pytest.fixture
def bench(tmp_path, monkeypatch):
    st = new_station(tmp_path)
    monkeypatch.setattr(live, "radio_in_use", lambda st_, rx: {"blocked": False, "text": ""})
    monkeypatch.setattr(live, "usb_radios", lambda: None)
    return st


def post(st, route, body):
    return live.setup_post(st, "/setup/" + route, body)


def station_json(st) -> dict:
    return json.loads(st.file.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- Detect says what each radio is to this station

def test_detect_marks_each_radio_already_added_removed_earlier_or_new(bench, monkeypatch):
    monkeypatch.setattr(live.host, "running_sdr_programs", lambda: [])
    monkeypatch.setattr(live, "detect_receivers", lambda st: {"found": [
        {"device": 0, "serial": "90000001", "tuner": "Elonics E4000", "product": "Made-up A", "manufacturer": ""},
        {"device": 1, "serial": "90000002", "tuner": "Rafael Micro R820T", "product": "Made-up B"},
        {"device": 2, "serial": "90000003", "tuner": "Rafael Micro R820T", "product": "Made-up spare"}]})
    post(bench, "radio_remove", {"radio": "bench-r"})
    found = {f["serial"]: f for f in post(bench, "detect", {})["found"]}
    assert found["90000001"]["radio"] == "bench-a" and found["90000001"]["tuner_key"] == "e4000"
    assert found["90000002"]["radio"] is None and found["90000002"]["archived"]["radio"] == "bench-r"
    assert found["90000003"]["radio"] is None and found["90000003"]["archived"] is None
    assert found["90000003"]["tuner_key"] == "r820t"


# ---------------------------------------------------------------- Edit: nickname, tuner, maker, product; never the serial

def test_the_serial_can_never_be_edited_but_the_same_serial_is_accepted(bench):
    with pytest.raises(ValueError, match="serial can't be changed"):
        post(bench, "radio_update", {"radio": "bench-a", "serial": "12345678"})
    out = post(bench, "radio_update", {"radio": "bench-a", "serial": "90000001", "maker": "MadeUp",
                                       "product": "Made-up A"})
    assert out["entry"]["usb"] == {"maker": "MadeUp", "product": "Made-up A"}
    assert station_json(bench)["recorder"]["serials"]["bench-a"] == "90000001"


def test_a_nickname_nothing_uses_yet_can_change_everywhere_the_setup_keeps_it(bench):
    S.redo_radio(bench, "bench-a")                       # something in the setup history to carry over
    assert post(bench, "radio_uses", {"radio": "bench-a"})["uses"] == []
    out = post(bench, "radio_update", {"radio": "bench-a", "nickname": "radio-one"})
    assert out["radio"] == "radio-one"
    st = station_json(bench)
    assert "radio-one" in st["receivers"] and "bench-a" not in st["receivers"]
    assert st["recorder"]["serials"]["radio-one"] == "90000001" and st["default_receiver"] == "radio-one"
    doc = S.load(bench)
    assert "radio-one" in doc["radios"] and doc["radios"]["radio-one"]["renamed"][0]["from"] == "bench-a"
    assert doc["radios"]["radio-one"]["redo_since"]


def test_a_nickname_a_run_or_rig_uses_stays_and_says_why(bench):
    (bench.root / "surveys").mkdir(exist_ok=True)
    (bench.root / "surveys" / "survey_x.json").write_text(json.dumps({"receiver": "bench-a"}))
    (bench.root / "chain_layout.json").write_text(json.dumps({"rigs": {"R": {"nodes": [
        {"part": {"type": "sdr", "rx": "bench-a"}}]}}}))
    uses = post(bench, "radio_uses", {"radio": "bench-a"})["uses"]
    assert any("1 run" in u for u in uses) and "the rig drawings" in uses
    before = bench.file.read_text()
    with pytest.raises(ValueError, match="already used by"):
        post(bench, "radio_update", {"radio": "bench-a", "nickname": "other", "maker": "X"})
    assert bench.file.read_text() == before                  # a refused rename changes nothing at all


# ---------------------------------------------------------------- Redo this radio's setup

def test_redo_keeps_every_result_and_marks_the_steps_to_redo(bench):
    S.record(bench, "bench-a", {"at": "2026-11-01T10:00:00", "step": "retune", "short": False, "ok": True,
                                "retune_ms_per_hop": 47, "note": "47 ms per hop"})
    S.confirm_rig(bench, "bench-a", "")
    assert S.checklist(bench, "bench-a", "", set())["steps"][1]["sub"][2]["state"] == "done"
    post(bench, "radio_redo", {"radio": "bench-a"})
    c = S.checklist(Station.load(bench.root), "bench-a", "", set())
    retune = next(x for x in c["steps"][1]["sub"] if x["step"] == "retune")
    assert retune["state"] == "to redo" and "kept in its history" in retune["detail"] and retune["results"] == 1
    assert c["steps"][0]["state"] == "done" and not c["ready"]
    assert S.load(bench)["radios"]["bench-a"]["results"]["retune"][0]["retune_ms_per_hop"] == 47   # still there
    S.record(bench, "bench-a", {"at": S.now(), "step": "retune", "short": False, "ok": True,
                                "retune_ms_per_hop": 48, "note": "48 ms per hop"})
    c = S.checklist(Station.load(bench.root), "bench-a", "", set())
    retune = next(x for x in c["steps"][1]["sub"] if x["step"] == "retune")
    assert retune["state"] == "done" and retune["results"] == 2


def test_redo_and_remove_wait_while_a_setup_step_runs(bench, monkeypatch):
    monkeypatch.setattr(live, "setup_running", lambda st: {("bench-a", "range")})
    with pytest.raises(ValueError, match="running"):
        post(bench, "radio_redo", {"radio": "bench-a"})
    with pytest.raises(ValueError, match="can't be removed now"):
        post(bench, "radio_remove", {"radio": "bench-a"})
    assert "bench-a" in station_json(bench)["receivers"]


def test_a_radio_in_use_cannot_be_removed_and_the_refusal_says_why(bench, monkeypatch):
    monkeypatch.setattr(live, "radio_in_use", lambda st_, rx: {"blocked": True, "text": "rtl_power holds it."})
    with pytest.raises(ValueError, match="in use, so it can't be removed now. rtl_power holds it"):
        post(bench, "radio_remove", {"radio": "bench-a"})


# ---------------------------------------------------------------- Remove and Restore

def test_remove_archives_first_then_takes_it_off_the_active_list(bench):
    S.record(bench, "bench-a", {"at": S.now(), "step": "retune", "short": False, "ok": True, "note": "47 ms"})
    raw = station_json(bench)
    raw["recorder"]["roles"] = {"surveyor": "bench-a", "recorder": "bench-r"}
    bench.file.write_text(json.dumps(raw))
    out = post(bench, "radio_remove", {"radio": "bench-a"})
    st = station_json(bench)
    assert "bench-a" not in st["receivers"] and "bench-a" not in st["recorder"]["serials"]
    assert st["default_receiver"] == "bench-r" and st["recorder"]["roles"] == {"recorder": "bench-r"}
    arch = json.loads((bench.root / S.ARCHIVE_FILE).read_text())
    item = arch["radios"][0]
    assert item["radio"] == "bench-a" and item["serial"] == "90000001" and item["receiver"]["tuner"] == "e4000"
    assert item["roles"] == {"surveyor": "bench-a"} and item["setup"]["results"]["retune"][0]["note"] == "47 ms"
    assert [a["radio"] for a in out["status"]["archived"]] == ["bench-a"]
    assert [r["radio"] for r in out["status"]["radios"]] == ["bench-r"]


def test_restore_brings_it_back_with_its_history_and_the_archive_keeps_the_record(bench):
    S.record(bench, "bench-a", {"at": S.now(), "step": "retune", "short": False, "ok": True, "note": "47 ms"})
    post(bench, "radio_remove", {"radio": "bench-a"})
    aid = S.archived_radios(Station.load(bench.root))[0]["id"]
    out = post(Station.load(bench.root), "radio_restore", {"id": aid})
    assert out["radio"] == "bench-a" and out["status"]["archived"] == []
    st = station_json(bench)
    assert st["receivers"]["bench-a"]["tuner"] == "e4000" and st["recorder"]["serials"]["bench-a"] == "90000001"
    assert S.load(bench)["radios"]["bench-a"]["results"]["retune"][0]["note"] == "47 ms"
    arch = json.loads((bench.root / S.ARCHIVE_FILE).read_text())
    assert arch["radios"][0]["restored"] and arch["radios"][0]["restored_as"] == "bench-a"


def test_adding_a_removed_serial_again_offers_restore_first(bench):
    post(bench, "radio_remove", {"radio": "bench-a"})
    st = Station.load(bench.root)
    with pytest.raises(ValueError, match="Restore it"):
        post(st, "radio", {"serial": "90000001", "nickname": "again", "tuner": "e4000"})
    out = post(st, "radio", {"serial": "90000001", "nickname": "again", "tuner": "e4000", "fresh": True})
    assert out["radio"] == "again"


def test_restore_is_refused_when_its_serial_is_taken_now(bench):
    post(bench, "radio_remove", {"radio": "bench-a"})
    st = Station.load(bench.root)
    post(st, "radio", {"serial": "90000001", "nickname": "again", "tuner": "e4000", "fresh": True})
    aid = S.archived_radios(Station.load(bench.root))[0]["id"]
    with pytest.raises(ValueError, match="already this station's again"):
        post(Station.load(bench.root), "radio_restore", {"id": aid})


def test_a_rig_with_a_path_to_a_removed_radio_keeps_it_flagged_in_checks(bench):
    post(bench, "radio_remove", {"radio": "bench-a"})
    assert live.chain_layout_summary(Station.load(bench.root))["removed_radios"] == {"bench-a": "90000001"}
    rig = {"nodes": [{"id": "a", "x": 0, "y": 0, "part": {"type": "antenna", "name": "Dipole"}},
                     {"id": "s", "x": 200, "y": 0, "part": {"type": "sdr", "name": "Radio A", "rx": "bench-a"}}],
           "wires": [{"from": "a", "to": "s", "port": 0}]}
    texts = [c["text"] for c in R.checks(rig, {"bench-a": "90000001"})]
    assert any(t.startswith("radio removed: bench-a") for t in texts)
    assert not any("radio removed" in c["text"] for c in R.checks(rig))       # still a radio: nothing flagged
    lv = {c["text"]: c["level"] for c in R.checks_by_level(rig, {"bench-a": "90000001"})}
    assert lv[next(t for t in lv if t.startswith("radio removed"))] == "problem"


# ---------------------------------------------------------------- recorded data is never deleted

def _tree(root: Path) -> dict:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


RECORDED = ("surveys", "reports", "recordings", "logs")


def test_no_new_action_changes_or_deletes_any_recorded_file(bench, monkeypatch):
    monkeypatch.setattr(live.host, "running_sdr_programs", lambda: [])
    monkeypatch.setattr(live, "detect_receivers", lambda st: {"found": [
        {"device": 0, "serial": "90000001", "tuner": "Elonics E4000", "product": "Made-up A"}]})
    root = bench.root
    for rel, body in (("surveys/survey_x_20261101-1000.csv", "2026-11-01, 10:00:00, 1, 2, 1, 1, -30\n"),
                      ("surveys/survey_x_20261101-1000.json", json.dumps({"receiver": "bench-r"})),
                      ("reports/survey_x/report.md", "# report\n"),
                      ("recordings/captures/2026-11-01/cap.sigmf-data", "\x00" * 64),
                      ("recordings/captures/2026-11-01/cap.sigmf-meta", "{}"),
                      ("logs/setup_range_bench-a.log", "tuner-sweep: progress 1 of 2\n")):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    (root / "occupancy.db").write_bytes(b"SQLite format 3\x00" + b"\x00" * 100)
    recorded = lambda: {k: v for k, v in _tree(root).items()
                        if k.split("\\")[0].split("/")[0] in RECORDED or k == "occupancy.db"}
    before = recorded()
    post(bench, "detect", {})
    post(bench, "radio_uses", {"radio": "bench-a"})
    post(bench, "radio_update", {"radio": "bench-a", "nickname": "radio-one", "maker": "MadeUp"})
    post(Station.load(root), "radio_redo", {"radio": "radio-one"})
    post(Station.load(root), "radio_remove", {"radio": "radio-one"})
    aid = S.archived_radios(Station.load(root))[0]["id"]
    post(Station.load(root), "radio_restore", {"id": aid})
    post(Station.load(root), "radio_remove", {"radio": "bench-r"})
    assert recorded() == before


DELETES = ("unlink(", "rmtree", "os.remove(", "rmdir(", "send2trash", ".remove(Path", "shutil.move(")


def test_the_new_actions_contain_no_deleting_call():
    """Every new action, searched: the service's routes and the setup functions they call, and the window's buttons."""
    import ast
    desk = Path(__file__).resolve().parents[1] / "marsad_desktop"     # read as text: no Qt in the public suite

    def methods(module: str, cls: str, names) -> list:
        text = (desk / module).read_text(encoding="utf-8")
        node = next(n for n in ast.walk(ast.parse(text)) if isinstance(n, ast.ClassDef) and n.name == cls)
        found = {f.name: ast.get_source_segment(text, f) for f in node.body if isinstance(f, ast.FunctionDef)}
        assert set(names) <= set(found), set(names) - set(found)
        return [found[n] for n in names]

    sources = [inspect.getsource(f) for f in (S.load_archive, S._save_archive, S._write_station, S.archived_radios,
                                             S.nickname_uses, S.rename_radio, S.redo_radio, S.remove_radio,
                                             S.restore_radio, S.add_radio)]
    route = inspect.getsource(live._setup_post)
    sources.append(route[route.index('"/setup/radio_restore"'):route.index('"/setup/accept_typical"')])
    for module, cls, names in (
            ("setup_screen.py", "AddRadioDialog", ("save", "take_other", "skip_other", "reject", "detect")),
            ("setup_screen.py", "EditRadioDialog", ("save", "redetect")),
            ("setup_screen.py", "RemovedRadiosDialog", ("restore",)),
            ("setup_screen.py", "SetupDialog", ("remove_radio", "redo_radio", "removed_radios", "add_radio",
                                                "edit_radio")),
            ("chain_screen.py", "ChainScreen", ("archive_rig", "restore_rig", "_lib_remove", "_lib_put_back")),
            ("scan_start.py", "StartScanDialog", ("_change",)), ("scan_start.py", "StartWatchDialog", ("_change",))):
        sources += methods(module, cls, names)
    for src in sources:
        assert not any(d in src for d in DELETES), src[:200]
    # and the files written in place are only ever replaced whole, never removed
    assert "os.replace(tmp, p)" in inspect.getsource(S._save_archive)
