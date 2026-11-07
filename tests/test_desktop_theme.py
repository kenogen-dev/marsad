"""The window's palette meets the contrast the brief asks for (relay 005): text 4.5:1
against what it sits on, icons and large text 3:1, and the wires that must be told apart
without any text differ in brightness as well as hue."""
import itertools

import pytest



from marsad_desktop import theme as T  # noqa: E402

C = T.C
SURFACES = ("background", "side", "panel", "raised", "raised2", "tile", "tile_hover", "menu", "menu_hover",
            "survey_sel", "check_bg", "plot")


@pytest.mark.parametrize("fg", ["text", "text2", "muted", "survey", "survey_light", "recorder_light"])
def test_text_meets_4_5_to_1_on_every_surface(fg):
    for bg in SURFACES:
        assert T.contrast(C[fg], C[bg]) >= 4.5, (fg, bg, round(T.contrast(C[fg], C[bg]), 2))


@pytest.mark.parametrize("fg", ["cat_source", "cat_sma", "cat_cable", "cat_condition", "cat_split", "cat_digital",
                                "cat_enclosure", "power", "wire", "guide", "lock", "survey", "recorder"])
def test_icons_and_lines_meet_3_to_1(fg):
    for bg in ("tile", "tile_hover", "raised", "plot", "background"):
        assert T.contrast(C[fg], C[bg]) >= 3.0, (fg, bg, round(T.contrast(C[fg], C[bg]), 2))


def test_wires_that_must_be_told_apart_differ_in_brightness():
    """A plain wire, a power wire and a branch in a radio's colour carry no text, so their
    colours must differ in brightness, not only in hue. Survey blue against recorder orange
    is left to the owner: both are base palette values (see relay 005's result)."""
    kinds = ("wire", "power", "survey", "recorder", "guide")
    for a, b in itertools.combinations(kinds, 2):
        if {a, b} in ({"survey", "recorder"}, {"guide", "survey"}, {"guide", "recorder"}):
            continue
        assert T.contrast(C[a], C[b]) >= 1.25, (a, b, round(T.contrast(C[a], C[b]), 3))


def test_spacing_and_weights_are_named():
    assert T.SPACE == 8 and T.BORDER == 1 and T.WIRE_W == 2.0 and T.WIRE_HOT_W > T.WIRE_W
    assert all(len(layer) == 3 for layer in T.SHADOW_LAYERS)


SECTIONS = ("cat_source", "cat_sma", "cat_cable", "cat_condition", "cat_split", "cat_digital", "cat_enclosure",
            "power", "survey")


def _hue_gap(a: str, b: str) -> float:
    import colorsys
    ha, la, sa = colorsys.rgb_to_hls(*(v / 255 for v in T._rgb(a)))
    hb, lb, sb = colorsys.rgb_to_hls(*(v / 255 for v in T._rgb(b)))
    d = abs(ha - hb)
    return min(d, 1 - d) * 360, abs(la - lb), abs(sa - sb)


def test_every_section_of_the_parts_list_has_a_colour_of_its_own():
    """Relay 006: before it, SMA parts, coax and radios were three close blues, and USB parts and USB
    cables one grey. Each section's colour now differs from every other in hue, lightness or saturation."""
    for a, b in itertools.combinations(SECTIONS, 2):
        hue, light, sat = _hue_gap(C[a], C[b])
        assert hue >= 18 or light >= 0.12 or sat >= 0.35, (a, b, round(hue), round(light, 2), round(sat, 2))


@pytest.mark.parametrize("name,fill", T.SWATCHES)
def test_text_on_an_owner_coloured_box_keeps_4_5_to_1(name, fill):
    main, soft = T.text_on(fill)
    assert T.contrast(main, fill) >= 4.5, name
    assert T.contrast(soft, fill) >= 4.5, name


@pytest.mark.parametrize("fill", ["#000000", "#FFFFFF", "#808080", "#777777", "#3B5BDB", "#FFEB3B", "#00897B"])
def test_text_on_any_colour_picks_light_or_dark_and_reads(fill):
    main, soft = T.text_on(fill)
    assert main in (C["text"], T.DARK_TEXT)
    assert T.contrast(main, fill) >= 4.5 and T.contrast(soft, fill) >= 4.5, fill


@pytest.mark.parametrize("colour", [hexv for _, hexv in T.SWATCHES] + ["#101010", "#2A0040"])
def test_an_enclosure_name_in_the_owners_colour_still_reads_on_the_grid(colour):
    assert T.contrast(T.readable_on(colour, C["plot"]), C["plot"]) >= 4.5


def test_kind_label_on_a_tinted_part_reads():
    for cat in ("source", "sma", "condition", "split", "digital"):
        fill = T.mix(C["raised"], C["cat_" + cat], 0.12)
        assert T.contrast(T.readable_on(C["cat_" + cat], fill), fill) >= 4.5, cat
        assert T.contrast(C["text"], fill) >= 4.5, cat



@pytest.mark.parametrize("under", ["#E8ECF1", "#4CC2FF", "#FFC850", "#FFFFFF"])
def test_see_through_menus_and_cards_keep_their_text_readable(under):
    """Relay 007: menus and the edit card are FLOAT_ALPHA opaque. Over the lightest thing in the window
    (a white swatch, a primary button, even pure white) their text still reads at 4.5:1."""
    for surface in ("menu", "raised"):
        seen = T.mix(C[surface], under, 1 - T.FLOAT_ALPHA)
        for fg in ("text", "muted", "survey_light"):
            assert T.contrast(C[fg], seen) >= 4.5, (surface, fg, under, round(T.contrast(C[fg], seen), 2))


def test_primary_and_danger_buttons_read():
    assert T.contrast(T.DARK_TEXT, C["primary"]) >= 4.5 and T.contrast(T.DARK_TEXT, C["primary_hover"]) >= 4.5
    assert T.contrast(C["alert_text"], C["background"]) >= 4.5 and T.contrast(C["alert_text"], C["alert_bg"]) >= 4.5
