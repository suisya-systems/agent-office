"""Smoke tests for tier detection and frame assembly (no herdr needed)."""

import re
import unittest

from office import sprites
from office import textwidth as tw
from office.renderer import (KEY_HINT, KEY_HINT_SHORT, Renderer, detect_caps,
                             format_name)
from office.state import OfficeState

ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")

# A Japanese pane title: 12 characters, 24 columns. Long enough to overflow a
# 16-column nameplate twice over, which is exactly what issue #25 reported.
JA_NAME = "変更が反映されているか確認"
JA_ROOM = "作業部屋いち"


def visible(line):
    """The line as the terminal shows it, with escape sequences removed."""
    return ANSI.sub("", line)


def _state():
    s = OfficeState()
    s.ingest_pane({"pane_id": "w1:p1", "workspace_id": "w1", "tab_id": "w1:t1",
                   "agent": "claude", "agent_status": "working"})
    s.ingest_pane({"pane_id": "w1:p2", "workspace_id": "w1", "tab_id": "w1:t1",
                   "agent": "codex", "agent_status": "blocked"})
    s.set_room_label("w1", "room-one")
    return s


class Stream:
    """A stdout stand-in that carries nothing but its encoding.

    Passed explicitly everywhere below, because detect_caps now consults
    stdout and the answer must not depend on how the CI runs the suite.
    """

    def __init__(self, encoding):
        self.encoding = encoding


UTF8 = Stream("utf-8")
CP932 = Stream("cp932")


class CapsTest(unittest.TestCase):
    def test_force_ascii(self):
        self.assertEqual(detect_caps("ascii", {}, UTF8)[0], 0)

    def test_force_unicode(self):
        self.assertEqual(detect_caps("unicode", {}, UTF8)[0], 1)

    def test_dumb_term_is_tier0(self):
        self.assertEqual(
            detect_caps(None, {"TERM": "dumb", "LANG": "C.UTF-8"}, UTF8)[0], 0)

    def test_utf8_is_tier1_truecolor(self):
        tier, tc = detect_caps(None, {"TERM": "xterm-256color",
                                      "LANG": "en_US.UTF-8",
                                      "COLORTERM": "truecolor"}, UTF8)
        self.assertEqual((tier, tc), (1, True))

    def test_non_utf8_is_tier0(self):
        self.assertEqual(
            detect_caps(None, {"TERM": "xterm", "LANG": "C"}, UTF8)[0], 0)

    def test_force_kitty_is_tier2(self):
        self.assertEqual(detect_caps("kitty", {}, UTF8)[0], 2)

    def test_auto_never_picks_kitty(self):
        """tier 2 is opt-in: experimental in herdr, and needs a capable term."""
        for env in ({"TERM": "xterm-kitty", "LANG": "en_US.UTF-8",
                     "COLORTERM": "truecolor"},
                    {"TERM": "xterm-256color", "LANG": "en_US.UTF-8"},
                    {"TERM": "dumb"}):
            self.assertNotEqual(detect_caps(None, env, UTF8)[0], 2)

    def test_a_windows_console_is_read_off_stdout(self):
        """Windows leaves the locale variables unset, so the old LANG-only
        test called every Windows console tier 0 - including UTF-8 ones."""
        self.assertEqual(detect_caps(None, {}, UTF8)[0], 1)
        self.assertEqual(detect_caps(None, {}, CP932)[0], 0)

    def test_an_encoder_that_cannot_take_the_frame_wins(self):
        """A half-block does not exist in cp932, so asking for one is not
        enough - the write would raise part-way through a frame."""
        self.assertEqual(detect_caps(None, {"LANG": "en_US.UTF-8"}, CP932)[0], 0)
        self.assertEqual(detect_caps("unicode", {}, CP932)[0], 0)
        self.assertEqual(detect_caps("kitty", {}, CP932)[0], 0)

    def test_encoding_aliases_are_normalised(self):
        for name in ("UTF8", "utf_8", "cp65001"):
            self.assertEqual(detect_caps(None, {}, Stream(name))[0], 1, name)

    def test_an_unknown_encoding_falls_back_to_the_locale(self):
        """A StringIO has no encoding; that is ignorance, not a cp932 console,
        so the locale keeps the last word and the conservative answer holds."""
        self.assertEqual(detect_caps(None, {}, Stream(None))[0], 0)
        self.assertEqual(
            detect_caps(None, {"LANG": "en_US.UTF-8"}, Stream(None))[0], 1)


