"""Sprite grids and per-desk rendering (tier 0 ASCII / tier 1 half-block).

Ported from mock/office_mock.py (Stage 1). Sprites are stored as
"char == palette key" grids so no image assets are needed; tier 2 (kitty)
generates a PNG from the very same grids (see office/graphics.py), which is why
`desk_pixel_rows` is the one place a visual state turns into colour.

A desk is 16px x 12px == 16 cols x 6 text rows (one half-block cell = 2px
tall). Visual states come from character-states.md section 1.

**Composition order matters.** A grid is built as BASE -> character decoration
-> visual state patch, in that order, so a state patch always wins: the raised
hand and the speech bubble are load-bearing (they are how "blocked" reads at a
glance) and must never be knocked out by whichever character happens to be
sitting there. The reverse order would let a tall helmet erase the bubble.

Colours come from a theme palette (office/themes.py) passed in by the caller;
the grids themselves are theme-independent.
"""

import re

from . import themes

# ---------------------------------------------------------------- palette

# The pre-theme palette, kept as the module-level default so a caller that does
# not care about theming (a test, the mock) still gets colours.
PALETTE = themes.DEFAULT_PALETTE

# Non-positional sprite chars -> fixed palette key. 'B' (shirt), 'M' (screen),
# 'S' (skin) and 'H' (character hair/headgear) are resolved per visual state
# and per character at render time.
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


# ------------------------------------------------------- agent characters

class Character:
    """One agent's look: a headgear patch, an accent colour and an ASCII head.

    design.md section 11 ("agent-specific characters", adopted): the `agent`
    field herdr already reports picks the character, so a new agent that herdr
    learns to detect shows up as the default character rather than as nothing.
    The patch only ever touches rows 1-3 around the head, which keeps every
    character compatible with every visual state.
    """

    __slots__ = ("name", "patch", "hair", "ascii_head")

    def __init__(self, name, patch, hair, ascii_head):
        self.name = name
        self.patch = patch
        self.hair = hair
        self.ascii_head = ascii_head


DEFAULT_CHARACTER = Character("default", [(2, 4, "HHH")], "hair_default", "o")

CHARACTERS = {
    # claude: side-swept lock       codex: cap with a brim to the right
    "claude": Character("claude", [(2, 4, "HHH"), (3, 3, "H")], "hair_a", "o"),
    "codex": Character("codex", [(2, 4, "HHH"), (3, 7, "H")], "hair_b", "O"),
    # gemini: twin antennae         cursor: single caret antenna
    "gemini": Character("gemini", [(1, 4, "H.H"), (2, 4, "HHH")], "hair_c", "8"),
    "cursor": Character("cursor", [(1, 5, "H"), (2, 4, "HHH")], "hair_d", "e"),
    # droid: tall block helmet
    "droid": Character("droid", [(1, 4, "HHH"), (2, 4, "HHH")], "hair_e", "#"),
}


def character_for(agent):
    """Pick a character for an `agent` value; unknown agents get the default."""
    if not agent:
        return DEFAULT_CHARACTER
    key = str(agent).strip().lower()
    found = CHARACTERS.get(key)
    if found is not None:
        return found
    # herdr names agents plainly ("claude", "codex"), but be forgiving about a
    # variant like "claude-code" rather than dropping it to the default.
    head = re.split(r"[^a-z0-9]+", key)[0]
    return CHARACTERS.get(head, DEFAULT_CHARACTER)


# ------------------------------------------------------------ visual state

# visual_state -> (frame 0 changes, frame 1 changes) applied over the
# character grid. Two entries for the 2 FPS animation phase (design.md
# section 5); a state that does not animate repeats its frame.
STATE_PATCHES = {
    "empty": ([(3, 4, "..."), (4, 4, "..."), (5, 3, "....."), (6, 3, ".....")],
              [(3, 4, "..."), (4, 4, "..."), (5, 3, "....."), (6, 3, ".....")]),
    "unknown": ([], []),
    "idle": ([(6, 12, "K")],
             [(6, 12, "K"), (5, 12, "~")]),
    "working": ([(7, 4, "S.S")],
                [(7, 3, "S...S")]),
    # The bubble blinks off on frame 1; the hand stays up.
    "blocked": ([(1, 2, "WWW"), (2, 2, "W!W"), (3, 8, "S"), (4, 8, "B")],
                [(3, 8, "S"), (4, 8, "B")]),
    # ESCALATED (character-states.md section 1): past blocked_threshold_s the
    # neutral "!" becomes a wider alert-red "!!", in sync with the toast.
    "blocked_escalated": ([(1, 2, "WWWW"), (2, 2, "WXXW"),
                           (3, 8, "S"), (4, 8, "B")],
                          [(3, 8, "S"), (4, 8, "B")]),
    "done": ([(0, 10, ".V"), (1, 8, "V.V"), (2, 9, "V")],
             [(0, 10, ".V"), (1, 8, "V.V"), (2, 9, "V")]),
}

