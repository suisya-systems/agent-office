"""Escalator - blocked-timer escalation to toasts (design.md section 7).

A desk that stays `blocked` past `blocked_threshold_s` (default 90s) earns a
`notification.show` toast; while it stays blocked it is re-notified every
`renotify_interval_s` (default 300s, 0 disables). Several desks that come due
close together are aggregated into a single toast ("3 agents are waiting"),
using a 5 second collection window that opens when the first one crosses the
threshold. Leaving `blocked` resets everything for that desk.

This class does **no I/O**: `tick()` is a pure state machine over a list of
desk snapshots and returns the Notification objects the caller should send,
and the caller reports each delivery back through `on_result()`. That keeps the
whole escalation policy unit-testable with an injected clock, and lets the
office pane send toasts on a background thread so a slow socket never stalls
the render loop.

Delivery reasons are honoured per section 7: `rate_limited` / `busy` roll the
batch back and retry after 30s, while `disabled` / `no_foreground_client` are
logged only - the raised hand is still on screen, so a toast that the user has
switched off is not an error worth retrying.
"""

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

AGGREGATE_WINDOW_S = 5.0        # section 7: collect co-blocked desks for 5s
RETRY_AFTER_S = 30.0            # section 7: rate_limited / busy backoff
TITLE_LIMIT = 80                # research section 6: server-side truncation
BODY_LIMIT = 240

HAND = "✋"                 # raised hand, toast only (never stdout)
CHECK = "✅"

RETRY_REASONS = ("rate_limited", "busy", "error")
QUIET_REASONS = ("disabled", "no_foreground_client")


@dataclass(frozen=True)
class Notification:
    """One toast to send, plus the desks it accounts for (for rollback)."""
    title: str
    body: str
    sound: str
    pane_ids: Tuple[str, ...]
    kind: str = "blocked"       # "blocked" | "done"


@dataclass
class _Entry:
    blocked_since: float
    notified_at: Optional[float] = None
    repeat_count: int = 0
    due_at: Optional[float] = None        # when the collection window opened
    _prev_notified_at: Optional[float] = field(default=None, repr=False)


