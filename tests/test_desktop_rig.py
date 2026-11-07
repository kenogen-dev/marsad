"""The desktop window's rig model: connected parts in, today's chain text out.

The text every run records must not change because the chain is now drawn on a
grid: a straight chain opened as a drawing gives back its text byte for byte, a
split rig gives one chain per radio with the text a person types today, and
chain.json stays readable by the old web app.
"""
import json
import sys

import pytest

from conftest import ROOT

sys.path.insert(0, str(ROOT))

from marsad import chain                      # noqa: E402
from marsad.config import Station             # noqa: E402
from marsad_desktop import rig as R           # noqa: E402


def P(type_, name, detail="", **f):
    return {"type": type_, "name": name, "detail": detail, **f}


# Made-up chains covering every part type and field today's chain.json can hold.
STRAIGHT = {
    "plain": [P("antenna", "antenna", "whip on the sill"), P("coax", "Coax", length="2 m"),
              P("sdr", "radio A", rx="rx1"), P("usb", "USB cable"), P("pc", "PC")],
    "every-type": [P("probe", "near-field probe", "held where the Probe tab says"),
                   P("adapter", "SMA adapter"), P("dcblock", "SMA DC block"),
                   P("filter", "FM notch", "first"), P("lna", "LNA", "powered over the coax"),
                   P("attenuator", "SMA attenuator", "two pads", db=16),
                   P("bulkhead_sma", "SMA bulkhead"), P("jumper", "SMA jumper", "S-bend", length='6"'),
                   P("sdr", "radio B", "in a box", rx="rx2"), P("bulkhead_usb", "USB bulkhead"),
                   P("usb", "USB cable", "1' with chokes", length="1'"), P("isolator", "USB isolator"),
                   P("pc", "PC")],
    "load": [P("load", "50 ohm load"), P("custom", "rear plate", "odd (part), with commas"),
             P("sdr", "radio A", rx="rx1"), P("pc", "PC")],
    "one-part": [P("pc", "PC")],
}


@pytest.mark.parametrize("name", list(STRAIGHT))
def test_a_straight_chain_opened_as_a_drawing_gives_its_text_back(name):
    parts = chain.validate({"chains": {name: {"parts": STRAIGHT[name]}}})["chains"][name]["parts"]
    rig = R.rig_from_parts(parts)
    assert R.chain_names(name, rig) == [name]
    got = R.rig_chains(name, rig)[name]
    assert got == parts                                          # same parts, same fields
    assert chain.chain_text(got) == chain.chain_text(parts)      # so the same text


def _split_rig():
    """antenna > notch > LNA > splitter, then two branches to two radios."""
    rig = R.new_rig()
    a = R.new_node(rig, "antenna", 0, 0)
    a["part"].update(name="antenna", detail="dipole")
    n = R.new_node(rig, "filter", 180, 0)
    l = R.new_node(rig, "lna", 360, 0)
    s = R.new_node(rig, R.SPLITTER, 540, 0)
    s["part"]["name"] = "2-way splitter"
    s["ports"] = ["RFout1 (right)", "RFout2 (left)"]
    c2 = R.new_node(rig, "coax", 720, -80)
    c2["part"].update(name="3 ft coax", length="3 ft")
    x = R.new_node(rig, "sdr", 900, -80)
    x["part"].update(name="radio B", rx="rx2")
    pc2 = R.new_node(rig, "pc", 1080, -80)
    d1 = R.new_node(rig, "dcblock", 720, 80)
    v = R.new_node(rig, "sdr", 900, 80)
    v["part"].update(name="radio A", rx="rx1")
    pc1 = R.new_node(rig, "pc", 1080, 80)
    for src, port, dst in [(a, 0, n), (n, 0, l), (l, 0, s), (s, 1, c2), (c2, 0, x), (x, 0, pc2),
                           (s, 0, d1), (d1, 0, v), (v, 0, pc1)]:
        assert R.connect(rig, src["id"], port, dst["id"]) is None
    return rig


def test_a_split_rig_gives_one_chain_per_radio_with_the_typed_text():
    rig = _split_rig()
    texts = R.rig_texts("two radios", rig)
    assert texts == {
        "two radios (radio A)": "antenna (dipole) > FM notch > LNA > 2-way splitter (RFout1 (right)) "
                                "> SMA DC block > radio A > PC",
        "two radios (radio B)": "antenna (dipole) > FM notch > LNA > 2-way splitter (RFout2 (left)) "
                                "> 3 ft coax > radio B > PC",
    }
    assert R.problems(rig) == []


def test_split_chains_are_plain_chain_json_that_the_old_app_accepts():
    rig = _split_rig()
    doc, lay = R.apply_rig({"schema": 1, "current": None, "chains": {}}, R.empty_layout(), "two radios", rig)
    checked = chain.validate(doc)                       # what the old app does on load
    for name, parts in R.rig_chains("two radios", rig).items():
        assert checked["chains"][name]["parts"] == parts
    assert all(p["type"] in chain.PART_TYPES for c in checked["chains"].values() for p in c["parts"])


def test_wiring_rules_refuse_in_words():
    rig = R.new_rig()
    a, b, c = (R.new_node(rig, t) for t in ("antenna", "coax", "pc"))
    assert R.connect(rig, a["id"], 0, b["id"]) is None
    assert "already wired" in R.connect(rig, a["id"], 0, c["id"])
    assert "source" in R.connect(rig, b["id"], 0, a["id"])
    assert R.connect(rig, b["id"], 0, c["id"]) is None
    assert "loop" in R.connect(rig, c["id"], 0, b["id"]) or "already has an input" in R.connect(rig, c["id"], 0, b["id"])
    assert R.snap(29) == 20 and R.snap(31) == 40


def _station(tmp_path):
    (tmp_path / "station.json").write_text(json.dumps({}), encoding="utf-8")
    return Station.load(tmp_path)


def test_drawn_rig_survives_an_old_app_save_and_a_change_there_wins(tmp_path):
    st = _station(tmp_path)
    base = chain.save(st, {"chains": {"plain": {"parts": STRAIGHT["plain"]}}, "current": "plain"})
    doc, lay = R.apply_rig(base, R.empty_layout(), "two radios", _split_rig())
    chain.save(st, doc)
    shown = R.rigs_for_display(chain.load(st), lay)
    assert set(shown) == {"plain", "two radios"}
    assert not shown["two radios"]["stale_drawing"]
    assert len(shown["two radios"]["chains"]) == 2

    # The old app saves the whole document back untouched: the drawing still holds.
    chain.save(st, chain.load(st))
    assert not R.rigs_for_display(chain.load(st), lay)["two radios"]["stale_drawing"]

    # The old app edits one branch: chain.json is the truth, the drawing is set aside.
    d = chain.load(st)
    d["chains"]["two radios (radio A)"]["parts"][1]["detail"] = "changed in the old app"
    chain.save(st, d)
    shown = R.rigs_for_display(chain.load(st), lay)
    assert "two radios" not in shown
    assert shown["two radios (radio A)"]["stale_drawing"]
    assert chain.chain_text(shown["two radios (radio A)"]["rig"] and
                            R.rig_chains("x", shown["two radios (radio A)"]["rig"])["x"]) == \
        chain.chain_text(d["chains"]["two radios (radio A)"]["parts"])


def test_nothing_is_dropped_and_a_rename_moves_current(tmp_path):
    st = _station(tmp_path)
    doc = chain.save(st, {"chains": {"plain": {"parts": STRAIGHT["plain"]}}, "current": "plain"})
    rig = _split_rig()
    doc, lay = R.apply_rig(doc, R.empty_layout(), "two radios", rig)
    doc = chain.save(st, doc)

    # Take a branch out: its chain stays in chain.json as a chain of its own.
    lone = json.loads(json.dumps(rig))
    for nid in ("n5", "n6", "n7"):          # coax, radio B, its PC
        R.remove_node(lone, nid)
    doc, lay = R.apply_rig(doc, lay, "two radios", lone)
    doc = chain.save(st, doc)
    assert "two radios (radio B)" in doc["chains"]
    assert "two radios" in doc["chains"]               # one path now: the rig's own name

    # Rename the straight 'plain' chain while it is current: current follows.
    doc = chain.use(st, "plain")
    doc, lay = R.apply_rig(doc, lay, "renamed", R.rig_from_parts(doc["chains"]["plain"]["parts"]),
                           renamed_from="plain")
    doc = chain.save(st, doc)
    assert "plain" not in doc["chains"] and doc["current"] == "renamed"


