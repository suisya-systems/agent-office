"""Notifier - the I/O half of escalation (design.md section 7).

The Escalator decides *what* to send and never touches a socket; this thread
does the sending. Splitting them that way keeps a slow or rate-limited
notification.show off the render loop, which would otherwise stall for the
length of a socket timeout every time a toast went out.

Like the Subscriber and the Reconciler, this owns a thread and reports back
through the shared office queue rather than touching OfficeState or the
Escalator directly, so the retry/rollback bookkeeping still runs single-writer
on the main loop:

  ("notify_result", (Notification, reason))   delivery outcome
  ("log", message)                            transport failure notice
"""

import queue
import threading

from . import protocol


class Notifier:
    def __init__(self, sock_path, out_queue):
        self.sock_path = sock_path
        self.out = out_queue
        self._q = queue.Queue()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="office-notifier")
        self._thread.start()

    def stop(self):
        self._q.put(None)                     # sentinel: drain, then exit
        if self._thread:
            self._thread.join(timeout=2.0)

    def send(self, note):
        """Queue one Notification. Returns immediately."""
        self._q.put(note)

    def _loop(self):
        while True:
            note = self._q.get()
            if note is None:
                return
            try:
                reason = protocol.notification_show(
                    self.sock_path, note.title, note.body, note.sound)
            except Exception as exc:                       # noqa: BLE001
                # A transport failure is reported as a retryable reason so the
                # Escalator rolls the batch back rather than counting it sent.
                reason = "error"
                self.out.put(("log", "toast failed: %s" % exc))
            self.out.put(("notify_result", (note, reason)))
