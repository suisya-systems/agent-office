"""Tests for the office event loop's non-I/O wiring.

Office's constructor opens no sockets and starts no threads, so the dispatch
and status-line logic can be exercised directly with a bogus socket path.
"""

import unittest

from office.config import Config
from office.escalator import Notification
from office.office import TOAST_HINT, Office


def make_office(config=None):
    return Office("/nonexistent.sock", "self-pane", tier=1, truecolor=True,
                  config=config or Config())


class StatusLineTest(unittest.TestCase):
    def test_clean_config_says_nothing(self):
        self.assertEqual(make_office()._status(), "")

    def test_config_warnings_survive_connection_logs(self):
        # regression: warnings lived in status_line, which the subscriber's
        # "connected" log overwrote a few milliseconds after startup - so the
        # documented "bad values are surfaced on the status line" was false
        # in the ordinary case where herdr connects fine.
        office = make_office(Config(warnings=("[office].fps must be a number",)))
        self.assertIn("fps", office._status())
        office._handle(("log", "connected"))
        self.assertIn("fps", office._status())
        self.assertIn("connected", office._status())

    def test_transient_logs_replace_each_other(self):
        office = make_office()
        office._handle(("log", "connected"))
        office._handle(("log", "reconnecting in 0.5s"))
        self.assertNotIn("connected", office._status())
        self.assertIn("reconnecting", office._status())


class NotifyResultTest(unittest.TestCase):
    def note(self):
        return Notification(title="t", body="b", sound="none",
                            pane_ids=("p1",), kind="blocked")

    def test_disabled_raises_the_setup_hint(self):
        office = make_office()
        office._handle(("notify_result", (self.note(), "disabled")))
        self.assertIn(TOAST_HINT, office._status())

    def test_a_later_success_clears_the_hint(self):
        office = make_office()
        office._handle(("notify_result", (self.note(), "disabled")))
        office._handle(("notify_result", (self.note(), "shown")))
        self.assertEqual(office._status(), "")

    def test_no_foreground_client_is_quiet(self):
        office = make_office()
        office._handle(("notify_result", (self.note(), "no_foreground_client")))
        self.assertEqual(office._status(), "")

    def test_rate_limited_is_reported(self):
        office = make_office()
        office._handle(("notify_result", (self.note(), "rate_limited")))
        self.assertIn("rate_limited", office._status())


class ConfigWiringTest(unittest.TestCase):
    def test_fps_drives_the_tick_interval(self):
        self.assertEqual(make_office(Config(fps=4)).tick_s, 0.25)
        self.assertEqual(make_office(Config(fps=1)).tick_s, 1.0)

    def test_include_config_reaches_the_state_model(self):
        office = make_office(Config(filter="all", workspaces=("w*",),
                                    exclude_agents=("codex",)))
        self.assertEqual(office.state.filter_mode, "all")
        self.assertEqual(office.state.workspace_globs, ("w*",))
        self.assertIn("codex", office.state.exclude_agents)

    def test_escalation_config_reaches_the_escalator(self):
        office = make_office(Config(blocked_threshold_s=10,
                                    renotify_interval_s=0, sound="none",
                                    notify_done=True))
        self.assertEqual(office.escalator.threshold_s, 10.0)
        self.assertEqual(office.escalator.renotify_s, 0.0)
        self.assertEqual(office.escalator.sound, "none")
        self.assertTrue(office.escalator.notify_done)

    def test_mute_key_reaches_the_escalator(self):
        office = make_office()
        office._handle(("key", "s"))
        self.assertTrue(office.muted)
        office._escalate()
        self.assertTrue(office.escalator.muted)


if __name__ == "__main__":
    unittest.main()
