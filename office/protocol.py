"""herdr socket protocol helpers (design.md section 3, research section 1).

NDJSON over a unix domain socket on linux/macOS, and over a named pipe on
Windows. Normal methods are one-request-per-connection (the server closes after
the response); only events.subscribe keeps the connection open and streams
event lines.

**Windows.** herdr publishes its API as a named pipe whose *name is the
filesystem path* it reports, so `HERDR_SOCKET_PATH` has to be prefixed with
`\\\\.\\pipe\\` before it can be opened. Skipping that conversion is not an
error: a 25-byte `pid:timestamp` marker file really does live at that path, so
`open()` succeeds and hands back the marker's contents. Every connection is
therefore checked with os.fstat/S_ISFIFO *before* anything is written - opening
the marker file "r+b" and writing NDJSON into it would corrupt herdr's own
liveness marker.

herdr keeps exactly one listening pipe instance up and creates the next one
only after accepting a client, so a second concurrent connect lands in the gap
and fails with ERROR_PIPE_BUSY - which the stdlib flattens to a bare
`OSError(EINVAL)` with no winerror. Roughly a third of connections hit it in
practice, so the Windows open is retried on a monotonic deadline. That retry
lives *only* inside `_connect_windows`, never in the shared `connect()`, so a
genuine EINVAL from a unix socket is never silently retried.

Named pipes have no `settimeout`, and closing a handle that another thread is
blocked reading hangs the closing thread as well (measured). `PipeConnection`
therefore never issues a blocking read: it polls PeekNamedPipe and only reads
what is already there, which gives real read timeouts and lets `close()` from
another thread unblock a reader. Sends still block until the server drains -
that is the one socket semantic Windows does not reproduce (design.md 12).
"""

import base64
import errno
import itertools
import json
import os
import socket
import stat
import threading
import time

#: `\\.\pipe\` - the Windows named-pipe namespace prefix.
PIPE_PREFIX = "\\\\.\\pipe\\"

# ERROR_PIPE_BUSY reaches Python as OSError(EINVAL) with winerror unset; some
# paths (WaitNamedPipe) do keep a winerror, so both shapes are recognised.
ERROR_PIPE_BUSY = 231
BUSY_RETRY_MAX_S = 1.0                 # measured worst-case wait was 0.021s
BUSY_RETRY_START_S = 0.02
BUSY_RETRY_CAP_S = 0.05

# Read polling for named pipes: short at first so a request round trip (~1ms on
# the wire) is not padded, backing off so an idle subscription costs ~50 wakeups
# a second rather than 500.
POLL_MIN_S = 0.002
POLL_MAX_S = 0.02

# How many lines that are not our answer we are willing to step over before
# giving up. Guards against reading a stream forever on a confused server.
MAX_SKIPPED_LINES = 8

_next_seq = itertools.count(1)


class ProtocolError(Exception):
    def __init__(self, code, message):
        super().__init__("%s: %s" % (code, message))
        self.code = code
        self.message = message


# ----------------------------------------------------------- named pipes

def pipe_name(sock_path: str) -> str:
    """Map the path herdr reports to the pipe name it actually listens on."""
    if sock_path.startswith("\\\\"):              # already a pipe name
        return sock_path
    return PIPE_PREFIX + sock_path


def _is_pipe(handle) -> bool:
    """True when the open handle is a pipe rather than a regular file.

    CPython maps GetFileType() onto st_mode on Windows, so FILE_TYPE_PIPE shows
    up as S_ISFIFO - which is exactly the marker-file check this needs, without
    reaching for ctypes.
    """
    try:
        return stat.S_ISFIFO(os.fstat(handle.fileno()).st_mode)
    except (OSError, ValueError):
        return False


_peek_impl = None


def _load_peek():
    """Bind kernel32.PeekNamedPipe once (Windows only; ctypes is stdlib)."""
    import ctypes
    import ctypes.wintypes as wintypes
    import msvcrt

    fn = ctypes.WinDLL("kernel32", use_last_error=True).PeekNamedPipe
    # HANDLE has to be declared: an implicit int argument is truncated to 32
    # bits and the call then fails on 64-bit handles.
    fn.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                   ctypes.POINTER(wintypes.DWORD),
                   ctypes.POINTER(wintypes.DWORD),
                   ctypes.POINTER(wintypes.DWORD)]
    fn.restype = wintypes.BOOL

    def peek(handle):
        available = wintypes.DWORD(0)
        ok = fn(msvcrt.get_osfhandle(handle.fileno()), None, 0, None,
                ctypes.byref(available), None)
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
        return available.value

    return peek


def _peek_named_pipe(handle) -> int:
    """Bytes readable right now, without consuming them (PeekNamedPipe)."""
    global _peek_impl
    if _peek_impl is None:
        _peek_impl = _load_peek()
    return _peek_impl(handle)


