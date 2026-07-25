"""Single-shot herdr actions (design.md section 6).

`herdr-agent-office.open` and `herdr-agent-office.jump-blocked` are global
actions that must work whether or not the office pane is running, so each is a
short-lived process: read `pane.list`, decide, issue one command, exit.

Both consult `state.json` (section 8) when an office process is actively
writing it, which makes them exact rather than approximate - the recorded
blocked_since gives the genuinely longest-blocked agent, and the recorded pane
id identifies the office pane outright. Both degrade cleanly without it:
jump-blocked falls back to the pane_id tiebreak of section 6, and open just
opens a new pane.

`herdr-agent-office.startup` is the third short-lived process here, run once
per server start rather than by the user (issue #39): herdr restores the office
pane's frame without re-running its command, and this is what puts the process
back inside it.

The choosing is kept apart from the doing. `visible_panes()`, `pick_blocked()`,
`office_frames()` and `reclaimable_frame()` answer "which pane should this
target?" and are directly testable, while the `action_*` entry points own the
exit codes. `focus_office()` sits between the two: it makes socket calls, but
what it returns is a *finding* - whether the office is focused, missing, or out
of reach - because deciding that from one `except` branch is what opened a
second office in issue #41. Messages are ASCII only (Windows cp932 safety).
"""

import os
import sys

from . import config as config_mod
from . import protocol, statefile
from .state import OfficeState

PANE_TITLE = "Agent Office"          # manifest [[panes]].title == the pane label
OFFICE_MODULE = "office"             # herdr spawns `<python> -m office run`

#: herdr's answer when a pane is not registered to *this plugin*. Measured on
#: 0.7.5 for two different situations: a pane that no longer exists, and a pane
#: that is still there with the office still running in it but whose plugin
#: ownership evaporated (issue #41). The code cannot tell them apart, so
#: receiving it settles nothing on its own.
PLUGIN_PANE_NOT_FOUND = "plugin_pane_not_found"
#: The generic pane API's answer when the pane genuinely is not there. Unlike
#: the above this one *is* decisive - which is why the fallback path asks the
#: generic API rather than guessing.
PANE_NOT_FOUND = "pane_not_found"

