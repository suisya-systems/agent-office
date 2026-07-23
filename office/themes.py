"""Colour themes - the `[office] theme` setting (design.md section 8).

A theme carries two independent colour sets, because the office draws in two
very different places:

* `palette` - the **sprite** colours. Every pixel the character grids produce
  is one of these, and each one is painted with its own background (a
  half-block cell in tier 1, a real pixel in tier 2), so a theme may pick any
  colour it likes here without worrying about what is behind it.
* `ui` - the **text** colours: the header, nameplates, status words and the
  selection frame. These are foreground colours drawn straight onto the user's
  own terminal background, which the office neither sets nor knows. They are
  therefore kept to mid-tone, saturated values that stay legible on a light and
  a dark terminal alike - a theme that pushed them as light or as dark as its
  sprite palette would be unreadable on half the terminals out there.

Splitting the two is also what lets a theme reach tier 0, which has no sprite
pixels at all and is styled entirely out of `ui`.

`default` reproduces the pre-theme colours exactly, so the setting is opt-in in
effect as well as in name. All palettes here are written for this project; no
third-party asset or palette is copied in (design.md section 11).
"""

# Sprite palette keys every theme must define. `hair_a`..`hair_e` are the
# character accent slots (sprites.CHARACTERS picks one per agent), kept as
# numbered slots rather than per-agent keys so adding an agent character never
# forces every theme to grow a matching entry.
DEFAULT_PALETTE = {
    "floor_a": (48, 48, 58),
    "floor_b": (42, 42, 50),
    # The lit floor under the focused desk. Named per theme rather than
    # derived, because a fixed brightness lift would blow out a light theme.
    "floor_focus_a": (70, 70, 92),
    "floor_focus_b": (62, 62, 82),
    "desk": (139, 94, 60),
    "desk_dark": (108, 72, 46),
    "screen_on": (80, 200, 120),
    "screen_off": (70, 70, 82),
    "skin": (255, 204, 153),
    "shirt_idle": (98, 160, 234),
    "shirt_working": (80, 200, 120),
    "shirt_blocked": (255, 165, 0),
    "shirt_done": (189, 147, 249),
    "shirt_unknown": (128, 128, 132),
    "bubble": (240, 240, 240),
    "bubble_text": (60, 60, 70),
    "alert": (255, 85, 85),
    "check": (80, 250, 123),
    "coffee": (200, 200, 210),
    "steam": (170, 170, 180),
    "accent": (80, 220, 220),
    "hair_default": (90, 78, 70),
    "hair_a": (222, 120, 80),
    "hair_b": (120, 200, 255),
    "hair_c": (170, 140, 255),
    "hair_d": (250, 210, 90),
    "hair_e": (140, 235, 200),
}

# Text colour keys. `unknown` doubles as the status colour for a desk whose
# agent status herdr has not resolved.
DEFAULT_UI = {
    "accent": (80, 220, 220),
    "alert": (255, 85, 85),
    "idle": (98, 160, 234),
    "working": (80, 200, 120),
    "blocked": (255, 165, 0),
    "done": (189, 147, 249),
    "unknown": (128, 128, 132),
}

# name -> (palette overrides, ui overrides). Anything a theme leaves out falls
# back to the default above, so a theme only states what it actually changes.
_THEMES = {
    "default": ({}, {}),
    "midnight": (
        {
            "floor_a": (26, 26, 46),
            "floor_b": (20, 20, 38),
            "floor_focus_a": (46, 44, 80),
            "floor_focus_b": (38, 36, 68),
            "desk": (74, 58, 94),
            "desk_dark": (54, 42, 70),
            "screen_on": (0, 229, 255),
            "screen_off": (44, 40, 66),
            "skin": (240, 190, 150),
            "shirt_idle": (110, 130, 255),
            "shirt_working": (0, 229, 255),
            "shirt_blocked": (255, 140, 60),
            "shirt_done": (215, 140, 255),
            "shirt_unknown": (110, 110, 140),
            "bubble": (230, 230, 255),
            "bubble_text": (40, 36, 60),
            "alert": (255, 70, 110),
            "check": (120, 255, 190),
            "coffee": (180, 180, 215),
            "steam": (150, 150, 185),
            "accent": (140, 120, 255),
            "hair_default": (70, 62, 92),
            "hair_a": (255, 120, 140),
            "hair_b": (110, 200, 255),
            "hair_c": (190, 150, 255),
            "hair_d": (255, 220, 120),
            "hair_e": (120, 255, 210),
        },
        {
            "accent": (150, 130, 255),
            "alert": (255, 80, 120),
            "idle": (120, 140, 255),
            "working": (0, 220, 245),
            "blocked": (255, 150, 70),
            "done": (215, 140, 255),
            "unknown": (120, 120, 150),
        },
    ),
    "daylight": (
        {
            "floor_a": (232, 232, 238),
            "floor_b": (220, 220, 228),
            "floor_focus_a": (208, 216, 240),
            "floor_focus_b": (196, 204, 230),
            "desk": (196, 146, 96),
            "desk_dark": (160, 116, 74),
            "screen_on": (40, 160, 90),
            "screen_off": (188, 188, 200),
            "skin": (245, 196, 150),
            "shirt_idle": (40, 110, 200),
            "shirt_working": (30, 150, 80),
            "shirt_blocked": (215, 120, 10),
            "shirt_done": (130, 80, 190),
            "shirt_unknown": (150, 150, 158),
            "bubble": (255, 255, 255),
            "bubble_text": (70, 70, 80),
            "alert": (200, 40, 40),
            "check": (30, 150, 70),
            "coffee": (110, 110, 125),
            "steam": (170, 170, 185),
            "accent": (20, 120, 160),
            "hair_default": (100, 80, 64),
            "hair_a": (180, 80, 50),
            "hair_b": (40, 120, 190),
            "hair_c": (120, 70, 180),
            "hair_d": (200, 150, 30),
            "hair_e": (30, 150, 120),
        },
        {
            "accent": (0, 140, 180),
            "alert": (215, 50, 50),
            "idle": (50, 120, 205),
            "working": (35, 155, 85),
            "blocked": (205, 120, 15),
            "done": (140, 90, 195),
            "unknown": (135, 135, 145),
        },
    ),
}

# `default` first so the config warning reads as an offer, not a list.
NAMES = ("default", "midnight", "daylight")

DEFAULT_NAME = "default"


class Theme:
    """A resolved theme: complete palette and ui maps, no lookups left."""

    __slots__ = ("name", "palette", "ui")

    def __init__(self, name, palette, ui):
        self.name = name
        self.palette = palette
        self.ui = ui


def get(name=DEFAULT_NAME) -> Theme:
    """Resolve a theme by name, falling back to `default` for a bad name.

    Config validation already rejects unknown names with a warning, so this
    only has to be total, not chatty: a Renderer constructed straight from a
    string in a test or a future caller still gets a usable theme.
    """
    palette_over, ui_over = _THEMES.get(name, _THEMES[DEFAULT_NAME])
    if name not in _THEMES:
        name = DEFAULT_NAME
    return Theme(name,
                 dict(DEFAULT_PALETTE, **palette_over),
                 dict(DEFAULT_UI, **ui_over))
