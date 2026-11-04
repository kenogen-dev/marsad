"""`marsad watch tidy`: the IQ of captures graded empty goes to a holding folder. Nothing is deleted.

Synthetic station, no radio. Pinned:
- the default is a plan: nothing moves, and a confirm with the wrong token moves nothing;
- only empty, written, in-pool captures are chosen, on the re-measured grade when there is one;
- held back: strong captures, archived rows, one a recorder is writing, one with events filed inside
  it, anything touching a timing-only band, excluded surveys (config and flag), a date filter;
- the move: IQ files only, relative paths kept, hashes in the manifest, row "set aside", reports
  and fingerprint stay, the pool shrinks; same drive and across drives;
- put-back: back with the old status, byte for byte; refuses to overwrite; says so when gone;
- the page: no capture, no IQ, no audio, no export, no archive, and the report still shows;
- `marsad watch list` shows the re-measured family and grade, and the stored rows do not change;
- the web app's restart names the listener, and /ping is what says it is up.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import pytest

from marsad import capture as cap
from marsad import capture_archive as A
from marsad import capture_tidy as T
from marsad import catalogue as C
from marsad import watch as W
from marsad import webapp
from marsad.config import Station


def sha(p) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


class Args:
    confirm = None
    exclude_survey = None
    before = None
    put_back = False
    ids = None

    def __init__(self, **kw):
        self.__dict__.update(kw)


@pytest.fixture
def st(tmp_path):
    (tmp_path / "station.json").write_text(json.dumps({"recorder": {
        "band_tiers": [], "timing_only_bands": [[900, 910]], "tidy_exclude_surveys": ["held-survey"]}}), encoding="utf-8")
    return Station.load(tmp_path)


def add(st, name, mhz=500.0, family="nothing found", survey="sv", status="written", started="2030-01-02T03:04:05", **kw):
    """One capture: its IQ pair, fingerprint and report, in a dated pool folder."""
    day = cap.pool_dir(st) / "2030-01-02"
    day.mkdir(parents=True, exist_ok=True)
    data = day / f"cap_{name}.sigmf-data"
    data.write_bytes(name.encode() * 1000)
    day.joinpath(f"cap_{name}.sigmf-meta").write_text(json.dumps(
        {"global": {"core:sample_rate": 2.4e5}, "captures": [{"core:frequency": mhz * 1e6}]}), encoding="utf-8")
    fp = day / f"cap_{name}.fp.npz"
    fp.write_bytes(b"fp")
    md = day / f"cap_{name}.characterise.md"
    md.write_text("# the report", encoding="utf-8")
    con = cap.connect(st)
    i = cap.add_row(con, {"started": started, "started_epoch": time.time() - 9000, "freq_hz": mhz * 1e6, "survey": survey,
                          "receiver": "v5", "tier": "open", "iq_status": status, "iq_path": str(data), "iq_bytes": data.stat().st_size,
                          "store_rate_hz": 2.4e5, "fingerprint": str(fp), "family": family, "characterised": str(md), "snr_peak_db": 3.0, **kw})
    con.close()
    return i, data


def status(st, i) -> dict:
    con = cap.connect(st)
    try:
        return dict(con.execute("SELECT * FROM captures WHERE id = ?", (i,)).fetchone())
    finally:
        con.close()


def test_plan_moves_nothing_and_a_wrong_token_moves_nothing(st, capsys):
    i, data = add(st, "a")
    assert W.main(["tidy"], station=st) == 0
    out = capsys.readouterr().out
    assert str(i) in out and "Nothing was moved" in out and "nothing found" in out and "sv" in out
    assert data.exists() and status(st, i)["iq_status"] == "written"
    assert W.main(["tidy", "--confirm", "deadbeef"], station=st) == 1
    assert "does not match" in capsys.readouterr().out and data.exists()


def test_selection_and_what_is_held_back(st, tmp_path):
    good, _ = add(st, "good")
    strong, _ = add(st, "strong", mhz=510, family="pulsed", capture_snr_db=30.0)
    zipped, _ = add(st, "zipped", mhz=520)
    timing, _ = add(st, "timing", mhz=905)
    edge, _ = add(st, "edge", mhz=910.05)                 # 240 kHz wide: its band touches 910
    held, _ = add(st, "held", mhz=530, survey="x-held-survey-y")
    flagged, _ = add(st, "flagged", mhz=540, survey="by-flag")
    late, _ = add(st, "late", mhz=550, started="2031-01-01T00:00:00")
    owner, _ = add(st, "owner", mhz=560)
    con = cap.connect(st)
    A.ensure_columns(con)
    con.execute("UPDATE captures SET archive_path = 'z.zip' WHERE id = ?", (zipped,))
    con.commit()
    now = status(st, owner)["started_epoch"]
    cap.add_row(con, {"started": "2030-01-02T03:04:06", "started_epoch": now + 1, "freq_hz": 560e6, "receiver": "v5",
                      "iq_status": C.INSIDE, "tier": "open"})
    con.close()
    p = T.plan(st, ["by-flag"], "2030-12-31")
    assert [t["id"] for t in p["take"]] == [good]
    why = {h["id"]: h["why"] for h in p["held"]}
    assert "archive" in why[zipped] and "timing_only" in why[timing] and "timing_only" in why[edge]
    assert why[held] == "survey excluded" and why[flagged] == "survey excluded"
    assert "not before" in why[late] and "filed inside" in why[owner]
    assert strong not in why and strong not in [t["id"] for t in p["take"]]


def test_the_grade_is_the_remeasured_one(st):
    now_empty, _ = add(st, "nowempty", family="OFDM-like", capture_snr_db=40.0)
    now_real, _ = add(st, "nowreal", mhz=510, family="nothing found")
    cfg = cap.settings(st)
    Path(cfg["_remeasured_index"]).write_text(json.dumps({"rows": {
        str(now_empty): {"family": "nothing found", "grade": "empty", "report": "r"},
        str(now_real): {"family": "pulsed", "grade": "strong", "capture_snr_db": 30.0, "report": "r"}}}), encoding="utf-8")
    assert [t["id"] for t in T.plan(st)["take"]] == [now_empty]


def test_a_capture_being_written_is_refused(st):
    i, data = add(st, "busy")
    pool = cap.pool_dir(st)
    pool.joinpath("recorder_status_1.json").write_text(json.dumps(
        {"state": "running", "updated": time.time(), "capture": {"file": data.name}}), encoding="utf-8")
    p = T.plan(st)
    assert not p["take"] and "writing" in p["held"][0]["why"]


@pytest.mark.parametrize("same_drive", [True, False])
def test_move_then_put_back(st, monkeypatch, same_drive, capsys):
    if not same_drive:
        monkeypatch.setattr(T, "_same_volume", lambda a, b: False)
    i, data = add(st, "a")
    keep, kdata = add(st, "k", mhz=510, family="pulsed", capture_snr_db=30.0)
    meta = T._meta_of(data)
    want = {data.name: sha(data), meta.name: sha(meta)}
    fp, md = Path(status(st, i)["fingerprint"]), Path(status(st, i)["characterised"])
    p = T.plan(st)
    used = cap.folder_bytes(cap.pool_dir(st))
    out = T.tidy(st, p["token"], today="2030-02-03")
    assert out["moved"] == [i] and not out["failed"] and out["bytes"] == p["bytes"]
    folder = st.root / "recordings" / "set_aside" / "2030-02-03"
    moved = folder / "2030-01-02" / data.name          # the path it had in the pool
    assert moved.exists() and not data.exists() and not meta.exists()
    assert {f.name: sha(f) for f in (moved, T._meta_of(moved))} == want
    assert fp.exists() and md.exists() and kdata.exists()
    assert not list(folder.rglob("*.part"))
    r = status(st, i)
    assert r["iq_status"] == "set aside" and r["set_aside_path"] == str(moved) and r["iq_path"] == str(data)
    m = json.loads((folder / T.MANIFEST).read_text(encoding="utf-8"))
    assert m["entries"][0]["state"] == "moved" and m["entries"][0]["old_status"] == "written"
    assert {f["sha256"] for f in m["entries"][0]["files"]} == set(want.values())
    assert used - cap.folder_bytes(cap.pool_dir(st)) == p["bytes"]
    C._POOL_CACHE["t"] = 0.0
    manifest_bytes = (folder / T.MANIFEST).stat().st_size
    assert C.summary(st)["storage"]["set_aside_gb"] == pytest.approx((p["bytes"] + manifest_bytes) / 1e9)
    # a look at what could go back changes nothing
    assert T.put_back(st)["ready"] == [i] and moved.exists()
    # back it goes
    back = T.put_back(st, confirm=True)
    assert back["returned"] == [i] and not back["blocked"]
    assert {f.name: sha(f) for f in (data, meta)} == want and not moved.exists()
    r = status(st, i)
    assert r["iq_status"] == "written" and r["set_aside_path"] is None
    assert T.put_back(st)["ready"] == []                 # already back
    assert [t["id"] for t in T.plan(st)["take"]] == [i]  # and eligible again


def test_put_back_overwrites_nothing_and_says_when_the_folder_is_gone(st):
    i, data = add(st, "a")
    T.tidy(st, T.plan(st)["token"], today="2030-02-03")
    data.write_bytes(b"something else")
    out = T.put_back(st, confirm=True)
    assert out["blocked"] and "nothing overwritten" in out["blocked"][0]["why"] and data.read_bytes() == b"something else"
    data.unlink()
    held = next((st.root / "recordings" / "set_aside").rglob("*.sigmf-data"))
    held.unlink()
    out = T.put_back(st, confirm=True)
    assert "gone" in out["blocked"][0]["why"] and status(st, i)["iq_status"] == "set aside"


def test_a_failed_move_leaves_the_capture_where_it_was(st, monkeypatch):
    i, data = add(st, "a")
    j, other = add(st, "b", mhz=510)
    real = T._move_checked

    def fails_on_a_meta(src, dst, size, sha_):
        if src.name == "cap_a.sigmf-meta":
            raise OSError("held open")
        real(src, dst, size, sha_)
    monkeypatch.setattr(T, "_move_checked", fails_on_a_meta)
    out = T.tidy(st, T.plan(st)["token"], say=lambda *_: None)
    assert out["moved"] == [j] and out["failed"][0]["id"] == i
    assert data.exists() and T._meta_of(data).exists() and status(st, i)["iq_status"] == "written"
    assert not other.exists() and status(st, j)["iq_status"] == "set aside"


def test_a_holding_folder_inside_the_pool_is_refused(tmp_path):
    (tmp_path / "station.json").write_text(json.dumps({"recorder": {"set_aside": "recordings/captures/x"}}), encoding="utf-8")
    with pytest.raises(C.Refused, match="inside the pool"):
        T.holding_root(Station.load(tmp_path))


def test_the_page_shows_set_aside_with_no_iq_and_no_broken_file(st):
    i, data = add(st, "a")
    T.tidy(st, T.plan(st)["token"])
    e = C.event(st, i)
    assert e["capture"] is None and "capture_note" not in e and e["report"] == "# the report"
    assert e["row"]["iq_status"] == "set aside" and "set_aside" in e["row"]["set_aside_path"]
    for call in (lambda: C.iq_view(st, i), lambda: C.export(st, i, "wav"), lambda: C.audio_plan(st, i, "am")):
        with pytest.raises(C.Refused, match="set aside"):
            call()
    row = next(r for g in C.summary(st)["groups"] for r in g["rows"] if r["id"] == i)
    assert row["inside_of"] is None and row["bytes"] == Path(row["fingerprint"]).stat().st_size
    with pytest.raises(C.Refused, match="set aside"):      # archiving would lose the way back
        A.plan(st, i)


def test_page_hides_the_iq_controls_for_set_aside():
    html = (Path(C.__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
    assert "r.iq_status==='set aside'" in html and "if(e.private||!e.capture)return;" in html


def test_watch_list_shows_the_remeasured_figures(st, capsys):
    i, _ = add(st, "a", family="OFDM-like", capture_snr_db=40.0)
    assert W.main(["list"], station=st) == 0
    assert "strong 40" in capsys.readouterr().out
    cfg = cap.settings(st)
    Path(cfg["_remeasured_index"]).write_text(json.dumps({"rows": {
        str(i): {"family": "nothing found", "grade": "empty", "capture_snr_db": 2.0, "report": "r"}}}), encoding="utf-8")
    before = status(st, i)
    assert W.main(["list"], station=st) == 0
    line = capsys.readouterr().out
    assert "empty 2" in line and "nothing found  (re-measured, was OFDM-like)" in line and "strong" not in line
    assert status(st, i) == before


def test_restart_names_the_listener_not_the_launcher(st, monkeypatch, capsys):
    class Launcher:
        pid = 100                                    # a venv's python.exe: its child listens
    up = []
    monkeypatch.setattr(webapp, "launch_background", lambda *a, **k: up.append(1) or Launcher())
    monkeypatch.setattr(webapp, "viewer_answers", lambda port=8731: {"app": "marsad", "loading": "big.csv"} if up else None)
    monkeypatch.setattr(webapp, "probe_state", lambda port=8731: "idle")
    monkeypatch.setattr(webapp, "stop", lambda port=8731: (True, None))
    monkeypatch.setattr(webapp, "listener_pid", lambda port=8731: 200)
    monkeypatch.setattr(webapp.host, "port_in_use", lambda *a, **k: False)
    assert webapp.main(["restart"], st) == 0
    out = capsys.readouterr().out
    assert "started on 8731, pid 200" in out and "big.csv" in out


def test_ping_is_tried_first_and_an_older_app_still_counts(monkeypatch):
    asked = []

    def old_app(port, path, timeout=1.5):
        asked.append(path)
        return {"follow": True} if path == "/surveys" else None
    monkeypatch.setattr(webapp, "_get_json", old_app)
    assert webapp.viewer_answers(8731) == {"follow": True} and asked == ["/ping", "/surveys"]
    asked.clear()
    monkeypatch.setattr(webapp, "_get_json", lambda port, path, timeout=1.5: asked.append(path) or {"app": "marsad"})
    assert webapp.viewer_answers(8731) == {"app": "marsad"} and asked == ["/ping"]


def test_status_does_not_call_a_busy_app_another_program(st, monkeypatch, capsys):
    monkeypatch.setattr(webapp, "viewer_answers", lambda port=8731: None)
    monkeypatch.setattr(webapp, "listener_pid", lambda port=8731: 200)
    monkeypatch.setattr(webapp.host, "port_in_use", lambda *a, **k: True)
    assert webapp.main(["status"], st) == 1
    out = capsys.readouterr().out
    assert "not answering" in out and "may be busy" in out
