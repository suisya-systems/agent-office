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

**Where the desks sit is a value, not a side effect** (issue #27). `layout()`
answers "which desk is drawn where" as a pure function of the state snapshot
and the pane size (see office/layout.py); `_full` and `_compact` draw *from*
that answer, and the input path asks the same question at key time to move the
cursor. Neither view mode is special-cased outside this module: compact is a
layout one desk wide.

**Widths are display columns, never character counts** (issue #25). Every line
here is composed the same way: measure and cut the *visible* text with
`office.textwidth`, then colour what survived - see `_fit`. Doing it the other
way round is what used to let a Japanese nameplate draw twice as wide as it
counted, and what made the compact row guess at ANSI overhead.
"""

import codecs
import os
import sys

from . import sprites, textwidth, themes
from .layout import MODE_COMPACT, build_compact, build_full

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

# The smallest frame that is composed at all - a *floor*, not a threshold, and
# therefore far below MIN_COLS / MIN_ROWS above rather than near them. Below
# these the frame is not made to fit: it is composed at 20x6 and left to the
# terminal, because nothing legible survives a pane that small and a floor
# keeps every division below out of the degenerate cases. Panes between the two
# pairs get the compact view. Named because two callers now depend on the exact
# value: render() composes at it, and `layout()` has to answer for the same
# frame render() would draw, clamp included (see Renderer._clamp).
MIN_FRAME_COLS, MIN_FRAME_ROWS = 20, 6

# Bottom-row key hint shown during normal operation when the status line has no
# real message to carry (Issue #17). Kept minimal on purpose - the full key map
# stays in the ? overlay. ASCII only: the separator has to survive a cp932
# console, where Screen's replace-fallback would otherwise punch a "?" through a
# fancier glyph. KEY_HINT_SHORT is the graceful-degrade form for narrow panes -
# it still surfaces the one key (?) that opens everything else, and it is what
# the header carries (issue #34): the bottom row only shows a hint while it has
# no message, and a connect notice arrives on it early and stays, so in normal
# operation the ? overlay was discoverable from the header or not at all.
KEY_HINT = "? help | Enter jump | b blocked | q quit"
KEY_HINT_SHORT = "? help"

TIER_ASCII, TIER_UNICODE, TIER_KITTY = 0, 1, 2

# Nameplate rows per desk (issue #25). 1 is the historical layout. 2 doubles
# what a Japanese title can say - a 16-column plate holds only eight full-width
# characters - at the cost of one row per desk, so fewer desks fit vertically.
# The cap lives here rather than in config.py because it is a layout fact, and
# config.py imports it so the validated range and the drawn range cannot drift.
PLATE_LINES_MIN, PLATE_LINES_MAX = 1, 2


def is_utf8_encoding(name):
    """True / False for a known codec, None when there is nothing to go on."""
    if not name:
        return None
    try:
        return codecs.lookup(name).name == "utf-8"
    except LookupError:
        return None


def detect_caps(force_renderer=None, env=None, stream=None):
    """Return (tier, truecolor). 0 = ASCII, 1 = half-block, 2 = kitty.

    Tier 2 is only ever reached by asking for it (`renderer = "kitty"`): it is
    an experimental herdr feature behind a config flag *and* needs a capable
    outer terminal, so auto-detection never selects it (design.md section 5).
    Whether it then actually works is a question for the server, not the
    environment - office.run() probes pane.graphics.info and drops back to
    tier 1 with a warning if the answer is no.

    Two things are asked, in this order. *Can* stdout carry the frame - a
    cp932 console cannot encode a half-block, so a demonstrably non-UTF-8
    encoder forces tier 0 even when the config asked for something richer.
    Then, does the terminal want it - the locale variables answer that on
    unix, and on Windows, which leaves them unset, stdout answers it instead.
    """
    env = env if env is not None else os.environ
    stream = stream if stream is not None else sys.stdout
    truecolor = env.get("COLORTERM", "").lower() in ("truecolor", "24bit")
    encoder_utf8 = is_utf8_encoding(getattr(stream, "encoding", None))
    if encoder_utf8 is False:
        return TIER_ASCII, truecolor
    if force_renderer == "ascii":
        return TIER_ASCII, truecolor
    if force_renderer == "unicode":
        return TIER_UNICODE, truecolor
    if force_renderer == "kitty":
        return TIER_KITTY, truecolor
    term = env.get("TERM", "")
    lang = (env.get("LC_ALL") or env.get("LC_CTYPE") or env.get("LANG") or "")
    if lang:
        utf8 = "utf-8" in lang.lower() or "utf8" in lang.lower()
    else:
        # No locale to read: trust stdout only when it says something. An
        # unknown encoding (a StringIO under test, a stream with no encoding
        # attribute) is not evidence of a UTF-8 terminal.
        utf8 = encoder_utf8 is True
    if term == "dumb" or not utf8:
        return TIER_ASCII, truecolor
    return TIER_UNICODE, truecolor


def format_name(name, template="{name}"):
    if template == "{name:last-segment}":
        name = name.rstrip("/").split("/")[-1]
    return name


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
                 name_template="{name}", theme=themes.DEFAULT_NAME,
                 plate_lines=1):
        self.tier = tier
        self.truecolor = truecolor
        self.name_template = name_template
        self.theme = theme if isinstance(theme, themes.Theme) else themes.get(theme)
        self.art = _ART_BY_TIER.get(tier, _HalfBlockArt)(self.theme, truecolor)
        self.desk_w = self.art.desk_w
        self.art_rows = self.art.art_rows
        # How many rows the nameplate gets. Clamped to the same range config.py
        # validates against, and soft on rubbish: the renderer is reached from
        # the config file, and a bad value there has never been allowed to stop
        # the office from opening (design.md section 8).
        try:
            self.plate_lines = max(PLATE_LINES_MIN,
                                   min(PLATE_LINES_MAX, int(plate_lines)))
        except (TypeError, ValueError):
            self.plate_lines = PLATE_LINES_MIN
        self.block_w = self.desk_w + 2          # +1 border each side
        # top + art + nameplate(s) + status + bottom. The nameplate is the only
        # variable part, and it has to be in here rather than assumed to be one
        # row: block_h is what row assembly indexes with and what scroll-to-
        # selection measures a desk by, so an understated height would drop the
        # second plate row and scroll to the wrong place.
        self.block_h = self.art_rows + 3 + self.plate_lines
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

    # -- composition ----------------------------------------------------

    def _fit(self, segments, budget):
        """Join `(colour, text)` segments into a line at most `budget` columns.

        The one place colour meets width. Each segment's *visible* text is cut
        against what is left of the shared budget and only then wrapped in its
        escape sequence, so no measurement ever sees an escape and no escape is
        ever cut in half. Segments are consumed left to right, which makes the
        earlier ones the ones that survive a narrow pane - deliberate: the
        status word and the room matter more than the tail of a long name.

        Once a non-empty segment has nothing left to spend, the rest are
        dropped rather than skipped over: the result is always an unbroken
        left prefix of the intended row, so a later short field can never jump
        the queue in front of a longer one that was cut.
        """
        out = []
        for color, text in segments:
            if not text:                          # nothing to say, not a stop
                continue
            if budget <= 0:
                break
            piece, used = textwidth.cut(text, budget)
            if not piece:
                break
            budget -= used
            out.append(color + piece + RESET if color else piece)
        return "".join(out)

    # -- geometry -------------------------------------------------------

    def _clamp(self, cols, rows):
        """The size the frame is actually composed at, for any pane size.

        The one place the floor is applied. `render` and `layout` have to agree
        about it exactly - a layout built for a different width than the frame
        on screen wraps its rows in different places, which is the whole class
        of bug issue #27 is about - so neither computes it itself. The "no line
        exceeds cols" guarantee therefore holds for cols >= MIN_FRAME_COLS only.
        """
        return max(MIN_FRAME_COLS, cols), max(MIN_FRAME_ROWS, rows)

    def per_row(self, cols):
        """Desks per row at width `cols`, clamped exactly as `render` clamps."""
        cols, _ = self._clamp(cols, MIN_FRAME_ROWS)
        return max(1, (cols + 1) // (self.block_w + 1))

    def is_compact(self, cols, rows):
        """True when this pane size falls back to the one-line-per-desk view."""
        cols, rows = self._clamp(cols, rows)
        return cols < MIN_COLS or rows < MIN_ROWS or self.block_w + 1 > cols

    # -- public ---------------------------------------------------------

    def layout(self, state, cols, rows):
        """Where every desk sits in the frame `render(state, cols, rows)` draws.

        Pure: it reads the state snapshot and the pane size and keeps nothing,
        which is what lets the input path call it at key time (office.py) and
        get the layout that is on screen rather than a remembered one that a
        resize, a filter change or a closed pane has since invalidated.
        """
        if self.is_compact(cols, rows):
            return build_compact(state.ordered_desks())
        return build_full(state.islands(), self.per_row(cols))

    def render(self, state, cols, rows, frame=0, muted=False, show_help=False,
               escalated=(), status="", show_hint=False):
        cols, rows = self._clamp(cols, rows)
        escalated = frozenset(escalated)
        self.sprite_boxes = []
        # A status line (config warnings, toast delivery hint, last error) takes
        # the bottom row when there is something to say; a real message wins,
        # and the key hint fills the row only when there is nothing else to say.
        bottom = self._status_line(status, show_hint, cols)
        inner = max(3, rows - 1) if bottom else rows
        if show_help:
            body = self._help_lines(cols, inner)
        else:
            # Drawn from the same layout object the cursor moves over, so the
            # two can never disagree about which desk is where (issue #27).
            desks = self.layout(state, cols, rows)
            if desks.mode == MODE_COMPACT:
                body = self._compact(state, desks, cols, inner, frame,
                                     escalated)
            else:
                body = self._full(state, desks, cols, inner, frame, muted,
                                  escalated)
        if bottom:
            body = list(body[:inner])
            body += [""] * (inner - len(body))
            body.append(DIM + bottom + RESET)
        return self._paint(body, rows)

    def _status_line(self, status, show_hint, cols):
        """The bottom row's text: a real message (truncated to width) wins;
        otherwise the key hint, but only when it fits. A hint too wide for the
        pane degrades to KEY_HINT_SHORT and then to nothing, rather than being
        cut mid-word - dropping a decorative hint beats corrupting the row."""
        if status:
            return textwidth.truncate(status, cols)
        if not show_hint:
            return ""
        if textwidth.width(KEY_HINT) <= cols:
            return KEY_HINT
        if textwidth.width(KEY_HINT_SHORT) <= cols:
            return KEY_HINT_SHORT
        return ""

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

    def _header(self, state, cols, muted, hint=""):
        """The top line: the readout, any scroll hint, then the `? help` hint.

        The hints are composed *here* rather than appended by the caller: they
        share one line and therefore one width budget, and appending after the
        header had already been cut to `cols` overran the pane by the length of
        the hint - with plain ASCII, every time the office scrolled.

        Each is also all-or-nothing, the same rule `_status_line` applies to the
        key hint. Half of "(scroll: 41-68 of 300)" is not a smaller readout,
        it is a wrong one, so a hint that does not fit is dropped entirely.

        Order is priority order, because `_fit` spends the budget left to right
        and because the tail is what a narrowing pane loses first. The readout
        (desks / blocked) is the reason the line exists; the scroll hint says
        which part of a scrolled office is on screen, which is state; `? help`
        is a permanent signpost that the reader stops needing. So the signpost
        goes last, and a pane too narrow for everything keeps the counts.
        """
        n = len(state.desks)
        blocked = len(state.blocked_desks())
        bits = ["AGENT OFFICE", "filter:%s" % state.filter_mode,
                "%d desk%s" % (n, "" if n == 1 else "s")]
        if blocked:
            bits.append("%d blocked" % blocked)
        if muted:
            bits.append("muted")
        body = "  ".join(bits)
        help_hint = "  " + KEY_HINT_SHORT
        used = textwidth.width(body)
        if hint and used + textwidth.width(hint) > cols:
            # Strictly ordered, not first-fit: the room a dropped scroll
            # position leaves behind is not room the signpost may take. A
            # header that says nothing about where the office is scrolled to
            # and still has width to spare reads as an office that is not
            # scrolled at all.
            hint = help_hint = ""
        used += textwidth.width(hint)
        if used + textwidth.width(help_hint) > cols:
            help_hint = ""
        return self._fit([(self.accent + BOLD, body),
                          (self.accent, hint),
                          (DIM, help_hint)], cols)

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

    def _full(self, state, layout, cols, rows, frame, muted,
              escalated=frozenset()):
        body = []
        anchors = {}                              # pane_id -> line index in body
        boxes = []                                # (body_row, col, pixel_rows)
        for group in layout.groups:
            room = format_name(group.label, self.name_template)
            body.append(self._fit([(DIM, "[ %s ]" % room)], cols))
            for chunk in group.rows:
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
        hint = ""
        if len(body) > avail:
            hint = "  (scroll: %d-%d of %d)" % (offset + 1,
                                                min(offset + avail, len(body)),
                                                len(body))
        return [self._header(state, cols, muted, hint)] + window, offset

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
        plate_color = self.accent if look.selected else BOLD
        # Always exactly self.plate_lines rows, blank ones included: the block
        # is assembled by index against block_h, so a short name must still
        # take up its full height or the desks below it shift up by a row.
        plate = [plate_color + textwidth.center(part, self.desk_w) + RESET
                 for part in textwidth.wrap(name, self.desk_w,
                                            self.plate_lines)]
        stat_txt = look.label
        word = desk.state_label_word
        if desk.status == "blocked":
            mark = "!!" if look.escalated else "!"
            stat_txt = ("%s %s" % (mark, word)) if word else ("%s %s"
                                                              % (mark,
                                                                 look.label))
        stat = look.color + textwidth.center(stat_txt, self.desk_w) + RESET

        lines = [hbar]
        for row in art:
            lines.append(side + row + side)
        for row in plate:
            lines.append(side + row + side)
        lines.append(side + stat + side)
        lines.append(bbar)
        return lines

    # -- compact fallback ----------------------------------------------

    def _compact(self, state, layout, cols, rows, frame,
                 escalated=frozenset()):
        body = []
        anchors = {}
        order = layout.flat
        for desk in order:
            anchors[desk.pane_id] = len(body)
            visual, label, color_key = STATUS_VISUAL.get(
                desk.status, STATUS_VISUAL["unknown"])
            color = self._status_color[color_key]
            if desk.status == "blocked" and desk.pane_id in escalated:
                label, color = "blocked!!", self.alert
            selected = desk.pane_id == state.selected_pane_id
            foc = "*" if desk.pane_id == state.focused_pane_id else " "
            name = format_name(desk.display_name, self.name_template)
            room = format_name(state.room_label(desk.workspace_id),
                               self.name_template)
            # Segments, not one formatted string: the cursor and the status dot
            # carry colour, so the row has to be measured before it is painted.
            # The fixed-width fields are cut to columns for the same reason
            # `%-10s` and `[:14]` no longer are - a CJK room label counted half
            # of what it drew and pushed the name off the pane.
            body.append(self._fit([
                (self.accent if selected else "", ">" if selected else " "),
                ("", " "),
                (color, "●" if self.tier else "*"),
                ("", " %s %s %s/%s" % (foc, textwidth.pad(label, 10),
                                       textwidth.truncate(room, 14), name)),
            ], cols))
        # Same tail, same rule as `_header`: the compact view is reached by a
        # short pane as readily as by a narrow one (rows < MIN_ROWS), so it is
        # not a view the ? signpost can be left out of - and where the pane
        # really is too narrow for both, the counts win and the hint goes.
        readout = ("AGENT OFFICE (compact)  %d desks  %d blocked"
                   % (len(order), len(state.blocked_desks())))
        help_hint = "  " + KEY_HINT_SHORT
        if textwidth.width(readout) + textwidth.width(help_hint) > cols:
            help_hint = ""
        header = self._fit([(self.accent + BOLD, readout),
                            (DIM, help_hint)], cols)
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
        lines = [self._fit([(self.accent + BOLD, "AGENT OFFICE - keys")], cols),
                 ""]
        for key, desc in keys:
            lines.append(self._fit([("", "  "),
                                    (BOLD, textwidth.pad(key, 16)),
                                    ("", desc)], cols))
        lines.append("")
        lines.append(self._fit([(DIM, "press ? to return")], cols))
        return lines
