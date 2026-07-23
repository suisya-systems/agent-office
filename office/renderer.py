"""Renderer - turns an OfficeState snapshot into a full terminal frame.

design.md section 5. Three tiers, all fed by the same OfficeState snapshot and
all producing the same information:
  tier 2 (opt-in):  tier 1's text frame plus a real pixel overlay pushed
                    through pane.graphics.set (see office/graphics.py)
  tier 1 (default): Unicode half-block pixel art + truecolor / 256-color ANSI
  tier 0 (fallback): ASCII + box art, for TERM=dumb / non-UTF-8 / --ascii

**Where the tier branch lives.** How one desk's sprite is drawn is the only
thing that actually differs between tiers, so that - and nothing else - is
behind the small `_DeskArt` strategies below (the interface section 5 said to
introduce once there was a second implementation to justify it). Layout,
scrolling, nameplates, the compact fallback and the help overlay are written
once and are tier-agnostic.

Tier 2 is deliberately *additive*: it draws the complete tier 1 frame and then
covers the sprite rectangles with an image. If the overlay never lands - an
outer terminal without kitty graphics support, which herdr cannot tell us about
- the user is left looking at a working tier 1 office rather than a blank one.
The overlay itself is built at animation phase 0 and is static, because 0.7.4
has no pane.graphics.stream to animate it with (design.md risk 6).

The whole frame is rebuilt every draw (cursor-home overwrite); differential
drawing is deferred. Layout groups desks into islands (workspaces), wraps to
the terminal width, and scrolls vertically to keep the selection visible. When
the terminal is too small for even one row of desks, it drops to a compact
one-line-per-desk summary.
"""

import os

from . import sprites, themes

RESET = "\x1b[0m"
DIM = "\x1b[2m"
BOLD = "\x1b[1m"

# status -> (sprite visual state, short label, theme ui colour key)
STATUS_VISUAL = {
    "idle": ("idle", "idle", "idle"),
    "working": ("working", "working", "working"),
    "blocked": ("blocked", "blocked", "blocked"),
    "done": ("done", "done", "done"),
    "unknown": ("unknown", "?", "unknown"),
}

MIN_COLS, MIN_ROWS = 80, 24

TIER_ASCII, TIER_UNICODE, TIER_KITTY = 0, 1, 2


def detect_caps(force_renderer=None, env=None):
    """Return (tier, truecolor). 0 = ASCII, 1 = half-block, 2 = kitty.

    Tier 2 is only ever reached by asking for it (`renderer = "kitty"`): it is
    an experimental herdr feature behind a config flag *and* needs a capable
    outer terminal, so auto-detection never selects it (design.md section 5).
    Whether it then actually works is a question for the server, not the
    environment - office.run() probes pane.graphics.info and drops back to
    tier 1 with a warning if the answer is no.
    """
    env = env if env is not None else os.environ
    truecolor = env.get("COLORTERM", "").lower() in ("truecolor", "24bit")
    if force_renderer == "ascii":
        return TIER_ASCII, truecolor
    if force_renderer == "unicode":
        return TIER_UNICODE, truecolor
    if force_renderer == "kitty":
        return TIER_KITTY, truecolor
    term = env.get("TERM", "")
    lang = (env.get("LC_ALL") or env.get("LC_CTYPE") or env.get("LANG") or "")
    utf8 = "utf-8" in lang.lower() or "utf8" in lang.lower()
    if term == "dumb" or not utf8:
        return TIER_ASCII, truecolor
    return TIER_UNICODE, truecolor


def format_name(name, template="{name}"):
    if template == "{name:last-segment}":
        name = name.rstrip("/").split("/")[-1]
    return name


def _center(text, width):
    text = text[:width]
    pad = width - len(text)
    left = pad // 2
    return " " * left + text + " " * (pad - left)


# ------------------------------------------------------- desk art strategies

class _AsciiArt:
    """tier 0: stick figures, no colour of its own beyond the status word."""

    desk_w = sprites.ASCII_W
    art_rows = sprites.ASCII_ROWS
    graphics = False

    def __init__(self, theme, truecolor):
        self.theme = theme
        self.truecolor = truecolor

    def lines(self, visual, phase, agent, focused):
        return sprites.desk_tier0_lines(visual, phase, agent)

    def selected_frame(self, desk_w):
        bar = "+" + "-" * desk_w + "+"
        return bar, bar, "|"


