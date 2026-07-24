"""Office - the resident pane: the event loop that drives everything else.

design.md sections 2 and 6. This module owns the loop and nothing else: the
terminal is the Screen's, frame content is the Renderer's, escalation policy is
the Escalator's, and each source of events runs in its own thread.

**Threading contract.** Five threads feed one `queue.Queue` of (kind, payload)
tuples - Subscriber (herdr events), Reconciler (periodic pane.list), InputReader
(stdin), Notifier (toast results) and Commander (jump/filter results). None of
them touch OfficeState, the Escalator or the Renderer. This loop is the *only*
writer of OfficeState and the only caller of Escalator.tick()/on_result(), so no
lock is needed anywhere: the queue is the entire thread boundary.

The Escalator (section 7) and the state.json writer (section 8) run on the
animation tick. Escalation is deliberately split in two - the Escalator decides
*what* to send with no I/O at all and the Notifier thread does the sending - so
a slow or rate-limited notification.show can never stall the render loop. The
key handlers are split the same way for the same reason (issue #12): no branch
below opens a socket, they hand the call to the Commander and pick the outcome
up later as an ("action", ...) item.
"""

import dataclasses
import os
import queue
import signal
import sys
import time

from . import graphics, statefile
from .commander import FOCUS, PANE_LIST, Commander
from .config import Config
from .config import load as load_config
from .escalator import Escalator
from .input import InputReader
from .notifier import Notifier
from .reconciler import Reconciler
from .renderer import (TIER_ASCII, TIER_KITTY, TIER_UNICODE, Renderer,
                       detect_caps, format_name)
from .screen import Screen
from .state import OfficeState
from .subscriber import Subscriber

MIN_REDRAW_S = 0.04                                # cap redraws at ~25 fps

# How long to leave a failed tier 2 overlay alone before trying it again. A
# transient socket error should heal within a redraw or two, but the refusal
# may also be standing - `herdr server reload-config` can switch
# experimental.kitty_graphics back off underneath a running office - and that
# must not turn into a pane.graphics.set on every single redraw.
GRAPHICS_RETRY_S = 5.0

TOAST_HINT = ("toasts are off: set [ui.toast] delivery = \"herdr\" "
              "in your herdr config")


