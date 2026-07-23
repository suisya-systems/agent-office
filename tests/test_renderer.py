"""Smoke tests for tier detection and frame assembly (no herdr needed)."""

import unittest

from office.renderer import Renderer, detect_caps, format_name
from office.state import OfficeState


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


if __name__ == "__main__":
    unittest.main()