def test_use_for_runs_records_exactly_what_the_old_app_would(tmp_path):
    st = _station(tmp_path)
    doc, lay = R.apply_rig({"chains": {}}, R.empty_layout(), "two radios", _split_rig())
    chain.save(st, doc)
    chain.use(st, "two radios (radio B)")               # the same route the old app calls
    st2 = Station.load(tmp_path)
    assert st2.data["defaults"]["chain_order"] == R.rig_texts("two radios", _split_rig())["two radios (radio B)"]


def test_rig_state_keeps_todays_meanings():
    a = {"static": True, "last_used": "2026-01-02T03:04:05", "parts": []}
    b = {"static": False, "last_used": None, "parts": []}
    assert R.rig_state({"a": a}) == {"static": True, "last_used": "2026-01-02T03:04:05", "needs_confirm": False}
    s = R.rig_state({"a": a, "b": dict(b, last_used="2026-01-03T00:00:00")})
    assert s["static"] is False and s["needs_confirm"] is True and s["last_used"] == "2026-01-03T00:00:00"


def test_layout_check_refuses_a_broken_document():
    with pytest.raises(ValueError):
        R.check_layout({"rigs": {"x": {"nodes": [{"id": "a", "part": {"type": "warp drive"}}], "wires": []}}})
    with pytest.raises(ValueError):
        R.check_layout({"rigs": {"x": {"nodes": [], "wires": [{"from": "a", "to": "b"}]}}})


def test_the_service_saves_both_files_and_the_old_route_still_reads_them(tmp_path):
    from marsad import live
    st = _station(tmp_path)
    doc, lay = R.apply_rig({"chains": {"plain": {"parts": STRAIGHT["plain"]}}, "current": "plain"},
                           R.empty_layout(), "two radios", _split_rig())
    out = live.chain_layout_save(st, {"chains": doc, "layout": lay})
    assert set(out["layout"]["rigs"]) == {"two radios"}
    assert (tmp_path / "chain_layout.json").exists()
    old = chain.summary(Station.load(tmp_path))         # what GET /chain answers the web page
    assert old["current"] == "plain"
    texts = R.rig_texts("two radios", _split_rig())
    for name, text in texts.items():
        assert old["chains"][name]["text"] == text
    shown = R.rigs_for_display(out["chain"], R.check_layout(out["layout"]), out["chain"]["parts"])
    assert not shown["two radios"]["stale_drawing"]


def test_the_service_refuses_a_bad_save_and_writes_nothing(tmp_path):
    from marsad import live
    st = _station(tmp_path)
    with pytest.raises(ValueError):
        live.chain_layout_save(st, {"chains": {"chains": {"x": {"parts": [{"type": "warp drive"}]}}},
                                    "layout": R.empty_layout()})
    assert not (tmp_path / "chain_layout.json").exists()
    assert not (tmp_path / "chain.json").exists()
    with pytest.raises(ValueError):
        live.chain_layout_save(st, {"chains": {"chains": {}}, "layout": []})


def test_an_old_chain_never_drawn_saves_under_its_own_name(tmp_path):
    st = _station(tmp_path)
    doc = chain.save(st, {"chains": {"plain": {"parts": STRAIGHT["plain"]}}, "current": "plain"})
    doc, lay = R.apply_rig(doc, R.empty_layout(), "plain", R.rig_from_parts(doc["chains"]["plain"]["parts"]))
    assert chain.chain_text(chain.save(st, doc)["chains"]["plain"]["parts"]) == chain.chain_text(
        chain.validate({"chains": {"plain": {"parts": STRAIGHT["plain"]}}})["chains"]["plain"]["parts"])
    with pytest.raises(ValueError):          # a new rig still may not take an existing chain's name
        R.apply_rig({"chains": {"x (radio A)": {"parts": []}}}, R.empty_layout(), "x", _split_rig())


# ---------------------------------------------------------------- cables on wires
def _p(t, name, **kw):
    return dict({"type": t, "name": name, "detail": ""}, **kw)


def test_old_chain_puts_its_cables_on_the_wires_and_keeps_its_text():
    parts = [_p("antenna", "whip"), _p("coax", "Coax", length="3 ft"), _p("lna", "LNA"),
             _p("jumper", "jumper"), _p("sdr", "radio-a", rx="radio-a"),
             _p("usb", "1 ft M:M"), _p("usb", "16 ft"), _p("pc", "PC")]
    rig = R.rig_from_parts(parts)
    assert [n["part"]["type"] for n in rig["nodes"]] == ["antenna", "lna", "sdr", "pc"]
    assert [len(w.get("cables", [])) for w in rig["wires"]] == [1, 1, 2]
    assert R.rig_texts("r", rig) == {"r": chain.chain_text(chain.validate(
        {"chains": {"r": {"parts": parts}}})["chains"]["r"]["parts"])}


def test_a_cable_with_nothing_after_it_stays_a_part():
    rig = R.rig_from_parts([_p("antenna", "whip"), _p("sdr", "radio-a"), _p("usb", "USB cable")])
    assert [n["part"]["type"] for n in rig["nodes"]] == ["antenna", "sdr", "usb"]


def test_a_cable_added_to_a_wire_is_written_between_its_parts():
    rig = R.rig_from_parts([_p("antenna", "whip"), _p("sdr", "radio-a")])
    R.wire_of(rig, "n1", 0)["cables"] = [dict(R.new_cable("coax"), detail="2 m", length="2 m")]
    assert R.rig_texts("r", rig)["r"] == "whip > Coax (2 m) > radio-a"


def test_layout_check_refuses_a_cable_of_unknown_type():
    rig = R.rig_from_parts([_p("antenna", "whip"), _p("sdr", "radio-a")])
    rig["wires"][0]["cables"] = [{"type": "lna", "name": "x"}]
    with pytest.raises(ValueError):
        R.check_layout({"rigs": {"r": dict(rig, chains={})}})


# ---------------------------------------------------------------- rule 4: the drawing controls lose nothing saved
def _saved_station(tmp_path):
    """A station whose chain.json holds a drawn split rig and two other chains."""
    from marsad import live
    st = _station(tmp_path)
    doc = {"chains": {n: {"parts": STRAIGHT[n]} for n in ("plain", "every-type")}, "current": "plain"}
    doc, lay = R.apply_rig(doc, R.empty_layout(), "two radios", _split_rig())
    live.chain_layout_save(st, {"chains": doc, "layout": lay})
    return st


def _resave(tmp_path, st, rig):
    """What the window's Save does: apply the drawing to chain.json as the service holds
    it, then save both through the service."""
    from marsad import live
    summ = chain.summary(Station.load(tmp_path))
    doc = {"schema": 1, "current": summ["current"],
           "chains": {n: {k: v for k, v in c.items() if k in ("static", "updated", "last_used", "parts")}
                      for n, c in summ["chains"].items()}}
    lay = R.read_layout(tmp_path)
    doc, lay = R.apply_rig(doc, lay, "two radios", rig)
    live.chain_layout_save(st, {"chains": doc, "layout": lay})
    return json.loads((tmp_path / "chain.json").read_text(encoding="utf-8"))["chains"]


def _texts(tmp_path):
    doc = json.loads((tmp_path / "chain.json").read_text(encoding="utf-8"))
    return {n: chain.chain_text(c["parts"]) for n, c in doc["chains"].items()}


@pytest.mark.parametrize("edit", ["clear grid, then one new part", "take out a part", "take out a branch",
                                  "clear grid (nothing to save)"])