class FormatNameTest(unittest.TestCase):
    def test_last_segment(self):
        self.assertEqual(
            format_name("claude-org/run/g7/project:x/a2", "{name:last-segment}"),
            "a2")

    def test_default(self):
        self.assertEqual(format_name("foo/bar"), "foo/bar")


class RenderSmokeTest(unittest.TestCase):
    def test_tier1_full_frame(self):
        r = Renderer(tier=1, truecolor=True)
        frame = r.render(_state(), 120, 40, frame=0)
        self.assertIn("AGENT OFFICE", frame)
        self.assertTrue(frame.startswith("\x1b[H"))

    def test_tier0_full_frame(self):
        r = Renderer(tier=0, truecolor=False)
        frame = r.render(_state(), 120, 40, frame=1)
        self.assertIn("AGENT OFFICE", frame)

    def test_compact_when_small(self):
        r = Renderer(tier=1, truecolor=True)
        frame = r.render(_state(), 40, 12)
        self.assertIn("compact", frame)

    def test_help_overlay(self):
        r = Renderer(tier=1, truecolor=True)
        frame = r.render(_state(), 120, 40, show_help=True)
        self.assertIn("keys", frame)

    def test_render_empty_state(self):
        r = Renderer(tier=1, truecolor=True)
        frame = r.render(OfficeState(), 120, 40)
        self.assertIn("0 desks", frame)

    def test_status_line_is_appended(self):
        r = Renderer(tier=1, truecolor=True)
        frame = r.render(_state(), 120, 40, status="config broke")
        self.assertIn("config broke", frame)
        self.assertEqual(frame.count("\r\n"), 39)     # still exactly `rows`

    def test_key_hint_shown_when_idle(self):
        # Normal operation, nothing to say: the hint fills the status row so the
        # ? overlay is discoverable, on every tier.
        for tier in (0, 1, 2):
            frame = Renderer(tier=tier, truecolor=(tier != 0)).render(
                _state(), 120, 40, show_hint=True)
            self.assertIn(KEY_HINT, frame)
            self.assertEqual(frame.count("\r\n"), 39)  # still exactly `rows`

    def test_key_hint_only_ascii(self):
        # cp932 consoles must render the hint intact; ASCII-only guarantees it.
        KEY_HINT.encode("cp932")
        KEY_HINT.encode("ascii")

    def test_real_message_wins_over_hint(self):
        # A real status message takes the row; the hint returns when it clears.
        r = Renderer(tier=1, truecolor=True)
        frame = r.render(_state(), 120, 40, status="config broke", show_hint=True)
        self.assertIn("config broke", frame)
        self.assertNotIn(KEY_HINT, frame)
        back = r.render(_state(), 120, 40, status="", show_hint=True)
        self.assertIn(KEY_HINT, back)

    def test_hint_not_shown_when_absent(self):
        # No message and hint disabled (e.g. help overlay open): no bottom row.
        r = Renderer(tier=1, truecolor=True)
        self.assertNotIn(KEY_HINT, r.render(_state(), 120, 40, show_hint=False))

    def test_hint_degrades_on_narrow_pane(self):
        r = Renderer(tier=1, truecolor=True)
        # Too narrow for the full hint but room for the short form.
        mid = r.render(_state(), len(KEY_HINT_SHORT) + 2, 30, show_hint=True)
        self.assertNotIn(KEY_HINT, mid)
        self.assertIn(KEY_HINT_SHORT, visible(mid))
        # No wrapping or row corruption: still exactly `rows` lines.
        self.assertEqual(mid.count("\r\n"), 29)

    def test_hint_dropped_when_too_narrow(self):
        # Narrower than even the short form: drop the hint rather than cut it.
        # render() clamps width to >= 20 (wider than the short form), so this
        # last-ditch branch is checked on the resolver directly.
        r = Renderer(tier=1, truecolor=True)
        self.assertEqual(
            r._status_line("", True, len(KEY_HINT_SHORT) - 1), "")
        self.assertEqual(
            r._status_line("", True, len(KEY_HINT_SHORT)), KEY_HINT_SHORT)

    def test_name_template_shortens_room_labels(self):
        s = OfficeState()
        s.ingest_pane({"pane_id": "p1", "workspace_id": "w1",
                       "agent": "claude", "agent_status": "idle"})
        s.set_room_label("w1", "claude-org/run/g7/a2")
        plain = Renderer(tier=0, truecolor=False).render(s, 120, 40)
        short = Renderer(tier=0, truecolor=False,
                         name_template="{name:last-segment}").render(s, 120, 40)
        self.assertIn("[ claude-org/run/g7/a2 ]", plain)
        self.assertIn("[ a2 ]", short)