class _HalfBlockArt:
    """tier 1: two pixel rows per text row, painted with the theme palette."""

    desk_w = sprites.DESK_W
    art_rows = sprites.DESK_ROWS
    graphics = False

    def __init__(self, theme, truecolor):
        self.theme = theme
        self.truecolor = truecolor
        self._focused_palette = dict(theme.palette)
        # The focused desk gets a lit floor. Each theme names those two
        # colours itself rather than the renderer brightening the floor by a
        # fixed amount, which would blow out a light theme and barely show on
        # a dark one.
        self._focused_palette["floor_a"] = theme.palette["floor_focus_a"]
        self._focused_palette["floor_b"] = theme.palette["floor_focus_b"]

    def palette(self, focused):
        return self._focused_palette if focused else self.theme.palette

    def lines(self, visual, phase, agent, focused):
        return sprites.desk_tier1_lines(visual, phase, self.truecolor,
                                        self.palette(focused), agent)

    def pixels(self, visual, phase, agent, focused):
        return sprites.desk_pixel_rows(visual, phase, self.palette(focused),
                                       agent)

    def selected_frame(self, desk_w):
        return ("┌" + "─" * desk_w + "┐",
                "└" + "─" * desk_w + "┘",
                "│")


class _KittyArt(_HalfBlockArt):
    """tier 2: identical text output, plus pixels for the graphics overlay."""

    graphics = True


_ART_BY_TIER = {
    TIER_ASCII: _AsciiArt,
    TIER_UNICODE: _HalfBlockArt,
    TIER_KITTY: _KittyArt,
}


class _Look:
    """What one desk looks like this frame, decided once and reused.

    The text block and the graphics overlay have to agree about the visual
    state - an escalated desk showing "!!" in text and a plain "!" in the
    image would be worse than having no overlay at all - so the decision is
    made here and both consume it.
    """

    __slots__ = ("visual", "phase", "label", "color", "escalated",
                 "selected", "focused")

    def __init__(self, visual, phase, label, color, escalated, selected,
                 focused):
        self.visual = visual
        self.phase = phase
        self.label = label
        self.color = color
        self.escalated = escalated
        self.selected = selected
        self.focused = focused


