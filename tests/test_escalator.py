"""Unit tests for the blocked-timer escalation policy (design.md section 7).

The Escalator does no I/O, so everything here runs on a fake clock: tick()
returns the toasts that would be sent and on_result() feeds the delivery
outcome back in.
"""

import unittest

from office.escalator import AGGREGATE_WINDOW_S, Escalator
from office.state import Desk

THRESHOLD = 90.0
RENOTIFY = 300.0


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def desk(pane_id, status="blocked", blocked_since=None, ws="w1", **kw):
    return Desk(pane_id=pane_id, workspace_id=ws, status=status,
                blocked_since=blocked_since, **kw)


class EscalatorTest(unittest.TestCase):
    def setUp(self):
        self.clk = FakeClock()
        self.esc = Escalator(threshold_s=THRESHOLD, renotify_s=RENOTIFY,
                             now=self.clk)
        self.t0 = self.clk.t

    def tick(self, desks, rooms=None):
        return self.esc.tick(desks, rooms or {})

    def _blocked(self, pane_id="p1", **kw):
        return desk(pane_id, blocked_since=self.t0, **kw)

    # -- threshold ---------------------------------------------------

    def test_no_toast_before_threshold(self):
        self.assertEqual(self.tick([self._blocked()]), [])
        self.clk.advance(THRESHOLD - 1)
        self.assertEqual(self.tick([self._blocked()]), [])

    def test_toast_after_threshold_plus_window(self):
        self.tick([self._blocked()])
        self.clk.advance(THRESHOLD)
        # the collection window has just opened: still silent
        self.assertEqual(self.tick([self._blocked()]), [])
        self.clk.advance(AGGREGATE_WINDOW_S)
        notes = self.tick([self._blocked()])
        self.assertEqual(len(notes), 1)
        self.assertIn("is waiting", notes[0].title)
        self.assertIn("blocked for 1m35s", notes[0].body)
        self.assertEqual(notes[0].pane_ids, ("p1",))

    def test_toast_body_names_room_and_state_label(self):
        d = self._blocked(state_labels={"blocked": "waiting for approval"})
        self.tick([d])
        self.clk.advance(THRESHOLD + AGGREGATE_WINDOW_S)
        notes = self.tick([d], rooms={"w1": "room-one"})
        self.assertIn("in room-one", notes[0].body)
        self.assertIn("waiting for approval", notes[0].body)

    def test_escalated_ids_track_threshold(self):
        self.tick([self._blocked()])
        self.assertEqual(self.esc.escalated_ids(), frozenset())
        self.clk.advance(THRESHOLD)
        self.tick([self._blocked()])
        self.assertEqual(self.esc.escalated_ids(), frozenset({"p1"}))

    # -- aggregation -------------------------------------------------

    def test_two_blocked_desks_share_one_toast(self):
        a = desk("pA", blocked_since=self.t0)
        b = desk("pB", blocked_since=self.t0 + 2)         # 2s later
        self.tick([a, b])
        self.clk.advance(THRESHOLD + AGGREGATE_WINDOW_S)
        notes = self.tick([a, b])
        self.assertEqual(len(notes), 1)
        self.assertIn("2 agents are waiting", notes[0].title)
        self.assertEqual(notes[0].pane_ids, ("pA", "pB"))

    def test_desk_blocking_after_the_window_gets_its_own_toast(self):
        a = desk("pA", blocked_since=self.t0)
        self.clk.advance(THRESHOLD + AGGREGATE_WINDOW_S)
        self.assertEqual(len(self.tick([a])), 1)
        b = desk("pB", blocked_since=self.clk.t)
        self.clk.advance(THRESHOLD + AGGREGATE_WINDOW_S)
        notes = self.tick([a, b])
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].pane_ids, ("pB",))      # pA is not yet due

    # -- renotify ----------------------------------------------------

    def _first_toast(self, d):
        self.tick([d])
        self.clk.advance(THRESHOLD + AGGREGATE_WINDOW_S)
        note = self.tick([d])[0]
        self.esc.on_result(note, "shown")
        return note

    def test_renotify_after_interval_is_labelled(self):
        d = self._blocked()
        self._first_toast(d)
        self.clk.advance(RENOTIFY - 1)
        self.assertEqual(self.tick([d]), [])
        self.clk.advance(1 + AGGREGATE_WINDOW_S)
        notes = self.tick([d])
        self.assertEqual(len(notes), 1)
        self.assertTrue(notes[0].body.startswith("2nd reminder."),
                        notes[0].body)

    def test_renotify_disabled_by_zero(self):
        self.esc.renotify_s = 0
        d = self._blocked()
        self._first_toast(d)
        self.clk.advance(RENOTIFY * 10)
        self.assertEqual(self.tick([d]), [])

    # -- reason handling ---------------------------------------------

    def test_rate_limited_rolls_back_and_retries_after_30s(self):
        d = self._blocked()
        self.tick([d])
        self.clk.advance(THRESHOLD + AGGREGATE_WINDOW_S)
        note = self.tick([d])[0]
        self.esc.on_result(note, "rate_limited")
        self.clk.advance(29)
        self.assertEqual(self.tick([d]), [])              # still backing off
        self.clk.advance(2)
        retry = self.tick([d])
        self.assertEqual(len(retry), 1)
        # the rollback means the retry is still the *first* toast, not a
        # "2nd reminder"
        self.assertFalse(retry[0].body.startswith("2nd"))

    def test_busy_is_retried_too(self):
        d = self._blocked()
        self.tick([d])
        self.clk.advance(THRESHOLD + AGGREGATE_WINDOW_S)
        self.esc.on_result(self.tick([d])[0], "busy")
        self.clk.advance(31)
        self.assertEqual(len(self.tick([d])), 1)

    def test_disabled_is_not_retried(self):
        d = self._blocked()
        self.tick([d])
        self.clk.advance(THRESHOLD + AGGREGATE_WINDOW_S)
        self.esc.on_result(self.tick([d])[0], "disabled")
        self.clk.advance(60)
        self.assertEqual(self.tick([d]), [])              # quiet until renotify
        self.clk.advance(RENOTIFY + AGGREGATE_WINDOW_S)
        self.assertEqual(len(self.tick([d])), 1)

    def test_no_foreground_client_is_not_retried(self):
        d = self._blocked()
        self.tick([d])
        self.clk.advance(THRESHOLD + AGGREGATE_WINDOW_S)
        self.esc.on_result(self.tick([d])[0], "no_foreground_client")
        self.clk.advance(60)
        self.assertEqual(self.tick([d]), [])

    # -- reset -------------------------------------------------------

    def test_unblocking_resets_everything(self):
        d = self._blocked()
        self._first_toast(d)
        self.tick([desk("p1", status="working", blocked_since=None)])
        self.assertEqual(self.esc.escalated_ids(), frozenset())
        # blocked again: the countdown starts from scratch
        again = desk("p1", blocked_since=self.clk.t)
        self.clk.advance(THRESHOLD - 1)
        self.assertEqual(self.tick([again]), [])
        self.clk.advance(1 + AGGREGATE_WINDOW_S)
        self.assertEqual(len(self.tick([again])), 1)

    def test_vanished_desk_is_forgotten(self):
        d = self._blocked()
        self.tick([d])
        self.clk.advance(THRESHOLD)
        self.tick([d])
        self.assertEqual(self.esc.escalated_ids(), frozenset({"p1"}))
        self.tick([])                                     # pane closed
        self.assertEqual(self.esc.escalated_ids(), frozenset())
        self.clk.advance(AGGREGATE_WINDOW_S)
        self.assertEqual(self.tick([]), [])

    def test_missing_blocked_since_falls_back_to_now(self):
        d = desk("p1", blocked_since=None)
        self.tick([d])
        self.clk.advance(THRESHOLD + AGGREGATE_WINDOW_S)
        self.assertEqual(len(self.tick([d])), 1)

    # -- mute --------------------------------------------------------

    def test_mute_suppresses_and_restarts_the_window(self):
        d = self._blocked()
        self.esc.muted = True
        self.clk.advance(THRESHOLD + AGGREGATE_WINDOW_S * 10)
        self.assertEqual(self.tick([d]), [])
        self.assertEqual(self.esc.escalated_ids(), frozenset({"p1"}))
        self.esc.muted = False
        self.assertEqual(self.tick([d]), [])              # window reopens
        self.clk.advance(AGGREGATE_WINDOW_S)
        self.assertEqual(len(self.tick([d])), 1)

    # -- done --------------------------------------------------------

    def test_done_is_silent_by_default(self):
        self.tick([desk("p1", status="working", blocked_since=None)])
        self.assertEqual(self.tick([desk("p1", status="done")]), [])

    def test_notify_done_opt_in_fires_once_on_transition(self):
        self.esc.notify_done = True
        self.tick([desk("p1", status="working", blocked_since=None)])
        notes = self.tick([desk("p1", status="done")])
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].kind, "done")
        self.assertIn("is done", notes[0].title)
        # staying done must not repeat
        self.assertEqual(self.tick([desk("p1", status="done")]), [])

    def test_notify_done_ignores_desks_already_done_at_startup(self):
        self.esc.notify_done = True
        self.assertEqual(self.tick([desk("p1", status="done")]), [])

    # -- formatting --------------------------------------------------

    def test_name_fn_is_applied(self):
        esc = Escalator(threshold_s=THRESHOLD, now=self.clk,
                        name_fn=lambda d: d.display_name.split("/")[-1])
        d = desk("p1", blocked_since=self.t0, label="a/very/long/name")
        esc.tick([d])
        self.clk.advance(THRESHOLD + AGGREGATE_WINDOW_S)
        self.assertIn("name is waiting", esc.tick([d])[0].title)

    def test_title_and_body_are_clipped(self):
        d = desk("p1", blocked_since=self.t0, label="x" * 200,
                 state_labels={"blocked": "y" * 400})
        self.tick([d])
        self.clk.advance(THRESHOLD + AGGREGATE_WINDOW_S)
        note = self.tick([d])[0]
        self.assertLessEqual(len(note.title), 80)
        self.assertLessEqual(len(note.body), 240)

    def test_sound_comes_from_config(self):
        esc = Escalator(threshold_s=THRESHOLD, sound="none", now=self.clk)
        d = desk("p1", blocked_since=self.t0)
        esc.tick([d])
        self.clk.advance(THRESHOLD + AGGREGATE_WINDOW_S)
        self.assertEqual(esc.tick([d])[0].sound, "none")


class DurationTest(unittest.TestCase):
    def test_formats(self):
        from office.escalator import _duration
        self.assertEqual(_duration(5), "5s")
        self.assertEqual(_duration(95), "1m35s")
        self.assertEqual(_duration(3725), "1h02m")
        self.assertEqual(_duration(-1), "0s")

    def test_ordinals(self):
        from office.escalator import _ordinal
        self.assertEqual([_ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21)],
                         ["1st", "2nd", "3rd", "4th", "11th", "12th", "13th",
                          "21st"])


if __name__ == "__main__":
    unittest.main()