class Office:
    def __init__(self, sock_path, self_pane_id, tier, truecolor, config=None):
        # No socket path is kept on the Office itself: with the jump/filter
        # calls moved onto the Commander, every socket in the process now
        # belongs to one of the feeder threads (issue #12).
        self.config = config or Config()
        self.q = queue.Queue()
        self.state = OfficeState(self_pane_id=self_pane_id,
                                 filter_mode=self.config.filter,
                                 workspace_globs=self.config.workspaces,
                                 exclude_agents=self.config.exclude_agents)
        self.renderer = Renderer(tier=tier, truecolor=truecolor,
                                 name_template=self.config.name_template,
                                 theme=self.config.theme)
        # tier 2 only, and only once run() has confirmed the pane can take
        # graphics at all - see graphics.probe and design.md section 5.
        self.graphics = (graphics.GraphicsSender(sock_path, self.q,
                                                 self_pane_id)
                         if tier == TIER_KITTY and self_pane_id else None)
        self.subscriber = Subscriber(sock_path, self.q, self_pane_id)
        self.reconciler = Reconciler(sock_path, self.q)
        self.input = InputReader(self.q)
        self.notifier = Notifier(sock_path, self.q)
        self.commander = Commander(sock_path, self.q)
        self.screen = Screen()
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
        # Kept apart from status_line: a rejected config value is a standing
        # authoring error the user has to go and fix, while status_line holds
        # transient notices ("connected", "jump failed") that would otherwise
        # overwrite the warnings milliseconds after startup.
        self.config_warning = "; ".join(self.config.warnings)
        self.status_line = ""
        self.toast_hint = ""
        self.graphics_note = ""
        # Sprite boxes last handed to the graphics thread. They are plain
        # scalars, so an unchanged frame is recognised without composing
        # anything - which is what keeps the static overlay (design.md risk 6)
        # from re-encoding a PNG on every animation tick.
        # The last sprite boxes *submitted* to the graphics thread, and whether
        # that submission landed. Two fields rather than one because "what was
        # asked for" and "what is on screen" answer different questions: the
        # first decides whether there is new work, the second whether a retry
        # is owed. Conflating them is what previously let a failed send be
        # mistaken for a drawn one.
        self._overlay_boxes = None
        self._overlay_ok = True
        # Monotonic time before which the *same* request is not retried; see
        # GRAPHICS_RETRY_S and _handle_graphics_result.
        self._overlay_retry_at = 0.0
        # The "...ing" half of a status_line message that an in-flight action
        # put there, so its result can take it back down without wiping a
        # newer, unrelated notice that arrived in between.
        self.pending_status = ""
        # Refreshes still out on the Commander. Only the last one home takes
        # the pending notice down; each carries its own status_epoch as a token,
        # so overlapping presses never borrow each other's.
        self.refreshes_in_flight = 0

    # -- main loop ------------------------------------------------------

    def run(self):
        sigterm = getattr(signal, "SIGTERM", None)
        if sigterm is not None:
            try:
                signal.signal(sigterm, lambda *_: self._quit())
            except (OSError, ValueError):         # not main thread / unsupported
                pass
        self.screen.install_resize_handler()
        self.screen.open()
        self.input.start()
        self.notifier.start()
        self.commander.start()
        if self.graphics:
            self.graphics.start()
        self.subscriber.start()
        self.reconciler.start()
        last_render = 0.0
        next_tick = time.monotonic() + self.tick_s
        try:
            while self.running:
                now = time.monotonic()
                timeout = max(0.0, next_tick - now)
                dirty = self.screen.resized
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
                    self.screen.clear_resized()
        except KeyboardInterrupt:
            pass
        finally:
            self.reconciler.stop()
            self.subscriber.stop()
            self.notifier.stop()
            self.commander.stop()
            if self.graphics:
                # Stop first, then clear synchronously: an overlay left behind
                # outlives the frame it belonged to, and the pane is about to
                # stop redrawing underneath it.
                self.graphics.stop()
                graphics.clear_now(self.graphics.sock_path,
                                   self.graphics.pane_id)
            self.input.stop()
            # Feeder threads are stopped, so the state is settled: record it
            # rather than whatever the last periodic write happened to hold.
            self.writer.write_stopped(self.state,
                                      self.escalator.escalated_ids())
            self.screen.close()

    def _quit(self):
        self.running = False

    def _escalate(self):
        """Run the blocked timers and hand any toasts to the sender thread."""
        self.escalator.muted = self.muted
        for note in self.escalator.tick(self.state.ordered_desks(),
                                        self.state.rooms):
            self.notifier.send(note)

    def _draw(self):
        cols, rows = self.screen.size()
        self.screen.write(self.renderer.render(
            self.state, cols, rows, self.frame,
            muted=self.muted, show_help=self.show_help,
            escalated=self.escalator.escalated_ids(), status=self._status()))
        self._sync_overlay()

    def _sync_overlay(self):
        """Keep the tier 2 image in step with the frame just written.

        The renderer leaves the sprite rectangles of the frame it produced on
        `sprite_boxes`; an unchanged list means an unchanged image, so nothing
        is composed or sent. The help and compact views have no sprites, and
        clear the overlay so it cannot sit on top of them.

        The backoff gates *retries*, not new work. A frame that wants something
        different from the last thing submitted is acted on at once - waiting
        would leave the previous image over content it does not belong to,
        which is precisely the case when the user presses `?` and the desks are
        replaced by help text. Only asking for the same thing again is made to
        wait, so a standing refusal costs one call per GRAPHICS_RETRY_S. A new
        desired state cannot arrive faster than the layout changes, so acting
        on it immediately cannot spin.
        """
        if self.graphics is None:
            return
        boxes = tuple(self.renderer.sprite_boxes)
        if boxes == self._overlay_boxes:
            if self._overlay_ok or self.state.now() < self._overlay_retry_at:
                return
        self._overlay_boxes = boxes
        # Assume it lands; a ("graphics", (False, ...)) report flips this back
        # and schedules the retry.
        self._overlay_ok = True
        if boxes:
            self.graphics.set_boxes(boxes, self.renderer.art)
        else:
            self.graphics.clear()

    def _status(self):
        parts = [p for p in (self.config_warning, self.toast_hint,
                             self.graphics_note, self.status_line) if p]
        return "  |  ".join(parts)

    # -- event dispatch -------------------------------------------------

    def _handle(self, item):
        kind, payload = item
        if kind == "key":
            self._handle_key(payload)
        elif kind == "snapshot":
            self._apply_snapshot(payload, seed=True)
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
        elif kind == "action":
            self._handle_action_result(*payload)
        elif kind == "graphics":
            self._handle_graphics_result(*payload)
        elif kind == "log":
            self.status_line = payload

    def _apply_snapshot(self, panes, seed=False, since_epoch=None):
        """Apply a pane.list; `seed` marks the authoritative Subscriber path.

        Only that path may spend the recovered blocked_since: a refresh the
        user asked for with `a` can land before the fleet is fully known -
        before workspace labels have arrived, say - and would otherwise burn
        the seed on a partial view, handing an already-stuck agent a fresh
        countdown.
        """
        self.state.reconcile_snapshot(panes, since_epoch=since_epoch)
        if seed and self._seed_blocked:
            # design.md section 7: adopt the previous run's blocked_since so an
            # agent stuck before the office opened is not given a fresh
            # countdown. Applies once, on the first snapshot.
            self.state.seed_blocked_since(self._seed_blocked)
            self._seed_blocked = None

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

    def _handle_graphics_result(self, ok, message):
        """Outcome of a pane.graphics.set/clear (tier 2 only).

        A failure is worth a line because the tier 1 art is still underneath:
        the office looks fine, and the user would otherwise have no way to
        tell the overlay never arrived.

        **Both outcomes move the state, and that is the point.** The sender is
        serial and reports every request it runs, so the last report always
        describes the last submission - which makes "latest report wins" the
        correct rule here. Writing only the failure edge looked sufficient
        (_sync_overlay already sets `_overlay_ok` optimistically on submit) but
        is not: submit A, submit B while A is still out, then A fails and B
        succeeds. The failure arrives second-to-last and leaves the office
        believing B is missing, so it re-sends an overlay that is already on
        screen. Cheap - the next optimistic submit heals it, so it costs one
        wasted encode rather than a standing loop - but simply wrong.
        """
        self.graphics_note = "" if ok else "graphics: %s" % message
        if ok:
            self._overlay_ok = True
            self._overlay_retry_at = 0.0
        else:
            self._overlay_ok = False
            self._overlay_retry_at = self.state.now() + GRAPHICS_RETRY_S

    def _handle_key(self, name):
        cols, _ = self.screen.size()
        per_row = self.renderer.per_row(cols)
        if name in ("q", "quit"):
            # "quit" is Ctrl+C/Ctrl+D. On unix the tty raises SIGINT before it
            # ever reaches us, but Windows hands the character straight over,
            # so without this branch Ctrl+C would do nothing there.
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
        """Ask for the focus; say nothing yet (issue #12).

        A successful jump announces itself - the terminal switches panes - so
        the only thing worth showing is a failure, and that can only be known
        once the Commander has been round the socket.
        """
        if not pane_id:
            return
        self.commander.focus(pane_id)

    def _toggle_filter(self):
        """Flip the filter now, refresh the fleet off-loop.

        set_filter takes effect on the next frame, but widening to "all" needs
        panes the office previously dropped, and those only arrive with the
        pane.list the Commander is now fetching. Without a word on the status
        line that gap reads as a dead keypress, so the pending notice goes up
        immediately and comes down when the snapshot lands.
        """
        new = "all" if self.state.filter_mode == "agents" else "agents"
        self.state.set_filter(new)
        self._set_pending("filter %s; refreshing" % new)
        self.refreshes_in_flight += 1
        self.commander.list_panes(token=self.state.status_epoch())

    def _handle_action_result(self, name, result, error, since_epoch=None):
        """Outcome of a Commander action, one socket round-trip after the key.

        Only the refresh puts a pending notice up, so only the refresh's own
        result takes it down: a jump that happens to land in between says its
        piece without cancelling a refresh that is still in flight.
        """
        if name == PANE_LIST:
            self.refreshes_in_flight = max(0, self.refreshes_in_flight - 1)
        if error:
            if name == PANE_LIST:
                self.pending_status = ""      # superseded by the failure below
            self.status_line = "%s failed: %s" % (
                "jump" if name == FOCUS else "filter refresh", error)
            return
        if name == PANE_LIST:
            # These panes are as herdr saw them when *this* refresh went out;
            # any status event handled since then is the newer truth.
            #
            # Membership is taken as authoritative all the same, which leaves
            # one accepted window, in both directions: a pane that closed
            # while the refresh was in flight is re-added here over the
            # pane.closed the loop already applied, and one that was created
            # is dropped again - either way until the next periodic reconcile.
            # That is the same staleness the 60s reconcile has always had (its
            # pane.list is fetched off-loop too) and the same ghost it exists
            # to sweep (issue #1); the window here is one socket round-trip -
            # 2ms against herdr 0.7.4 - and self-healing, so it is documented
            # rather than defended against with tombstones in OfficeState.
            self._apply_snapshot(result, since_epoch=since_epoch)
            if not self.refreshes_in_flight:
                self._clear_pending()

    def _set_pending(self, text):
        self.pending_status = text
        self.status_line = text

    def _clear_pending(self):
        if self.status_line == self.pending_status:
            self.status_line = ""
        self.pending_status = ""


