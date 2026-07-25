"""Single-shot herdr actions (design.md section 6).

`agent-office.open` and `agent-office.jump-blocked` are global actions that
must work whether or not the office pane is running, so each is a short-lived
process: read `pane.list`, decide, issue one command, exit.

Both consult `state.json` (section 8) when an office process is actively
writing it, which makes them exact rather than approximate - the recorded
blocked_since gives the genuinely longest-blocked agent, and the recorded pane
id identifies the office pane outright. Both degrade cleanly without it:
jump-blocked falls back to the pane_id tiebreak of section 6, and open just
opens a new pane.

The choosing is kept apart from the doing. `visible_panes()` and
`pick_blocked()` answer "which pane should this target?" and are directly
testable, while the `action_*` entry points own the socket calls and the exit
codes. Messages are ASCII only (Windows cp932 safety).
"""

import os
import sys

from . import config as config_mod
from . import protocol, statefile
from .state import OfficeState

PANE_TITLE = "Agent Office"          # manifest [[panes]].title == the pane label


def office_entrypoint(os_name=None):
    """The manifest pane id to open for this platform.

    The manifest declares the pane twice because the interpreter argv differs
    per platform (see herdr-plugin.toml), and herdr requires ids to be unique
    - so "which pane do I open" has a platform-dependent answer, and asking
    for the wrong one comes back as `platform_unsupported`.
    """
    name = os.name if os_name is None else os_name
    return "office-windows" if name == "nt" else "office"


def _sock():
    sock = os.environ.get("HERDR_SOCKET_PATH")
    if not sock:
        sys.stderr.write("HERDR_SOCKET_PATH not set; run me from herdr.\n")
        raise SystemExit(2)
    return sock


def _state():
    return statefile.read(statefile.state_path())


# -- target selection (no I/O beyond the optional label lookup) ----------

def visible_panes(sock, panes, cfg):
    """Narrow a pane.list to what the office itself would show.

    Without this the global jump action could focus a pane the user filtered
    out with [include] (design.md section 8) - an excluded `codex` agent, say,
    that happens to be the only blocked one. Rather than reimplement the rules,
    this runs the panes through OfficeState, so the action and the resident
    view can never disagree about who is in the fleet.
    """
    state = OfficeState(filter_mode=cfg.filter,
                        workspace_globs=cfg.workspaces,
                        exclude_agents=cfg.exclude_agents)
    if cfg.workspaces:
        # Workspace globs match the label, which pane.list does not carry.
        try:
            for workspace in protocol.workspace_list(sock):
                wid, label = (workspace.get("workspace_id"),
                              workspace.get("label"))
                if wid and label:
                    state.set_room_label(wid, label)
        except Exception:                                 # noqa: BLE001
            pass                        # fall back to matching raw ids
    state.reconcile_snapshot(panes)
    return [pane for pane in panes if pane.get("pane_id") in state.desks]


def pick_blocked(panes, blocked_since_by_pane=None):
    """Longest-blocked pane id among `panes`, or None.

    pane.list alone cannot say *how long* a pane has been blocked, so panes
    with a recorded blocked_since sort first (oldest wins) and the rest keep
    the section 6 pane_id tiebreak behind them.
    """
    blocked_since_by_pane = blocked_since_by_pane or {}
    blocked = [p for p in panes if p.get("agent_status") == "blocked"
               and p.get("pane_id")]
    if not blocked:
        return None

    def sort_key(pane):
        recorded = blocked_since_by_pane.get(pane["pane_id"])
        known = recorded is not None
        return (0 if known else 1, recorded if known else 0.0, pane["pane_id"])
    return sorted(blocked, key=sort_key)[0]["pane_id"]


