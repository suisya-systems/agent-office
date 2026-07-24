"""The connection layer, including the Windows branch, from a unix CI.

Windows is where this code is hardest to get right and hardest to run, so
every named-pipe behaviour that was established by measuring a real herdr is
pinned down here with fakes instead: the busy-open retry, the socket-shaped
adapter over a pipe handle, and the marker-file trap.

That last one is the reason the health check exists. On Windows herdr keeps a
25-byte `pid:timestamp` marker file at exactly the path it also uses as its
pipe name, so connecting to the path instead of the pipe *succeeds* - it just
hands back the marker. Opening it "r+b" and writing a request into it would
overwrite herdr's own liveness marker, which is why the check has to happen
before a single byte goes out.

The request/subscribe tests cover the two failure modes that are silent rather
than loud: an answer that belongs to a different request being taken for ours,
and a subscription that is reported as open but will never deliver an event.
"""

import errno
import itertools
import json
import os
import queue
import threading
import unittest

from office import protocol


class FakePipe:
    """A pipe handle: hands out queued bytes, records what was written."""

    def __init__(self, chunks=(), write_limit=None):
        self.inbox = bytearray(b"".join(chunks))
        self.written = bytearray()
        self.closed = False
        self.close_calls = 0
        self.write_limit = write_limit

    def read(self, size):
        if self.closed:
            raise ValueError("read of closed file")
        data = bytes(self.inbox[:size])
        del self.inbox[:size]
        return data

    def write(self, data):
        if self.closed:
            raise ValueError("write to closed file")
        chunk = bytes(data)
        if self.write_limit is not None:
            chunk = chunk[:self.write_limit]
        self.written.extend(chunk)
        return len(chunk)

    def close(self):
        self.close_calls += 1
        self.closed = True

    def fileno(self):
        return -1


def _peek(handle):
    return len(handle.inbox)


def _conn(handle, timeout=None, sleep=None, monotonic=None):
    clock = [0.0]

    def tick(seconds):
        clock[0] += seconds
    return protocol.PipeConnection(
        handle, timeout=timeout, peek=_peek,
        sleep=sleep or tick,
        monotonic=monotonic or (lambda: clock[0]))


class PipeNameTest(unittest.TestCase):
    def test_the_reported_path_becomes_a_pipe_name(self):
        self.assertEqual(
            protocol.pipe_name(r"C:\Users\a\AppData\Roaming\herdr\herdr.sock"),
            r"\\.\pipe\C:\Users\a\AppData\Roaming\herdr\herdr.sock")

    def test_an_existing_pipe_name_is_left_alone(self):
        name = r"\\.\pipe\herdr.sock"
        self.assertEqual(protocol.pipe_name(name), name)