def run():
    sock = os.environ.get("HERDR_SOCKET_PATH")
    if not sock:
        sys.stderr.write("HERDR_SOCKET_PATH not set; run me as a herdr pane.\n")
        return 2
    self_pane = os.environ.get("HERDR_PANE_ID")
    cfg = load_config()
    tier, truecolor = detect_caps(cfg.force_renderer)
    if cfg.force_renderer in ("unicode", "kitty") and tier == TIER_ASCII:
        # detect_caps overrode the config because stdout cannot encode the
        # frame (a cp932 console, typically). Say so rather than leaving the
        # user wondering why `renderer` did nothing.
        cfg = dataclasses.replace(
            cfg, warnings=cfg.warnings
            + ("renderer=%s needs a UTF-8 stdout (got %s); using ascii"
               % (cfg.force_renderer,
                  getattr(sys.stdout, "encoding", None) or "unknown"),))
    if tier == TIER_KITTY:
        # design.md section 5: an explicit renderer="kitty" still falls back to
        # tier 1 *with a warning* when the server says no - which it does by
        # default, since [experimental] kitty_graphics ships off. Asking once
        # at startup keeps the answer out of the render loop.
        ok, reason = graphics.probe(sock, self_pane)
        if not ok:
            tier = TIER_UNICODE
            cfg = dataclasses.replace(
                cfg, warnings=cfg.warnings
                + ("renderer=kitty unavailable (%s); using unicode" % reason,))
    Office(sock, self_pane, tier, truecolor, config=cfg).run()
    return 0
