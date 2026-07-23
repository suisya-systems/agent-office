"""OfficeState - the pure state model (design.md section 4).

No socket, no rendering, no wall-clock dependency (time is injected as `now`).
This is the single unit-tested component: events go in, an ordered set of
desks comes out. herdr's AgentStatus is the only source of truth; we never
guess status ourselves.
"""

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

STATUSES = ("idle", "working", "blocked", "done", "unknown")


@dataclass
class Desk:
    pane_id: str
    workspace_id: str
    tab_id: str = ""
    agent: Optional[str] = None
    display_agent: Optional[str] = None
    label: Optional[str] = None
    terminal_title: Optional[str] = None      # terminal_title_stripped
    status: str = "unknown"
    status_since: float = 0.0
    blocked_since: Optional[float] = None
    state_labels: Dict[str, str] = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        """design.md section 4: display_agent > label > title > agent > id."""
        for candidate in (self.display_agent, self.label,
                          self.terminal_title, self.agent):
            if candidate:
                return candidate
        return self.pane_id

    @property
    def state_label_word(self) -> Optional[str]:
        """First word of the current status's state_label, if any (bubble)."""
        text = self.state_labels.get(self.status)
        return text.split()[0] if text else None


class OfficeState:
    def __init__(self, self_pane_id: Optional[str] = None,
                 filter_mode: str = "agents",
                 now: Callable[[], float] = None):
        import time
        self.desks: Dict[str, Desk] = {}
        self.rooms: Dict[str, str] = {}          # workspace_id -> label
        self.focused_pane_id: Optional[str] = None
        self.selected_pane_id: Optional[str] = None
        self.self_pane_id = self_pane_id
        self.filter_mode = filter_mode           # "agents" | "all"
        self._now = now or time.monotonic

    # -- membership -----------------------------------------------------

    def _visible(self, pane_id: str, agent: Optional[str]) -> bool:
        if pane_id == self.self_pane_id:
            return False
        if self.filter_mode == "all":
            return True
        return agent is not None

    def ingest_pane(self, info: dict) -> None:
        """Upsert from a full PaneInfo (pane.list / pane.created / .updated).

        Idempotent: re-applying the same info is a no-op for timers because
        _set_status only moves status_since when the status actually changes.
        """
        pane_id = info.get("pane_id")
        if not pane_id:
            return
        agent = info.get("agent")
        if not self._visible(pane_id, agent):
            # A pane that stopped qualifying (e.g. agent released under
            # filter="agents") should be removed if we were showing it.
            self.desks.pop(pane_id, None)
            self._fix_selection()
            return
        desk = self.desks.get(pane_id)
        if desk is None:
            desk = Desk(pane_id=pane_id, workspace_id=info.get("workspace_id", ""))
            self.desks[pane_id] = desk
        desk.workspace_id = info.get("workspace_id", desk.workspace_id)
        desk.tab_id = info.get("tab_id", desk.tab_id)
        # A full PaneInfo always carries agent_status, so its agent field is
        # authoritative (agent=None means the agent was released -> clear it, so
        # display falls back to title/label/id). The agent_detected partial has
        # no agent_status; treat it as a hint that only sets a present agent.
        if "agent_status" in info:
            desk.agent = agent
        elif agent is not None:
            desk.agent = agent
        if info.get("display_agent") is not None:
            desk.display_agent = info["display_agent"]
        if info.get("label") is not None:
            desk.label = info["label"]
        if info.get("terminal_title_stripped") is not None:
            desk.terminal_title = info["terminal_title_stripped"]
        if isinstance(info.get("state_labels"), dict):
            desk.state_labels = info["state_labels"]
        self._set_status(desk, info.get("agent_status", desk.status))
        self._fix_selection()

    def set_status(self, pane_id: str, status: str, *, agent=None,
                   display_agent=None, title=None, state_labels=None,
                   workspace_id=None) -> None:
        """From pane.agent_status_changed (per-pane subscription)."""
        desk = self.desks.get(pane_id)
        if desk is None:
            # Unknown pane: create a minimal desk if it would be visible.
            if not self._visible(pane_id, agent):
                return
            desk = Desk(pane_id=pane_id, workspace_id=workspace_id or "")
            self.desks[pane_id] = desk
        if agent is not None:
            desk.agent = agent
        if display_agent is not None:
            desk.display_agent = display_agent
        if title is not None:
            desk.terminal_title = title
        if isinstance(state_labels, dict):
            desk.state_labels = state_labels
        self._set_status(desk, status)
        self._fix_selection()

    def _set_status(self, desk: Desk, status: str) -> None:
        if status not in STATUSES:
            status = "unknown"
        if status == desk.status and desk.status_since:
            return                                # no change: keep timers
        desk.status = status
        desk.status_since = self._now()
        if status == "blocked":
            if desk.blocked_since is None:
                desk.blocked_since = desk.status_since
        else:
            desk.blocked_since = None

    def remove_pane(self, pane_id: str) -> None:
        if self.desks.pop(pane_id, None) is not None:
            self._fix_selection()

    def reconcile_snapshot(self, panes) -> None:
        """Apply a full pane.list snapshot as authoritative membership.

        Upserts every pane, then drops desks whose pane is absent (covers
        panes closed while we were disconnected / re-establishing).
        """
        for info in panes:
            self.ingest_pane(info)
        present = {info.get("pane_id") for info in panes}
        for pid in [d.pane_id for d in self.desks.values() if d.pane_id not in present]:
            self.desks.pop(pid, None)
        self._fix_selection()

    def set_focused(self, pane_id: Optional[str]) -> None:
        self.focused_pane_id = pane_id

    def set_room_label(self, workspace_id: str, label: str) -> None:
        self.rooms[workspace_id] = label

    def remove_room(self, workspace_id: str) -> None:
        self.rooms.pop(workspace_id, None)
        for pid in [d.pane_id for d in self.desks.values()
                    if d.workspace_id == workspace_id]:
            self.desks.pop(pid, None)
        self._fix_selection()

    def set_filter(self, mode: str) -> None:
        if mode in ("agents", "all"):
            self.filter_mode = mode

    # -- ordering / views ----------------------------------------------

    def ordered_desks(self) -> List[Desk]:
        """Stable order: workspace_id, tab_id, pane_id (design.md section 4)."""
        return sorted(self.desks.values(),
                      key=lambda d: (d.workspace_id, d.tab_id, d.pane_id))

    def islands(self):
        """List of (workspace_id, room_label, [Desk,...]) in stable order."""
        out = []
        current = None
        for desk in self.ordered_desks():
            if current is None or current[0] != desk.workspace_id:
                current = (desk.workspace_id,
                           self.rooms.get(desk.workspace_id, desk.workspace_id),
                           [])
                out.append(current)
            current[2].append(desk)
        return out

    def blocked_desks(self) -> List[Desk]:
        """Blocked desks, oldest raised hand first (tiebreak pane_id)."""
        blocked = [d for d in self.desks.values() if d.status == "blocked"]
        return sorted(blocked, key=lambda d: (d.blocked_since or 0.0, d.pane_id))

    def oldest_blocked(self) -> Optional[Desk]:
        blocked = self.blocked_desks()
        return blocked[0] if blocked else None

    # -- selection ------------------------------------------------------

    def _fix_selection(self) -> None:
        if self.selected_pane_id not in self.desks:
            first = self.ordered_desks()
            self.selected_pane_id = first[0].pane_id if first else None

    def selected_desk(self) -> Optional[Desk]:
        return self.desks.get(self.selected_pane_id) if self.selected_pane_id else None

    def select(self, pane_id: str) -> None:
        if pane_id in self.desks:
            self.selected_pane_id = pane_id

    def move_selection(self, dx: int, dy: int, per_row: int) -> None:
        """Move the cursor over the flat ordered grid laid out `per_row` wide."""
        order = self.ordered_desks()
        if not order:
            return
        per_row = max(1, per_row)
        ids = [d.pane_id for d in order]
        try:
            idx = ids.index(self.selected_pane_id)
        except ValueError:
            idx = 0
        idx = max(0, min(len(ids) - 1, idx + dx + dy * per_row))
        self.selected_pane_id = ids[idx]

    def select_next_blocked(self) -> Optional[Desk]:
        """Cycle selection to the next blocked desk (Tab)."""
        blocked = self.blocked_desks()
        if not blocked:
            return None
        ids = [d.pane_id for d in blocked]
        if self.selected_pane_id in ids:
            nxt = ids[(ids.index(self.selected_pane_id) + 1) % len(ids)]
        else:
            nxt = ids[0]
        self.selected_pane_id = nxt
        return self.desks[nxt]
