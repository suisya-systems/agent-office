"""CLI dispatch for the Agent Office plugin.

Subcommands (invoked by herdr via the manifest, CWD = plugin root):
  run                    run the resident office pane (default)
  action-open            open (or re-open) the office pane
  action-jump-blocked    focus the longest-blocked agent
  config-check           validate config.toml and print the effective settings

The single-shot actions consult `state.json` (design.md section 8) when the
office pane is running, which makes them exact rather than approximate: the
recorded blocked_since gives the genuinely longest-blocked agent, and the
recorded office pane id identifies the office pane outright. Both degrade
cleanly when the office is not running - jump-blocked falls back to the pane_id
tiebreak of section 6, and action-open just opens a new pane.

Help/print text is ASCII only (Windows cp932 safety).
"""

import os
import sys

from . import config as config_mod
from . import office, protocol, statefile

PANE_TITLE = "Agent Office"          # manifest [[panes]].title == the pane label

USAGE = """Agent Office - herdr plugin

usage: python3 -m office [run|action-open|action-jump-blocked|config-check]

  run                  run the resident office pane (default)
  action-open          focus the running office pane, or open one
  action-jump-blocked  focus the longest-blocked agent
  config-check         validate config.toml and show the effective settings
"""


def _sock():
    sock = os.environ.get("HERDR_SOCKET_PATH")
    if not sock:
        sys.stderr.write("HERDR_SOCKET_PATH not set; run me from herdr.\n")
        raise SystemExit(2)
    return sock


def _state():
    return statefile.read(statefile.state_path())


def action_open():
    sock = _sock()
    plugin_id = os.environ.get("HERDR_PLUGIN_ID", "agent-office")
    try:
        panes = protocol.pane_list(sock)
    except Exception:                                    # noqa: BLE001
        panes = []
    target = _running_office_pane(panes)
    if target:
        try:
            protocol.request(sock, "plugin.pane.focus", {"pane_id": target})
            return 0
        except Exception:                                # noqa: BLE001
            pass                                         # fall through to open
    try:
        protocol.request(sock, "plugin.pane.open",
                         {"plugin_id": plugin_id, "entrypoint": "office",
                          "focus": True})
    except Exception as exc:                              # noqa: BLE001
        sys.stderr.write("open failed: %s\n" % exc)
        return 1
    return 0


def _running_office_pane(panes):
    """Identify the live office pane, preferring state.json's exact id.

    state.json names the pane the office process is actually drawing in, and
    is only trusted while it is fresh (statefile.FRESH_S) - a stale file means
    the office died and herdr may have recycled that pane id. The label match
    is a fallback for herdr builds that expose `label` in pane.list; 0.7.4
    does not, so on 0.7.4 an office started outside this plugin's state dir
    simply results in a second pane being opened.
    """
    live = {p.get("pane_id") for p in panes if p.get("pane_id")}
    recorded = statefile.live_office_pane_id(_state())
    if recorded and recorded in live:
        return recorded
    for pane in panes:
        if pane.get("label") == PANE_TITLE and pane.get("pane_id"):
            return pane["pane_id"]
    return None


def action_jump_blocked():
    sock = _sock()
    try:
        panes = protocol.pane_list(sock)
    except Exception as exc:                              # noqa: BLE001
        sys.stderr.write("pane.list failed: %s\n" % exc)
        return 1
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


def config_check():
    """Print the effective configuration; non-zero if anything was rejected."""
    cfg = config_mod.load()
    path = config_mod.config_path() or "(HERDR_PLUGIN_CONFIG_DIR unset)"
    sys.stdout.write("config file: %s\n" % path)
    if not os.path.exists(config_mod.config_path() or ""):
        sys.stdout.write("  (not present - using defaults, zero-config)\n")
    for section, keys in (
            ("office", ("filter", "renderer", "fps", "theme", "name_template")),
            ("escalation", ("blocked_threshold_s", "renotify_interval_s",
                            "sound", "notify_done")),
            ("include", ("workspaces", "exclude_agents"))):
        sys.stdout.write("[%s]\n" % section)
        for key in keys:
            # ascii() rather than %r: a workspace glob or agent name may hold
            # non-ASCII the console cannot encode (Windows cp932).
            sys.stdout.write("  %-20s %s\n" % (key, ascii(getattr(cfg, key))))
    for warning in cfg.warnings:
        sys.stdout.write("warning: %s\n" % warning)
    return 1 if cfg.warnings else 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "run"
    if cmd in ("-h", "--help", "help"):
        sys.stdout.write(USAGE)
        return 0
    if cmd in ("run", "office"):
        return office.run()
    if cmd == "action-open":
        return action_open()
    if cmd == "action-jump-blocked":
        return action_jump_blocked()
    if cmd == "config-check":
        return config_check()
    sys.stderr.write("unknown subcommand: %s\n\n%s" % (cmd, USAGE))
    return 2
