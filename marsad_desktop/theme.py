"""The window's palette, in one place. Swap the base values here and every screen
follows: nothing else in the program names a colour.

Base values are the owner's current Chain design (relay 002). The derived shades
(raised panels, strong lines, the wire colour, tinted backgrounds) are mixed from
the base values the same way the design canvas mixes them.
"""
from __future__ import annotations

from functools import lru_cache

# The palette itself is plain Python, so its arithmetic (and the contrast tests) need no Qt;
# Qt is loaded only by the functions that hand colours and fonts to the window.

BASE = {
    "background": "#0E1116",
    "panel": "#161B22",
    "lines": "#2A313C",
    "text": "#FFFFFF",
    "muted": "#9AA4B2",
    "survey": "#4CC2FF",
    "recorder": "#FF9F43",
    "open": "#3CC8BE",
    "private": "#B48CFF",
    "anomaly": "#FFC850",
    "alert": "#FF5C5C",
}
RADIUS = 12                      # px, panels; smaller controls use 3/4 and 1/2 of it
SPACE = 8                        # px, the spacing rhythm: gaps and paddings are multiples of it
BORDER = 1                       # px, every panel, toolbar and menu border
TOOL_H = 32                      # px, every toolbar control's height (relay 007)
FLOAT_ALPHA = 0.92               # floating cards are faintly see-through (relay 007)
MENU_ALPHA = 0.85                # relay 016: menus a little more see-through; their text keeps 4.5:1 over any part
                                 # (menu_muted below is the lighter grey that makes that hold over a white node)
NAV_PAD_X = 16                   # px, the top navigation tabs' side padding (app.py sizes them from it)
SELECTED_BORDER = 2              # px, a selected part's border
WIRE_W, WIRE_HOT_W = 2.0, 3.0    # px, a wire at rest and when hovered or selected
# Relay 016: depth. Four elevation levels, each one step lighter than the one below, with a soft shadow that grows
# with height. ground: the window; surface: panels; raised: cards, toolbar groups, buttons and the parts on the grid;
# overlay: menus, floating cards, small dialogs and a selected part (lifted one level). The fills are in C as
# elev_<level>, the 1 px top-edge highlight's strength per level in HIGHLIGHT_ALPHA, the shadows in SHADOWS.
ELEVATIONS = ("ground", "surface", "raised", "overlay")
# A shadow: (offset y, spread, alpha) layers drawn as plain shapes, offset, which costs a few fills instead of a
# blur (speed first, relay 005). One step of lightness and shadow per level, kept subtle.
SHADOWS = {"ground": (), "surface": ((1, 0, 0.16),), "raised": ((2, 1, 0.20), (4, 3, 0.10)),
           "overlay": ((4, 2, 0.26), (9, 6, 0.11))}           # deeper, still two fills (speed)
SHADOW_LAYERS = SHADOWS["raised"] + ((7, 6, 0.05),)   # (kept for older callers: a part at rest)
HIGHLIGHT_ALPHA = {"ground": 0.0, "surface": 0.05, "raised": 0.09, "overlay": 0.12}
# Floating cards (a widget, not drawn by hand): a blurred shadow, made once per card.
CARD_SHADOW = {"blur": 22, "dy": 6, "alpha": 0.45}
GRID_RECESS = (14, 0.32)         # px and alpha: the inner shade at the grid's edges, so the drawing sits in something
UI_FONTS = ["Inter", "Segoe UI Variable Text", "Segoe UI"]
MONO_FONTS = ["JetBrains Mono", "Cascadia Mono", "Consolas"]


def _rgb(hex_colour: str) -> tuple[int, int, int]:
    h = hex_colour.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _mix(a: str, b: str, t: float) -> str:
    ca, cb = _rgb(a), _rgb(b)
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(ca, cb))