# What `focus_office` concluded. Three outcomes, because "the office is gone"
# and "the focus did not land" are different facts and only the first of them
# justifies opening a second office.
FOCUSED = "focused"                  # the office pane has the focus
NO_OFFICE = "no_office"              # nothing live to focus; open one
FOCUS_FAILED = "focus_failed"        # it did not land, and why is unknown


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
    behind it covers an office started outside this plugin's state dir, and is
    always available now that the manifest asks for herdr 0.7.5: 0.7.4 left
    `label` out of pane.list, which is why this is written as a fallback.

    Neither signal is affected by the ownership loss of issue #41 (measured on
    0.7.5 across a live handoff): the pane keeps its id, pane.list keeps
    reporting its `label`, and the office process keeps writing state.json
    because it never noticed. So this still names the right pane there - it is
    what happens to the answer afterwards that was wrong.
    """
    live = {p.get("pane_id") for p in panes if p.get("pane_id")}
    recorded = statefile.live_office_pane_id(data)
    if recorded and recorded in live:
        return recorded
    for pane in panes:
        if pane.get("label") == PANE_TITLE and pane.get("pane_id"):
            return pane["pane_id"]
    return None


def office_frames(panes, data):
    """Panes wearing this plugin's label, the recorded one first (issue #39).

    `running_office_pane` answers the same question for action-open, but the
    startup hook *closes* what it picks, so it cannot inherit that function's
    willingness to act on state.json's recorded pane id by itself: a restart
    may have handed that id to an unrelated pane, and closing a stranger's pane
    over a stale record is not a risk worth taking to save a tab. Requiring the
    label costs nothing here: the manifest's floor is herdr 0.7.5, which does
    report `label` in pane.list (and which is also the first herdr with the
    `[[startup]]` hook that calls this).

    The recorded id still decides the *order*, which is what settles matters if
    more than one pane wears the label.
    """
    frames = [pane["pane_id"] for pane in panes
              if pane.get("label") == PANE_TITLE and pane.get("pane_id")]
    recorded = statefile.live_office_pane_id(data)
    if recorded in frames:
        frames.remove(recorded)
        frames.insert(0, recorded)
    return frames


def _foreground(info):
    """The `foreground_processes` of a pane.process_info reply, defensively."""
    if not isinstance(info, dict):
        return []
    process_info = info.get("process_info")
    if not isinstance(process_info, dict):
        process_info = info                       # tolerate an unwrapped reply
    procs = process_info.get("foreground_processes")
    if not isinstance(procs, list):
        return []
    return [proc for proc in procs if isinstance(proc, dict)]


def _shell_pid(info):
    if not isinstance(info, dict):
        return None
    process_info = info.get("process_info")
    if not isinstance(process_info, dict):
        process_info = info
    return process_info.get("shell_pid")


def runs_office(info):
    """Is an office process the pane's foreground process?

    Matched on `-m office` rather than on the interpreter, because the manifest
    spawns `python3` on unix and `py -3` on Windows and neither name is the
    point. `argv` is what herdr reports; `cmdline` is consulted as well so a
    herdr that fills in only the flat string cannot make a *live* office look
    dead - that mistake would end with this hook closing a working office.
    """
    for proc in _foreground(info):
        argv = proc.get("argv")
        if isinstance(argv, list):
            for flag, name in zip(argv, argv[1:]):
                if flag == "-m" and name == OFFICE_MODULE:
                    return True
        cmdline = proc.get("cmdline")
        if isinstance(cmdline, str) and "-m %s" % OFFICE_MODULE in cmdline:
            return True
    return False


def reclaimable_frame(info):
    """Does this pane hold nothing but the shell herdr restored it with?

    True only for the litter issue #39 is about: a pane whose single foreground
    process *is* the pane's own shell, sitting at a prompt. Anything else - a
    command the user started in it, an office that is still running, a reply
    that could not be read - answers False, because the caller's next move is
    to close the pane and that is not undoable.
    """
    procs = _foreground(info)
    if len(procs) != 1 or runs_office(info):
        return False
    shell_pid = _shell_pid(info)
    return shell_pid is not None and procs[0].get("pid") == shell_pid


# -- entry points (own the socket calls and the exit codes) --------------

def _open_office(sock, focus):
    """Ask herdr to launch the office pane.

    `focus` is the caller's decision, and the two callers differ: action-open
    is the user asking for the office and wants it in front, while the startup
    hook runs with the user's own pane focused (HERDR_PANE_ID) and must leave
    the focus exactly where it found it - moving it is the failure issue #21
    was about, arriving by a different road.
    """
    plugin_id = os.environ.get("HERDR_PLUGIN_ID", "herdr-agent-office")
    return protocol.request(sock, "plugin.pane.open",
                            {"plugin_id": plugin_id,
                             "entrypoint": office_entrypoint(),
                             "focus": focus})


def action_startup():
    """Put the office process back after a server restart (issue #39).

    herdr restores the *frame* of the office pane - label, cwd, tab position -
    but does not re-run the manifest command, so what comes back is a bare
    shell wearing the "Agent Office" label while the toasts, the escalations
    and state.json are all gone. This hook is what notices.

    Three things it deliberately does not do:

    * **Open the office for someone who closed it.** A restored frame carrying
      our label is the evidence that the office was open when the server went
      down; no frame, no office. herdr not firing this hook at all for a
      disabled plugin (measured) is the same principle one level up.
    * **Trust state.json.** After a restart the file is still fresh and still
      says `running: true`, naming a pane id that now resolves to the restored
      frame - a dead office looks exactly like a live one from there. Only
      `pane.process_info` can tell them apart, so that is what decides.
    * **Take the focus.** The hook runs with the user's pane focused, and the
      office is restored behind it.

    Exit code 0 unless the open itself failed; herdr logs it either way
    (`herdr plugin log list`), which is where a silent no-op becomes visible.
    """
    sock = _sock()
    try:
        panes = protocol.pane_list(sock)
    except Exception as exc:                              # noqa: BLE001
        # The office not coming back is bad; a startup hook that fails loudly
        # on every server start is worse. Say why and leave it.
        sys.stderr.write("pane.list failed: %s\n" % exc)
        return 0

    frames = office_frames(panes, _state())
    if not frames:
        return 0                    # the office was not open. Leave it closed.

    stale = []
    for pane_id in frames:
        try:
            info = protocol.pane_process_info(sock, pane_id)
        except Exception as exc:                          # noqa: BLE001
            sys.stderr.write("process_info of %s failed: %s\n" % (pane_id, exc))
            info = None
        if runs_office(info):
            # A live handoff fires this hook too, and there the process
            # survives (measured). Opening now would mean a second office -
            # and two offices both escalating means two toasts per agent.
            return 0
        if reclaimable_frame(info):
            stale.append(pane_id)

    if not stale:
        sys.stderr.write(
            "office frame(s) %s are not idle shells; leaving them alone.\n"
            % ", ".join(frames))
        return 0

    # Close first, open second. The other order would leave a duplicate behind
    # whenever the close failed, and a duplicate is the one outcome issue #39
    # rules out; a close that succeeds without its open leaves a plugin that is
    # merely as dead as it already was, and says so in the log.
    closed = []
    for pane_id in stale:
        try:
            protocol.pane_close(sock, pane_id)
        except Exception as exc:                          # noqa: BLE001
            sys.stderr.write("close of %s failed: %s\n" % (pane_id, exc))
        else:
            closed.append(pane_id)
    if not closed:
        return 0

    try:
        _open_office(sock, focus=False)
    except Exception as exc:                              # noqa: BLE001
        sys.stderr.write("open failed: %s\n" % exc)
        return 1
    return 0


def _read_focus(target, result):
    """Turn a focus reply into an outcome (issue #20's three values)."""
    confirmed = focus_confirmed(result)
    if confirmed is False:
        # herdr answered, and its answer was "still not focused". Opening is
        # the only recovery this action has, and it is what the action would
        # have done had the pane not been found at all - so take it rather
        # than exiting 0 on a no-op.
        sys.stderr.write("focus of %s was accepted but the pane is not "
                         "focused; opening a new one.\n" % target)
        return NO_OFFICE
    if confirmed is None:
        # Ambiguous, not failed. Opening here would mean a second office pane
        # on every invocation for anyone whose herdr simply words the reply
        # differently, which is worse than the no-op; a line in `herdr plugin
        # log` is enough to see that the focus went unverified.
        sys.stderr.write("focus of %s was accepted but herdr did not report "
                         "the pane focused.\n" % target)
    return FOCUSED


def _focus_unowned(sock, target):
    """`plugin.pane.focus` said not-found. Work out which not-found it was.

    herdr keeps its record of which panes belong to which plugin in the server
    process, so it does not outlive one: a server restart *or* a live handoff
    leaves the pane exactly where it was and the office still running in it,
    while `plugin.pane.focus` starts answering `plugin_pane_not_found` (issue
    #41; `herdr update` performs a handoff, so users meet this on every
    update). The same reply is what a pane that genuinely no longer exists
    gets, and treating both as "no office" is what opened a second one beside
    the first - two panes, two escalators, two state.json writers.

    So ask the questions the plugin API cannot answer, in an order that cannot
    do harm on the way:

    * `pane.process_info` - which distinguishes the three cases outright. It
      answers `pane_not_found` when the pane is gone, reports the office's own
      argv when the office is alive and merely unowned, and reports a bare
      shell for the frame a restart restored without its process (issue #39).
      Only the middle case is worth focusing; the other two need a new office.
    * the generic `pane.focus`, which keeps working on a pane whose plugin
      ownership is gone (measured on 0.7.5, alongside the generic `pane.close`
      the startup hook already relies on for the same reason).

    Asking before focusing is the point of that order: a restored dead frame
    wears our label and would take the focus just as willingly, and moving the
    user to a shell prompt they cannot use is not better than opening the
    office they asked for.
    """
    try:
        info = protocol.pane_process_info(sock, target)
    except protocol.ProtocolError as exc:
        if exc.code == PANE_NOT_FOUND:
            return NO_OFFICE            # it really is gone. Open a fresh one.
        sys.stderr.write("process_info of %s failed: %s\n" % (target, exc))
        return FOCUS_FAILED
    except Exception as exc:                              # noqa: BLE001
        sys.stderr.write("process_info of %s failed: %s\n" % (target, exc))
        return FOCUS_FAILED

    if not runs_office(info):
        # The pane is there but nothing of ours is running in it - a frame a
        # restart restored while the startup hook was unable to reclaim it, or
        # a pane that took our label. Either way the office is missing.
        sys.stderr.write("no office is running in %s; opening a new one.\n"
                         % target)
        return NO_OFFICE

    try:
        result = protocol.pane_focus(sock, target)
    except Exception as exc:                              # noqa: BLE001
        sys.stderr.write("generic focus of %s failed: %s\n" % (target, exc))
        return FOCUS_FAILED
    return _read_focus(target, result)


def focus_office(sock, target):
    """Focus the office already running in `target`; say what happened.

    Returns FOCUSED, NO_OFFICE or FOCUS_FAILED. Only NO_OFFICE means the
    caller should open a second pane, and that is the whole point of the
    split: the version of this that lived inside `action_open` had one `except`
    over every failure and opened on all of them, so an office that was alive
    and simply unowned got a duplicate (issue #41).
    """
    try:
        result = protocol.request(sock, "plugin.pane.focus",
                                  {"pane_id": target})
    except protocol.ProtocolError as exc:
        if exc.code != PLUGIN_PANE_NOT_FOUND:
            sys.stderr.write("focus of %s failed: %s\n" % (target, exc))
            return FOCUS_FAILED
        # A statement about *ownership*, which says nothing about whether the
        # pane or the office is there. Somebody else has to answer that.
        return _focus_unowned(sock, target)
    except Exception as exc:                              # noqa: BLE001
        sys.stderr.write("focus of %s failed: %s\n" % (target, exc))
        return FOCUS_FAILED
    return _read_focus(target, result)


def action_open():
    sock = _sock()
    try:
        panes = protocol.pane_list(sock)
    except Exception:                                    # noqa: BLE001
        panes = []
    target = running_office_pane(panes, _state())
    if target:
        outcome = focus_office(sock, target)
        if outcome == FOCUSED:
            return 0
        if outcome == FOCUS_FAILED:
            # We could not focus the pane and could not establish that the
            # office is gone. Opening on that would be a guess, and the wrong
            # guess is the duplicate office of issue #41; the failure is in
            # `herdr plugin log` and pressing the key again costs nothing.
            return 1
    try:
        _open_office(sock, focus=True)
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
