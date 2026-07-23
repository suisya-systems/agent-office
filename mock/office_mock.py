#!/usr/bin/env python3
"""Agent Office - static rendering mock (Stage 1).

Renders one desk per character state as terminal pixel art (Unicode
half-blocks + truecolor ANSI), with an ASCII fallback. No herdr
connection; sprites are static (two animation frames selectable).

Usage:
  python3 office_mock.py             # tier 1: half-block pixel art
  python3 office_mock.py --ascii     # tier 0: ASCII fallback
  python3 office_mock.py --frame 1   # second animation frame
"""

import argparse
import shutil
import sys

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
}

# Sprite grids: 16 columns x 12 rows, one char per pixel.
# '.' = transparent (floor checker), letters index SPRITE_COLORS below.
SPRITE_COLORS = {
    "S": "skin",
    "B": "shirt",       # resolved per state
    "M": "screen",      # resolved per state (on/off)
    "D": "desk",
    "d": "desk_dark",
    "W": "bubble",
    "!": "bubble_text",   # neutral '!' in the BLOCKED bubble
    "X": "alert",         # red '!!' in the ESCALATED bubble
    "V": "check",
    "K": "coffee",
    "~": "steam",
}

DESK_W, DESK_H = 16, 12

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


def patch(grid, changes):
    g = list(grid)
    for row, col, text in changes:
        g[row] = g[row][:col] + text + g[row][col + len(text):]
    return g


EMPTY = patch(BASE, [
    (3, 4, "..."), (4, 4, "..."),
    (5, 3, "....."), (6, 3, "....."),
])

UNKNOWN = list(BASE)  # gray shirt/screen resolved at render time

IDLE = patch(BASE, [
    (6, 12, "K"),       # coffee cup by the monitor
])
IDLE_F1 = patch(IDLE, [
    (5, 12, "~"),       # steam puff
])

WORKING = patch(BASE, [
    (7, 4, "S.S"),      # hands down on the keyboard row
])
WORKING_F1 = patch(BASE, [
    (7, 3, "S...S"),    # hands shifted: typing
])

BLOCKED = patch(BASE, [
    (1, 2, "WWW"),
    (2, 2, "W!W"),      # single neutral '!'
    (3, 8, "S"),        # raised hand
    (4, 8, "B"),        # arm
])
BLOCKED_F1 = patch(BASE, [   # bubble blinks off; hand stays raised
    (3, 8, "S"),
    (4, 8, "B"),
])

ESCALATED = patch(BASE, [
    (0, 1, "WWWW"),
    (1, 1, "WXXW"),     # red '!!'
    (2, 1, "WWWW"),
    (3, 8, "S"),
    (4, 8, "B"),
])

DONE = patch(BASE, [
    (0, 10, ".V"),
    (1, 8, "V.V"),
    (2, 9, "V"),
])

STATES = [
    # (label, status text, grid frame0, grid frame1, shirt key, screen key)
    ("empty", "-", EMPTY, EMPTY, "shirt_unknown", "screen_off"),
    ("unknown", "?", UNKNOWN, UNKNOWN, "shirt_unknown", "screen_off"),
    ("idle", "idle", IDLE, IDLE_F1, "shirt_idle", "screen_off"),
    ("working", "working", WORKING, WORKING_F1, "shirt_working", "screen_on"),
    ("blocked", "BLOCKED", BLOCKED, BLOCKED_F1, "shirt_blocked", "screen_on"),
    ("blocked!!", "ESCALATED", ESCALATED, BLOCKED, "shirt_blocked", "screen_on"),
    ("done", "done", DONE, DONE, "shirt_done", "screen_off"),
]

NAMES = ["(vacant)", "shell", "reviewer", "builder", "worker-a2", "worker-a7", "scout"]

# ---------------------------------------------------------------- tier 1


