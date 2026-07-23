"""Renderer - turns an OfficeState snapshot into a full terminal frame.

design.md section 5. Two tiers here (tier 2 kitty is out of scope):
  tier 1 (default): Unicode half-block pixel art + truecolor / 256-color ANSI
  tier 0 (fallback): ASCII + box art, for TERM=dumb / non-UTF-8 / --ascii

The whole frame is rebuilt every draw (cursor-home overwrite); differential
drawing is deferred. Layout groups desks into islands (workspaces), wraps to
the terminal width, and scrolls vertically to keep the selection visible. When
the terminal is too small for even one row of desks, it drops to a compact
one-line-per-desk summary.
"""

import os

from . import sprites

RESET = "\x1b[0m"
ACCENT = "\x1b[38;2;80;220;220m"        # cyan, selection / header
DIM = "\x1b[2m"
BOLD = "\x1b[1m"

# status -> (sprite visual state, short label, color)
STATUS_VISUAL = {
    "idle": ("idle", "idle", "\x1b[38;2;98;160;234m"),
    "working": ("working", "working", "\x1b[38;2;80;200;120m"),
    "blocked": ("blocked", "blocked", "\x1b[38;2;255;165;0m"),
    "done": ("done", "done", "\x1b[38;2;189;147;249m"),
    "unknown": ("unknown", "?", "\x1b[38;2;128;128;132m"),
}

MIN_COLS, MIN_ROWS = 80, 24


def detect_caps(force_renderer=None, env=None):
    """Return (tier, truecolor). tier 0 = ASCII, tier 1 = half-block."""
    env = env if env is not None else os.environ
    truecolor = env.get("COLORTERM", "").lower() in ("truecolor", "24bit")
    if force_renderer == "ascii":
        return 0, truecolor
    if force_renderer == "unicode":
        return 1, truecolor
    term = env.get("TERM", "")
    lang = (env.get("LC_ALL") or env.get("LC_CTYPE") or env.get("LANG") or "")
    utf8 = "utf-8" in lang.lower() or "utf8" in lang.lower()
    if term == "dumb" or not utf8:
        return 0, truecolor
    return 1, truecolor


def format_name(name, template="{name}"):
    if template == "{name:last-segment}":
        name = name.rstrip("/").split("/")[-1]
    return name


def _center(text, width):
    text = text[:width]
    pad = width - len(text)
    left = pad // 2
    return " " * left + text + " " * (pad - left)


