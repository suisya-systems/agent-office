"""Commander - the I/O half of the interactive actions (design.md section 6).

Enter / `b` (jump) and `a` (filter toggle) used to call `pane.focus` /
`pane.list` inline on the event loop, so a slow or stuck herdr froze the whole
office for the length of a socket timeout - up to ~5s per keypress (issue #12).
This is the same split the Notifier already applies to escalation toasts: the
loop decides *what* to ask for and returns immediately, this thread does the
asking, and the outcome comes back through the shared office queue:

  ("action", (name, result, error))    name is "focus" or "pane_list";
                                       exactly one of result/error is set,
                                       error being the message string.

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

    def focus(self, pane_id):
        """Focus a pane. Reports back as ("action", ("focus", None, error))."""
        self._q.put((FOCUS, pane_id))

    def list_panes(self):
        """Fetch pane.list, reported as ("action", ("pane_list", panes, None))."""
        self._q.put((PANE_LIST, None))

    # -- worker ----------------------------------------------------------

    def _loop(self):
        while True:
            item = self._q.get()
            if item is None:
                return
            name, arg = item
            try:
                if name == FOCUS:
                    protocol.pane_focus(self.sock_path, arg)
                    result = None
                else:
                    result = protocol.pane_list(self.sock_path)
            except Exception as exc:                       # noqa: BLE001
                # The loop owns the wording and the status line; report the
                # failure rather than deciding what the user should read.
                self.out.put(("action", (name, None, "%s" % exc)))
                continue
            self.out.put(("action", (name, result, None)))