def pixel_color(ch, x, y, shirt, screen, skin):
    if ch == ".":
        key = "floor_a" if ((x // 4) + (y // 4)) % 2 == 0 else "floor_b"
    elif ch == "B":
        key = shirt
    elif ch == "M":
        key = screen
    elif ch == "S":
        key = skin
    else:
        key = SPRITE_COLORS[ch]
    return PALETTE[key]


def render_desk_pixels(grid, shirt, screen, skin="skin"):
    """Return DESK_H rows of DESK_W (r,g,b) tuples."""
    rows = []
    for y, line in enumerate(grid):
        rows.append([pixel_color(ch, x, y, shirt, screen, skin)
                     for x, ch in enumerate(line)])
    return rows


def halfblock_lines(pixel_rows):
    """Two pixel rows -> one text row using the upper half block."""
    out = []
    for y in range(0, len(pixel_rows), 2):
        top, bottom = pixel_rows[y], pixel_rows[y + 1]
        cells = []
        for t, b in zip(top, bottom):
            cells.append("\x1b[38;2;%d;%d;%dm\x1b[48;2;%d;%d;%dm▀" % (t + b))
        out.append("".join(cells) + "\x1b[0m")
    return out


def render_tier1(frame):
    desks = []
    for (label, status, g0, g1, shirt, screen), name in zip(STATES, NAMES):
        grid = g1 if frame % 2 else g0
        skin = "shirt_unknown" if label == "unknown" else "skin"  # gray silhouette
        art = halfblock_lines(render_desk_pixels(grid, shirt, screen, skin))
        plate = ("%-10s" % name[:10]).center(DESK_W)[:DESK_W]
        stat = status.center(DESK_W)[:DESK_W]
        desks.append(art + ["\x1b[1m" + plate + "\x1b[0m", "\x1b[2m" + stat + "\x1b[0m"])
    return desks


# ---------------------------------------------------------------- tier 0

ASCII_ART = {
    "empty":     ["         ", "         ", " [_____] ", "         "],
    "unknown":   ["    ?    ", "   (o)   ", " [_____] ", "         "],
    "idle":      ["         ", "   o   c ", "  /|\\    ", " [_____] "],
    "working":   ["         ", "   o     ", "  /|\\ ## ", " [_____] "],
    "blocked":   ["    !    ", "   o/    ", "  /|     ", " [_____] "],
    "blocked!!": ["   !!    ", "   o/    ", "  /|     ", " [_____] "],
    "done":      ["    *    ", "   o     ", "  /|\\    ", " [_____] "],
}
ASCII_W = 9


def render_tier0(frame):
    desks = []
    for (label, status, _g0, _g1, _shirt, _screen), name in zip(STATES, NAMES):
        art = list(ASCII_ART[label])
        if label == "blocked" and frame % 2:
            art[0] = "         "  # blink
        desks.append(art + [name[:ASCII_W].center(ASCII_W),
                            status[:ASCII_W].center(ASCII_W)])
    return desks


# ---------------------------------------------------------------- layout


def print_office(desks, desk_w, gap=2):
    cols = shutil.get_terminal_size((100, 24)).columns
    per_row = max(1, (cols + gap) // (desk_w + gap))
    print("AGENT OFFICE (mock)  -  states: " +
          ", ".join(s[0] for s in STATES))
    print()
    for start in range(0, len(desks), per_row):
        chunk = desks[start:start + per_row]
        for line_idx in range(len(chunk[0])):
            print((" " * gap).join(d[line_idx] for d in chunk))
        print()


def main():
    ap = argparse.ArgumentParser(
        description="Agent Office static rendering mock (no herdr required)")
    ap.add_argument("--ascii", action="store_true",
                    help="force tier 0 ASCII rendering")
    ap.add_argument("--frame", type=int, default=0,
                    help="animation frame index (0 or 1)")
    args = ap.parse_args()

    use_ascii = args.ascii
    if not use_ascii:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        try:
            "▀".encode(enc)
        except (UnicodeEncodeError, LookupError):
            print("[notice] terminal encoding %r cannot show half-blocks; "
                  "falling back to --ascii" % enc)
            use_ascii = True

    if use_ascii:
        print_office(render_tier0(args.frame), ASCII_W)
    else:
        print_office(render_tier1(args.frame), DESK_W)


if __name__ == "__main__":
    main()
