"""Tests for the office event loop's non-I/O wiring.

Office's constructor opens no sockets and starts no threads, so the dispatch
and status-line logic can be exercised directly with a bogus socket path. The
last class does run the real loop, with every thread but the Commander stubbed
out and the Commander's socket faked, to pin down issue #12: a stuck herdr must
not stop the frames.
"""

import queue
import signal
import threading
import time
import unittest

from office import commander as commander_mod
from office import office as office_mod
from office.config import Config
from office.escalator import Notification
from office.office import TOAST_HINT, Office
from office.screen import Screen


WORKING_P1 = {"pane_id": "p1", "agent": "claude", "agent_status": "working"}


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

    def test_theme_reaches_the_renderer(self):
        office = make_office(Config(theme="midnight"))
        self.assertEqual(office.renderer.theme.name, "midnight")


class FakeSender:
    def __init__(self):
        self.calls = []

    def set_boxes(self, boxes, art):
        self.calls.append(("set", tuple(boxes)))

    def clear(self):
        self.calls.append(("clear",))


class GraphicsWiringTest(unittest.TestCase):
    """Tier 2's overlay bookkeeping (design.md section 5, risk 6)."""

    def office(self, tier=2, pane="self-pane"):
        return Office("/nonexistent.sock", pane, tier=tier, truecolor=True,
                      config=Config())

    def test_only_tier2_owns_a_graphics_sender(self):
        self.assertIsNone(self.office(tier=0).graphics)
        self.assertIsNone(self.office(tier=1).graphics)
        self.assertIsNotNone(self.office(tier=2).graphics)

    def test_without_a_pane_id_there_is_nothing_to_draw_on(self):
        self.assertIsNone(self.office(tier=2, pane=None).graphics)

    def test_an_unchanged_frame_sends_nothing(self):
        # The overlay is static and a PNG encode is not free: re-sending it on
        # every animation tick is exactly what the box comparison prevents.
        office = self.office()
        office.graphics = fake = FakeSender()
        office.renderer.sprite_boxes = [(1, 1, "working", "claude", False)]
        office._sync_overlay()
        office._sync_overlay()
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0][0], "set")

    def test_a_changed_frame_sends_again(self):
        office = self.office()
        office.graphics = fake = FakeSender()
        office.renderer.sprite_boxes = [(1, 1, "working", "claude", False)]
        office._sync_overlay()
        office.renderer.sprite_boxes = [(1, 1, "blocked", "claude", False)]
        office._sync_overlay()
        self.assertEqual([c[0] for c in fake.calls], ["set", "set"])

    def test_losing_the_sprites_takes_the_overlay_down(self):
        # The help overlay and the compact view have no sprites; an image left
        # up would sit on top of them.
        office = self.office()
        office.graphics = fake = FakeSender()
        office.renderer.sprite_boxes = [(1, 1, "working", "claude", False)]
        office._sync_overlay()
        office.renderer.sprite_boxes = []
        office._sync_overlay()
        office._sync_overlay()
        self.assertEqual([c[0] for c in fake.calls], ["set", "clear"])

    def test_tier1_never_touches_the_overlay(self):
        office = self.office(tier=1)
        office.renderer.sprite_boxes = [(1, 1, "working", "claude", False)]
        office._sync_overlay()                     # must not raise

    def test_a_graphics_failure_is_visible_on_the_status_line(self):
        office = self.office()
        office._handle(("graphics", (False, "feature_disabled")))
        self.assertIn("feature_disabled", office._status())
        office._handle(("graphics", (True, "")))
        self.assertEqual(office._status(), "")

    def test_a_failed_send_is_retried_rather_than_believed(self):
        """Regression: the cache recorded intent, not what is on screen.

        After a failed set, _overlay_boxes still matched the frame, so the
        office believed the image was up and never resent it - the overlay
        stayed missing until some unrelated change moved a desk.
        """
        clock = [1000.0]
        office = self.office()
        office.state._now = lambda: clock[0]
        office.graphics = fake = FakeSender()
        office.renderer.sprite_boxes = [(1, 1, "working", "claude", False)]
        office._sync_overlay()
        office._handle(("graphics", (False, "busy")))
        office._sync_overlay()                     # still inside the backoff
        self.assertEqual(len(fake.calls), 1)
        clock[0] += office_mod.GRAPHICS_RETRY_S + 0.1
        office._sync_overlay()
        self.assertEqual([c[0] for c in fake.calls], ["set", "set"])

    def test_repeated_identical_failures_keep_being_retried(self):
        """Regression: the two rate limiters cancelled out.

        Driven through the *real* GraphicsSender rather than a stub, because
        the bug lived in the interaction: the sender used to drop a repeat of
        the same failure, so the loop never heard about the second one, went
        on believing the overlay was up, and stopped retrying for good.
        """
        from office import graphics as graphics_mod

        class AlwaysBusy:
            ProtocolError = graphics_mod.protocol.ProtocolError

            def __init__(self):
                self.sets = 0

            def pane_graphics_set(self, *a, **kw):
                self.sets += 1
                raise self.ProtocolError("busy", "server busy")

            def pane_graphics_clear(self, *a, **kw):
                pass

        real = graphics_mod.protocol
        self.addCleanup(setattr, graphics_mod, "protocol", real)
        graphics_mod.protocol = proto = AlwaysBusy()

        clock = [1000.0]
        office = self.office()
        office.state._now = lambda: clock[0]
        office.renderer.sprite_boxes = [(1, 1, "working", "claude", False)]
        for _ in range(4):
            office._sync_overlay()
            pending = office.graphics._pending
            if pending:
                office.graphics._pending = None
                office.graphics._run(pending)
            while True:
                try:
                    office._handle(office.q.get_nowait())
                except queue.Empty:
                    break
            clock[0] += office_mod.GRAPHICS_RETRY_S + 1
        self.assertEqual(proto.sets, 4)
        self.assertIn("busy", office._status())

    def test_switching_to_a_sprite_free_view_clears_despite_the_backoff(self):
        """Regression: the backoff held back the clear the help view needs.

        A failed send starts the backoff. Pressing `?` right after replaces
        the desks with help text, and the stale image - still up, if the
        failure's best-effort clear was refused too - sat on top of it until
        the window elapsed. The backoff gates retries, not new intent.
        """
        clock = [1000.0]
        office = self.office()
        office.state._now = lambda: clock[0]
        office.graphics = fake = FakeSender()
        office.renderer.sprite_boxes = [(1, 1, "working", "claude", False)]
        office._sync_overlay()
        office._handle(("graphics", (False, "busy")))
        clock[0] += 0.05                           # well inside the backoff
        office.renderer.sprite_boxes = []          # help / compact view
        office._sync_overlay()
        self.assertEqual([c[0] for c in fake.calls], ["set", "clear"])

    def test_a_repeatedly_failing_clear_still_does_not_spin(self):
        # The flip side of the test above: acting on new intent at once must
        # not let a persistently refused clear run once per redraw.
        clock = [1000.0]
        office = self.office()
        office.state._now = lambda: clock[0]
        office.graphics = fake = FakeSender()
        office.renderer.sprite_boxes = []
        for _ in range(50):
            office._sync_overlay()
            office._handle(("graphics", (False, "busy")))
            clock[0] += 0.04                       # MIN_REDRAW_S: ~25 fps
        self.assertLessEqual(len(fake.calls), 2)

    def test_a_sprite_free_first_frame_clears_once(self):
        """Hygiene, and deliberately not skipped as a no-op.

        The office pane can be reopened over a herdr that still holds the
        previous process's graphics layer, so the first frame says what it
        wants even when what it wants is nothing. Once, though - the state
        then matches and stays quiet.
        """
        office = self.office()
        office.graphics = fake = FakeSender()
        office.renderer.sprite_boxes = []
        for _ in range(5):
            office._sync_overlay()
        self.assertEqual([c[0] for c in fake.calls], ["clear"])

    def test_a_late_failure_does_not_bury_a_newer_success(self):
        """Regression: only the failure edge of _overlay_ok was wired.

        Submit A, submit B while A is still out, then A fails and B succeeds.
        The failure arrives second-to-last, so the office was left believing
        B was missing and re-sent an overlay that was already on screen.
        """
        clock = [1000.0]
        office = self.office()
        office.state._now = lambda: clock[0]
        office.graphics = fake = FakeSender()
        office.renderer.sprite_boxes = [(1, 1, "working", "claude", False)]
        office._sync_overlay()
        office.renderer.sprite_boxes = [(1, 1, "blocked", "claude", False)]
        office._sync_overlay()
        office._handle(("graphics", (False, "busy")))     # A, sent first
        office._handle(("graphics", (True, "")))          # B, sent second
        self.assertTrue(office._overlay_ok)
        fake.calls.clear()
        for _ in range(6):
            clock[0] += office_mod.GRAPHICS_RETRY_S + 1
            office._sync_overlay()
        self.assertEqual(fake.calls, [])

    def test_the_overlay_state_machine_over_every_short_interleaving(self):
        """Exhaustive check of the invariant, not just the cases we thought of.

        Three findings in a row landed in this bookkeeping, so the rule is
        asserted directly over every ordering of submits and reports up to
        length 5: the sender is serial and reports each request it runs, so
        after the queue is drained the office must agree with the last report
        - and must never re-send an overlay whose own report said it landed.
        """
        import itertools

        A = [(1, 1, "working", "claude", False)]
        B = [(1, 1, "blocked", "claude", False)]
        events = (("submit", A), ("submit", B), ("submit", []),
                  ("report", True), ("report", False))
        checked = 0
        for length in range(1, 6):
            for seq in itertools.product(events, repeat=length):
                clock = [1000.0]
                office = self.office()
                office.state._now = lambda: clock[0]
                office.graphics = fake = FakeSender()
                last_report = None
                for kind, value in seq:
                    if kind == "submit":
                        office.renderer.sprite_boxes = list(value)
                        office._sync_overlay()
                    else:
                        office._handle(("graphics", (value, "busy")))
                        last_report = value
                if last_report is not True or office._overlay_boxes is None:
                    continue        # nothing was ever submitted to land
                # The last thing we heard was "it landed". Nothing may be
                # re-sent while the frame keeps asking for the same thing.
                office.renderer.sprite_boxes = list(office._overlay_boxes)
                checked += 1
                self.assertTrue(office._overlay_ok, seq)
                fake.calls.clear()
                for _ in range(3):
                    clock[0] += office_mod.GRAPHICS_RETRY_S + 1
                    office._sync_overlay()
                self.assertEqual(fake.calls, [], seq)
        self.assertGreater(checked, 100)           # the sweep really ran

    def test_a_standing_refusal_does_not_send_on_every_redraw(self):
        # herdr can turn experimental.kitty_graphics back off under a running
        # office (server reload-config), so the failure path must be bounded.
        clock = [1000.0]
        office = self.office()
        office.state._now = lambda: clock[0]
        office.graphics = fake = FakeSender()
        office.renderer.sprite_boxes = [(1, 1, "working", "claude", False)]
        for _ in range(50):
            office._sync_overlay()
            office._handle(("graphics", (False, "feature_disabled")))
            clock[0] += 0.04                       # MIN_REDRAW_S: ~25 fps
        self.assertLessEqual(len(fake.calls), 2)

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

    def test_a_status_event_outranks_the_refresh_it_overtook(self):
        # The refresh's panes are as herdr saw them when `a` was pressed. A
        # pane.agent_status_changed handled while it was in flight is newer,
        # and rolling it back here would blank the escalation timer.
        office = self.office()
        office._handle(("snapshot", [{"pane_id": "p1", "agent": "claude",
                                      "agent_status": "working"}]))
        office._handle(("key", "a"))
        office._handle(("status", {"pane_id": "p1", "agent_status": "blocked"}))
        blocked_since = office.state.desks["p1"].blocked_since
        office._handle(("action", ("pane_list", [WORKING_P1], None,
                                   office.commander.tokens[0])))
        self.assertEqual(office.state.desks["p1"].status, "blocked")
        self.assertEqual(office.state.desks["p1"].blocked_since, blocked_since)

    def test_overlapping_refreshes_keep_their_own_timestamps(self):
        # Two `a` presses in flight at once: the first result home must not
        # take the second one's token with it, or the second would be applied
        # as if nothing could have overtaken it.
        office = self.office()
        office._handle(("snapshot", [{"pane_id": "p1", "agent": "claude",
                                      "agent_status": "working"}]))
        office._handle(("key", "a"))
        office._handle(("key", "a"))
        first, second = office.commander.tokens
        office._handle(("status", {"pane_id": "p1", "agent_status": "blocked"}))
        office._handle(("action", ("pane_list", [WORKING_P1], None, first)))
        self.assertIn("refreshing", office._status())   # one still out
        office._handle(("action", ("pane_list", [WORKING_P1], None, second)))
        self.assertEqual(office.state.desks["p1"].status, "blocked")
        self.assertEqual(office._status(), "")

    def test_the_refresh_still_brings_in_panes_it_had_not_seen(self):
        office = self.office()
        office._handle(("key", "a"))
        office._handle(("action", ("pane_list", [{"pane_id": "p9",
                                                  "agent_status": "blocked"}],
                                   None)))
        self.assertEqual(office.state.desks["p9"].status, "blocked")

    def test_a_user_refresh_does_not_spend_the_startup_seed(self):
        # design.md section 7: the recovered blocked_since belongs to the
        # authoritative startup snapshot. An `a` refresh can arrive first (and
        # on a partial fleet), and must not consume it - the desk would then
        # start a fresh 90s countdown instead of inheriting its real one.
        office = self.office()
        # Taken from the model's own clock, not written out as a constant: a
        # bare 1000.0 is in the *future* on a freshly booted machine, where
        # time.monotonic() is still a two-digit number, and would be ignored
        # for a reason that has nothing to do with this test (CI, PR #13).
        seeded = office.state.now() - 500.0
        office._seed_blocked = {"p1": seeded}
        panes = [{"pane_id": "p1", "agent": "claude", "agent_status": "blocked"}]
        office._handle(("action", ("pane_list", panes, None)))
        self.assertNotEqual(office.state.desks["p1"].blocked_since, seeded)
        office._handle(("snapshot", panes))
        self.assertEqual(office.state.desks["p1"].blocked_since, seeded)

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
        self.tokens = []                  # one per pane.list request

    def start(self):
        pass

    def stop(self):
        pass

    def focus(self, pane_id, token=None):
        self.focused.append(pane_id)

    def list_panes(self, token=None):
        self.tokens.append(token)

    @property
    def lists(self):
        return len(self.tokens)


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
