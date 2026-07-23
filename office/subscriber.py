"""Subscriber - socket connection management (design.md section 3).

Two long-lived connections plus short-lived commands:

  * Connection L (lifecycle): broadcast pane.* / workspace.* subscriptions;
    tracks fleet membership, focus and metadata. This is the critical
    connection - a drop here triggers a full reconnect with backoff.
  * Connection S (status): one pane.agent_status_changed subscription per known
    pane on a single connection, re-established (make-before-break, debounced)
    whenever membership changes. Section 3 was empirically confirmed in Stage 2:
    pane.updated does NOT fire on agent_status change, so S is required.

The pane set for S is sourced from a fresh, authoritative pane.list at
(re)subscribe time - not from the incrementally-tracked membership, which can
briefly hold stale pane_ids from replayed events and make the whole
events.subscribe fail with a server-side "pane get" error. An S failure is
isolated: it is retried with backoff while L keeps running, so live status may
lag momentarily but membership/focus never stop.

Startup is subscribe-then-snapshot: open L, snapshot with pane.list, open S for
all known panes, then snapshot again to overwrite any status that changed before
S was established. Everything downstream is idempotent, so replayed/duplicated
events are harmless.

Normalized events are pushed onto an out queue as (kind, payload) tuples:
  ("snapshot", [PaneInfo,...])   full authoritative membership
  ("pane", PaneInfo)             upsert one pane
  ("closed", pane_id)
  ("focused", pane_id)
  ("status", {pane_id, agent_status, ...})
  ("room", (workspace_id, label))
  ("room_closed", workspace_id)
  ("log", message)               human-facing notice (connection up/down)
"""

import json
import threading

from . import protocol

L_SUBSCRIPTIONS = [
    "pane.created", "pane.closed", "pane.exited", "pane.updated",
    "pane.focused", "pane.agent_detected",
    "workspace.created", "workspace.renamed", "workspace.closed",
]

RESUB_DEBOUNCE_S = 0.25
S_RETRY_MAX_S = 5.0
BACKOFF_START_S = 0.5
BACKOFF_MAX_S = 8.0


