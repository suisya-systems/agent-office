"""Regression for issue #1: a pane that vanishes with no pane.closed/pane.exited
event (e.g. TTL expiry of a reported agent) must stop being a ghost desk within
the reconcile interval. The periodic Reconciler fetches an authoritative
pane.list and feeds it through the same snapshot path OfficeState applies to drop
absent desks - so ghosts clear even when no lifecycle event ever fires."""

import queue
import unittest

from office import protocol
from office.reconciler import Reconciler
from office.state import OfficeState


def _pane(pid, status="idle"):
    return {"pane_id": pid, "workspace_id": "w1", "tab_id": "w1:t1",
            "agent": "claude", "agent_status": status}


class ReconcilerTest(unittest.TestCase):
    def setUp(self):
        self._orig_pane_list = protocol.pane_list

    def tearDown(self):
        protocol.pane_list = self._orig_pane_list

    def _drain_first(self, q):
        # A tiny interval can queue several ticks before stop(); the first item
        # is representative.
        return q.get(timeout=2.0)

    def test_periodic_snapshot_drops_ghost_without_close_event(self):
        # herdr now lists only p2; p1 vanished silently (TTL expiry) - no
        # pane.closed/exited was ever delivered, so events alone leave it behind.
        live = [_pane("p2")]
        protocol.pane_list = lambda sock, timeout=5.0: live

        q = queue.Queue()
        rec = Reconciler("/nonexistent.sock", q, interval_s=0.02)
        rec.start()
        try:
            kind, payload = self._drain_first(q)
        finally:
            rec.stop()
        self.assertEqual(kind, "snapshot")

        # Applying that authoritative snapshot is exactly what the office loop
        # does for a ("snapshot", ...) item; it must evict the ghost p1.
        st = OfficeState()
        st.ingest_pane(_pane("p1", status="blocked"))
        st.ingest_pane(_pane("p2"))
        self.assertIn("p1", st.desks)
        st.reconcile_snapshot(payload)
        self.assertNotIn("p1", st.desks)              # ghost gone within one tick
        self.assertIn("p2", st.desks)

    def test_snapshot_reoverwrites_stale_status(self):
        # A missed pane.agent_status_changed left a stale desk; the periodic
        # snapshot carries the authoritative status and overwrites it.
        protocol.pane_list = lambda sock, timeout=5.0: [_pane("p1", status="idle")]
        q = queue.Queue()
        rec = Reconciler("/nonexistent.sock", q, interval_s=0.02)
        rec.start()
        try:
            kind, payload = self._drain_first(q)
        finally:
            rec.stop()

        st = OfficeState()
        st.ingest_pane(_pane("p1", status="blocked"))
        self.assertEqual(st.desks["p1"].status, "blocked")
        st.reconcile_snapshot(payload)
        self.assertEqual(st.desks["p1"].status, "idle")

    def test_fetch_failure_is_logged_not_fatal(self):
        def boom(sock, timeout=5.0):
            raise ConnectionError("server down")
        protocol.pane_list = boom

        q = queue.Queue()
        rec = Reconciler("/nonexistent.sock", q, interval_s=0.02)
        rec.start()
        try:
            kind, payload = self._drain_first(q)
        finally:
            rec.stop()
        # A failed fetch must not blank the office: it logs and keeps last-known
        # desks (no "snapshot" is emitted for the failed tick).
        self.assertEqual(kind, "log")
        self.assertIn("reconcile failed", payload)


if __name__ == "__main__":
    unittest.main()
