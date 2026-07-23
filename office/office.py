"""Office - the resident pane: event loop wiring Subscriber + Input + Renderer.

design.md sections 2 and 6. Single process; the Subscriber and InputReader each
run a thread and feed one shared queue, while this loop owns the OfficeState
(single-writer) and redraws on events or on the 2 FPS animation tick.
"""

import os
import queue
import signal
import sys
import time

from . import protocol
from .input import InputReader
from .renderer import Renderer, detect_caps
from .state import OfficeState
from .subscriber import Subscriber

TICK_S = 0.5                                       # 2 FPS animation
MIN_REDRAW_S = 0.04                                # cap redraws at ~25 fps


class Office:
    def __init__(self, sock_path, self_pane_id, tier, truecolor):
        self.sock_path = sock_path
        self.q = queue.Queue()
        self.state = OfficeState(self_pane_id=self_pane_id, filter_mode="agents")
        self.renderer = Renderer(tier=tier, truecolor=truecolor)
        self.subscriber = Subscriber(sock_path, self.q, self_pane_id)
        self.input = InputReader(self.q)
        self.frame = 0
        self.muted = False
        self.show_help = False
        self.running = True
        self.status_line = "connecting..."
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
        self.subscriber.start()
        last_render = 0.0
        next_tick = time.monotonic() + TICK_S
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
                    next_tick = now + TICK_S
                    dirty = True
                if dirty and now - last_render >= MIN_REDRAW_S:
                    self._draw()
                    last_render = now
                    self._resize = False
        except KeyboardInterrupt:
            pass
        finally:
            self.subscriber.stop()
            self.input.stop()
            self._leave_screen()

    def _quit(self):
        self.running = False

    def _draw(self):
        cols, rows = self._size()
        frame = self.renderer.render(self.state, cols, rows, self.frame,
                                     muted=self.muted, show_help=self.show_help)
        sys.stdout.write(frame)
        sys.stdout.flush()

    # -- event dispatch -------------------------------------------------

    def _handle(self, item):
        kind, payload = item
        if kind == "key":
            self._handle_key(payload)
        elif kind == "snapshot":
            self.state.reconcile_snapshot(payload)
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
        elif kind == "log":
            self.status_line = payload

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
    tier, truecolor = detect_caps()
    Office(sock, self_pane, tier, truecolor).run()
    return 0
