"""Reader-thread drop detection: a socket that has been replaced (deliberate
make-before-break during an S rebuild) must NOT be reported as a drop."""

import queue
import threading
import unittest

from office.subscriber import Subscriber


class FakeSock:
    """Minimal socket stub whose recv signals EOF immediately."""
    def settimeout(self, _):
        pass

    def recv(self, _n):
        return b""


class ReaderDropTest(unittest.TestCase):
    def _make(self):
        return Subscriber("/nonexistent.sock", queue.Queue())

    def test_current_socket_drop_flags_broken(self):
        sub = self._make()
        sock = FakeSock()
        sub._s_sock = sock                            # this reader is current
        broken = threading.Event()
        sub._read_loop(sock, bytearray(), lambda o: None, broken, "_s_sock")
        self.assertTrue(broken.is_set())

    def test_replaced_socket_drop_is_ignored(self):
        sub = self._make()
        old = FakeSock()
        sub._s_sock = FakeSock()                      # a newer socket is current
        broken = threading.Event()
        sub._read_loop(old, bytearray(), lambda o: None, broken, "_s_sock")
        self.assertFalse(broken.is_set())

    def test_stopped_reader_does_not_flag(self):
        sub = self._make()
        sock = FakeSock()
        sub._s_sock = sock
        sub._stop.set()
        broken = threading.Event()
        sub._read_loop(sock, bytearray(), lambda o: None, broken, "_s_sock")
        self.assertFalse(broken.is_set())


if __name__ == "__main__":
    unittest.main()