class Renderer:
    def __init__(self, tier=TIER_UNICODE, truecolor=True,
                 name_template="{name}", theme=themes.DEFAULT_NAME):
        self.tier = tier
        self.truecolor = truecolor
        self.name_template = name_template
        self.theme = theme if isinstance(theme, themes.Theme) else themes.get(theme)
        self.art = _ART_BY_TIER.get(tier, _HalfBlockArt)(self.theme, truecolor)
        self.desk_w = self.art.desk_w
        self.art_rows = self.art.art_rows
        self.block_w = self.desk_w + 2          # +1 border each side
        self.block_h = self.art_rows + 4        # top + art + name + status + bottom
        ui = self.theme.ui
        self.accent = sprites.fg(ui["accent"], truecolor)
        self.alert = sprites.fg(ui["alert"], truecolor)
        self._status_color = {key: sprites.fg(ui[key], truecolor)
                              for key in ("idle", "working", "blocked", "done",
                                          "unknown")}
        # Sprite rectangles of the most recent render, in absolute frame cells:
        # (row, col, pixel_rows). Empty for every tier but 2, and for the
        # compact and help views, which have no sprites to overlay. The
        # graphics layer reads this straight after render(); it is output, not
        # state the renderer itself consults.
        self.sprite_boxes = []

    # -- public ---------------------------------------------------------

    def per_row(self, cols):
        """Desks per row for the current width (used for cursor movement)."""
        return max(1, (max(20, cols) + 1) // (self.block_w + 1))

    def render(self, state, cols, rows, frame=0, muted=False, show_help=False,
               escalated=(), status=""):
        cols = max(20, cols)
        rows = max(6, rows)
        escalated = frozenset(escalated)
        self.sprite_boxes = []
        # A status line (config warnings, toast delivery hint, last error)
        # takes the bottom row when there is something to say.
        inner = max(3, rows - 1) if status else rows
        if show_help:
            body = self._help_lines(cols, inner)
        elif cols < MIN_COLS or rows < MIN_ROWS or self.block_w + 1 > cols:
            body = self._compact(state, cols, inner, frame, escalated)
        else:
            body = self._full(state, cols, inner, frame, muted, escalated)
        if status:
            body = list(body[:inner])
            body += [""] * (inner - len(body))
            body.append(DIM + status[:cols] + RESET)
        return self._paint(body, rows)

    # -- frame assembly -------------------------------------------------

    def _paint(self, lines, rows):
        out = ["\x1b[H"]                          # cursor home
        for i in range(rows):
            out.append("\x1b[K")                  # clear line
            if i < len(lines):
                out.append(lines[i])
            if i < rows - 1:
                out.append("\r\n")
        out.append("\x1b[J")                      # clear below
        return "".join(out)

    def _header(self, state, cols, muted):
        n = len(state.desks)
        blocked = len(state.blocked_desks())
        bits = ["AGENT OFFICE", "filter:%s" % state.filter_mode,
                "%d desk%s" % (n, "" if n == 1 else "s")]
        if blocked:
            bits.append("%d blocked" % blocked)
        if muted:
            bits.append("muted")
        text = "  ".join(bits)
        return self.accent + BOLD + text[:cols] + RESET

    # -- full layout ----------------------------------------------------

    def _look(self, desk, state, frame, escalated):
        visual, label, color_key = STATUS_VISUAL.get(desk.status,
                                                     STATUS_VISUAL["unknown"])
        color = self._status_color[color_key]
        is_escalated = desk.status == "blocked" and desk.pane_id in escalated
        if is_escalated:
            visual = "blocked_escalated"
            color = self.alert
        return _Look(visual=visual,
                     phase=frame + (hash(desk.pane_id) & 1),  # desync the anim
                     label=label,
                     color=color,
                     escalated=is_escalated,
                     selected=desk.pane_id == state.selected_pane_id,
                     focused=desk.pane_id == state.focused_pane_id)

    def _full(self, state, cols, rows, frame, muted, escalated=frozenset()):
        per_row = max(1, (cols + 1) // (self.block_w + 1))
        body = []
        anchors = {}                              # pane_id -> line index in body
        boxes = []                                # (body_row, col, pixel_rows)
        for wid, label, desks in state.islands():
            room = format_name(label, self.name_template)
            body.append(DIM + ("[ %s ]" % room)[:cols] + RESET)
            for start in range(0, len(desks), per_row):
                chunk = desks[start:start + per_row]
                block_lines = []
                for column, desk in enumerate(chunk):
                    look = self._look(desk, state, frame, escalated)
                    block_lines.append(self._desk_block(desk, look))
                    anchors[desk.pane_id] = len(body)
                    if self.art.graphics:
                        # +1: the block's first line is its top border, the
                        # art starts on the next one. +1 on the column for the
                        # left border character. Pixels are not built here -
                        # the box is a cheap scalar description, so the caller
                        # can tell "nothing changed" without painting anything.
                        boxes.append((len(body) + 1,
                                      column * (self.block_w + 1) + 1,
                                      look.visual, desk.agent, look.focused))
                for line_idx in range(self.block_h):
                    body.append(" ".join(bl[line_idx] for bl in block_lines))
            body.append("")
        window, offset = self._scroll(body, anchors, state.selected_pane_id,
                                      state, cols, rows, muted)
        if self.art.graphics:
            self._place_boxes(boxes, offset, len(window))
        return window

    def _place_boxes(self, boxes, offset, window_len):
        """Move sprite rectangles into absolute frame cells, dropping clipped.

        The header occupies row 0 and the body is scrolled by `offset`, so a
        box sits at `1 + body_row - offset`. A box only partly on screen is
        dropped rather than cropped: a half-drawn character reads as a glitch,
        and the text art underneath is still there to show it properly.

        The window's last usable line is `window_len - 1`, so a box is fully on
        screen only while `row + art_rows <= window_len`. Being one out here
        puts the bottom row of an image past the end of the frame - over the
        status line, or off the pane entirely.
        """
        for body_row, col, visual, agent, focused in boxes:
            row = 1 + body_row - offset
            if row < 1 or row + self.art_rows > window_len:
                continue
            self.sprite_boxes.append((row, col, visual, agent, focused))

    def _scroll(self, body, anchors, selected, state, cols, rows, muted):
        avail = rows - 1                          # header takes 1 line
        offset = 0
        if selected in anchors and len(body) > avail:
            sel = anchors[selected]
            if sel >= avail - self.block_h:
                offset = min(len(body) - avail, sel - (avail - self.block_h) + 1)
            offset = max(0, offset)
        window = body[offset:offset + avail]
        header = self._header(state, cols, muted)
        if len(body) > avail:
            hint = "  (scroll: %d-%d of %d)" % (offset + 1,
                                                min(offset + avail, len(body)),
                                                len(body))
            header = header + self.accent + hint + RESET
        return [header] + window, offset

    def _desk_block(self, desk, look):
        art = self.art.lines(look.visual, look.phase, desk.agent, look.focused)
        if look.selected:
            hbar, bbar, edge = self.art.selected_frame(self.desk_w)
            if self.tier:
                hbar = self.accent + hbar + RESET
                bbar = self.accent + bbar + RESET
                side = self.accent + edge + RESET
            else:
                side = edge
        else:
            hbar = bbar = " " * self.block_w
            side = " "

        name = format_name(desk.display_name, self.name_template)
        plate = (self.accent if look.selected else BOLD) + _center(
            name, self.desk_w) + RESET
        stat_txt = look.label
        word = desk.state_label_word
        if desk.status == "blocked":
            mark = "!!" if look.escalated else "!"
            stat_txt = ("%s %s" % (mark, word)) if word else ("%s %s"
                                                              % (mark,
                                                                 look.label))
        stat = look.color + _center(stat_txt, self.desk_w) + RESET

        lines = [hbar]
        for row in art:
            lines.append(side + row + side)
        lines.append(side + plate + side)
        lines.append(side + stat + side)
        lines.append(bbar)
        return lines

    # -- compact fallback ----------------------------------------------

    def _compact(self, state, cols, rows, frame, escalated=frozenset()):
        body = []
        anchors = {}
        order = state.ordered_desks()
        for desk in order:
            anchors[desk.pane_id] = len(body)
            visual, label, color_key = STATUS_VISUAL.get(
                desk.status, STATUS_VISUAL["unknown"])
            color = self._status_color[color_key]
            if desk.status == "blocked" and desk.pane_id in escalated:
                label, color = "blocked!!", self.alert
            sel = (self.accent + ">" + RESET
                   if desk.pane_id == state.selected_pane_id else " ")
            foc = "*" if desk.pane_id == state.focused_pane_id else " "
            dot = color + ("●" if self.tier else "*") + RESET
            name = format_name(desk.display_name, self.name_template)
            room = format_name(state.room_label(desk.workspace_id),
                               self.name_template)
            text = "%s %s %s %-10s %s/%s" % (sel, dot, foc,
                                             label[:10], room[:14], name)
            body.append(text[:cols + 40])         # allow ANSI overhead
        header = self.accent + BOLD + (
            "AGENT OFFICE (compact)  %d desks  %d blocked"
            % (len(order), len(state.blocked_desks())))[:cols] + RESET
        avail = rows - 1
        offset = 0
        sel_idx = anchors.get(state.selected_pane_id)
        if sel_idx is not None and len(body) > avail and sel_idx >= avail:
            offset = min(len(body) - avail, sel_idx - avail + 1)
        return [header] + body[offset:offset + avail]

    # -- help -----------------------------------------------------------

    def _help_lines(self, cols, rows):
        keys = [
            ("arrows / hjkl", "move the desk cursor"),
            ("Enter", "focus the selected agent's pane (jump)"),
            ("b", "jump to the longest-blocked agent"),
            ("Tab", "cycle through blocked agents"),
            ("a", "toggle filter (agents / all)"),
            ("s", "toggle escalation mute (no toasts while muted)"),
            ("?", "toggle this help"),
            ("q", "close the office pane"),
        ]
        lines = [self.accent + BOLD + "AGENT OFFICE - keys" + RESET, ""]
        for key, desc in keys:
            lines.append("  " + BOLD + ("%-16s" % key) + RESET + desc)
        lines.append("")
        lines.append(DIM + "press ? to return" + RESET)
        return lines
