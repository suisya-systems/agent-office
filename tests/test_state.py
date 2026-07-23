"""Unit tests for the pure OfficeState model (design.md section 4)."""

import unittest

from office.state import Desk, OfficeState


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def pane(pid, ws="w1", tab="w1:t1", agent="claude", status="idle", **kw):
    d = {"pane_id": pid, "workspace_id": ws, "tab_id": tab,
         "agent": agent, "agent_status": status}
    d.update(kw)
    return d


class DisplayNameTest(unittest.TestCase):
    def test_priority(self):
        d = Desk("p1", "w1")
        self.assertEqual(d.display_name, "p1")            # fallback to id
        d.agent = "claude"
        self.assertEqual(d.display_name, "claude")
        d.terminal_title = "my title"
        self.assertEqual(d.display_name, "my title")
        d.label = "the-label"
        self.assertEqual(d.display_name, "the-label")
        d.display_agent = "Display"
        self.assertEqual(d.display_name, "Display")

    def test_state_label_word(self):
        d = Desk("p1", "w1", status="blocked",
                 state_labels={"blocked": "waiting for approval"})
        self.assertEqual(d.state_label_word, "waiting")
        d.status = "idle"
        self.assertIsNone(d.state_label_word)


class MembershipTest(unittest.TestCase):
    def test_filter_agents_excludes_non_agent(self):
        s = OfficeState(filter_mode="agents")
        s.ingest_pane(pane("p1", agent=None, status="unknown"))
        self.assertEqual(len(s.desks), 0)
        s.ingest_pane(pane("p2", agent="claude"))
        self.assertEqual(len(s.desks), 1)

    def test_filter_all_includes_non_agent(self):
        s = OfficeState(filter_mode="all")
        s.ingest_pane(pane("p1", agent=None, status="unknown"))
        self.assertEqual(len(s.desks), 1)

    def test_self_pane_excluded(self):
        s = OfficeState(self_pane_id="self", filter_mode="all")
        s.ingest_pane(pane("self"))
        self.assertEqual(len(s.desks), 0)

    def test_agent_released_removes_desk_under_agents_filter(self):
        s = OfficeState(filter_mode="agents")
        s.ingest_pane(pane("p1", agent="claude"))
        self.assertIn("p1", s.desks)
        s.ingest_pane(pane("p1", agent=None))            # agent released
        self.assertNotIn("p1", s.desks)

    def test_remove_pane(self):
        s = OfficeState()
        s.ingest_pane(pane("p1"))
        s.remove_pane("p1")
        self.assertEqual(len(s.desks), 0)

    def test_full_update_clears_released_agent_under_all(self):
        s = OfficeState(filter_mode="all")
        s.ingest_pane(pane("p1", agent="claude"))     # no title/label -> "claude"
        self.assertEqual(s.desks["p1"].display_name, "claude")
        # authoritative full PaneInfo with the agent released (key omitted)
        s.ingest_pane({"pane_id": "p1", "workspace_id": "w1", "tab_id": "w1:t1",
                       "agent_status": "unknown"})
        self.assertIsNone(s.desks["p1"].agent)
        self.assertEqual(s.desks["p1"].display_name, "p1")   # falls back to id

    def test_agent_detected_partial_does_not_clear_agent(self):
        s = OfficeState(filter_mode="all")
        s.ingest_pane(pane("p1", agent="claude"))
        # a partial upsert (no agent_status, no agent) must not wipe the agent
        s.ingest_pane({"pane_id": "p1", "workspace_id": "w1"})
        self.assertEqual(s.desks["p1"].agent, "claude")


class StatusTimingTest(unittest.TestCase):
    def setUp(self):
        self.clk = FakeClock()
        self.s = OfficeState(now=self.clk)

    def test_status_since_moves_only_on_change(self):
        self.s.ingest_pane(pane("p1", status="idle"))
        t0 = self.s.desks["p1"].status_since
        self.clk.advance(5)
        self.s.ingest_pane(pane("p1", status="idle"))     # unchanged
        self.assertEqual(self.s.desks["p1"].status_since, t0)
        self.clk.advance(5)
        self.s.set_status("p1", "working")
        self.assertEqual(self.s.desks["p1"].status_since, 1010.0)

    def test_blocked_since_set_and_preserved_and_cleared(self):
        self.s.ingest_pane(pane("p1", status="working"))
        self.clk.advance(1)
        self.s.set_status("p1", "blocked")
        bs = self.s.desks["p1"].blocked_since
        self.assertEqual(bs, 1001.0)
        self.clk.advance(10)
        self.s.set_status("p1", "blocked")                # idempotent re-report
        self.assertEqual(self.s.desks["p1"].blocked_since, bs)
        self.s.set_status("p1", "idle")
        self.assertIsNone(self.s.desks["p1"].blocked_since)

    def test_unknown_status_normalized(self):
        self.s.set_status("p1", "bogus", agent="claude", workspace_id="w1")
        self.assertEqual(self.s.desks["p1"].status, "unknown")

    def test_status_for_unknown_pane_without_agent_ignored_under_agents(self):
        self.s.set_status("ghost", "working", agent=None, workspace_id="w1")
        self.assertNotIn("ghost", self.s.desks)