def _derive(b: dict) -> dict:
    c = dict(b)
    c.update(
        bar=_mix(b["background"], b["panel"], 0.5),
        plot=_mix(b["background"], "#000000", 0.3),
        raised=_mix(b["panel"], b["text"], 0.05),
        raised2=_mix(b["panel"], b["text"], 0.10),
        line_strong=_mix(b["lines"], b["text"], 0.10),
        text2=_mix(b["text"], b["muted"], 0.5),
        faint=_mix(b["muted"], b["panel"], 0.5),
        wire=_mix(b["muted"], b["panel"], 0.25),
        survey_light=_mix(b["survey"], "#FFFFFF", 0.35),
        survey_bg=_mix(b["panel"], b["survey"], 0.08),
        survey_sel=_mix(b["panel"], b["survey"], 0.12),
        recorder_light=_mix(b["recorder"], "#FFFFFF", 0.3),
        recorder_bg=_mix(b["panel"], b["recorder"], 0.10),
        anomaly_bg=_mix(b["panel"], b["anomaly"], 0.10),
        alert_bg=_mix(b["panel"], b["alert"], 0.10),
        # Colour coding of parts by what they do (Chain screen). Relay 006: every section of the
        # Parts list has a hue of its own, told apart from its neighbours (tests/test_desktop_theme.py).
        cat_source=b["open"],
        cat_sma="#F48FB1",                                        # SMA adapters, DC blocks, pads, bulkheads
        cat_cable="#E07A5F",                                      # coax and jumpers (copper), on the wires
        cat_condition=b["private"],
        cat_split=b["anomaly"],
        cat_digital=_mix(b["muted"], b["panel"], 0.15),
        cat_enclosure="#C9B79C",
        # Relay 005: the finished look.
        side=_mix(b["background"], b["panel"], 0.6),            # the Parts panel's own surface
        tile=_mix(b["panel"], b["text"], 0.07),                  # a palette tile, a step up from it
        tile_hover=_mix(b["panel"], b["text"], 0.12),
        tile_pressed=_mix(b["panel"], b["survey"], 0.20),
        tile_edge=_mix(b["lines"], b["text"], 0.06),
        menu=_mix(b["panel"], b["text"], 0.05),
        menu_hover=_mix(b["panel"], b["text"], 0.11),
        button_pressed=_mix(b["panel"], b["survey"], 0.16),
        shadow="#000000",
        # Power sources and power wires: lighter than the radio colours, so a power wire differs
        # from an RF branch in brightness as well as in hue (and it is dashed).
        power=_mix(_mix(b["open"], b["anomaly"], 0.55), "#FFFFFF", 0.4),
        power_bg=_mix(b["panel"], _mix(b["open"], b["anomaly"], 0.55), 0.10),
        guide=_mix(b["alert"], "#FFFFFF", 0.15),                 # smart guide lines while dragging
        lock=_mix(b["muted"], b["text"], 0.2),
        check_bg=_mix(b["panel"], b["anomaly"], 0.08),
        # Relay 007: the toolbar's primary and risky actions.
        primary=b["survey"],
        primary_hover=_mix(b["survey"], "#FFFFFF", 0.2),
        primary_pressed=_mix(b["survey"], "#000000", 0.15),
        alert_text=_mix(b["alert"], "#FFFFFF", 0.25),
    )
    # Relay 016: the elevation levels, a step of lightness each, and what makes a control read as pressable.
    c.update(
        elev_ground=b["background"],
        elev_surface=b["panel"],
        elev_raised=c["raised"],
        elev_overlay=_mix(b["panel"], b["text"], 0.11),
        highlight="#FFFFFF",                                     # the 1 px top edge, at HIGHLIGHT_ALPHA
        menu_muted=_mix(b["muted"], b["text"], 0.35),            # a menu's shortcuts and headings (4.5:1 over anything)
        edge_top=_mix(b["lines"], b["text"], 0.16),              # a control's top border: lit from above
        edge_bottom=_mix(b["lines"], "#000000", 0.35),           # and its bottom border: in shade
        btn_top=_mix(b["panel"], b["text"], 0.10),               # a button's fill, top to bottom
        btn_bottom=_mix(b["panel"], b["text"], 0.03),
        btn_hover_top=_mix(b["panel"], b["text"], 0.16),
        btn_hover_bottom=_mix(b["panel"], b["text"], 0.08),
        btn_sunk=_mix(b["background"], b["panel"], 0.5),         # pressed: darker than its surface, as if pushed in
        primary_top=_mix(b["survey"], "#FFFFFF", 0.14),          # the accent, with a gentle gradient
        primary_bottom=_mix(b["survey"], "#000000", 0.06),
        primary_hover_top=_mix(b["survey"], "#FFFFFF", 0.26),
        primary_hover_bottom=_mix(b["survey"], "#FFFFFF", 0.08),
        focus=_mix(b["survey"], "#FFFFFF", 0.2),                 # the focus ring
        seg_top=_mix(b["panel"], b["text"], 0.085),              # a toolbar group: raised, lit from above
        seg_bottom=_mix(b["panel"], b["text"], 0.02),
    )
    return c