# visual_state -> (shirt palette key, screen palette key)
VISUAL_STYLE = {
    "empty": ("shirt_unknown", "screen_off"),
    "unknown": ("shirt_unknown", "screen_off"),
    "idle": ("shirt_idle", "screen_off"),
    "working": ("shirt_working", "screen_on"),
    "blocked": ("shirt_blocked", "screen_on"),
    "blocked_escalated": ("shirt_blocked", "screen_on"),
    "done": ("shirt_done", "screen_off"),
}

# "empty" is an unoccupied desk: no character, so no headgear either.
_UNOCCUPIED = ("empty",)

_grid_cache = {}


def grid_for(visual_state, frame, agent=None):
    """The char grid for a state + animation phase + agent, memoised."""
    character = character_for(agent)
    parity = frame % 2
    key = (character.name, visual_state, parity)
    cached = _grid_cache.get(key)
    if cached is not None:
        return cached
    grid = BASE
    if visual_state not in _UNOCCUPIED:
        grid = _patch(grid, character.patch)
    grid = _patch(grid, STATE_PATCHES[visual_state][parity])
    _grid_cache[key] = grid
    return grid


# ---------------------------------------------------------------- tier 1

def _pixel_key(ch, x, y, shirt, screen, skin, hair):
    if ch == ".":
        return "floor_a" if ((x // 4) + (y // 4)) % 2 == 0 else "floor_b"
    if ch == "B":
        return shirt
    if ch == "M":
        return screen
    if ch == "S":
        return skin
    if ch == "H":
        return hair
    return SPRITE_COLORS[ch]


def desk_pixel_rows(visual_state, frame, palette=None, agent=None):
    """Return DESK_H rows of (r,g,b) tuples for a visual state + anim frame."""
    palette = PALETTE if palette is None else palette
    shirt, screen = VISUAL_STYLE[visual_state]
    grid = grid_for(visual_state, frame, agent)
    # An unresolved status greys the whole occupant out, headgear included, so
    # "we do not know" reads the same whichever character is at the desk.
    if visual_state == "unknown":
        skin = hair = "shirt_unknown"
    else:
        skin = "skin"
        hair = character_for(agent).hair
    rows = []
    for y, line in enumerate(grid):
        rows.append([palette[_pixel_key(ch, x, y, shirt, screen, skin, hair)]
                     for x, ch in enumerate(line)])
    return rows


def _truecolor_cell(top, bottom):
    return "\x1b[38;2;%d;%d;%dm\x1b[48;2;%d;%d;%dm▀" % (top + bottom)


def rgb_to_256(r, g, b):
    # 6x6x6 color cube (indices 16..231); grays handled by the cube too.
    def c(v):
        return 0 if v < 48 else (5 if v > 230 else round((v - 35) / 40))
    return 16 + 36 * c(r) + 6 * c(g) + c(b)


def fg(rgb, truecolor=True):
    """Foreground ANSI escape for an (r,g,b), quantised when not truecolor.

    Text colours are theme values now (office/themes.py), so the 256-colour
    fallback that tier 1's pixels always had has to cover the text as well -
    a hard-coded 24-bit escape would come out as garbage on a 256-colour term.
    """
    if truecolor:
        return "\x1b[38;2;%d;%d;%dm" % tuple(rgb)
    return "\x1b[38;5;%dm" % rgb_to_256(*rgb)


def _c256_cell(top, bottom):
    return "\x1b[38;5;%dm\x1b[48;5;%dm▀" % (
        rgb_to_256(*top), rgb_to_256(*bottom))


def halfblock_lines(pixel_rows, truecolor=True):
    """Fold two pixel rows into one text row using the upper half block."""
    cell = _truecolor_cell if truecolor else _c256_cell
    out = []
    for y in range(0, len(pixel_rows), 2):
        top, bottom = pixel_rows[y], pixel_rows[y + 1]
        out.append("".join(cell(t, b) for t, b in zip(top, bottom)) + "\x1b[0m")
    return out


def desk_tier1_lines(visual_state, frame, truecolor=True, palette=None,
                     agent=None):
    return halfblock_lines(
        desk_pixel_rows(visual_state, frame, palette, agent), truecolor)


# ---------------------------------------------------------------- tier 0

# '@' is the head: the character's ASCII head glyph is substituted in, so the
# agent still reads as itself on a dumb terminal (design.md section 5: tier 0
# carries the same information, only plainer).
ASCII_ART = {
    "empty":   ["         ", "         ", " [_____] ", "         "],
    "unknown": ["    ?    ", "   (@)   ", " [_____] ", "         "],
    "idle":    ["         ", "   @   c ", "  /|\\    ", " [_____] "],
    "working": ["         ", "   @     ", "  /|\\ ## ", " [_____] "],
    "blocked": ["    !    ", "   @/    ", "  /|     ", " [_____] "],
    "blocked_escalated": ["   !!    ", "   @/    ", "  /|     ", " [_____] "],
    "done":    ["    *    ", "   @     ", "  /|\\    ", " [_____] "],
}
ASCII_W = 9
ASCII_ROWS = 4


def desk_tier0_lines(visual_state, frame, agent=None):
    head = character_for(agent).ascii_head
    art = [line.replace("@", head) for line in ASCII_ART[visual_state]]
    if visual_state.startswith("blocked") and frame % 2:
        art[0] = " " * ASCII_W          # bubble blink
    return art