class PipeConnection:
    """Socket-shaped adapter over a named pipe handle.

    Only what the callers actually use is implemented - sendall / recv /
    settimeout / close - which is why subscriber, commander, graphics and
    notifier need no Windows branch of their own.
    """

    def __init__(self, handle, *, timeout=None, peek=None,
                 sleep=time.sleep, monotonic=time.monotonic):
        self._handle = handle
        self._timeout = timeout
        self._peek = peek if peek is not None else _peek_named_pipe
        self._sleep = sleep
        self._monotonic = monotonic
        # Held across peek+read and across close, so a reader can never be
        # inside ReadFile while the handle is closed underneath it. Reads only
        # run once peek has said bytes are waiting, so they return at once and
        # the lock is never held for long.
        self._lock = threading.Lock()
        self._closed = False

    # -- socket surface -------------------------------------------------

    def settimeout(self, value):
        self._timeout = value

    def gettimeout(self):
        return self._timeout

    def sendall(self, data):
        """Write every byte. A pipe write is free to accept only some."""
        view = memoryview(data)
        while len(view):
            if self._closed:
                raise ConnectionError("named pipe closed while sending")
            written = self._handle.write(view)
            if not written:
                raise ConnectionError("named pipe accepted no bytes")
            view = view[written:]

    def recv(self, bufsize):
        """Read what is available, socket-style: b"" means end of stream."""
        deadline = (None if self._timeout is None
                    else self._monotonic() + self._timeout)
        delay = POLL_MIN_S
        while True:
            with self._lock:
                if self._closed:
                    return b""
                try:
                    available = self._peek(self._handle)
                except OSError:
                    return b""            # broken pipe reads as EOF
                if available:
                    try:
                        chunk = self._handle.read(min(bufsize, available))
                    except (OSError, ValueError):
                        return b""
                    return chunk or b""
            if deadline is not None and self._monotonic() >= deadline:
                raise TimeoutError("timed out reading the named pipe")
            self._sleep(delay)
            delay = min(POLL_MAX_S, delay * 2)

    def close(self):
        """Idempotent, and never raises anything but OSError."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._handle.close()
            except ValueError:
                pass


def _open_pipe(name, opener, is_pipe):
    handle = opener(name, "r+b", buffering=0)
    try:
        if not is_pipe(handle):
            raise ProtocolError(
                "not_a_pipe",
                "HERDR_SOCKET_PATH opened a regular file, not herdr's pipe")
    except BaseException:
        handle.close()
        raise
    return handle


def _is_pipe_busy(exc) -> bool:
    return (exc.errno == errno.EINVAL
            or getattr(exc, "winerror", None) == ERROR_PIPE_BUSY)


def _connect_windows(sock_path, timeout, *, opener=open, is_pipe=_is_pipe,
                     peek=None, sleep=time.sleep, monotonic=time.monotonic):
    name = pipe_name(sock_path)
    deadline = monotonic() + min(timeout, BUSY_RETRY_MAX_S)
    delay = BUSY_RETRY_START_S
    while True:
        try:
            handle = _open_pipe(name, opener, is_pipe)
        except FileNotFoundError:
            raise                        # herdr is not running; do not wait
        except OSError as exc:
            if not _is_pipe_busy(exc) or monotonic() >= deadline:
                raise
            sleep(delay)
            delay = min(BUSY_RETRY_CAP_S, delay * 1.5)
            continue
        return PipeConnection(handle, timeout=timeout, peek=peek,
                              sleep=sleep, monotonic=monotonic)


# ---------------------------------------------------------- unix sockets

def _connect_unix(sock_path, timeout):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(timeout)
        s.connect(sock_path)
    except BaseException:
        # A failed connect would otherwise leak the fd, and the Subscriber
        # retries on a backoff loop - it must not bleed descriptors while the
        # herdr server is down.
        s.close()
        raise
    return s


def connect(sock_path: str, timeout: float = 5.0, *, windows=None):
    """Open one connection to herdr. `windows` is an override for tests."""
    use_pipe = (os.name == "nt") if windows is None else windows
    if use_pipe:
        return _connect_windows(sock_path, timeout)
    return _connect_unix(sock_path, timeout)


# -------------------------------------------------------------- framing

def _read_line(sock, buf: bytearray) -> bytes:
    """Read one newline-terminated line, buffering any overflow into buf."""
    while b"\n" not in buf:
        chunk = sock.recv(65536)
        if not chunk:
            # A truncated line is never valid NDJSON, so returning it would
            # only turn a dropped connection into a JSON parse error.
            raise ConnectionError("connection closed before a full line arrived")
        buf.extend(chunk)
    idx = buf.index(b"\n")
    line = bytes(buf[:idx])
    del buf[:idx + 1]
    return line


def _is_answer(obj, req_id) -> bool:
    """True when this line answers our request rather than broadcasting news.

    herdr echoes the request id, but an id-less reply is still accepted so a
    build that drops it does not lock the office out; a broadcast event line
    carries neither result nor error and never matches.
    """
    if not isinstance(obj, dict):
        return False
    if "result" not in obj and "error" not in obj:
        return False
    ident = obj.get("id")
    return ident is None or ident == req_id


def _raise_if_error(obj):
    if "error" in obj:
        err = obj["error"] or {}
        raise ProtocolError(err.get("code", "error"), err.get("message", ""))


def _read_answer(conn, buf, req_id, timeout, skipped=None):
    """Read lines until one answers `req_id`; stash the rest in `skipped`."""
    deadline = time.monotonic() + timeout
    for _ in range(MAX_SKIPPED_LINES + 1):
        line = _read_line(conn, buf)
        obj = json.loads(line)
        if _is_answer(obj, req_id):
            return obj
        if skipped is not None:
            skipped.append(line)
        if time.monotonic() >= deadline:
            break
    raise ProtocolError("no_response",
                        "no reply to request id %s" % ascii(req_id))


# ------------------------------------------------------------- requests

def request(sock_path: str, method: str, params=None, *,
            req_id: str = None, timeout: float = 5.0):
    """Send one request, return its `result`, raising ProtocolError on error."""
    if req_id is None:
        # Unique per call: without it every request would answer to the same
        # name and the id check below could not tell two apart.
        req_id = "office-%d" % next(_next_seq)
    payload = {"id": req_id, "method": method, "params": params or {}}
    conn = connect(sock_path, timeout)
    try:
        conn.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        obj = _read_answer(conn, bytearray(), req_id, timeout)
    finally:
        conn.close()
    _raise_if_error(obj)
    return obj.get("result")


def open_subscription(sock_path: str, subscriptions, *,
                      req_id: str = "office-sub", timeout: float = 5.0):
    """Open a long-lived subscription. Returns (connection, leftover_buffer).

    Blocks until the subscription_started ack is read; raises on error. Every
    failure closes the connection: on Windows a leaked connection holds a pipe
    instance, which is the very resource the reconnect loop is contending for.
    """
    payload = {"id": req_id, "method": "events.subscribe",
               "params": {"subscriptions": list(subscriptions)}}
    conn = connect(sock_path, timeout)
    buf = bytearray()
    early = []
    try:
        conn.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        ack = _read_answer(conn, buf, req_id, timeout, skipped=early)
        _raise_if_error(ack)
        result = ack.get("result")
        if not isinstance(result, dict) or result.get("type") != "subscription_started":
            # Without this check any stray line reads as success and the
            # caller sits on a connection that will never deliver an event.
            raise ProtocolError("bad_ack", "expected subscription_started, got %s"
                                % ascii(result))
    except BaseException:
        conn.close()
        raise
    if early:
        # Events that raced ahead of the ack belong to the caller's stream.
        buf[:0] = b"".join(line + b"\n" for line in early)
    return conn, buf


def pane_list(sock_path: str, timeout: float = 5.0):
    result = request(sock_path, "pane.list", {}, timeout=timeout)
    return (result or {}).get("panes", [])


def pane_focus(sock_path: str, pane_id: str, timeout: float = 5.0):
    return request(sock_path, "pane.focus", {"pane_id": pane_id}, timeout=timeout)


def workspace_list(sock_path: str, timeout: float = 5.0):
    """Workspaces with their labels (the office's room names, section 4).

    pane.list carries no workspace label in herdr 0.7.4, so without this the
    islands would be named after raw workspace ids until a workspace.renamed
    event happened to arrive.
    """
    result = request(sock_path, "workspace.list", {}, timeout=timeout)
    return (result or {}).get("workspaces", [])


def pane_graphics_info(sock_path: str, pane_id: str, timeout: float = 5.0):
    """Probe the pane graphics feature (design.md section 5, tier 2).

    Raises ProtocolError with code `feature_disabled` unless the user has set
    `[experimental] kitty_graphics = true` in their herdr config, which is off
    by default - so this is the check that decides whether tier 2 is real.
    """
    return request(sock_path, "pane.graphics.info", {"pane_id": pane_id},
                   timeout=timeout)


def pane_graphics_set(sock_path: str, pane_id: str, data: bytes,
                      image_width: int, image_height: int, placement=None,
                      timeout: float = 5.0):
    """Place a PNG over a pane's cell grid.

    `placement` is the cell rectangle the image is drawn into
    (viewport_col/viewport_row/grid_cols/grid_rows); herdr scales the image to
    it, which is why the office can lay itself out in cells and stay correct
    whatever the terminal's cell size turns out to be.
    """
    params = {"pane_id": pane_id, "format": "png",
              "image_width": image_width, "image_height": image_height,
              "data_base64": base64.b64encode(data).decode("ascii")}
    if placement:
        params["placement"] = placement
    return request(sock_path, "pane.graphics.set", params, timeout=timeout)


def pane_graphics_clear(sock_path: str, pane_id: str, timeout: float = 5.0):
    return request(sock_path, "pane.graphics.clear", {"pane_id": pane_id},
                   timeout=timeout)


def notification_show(sock_path: str, title: str, body: str = "",
                      sound: str = "request", timeout: float = 5.0) -> str:
    """Show a toast; return the server's reason (design.md section 7).

    Reasons are `shown` / `disabled` / `rate_limited` / `no_foreground_client`
    / `busy` (research section 6). The Escalator decides what each one means.
    """
    params = {"title": title}
    if body:
        params["body"] = body
    if sound:
        params["sound"] = sound
    result = request(sock_path, "notification.show", params, timeout=timeout)
    return (result or {}).get("reason", "shown")