class PipeConnectionTest(unittest.TestCase):
    def test_recv_returns_what_is_waiting(self):
        conn = _conn(FakePipe([b"hello\n"]))
        self.assertEqual(conn.recv(65536), b"hello\n")

    def test_recv_never_returns_more_than_asked_for(self):
        conn = _conn(FakePipe([b"abcdef"]))
        self.assertEqual(conn.recv(3), b"abc")

    def test_sendall_resends_what_the_pipe_would_not_take(self):
        """A pipe write is free to accept only part of the buffer.

        socket.sendall loops for its caller; a raw handle's write does not, so
        a large pane.graphics.set would silently lose its tail without this.
        """
        handle = FakePipe(write_limit=1)
        _conn(handle).sendall(b"abcdef")
        self.assertEqual(bytes(handle.written), b"abcdef")

    def test_a_broken_pipe_reads_as_end_of_stream(self):
        """PeekNamedPipe raises ERROR_BROKEN_PIPE where a socket returns b""."""
        conn = protocol.PipeConnection(
            FakePipe(), timeout=1.0,
            peek=lambda _h: (_ for _ in ()).throw(OSError(errno.EPIPE, "gone")),
            sleep=lambda _s: None, monotonic=lambda: 0.0)
        self.assertEqual(conn.recv(4096), b"")

    def test_recv_times_out_as_an_oserror(self):
        """The reader thread only catches OSError, so this must be one."""
        clock = [0.0]
        conn = protocol.PipeConnection(
            FakePipe(), timeout=0.5, peek=_peek,
            sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
            monotonic=lambda: clock[0])
        with self.assertRaises(OSError):
            conn.recv(4096)

    def test_an_untimed_recv_waits_for_the_data(self):
        handle = FakePipe()
        conn = _conn(handle, timeout=None,
                     sleep=lambda _s: handle.inbox.extend(b"late\n"))
        self.assertEqual(conn.recv(4096), b"late\n")

    def test_closing_releases_a_waiting_reader(self):
        """The reason recv polls instead of blocking.

        Closing a pipe handle that another thread is inside ReadFile on hangs
        the closing thread too (measured on Windows), so the office would
        deadlock on shutdown. Polling means close() only has to set a flag the
        reader is already looking at.
        """
        handle = FakePipe()
        conn = protocol.PipeConnection(handle, timeout=None, peek=_peek,
                                       sleep=lambda _s: None)
        reading = threading.Event()
        result = queue.Queue()

        def read():
            reading.set()
            result.put(conn.recv(4096))

        thread = threading.Thread(target=read)
        thread.start()
        self.assertTrue(reading.wait(1.0))
        conn.close()
        thread.join(2.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result.get(timeout=1.0), b"")

    def test_close_is_idempotent(self):
        handle = FakePipe()
        conn = _conn(handle)
        conn.close()
        conn.close()
        self.assertEqual(handle.close_calls, 1)

    def test_reading_a_closed_connection_is_end_of_stream(self):
        conn = _conn(FakePipe([b"unread"]))
        conn.close()
        self.assertEqual(conn.recv(4096), b"")


class ConnectWindowsTest(unittest.TestCase):
    """herdr keeps one listening pipe instance up, so connects collide."""

    def setUp(self):
        self.clock = [0.0]
        self.slept = []

    def _sleep(self, seconds):
        self.slept.append(seconds)
        self.clock[0] += seconds

    def _connect(self, opener, is_pipe=lambda _h: True, timeout=5.0):
        return protocol._connect_windows(
            "C:\\herdr.sock", timeout, opener=opener, is_pipe=is_pipe,
            peek=_peek, sleep=self._sleep, monotonic=lambda: self.clock[0])

    def test_a_busy_pipe_is_retried_until_it_opens(self):
        """ERROR_PIPE_BUSY arrives as a bare EINVAL with no winerror set.

        About a third of connections hit it in practice, so without the retry
        the office would fail to reconnect roughly every third attempt.
        """
        handle = FakePipe()
        attempts = []

        def opener(name, mode, buffering=0):
            attempts.append(name)
            if len(attempts) < 3:
                raise OSError(errno.EINVAL, "Invalid argument")
            return handle

        conn = self._connect(opener)
        self.assertIsInstance(conn, protocol.PipeConnection)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(len(self.slept), 2)
        self.assertEqual(attempts[0], r"\\.\pipe\C:\herdr.sock")

    def test_a_missing_pipe_fails_at_once(self):
        """herdr not running is not a collision - waiting would just stall."""
        def opener(_name, _mode, buffering=0):
            raise FileNotFoundError(errno.ENOENT, "no such file")

        with self.assertRaises(FileNotFoundError):
            self._connect(opener)
        self.assertEqual(self.slept, [])

    def test_retrying_gives_up_on_the_deadline(self):
        def opener(_name, _mode, buffering=0):
            raise OSError(errno.EINVAL, "Invalid argument")

        with self.assertRaises(OSError):
            self._connect(opener)
        self.assertGreaterEqual(self.clock[0], protocol.BUSY_RETRY_MAX_S)

    def test_an_unrelated_oserror_is_not_retried(self):
        def opener(_name, _mode, buffering=0):
            raise OSError(errno.EACCES, "denied")

        with self.assertRaises(OSError):
            self._connect(opener)
        self.assertEqual(self.slept, [])

    def test_the_marker_file_is_rejected_before_anything_is_written(self):
        """Writing NDJSON into it would destroy herdr's liveness marker."""
        handle = FakePipe([b"25080:1784860611089550300"])
        with self.assertRaises(protocol.ProtocolError) as caught:
            self._connect(lambda *_a, **_k: handle, is_pipe=lambda _h: False)
        self.assertEqual(caught.exception.code, "not_a_pipe")
        self.assertTrue(handle.closed)
        self.assertEqual(bytes(handle.written), b"")
        self.assertEqual(self.slept, [])


