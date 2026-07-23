"""Commander: keypress-driven socket calls run off the loop thread (issue #12).

The socket itself is faked at the protocol module, so these exercise the queue
contract - what the worker reports back, and that asking costs the caller
nothing - without a herdr server.
"""

import queue
import threading
import time
import unittest

from office import commander as commander_mod
from office.commander import Commander


class FakeProtocol:
    """Stands in for office.protocol, optionally slow, optionally failing."""

    def __init__(self, block=False, error=None, panes=None):
        self.block = block
        self.error = error
        self.panes = panes if panes is not None else []
        self.focused = []
        self.list_calls = 0
        self.entered = threading.Event()      # the worker reached the socket
        self.release = threading.Event()      # ...and may now come back

    def _work(self):
        self.entered.set()
        if self.block:
            self.release.wait(5.0)            # a stuck herdr, without the wall
        if self.error:
            raise self.error

    def pane_focus(self, _sock, pane_id, timeout=5.0):
        self.focused.append(pane_id)
        self._work()

    def pane_list(self, _sock, timeout=5.0):
        self.list_calls += 1
        self._work()
        return self.panes


class CommanderTest(unittest.TestCase):
    def setUp(self):
        self.real = commander_mod.protocol
        self.out = queue.Queue()
        self.commander = None
        self.fake = None

    def tearDown(self):
        commander_mod.protocol = self.real
        if self.fake:
            self.fake.release.set()           # let a blocked worker finish
        if self.commander:
            self.commander.stop()

    def start(self, fake):
        commander_mod.protocol = self.fake = fake
        self.commander = Commander("/nonexistent.sock", self.out)
        self.commander.start()
        return self.commander

    def result(self, timeout=2.0):
        kind, payload = self.out.get(timeout=timeout)
        self.assertEqual(kind, "action")
        return payload

    def test_focus_reports_success_with_no_error(self):
        fake = FakeProtocol()
        self.start(fake).focus("pane-7")
        name, result, error, token = self.result()
        self.assertEqual((name, result, error, token),
                         ("focus", None, None, None))
        self.assertEqual(fake.focused, ["pane-7"])

    def test_focus_failure_comes_back_as_a_message(self):
        self.start(FakeProtocol(error=RuntimeError("herdr is down"))).focus("p1")
        name, _result, error, _token = self.result()
        self.assertEqual(name, "focus")
        self.assertIn("herdr is down", error)

    def test_pane_list_carries_the_panes_back(self):
        panes = [{"pane_id": "p1"}, {"pane_id": "p2"}]
        self.start(FakeProtocol(panes=panes)).list_panes(token=7)
        name, result, error, token = self.result()
        self.assertEqual((name, result, error, token),
                         ("pane_list", panes, None, 7))

    def test_pane_list_failure_is_reported_not_raised(self):
        self.start(FakeProtocol(error=OSError("timed out"))).list_panes()
        name, result, error, _token = self.result()
        self.assertEqual((name, result), ("pane_list", None))
        self.assertIn("timed out", error)

    def test_the_worker_survives_a_failed_call(self):
        # A one-off failure must not kill the thread: the next keypress still
        # has to reach herdr.
        fake = FakeProtocol(error=RuntimeError("boom"))
        commander = self.start(fake)
        commander.focus("p1")
        self.result()
        fake.error = None
        commander.focus("p2")
        _name, _result, error, _token = self.result()
        self.assertIsNone(error)
        self.assertEqual(fake.focused, ["p1", "p2"])

    def test_requests_do_not_block_the_caller(self):
        fake = FakeProtocol(block=True)
        commander = self.start(fake)
        started = time.monotonic()
        for _ in range(5):
            commander.focus("p1")
        elapsed = time.monotonic() - started
        self.assertTrue(fake.entered.wait(1.0))    # the worker really is on it
        self.assertLess(elapsed, 0.2, "queueing waited on the socket")


if __name__ == "__main__":
    unittest.main()
