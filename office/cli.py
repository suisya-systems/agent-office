"""Command-line dispatch for the Agent Office plugin.

Subcommands (invoked by herdr via the manifest, CWD = plugin root):
  run                    run the resident office pane (default)
  action-open            focus the running office pane, or open one
  action-jump-blocked    focus the longest-blocked agent
  config-check           validate config.toml and print the effective settings

This module only maps argv to a handler and prints; `run` lives in office.py
and the two global actions in actions.py. Help/print text is ASCII only
(Windows cp932 safety).
"""

import os
import sys

from . import actions
from . import config as config_mod
from . import office

USAGE = """Agent Office - herdr plugin

usage: python3 -m office [run|action-open|action-jump-blocked|config-check]

  run                  run the resident office pane (default)
  action-open          focus the running office pane, or open one
  action-jump-blocked  focus the longest-blocked agent
  config-check         validate config.toml and show the effective settings
"""

CONFIG_SECTIONS = (
    ("office", ("filter", "renderer", "fps", "theme", "name_template")),
    ("escalation", ("blocked_threshold_s", "renotify_interval_s", "sound",
                    "notify_done")),
    ("include", ("workspaces", "exclude_agents")),
)


def config_check():
    """Print the effective configuration; non-zero if anything was rejected."""
    cfg = config_mod.load()
    path = config_mod.config_path()
    sys.stdout.write("config file: %s\n"
                     % (path or "(HERDR_PLUGIN_CONFIG_DIR unset)"))
    if not path or not os.path.exists(path):
        sys.stdout.write("  (not present - using defaults, zero-config)\n")
    for section, keys in CONFIG_SECTIONS:
        sys.stdout.write("[%s]\n" % section)
        for key in keys:
            # ascii() rather than %r: a workspace glob or agent name may hold
            # non-ASCII the console cannot encode (Windows cp932).
            sys.stdout.write("  %-20s %s\n" % (key, ascii(getattr(cfg, key))))
    for warning in cfg.warnings:
        sys.stdout.write("warning: %s\n" % warning)
    return 1 if cfg.warnings else 0


COMMANDS = {
    "run": office.run,
    "office": office.run,
    "action-open": actions.action_open,
    "action-jump-blocked": actions.action_jump_blocked,
    "config-check": config_check,
}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "run"
    if cmd in ("-h", "--help", "help"):
        sys.stdout.write(USAGE)
        return 0
    handler = COMMANDS.get(cmd)
    if handler is None:
        sys.stderr.write("unknown subcommand: %s\n\n%s" % (cmd, USAGE))
        return 2
    return handler()