class Renderer:
    def __init__(self, tier=1, truecolor=True, name_template="{name}"):
        self.tier = tier
        self.truecolor = truecolor
        self.name_template = name_template
        if tier == 0:
            self.desk_w = sprites.ASCII_W
            self.art_rows = sprites.ASCII_ROWS
        else:
            self.desk_w = sprites.DESK_W
            self.art_rows = sprites.DESK_ROWS
        self.block_w = self.desk_w + 2          # +1 border each side
        self.block_h = self.art_rows + 4        # top + art + name + status + bottom

    # -- public ---------------------------------------------------------

    def per_row(self, cols):
        """Desks per row for the current width (used for cursor movement)."""
        return max(1, (max(20, cols) + 1) // (self.block_w + 1))

    def render(self, state, cols, rows, frame=0, muted=False, show_help=False):
        cols = max(20, cols)
        rows = max(6, rows)
        if show_help:
            body = self._help_lines(cols, rows)
        elif cols < MIN_COLS or rows < MIN_ROWS or self.block_w + 1 > cols:
            body = self._compact(state, cols, rows, frame)
        else:
            body = self._full(state, cols, rows, frame, muted)
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
        return ACCENT + BOLD + text[:cols] + RESET

    # -- full layout ----------------------------------------------------

    def _full(self, state, cols, rows, frame, muted):
        per_row = max(1, (cols + 1) // (self.block_w + 1))
        body = []
        anchors = {}                              # pane_id -> line index in body
        for wid, label, desks in state.islands():
            body.append(DIM + ("[ %s ]" % label)[:cols] + RESET)
            for start in range(0, len(desks), per_row):
                chunk = desks[start:start + per_row]
                block_lines = [self._desk_block(d, state, frame) for d in chunk]
                for d in chunk:
                    anchors[d.pane_id] = len(body)
                for line_idx in range(self.block_h):
                    body.append(" ".join(bl[line_idx] for bl in block_lines))
            body.append("")
        return self._scroll(body, anchors, state.selected_pane_id,
                            state, cols, rows, muted)

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
            header = header + ACCENT + hint + RESET
        return [header] + window

    def _desk_block(self, desk, state, frame):
        selected = desk.pane_id == state.selected_pane_id
        focused = desk.pane_id == state.focused_pane_id
        visual, label, color = STATUS_VISUAL.get(desk.status,
                                                 STATUS_VISUAL["unknown"])
        phase = frame + (hash(desk.pane_id) & 1)  # desync animation phase
        if self.tier == 0:
            art = sprites.desk_tier0_lines(visual, phase)
        else:
            pal = sprites.PALETTE
            if focused:
                pal = dict(pal)
                pal["floor_a"] = (70, 70, 92)
                pal["floor_b"] = (62, 62, 82)
            art = sprites.desk_tier1_lines(visual, phase, self.truecolor, pal)

        side = ACCENT + "│" + RESET if selected and self.tier else (
            "|" if selected else " ")
        if selected:
            hbar = ("+" + "-" * self.desk_w + "+") if self.tier == 0 else (
                ACCENT + "┌" + "─" * self.desk_w + "┐" + RESET)
            bbar = ("+" + "-" * self.desk_w + "+") if self.tier == 0 else (
                ACCENT + "└" + "─" * self.desk_w + "┘" + RESET)
        else:
            hbar = " " * self.block_w
            bbar = " " * self.block_w

        name = format_name(desk.display_name, self.name_template)
        plate = (ACCENT if selected else BOLD) + _center(name, self.desk_w) + RESET
        stat_txt = label
        word = desk.state_label_word
        if desk.status == "blocked" and word:
            stat_txt = ("! " + word)
        stat = color + _center(stat_txt, self.desk_w) + RESET

        lines = [hbar]
        for row in art:
            lines.append(side + row + side)
        lines.append(side + plate + side)
        lines.append(side + stat + side)
        lines.append(bbar)
        return lines

    # -- compact fallback ----------------------------------------------

    def _compact(self, state, cols, rows, frame):
        body = []
        anchors = {}
        order = state.ordered_desks()
        for desk in order:
            anchors[desk.pane_id] = len(body)
            visual, label, color = STATUS_VISUAL.get(
                desk.status, STATUS_VISUAL["unknown"])
            sel = ACCENT + ">" + RESET if desk.pane_id == state.selected_pane_id else " "
            foc = "*" if desk.pane_id == state.focused_pane_id else " "
            dot = color + "●" + RESET if self.tier else color + "*" + RESET
            name = format_name(desk.display_name, self.name_template)
            room = state.rooms.get(desk.workspace_id, desk.workspace_id)
            text = "%s %s %s %-10s %s/%s" % (sel, dot, foc,
                                             label[:10], room[:14], name)
            body.append(text[:cols + 40])         # allow ANSI overhead
        header = ACCENT + BOLD + ("AGENT OFFICE (compact)  %d desks  %d blocked"
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
            ("s", "toggle mute (escalation - Stage 2 item 2)"),
            ("?", "toggle this help"),
            ("q", "close the office pane"),
        ]
        lines = [ACCENT + BOLD + "AGENT OFFICE - keys" + RESET, ""]
        for key, desc in keys:
            lines.append("  " + BOLD + ("%-16s" % key) + RESET + desc)
        lines.append("")
        lines.append(DIM + "press ? to return" + RESET)
        return lines