def _ja_state(count=2, room=JA_ROOM):
    """A fleet whose names and room label are full-width."""
    s = OfficeState()
    for i in range(count):
        s.ingest_pane({"pane_id": "w1:p%d" % i, "workspace_id": "w1",
                       "tab_id": "w1:t1", "agent": "claude",
                       "display_agent": "%s%d" % (JA_NAME, i),
                       "agent_status": "working"})
    s.set_room_label("w1", room)
    return s


class DisplayWidthTest(unittest.TestCase):
    """Issue #25: every line is budgeted in terminal columns, not characters.

    The frame is checked after stripping ANSI, because that is the only thing
    the terminal actually measures - and because the old code's `[:cols + 40]`
    fudge for escape overhead is precisely the sort of bug that hides from a
    raw-string assertion.
    """

    def widths(self, frame):
        return [tw.width(visible(line)) for line in frame.split("\r\n")]

    def assertFits(self, frame, cols, why=""):
        for i, w in enumerate(self.widths(frame)):
            self.assertLessEqual(w, cols, "%s line %d is %d columns wide: %r"
                                 % (why, i, w, visible(frame.split("\r\n")[i])))

    def test_full_width_names_stay_inside_the_frame(self):
        for tier in (0, 1, 2):
            r = Renderer(tier=tier, truecolor=(tier != 0))
            for cols in (80, 97, 120):
                frame = r.render(_ja_state(4), cols, 40)
                self.assertFits(frame, cols, "tier %d %dcols:" % (tier, cols))

    def test_a_nameplate_never_breaks_out_of_its_desk_box(self):
        """The reported symptom: 12 characters drawn as 24 columns.

        Checked on the block itself rather than on a whole frame line, so a
        plate that overflows cannot hide behind a neighbouring desk's padding.
        """
        for tier in (0, 1):
            r = Renderer(tier=tier, truecolor=(tier != 0))
            desk = _ja_state(1).ordered_desks()[0]
            look = r._look(desk, _ja_state(1), 0, frozenset())
            block = r._desk_block(desk, look)
            self.assertEqual(len(block), r.block_h)
            for i, line in enumerate(block):
                self.assertEqual(tw.width(visible(line)), r.block_w,
                                 "tier %d line %d: %r" % (tier, i,
                                                          visible(line)))

    def test_a_full_width_state_label_stays_inside_the_desk(self):
        """The status row under the nameplate carries herdr's state_label,
        which on a Japanese fleet is Japanese. It is centred by the same
        helper as the plate and needs its own guard - the plate tests do not
        reach it, because the two rows are built from different strings."""
        s = OfficeState()
        s.ingest_pane({"pane_id": "w1:p1", "workspace_id": "w1",
                       "agent": "claude", "agent_status": "blocked",
                       "state_labels": {"blocked": "確認待ちですお願いします"}})
        for tier in (0, 1):
            r = Renderer(tier=tier, truecolor=(tier != 0))
            desk = s.ordered_desks()[0]
            block = r._desk_block(desk, r._look(desk, s, 0, frozenset()))
            for line in block:
                self.assertEqual(tw.width(visible(line)), r.block_w,
                                 "tier %d: %r" % (tier, visible(line)))

    def test_a_full_width_room_label_is_cut_to_the_pane(self):
        s = _ja_state(1, room=JA_ROOM * 8)     # 96 columns of room name
        frame = Renderer(tier=1, truecolor=True).render(s, 80, 40)
        self.assertFits(frame, 80, "room label:")

    def test_a_full_width_status_line_is_cut_to_the_pane(self):
        r = Renderer(tier=1, truecolor=True)
        frame = r.render(_state(), 80, 40, status=JA_NAME * 10)
        self.assertFits(frame, 80, "status line:")

    def test_the_scroll_hint_shares_the_header_budget(self):
        """The hint used to be appended after the header was already cut.

        With enough desks to scroll, the header line ran past `cols` on every
        frame - with plain ASCII, so this needs no CJK at all to reproduce.
        The hint is also all-or-nothing: half of "(scroll: 74-102 of 102)" is
        not a shorter readout but a wrong one, so a header that cannot take
        the whole thing shows none of it.
        """
        s = OfficeState()
        for i in range(40):
            s.ingest_pane({"pane_id": "p%02d" % i, "workspace_id": "w1",
                           "agent": "claude", "agent_status": "blocked"})
        s.set_room_label("w1", "a-fairly-long-workspace-label-here")
        r = Renderer(tier=1, truecolor=True)
        seen, dropped = False, False
        for cols in range(80, 121):
            s.select("p39")
            frame = r.render(s, cols, 30, muted=True)
            self.assertFits(frame, cols, "%d cols:" % cols)
            header = visible(frame).split("\r\n")[0]
            if "scroll:" in header:
                seen = True
                self.assertTrue(header.endswith(")"),
                                "%d cols: hint cut mid-token: %r"
                                % (cols, header))
            else:
                dropped = True
                self.assertNotIn("(", header, "%d cols: %r" % (cols, header))
        # Both branches must have been taken, or the sweep proves nothing.
        self.assertTrue(seen, "the hint never fitted at any width")
        self.assertTrue(dropped, "the hint always fitted; the drop is untested")

    def test_compact_rows_are_measured_without_their_colour(self):
        """The compact fallback used to cut at `cols + 40` to leave room for
        escapes, which is a guess in both directions: too generous for ASCII,
        far too generous for CJK."""
        s = _ja_state(6, room=JA_ROOM * 3)
        s.select("w1:p3")
        s.set_focused("w1:p4")
        for tier in (0, 1):
            r = Renderer(tier=tier, truecolor=(tier != 0))
            for cols in (20, 40, 60, 79):
                frame = r.render(s, cols, 12, escalated={"w1:p1"})
                self.assertFits(frame, cols, "tier %d %dcols compact:"
                                % (tier, cols))

    def test_the_help_overlay_is_cut_to_the_pane(self):
        r = Renderer(tier=1, truecolor=True)
        for cols in (20, 30, 45, 80):
            self.assertFits(r.render(_state(), cols, 40, show_help=True), cols,
                            "%d cols help:" % cols)

    def test_an_ascii_desk_block_is_laid_out_exactly_as_before(self):
        """Latin text must land where it always did, to the column.

        Pinned as a literal rather than as "width == len", which is true of
        any ASCII string by construction and so would survive centre becoming
        left-align, a row reorder, or a changed block height.
        """
        s = OfficeState()
        s.ingest_pane({"pane_id": "w1:p1", "workspace_id": "w1",
                       "agent": "claude", "agent_status": "working"})
        s.select("w1:p1")
        r = Renderer(tier=0, truecolor=False)
        desk = s.ordered_desks()[0]
        block = [visible(line)
                 for line in r._desk_block(desk, r._look(desk, s, 0,
                                                         frozenset()))]
        self.assertEqual(block, ["+---------+",
                                 "|         |",
                                 "|   o     |",
                                 "|  /|\\ ## |",
                                 "| [_____] |",
                                 "| claude  |",
                                 "| working |",
                                 "+---------+"])

    def test_an_emoji_presentation_selector_is_measured_as_drawn(self):
        """U+FE0F makes its base two cells wide; counting it as a zero-width
        mark left the plate one column over its box - a regression against the
        len()-based code, which happened to get this input right."""
        self.assertEqual(tw.width("⚠️"), 2)
        self.assertEqual(tw.width("⚠️ build"), 8)
        # And the selector is never separated from the base it modifies.
        self.assertEqual(tw.truncate("⚠️x", 1), "")
        self.assertEqual(tw.truncate("⚠️x", 2), "⚠️")
        s = OfficeState()
        s.ingest_pane({"pane_id": "w1:p1", "workspace_id": "w1",
                       "agent": "claude", "agent_status": "working",
                       "label": "⚠️ build ⚠️ now"})
        for tier in (0, 1):
            r = Renderer(tier=tier, truecolor=(tier != 0))
            desk = s.ordered_desks()[0]
            for line in r._desk_block(desk, r._look(desk, s, 0, frozenset())):
                self.assertEqual(tw.width(visible(line)), r.block_w,
                                 "tier %d: %r" % (tier, visible(line)))


