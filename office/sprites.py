"""Sprite grids and per-desk rendering (tier 0 ASCII / tier 1 half-block).

Ported from mock/office_mock.py (Stage 1). Sprites are stored as
"char == palette key" grids so no image assets are needed; tier 2 (kitty)
would generate a PNG from the same grids and is out of scope here.

A desk is 16px x 12px == 16 cols x 6 text rows (one half-block cell = 2px
tall). Visual states come from character-states.md section 1.
"""

# ---------------------------------------------------------------- palette

PALETTE = {
    "floor_a": (48, 48, 58),
    "floor_b": (42, 42, 50),
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
}

# Non-positional sprite chars -> fixed palette key. 'B' (shirt), 'M' (screen)
# and 'S' (skin) are resolved per visual state at render time.
SPRITE_COLORS = {
    "W": "bubble",
    "!": "bubble_text",
    "X": "alert",
    "V": "check",
    "K": "coffee",
    "~": "steam",
    "D": "desk",
    "d": "desk_dark",
}

DESK_W, DESK_H = 16, 12          # pixels
DESK_ROWS = DESK_H // 2          # text rows for the sprite (6)

# ---------------------------------------------------------------- grids

BASE = [
    "................",
    "................",
    "................",
    "....SSS.........",
    "....SSS.........",
    "...BBBBB..MMMM..",
    "...BBBBB..MMMM..",
    "................",
    "DDDDDDDDDDDDDDDD",
    ".dd..........dd.",
    ".dd..........dd.",
    "................",
]


def _patch(grid, changes):
    g = list(grid)
    for row, col, text in changes:
        g[row] = g[row][:col] + text + g[row][col + len(text):]
    return g


EMPTY = _patch(BASE, [
    (3, 4, "..."), (4, 4, "..."),
    (5, 3, "....."), (6, 3, "....."),
])

UNKNOWN = list(BASE)

IDLE = _patch(BASE, [(6, 12, "K")])
IDLE_F1 = _patch(IDLE, [(5, 12, "~")])

WORKING = _patch(BASE, [(7, 4, "S.S")])
WORKING_F1 = _patch(BASE, [(7, 3, "S...S")])

BLOCKED = _patch(BASE, [
    (1, 2, "WWW"),
    (2, 2, "W!W"),
    (3, 8, "S"),
    (4, 8, "B"),
])
BLOCKED_F1 = _patch(BASE, [   # bubble blinks off; hand stays raised
    (3, 8, "S"),
    (4, 8, "B"),
])

DONE = _patch(BASE, [
    (0, 10, ".V"),
    (1, 8, "V.V"),
    (2, 9, "V"),
])

# visual_state -> (frame0 grid, frame1 grid, shirt key, screen key)
VISUAL = {
    "empty":   (EMPTY, EMPTY, "shirt_unknown", "screen_off"),
    "unknown": (UNKNOWN, UNKNOWN, "shirt_unknown", "screen_off"),
    "idle":    (IDLE, IDLE_F1, "shirt_idle", "screen_off"),
    "working": (WORKING, WORKING_F1, "shirt_working", "screen_on"),
    "blocked": (BLOCKED, BLOCKED_F1, "shirt_blocked", "screen_on"),
    "done":    (DONE, DONE, "shirt_done", "screen_off"),
}

# ---------------------------------------------------------------- tier 1

def _pixel_key(ch, x, y, shirt, screen, skin):
    if ch == ".":
        return "floor_a" if ((x // 4) + (y // 4)) % 2 == 0 else "floor_b"
    if ch == "B":
        return shirt
    if ch == "M":
        return screen
    if ch == "S":
        return skin
    return SPRITE_COLORS[ch]


def desk_pixel_rows(visual_state, frame, palette=PALETTE):
    """Return DESK_H rows of (r,g,b) tuples for a visual state + anim frame."""
    g0, g1, shirt, screen = VISUAL[visual_state]
    grid = g1 if frame % 2 else g0
    skin = "shirt_unknown" if visual_state == "unknown" else "skin"
    rows = []
    for y, line in enumerate(grid):
        rows.append([palette[_pixel_key(ch, x, y, shirt, screen, skin)]
                     for x, ch in enumerate(line)])
    return rows


def _truecolor_cell(top, bottom):
    return "\x1b[38;2;%d;%d;%dm\x1b[48;2;%d;%d;%dm▀" % (top + bottom)


def _rgb_to_256(r, g, b):
    # 6x6x6 color cube (indices 16..231); grays handled by the cube too.
    def c(v):
        return 0 if v < 48 else (5 if v > 230 else round((v - 35) / 40))
    return 16 + 36 * c(r) + 6 * c(g) + c(b)


def _c256_cell(top, bottom):
    return "\x1b[38;5;%dm\x1b[48;5;%dm▀" % (
        _rgb_to_256(*top), _rgb_to_256(*bottom))


def halfblock_lines(pixel_rows, truecolor=True):
    """Fold two pixel rows into one text row using the upper half block."""
    cell = _truecolor_cell if truecolor else _c256_cell
    out = []
    for y in range(0, len(pixel_rows), 2):
        top, bottom = pixel_rows[y], pixel_rows[y + 1]
        out.append("".join(cell(t, b) for t, b in zip(top, bottom)) + "\x1b[0m")
    return out


def desk_tier1_lines(visual_state, frame, truecolor=True, palette=PALETTE):
    return halfblock_lines(desk_pixel_rows(visual_state, frame, palette),
                           truecolor)


# ---------------------------------------------------------------- tier 0

ASCII_ART = {
    "empty":   ["         ", "         ", " [_____] ", "         "],
    "unknown": ["    ?    ", "   (o)   ", " [_____] ", "         "],
    "idle":    ["         ", "   o   c ", "  /|\\    ", " [_____] "],
    "working": ["         ", "   o     ", "  /|\\ ## ", " [_____] "],
    "blocked": ["    !    ", "   o/    ", "  /|     ", " [_____] "],
    "done":    ["    *    ", "   o     ", "  /|\\    ", " [_____] "],
}
ASCII_W = 9
ASCII_ROWS = 4


def desk_tier0_lines(visual_state, frame):
    art = list(ASCII_ART[visual_state])
    if visual_state == "blocked" and frame % 2:
        art[0] = " " * ASCII_W          # bubble blink
    return art
