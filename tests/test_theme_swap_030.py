"""Relay 030 (known issue 14): a theme change is applied in place. Each widget's own style sheet has its old palette
colours swapped for the new theme's, hex and rgba alike; colours that are not palette colours are left alone."""
import sys

from conftest import ROOT

sys.path.insert(0, str(ROOT))

from marsad_desktop import theme as T  # noqa: E402


def test_every_token_is_swapped_in_hex_and_rgba():
    names = list(T.THEMES)
    for a, b in zip(names, names[1:] + names[:1]):
        old, new = T.palette(a), T.palette(b)
        swap = T.colour_swap(old, new)
        for k in ("text", "muted", "panel", "lines", "survey", "recorder", "alert", "anomaly"):
            sheet = f"color: {old[k]}; border: 1px solid {T.rgba(k, 0.5, old)};"
            got = T.swap_sheet(sheet, swap)
            first = next(t for t in old if T.is_hex(old[t]) and old[t].lower() == old[k].lower())
            assert new[first] in got, (a, b, k)
            r, g, bb = T._rgb(new[first])
            assert f"rgba({r},{g},{bb},0.5)" in got


def test_other_colours_and_text_stay():
    old, new = T.palette(list(T.THEMES)[0]), T.palette(list(T.THEMES)[1])
    swap = T.colour_swap(old, new)
    odd = "#123457" if "#123457" not in {v.lower() for v in old.values() if T.is_hex(v)} else "#123459"
    sheet = f"QLabel#X {{ color: {odd}; font-weight: 600; }}"
    assert T.swap_sheet(sheet, swap) == sheet
    assert T.swap_sheet("", swap) == ""