C = _derive(BASE)

# A node's own colour (relay 006): the swatches offered on its card and right-click menu.
SWATCHES = [("Blue", "#4CC2FF"), ("Orange", "#FF9F43"), ("Teal", "#3CC8BE"), ("Violet", "#B48CFF"),
            ("Yellow", "#FFC850"), ("Red", "#FF5C5C"), ("Rose", "#F48FB1"), ("Green", "#7BD88F"),
            ("Slate", "#5B6878"), ("Navy", "#1F3A5F"), ("Plum", "#4A2D5C"), ("White", "#E8ECF1")]
DARK_TEXT = "#000000"           # pure black and white: one of the two always reaches 4.5:1 on any fill


@lru_cache(maxsize=4096)
def mix(a: str, b: str, t: float) -> str:
    """Two #rrggbb colours mixed, t of the way from a to b (cached: parts are painted often)."""
    return _mix(a, b, t)


def is_hex(v) -> bool:
    return isinstance(v, str) and len(v) == 7 and v[0] == "#" and all(c in "0123456789abcdefABCDEF" for c in v[1:])


@lru_cache(maxsize=1024)
def text_on(fill: str) -> tuple[str, str]:
    """Text for a node filled with `fill`: (main, secondary), each at least 4.5:1 against it.
    Light or dark, whichever reads better; the secondary is a softer shade of it when that
    still keeps 4.5:1, else the main colour again."""
    main = C["text"] if contrast(C["text"], fill) >= contrast(DARK_TEXT, fill) else DARK_TEXT
    soft = _mix(main, fill, 0.22)
    return main, soft if contrast(soft, fill) >= 4.5 else main


@lru_cache(maxsize=4096)
def readable_on(colour: str, bg: str, need: float = 4.5) -> str:
    """`colour` lightened (on a dark background) or darkened until it reads at `need`:1 on `bg`.
    For an enclosure's title in the owner's own colour."""
    toward = "#FFFFFF" if luminance(bg) < 0.5 else "#000000"
    for k in range(0, 21):
        c = _mix(colour, toward, k / 20)
        if contrast(c, bg) >= need:
            return c
    return toward


_QT: dict = {}            # Qt classes, imported once on first use (an import per call is slow in PySide)
_CACHE: dict = {}         # colours, family names and fonts: made once, handed out as copies


def _qt(name: str):
    if not _QT:
        from PySide6.QtGui import QColor, QFont, QFontDatabase
        _QT.update(QColor=QColor, QFont=QFont, QFontDatabase=QFontDatabase)
    return _QT[name]


def color(name: str, alpha: float | None = None):
    """A palette colour as a QColor (a copy each time, so the caller may change it)."""
    key = ("c", name, alpha)
    q = _CACHE.get(key)
    if q is None:
        q = _qt("QColor")(C[name])
        if alpha is not None:
            q.setAlphaF(alpha)
        _CACHE[key] = q
    return _qt("QColor")(q)


def qc(name: str, alpha: float | None = None):
    """A palette colour as the cached QColor itself, no copy (relay 016, speed): only for handing straight to a
    pen or a brush, which copy it. Never change what this returns; use color() for a colour to change."""
    key = ("c", name, alpha)
    q = _CACHE.get(key)
    if q is None:
        color(name, alpha)
        q = _CACHE[key]
    return q