def test_clear_grid_or_taking_parts_out_then_save_removes_no_saved_chain(tmp_path, edit):
    st = _saved_station(tmp_path)
    before = _texts(tmp_path)
    rig = _split_rig()
    ids = {n["part"]["name"]: n["id"] for n in rig["nodes"]}
    if edit.startswith("clear grid (nothing"):
        rig = R.new_rig()
        doc, _ = R.apply_rig({"chains": {n: {"parts": []} for n in before}}, R.read_layout(tmp_path),
                             "two radios", rig)
        assert set(doc["chains"]) == set(before)          # an empty drawing writes no chain away
        return                                            # (and the window refuses to Save it at all)
    if edit.startswith("clear grid"):
        rig = R.new_rig()
        R.new_node(rig, "antenna", 0, 0)
    elif edit == "take out a part":
        R.remove_node(rig, ids["SMA DC block"])
    else:
        for name in ("SMA DC block", "radio A"):
            R.remove_node(rig, ids[name])
        R.remove_node(rig, next(n["id"] for n in rig["nodes"] if n["x"] == 1080 and n["y"] == 80))
    after = _resave(tmp_path, st, rig)
    assert set(before) <= set(after), f"lost: {set(before) - set(after)}"
    now = _texts(tmp_path)
    for n in ("plain", "every-type"):                     # chains the rig never wrote are untouched
        assert now[n] == before[n]


# ---------------------------------------------------------------- enclosures: drawing only
def _boxed_rig():
    rig = _split_rig()
    e = R.new_enclosure(rig, 860, -140, 300, 320)         # round both radios and their PCs
    e.update(name="aluminium cage", note="bonded lid")
    return rig, e


def test_an_enclosure_writes_nothing_into_any_text():
    plain = _split_rig()
    rig, _ = _boxed_rig()
    assert R.rig_texts("two radios", rig) == R.rig_texts("two radios", plain)
    assert R.paths(rig) == R.paths(plain)
    assert R.problems(rig) == R.problems(plain)
    doc_a, lay_a = R.apply_rig({"chains": {}}, R.empty_layout(), "two radios", plain)
    doc_b, lay_b = R.apply_rig({"chains": {}}, R.empty_layout(), "two radios", rig)
    assert doc_a == doc_b                                 # chain.json is identical
    assert lay_b["rigs"]["two radios"]["enclosures"][0]["name"] == "aluminium cage"
    assert "enclosures" not in lay_a["rigs"]["two radios"]   # old layouts stay exactly as they were


def test_an_enclosure_survives_the_layout_check_and_display():
    rig, _ = _boxed_rig()
    doc, lay = R.apply_rig({"chains": {}}, R.empty_layout(), "two radios", rig)
    lay = R.check_layout(json.loads(json.dumps(lay)))
    shown = R.rigs_for_display(doc, lay)
    assert shown["two radios"]["rig"]["enclosures"][0]["note"] == "bonded lid"
    bad = json.loads(json.dumps(lay))
    bad["rigs"]["two radios"]["enclosures"][0]["w"] = 5
    with pytest.raises(ValueError, match="impossible size"):
        R.check_layout(bad)
    bad["rigs"]["two radios"]["enclosures"] = [{"name": "no place"}]
    with pytest.raises(ValueError, match="enclosure"):
        R.check_layout(bad)


def test_taking_an_enclosure_out_leaves_its_parts():
    rig, e = _boxed_rig()
    n = len(rig["nodes"])
    R.remove_enclosure(rig, e["id"])
    assert len(rig["nodes"]) == n and "enclosures" not in rig


# ---------------------------------------------------------------- relay 004: layering and groups, drawing only
def _every_drawing_action(rig):
    """Every layering, grouping and enclosure action the window offers, applied in turn.
    Yields a label after each, so a test can check the text at every step."""
    ids = [n["id"] for n in rig["nodes"]]
    R.new_enclosure(rig, -40, -40, name=R.ENC_PRESETS["aluminium"])
    yield "aluminium enclosure dropped"
    e2 = R.new_enclosure(rig, 200, 200, name=R.ENC_PRESETS["plastic"])
    yield "plastic enclosure dropped"
    for how in R.ARRANGE:
        R.restack(rig, ids[:2] + [e2["id"]], how)
        yield f"arranged: {how}"
    g = R.make_group(rig, ids[:3] if len(ids) >= 3 else ids + [e2["id"]])
    assert g is not None
    yield "grouped"
    R.make_group(rig, [g["members"][0], e2["id"]])
    yield "grouped again (merged)"
    R.restack(rig, [g["members"][0]], "back")
    yield "group member sent to back"
    import marsad_desktop.geometry as G
    pos, rects = G.tidy_all(rig)
    for n in rig["nodes"]:
        n["x"], n["y"] = pos[n["id"]]
    for e in R.enclosures(rig):
        if e["id"] in rects:
            e["x"], e["y"], e["w"], e["h"] = rects[e["id"]]
    yield "tidied"
    R.ungroup(rig, ids)
    yield "ungrouped"
    R.remove_enclosure(rig, e2["id"])
    yield "enclosure taken out"
    yield from _relay_005_actions(rig)


def _relay_005_actions(rig):
    """Relay 005's drawing actions: power, figures, lock, nudge, align, distribute, guides'
    moves, copy/paste, cut and paste back. Each leaves the recorded text alone."""
    import marsad_desktop.geometry as G
    ids = [n["id"] for n in rig["nodes"]]
    p = R.new_power(rig, "usbc", -200, -120)
    yield "power source dropped"
    rig["nodes"][0]["needs_power"] = True                  # an "active antenna", say
    assert R.connect_power(rig, p["id"], ids[0]) is None
    yield "power wire joined"
    b = R.new_power(rig, "biastee", 400, -120)
    for n in rig["nodes"]:
        if R.needs_power(n) and not R.power_feed(rig, n["id"]):
            R.connect_power(rig, b["id"], n["id"])
    yield "every part that needs power fed"
    for k, n in enumerate(rig["nodes"]):
        n["gain_db"] = float(k) - 2.5
        for w in rig["wires"]:
            for c in w.get("cables") or []:
                c["gain_db"] = -0.8
    yield "gain and loss figures typed"
    R.set_locked(rig, ids[:1], True)
    yield "locked"
    G.shift(rig, ids, R.GRID, 0)
    G.shift(rig, ids, 0, -5 * R.GRID)
    yield "nudged"
    G.place(rig, G.align(rig, ids, "top"))
    yield "aligned top"
    G.place(rig, G.distribute(rig, ids, "across"))
    yield "distributed across"
    R.set_locked(rig, ids[:1], False)
    yield "unlocked"
    made = R.paste(rig, R.fragment(rig, ids + [p["id"]]), 40, 40)
    for bid in made:
        R.remove_box(rig, bid)
    yield "copied, pasted and the copy taken out again"
    frag = R.fragment(rig, ids)
    for bid in ids:
        R.remove_box(rig, bid)
    R.paste(rig, frag, 0, 0)
    yield "cut and pasted back in place"
    R.remove_power(rig, p["id"])
    yield "power source taken out"
    yield from _relay_006_actions(rig)


def _relay_006_actions(rig):
    """Relay 006's drawing-only actions: a colour of its own on every box and back, a gender on
    every SMA part that is not an adapter, every cable-less wire made a direct mating and back,
    Tidy and a nudge with matings in place. None may move a recorded text. (An adapter's chosen
    kind does change its text, by the owner's decision: tested on its own below.)"""
    import marsad_desktop.geometry as G
    for b in R._boxes(rig):
        b["colour"] = "#1F3A5F"
    yield "every box given a colour of its own"
    for n in rig["nodes"]:
        if n["part"]["type"] in R.SMA_TYPES and n["part"]["type"] != "adapter":
            R.set_sma(n, gender="MF")
    yield "genders on DC blocks, pads and bulkheads"
    plain = [w for w in rig["wires"] if not w.get("cables")]
    for w in plain:
        w["mated"] = True
    yield "every cable-less wire made a direct mating"
    R.check_layout(json.loads(json.dumps({"rigs": {"r": rig}})))
    pos, rects = G.tidy_all(rig)
    for n in rig["nodes"]:
        n["x"], n["y"] = pos[n["id"]]
    yield "tidied with matings"
    G.shift(rig, [n["id"] for n in rig["nodes"][:1]], R.GRID, R.GRID)
    yield "a mated piece nudged"
    for w in plain:
        w.pop("mated", None)
    for b in R._boxes(rig):
        b.pop("colour", None)
    yield "matings and colours taken off again"
    yield from _relay_007_actions(rig)


