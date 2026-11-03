"""Collapsible panels on every tab.

The pages make every panel collapsible from its title line at load time, so a panel
added later without a title would silently get no control. This pins the markup the
scripts rely on: every .wrap in index.html starts with its .lab title, every section
of the Builder starts with its .ph, and the scripts keep their accessible button,
their per-viewer storage (inside try/catch) and the redraw on expand.
"""
from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "marsad" / "web"
VOID = {"br", "img", "input", "hr", "meta", "link", "source", "col", "wbr"}


class FirstChild(HTMLParser):
    """For every element with `cls`, the classes of its first child element."""

    def __init__(self, tag: str | None, cls: str):
        super().__init__()
        self.tag, self.cls, self.found, self.waiting = tag, cls, [], False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = (a.get("class") or "").split()
        if self.waiting:
            self.found[-1] = (self.found[-1][0], classes)
            self.waiting = False
        if self.cls in classes and (self.tag is None or tag == self.tag) and tag not in VOID:
            self.found.append((a.get("id"), None))
            self.waiting = True


def first_children(path: Path, tag: str | None, cls: str) -> list:
    s = path.read_text(encoding="utf-8")
    body = s[s.index("<body"):]
    p = FirstChild(tag, cls)
    p.feed(body)
    return p.found


def test_every_web_app_panel_starts_with_its_title():
    found = first_children(WEB / "index.html", None, "wrap")
    assert len(found) >= 20
    missing = [pid for pid, first in found if not first or "lab" not in first]
    assert missing == []


def test_every_builder_panel_starts_with_its_title():
    found = first_children(WEB / "builder.html", "section", "panel")
    assert len(found) == 8
    assert all(first and "ph" in first for _, first in found)


def test_panel_scripts_are_accessible_remembered_and_redraw():
    for name in ("index.html", "builder.html"):
        s = (WEB / name).read_text(encoding="utf-8")
        assert "aria-expanded" in s
        assert ('b.type = "button"' in s) or ("b.type='button'" in s)  # a real button: keyboard reaches it
        assert "sdr-panel:" in s
        i = s.index("localStorage.getItem(\"sdr-panel:" if name == "builder.html" else "localStorage.getItem(PANEL_PREFIX")
        assert "try" in s[i - 60:i]                                  # the page works without storage
    s = (WEB / "index.html").read_text(encoding="utf-8")
    assert "if(panelOpen('spec'))drawSpec" in s and "panelRedraw" in s