class PlateLinesTest(unittest.TestCase):
    """`plate_lines` (issue #25): an opt-in second nameplate row."""

    def test_default_is_one_line_and_the_historical_height(self):
        r = Renderer(tier=1, truecolor=True)
        self.assertEqual(r.plate_lines, 1)
        self.assertEqual(r.block_h, r.art_rows + 4)

    def test_two_lines_makes_every_desk_one_row_taller(self):
        one = Renderer(tier=1, truecolor=True)
        two = Renderer(tier=1, truecolor=True, plate_lines=2)
        self.assertEqual(two.block_h, one.block_h + 1)

    def test_the_default_desk_block_keeps_its_historical_shape(self):
        """"The default does not move" pinned against a literal, not against
        a second renderer built with the same argument."""
        s = _ja_state(1)
        r = Renderer(tier=0, truecolor=False)
        desk = s.ordered_desks()[0]
        block = [visible(line)
                 for line in r._desk_block(desk, r._look(desk, s, 0,
                                                         frozenset()))]
        self.assertEqual(len(block), r.art_rows + 4)
        # art, then exactly one nameplate row, then the status row.
        self.assertEqual(block[1 + r.art_rows], "|" + "変更が反" + " |")
        self.assertEqual(block[2 + r.art_rows], "| working |")

    def test_a_bad_plate_lines_value_degrades_instead_of_raising(self):
        """The value reaches here from a config file, and design.md section 8
        forbids a config typo from stopping the office opening."""
        for value in (0, -5, 99, 2.7, None, "two", object()):
            self.assertIn(Renderer(tier=1, truecolor=True,
                                   plate_lines=value).plate_lines, (1, 2),
                          repr(value))
        self.assertEqual(Renderer(tier=1, plate_lines=99).plate_lines, 2)
        self.assertEqual(Renderer(tier=1, plate_lines="two").plate_lines, 1)

    def test_the_frame_assembly_loop_emits_every_block_row(self):
        """Regression for the pre-implementation review's BLOCKER.

        block_h is what `_full` indexes rows with, so asserting the attribute
        and the block length is not enough: the assembly loop has to actually
        put all of them on screen. Mutating its bound to `block_h - 1` used to
        leave the whole suite green while every desk lost its bottom border.
        """
        for plate_lines in (1, 2):
            for tier in (0, 1):
                s = OfficeState()
                s.ingest_pane({"pane_id": "w1:p1", "workspace_id": "w1",
                               "agent": "claude", "agent_status": "working",
                               "display_agent": JA_NAME})
                s.select("w1:p1")
                r = Renderer(tier=tier, truecolor=(tier != 0),
                             plate_lines=plate_lines)
                desk = s.ordered_desks()[0]
                block = [visible(line) for line
                         in r._desk_block(desk, r._look(desk, s, 0,
                                                        frozenset()))]
                lines = [visible(line) for line
                         in r.render(s, 120, 40).split("\r\n")]
                where = "tier %d plate_lines %d" % (tier, plate_lines)
                # The block appears in the frame whole and in order, bottom
                # border included - that is what the loop bound governs.
                start = lines.index(block[0])
                self.assertEqual(lines[start:start + r.block_h], block, where)

    def test_the_second_line_carries_the_rest_of_the_name(self):
        r = Renderer(tier=1, truecolor=True, plate_lines=2)
        desk = _ja_state(1).ordered_desks()[0]
        look = r._look(desk, _ja_state(1), 0, frozenset())
        block = r._desk_block(desk, look)
        # [1:-1] drops the block's own side borders before reading the plate.
        plates = [visible(line)[1:-1].strip()
                  for line in block[1 + r.art_rows:1 + r.art_rows + 2]]
        # 16 columns of plate = 8 full-width characters per row, so a 14
        # character name needs both rows and fits in them.
        self.assertEqual(plates[0], (JA_NAME + "0")[:8])
        self.assertEqual(plates[1], (JA_NAME + "0")[8:])
        for line in block:
            self.assertEqual(tw.width(visible(line)), r.block_w)

    def test_every_block_line_is_accounted_for(self):
        """block_h drives row assembly by index, so it must equal the block."""
        for tier in (0, 1, 2):
            for plate_lines in (1, 2):
                r = Renderer(tier=tier, truecolor=(tier != 0),
                             plate_lines=plate_lines)
                desk = _state().ordered_desks()[0]
                look = r._look(desk, _state(), 0, frozenset())
                self.assertEqual(len(r._desk_block(desk, look)), r.block_h)

    def test_layout_regression_with_many_desks_at_two_plate_lines(self):
        """Scrolling and tier-2 overlay placement must follow the taller desk.

        The failure this guards is silent: an understated block_h leaves the
        selected desk half off the bottom and puts sprite rectangles over the
        status line, and both look like ordinary layout until measured.
        """
        s = OfficeState()
        for i in range(20):
            s.ingest_pane({"pane_id": "p%02d" % i, "workspace_id": "w1",
                           "agent": "claude", "agent_status": "working",
                           "display_agent": "%s%02d" % (JA_NAME, i)})
        s.set_room_label("w1", JA_ROOM)
        r = Renderer(tier=2, truecolor=True, plate_lines=2)
        for rows in range(24, 40, 3):
            for cols in (80, 100, 120):
                for pick in (0, 7, 19):
                    s.select("p%02d" % pick)
                    for status in ("", JA_NAME):
                        frame = r.render(s, cols, rows, status=status)
                        lines = frame.split("\r\n")
                        self.assertEqual(len(lines), rows)
                        where = "%dx%d sel=%d" % (cols, rows, pick)
                        for line in lines:
                            self.assertLessEqual(tw.width(visible(line)), cols,
                                                 "%s: %r" % (where,
                                                             visible(line)))
                        for row, col, _v, _a, _f in r.sprite_boxes:
                            self.assertGreaterEqual(row, 1, where)
                            self.assertLessEqual(row + r.art_rows, len(lines),
                                                 "%s: box at %d" % (where, row))
                            # Still pointing at painted half-blocks, not at the
                            # row the extra nameplate pushed everything to.
                            for dy in range(r.art_rows):
                                cells = visible(
                                    lines[row + dy])[col:col + sprites.DESK_W]
                                self.assertEqual(set(cells), {"▀"},
                                                 "%s: row %d" % (where,
                                                                 row + dy))

    def test_the_selected_desk_stays_on_screen_when_it_is_taller(self):
        s = OfficeState()
        for i in range(30):
            s.ingest_pane({"pane_id": "p%02d" % i, "workspace_id": "w1",
                           "agent": "claude", "agent_status": "working",
                           "display_agent": "desk%02d" % i})
        r = Renderer(tier=1, truecolor=True, plate_lines=2)
        for pick in (0, 15, 29):
            s.select("p%02d" % pick)
            frame = visible(r.render(s, 120, 30))
            self.assertIn("desk%02d" % pick, frame,
                          "selection %d scrolled off screen" % pick)