def _relay_007_actions(rig):
    """Relay 007's drawing-only actions: a shape on every box, antenna settings on every antenna,
    every box resized, a saved version kept in history, archiving. No recorded text moves."""
    import marsad_desktop.geometry as G
    for k, b in enumerate(R._boxes(rig)):
        if R.kind_of(rig, b["id"]) != "enclosure":
            b["shape"] = list(R.SHAPES)[k % len(R.SHAPES)]
    yield "every box drawn in another shape"
    for n in rig["nodes"]:
        if n["part"]["type"] == "antenna":
            n["antenna"] = {"type": "yagi", "az": 135.0, "el": 30.0, "elements": 5.0, "gain_dbi": 10.0,
                            "pol": "horizontal", "height": "6 m", "boom": "1.2 m"}
    yield "antenna settings: a Yagi at az 135, el 30"
    for b in R._boxes(rig):
        if R.kind_of(rig, b["id"]) != "enclosure":
            x, y, w, h = G.resize((b["x"], b["y"], b.get("w") or 140, b.get("h") or 64), "br", 40, 40, snap=R.GRID)
            b["w"], b["h"] = w, h
    yield "every box resized from a corner"
    doc, lay = R.apply_rig({"chains": {}}, R.empty_layout(), "r", rig)
    doc, lay = R.apply_rig(doc, lay, "r", rig)
    assert len(lay["rigs"]["r"]["history"]) == 1
    lay2 = R.set_archived(doc, lay, "r", rig, True)
    assert lay2["rigs"]["r"]["archived"] is True and R.check_layout(json.loads(json.dumps(lay2)))
    yield "saved twice (one version kept) and archived"
    for b in R._boxes(rig):
        b.pop("shape", None)
    yield "shapes back to their kinds'"


@pytest.mark.parametrize("name", list(STRAIGHT) + ["split rig"])
def test_no_layering_grouping_or_enclosure_action_changes_any_recorded_text(name):
    rig = _split_rig() if name == "split rig" else R.rig_from_parts(STRAIGHT[name])
    want = R.rig_texts(name, rig)
    doc_want, _ = R.apply_rig({"chains": {}}, R.empty_layout(), name, rig)
    for step in _every_drawing_action(rig):
        assert R.rig_texts(name, rig) == want, step
        assert R.problems(rig) == [] or name == "one-part", step
        doc, lay = R.apply_rig({"chains": {}}, R.empty_layout(), name, rig)
        assert doc == doc_want, step                       # chain.json byte for byte the same
        R.check_layout(json.loads(json.dumps(lay)))        # and the layout stays valid


def test_a_rig_never_layered_stores_exactly_what_it_did():
    rig = _split_rig()
    R.new_enclosure(rig, 0, 0)
    R.restack(rig, [rig["nodes"][-1]["id"]], "front")        # already at the front: nothing written
    _, lay = R.apply_rig({"chains": {}}, R.empty_layout(), "r", rig)
    r = lay["rigs"]["r"]
    assert "groups" not in r and not any("z" in b for b in r["nodes"] + r["enclosures"])


def test_stacking_moves_as_a_drawing_program_does():
    rig = R.new_rig()
    a, b, c, d = (R.new_node(rig, "custom", 0, 0)["id"] for _ in range(4))
    e = R.new_enclosure(rig, 0, 0)["id"]
    assert R.stack(rig) == [e, a, b, c, d]                    # enclosures start behind
    assert R.restack(rig, [a], "front") and R.stack(rig) == [e, b, c, d, a]
    assert R.restack(rig, [a], "backward") and R.stack(rig) == [e, b, c, a, d]
    assert R.restack(rig, [b, a], "forward") and R.stack(rig) == [e, c, b, d, a]
    assert R.restack(rig, [e], "front") and R.stack(rig) == [c, b, d, a, e]
    assert R.restack(rig, [a, e], "back") and R.stack(rig) == [a, e, c, b, d]
    assert not R.restack(rig, [a], "back")
    new = R.new_node(rig, "pc", 0, 0)["id"]                   # once layered: a new part on top,
    box = R.new_enclosure(rig, 0, 0)["id"]                    # a new enclosure behind everything
    assert R.stack(rig)[-1] == new and R.stack(rig)[0] == box


def test_groups_merge_dissolve_and_follow_what_is_taken_out():
    rig = R.new_rig()
    a, b, c, d = (R.new_node(rig, "custom", 0, 0)["id"] for _ in range(4))
    e = R.new_enclosure(rig, 0, 0)["id"]
    assert R.make_group(rig, [a]) is None and "groups" not in rig
    g1 = R.make_group(rig, [a, b])
    g2 = R.make_group(rig, [c, e])
    g3 = R.make_group(rig, [b, c])                            # a box is never in two groups
    assert [g["id"] for g in R.groups(rig)] == [g3["id"]] and set(g3["members"]) == {a, b, c, e}
    assert R.group_of(rig, e) is g3 and R.group_of(rig, d) is None and g1 and g2
    R.remove_node(rig, a)
    R.remove_enclosure(rig, e)
    assert R.groups(rig)[0]["members"] == [b, c]
    R.remove_node(rig, b)
    assert "groups" not in rig                                # one member left: no group
    R.make_group(rig, [c, d])
    assert R.ungroup(rig, [d]) == 1 and "groups" not in rig


def test_layout_check_carries_groups_and_stacking_and_refuses_bad_ones():
    rig = _split_rig()
    ids = [n["id"] for n in rig["nodes"]]
    R.restack(rig, ids[:1], "front")
    R.make_group(rig, ids[:2])
    doc, lay = R.apply_rig({"chains": {}}, R.empty_layout(), "two radios", rig)
    shown = R.rigs_for_display(doc, R.check_layout(json.loads(json.dumps(lay))))["two radios"]["rig"]
    assert shown["groups"] == rig["groups"] and R.stack(shown) == R.stack(rig)
    for breakage, why in ((lambda r: r["groups"][0]["members"].append("nowhere"), "not there"),
                          (lambda r: r["groups"].append({"id": "g9", "members": ids[:2]}), "two groups"),
                          (lambda r: r["nodes"][0].update(z="top"), "not a number")):
        bad = json.loads(json.dumps(lay))
        breakage(bad["rigs"]["two radios"])
        with pytest.raises(ValueError, match=why):
            R.check_layout(bad)


# ---------------------------------------------------------------- relay 005: power, checks, clipboard, library, estimate
def test_power_wires_never_reach_the_recorded_text():
    plain = _split_rig()
    rig = _split_rig()
    lna = next(n for n in rig["nodes"] if n["part"]["type"] == "lna")
    p = R.new_power(rig, "usbc", 360, -140)
    assert R.connect_power(rig, p["id"], lna["id"]) is None
    assert R.rig_texts("two radios", rig) == R.rig_texts("two radios", plain)
    assert R.paths(rig) == R.paths(plain)                 # the path walk never follows a power wire
    doc_a, _ = R.apply_rig({"chains": {}}, R.empty_layout(), "two radios", plain)
    doc_b, lay = R.apply_rig({"chains": {}}, R.empty_layout(), "two radios", rig)
    assert doc_a == doc_b
    shown = R.rigs_for_display(doc_b, R.check_layout(json.loads(json.dumps(lay))))["two radios"]["rig"]
    assert shown["power_wires"] == [{"from": p["id"], "to": lna["id"]}]