class ConnectBranchTest(unittest.TestCase):
    def setUp(self):
        self.real = protocol._connect_windows
        self.addCleanup(setattr, protocol, "_connect_windows", self.real)
        self.windows_calls = []
        protocol._connect_windows = lambda *a, **k: self.windows_calls.append(a)

    @unittest.skipIf(os.name == "nt", "no AF_UNIX to connect to on Windows")
    def test_unix_never_reaches_the_pipe_path(self):
        """The busy retry must not see a unix socket's own EINVAL.

        EINVAL means "busy" only for a named pipe; on a unix socket it is a
        genuine error, so the retry lives in the Windows branch alone.
        """
        with self.assertRaises(OSError):
            protocol.connect("/nonexistent.sock", 0.1, windows=False)
        self.assertEqual(self.windows_calls, [])

    def test_windows_takes_the_pipe_path(self):
        protocol.connect("C:\\herdr.sock", 0.1, windows=True)
        self.assertEqual(len(self.windows_calls), 1)


class FakeConnection:
    """A connection whose reads are scripted, for the request-level tests."""

    def __init__(self, lines=(), fail_send=None):
        self.inbox = bytearray(b"".join(
            json.dumps(line).encode("utf-8") + b"\n" for line in lines))
        self.sent = bytearray()
        self.closed = 0
        self.fail_send = fail_send

    def sendall(self, data):
        if self.fail_send:
            raise self.fail_send
        self.sent.extend(data)

    def recv(self, size):
        data = bytes(self.inbox[:size])
        del self.inbox[:size]
        return data

    def settimeout(self, _value):
        pass

    def close(self):
        self.closed += 1


class RequestTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, protocol, "connect", protocol.connect)
        # Request ids are drawn from a process-wide counter; pin it so the
        # expected id below does not depend on what ran before.
        self.addCleanup(setattr, protocol, "_next_seq", protocol._next_seq)
        protocol._next_seq = itertools.count(1)

    def _serve(self, conn):
        protocol.connect = lambda _path, _timeout=5.0, **_k: conn
        return conn

    def test_a_reply_to_another_request_is_stepped_over(self):
        """Every request used to answer to the same id, so nothing could tell
        a stale reply left in a reused pipe instance from its own."""
        conn = self._serve(FakeConnection([
            {"id": "someone-else", "result": {"panes": ["wrong"]}},
            {"id": "office-1", "result": {"panes": ["right"]}},
        ]))
        self.assertEqual(protocol.request("/s", "pane.list"),
                         {"panes": ["right"]})
        self.assertEqual(conn.closed, 1)

    def test_a_reply_without_an_id_is_still_accepted(self):
        self._serve(FakeConnection([{"result": {"ok": True}}]))
        self.assertEqual(protocol.request("/s", "pane.list"), {"ok": True})

    def test_an_event_line_is_not_mistaken_for_the_reply(self):
        conn = self._serve(FakeConnection([
            {"event": "pane.focused", "data": {"pane_id": "w1:p1"}},
            {"result": {"panes": []}},
        ]))
        self.assertEqual(protocol.request("/s", "pane.list"), {"panes": []})
        self.assertEqual(conn.closed, 1)

    def test_an_error_herdr_could_not_name_is_still_ours(self):
        """herdr answers `"id": ""` when the request failed to deserialize.

        Matching the id exactly dropped that line, and since herdr closes the
        connection right after an error, the next read raised "connection
        closed" - so a precise `invalid_request: missing field pane_id` came
        back as a bare disconnect, on the platform with the least other
        diagnostics available.
        """
        conn = self._serve(FakeConnection([
            {"id": "", "error": {"code": "invalid_request",
                                 "message": "missing field pane_id"}}]))
        with self.assertRaises(protocol.ProtocolError) as caught:
            protocol.request("/s", "pane.focus")
        self.assertEqual(caught.exception.code, "invalid_request")
        self.assertEqual(conn.closed, 1)

    def test_an_error_reply_raises_and_still_closes(self):
        conn = self._serve(FakeConnection([
            {"error": {"code": "pane_not_found", "message": "gone"}}]))
        with self.assertRaises(protocol.ProtocolError) as caught:
            protocol.request("/s", "pane.focus")
        self.assertEqual(caught.exception.code, "pane_not_found")
        self.assertEqual(conn.closed, 1)

    def test_a_dropped_connection_still_closes(self):
        conn = self._serve(FakeConnection([]))
        with self.assertRaises(ConnectionError):
            protocol.request("/s", "pane.list")
        self.assertEqual(conn.closed, 1)


