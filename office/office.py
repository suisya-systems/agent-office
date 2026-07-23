"""Office - the resident pane: event loop wiring Subscriber + Input + Renderer.

design.md sections 2 and 6. Single process; the Subscriber, InputReader and
Notifier each run a thread and feed one shared queue, while this loop owns the
OfficeState (single-writer) and redraws on events or on the animation tick.

The Escalator (section 7) and the state.json writer (section 8) are driven from
the same tick. Escalation is deliberately split in two: the Escalator decides
*what* to send with no I/O at all, and the Notifier thread does the sending, so
a slow or rate-limited notification.show can never stall the render loop.
"""

import os
import queue
import signal
import sys
import threading
import time

from . import protocol, statefile
from .config import Config
from .config import load as load_config
from .escalator import Escalator
from .input import InputReader
from .reconciler import Reconciler
from .renderer import Renderer, detect_caps, format_name
from .state import OfficeState
from .subscriber import Subscriber

MIN_REDRAW_S = 0.04                                # cap redraws at ~25 fps

TOAST_HINT = ("toasts are off: set [ui.toast] delivery = \"herdr\" "
              "in your herdr config")


class Notifier:
    """Background sender for notification.show (never blocks the render loop).

    Results come back through the office queue as ("notify_result", (note,
    reason)) so the Escalator's retry/rollback logic still runs single-writer
    on the main loop.
    """

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
        self._q.put(None)
        if self._thread:
            self._thread.join(timeout=2.0)

    def send(self, note):
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
                reason = "error"
                self.out.put(("log", "toast failed: %s" % exc))
            self.out.put(("notify_result", (note, reason)))


