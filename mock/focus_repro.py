#!/usr/bin/env python3
"""Repro harness for issue #21 - the office handing its own focus away.

The bug needs a keystroke to reach the office pane within milliseconds of that
pane gaining the focus, which is why hand-driven testing hit it and scripted
runs did not. This drives that timing directly against a real herdr server, so
the bounce is reproducible on demand.

What it does, given a throwaway session's socket plus two pane ids:

  1. park the focus on `--user` and confirm herdr agrees
  2. focus the office pane (`plugin.pane.focus`, what action-open does, falling
     back to `pane.focus` when the office is not a plugin-owned pane - the two
     are indistinguishable from the office's side, which learns of the change
     only from the `pane.focused` event either one emits)
  3. after --delay-ms, pane.send_keys(--office, Enter)
     (a keystroke the user aimed at --user, delivered to the office because the
      focus moved out from under it). A negative --delay-ms sends the key
      *before* the focus request, which is the other order the two can reach
      the office in - they travel down different threads.
  4. ask herdr who holds the focus now

--expect says which outcome passes, because both are correct at different
delays: an enter that arrives with the focus must be ignored (`keep`), while
one the user typed at an office they can see must still jump (`jump`). Exit 1
on anything else.

Setting up an isolated session (never run this against the session you work
in: it moves the focus around):

  # 1. headless server for a throwaway session. The client cannot draw without
  #    a tty and will panic, but the server it started stays up, which is all
  #    this harness needs.
  env -u HERDR_ENV -u HERDR_PANE_ID -u HERDR_SOCKET_PATH \\
      -u HERDR_TAB_ID -u HERDR_WORKSPACE_ID \\
      herdr --session ao-repro < /dev/null > /dev/null 2>&1 &
  S=~/.config/herdr/sessions/ao-repro/herdr.sock

  # 2. a config dir with filter = "all", so the plain shell panes of an empty
  #    session count as desks (with the default filter = "agents" there would
  #    be nothing to jump to and enter would be a no-op).
  mkdir -p /tmp/ao/cfg /tmp/ao/state
  printf '[office]\\nfilter = "all"\\n' > /tmp/ao/cfg/config.toml

  # 3. the office pane
  HERDR_SOCKET_PATH=$S herdr tab create --cwd <plugin root> --no-focus \\
      --label "Agent Office" \\
      --env HERDR_PLUGIN_CONFIG_DIR=/tmp/ao/cfg \\
      --env HERDR_PLUGIN_STATE_DIR=/tmp/ao/state
  HERDR_SOCKET_PATH=$S herdr pane send-text <office pane> 'python3 -m office run'
  HERDR_SOCKET_PATH=$S herdr pane send-keys <office pane> Enter

  # 4. the repro, and the deliberate jump it must not break
  python3 mock/focus_repro.py --sock $S --office w1:p2 --user w1:p1
  python3 mock/focus_repro.py --sock $S --office w1:p2 --user w1:p1 \\
      --delay-ms 500 --expect jump

  # 5. tear the session down
  herdr session stop ao-repro
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from office import protocol                                    # noqa: E402


def focused_pane(sock):
    for pane in protocol.pane_list(sock):
        if pane.get("focused"):
            return pane.get("pane_id")
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Reproduce issue #21: the office pane handing its focus "
                    "away on an enter that arrives as it gains focus")
    ap.add_argument("--sock", required=True,
                    help="herdr socket of a throwaway session")
    ap.add_argument("--office", required=True, help="pane id of the office")
    ap.add_argument("--user", required=True,
                    help="pane id the focus starts on (a desk in the office)")
    ap.add_argument("--delay-ms", type=float, default=0.0,
                    help="wait this long after the focus before the keystroke; "
                         "negative sends the keystroke first")
    ap.add_argument("--key", default="Enter", help="key name to deliver")
    ap.add_argument("--expect", choices=("keep", "jump"), default="keep",
                    help="which outcome passes (default: keep)")
    ap.add_argument("--settle", type=float, default=1.0,
                    help="seconds to wait before reading the focus back")
    args = ap.parse_args(argv)

    protocol.request(args.sock, "pane.focus", {"pane_id": args.user})
    time.sleep(0.4)
    before = focused_pane(args.sock)
    print("step 1: focused=%s (want %s)" % (before, args.user))
    if before != args.user:
        print("SETUP FAILED: the focus would not park on %s" % args.user)
        return 2

    t0 = time.monotonic()

    def send_focus():
        method = "plugin.pane.focus"
        try:
            protocol.request(args.sock, method, {"pane_id": args.office})
        except protocol.ProtocolError as exc:
            if exc.code != "plugin_pane_not_found":
                raise
            method = "pane.focus"
            protocol.request(args.sock, method, {"pane_id": args.office})
        print("  %s(%s) at +%.1fms"
              % (method, args.office, (time.monotonic() - t0) * 1000))

    def send_key():
        protocol.request(args.sock, "pane.send_keys",
                         {"pane_id": args.office, "keys": [args.key]})
        print("  send_keys(%s, %s) at +%.1fms"
              % (args.office, args.key, (time.monotonic() - t0) * 1000))

    print("steps 2-3:")
    if args.delay_ms < 0:
        send_key()
        time.sleep(-args.delay_ms / 1000.0)
        send_focus()
    else:
        send_focus()
        if args.delay_ms:
            time.sleep(args.delay_ms / 1000.0)
        send_key()

    time.sleep(args.settle)
    after = focused_pane(args.sock)
    outcome = "keep" if after == args.office else "jump"
    print("step 4: focused=%s after %.1fs -> %s (expected %s)"
          % (after, args.settle, outcome, args.expect))
    if outcome == args.expect:
        print("RESULT: PASS")
        return 0
    if outcome == "jump":
        print("RESULT: FAIL - the office handed the focus to %s" % after)
    else:
        print("RESULT: FAIL - the office did not jump")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