class OpenSubscriptionTest(unittest.TestCase):
    def setUp(self):
        self.real = protocol.connect
        self.addCleanup(setattr, protocol, "connect", self.real)

    def _serve(self, conn):
        protocol.connect = lambda _path, _timeout=5.0, **_k: conn
        return conn

    def _open(self):
        return protocol.open_subscription("/s", [{"type": "pane.created"}],
                                          req_id="office-L")

    def test_a_good_ack_hands_back_the_connection(self):
        conn = self._serve(FakeConnection([
            {"id": "office-L", "result": {"type": "subscription_started"}}]))
        got, buf = self._open()
        self.assertIs(got, conn)
        self.assertEqual(bytes(buf), b"")
        self.assertEqual(conn.closed, 0)

    def test_an_ack_that_is_not_subscription_started_is_refused(self):
        """Anything else meant a live connection that never delivered an event
        and a fleet view frozen behind a cheerful "connected"."""
        conn = self._serve(FakeConnection([
            {"id": "office-L", "result": {"type": "something_else"}}]))
        with self.assertRaises(protocol.ProtocolError) as caught:
            self._open()
        self.assertEqual(caught.exception.code, "bad_ack")
        self.assertEqual(conn.closed, 1)

    def test_a_rejected_subscription_keeps_its_reason(self):
        """herdr names the offending subscription, not the request.

        A stale pane_id is the failure mode subscriber.py is written around -
        it re-subscribes from a fresh pane.list precisely because one can go
        away underneath it - so `pane_not_found` is the line that has to
        survive to the log.
        """
        conn = self._serve(FakeConnection([
            {"id": "office-L:sub:0:probe",
             "error": {"code": "pane_not_found",
                       "message": "pane w1:p9 not found"}}]))
        with self.assertRaises(protocol.ProtocolError) as caught:
            self._open()
        self.assertEqual(caught.exception.code, "pane_not_found")
        self.assertEqual(conn.closed, 1)

    def test_an_error_ack_closes_the_connection(self):
        conn = self._serve(FakeConnection([
            {"id": "office-L",
             "error": {"code": "invalid_request", "message": "bad"}}]))
        with self.assertRaises(protocol.ProtocolError):
            self._open()
        self.assertEqual(conn.closed, 1)

    def test_a_send_failure_closes_the_connection(self):
        """On Windows a leaked connection holds the pipe instance that the
        reconnect loop is queueing for, so a failure has to give it back."""
        conn = self._serve(FakeConnection([], fail_send=BrokenPipeError()))
        with self.assertRaises(BrokenPipeError):
            self._open()
        self.assertEqual(conn.closed, 1)

    def test_a_dropped_ack_closes_the_connection(self):
        conn = self._serve(FakeConnection([]))
        with self.assertRaises(ConnectionError):
            self._open()
        self.assertEqual(conn.closed, 1)

    def test_a_non_dict_ack_is_refused_rather_than_crashing(self):
        conn = self._serve(FakeConnection([
            {"id": "office-L", "result": "subscription_started"}]))
        with self.assertRaises(protocol.ProtocolError):
            self._open()
        self.assertEqual(conn.closed, 1)

    def test_events_that_beat_the_ack_are_kept_for_the_reader(self):
        conn = self._serve(FakeConnection([
            {"data": {"type": "pane_created", "pane": {"pane_id": "w1:p1"}}},
            {"id": "office-L", "result": {"type": "subscription_started"}},
        ]))
        _got, buf = self._open()
        self.assertIn(b"pane_created", bytes(buf))
        self.assertTrue(bytes(buf).endswith(b"\n"))
        self.assertEqual(conn.closed, 0)


if __name__ == "__main__":
    unittest.main()