def test_an_lna_with_no_power_feed_is_flagged_and_a_power_wire_clears_it():
    rig = _split_rig()
    lna = next(n for n in rig["nodes"] if n["part"]["type"] == "lna")
    texts = lambda: [c["text"] for c in R.checks(rig) if c["kind"] == "power"]
    assert texts() == ["LNA has no power feed"]
    p = R.new_power(rig, "usbc", 360, -140)
    assert R.connect_power(rig, p["id"], lna["id"]) is None
    assert texts() == []
    assert "already has a power feed" in R.connect_power(rig, p["id"], lna["id"])
    radio = next(n for n in rig["nodes"] if n["part"]["type"] == "sdr")
    assert "does not need power" in R.connect_power(rig, p["id"], radio["id"])
    lna["needs_power"] = False                             # a passive part the owner unticked
    R.disconnect_power(rig, lna["id"])
    assert texts() == [] and "power_wires" not in rig
    R.remove_node(rig, lna["id"])                          # taking a part out drops its feed too
    assert R.power_wires(rig) == []


def test_checks_name_each_problem_and_the_parts_it_is_about():
    rig = R.new_rig()
    a = R.new_node(rig, "antenna", 0, 0)
    s = R.new_node(rig, R.SPLITTER, 200, 0)
    r1 = R.new_node(rig, "sdr", 400, -80)
    r1["part"].update(name="radio A", rx="rx1")
    r2 = R.new_node(rig, "sdr", 400, 80)
    r2["part"].update(name="radio A too", rx="rx1")
    c = R.new_node(rig, "coax", 200, 300)
    R.connect(rig, a["id"], 0, s["id"])
    R.connect(rig, s["id"], 0, r1["id"])
    R.new_node(rig, "pc", 600, 300)                        # off on its own
    r2_feed = R.new_node(rig, "adapter", 200, 200)
    R.connect(rig, r2_feed["id"], 0, r2["id"])
    got = {(x["kind"], x["text"]): x["ids"] for x in R.checks(rig)}
    assert got[("radio", "No antenna, probe or load reaches radio A too: its chain starts at SMA adapter")] == \
        [r2_feed["id"], r2["id"]]
    assert ("wiring", "Splitter has 1 output with nothing on it") in got
    assert got[("radio", "2 radios in this rig are all rx1")] == [r1["id"], r2["id"]]
    assert ("loose", "PC is on its own, connected to nothing") in got
    assert ("loose", "Coax is on its own, connected to nothing") in got
    R.new_power(rig, "battery", 0, 400)
    assert any(x["text"] == "Battery powers nothing" for x in R.checks(rig))
    assert R.checks(R.new_rig()) == []


def test_copy_and_paste_bring_the_wires_whose_two_ends_were_copied():
    rig = _split_rig()
    branch = [n["id"] for n in rig["nodes"] if n["y"] == -80]          # coax, radio B, its PC
    made = R.paste(rig, R.fragment(rig, branch), 0, 300)
    new = {n["id"]: n for n in rig["nodes"] if n["id"] in made}
    assert len(made) == 3 and {n["y"] for n in new.values()} == {220}
    inner = [w for w in rig["wires"] if w["from"] in made or w["to"] in made]
    assert len(inner) == 2 and all(w["from"] in made and w["to"] in made for w in inner)
    old_branch = [p for p in R.paths(_split_rig()) if p[-1][0] == branch[-1]][0]
    pasted = [p for p in R.paths(rig) if p[0][0] in made][0]
    # The pasted branch has no source yet, but its parts are the copied parts, word for word.
    assert R.path_parts(rig, pasted) == R.path_parts(_split_rig(), old_branch)[-3:]
    assert "groups" not in rig


def test_paste_gives_fresh_ids_keeps_groups_and_power_and_stacks_on_top():
    rig = _split_rig()
    ids = [n["id"] for n in rig["nodes"]][:3]
    p = R.new_power(rig, "usbc", 0, -200)
    R.connect_power(rig, p["id"], ids[2])                    # the LNA
    R.make_group(rig, ids[:2])
    R.restack(rig, [ids[0]], "front")
    made = R.paste(rig, R.fragment(rig, ids + [p["id"]]), 20, 20)
    assert not set(made) & set(ids + [p["id"]])
    assert len(R.groups(rig)) == 2 and R.power_wires(rig)[-1]["to"] in made
    assert R.stack(rig)[-len(made):] == sorted(made, key=lambda b: R.stack(rig).index(b))
    R.check_layout({"rigs": {"r": dict(rig, chains={})}})


def test_lock_keeps_a_box_still_through_nudge_align_and_tidy():
    import marsad_desktop.geometry as G
    rig = _split_rig()
    a, n = rig["nodes"][0], rig["nodes"][1]
    R.set_locked(rig, [a["id"]], True)
    before = (a["x"], a["y"])
    assert G.shift(rig, [a["id"], n["id"]], 20, 0)
    assert (a["x"], a["y"]) == before and n["x"] == 200
    G.place(rig, G.align(rig, [a["id"], n["id"]], "bottom"))
    assert (a["x"], a["y"]) == before
    pos, _ = G.tidy_all(rig)
    assert pos[a["id"]] == before
    assert R.set_locked(rig, [a["id"]], False) and "locked" not in a


def test_an_enclosure_carries_its_parts_when_nudged_but_not_a_locked_one():
    import marsad_desktop.geometry as G
    rig = _split_rig()
    radio = next(n for n in rig["nodes"] if n["part"].get("rx") == "rx2")
    e = R.new_enclosure(rig, radio["x"] - 20, radio["y"] - 40, 200, 140)
    G.shift(rig, [e["id"]], 0, 40)
    assert radio["y"] == -40
    R.set_locked(rig, [radio["id"]], True)
    G.shift(rig, [e["id"]], 0, 40)
    assert radio["y"] == -40 and e["y"] == -40              # the box moved; the locked radio did not


def test_align_and_distribute_as_a_drawing_program_does():
    import marsad_desktop.geometry as G
    rig = R.new_rig()
    ns = [R.new_node(rig, "custom", x, y) for x, y in ((0, 0), (100, 60), (500, 20))]
    ids = [n["id"] for n in ns]
    G.place(rig, G.align(rig, ids, "left"))
    assert {n["x"] for n in ns} == {0}
    G.place(rig, G.align(rig, ids, "middle"))
    assert len({n["y"] for n in ns}) == 1
    ns[1]["x"], ns[2]["x"] = 160, 900
    G.place(rig, G.distribute(rig, ids, "across"))
    w = R.PART_W
    gaps = [ns[1]["x"] - (ns[0]["x"] + w), ns[2]["x"] - (ns[1]["x"] + w)]
    assert abs(gaps[0] - gaps[1]) <= 1
    assert G.align(rig, ids[:1], "left") == {} and G.distribute(rig, ids[:2], "down") == {}


def test_smart_guides_snap_to_a_neighbours_edge_or_centre():
    import marsad_desktop.geometry as G
    dx, dy, lines = G.guide_snap((103, 400, 140, 64), [(100, 0, 140, 64)])
    assert (dx, dy) == (-3, 0) and lines[0][0] == "v" and lines[0][1] == 100
    dx, dy, lines = G.guide_snap((300, 33, 140, 64), [(0, 0, 140, 64)])    # centre to centre
    assert dy == -1 and lines[0] == ("h", 32, -12, 452)
    assert G.guide_snap((300, 300, 140, 64), [(0, 0, 140, 64)]) == (0, 0, [])


def test_tidy_moves_parts_aside_so_a_rigid_group_never_overlaps_them():
    import marsad_desktop.geometry as G
    rig = _split_rig()
    ids = [n["id"] for n in rig["nodes"]]
    # Group two parts that Tidy would put far apart, so the rigid group lands on top of others.
    far = [ids[0], ids[-1]]
    rig["nodes"][-1]["x"], rig["nodes"][-1]["y"] = 40, 40
    R.make_group(rig, far)
    pos, _ = G.tidy_all(rig)
    nodes = {n["id"]: n for n in rig["nodes"]}
    boxes = {i: (pos[i][0], pos[i][1], *G.size(nodes[i])) for i in ids}
    for i in ids:
        for j in ids:
            if i < j and not (i in far and j in far):
                assert not G._overlap(boxes[i], boxes[j]), (i, j)


