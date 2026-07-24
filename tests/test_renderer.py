"""Smoke tests for tier detection and frame assembly (no herdr needed)."""

import re
import unittest

from office import sprites
from office.renderer import Renderer, detect_caps, format_name
from office.state import OfficeState

ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


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


class CapsTest(unittest.TestCase):
    def test_force_ascii(self):
        self.assertEqual(detect_caps("ascii", {})[0], 0)

    def test_force_unicode(self):
        self.assertEqual(detect_caps("unicode", {})[0], 1)

    def test_dumb_term_is_tier0(self):
        self.assertEqual(detect_caps(None, {"TERM": "dumb", "LANG": "C.UTF-8"})[0], 0)

    def test_utf8_is_tier1_truecolor(self):
        tier, tc = detect_caps(None, {"TERM": "xterm-256color",
                                      "LANG": "en_US.UTF-8",
                                      "COLORTERM": "truecolor"})
        self.assertEqual((tier, tc), (1, True))

    def test_non_utf8_is_tier0(self):
        self.assertEqual(detect_caps(None, {"TERM": "xterm", "LANG": "C"})[0], 0)

    def test_force_kitty_is_tier2(self):
        self.assertEqual(detect_caps("kitty", {})[0], 2)

    def test_auto_never_picks_kitty(self):
        """tier 2 is opt-in: experimental in herdr, and needs a capable term."""
        for env in ({"TERM": "xterm-kitty", "LANG": "en_US.UTF-8",
                     "COLORTERM": "truecolor"},
                    {"TERM": "xterm-256color", "LANG": "en_US.UTF-8"},
                    {"TERM": "dumb"}):
            self.assertNotEqual(detect_caps(None, env)[0], 2)


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
