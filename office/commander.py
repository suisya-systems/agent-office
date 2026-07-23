"""Commander - the I/O half of the interactive actions (design.md section 6).

Enter / `b` (jump) and `a` (filter toggle) used to call `pane.focus` /
`pane.list` inline on the event loop, so a slow or stuck herdr froze the whole
office for the length of a socket timeout - up to ~5s per keypress (issue #12).
This is the same split the Notifier already applies to escalation toasts: the
loop decides *what* to ask for and returns immediately, this thread does the
asking, and the outcome comes back through the shared office queue:

  ("action", (name, result, error, token))
                                       name is "focus" or "pane_list"; at most
                                       one of result/error is set, error being
                                       the message string; token is whatever
                                       the caller attached to the request, so
                                       overlapping requests stay told apart.

Deliberately a second thread rather than a queue shared with the Notifier: a
toast can sit in a rate-limit or a retry for a while, and a keypress must not
wait behind it. Like the other feeder threads it never touches OfficeState -
the loop stays the single writer, so applying a refreshed pane.list still runs
through the same reconcile path as the Reconciler's periodic snapshot.
"""

import queue
import threading

from . import protocol

FOCUS = "focus"
PANE_LIST = "pane_list"


class Commander:
    def __init__(self, sock_path, out_queue):
        self.sock_path = sock_path
        self.out = out_queue
        self._q = queue.Queue()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="office-commander")
        self._thread.start()

    def stop(self):
        self._q.put(None)                     # sentinel: drain, then exit
        if self._thread:
            self._thread.join(timeout=2.0)

    # -- requests (return immediately) -----------------------------------

    def focus(self, pane_id, token=None):
        """Focus a pane, reported as ("focus", None, error, token)."""
        self._q.put((FOCUS, pane_id, token))

    def list_panes(self, token=None):
        """Fetch pane.list, reported as ("pane_list", panes, error, token).

        The token comes back untouched: the caller decides what a request has
        to remember about itself, and two refreshes in flight at once cannot
        be confused for one another.
        """
        self._q.put((PANE_LIST, None, token))

    # -- worker ----------------------------------------------------------

    def _loop(self):
        while True:
            item = self._q.get()
            if item is None:
                return
            name, pane_id, token = item
            try:
                if name == FOCUS:
                    protocol.pane_focus(self.sock_path, pane_id)
                    result = None
                else:
                    result = protocol.pane_list(self.sock_path)
            except Exception as exc:                       # noqa: BLE001
                # The loop owns the wording and the status line; report the
                # failure rather than deciding what the user should read.
                self.out.put(("action", (name, None, "%s" % exc, token)))
                continue
            self.out.put(("action", (name, result, None, token)))