def test_the_estimate_adds_the_figures_between_source_and_radio():
    rig = R.new_rig()
    a = R.new_node(rig, "antenna", 0, 0)
    lna = R.new_node(rig, "lna", 200, 0)
    lna["gain_db"] = 20
    s = R.new_node(rig, R.SPLITTER, 400, 0)
    s["gain_db"] = -3.5
    blank = R.new_node(rig, "adapter", 600, 0)
    r = R.new_node(rig, "sdr", 800, 0)
    for x, y in ((a, lna), (lna, s), (s, blank), (blank, r)):
        R.connect(rig, x["id"], 0, y["id"])
    path = R.paths(rig)[0]
    assert R.estimate_text(R.estimate(rig, path)) == "est. +16.5 dB (1 part unknown)"
    R.wire_of(rig, a["id"], 0)["cables"] = [dict(R.new_cable("coax"), gain_db=-1.2)]
    blank["gain_db"] = -0.1
    assert R.estimate_text(R.estimate(rig, R.paths(rig)[0])) == "est. +15.2 dB (every part has a figure)"
    pad = R.new_node(rig, "attenuator", 0, 200)
    pad["part"]["db"] = 10
    assert R.figure(pad, pad["part"]) is None               # relay 006: a pad with no typed figure is unknown
    pad["gain_db"] = -10
    assert R.figure(pad, pad["part"]) == -10.0              # its typed figure counts like any other part's
    assert R.estimate(rig, [(r["id"], None)]) is not None and R.estimate(rig, [(a["id"], None)]) is None
    assert R.estimate_text(R.estimate(R.rig_from_parts([P("antenna", "a"), P("lna", "LNA"), P("sdr", "r")]),
                                      [("n1", 0), ("n2", 0), ("n3", None)])) == "est. no figures yet (1 part unknown)"
    # The figures are drawing only: the text and chain.json do not change.
    assert R.rig_texts("r", rig) == R.rig_texts("r", _strip(rig, "gain_db"))


def _strip(rig, key):
    out = json.loads(json.dumps(rig))
    for n in out["nodes"]:
        n.pop(key, None)
    for w in out["wires"]:
        for c in w.get("cables") or []:
            c.pop(key, None)
    return out


def test_a_library_part_drops_with_the_same_name_and_note_every_time():
    rig = _split_rig()
    lna = next(n for n in rig["nodes"] if n["part"]["type"] == "lna")
    lna["part"].update(name="Bench LNA", detail="window end")
    lna.update(w=180, h=80, gain_db=18.0)
    entry = R.lib_entry(lna, [])
    lay = R.check_layout({"rigs": {}, "library": [entry]})
    assert R.library(lay)[0]["part"] == {"type": "lna", "name": "Bench LNA", "detail": "window end"}
    first = R.node_from_lib(R.new_rig(), R.library(lay)[0], 0, 0)
    again = R.node_from_lib(R.new_rig(), R.library(lay)[0], 0, 0)
    assert again["part"] == first["part"] and (again["w"], again["h"], again["gain_db"]) == (180, 80, 18.0)
    assert R.rig_texts("a", R.rig_from_parts([first["part"]])) == R.rig_texts("a", R.rig_from_parts([again["part"]]))
    R.library(lay)[0]["part"]["name"] = "renamed later"       # editing the entry afterwards
    assert first["part"]["name"] == "Bench LNA"                   # leaves a part already dropped alone
    assert R.node_from_lib(R.new_rig(), R.library(lay)[0], 0, 0)["part"]["name"] == "renamed later"
    radio = R.lib_entry(next(n for n in rig["nodes"] if n["part"]["type"] == "sdr"), ["L1"])
    assert radio["id"] == "L2" and "rx" not in radio["part"]        # a receiver is matched afresh


def test_the_library_survives_saving_a_rig():
    lay = R.check_layout({"rigs": {}, "library": [{"id": "L1", "part": {"type": "coax", "name": "3 ft"}}]})
    _, lay2 = R.apply_rig({"chains": {}}, lay, "r", _split_rig())
    assert R.library(lay2) == R.library(lay)
    with pytest.raises(ValueError, match="library"):
        R.check_layout({"rigs": {}, "library": [{"id": "L1", "part": {"type": "warp drive"}}]})


def test_layout_check_refuses_broken_power():
    rig = _split_rig()
    p = R.new_power(rig, "battery", 0, 0)
    R.connect_power(rig, p["id"], "n3")
    good = {"rigs": {"r": dict(rig, chains={})}}
    R.check_layout(json.loads(json.dumps(good)))
    for breakage, why in ((lambda r: r["power_wires"].append({"from": "p1", "to": "n99"}), "not there"),
                          (lambda r: r["power_wires"].append({"from": "p1", "to": "n3"}), "two power feeds"),
                          (lambda r: r["power"][0].update(kind="nuclear"), "known kind"),
                          (lambda r: r["nodes"][0].update(gain_db="lots"), "not a number"),
                          (lambda r: r["nodes"][0].update(locked="yes"), "yes or no")):
        bad = json.loads(json.dumps(good))
        breakage(bad["rigs"]["r"])
        with pytest.raises(ValueError, match=why):
            R.check_layout(bad)


# ---------------------------------------------------------------- relay 006
def _adapter_rig(shape=None, gender=None, note=""):
    rig = R.new_rig()
    a = R.new_node(rig, "antenna", 0, 0)
    ad = R.new_node(rig, "adapter", 180, 0)
    ad["part"]["detail"] = note
    lna = R.new_node(rig, "lna", 320, 0)
    r = R.new_node(rig, "sdr", 600, 0)
    r["part"].update(name="radio A", rx="rx1")
    R.connect(rig, a["id"], 0, ad["id"])
    assert R.mate(rig, ad["id"], 0, lna["id"]) is None
    R.connect(rig, lna["id"], 0, r["id"])
    if shape or gender:
        R.set_sma(ad, shape=shape, gender=gender)
    return rig, ad, lna


def test_an_adapter_with_a_kind_writes_it_as_its_detail():
    rig, ad, _ = _adapter_rig("90", "MF")
    assert R.rig_texts("r", rig) == {"r": "Antenna > SMA adapter (90° M:F) > LNA > radio A"}
    for shape, gender, want in (("straight", None, "straight"), ("T", "FF", "T F:F"), ("180", "FM", "180° F:M"),
                                (None, "MM", "M:M")):
        R.set_sma(ad, shape=shape, gender=gender)
        if shape is None:
            ad["sma"].pop("shape", None)
        assert R.adapter_kind_text(ad) == want
    rig, ad, _ = _adapter_rig("90", "MF", note="gold")
    assert "SMA adapter (90° M:F, gold)" in R.rig_texts("r", rig)["r"]


def test_an_adapter_with_no_kind_writes_exactly_what_it_did():
    rig, ad, _ = _adapter_rig()
    plain = R.rig_texts("r", rig)
    assert plain == {"r": "Antenna > SMA adapter > LNA > radio A"}
    R.set_sma(ad, turn=2)                              # a turn alone is drawing only
    R.set_sma(ad, shape=None, gender=None)
    assert R.rig_texts("r", rig) == plain
    for name, parts in STRAIGHT.items():                # every made-up straight chain keeps its text
        r = R.rig_from_parts(parts)
        assert R.rig_texts(name, r) == {name: chain.chain_text(parts)}


def test_a_mating_is_exactly_a_wire_with_no_cable_in_the_path():
    rig, ad, lna = _adapter_rig("90", "MF")
    mated = R.rig_texts("r", rig)
    w = R.wire_of(rig, ad["id"], 0)
    w.pop("mated")
    assert R.rig_texts("r", rig) == mated
    doc_m, _ = R.apply_rig({"chains": {}}, R.empty_layout(), "r", _adapter_rig("90", "MF")[0])
    doc_w, _ = R.apply_rig({"chains": {}}, R.empty_layout(), "r", rig)
    assert doc_m == doc_w