class Escalator:
    def __init__(self, *, threshold_s=90.0, renotify_s=300.0, sound="request",
                 notify_done=False, name_fn=None, now=None):
        self.threshold_s = float(threshold_s)
        self.renotify_s = float(renotify_s)
        self.sound = sound
        self.notify_done = notify_done
        self.muted = False
        self._name_fn = name_fn or (lambda desk: desk.display_name)
        self._now = now or time.monotonic
        self._entries: Dict[str, _Entry] = {}
        self._last_status: Dict[str, str] = {}
        self._retry_at: Optional[float] = None
        self._window_floor = 0.0              # unmute time; see _due_entries
        self._escalated = set()
        self._rooms: Dict[str, str] = {}      # workspace_id -> label, per tick

    # -- public ---------------------------------------------------------

    def escalated_ids(self):
        """Pane ids whose blocked time is past the threshold (ESCALATED)."""
        return frozenset(self._escalated)

    def tick(self, desks, rooms=None) -> List[Notification]:
        """Advance timers over the current desks; return toasts to send."""
        now = self._now()
        self._rooms = rooms or {}
        alive = set()
        done_desks = []
        for desk in desks:
            alive.add(desk.pane_id)
            previous = self._last_status.get(desk.pane_id)
            self._last_status[desk.pane_id] = desk.status
            if desk.status == "blocked":
                self._track_blocked(desk, now)
                continue
            self._forget(desk.pane_id)
            if (self.notify_done and desk.status == "done"
                    and previous is not None and previous != "done"):
                done_desks.append(desk)
        for pane_id in [p for p in self._entries if p not in alive]:
            self._forget(pane_id)
        for pane_id in [p for p in self._last_status if p not in alive]:
            del self._last_status[pane_id]

        by_id = {desk.pane_id: desk for desk in desks}
        self._escalated = {
            pane_id for pane_id, entry in self._entries.items()
            if now - entry.blocked_since >= self.threshold_s
        }
        if self.muted:
            # Stay silent, but do not bank up a backlog: the collection window
            # restarts from the moment the user unmutes.
            for entry in self._entries.values():
                entry.due_at = None
            self._window_floor = now
            return []
        notes = [self._done_note(desk) for desk in done_desks]
        blocked_note = self._blocked_note(by_id, now)
        if blocked_note is not None:
            notes.append(blocked_note)
        return notes

    def on_result(self, note: Notification, reason: str) -> None:
        """Record a delivery outcome (section 7 reason handling)."""
        if note.kind != "blocked":
            return
        if reason in RETRY_REASONS:
            now = self._now()
            self._retry_at = now + RETRY_AFTER_S
            for pane_id in note.pane_ids:            # roll the batch back
                entry = self._entries.get(pane_id)
                if entry is None or entry.notified_at is None:
                    continue
                entry.notified_at = entry._prev_notified_at
                entry.repeat_count = max(0, entry.repeat_count - 1)
                entry.due_at = None
        # "shown" and the quiet reasons both leave the optimistic mark from
        # tick() in place: a toast the user disabled must not become a retry
        # loop, the on-screen raised hand already carries the signal.

    # -- internals ------------------------------------------------------

    def _track_blocked(self, desk, now):
        entry = self._entries.get(desk.pane_id)
        if entry is None:
            # A desk with no blocked_since (a status event that arrived before
            # the state model had a timestamp) starts its countdown now.
            self._entries[desk.pane_id] = _Entry(
                blocked_since=now if desk.blocked_since is None
                else desk.blocked_since)
        elif desk.blocked_since is not None:
            # Never let a missing timestamp push the countdown forward: that
            # would reset the clock on every single tick.
            entry.blocked_since = desk.blocked_since

    def _forget(self, pane_id):
        self._entries.pop(pane_id, None)
        self._escalated.discard(pane_id)

    def _crossing(self, entry):
        """Absolute time this entry becomes eligible, or None if it never is."""
        if entry.notified_at is None:
            return entry.blocked_since + self.threshold_s
        if self.renotify_s <= 0:
            return None                          # renotify explicitly disabled
        return entry.notified_at + self.renotify_s

    def _due_entries(self, now):
        due = []
        for pane_id, entry in self._entries.items():
            crossing = self._crossing(entry)
            if crossing is None or now < crossing:
                entry.due_at = None
                continue
            if entry.due_at is None:
                # The collection window opens when the desk actually came due,
                # not when this tick happened to notice, so toast timing does
                # not drift with the animation frame rate. `_window_floor`
                # holds it back to the moment escalation was unmuted.
                entry.due_at = max(crossing, self._window_floor)
            due.append((pane_id, entry))
        return due

    def _blocked_note(self, by_id, now):
        due = self._due_entries(now)
        if not due:
            return None
        if self._retry_at is not None:
            if now < self._retry_at:
                return None
            self._retry_at = None
        window_opened = min(entry.due_at for _, entry in due)
        if now - window_opened < AGGREGATE_WINDOW_S:
            return None                      # still collecting co-blocked desks
        due = [(pane_id, entry) for pane_id, entry in due if pane_id in by_id]
        if not due:
            return None
        due.sort(key=lambda item: (item[1].blocked_since, item[0]))
        note = self._compose(due, by_id, now)
        for _, entry in due:                 # optimistic mark, undone on retry
            entry._prev_notified_at = entry.notified_at
            entry.notified_at = now
            entry.repeat_count += 1
            entry.due_at = None
        return note

    def _compose(self, due, by_id, now):
        desks = [by_id[pane_id] for pane_id, _ in due]
        # 1-based index of the toast being composed: repeat_count counts the
        # ones already delivered, so the second toast reads "2nd reminder".
        repeat = max(entry.repeat_count for _, entry in due) + 1
        if len(desks) == 1:
            desk = desks[0]
            entry = due[0][1]
            title = "%s %s is waiting" % (HAND, self._name_fn(desk))
            parts = ["blocked for %s" % _duration(now - entry.blocked_since)]
            room = _room_label(desk, self._rooms)
            if room:
                parts.append("in %s" % room)
            body = " ".join(parts)
            label = desk.state_labels.get("blocked")
            if label:
                body += " - %s" % label
        else:
            title = "%s %d agents are waiting" % (HAND, len(desks))
            body = "; ".join(
                "%s (%s)" % (self._name_fn(desk),
                             _duration(now - entry.blocked_since))
                for desk, (_, entry) in zip(desks, due))
        if repeat > 1:
            body = "%s reminder. %s" % (_ordinal(repeat), body)
        return Notification(title=_clip(title, TITLE_LIMIT),
                            body=_clip(body, BODY_LIMIT),
                            sound=self.sound,
                            pane_ids=tuple(pane_id for pane_id, _ in due),
                            kind="blocked")

    def _done_note(self, desk):
        room = _room_label(desk, self._rooms)
        body = "finished%s" % (" in %s" % room if room else "")
        return Notification(
            title=_clip("%s %s is done" % (CHECK, self._name_fn(desk)),
                        TITLE_LIMIT),
            body=_clip(body, BODY_LIMIT),
            sound="done" if self.sound != "none" else "none",
            pane_ids=(desk.pane_id,), kind="done")


def _room_label(desk, rooms):
    return rooms.get(desk.workspace_id, desk.workspace_id)


def _duration(seconds: float) -> str:
    total = max(0, int(seconds))
    if total < 60:
        return "%ds" % total
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return "%dm%02ds" % (minutes, secs)
    hours, minutes = divmod(minutes, 60)
    return "%dh%02dm" % (hours, minutes)


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        return "%dth" % n
    return "%d%s" % (n, {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th"))


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())              # server normalises anyway
    return text if len(text) <= limit else text[:limit - 1] + "…"