def rgba(name: str, alpha: float) -> str:
    """A palette colour with transparency, for style sheets."""
    r, g, b = _rgb(C[name])
    return f"rgba({r},{g},{b},{alpha})"


def luminance(hex_colour: str) -> float:
    """WCAG relative luminance of a colour."""
    r, g, b = _rgb(hex_colour)

    def ch(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio between two colours (1 to 21)."""
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _first(families: list[str]) -> str:
    """The first of these font families installed here (asked of Qt once, then remembered:
    the list of installed families is slow to fetch and painting asks for fonts constantly)."""
    key = ("family", tuple(families))
    if key not in _CACHE:
        have = set(_qt("QFontDatabase").families())
        _CACHE[key] = next((f for f in families if f in have), families[-1])
    return _CACHE[key]


def ui_font(size: float = 10.5, weight=None):
    QFont = _qt("QFont")
    weight = weight if weight is not None else QFont.Weight.Normal
    key = ("ui", size, weight)
    f = _CACHE.get(key)
    if f is None:
        f = QFont(_first(UI_FONTS))
        f.setPointSizeF(size)
        f.setWeight(weight)
        _CACHE[key] = f
    return QFont(f)


def mono_font(size: float = 9.5):
    QFont = _qt("QFont")
    key = ("mono", size)
    f = _CACHE.get(key)
    if f is None:
        f = QFont(_first(MONO_FONTS))
        f.setPointSizeF(size)
        f.setStyleHint(QFont.StyleHint.Monospace)
        _CACHE[key] = f
    return QFont(f)


def gradient(top: str, bottom: str) -> str:
    """A top-to-bottom fill for a style sheet: lit from above (relay 016)."""
    return f"qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {top}, stop:1 {bottom})"


def lit_edges(level: str, side: str = "lines") -> str:
    """A hairline border whose top edge carries the level's 1 px highlight, so layers separate (relay 016)."""
    a = HIGHLIGHT_ALPHA[level]
    top = _mix(C[side], "#FFFFFF", min(1.0, a * 2.5))
    bottom = _mix(C[side], "#000000", 0.25)
    return f"border: {BORDER}px solid {C[side]}; border-top-color: {top}; border-bottom-color: {bottom};"


def stylesheet() -> str:
    r6, r4 = round(RADIUS * 0.75), round(RADIUS * 0.5)
    b, s = BORDER, SPACE
    btn = gradient(C["btn_top"], C["btn_bottom"])
    btn_hover = gradient(C["btn_hover_top"], C["btn_hover_bottom"])
    prim = gradient(C["primary_top"], C["primary_bottom"])
    prim_hover = gradient(C["primary_hover_top"], C["primary_hover_bottom"])
    # Relay 016, depth: buttons read as pressable (lit top edge, shaded bottom edge, a gentle gradient; a lighter
    # fill on hover; pressed sinks: darker, edges swapped, the label one pixel lower); panels and cards carry a
    # hairline border with a highlight on the top edge; floating things sit on the overlay level; a clear focus ring.
    pressable = f"border: {b}px solid {C['line_strong']}; border-top-color: {C['edge_top']}; " \
                f"border-bottom-color: {C['edge_bottom']};"
    sunk = f"border-top-color: {C['edge_bottom']}; border-bottom-color: {C['edge_top']};"
    return f"""
    QWidget {{ background: {C['elev_ground']}; color: {C['text']}; }}
    QLabel {{ background: transparent; }}
    QLabel[muted="true"] {{ color: {C['muted']}; }}
    #TopBar {{ background: {gradient(_mix(C['bar'], '#FFFFFF', 0.025), C['bar'])}; border-bottom: {b}px solid {C['edge_bottom']}; }}
    #Toolbar {{ border-bottom: {b}px solid {C['lines']}; }}
    #Side {{ background: {C['side']}; border-right: {b}px solid {C['lines']}; }}
    #Side QLabel {{ background: transparent; }}
    #Palette {{ background: transparent; border: none; outline: none; }}
    #Checks {{ background: {C['check_bg']}; {lit_edges('surface')} border-radius: {r6}px; }}
    QPushButton#CheckChip {{ background: transparent; border: {b}px solid transparent; border-radius: {r4}px;
                   padding: 3px {s}px; min-height: 18px; text-align: left; color: {C['text']}; }}
    QPushButton#CheckChip:hover {{ background: {C['raised']}; border-color: {C['line_strong']}; }}
    QMenu {{ background: {rgba('menu', MENU_ALPHA)}; border: {b}px solid {C['line_strong']}; padding: {s - 2}px 0; }}
    QMenu::item {{ padding: 7px {s * 6}px 7px {s * 4 + 2}px; }}
    QMenu::separator {{ height: {s + 1}px; }}
    QPushButton {{ background: {btn}; {pressable} border-radius: {r6}px; padding: 7px 14px; min-height: 22px; }}
    QPushButton:hover {{ background: {btn_hover}; }}
    QPushButton:pressed {{ background: {C['btn_sunk']}; {sunk} padding: 8px 14px 6px 14px; }}
    QPushButton:focus {{ border-color: {C['focus']}; }}
    QPushButton::menu-indicator {{ image: none; width: 0px; }}
    #Segment {{ background: {gradient(C['seg_top'], C['seg_bottom'])}; {lit_edges('raised')} border-radius: {r6}px; }}
    #Segment QLabel, #Segment QCheckBox {{ background: transparent; padding: 0 {s // 2}px; }}
    QToolButton#Tool, QToolButton#Primary, QToolButton#Danger {{ border-radius: {r4}px; padding: 4px 10px;
                   border: {b}px solid transparent; color: {C['text']}; }}
    QToolButton#Tool {{ background: {btn}; {pressable} }}
    #Segment QToolButton#Tool {{ background: transparent; border-color: transparent; }}
    QToolButton#Tool:hover, #Segment QToolButton#Tool:hover {{ background: {btn_hover}; border-color: {C['line_strong']};
                   border-top-color: {C['edge_top']}; border-bottom-color: {C['edge_bottom']}; }}
    QToolButton#Tool:pressed, #Segment QToolButton#Tool:pressed {{ background: {C['btn_sunk']}; border-color: {C['lines']};
                   {sunk} padding: 5px 10px 3px 10px; }}
    QToolButton#Tool:checked, #Segment QToolButton#Tool:checked {{ background: {C['survey_sel']}; border-color: {C['survey']};
                   color: {C['survey_light']}; }}
    QToolButton#Tool:disabled, #Segment QToolButton#Tool:disabled {{ color: {C['faint']}; }}
    QToolButton#Tool:disabled {{ background: transparent; border-color: {C['lines']}; }}
    #Segment QToolButton#Tool:disabled {{ border-color: transparent; }}
    QToolButton#Tool:focus, #Segment QToolButton#Tool:focus {{ border-color: {C['focus']}; }}
    QToolButton#Primary, QPushButton#Primary {{ background: {prim}; color: {DARK_TEXT}; font-weight: 600; padding: 4px 14px;
                   border: {b}px solid {C['primary_bottom']}; border-top-color: {C['primary_hover_top']};
                   border-bottom-color: {_mix(C['survey'], '#000000', 0.3)}; }}
    QToolButton#Primary:hover, QPushButton#Primary:hover {{ background: {prim_hover}; }}
    QToolButton#Primary:pressed, QPushButton#Primary:pressed {{ background: {C['primary_pressed']};
                   border-top-color: {_mix(C['survey'], '#000000', 0.3)}; border-bottom-color: {C['primary_hover_top']};
                   padding: 5px 14px 3px 14px; }}
    QToolButton#Primary:disabled, QPushButton#Primary:disabled {{ background: {C['raised']}; color: {C['faint']};
                   border-color: {C['lines']}; }}
    QToolButton#Danger {{ background: transparent; color: {C['alert_text']}; border-color: {rgba('alert', 0.45)}; }}
    QToolButton#Danger:hover {{ background: {C['alert_bg']}; border-color: {C['alert']}; }}
    QToolButton#Danger:pressed {{ background: {rgba('alert', 0.25)}; padding: 5px 10px 3px 10px; }}
    QToolButton#Danger:disabled {{ color: {C['faint']}; border-color: {C['lines']}; }}
    QToolButton:focus {{ border-color: {C['focus']}; }}
    QToolButton#Primary:focus, QPushButton#Primary:focus {{ border-color: {C['text']}; }}
    QToolButton::menu-indicator {{ image: none; width: 0px; }}
    QComboBox#RigPick {{ background: {C['bar']}; font-weight: 600; }}
    QPushButton:disabled {{ color: {C['faint']}; background: {C['panel']}; border-color: {C['lines']}; }}
    QPushButton[accent="true"] {{ background: transparent; border-color: {C['survey']}; color: {C['survey']}; }}
    QPushButton[accent="true"]:hover {{ background: {C['survey_bg']}; }}
    QPushButton[accent="true"]:checked {{ background: {C['survey_sel']}; }}
    QPushButton[nav="true"] {{ background: transparent; border: {b}px solid transparent; color: {C['text2']};
                   padding: 8px {NAV_PAD_X}px; }}
    QPushButton[nav="true"]:checked {{ background: {gradient(C['btn_hover_top'], C['raised'])}; color: {C['text']};
                   font-weight: 600; border-top-color: {C['edge_top']}; }}
    QPushButton[nav="true"]:hover {{ background: {C['raised']}; color: {C['text']}; }}
    QPushButton[nav="true"]:checked:hover {{ background: {C['raised2']}; }}
    QPushButton[nav="true"]:pressed {{ background: {C['btn_sunk']}; }}
    QComboBox, QLineEdit, QSpinBox, QPlainTextEdit {{ background: {C['bar']}; border: 1px solid {C['line_strong']};
                   border-top-color: {C['edge_bottom']}; border-radius: {r4}px; padding: 5px 8px;
                   selection-background-color: {C['survey_sel']}; }}
    QComboBox {{ background: {C['btn_bottom']}; min-height: 22px; border-top-color: {C['edge_top']};
                   border-bottom-color: {C['edge_bottom']}; }}
    QComboBox:hover {{ background: {C['btn_hover_bottom']}; }}
    QComboBox:focus, QLineEdit:focus, QSpinBox:focus, QPlainTextEdit:focus {{ border-color: {C['focus']}; }}
    QComboBox QAbstractItemView {{ background: {C['elev_overlay']}; border: 1px solid {C['line_strong']};
                   selection-background-color: {C['raised2']}; }}
    QListWidget {{ background: transparent; border: none; outline: none; }}
    QListWidget::item {{ background: {C['panel']}; {lit_edges('surface')} border-radius: {r6}px;
                   padding: 6px 10px; margin: 2px 0; }}
    QListWidget::item:hover {{ background: {C['raised']}; }}
    #Templates::item:selected {{ background: {C['raised2']}; border: 1px solid {C['survey']}; color: {C['text']}; }}
    QCheckBox {{ background: transparent; spacing: 8px; }}
    #Card {{ background: {rgba('elev_overlay', FLOAT_ALPHA)}; {lit_edges('overlay', 'line_strong')} border-radius: {RADIUS}px; }}
    #Card QLabel, #Card QCheckBox {{ background: transparent; }}
    #TextPanel {{ background: {C['elev_surface']}; {lit_edges('surface')} border-radius: {RADIUS}px; }}
    #Notice {{ background: {C['anomaly_bg']}; border: 1px solid {C['anomaly']}; border-radius: {r6}px; padding: 8px 12px; }}
    #Pill {{ border-radius: 10px; padding: 2px 10px; font-weight: 600; }}
    QMessageBox, QInputDialog, #NewRigDialog {{ background: {C['elev_overlay']}; }}
    QScrollBar {{ background: {C['background']}; width: 10px; height: 10px; }}
    QScrollBar::handle {{ background: {C['line_strong']}; border-radius: 4px; min-height: 20px; min-width: 20px; }}
    QScrollBar::handle:hover {{ background: {C['edge_top']}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
    QToolTip {{ background: {C['elev_overlay']}; color: {C['text']}; border: 1px solid {C['line_strong']}; padding: 4px; }}
    """