class Office:
    def __init__(self, sock_path, self_pane_id, tier, truecolor, config=None):
        self.sock_path = sock_path
        self.config = config or Config()
        self.q = queue.Queue()
        self.state = OfficeState(self_pane_id=self_pane_id,
                                 filter_mode=self.config.filter,
                                 workspace_globs=self.config.workspaces,
                                 exclude_agents=self.config.exclude_agents)
        self.renderer = Renderer(tier=tier, truecolor=truecolor,
                                 name_template=self.config.name_template)
        self.subscriber = Subscriber(sock_path, self.q, self_pane_id)
        self.reconciler = Reconciler(sock_path, self.q)
        self.input = InputReader(self.q)
        self.notifier = Notifier(sock_path, self.q)
        self.escalator = Escalator(
            threshold_s=self.config.blocked_threshold_s,
            renotify_s=self.config.renotify_interval_s,
            sound=self.config.sound,
            notify_done=self.config.notify_done,
            name_fn=lambda desk: format_name(desk.display_name,
                                             self.config.name_template))
        self.tick_s = 1.0 / max(1, self.config.fps)
        state_file = statefile.state_path()
        self.writer = statefile.StateWriter(state_file,
                                            office_pane_id=self_pane_id)
        # Read before the writer can overwrite it: section 7's restart
        # inheritance needs the *previous* run's blocked_since values. Only a
        # recently-written file qualifies (statefile.SEED_MAX_GAP_S) - across a
        # long outage a desk may have unblocked and reblocked unobserved, and
        # inheriting then would escalate a desk that only just blocked.
        self._seed_blocked = statefile.blocked_since_map(
            statefile.read(state_file))
        self.frame = 0
        self.muted = False
        self.show_help = False
        self.running = True
        self.status_line = "; ".join(self.config.warnings)
        self.toast_hint = ""
        self._resize = True

    # -- terminal -------------------------------------------------------

    def _enter_screen(self):
        sys.stdout.write("\x1b[?1049h\x1b[?25l\x1b[2J")   # alt screen, hide cursor
        sys.stdout.flush()

    def _leave_screen(self):
        sys.stdout.write("\x1b[?25h\x1b[?1049l")          # show cursor, main screen
        sys.stdout.flush()

    def _on_winch(self, *_):
        self._resize = True

    def _size(self):
        import shutil
        sz = shutil.get_terminal_size((100, 30))
        return sz.columns, sz.lines

    # -- main loop ------------------------------------------------------

    def run(self):
        for signame, handler in (("SIGTERM", lambda *_: self._quit()),
                                 ("SIGWINCH", self._on_winch)):
            sig = getattr(signal, signame, None)
            if sig is not None:
                try:
                    signal.signal(sig, handler)
                except (OSError, ValueError):     # not main thread / unsupported
                    pass
        self._enter_screen()
        self.input.start()
        self.notifier.start()
        self.subscriber.start()
        self.reconciler.start()
        last_render = 0.0
        next_tick = time.monotonic() + self.tick_s
        try:
            while self.running:
                now = time.monotonic()
                timeout = max(0.0, next_tick - now)
                dirty = self._resize
                try:
                    item = self.q.get(timeout=timeout)
                    dirty = True
                    self._handle(item)
                    # drain any burst without blocking
                    for _ in range(256):
                        try:
                            self._handle(self.q.get_nowait())
                        except queue.Empty:
                            break
                except queue.Empty:
                    pass
                now = time.monotonic()
                if now >= next_tick:
                    self.frame += 1
                    next_tick = now + self.tick_s
                    dirty = True
                    self._escalate()
                    self.writer.maybe_write(self.state,
                                            self.escalator.escalated_ids())
                if dirty and now - last_render >= MIN_REDRAW_S:
                    self._draw()
                    last_render = now
                    self._resize = False
        except KeyboardInterrupt:
            pass
        finally:
            self.reconciler.stop()
            self.subscriber.stop()
            self.notifier.stop()
            self.input.stop()
            self.writer.write_stopped()
            self._leave_screen()

    def _quit(self):
        self.running = False

    def _escalate(self):
        """Run the blocked timers and hand any toasts to the sender thread."""
        self.escalator.muted = self.muted
        for note in self.escalator.tick(self.state.ordered_desks(),
                                        self.state.rooms):
            self.notifier.send(note)

    def _draw(self):
        cols, rows = self._size()
        frame = self.renderer.render(self.state, cols, rows, self.frame,
                                     muted=self.muted,
                                     show_help=self.show_help,
                                     escalated=self.escalator.escalated_ids(),
                                     status=self._status())
        sys.stdout.write(frame)
        sys.stdout.flush()

    def _status(self):
        parts = [p for p in (self.toast_hint, self.status_line) if p]
        return "  |  ".join(parts)

    # -- event dispatch -------------------------------------------------

    def _handle(self, item):
        kind, payload = item
        if kind == "key":
            self._handle_key(payload)
        elif kind == "snapshot":
            self.state.reconcile_snapshot(payload)
            if self._seed_blocked:
                # design.md section 7: adopt the previous run's blocked_since
                # so an agent stuck before the office opened is not given a
                # fresh countdown. Applies once, on the first snapshot.
                self.state.seed_blocked_since(self._seed_blocked)
                self._seed_blocked = None
        elif kind == "pane":
            self.state.ingest_pane(payload)
        elif kind == "closed":
            self.state.remove_pane(payload)
        elif kind == "focused":
            self.state.set_focused(payload)
        elif kind == "status":
            self.state.set_status(
                payload.get("pane_id"), payload.get("agent_status", "unknown"),
                agent=payload.get("agent"),
                display_agent=payload.get("display_agent"),
                title=payload.get("title"),
                state_labels=payload.get("state_labels"),
                workspace_id=payload.get("workspace_id"))
        elif kind == "room":
            self.state.set_room_label(payload[0], payload[1])
        elif kind == "room_closed":
            self.state.remove_room(payload)
        elif kind == "notify_result":
            self._handle_notify_result(*payload)
        elif kind == "log":
            self.status_line = payload

    def _handle_notify_result(self, note, reason):
        self.escalator.on_result(note, reason)
        if reason == "disabled":
            # design.md section 13 risk 4: the commonest "no toasts" cause is
            # herdr's default [ui.toast] delivery = "off". Say so on screen
            # once instead of retrying a delivery the user switched off.
            self.toast_hint = TOAST_HINT
        elif reason == "shown":
            self.toast_hint = ""
        elif reason != "no_foreground_client":
            self.status_line = "toast %s; retrying" % reason

    def _handle_key(self, name):
        cols, _ = self._size()
        per_row = self.renderer.per_row(cols)
        if name in ("q",):
            self._quit()
        elif name == "escape" and self.show_help:
            self.show_help = False
        elif name == "?":
            self.show_help = not self.show_help
        elif name == "left" or name == "h":
            self.state.move_selection(-1, 0, per_row)
        elif name == "right" or name == "l":
            self.state.move_selection(1, 0, per_row)
        elif name == "up" or name == "k":
            self.state.move_selection(0, -1, per_row)
        elif name == "down" or name == "j":
            self.state.move_selection(0, 1, per_row)
        elif name == "enter":
            self._jump(self.state.selected_pane_id)
        elif name == "b":
            desk = self.state.oldest_blocked()
            if desk:
                self.state.select(desk.pane_id)
                self._jump(desk.pane_id)
        elif name == "tab":
            self.state.select_next_blocked()
        elif name == "a":
            self._toggle_filter()
        elif name == "s":
            self.muted = not self.muted
            self.escalator.muted = self.muted

    def _jump(self, pane_id):
        if not pane_id:
            return
        try:
            protocol.pane_focus(self.sock_path, pane_id)
        except Exception as exc:                          # noqa: BLE001
            self.status_line = "jump failed: %s" % exc

    def _toggle_filter(self):
        new = "all" if self.state.filter_mode == "agents" else "agents"
        self.state.set_filter(new)
        try:
            self.state.reconcile_snapshot(protocol.pane_list(self.sock_path))
        except Exception as exc:                          # noqa: BLE001
            self.status_line = "filter refresh failed: %s" % exc


def run():
    sock = os.environ.get("HERDR_SOCKET_PATH")
    if not sock:
        sys.stderr.write("HERDR_SOCKET_PATH not set; run me as a herdr pane.\n")
        return 2
    self_pane = os.environ.get("HERDR_PANE_ID")
    cfg = load_config()
    tier, truecolor = detect_caps(cfg.force_renderer)
    Office(sock, self_pane, tier, truecolor, config=cfg).run()
    return 0