def test_two_males_or_two_females_mated_warn_in_words():
    rig, ad, lna = _adapter_rig("straight", "MM")
    assert not [c for c in R.checks(rig) if "screw" in c["text"]]       # the LNA's gender is not known
    blk = R.new_node(rig, "dcblock", 0, 200)
    R.set_sma(blk, gender="MM")
    ad2 = R.new_node(rig, "adapter", 140, 200)
    R.set_sma(ad2, gender="MF")
    assert R.mate(rig, blk["id"], 0, ad2["id"]) is None
    msgs = [c["text"] for c in R.checks(rig) if "screw" in c["text"]]
    assert msgs == ["M to M at SMA adapter: these won't screw together"]
    R.set_sma(ad2, gender="FM")
    assert not [c for c in R.checks(rig) if "screw" in c["text"]]


def test_shaped_adapters_put_their_points_on_fixed_sides_and_turn():
    import marsad_desktop.geometry as G
    rig = R.new_rig()
    ad = R.new_node(rig, "adapter", 0, 0)
    assert R.port_sides(ad) is None
    for shape, want in (("straight", ("left", ["right"])), ("90", ("left", ["bottom"])),
                        ("T", ("left", ["right", "bottom"])), ("180", ("left", ["left"]))):
        R.set_sma(ad, shape=shape)
        assert (R.port_sides(ad)["in"], R.port_sides(ad)["out"]) == want
    R.set_sma(ad, shape="90", turn=1)
    assert R.port_sides(ad) == {"in": "top", "out": ["left"]}
    R.set_sma(ad, shape="T", turn=0)
    assert R.out_ports(ad) == 2
    am = G.anchors(rig)
    assert {am[(ad["id"], "out", 0)][2], am[(ad["id"], "out", 1)][2], am[(ad["id"], "in", 0)][2]} == \
        {"right", "bottom", "left"}


def test_a_t_adapter_branches_like_a_splitter_and_warns_of_an_open_port():
    rig, ad, lna = _adapter_rig("T", "FF")
    assert [c["text"] for c in R.checks(rig) if "output" in c["text"]] == ["SMA adapter has 1 output with nothing on it"]
    r2 = R.new_node(rig, "sdr", 600, 200)
    r2["part"].update(name="radio B", rx="rx2")
    assert R.connect(rig, ad["id"], 1, r2["id"]) is None
    assert sorted(R.rig_texts("r", rig).values()) == ["Antenna > SMA adapter (T F:F) > LNA > radio A",
                                                       "Antenna > SMA adapter (T F:F) > radio B"]


def test_mated_parts_move_as_one_piece_and_a_locked_member_holds_it():
    import marsad_desktop.geometry as G
    rig, ad, lna = _adapter_rig()
    assert R.assembly(rig, ad["id"]) == {ad["id"], lna["id"]}
    assert G.moving_with(rig, [ad["id"]]) == {ad["id"], lna["id"]}
    x0 = lna["x"]
    G.shift(rig, [ad["id"]], R.GRID, 0)
    assert lna["x"] == x0 + R.GRID
    R.set_locked(rig, [lna["id"]], True)
    assert G.moving_with(rig, [ad["id"]]) == set()
    assert G.shift(rig, [ad["id"]], R.GRID, 0) is False
    R.set_locked(rig, [lna["id"]], False)
    pos, _ = G.tidy_all(rig)
    assert (pos[lna["id"]][0] - pos[ad["id"]][0], pos[lna["id"]][1] - pos[ad["id"]][1]) == \
        (lna["x"] - ad["x"], lna["y"] - ad["y"])          # Tidy keeps a mated piece in one piece


def test_colour_and_sma_kind_survive_the_layout_check_and_the_library():
    rig, ad, lna = _adapter_rig("90", "MF")
    lna["colour"] = "#1F3A5F"
    R.new_enclosure(rig, 0, 300)["colour"] = "#FF9F43"
    R.new_power(rig, "usbc", 0, 600)["colour"] = "#7BD88F"
    lay = R.check_layout(json.loads(json.dumps({"rigs": {"r": rig}})))
    got = lay["rigs"]["r"]
    assert got["nodes"][2]["colour"] == "#1F3A5F" and got["nodes"][1]["sma"] == {"shape": "90", "gender": "MF"}
    assert R.wire_of(got, ad["id"], 0)["mated"] is True
    e = R.lib_entry(ad, [])
    assert e["sma"] == {"shape": "90", "gender": "MF"}
    back = R.node_from_lib(R.new_rig(), e, 0, 0)
    assert R.adapter_kind_text(back) == "90° M:F"
    for bad in ({"colour": "blue"}, {"sma": {"shape": "45"}}, {"sma": {"gender": "XY"}}, {"sma": {"turn": 5}}):
        r2 = json.loads(json.dumps(rig))
        r2["nodes"][1].update(bad)
        with pytest.raises(ValueError):
            R.check_layout({"rigs": {"r": r2}})
    r3 = json.loads(json.dumps(rig))
    R.wire_of(r3, ad["id"], 0)["cables"] = [R.new_cable("coax")]
    with pytest.raises(ValueError):
        R.check_layout({"rigs": {"r": r3}})            # a mating carries no cable


def test_colours_never_reach_the_recorded_text():
    rig = _split_rig()
    want = R.rig_texts("s", rig)
    for b in R._boxes(rig):
        b["colour"] = "#FF5C5C"
    assert R.rig_texts("s", rig) == want


# ---------------------------------------------------------------- relay 007
def test_antenna_settings_never_reach_the_recorded_text():
    rig = R.rig_from_parts(STRAIGHT["plain"])
    want = R.rig_texts("p", rig)
    a = rig["nodes"][0]
    a["antenna"] = {"type": "dish", "diameter": "1.2 m", "feed": "cantenna", "az": 180.0, "el": 45.0, "gain_dbi": 21.0}
    assert R.rig_texts("p", rig) == want
    assert R.shape_of(a) == "rect"                                  # relay 008: every kind starts as a rectangle
    assert ("Pointing azimuth", "180\u00b0") in R.antenna_lines(a) and ("Nominal gain", "21 dBi") in R.antenna_lines(a)
    e = R.estimate(rig, R.paths(rig)[0])
    assert e["total"] == 0.0 and e["known"] == 0                     # the antenna's 21 dBi is not added
    assert R.path_report(rig, R.paths(rig)[0])["antenna_gain"] == 21.0                      # but it is reported
    lay = R.check_layout(json.loads(json.dumps({"rigs": {"p": rig}})))
    assert lay["rigs"]["p"]["nodes"][0]["antenna"]["type"] == "dish"
    bad = json.loads(json.dumps(rig))
    bad["nodes"][0]["antenna"] = {"type": "laser"}
    with pytest.raises(ValueError):
        R.check_layout({"rigs": {"p": bad}})


def test_shapes_are_drawing_only_and_checked():
    rig = _split_rig()
    want = R.rig_texts("s", rig)
    for n in rig["nodes"]:
        n["shape"] = "hexagon"
    assert R.rig_texts("s", rig) == want
    assert R.shape_of(rig["nodes"][0]) == "hexagon"
    rig["nodes"][0].pop("shape")
    assert R.shape_of(rig["nodes"][0]) == "rect"
    bad = json.loads(json.dumps(rig))
    bad["nodes"][0]["shape"] = "star"
    with pytest.raises(ValueError):
        R.check_layout({"rigs": {"s": bad}})


