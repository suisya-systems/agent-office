"""herdr socket protocol helpers (design.md section 3, research section 1).

NDJSON over a unix domain socket. Normal methods are one-request-per-connection
(the server closes after the response); only events.subscribe keeps the
connection open and streams event lines. Windows named pipes are out of scope
for Stage 2 core (linux/macOS only).
"""

import base64
import json
import socket


class ProtocolError(Exception):
    def __init__(self, code, message):
        super().__init__("%s: %s" % (code, message))
        self.code = code
        self.message = message


def connect(sock_path: str, timeout: float = 5.0) -> socket.socket:
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


def _read_line(sock: socket.socket, buf: bytearray) -> bytes:
    """Read one newline-terminated line, buffering any overflow into buf."""
    while b"\n" not in buf:
        chunk = sock.recv(65536)
        if not chunk:
            if buf:
                line = bytes(buf)
                del buf[:]
                return line
            raise ConnectionError("socket closed before a full line arrived")
        buf.extend(chunk)
    idx = buf.index(b"\n")
    line = bytes(buf[:idx])
    del buf[:idx + 1]
    return line


def request(sock_path: str, method: str, params=None, *,
            req_id: str = "office", timeout: float = 5.0):
    """Send one request, return its `result`, raising ProtocolError on error."""
    payload = {"id": req_id, "method": method, "params": params or {}}
    s = connect(sock_path, timeout)
    try:
        s.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        buf = bytearray()
        line = _read_line(s, buf)
        obj = json.loads(line)
    finally:
        s.close()
    if "error" in obj:
        err = obj["error"] or {}
        raise ProtocolError(err.get("code", "error"), err.get("message", ""))
    return obj.get("result")


def open_subscription(sock_path: str, subscriptions, *,
                      req_id: str = "office-sub", timeout: float = 5.0):
    """Open a long-lived subscription. Returns (socket, leftover_buffer).

    Blocks until the subscription_started ack is read; raises on error.
    """
    payload = {"id": req_id, "method": "events.subscribe",
               "params": {"subscriptions": list(subscriptions)}}
    s = connect(sock_path, timeout)
    s.sendall((json.dumps(payload) + "\n").encode("utf-8"))
    buf = bytearray()
    ack = json.loads(_read_line(s, buf))
    if "error" in ack:
        s.close()
        err = ack["error"] or {}
        raise ProtocolError(err.get("code", "error"), err.get("message", ""))
    return s, buf


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