class EscalatedTest(unittest.TestCase):
    """The ESCALATED overlay (character-states.md section 1)."""

    def test_tier0_blocked_bubble_becomes_double_bang(self):
        r = Renderer(tier=0, truecolor=False)
        plain = r.render(_state(), 120, 40, frame=0)
        loud = r.render(_state(), 120, 40, frame=0, escalated={"w1:p2"})
        self.assertIn("!!", loud)
        self.assertNotIn("!!", plain)

    def test_escalating_an_unblocked_desk_changes_nothing(self):
        r = Renderer(tier=0, truecolor=False)
        self.assertEqual(r.render(_state(), 120, 40, frame=0),
                         r.render(_state(), 120, 40, frame=0,
                                  escalated={"w1:p1"}))   # p1 is working

    def test_tier1_escalated_frame_still_renders(self):
        r = Renderer(tier=1, truecolor=True)
        frame = r.render(_state(), 120, 40, frame=0, escalated={"w1:p2"})
        self.assertIn("AGENT OFFICE", frame)

    def test_compact_marks_escalated(self):
        r = Renderer(tier=1, truecolor=True)
        frame = r.render(_state(), 40, 12, escalated={"w1:p2"})
        self.assertIn("blocked!!", frame)


class Tier2LayoutTest(unittest.TestCase):
    """The sprite rectangles tier 2 hands to the graphics layer (design 5)."""

    def render(self, tier, **kw):
        r = Renderer(tier=tier, truecolor=True)
        return r, r.render(_state(), 120, 40, **kw)

    def test_tier2_text_is_exactly_the_tier1_frame(self):
        # tier 2 is additive: it draws the whole tier 1 office and puts an
        # image on top, so an overlay that never arrives is invisible rather
        # than fatal.
        _, one = self.render(1, frame=3)
        _, two = self.render(2, frame=3)
        self.assertEqual(one, two)

    def test_only_tier2_reports_boxes(self):
        for tier in (0, 1):
            r, _ = self.render(tier)
            self.assertEqual(r.sprite_boxes, [])
        r, _ = self.render(2)
        self.assertEqual(len(r.sprite_boxes), 2)

    def test_boxes_point_at_the_cells_the_sprite_was_painted_in(self):
        """The overlay is placed by cell, so this is the alignment contract."""
        r, frame = self.render(2)
        lines = frame.split("\r\n")
        for row, col, _visual, _agent, _focused in r.sprite_boxes:
            for dy in range(r.art_rows):
                text = visible(lines[row + dy])
                cells = text[col:col + sprites.DESK_W]
                self.assertEqual(len(cells), sprites.DESK_W)
                self.assertEqual(set(cells), {"▀"},
                                 "row %d: %r" % (row + dy, cells))

    def test_boxes_carry_the_escalated_visual_not_the_plain_one(self):
        r, _ = self.render(2, escalated={"w1:p2"})
        visuals = {agent: vis for _, _, vis, agent, _ in r.sprite_boxes}
        self.assertEqual(visuals["codex"], "blocked_escalated")
        self.assertEqual(visuals["claude"], "working")

    def test_boxes_are_stable_across_animation_frames(self):
        """The overlay is static, so ticking the animation must not resend it."""
        r0, _ = self.render(2, frame=0)
        r1, _ = self.render(2, frame=1)
        self.assertEqual(r0.sprite_boxes, r1.sprite_boxes)

    def test_the_focused_desk_is_marked_so_its_floor_lights_up(self):
        s = _state()
        s.set_focused("w1:p1")
        r = Renderer(tier=2, truecolor=True)
        r.render(s, 120, 40)
        focus = {agent: foc for _, _, _, agent, foc in r.sprite_boxes}
        self.assertTrue(focus["claude"])
        self.assertFalse(focus["codex"])

    def test_help_and_compact_views_report_no_sprites(self):
        r, _ = self.render(2, show_help=True)
        self.assertEqual(r.sprite_boxes, [])
        r = Renderer(tier=2, truecolor=True)
        r.render(_state(), 40, 12)                  # too small: compact view
        self.assertEqual(r.sprite_boxes, [])

    def test_no_box_ever_reaches_past_the_last_frame_line(self):
        """Regression: the clip bound was one too generous.

        At 80x28 with 20 desks the bottom row of desks starts on frame line 23
        and a sprite is 6 rows tall, so it ran to line 28 - one past the last
        line the frame actually has. The image was placed over the status line
        or off the pane, in the very place the text layout had scrolled away
        from. Swept across sizes and selections rather than pinned to the one
        that happened to expose it.
        """
        s = OfficeState()
        for i in range(20):
            s.ingest_pane({"pane_id": "p%02d" % i, "workspace_id": "w1",
                           "agent": "claude", "agent_status": "working"})
        r = Renderer(tier=2, truecolor=True)
        for rows in range(24, 40):
            for cols in (80, 100, 120):
                for pick in (0, 7, 19):
                    s.select("p%02d" % pick)
                    for status in ("", "a warning"):
                        frame = r.render(s, cols, rows, status=status)
                        lines = frame.split("\r\n")
                        self.assertEqual(len(lines), rows)
                        for row, _c, _v, _a, _f in r.sprite_boxes:
                            self.assertGreaterEqual(row, 1)
                            self.assertLessEqual(
                                row + r.art_rows, len(lines),
                                "%dx%d sel=%d: box at row %d overruns"
                                % (cols, rows, pick, row))

    def test_boxes_scrolled_off_the_screen_are_dropped(self):
        s = OfficeState()
        for i in range(40):
            s.ingest_pane({"pane_id": "p%02d" % i, "workspace_id": "w1",
                           "agent": "claude", "agent_status": "working"})
        r = Renderer(tier=2, truecolor=True)
        s.select("p39")
        frame = r.render(s, 120, 30)
        lines = frame.split("\r\n")
        self.assertLess(len(r.sprite_boxes), 40)
        self.assertTrue(r.sprite_boxes)
        for row, col, _v, _a, _f in r.sprite_boxes:
            self.assertGreaterEqual(row, 1)
            self.assertLessEqual(row + r.art_rows, len(lines))
            for dy in range(r.art_rows):
                cells = visible(lines[row + dy])[col:col + sprites.DESK_W]
                self.assertEqual(set(cells), {"▀"})


if __name__ == "__main__":
    unittest.main()