def test_templates_give_wired_rigs():
    for key, want in (("blank", {}), ("one_radio", {"t": "Antenna > Coax > rx1 > PC"}),
                      ("lna_radio", {"t": "Antenna > LNA > Coax > rx1 > PC"}),
                      ("two_radios", {"t (rx1)": "Antenna > Splitter (out 1) > rx1",
                                      "t (rx2)": "Antenna > Splitter (out 2) > rx2"})):
        rig = R.template(key, None, ["rx1", "rx2"])
        assert (R.rig_texts("t", rig) if rig["nodes"] else {}) == want, key
    with pytest.raises(ValueError):
        R.template("spaceship")


def test_every_save_keeps_the_drawing_it_replaces_and_a_restore_loses_nothing():
    rig = _split_rig()
    doc, lay = R.apply_rig({"chains": {}}, R.empty_layout(), "s", rig)
    assert "history" not in lay["rigs"]["s"]
    first = json.loads(json.dumps(rig))
    rig["nodes"][0]["part"]["detail"] = "a longer whip"
    doc, lay = R.apply_rig(doc, lay, "s", rig)
    hist = lay["rigs"]["s"]["history"]
    assert len(hist) == 1 and hist[0]["rig"]["nodes"] == first["nodes"] and hist[0]["saved"]
    assert all("dipole" in t for t in hist[0]["chains"].values())
    # restoring = the old drawing saved again: the version it replaces is kept as well
    doc, lay = R.apply_rig(doc, lay, "s", hist[0]["rig"])
    assert len(lay["rigs"]["s"]["history"]) == 2
    assert lay["rigs"]["s"]["history"][1]["rig"]["nodes"][0]["part"]["detail"] == "a longer whip"
    assert R.rig_texts("s", hist[0]["rig"]) == R.rig_texts("s", first)
    shown = R.rigs_for_display(doc, R.check_layout(json.loads(json.dumps(lay))))
    assert len(shown["s"]["history"]) == 2 and not shown["s"]["archived"]
    assert {n: chain.chain_text(c["parts"]) for n, c in doc["chains"].items()} == R.rig_texts("s", first)


def test_archiving_changes_only_the_drawings_flag():
    rig = _split_rig()
    doc, lay = R.apply_rig({"chains": {}}, R.empty_layout(), "s", rig)
    before = json.dumps(doc, sort_keys=True)
    arch = R.set_archived(doc, lay, "s", rig, True)
    assert json.dumps(doc, sort_keys=True) == before                    # chain.json untouched
    assert arch["rigs"]["s"]["archived"] is True
    assert {k: v for k, v in arch["rigs"]["s"].items() if k != "archived"} == lay["rigs"]["s"]
    shown = R.rigs_for_display(doc, R.check_layout(json.loads(json.dumps(arch))))
    assert shown["s"]["archived"] and set(shown["s"]["chains"]) == set(doc["chains"])
    back = R.set_archived(doc, arch, "s", rig, False)
    assert back["rigs"]["s"] == lay["rigs"]["s"]
    # a chain never drawn: its straight row is written to hold the flag; chain.json still untouched
    parts = chain.validate({"chains": {"old": {"parts": STRAIGHT["plain"]}}})["chains"]["old"]["parts"]
    straight = {"chains": {"old": {"parts": parts}}}
    rig2 = R.rig_from_parts(parts)
    lay3 = R.set_archived(straight, R.empty_layout(), "old", rig2, True)
    assert lay3["rigs"]["old"]["archived"] and lay3["rigs"]["old"]["chains"] == {"old": chain.chain_text(parts)}
    with pytest.raises(ValueError):                                     # unsaved: nothing to archive
        R.set_archived({"chains": {}}, R.empty_layout(), "new", rig2, True)


def test_path_report_adds_up_cables_joins_genders_and_power():
    rig = R.new_rig()
    a = R.new_node(rig, "antenna", 0, 0)
    ad = R.new_node(rig, "adapter", 200, 0)
    R.set_sma(ad, gender="MF")
    blk = R.new_node(rig, "dcblock", 400, 0)
    R.set_sma(blk, gender="MF")
    lna = R.new_node(rig, "lna", 600, 0)
    r = R.new_node(rig, "sdr", 800, 0)
    R.connect(rig, a["id"], 0, ad["id"])
    R.wire_of(rig, a["id"], 0)["cables"] = [dict(R.new_cable("coax"), length="2 m"),
                                            dict(R.new_cable("jumper"), length='6"')]
    R.mate(rig, ad["id"], 0, blk["id"])
    R.connect(rig, blk["id"], 0, lna["id"])
    R.connect(rig, lna["id"], 0, r["id"])
    R.wire_of(rig, lna["id"], 0)["cables"] = [dict(R.new_cable("coax"), length="a bit")]
    rep = R.path_report(rig, R.paths(rig)[0])
    assert rep["cables"] == 3 and abs(rep["cable_m"] - 2.152) < 1e-3 and rep["unread_lengths"] == ["Coax"]
    assert rep["joins"] == 7 and rep["connectors"] == 14 and rep["mated"] == 1
    assert rep["genders"] == ["SMA adapter F \u2192 M SMA DC block: fits"]
    assert rep["power"] == [("LNA", False)]
    assert rep["estimate"]["unknown"] == 6


def test_length_reading():
    for text, m in (("2 m", 2.0), ("3 ft", 0.9144), ('6"', 0.1524), ("1'", 0.3048), ("150 cm", 1.5),
                    ("8ft", 2.4384), ("2,5 m", 2.5)):
        assert abs(R.length_m(text) - m) < 1e-9, text
    for text in (None, "", "long", "2 parsecs"):
        assert R.length_m(text) is None


# ---------------------------------------------------------------- relay 008
def test_every_kind_starts_as_a_rectangle_and_a_chosen_shape_stays():
    rig = R.new_rig()
    for t in R.PART_TYPES:
        assert R.shape_of(R.new_node(rig, t, 0, 0)) == "rect", t
    n = R.new_node(rig, "lna", 0, 0)
    n["shape"] = "triangle"
    assert R.shape_of(n) == "triangle"
    n["shape"] = "pipe"                                     # a pipe is an adapter's own drawing
    assert R.shape_of(n) == "rect"
    ad = R.new_node(rig, "adapter", 0, 0)
    ad["shape"] = "pipe"
    assert R.shape_of(ad) == "pipe"


def test_text_position_is_drawing_only_checked_and_kept_in_my_parts():
    rig = _split_rig()
    want = R.rig_texts("s", rig)
    for k, n in enumerate(rig["nodes"]):
        n["text_pos"] = R.TEXT_POS[k % 9]
    assert R.rig_texts("s", rig) == want
    lay = R.check_layout(json.loads(json.dumps({"rigs": {"s": rig}})))
    assert [n["text_pos"] for n in lay["rigs"]["s"]["nodes"]][:3] == ["tl", "tc", "tr"]
    assert R.text_pos({}) == "ml" and R.text_pos({"text_pos": "br"}) == "br" and R.text_pos({"text_pos": "x"}) == "ml"
    e = R.lib_entry(rig["nodes"][2], [])
    assert e["text_pos"] == "tr"
    assert R.node_from_lib(R.new_rig(), e, 0, 0)["text_pos"] == "tr"
    bad = json.loads(json.dumps(rig))
    bad["nodes"][0]["text_pos"] = "middle"
    with pytest.raises(ValueError):
        R.check_layout({"rigs": {"s": bad}})


def test_checks_for_runs_are_the_open_checks_of_the_rig_used_for_runs():
    """Relay 010: the start panel shows the Chain screen's open checks for the rig runs record,
    and nothing for a rig that is not used for runs."""
    rig = _split_rig()
    doc, lay = R.apply_rig({"chains": {}}, R.empty_layout(), "two radios", rig)
    lay = json.loads(json.dumps(lay))
    names = list(doc["chains"])
    used = {"chain": {"chains": doc["chains"], "current": names[0]}, "layout": lay}
    got = R.checks_for_runs(used)
    assert "LNA has no power feed" in [c["text"] for c in got] and all(c["rig"] == "two radios" for c in got)
    other = {"chain": {"chains": doc["chains"], "current": "something else"}, "layout": lay}
    assert R.checks_for_runs(other) == [] and R.checks_for_runs({}) == []
