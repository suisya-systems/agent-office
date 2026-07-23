"""Reconciler - periodic authoritative re-sync (design.md sections 3 and 4).

Event subscription (Subscriber, connections L/S) is the primary path and stays
that way. But some state changes emit no event at all - notably TTL expiry of a
reported agent, where a pane simply stops being listed with no
pane.closed/pane.exited. Without a periodic authoritative check those desks
linger forever as "ghosts" (the 2026-07-24 dogfood SMOKE ghost, issue #1).

This thread fetches a full pane.list on a fixed interval (default 60s) and pushes
it onto the same out queue as a ("snapshot", panes) item, so it flows through the
identical, idempotent reconcile path the Subscriber already uses on (re)connect.
The main loop applies it single-writer via OfficeState.reconcile_snapshot, which
drops desks whose pane is absent and overwrites status/label/agent for the rest.
A failed fetch is logged and retried on the next tick; the thread never touches
OfficeState directly, so it cannot corrupt state or block redraws.
"""

import threading

from . import protocol

RECONCILE_INTERVAL_S = 60.0


class Reconciler:
    def __init__(self, sock_path, out_queue, interval_s=RECONCILE_INTERVAL_S):
        self.sock_path = sock_path
        self.out = out_queue
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="office-reconciler")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def _loop(self):
        # wait() returns True once stopped, False on interval timeout; so the
        # first reconcile happens one interval in (startup already snapshots via
        # the Subscriber) and the loop exits promptly on stop.
        while not self._stop.wait(self.interval_s):
            self._reconcile_once()

    def _reconcile_once(self):
        try:
            panes = protocol.pane_list(self.sock_path)
        except Exception as exc:                          # noqa: BLE001
            # herdr unreachable or slow: keep the last-known desks and retry on
            # the next tick rather than blanking the office.
            self.out.put(("log", "reconcile failed: %s" % exc))
            return
        self.out.put(("snapshot", panes))
