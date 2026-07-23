"""CLI dispatch for the Agent Office plugin.

Subcommands (invoked by herdr via the manifest, CWD = plugin root):
  run                    run the resident office pane (default)
  action-open            open (or re-open) the office pane
  action-jump-blocked    focus the longest-blocked agent, stateless variant

Note: the single-shot actions here use the pane_id tiebreak described in
design.md section 6 (no state.json); the state.json-accurate "oldest blocked"
is Stage 2 item 2. Help/print text is ASCII only (Windows cp932 safety).
"""

import os
import sys

from . import office, protocol

USAGE = """Agent Office - herdr plugin (Stage 2 core)

usage: python3 -m office [run|action-open|action-jump-blocked]

  run                  run the resident office pane (default)
  action-open          open the office pane via the plugin API
  action-jump-blocked  focus the longest-blocked agent (pane_id tiebreak)
"""


def _sock():
    sock = os.environ.get("HERDR_SOCKET_PATH")
    if not sock:
        sys.stderr.write("HERDR_SOCKET_PATH not set; run me from herdr.\n")
        raise SystemExit(2)
    return sock


def action_open():
    sock = _sock()
    plugin_id = os.environ.get("HERDR_PLUGIN_ID", "agent-office")
    try:
        protocol.request(sock, "plugin.pane.open",
                         {"plugin_id": plugin_id, "entrypoint": "office",
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
    blocked = sorted((p for p in panes if p.get("agent_status") == "blocked"),
                     key=lambda p: p.get("pane_id", ""))
    if not blocked:
        sys.stderr.write("no blocked agents.\n")
        return 0
    target = blocked[0]["pane_id"]
    try:
        protocol.pane_focus(sock, target)
    except Exception as exc:                              # noqa: BLE001
        sys.stderr.write("pane.focus failed: %s\n" % exc)
        return 1
    return 0


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
    sys.stderr.write("unknown subcommand: %s\n\n%s" % (cmd, USAGE))
    return 2