def focus_confirmed(result):
    """Did herdr's reply say the pane now holds the focus? (issue #20)

    `plugin.pane.focus` answering without an error only means the request was
    accepted, and action-open used to read that as "focused" and exit 0 - so a
    focus that was accepted and then did nothing was indistinguishable from a
    focus that worked, which is the half of issue #20 that made it hard to
    notice. The reply does carry the answer, nested as
    `plugin_pane.pane.focused` on 0.7.5 (a flat `pane` is accepted too, since
    the shape is not part of any documented contract).

    Three-valued on purpose. None means *the reply did not say*, which is what
    a herdr whose reply shape we have not seen looks like, and it must not be
    confused with a herdr that positively reported the pane still unfocused:
    only the latter is worth opening a second pane over.
    """
    if not isinstance(result, dict):
        return None
    pane = result.get("plugin_pane")
    pane = pane.get("pane") if isinstance(pane, dict) else result.get("pane")
    if not isinstance(pane, dict) or "focused" not in pane:
        return None
    return bool(pane["focused"])


def running_office_pane(panes, data):
    """Identify the live office pane, preferring state.json's exact id.

    state.json names the pane the office process is actually drawing in, and
    is only trusted while it is fresh (statefile.FRESH_S) - a stale file means
    the office died and herdr may have recycled that pane id. The label match
    is a fallback for herdr builds that expose `label` in pane.list; 0.7.4
    does not, so on 0.7.4 an office started outside this plugin's state dir
    simply results in a second pane being opened.
    """
    live = {p.get("pane_id") for p in panes if p.get("pane_id")}
    recorded = statefile.live_office_pane_id(data)
    if recorded and recorded in live:
        return recorded
    for pane in panes:
        if pane.get("label") == PANE_TITLE and pane.get("pane_id"):
            return pane["pane_id"]
    return None


# -- entry points (own the socket calls and the exit codes) --------------

def action_open():
    sock = _sock()
    plugin_id = os.environ.get("HERDR_PLUGIN_ID", "agent-office")
    try:
        panes = protocol.pane_list(sock)
    except Exception:                                    # noqa: BLE001
        panes = []
    target = running_office_pane(panes, _state())
    if target:
        try:
            confirmed = focus_confirmed(
                protocol.request(sock, "plugin.pane.focus", {"pane_id": target}))
        except Exception as exc:                         # noqa: BLE001
            sys.stderr.write("focus of %s failed: %s\n" % (target, exc))
        else:
            if confirmed is False:
                # herdr answered, and its answer was "still not focused".
                # Opening is the only recovery this action has, and it is what
                # the action would have done had the pane not been found at
                # all - so take it rather than exiting 0 on a no-op.
                sys.stderr.write(
                    "focus of %s was accepted but the pane is not focused; "
                    "opening a new one.\n" % target)
            else:
                if confirmed is None:
                    # Ambiguous, not failed. Opening here would mean a second
                    # office pane on every invocation for anyone whose herdr
                    # simply words the reply differently, which is worse than
                    # the no-op; a line in `herdr plugin log` is enough to see
                    # that the focus went unverified.
                    sys.stderr.write(
                        "focus of %s was accepted but herdr did not report "
                        "the pane focused.\n" % target)
                return 0
    try:
        protocol.request(sock, "plugin.pane.open",
                         {"plugin_id": plugin_id,
                          "entrypoint": office_entrypoint(),
                          "focus": True})
    except Exception as exc:                              # noqa: BLE001
        sys.stderr.write("open failed: %s\n" % exc)
        return 1
    return 0


def action_jump_blocked():
    sock = _sock()
    try:
        panes = protocol.pane_list(sock)
    except Exception as exc:                              # noqa: BLE001
        sys.stderr.write("pane.list failed: %s\n" % exc)
        return 1
    panes = visible_panes(sock, panes, config_mod.load())
    # design.md section 6: the recorded blocked_since is authoritative only
    # *while the office is running*. A stopped file's timestamps predate an
    # unknown stretch of time in which an agent may have unblocked and blocked
    # again, which would confidently rank the wrong pane first; the pane_id
    # tiebreak is the honest answer there.
    data = _state()
    recorded = statefile.blocked_epoch_map(data) if statefile.is_live(data) else {}
    target = pick_blocked(panes, recorded)
    if not target:
        sys.stderr.write("no blocked agents.\n")
        return 0
    try:
        protocol.pane_focus(sock, target)
    except Exception as exc:                              # noqa: BLE001
        sys.stderr.write("pane.focus failed: %s\n" % exc)
        return 1
    return 0