class OrderingTest(unittest.TestCase):
    def test_ordered_by_ws_tab_pane(self):
        s = OfficeState()
        s.ingest_pane(pane("w2:p1", ws="w2", tab="w2:t1"))
        s.ingest_pane(pane("w1:p2", ws="w1", tab="w1:t1"))
        s.ingest_pane(pane("w1:p1", ws="w1", tab="w1:t1"))
        order = [d.pane_id for d in s.ordered_desks()]
        self.assertEqual(order, ["w1:p1", "w1:p2", "w2:p1"])

    def test_islands_group_and_label(self):
        s = OfficeState()
        s.ingest_pane(pane("w1:p1", ws="w1"))
        s.ingest_pane(pane("w2:p1", ws="w2"))
        s.set_room_label("w1", "room-one")
        islands = s.islands()
        self.assertEqual([i[0] for i in islands], ["w1", "w2"])
        self.assertEqual(islands[0][1], "room-one")
        self.assertEqual(islands[1][1], "w2")             # falls back to id

    def test_pane_move_rehomes_desk(self):
        # a pane_moved upsert carries the same pane_id with a new workspace/tab
        s = OfficeState()
        s.ingest_pane(pane("p1", ws="w1", tab="w1:t1"))
        self.assertEqual(s.islands()[0][0], "w1")
        s.ingest_pane(pane("p1", ws="w2", tab="w2:t1"))   # moved to w2
        self.assertEqual(len(s.desks), 1)
        self.assertEqual(s.desks["p1"].workspace_id, "w2")
        self.assertEqual(s.islands()[0][0], "w2")

    def test_remove_room_drops_desks(self):
        s = OfficeState()
        s.ingest_pane(pane("w1:p1", ws="w1"))
        s.set_room_label("w1", "r1")
        s.remove_room("w1")
        self.assertEqual(len(s.desks), 0)
        self.assertNotIn("w1", s.rooms)


class BlockedTest(unittest.TestCase):
    def setUp(self):
        self.clk = FakeClock()
        self.s = OfficeState(now=self.clk)

    def _block(self, pid):
        self.s.ingest_pane(pane(pid, status="working"))
        self.s.set_status(pid, "blocked")

    def test_blocked_order_oldest_first(self):
        self._block("p1")
        self.clk.advance(5)
        self._block("p2")
        order = [d.pane_id for d in self.s.blocked_desks()]
        self.assertEqual(order, ["p1", "p2"])
        self.assertEqual(self.s.oldest_blocked().pane_id, "p1")

    def test_blocked_tiebreak_by_pane_id(self):
        # both blocked at the same instant -> pane_id tiebreak
        self.s.ingest_pane(pane("pB", status="working"))
        self.s.ingest_pane(pane("pA", status="working"))
        self.s.set_status("pB", "blocked")
        self.s.set_status("pA", "blocked")
        self.assertEqual(self.s.oldest_blocked().pane_id, "pA")


class SelectionTest(unittest.TestCase):
    def setUp(self):
        self.s = OfficeState()
        for i in range(6):
            self.s.ingest_pane(pane("w1:p%d" % i, tab="w1:t1"))

    def test_first_desk_auto_selected(self):
        self.assertEqual(self.s.selected_pane_id, "w1:p0")

    def test_move_selection_grid(self):
        self.s.move_selection(1, 0, per_row=3)            # -> p1
        self.assertEqual(self.s.selected_pane_id, "w1:p1")
        self.s.move_selection(0, 1, per_row=3)            # down a row -> p4
        self.assertEqual(self.s.selected_pane_id, "w1:p4")
        self.s.move_selection(0, 1, per_row=3)            # clamp at end
        self.assertEqual(self.s.selected_pane_id, "w1:p5")

    def test_move_clamps_low(self):
        self.s.move_selection(-5, 0, per_row=3)
        self.assertEqual(self.s.selected_pane_id, "w1:p0")

    def test_selection_fixed_after_removal(self):
        self.s.select("w1:p3")
        self.s.remove_pane("w1:p3")
        self.assertEqual(self.s.selected_pane_id, "w1:p0")

    def test_select_next_blocked_cycles(self):
        self.s.set_status("w1:p1", "blocked")
        self.s.set_status("w1:p4", "blocked")
        first = self.s.select_next_blocked().pane_id
        second = self.s.select_next_blocked().pane_id
        third = self.s.select_next_blocked().pane_id
        self.assertEqual([first, second], ["w1:p1", "w1:p4"])
        self.assertEqual(third, "w1:p1")                  # wraps around


class ReconcileTest(unittest.TestCase):
    def test_reconcile_drops_absent(self):
        s = OfficeState()
        s.ingest_pane(pane("p1"))
        s.ingest_pane(pane("p2"))
        s.reconcile_snapshot([pane("p2"), pane("p3")])
        self.assertEqual(sorted(s.desks), ["p2", "p3"])


if __name__ == "__main__":
    unittest.main()