class Subscriber:
    def __init__(self, sock_path, out_queue, self_pane_id=None):
        self.sock_path = sock_path
        self.out = out_queue
        self.self_pane_id = self_pane_id
        self._stop = threading.Event()
        self._thread = None
        # per-session sockets and signals
        self._l_sock = None
        self._s_sock = None
        self._l_broken = threading.Event()        # L drop -> full reconnect
        self._s_broken = threading.Event()        # S drop -> rebuild S only
        self._wake = threading.Event()            # membership changed / poke
        self._resub_deadline = None               # monotonic time to rebuild S
        self._s_backoff = RESUB_DEBOUNCE_S

    # -- lifecycle ------------------------------------------------------

    def start(self):
        self._thread = threading.Thread(target=self._supervise, daemon=True,
                                        name="office-subscriber")
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._l_broken.set()
        self._wake.set()
        self._close_sockets()
        if self._thread:
            self._thread.join(timeout=2.0)

    def _close_sockets(self):
        for attr in ("_l_sock", "_s_sock"):
            sock = getattr(self, attr)
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
                setattr(self, attr, None)

    # -- supervisor -----------------------------------------------------

    def _supervise(self):
        backoff = BACKOFF_START_S
        while not self._stop.is_set():
            try:
                self._establish()
                backoff = BACKOFF_START_S
                self._emit("log", "connected")
                self._session_loop()
            except Exception as exc:                       # noqa: BLE001
                self._emit("log", "connection error: %s" % exc)
            self._close_sockets()
            if self._stop.is_set():
                break
            self._emit("log", "reconnecting in %.1fs" % backoff)
            self._stop.wait(backoff)
            backoff = min(BACKOFF_MAX_S, backoff * 2)

    def _establish(self):
        self._l_broken.clear()
        self._s_broken.clear()
        self._resub_deadline = None
        self._s_backoff = RESUB_DEBOUNCE_S
        # 1. Connection L first, so no lifecycle event is missed.
        self._l_sock, l_buf = protocol.open_subscription(
            self.sock_path, [{"type": t} for t in L_SUBSCRIPTIONS],
            req_id="office-L")
        self._start_reader(self._l_sock, l_buf, self._handle_l,
                           self._l_broken, "_l_sock", "office-L-reader")
        # 2. Initial snapshot.
        self._emit("snapshot", protocol.pane_list(self.sock_path))
        # 3. Connection S for all live panes, then re-snapshot to close the gap.
        if self._open_s():
            self._emit("snapshot", protocol.pane_list(self.sock_path))

    def _open_s(self):
        """Open a fresh S connection for the current live pane set.

        Returns True on success. On failure, leaves S unset and schedules a
        retry; never raises (an S problem must not take down L).
        """
        try:
            panes = protocol.pane_list(self.sock_path)
        except Exception as exc:                           # noqa: BLE001
            self._schedule_resub("pane.list failed: %s" % exc)
            return False
        ids = [p["pane_id"] for p in panes
               if p.get("pane_id") and p["pane_id"] != self.self_pane_id]
        old = self._s_sock
        new_sock = None
        if ids:
            subs = [{"type": "pane.agent_status_changed", "pane_id": pid}
                    for pid in ids]
            try:
                new_sock, s_buf = protocol.open_subscription(
                    self.sock_path, subs, req_id="office-S")
            except Exception as exc:                       # noqa: BLE001
                self._schedule_resub("status subscribe failed: %s" % exc)
                return False
        # Publish the new socket as current BEFORE closing the old one, so the
        # old reader's finally sees it is no longer current and does NOT flag
        # _s_broken (a deliberate replacement must not look like a drop).
        self._s_sock = new_sock
        self._s_broken.clear()
        self._s_backoff = RESUB_DEBOUNCE_S
        if new_sock is not None:
            self._start_reader(new_sock, s_buf, self._handle_s,
                               self._s_broken, "_s_sock", "office-S-reader")
        if old is not None and old is not new_sock:
            try:
                old.close()
            except OSError:
                pass
        return True

    def _schedule_resub(self, reason=None):
        import time
        if reason:
            self._emit("log", reason)
        self._resub_deadline = time.monotonic() + self._s_backoff
        self._s_backoff = min(S_RETRY_MAX_S, self._s_backoff * 2)
        self._wake.set()

    def _session_loop(self):
        import time
        while not self._stop.is_set() and not self._l_broken.is_set():
            timeout = 0.5
            if self._resub_deadline is not None:
                timeout = max(0.0, self._resub_deadline - time.monotonic())
            self._wake.wait(timeout)
            self._wake.clear()
            if self._l_broken.is_set() or self._stop.is_set():
                break
            if self._s_broken.is_set() and self._resub_deadline is None:
                self._schedule_resub()
            if (self._resub_deadline is not None
                    and time.monotonic() >= self._resub_deadline):
                self._resub_deadline = None
                if self._open_s():
                    self._emit("snapshot", protocol.pane_list(self.sock_path))
        if self._l_broken.is_set():
            raise ConnectionError("lifecycle connection dropped")

    # -- reader threads -------------------------------------------------

    def _start_reader(self, sock, buf, handler, broken_event, sock_attr, name):
        t = threading.Thread(target=self._read_loop,
                             args=(sock, buf, handler, broken_event, sock_attr),
                             daemon=True, name=name)
        t.start()

    def _read_loop(self, sock, buf, handler, broken_event, sock_attr):
        try:
            sock.settimeout(None)
            while not self._stop.is_set():
                while b"\n" in buf:
                    line, _, rest = buf.partition(b"\n")
                    buf = bytearray(rest)
                    if line.strip():
                        try:
                            handler(json.loads(line))
                        except Exception:                  # noqa: BLE001
                            pass                            # skip malformed line
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf.extend(chunk)
        except OSError:
            pass
        finally:
            # Only report a real drop of the *current* socket; a socket that has
            # since been replaced was closed on purpose (make-before-break).
            if not self._stop.is_set() and getattr(self, sock_attr) is sock:
                broken_event.set()
                self._wake.set()

    # -- event handlers -------------------------------------------------

    def _mark_resub(self):
        import time
        self._s_backoff = RESUB_DEBOUNCE_S
        self._resub_deadline = time.monotonic() + RESUB_DEBOUNCE_S
        self._wake.set()

    def _handle_l(self, obj):
        data = obj.get("data") or {}
        kind = data.get("type") or obj.get("event")
        if kind in ("pane_created", "pane_updated"):
            pane = data.get("pane")
            if pane and pane.get("pane_id"):
                if kind == "pane_created":
                    self._mark_resub()
                self._emit("pane", pane)
        elif kind in ("pane_closed", "pane_exited"):
            pid = data.get("pane_id")
            if pid:
                self._mark_resub()
                self._emit("closed", pid)
        elif kind == "pane_focused":
            self._emit("focused", data.get("pane_id"))
        elif kind == "pane_agent_detected":
            pid = data.get("pane_id")
            if pid:
                self._emit("pane", {"pane_id": pid,
                                    "workspace_id": data.get("workspace_id", ""),
                                    "agent": data.get("agent")})
        elif kind in ("workspace_created", "workspace_renamed"):
            ws = data.get("workspace") or {}
            wid = data.get("workspace_id") or ws.get("workspace_id")
            label = data.get("label") or ws.get("label")
            if wid and label:
                self._emit("room", (wid, label))
        elif kind == "workspace_closed":
            wid = data.get("workspace_id")
            if wid:
                self._emit("room_closed", wid)

    def _handle_s(self, obj):
        data = obj.get("data") or {}
        if obj.get("event") == "pane.agent_status_changed" or "agent_status" in data:
            pid = data.get("pane_id")
            if pid:
                self._emit("status", data)

    def _emit(self, kind, payload):
        self.out.put((kind, payload))
