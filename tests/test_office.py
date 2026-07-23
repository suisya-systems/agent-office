"""Tests for the office event loop's non-I/O wiring.

Office's constructor opens no sockets and starts no threads, so the dispatch
and status-line logic can be exercised directly with a bogus socket path. The
last class does run the real loop, with every thread but the Commander stubbed
out and the Commander's socket faked, to pin down issue #12: a stuck herdr must
not stop the frames.
"""

import signal
import threading
import time
import unittest

from office import commander as commander_mod
from office.config import Config
from office.escalator import Notification
from office.office import TOAST_HINT, Office
from office.screen import Screen


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


class ActionFeedbackTest(unittest.TestCase):
    """Jump/filter results arrive a socket round-trip after the keypress."""

    def office(self):
        office = make_office()
        office.commander = RecordingCommander()
        return office

    def test_enter_only_queues_the_focus(self):
        office = self.office()
        office.state.reconcile_snapshot([{"pane_id": "p1", "agent": "claude"}])
        office._handle(("key", "enter"))
        self.assertEqual(office.commander.focused, ["p1"])
        self.assertEqual(office._status(), "")     # success speaks for itself

    def test_a_late_jump_failure_reaches_the_status_line(self):
        office = self.office()
        office._handle(("action", ("focus", None, "herdr is down")))
        self.assertIn("jump failed", office._status())
        self.assertIn("herdr is down", office._status())

    def test_filter_says_it_is_refreshing_until_the_panes_land(self):
        office = self.office()
        office._handle(("key", "a"))
        self.assertEqual(office.state.filter_mode, "all")
        self.assertEqual(office.commander.lists, 1)
        self.assertIn("refreshing", office._status())
        office._handle(("action", ("pane_list", [{"pane_id": "p1"}], None)))
        self.assertEqual(office._status(), "")
        self.assertIn("p1", office.state.desks)

    def test_a_failed_refresh_replaces_the_pending_notice(self):
        office = self.office()
        office._handle(("key", "a"))
        office._handle(("action", ("pane_list", None, "timed out")))
        self.assertNotIn("refreshing", office._status())
        self.assertIn("filter refresh failed", office._status())

    def test_a_jump_landing_mid_refresh_leaves_the_notice_alone(self):
        # Enter after `a`: the focus result comes back first, but the fleet
        # the refresh will bring is still in flight, so the notice stands.
        office = self.office()
        office._handle(("key", "a"))
        office._handle(("action", ("focus", None, None)))
        self.assertIn("refreshing", office._status())

    def test_a_user_refresh_does_not_spend_the_startup_seed(self):
        # design.md section 7: the recovered blocked_since belongs to the
        # authoritative startup snapshot. An `a` refresh can arrive first (and
        # on a partial fleet), and must not consume it - the desk would then
        # start a fresh 90s countdown instead of inheriting its real one.
        office = self.office()
        office._seed_blocked = {"p1": 1000.0}
        panes = [{"pane_id": "p1", "agent": "claude", "agent_status": "blocked"}]
        office._handle(("action", ("pane_list", panes, None)))
        self.assertNotEqual(office.state.desks["p1"].blocked_since, 1000.0)
        office._handle(("snapshot", panes))
        self.assertEqual(office.state.desks["p1"].blocked_since, 1000.0)

    def test_a_newer_notice_outlives_the_pending_one(self):
        # The refresh result must clear its own message, not whatever the
        # subscriber or notifier put up while the socket was in flight.
        office = self.office()
        office._handle(("key", "a"))
        office._handle(("log", "reconnecting in 0.5s"))
        office._handle(("action", ("pane_list", [], None)))
        self.assertIn("reconnecting", office._status())


class RecordingCommander:
    """Commander stand-in: records the asks, reports nothing back."""

    def __init__(self):
        self.focused = []
        self.lists = 0

    def start(self):
        pass

    def stop(self):
        pass

    def focus(self, pane_id):
        self.focused.append(pane_id)

    def list_panes(self):
        self.lists += 1


class StuckProtocol:
    """office.protocol stand-in whose calls hang until the test lets go."""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.focused = []

    def pane_focus(self, _sock, pane_id, timeout=5.0):
        self.focused.append(pane_id)
        self.entered.set()
        self.release.wait(5.0)

    def pane_list(self, _sock, timeout=5.0):
        self.entered.set()
        self.release.wait(5.0)
        return []


class StubThread:
    def start(self):
        pass

    def stop(self):
        pass


class StubWriter:
    def maybe_write(self, *_):
        pass

    def write_stopped(self, *_):
        pass


class FrameCounter:
    """Screen stream that counts frames and stops the office after `frames`."""

    def __init__(self, office, frames, on_done=None):
        self.office = office
        self.frames = frames
        self.on_done = on_done
        self.count = 0
        self.done = False

    def write(self, _text):
        self.count += 1
        if self.count >= self.frames and not self.done:
            self.done = True
            self.office._quit()
            if self.on_done:
                self.on_done()      # unstick herdr so teardown is not a wait

    def flush(self):
        pass


class LoopKeepsTickingTest(unittest.TestCase):
    """Issue #12: a keypress whose socket call hangs must not stop the frames.

    Before the Commander, `_jump` called pane.focus inline, so this loop would
    have sat inside the stuck fake for the whole socket timeout - no ticks, no
    redraws, no way to quit.
    """

    def setUp(self):
        self.real_protocol = commander_mod.protocol
        self.handlers = [(sig, signal.getsignal(sig))
                         for sig in (getattr(signal, "SIGWINCH", None),
                                     getattr(signal, "SIGTERM", None))
                         if sig is not None]
        self.stuck = StuckProtocol()
        commander_mod.protocol = self.stuck

    def tearDown(self):
        self.stuck.release.set()
        commander_mod.protocol = self.real_protocol
        for sig, handler in self.handlers:
            signal.signal(sig, handler)

    def test_a_stuck_pane_focus_does_not_freeze_the_loop(self):
        office = make_office(Config(fps=30))
        office.subscriber = office.reconciler = office.input = StubThread()
        office.notifier = StubThread()
        office.writer = StubWriter()
        counter = FrameCounter(office, frames=8,
                               on_done=self.stuck.release.set)
        office.screen = Screen(stream=counter)
        office.state.reconcile_snapshot([{"pane_id": "p1", "agent": "claude"}])
        office.q.put(("key", "enter"))

        started = time.monotonic()
        office.run()
        elapsed = time.monotonic() - started

        self.assertTrue(self.stuck.entered.is_set())   # the jump did go out
        self.assertEqual(self.stuck.focused, ["p1"])
        self.assertGreaterEqual(counter.count, 8)      # and frames kept coming
        self.assertLess(elapsed, 2.0, "the loop waited on the socket")


if __name__ == "__main__":
    unittest.main()
