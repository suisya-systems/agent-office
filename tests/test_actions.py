"""Unit tests for the single-shot actions (design.md section 6, issue #20).

`action_open` owns socket calls, so the protocol module is swapped out the way
test_reconciler does it and every request is recorded instead of sent. The
question these tests exist to answer is what the action does with the *reply*
to `plugin.pane.focus`: accepting it blindly is what let a focus that did
nothing exit 0 (issue #20), and treating every failure of it as "there is no
office" is what opened a second office beside a running one (issue #41).
"""

import io
import sys
import time
import unittest

from office import actions, protocol

SOCK = "/tmp/fake-herdr.sock"
OFFICE_PANE = "w1Z:p36"


def _office_pane(pane_id=OFFICE_PANE):
    return {"pane_id": pane_id, "label": actions.PANE_TITLE}


def _focus_reply(focused=True, pane_id=OFFICE_PANE):
    """The shape herdr 0.7.5 answers `plugin.pane.focus` with."""
    return {"type": "plugin_pane_focused",
            "plugin_pane": {"plugin_id": "agent-office",
                            "entrypoint": "office",
                            "pane": {"pane_id": pane_id, "focused": focused}}}


class FocusConfirmedTest(unittest.TestCase):
    def test_nested_reply_reports_focused(self):
        self.assertIs(actions.focus_confirmed(_focus_reply(True)), True)

    def test_nested_reply_reports_unfocused(self):
        self.assertIs(actions.focus_confirmed(_focus_reply(False)), False)

    def test_flat_pane_shape_is_accepted(self):
        self.assertIs(actions.focus_confirmed({"pane": {"focused": True}}),
                      True)

    def test_reply_without_the_field_is_unknown_not_failed(self):
        # The distinction the fallback rests on: an unrecognised reply must not
        # be read as "not focused", or it would open a pane every time.
        self.assertIsNone(actions.focus_confirmed({"type": "ok"}))
        self.assertIsNone(actions.focus_confirmed(
            {"plugin_pane": {"pane": {"pane_id": OFFICE_PANE}}}))

    def test_non_dict_replies_are_unknown(self):
        for result in (None, "ok", [], 3):
            self.assertIsNone(actions.focus_confirmed(result))


class ActionOpenTest(unittest.TestCase):
    """action_open with the protocol layer and the state file faked out."""

    def setUp(self):
        self.calls = []                      # (method, params) in order
        self.reply = _focus_reply(True)
        self.raise_on_focus = None
        self.panes = [_office_pane()]
        # Only consulted once `plugin.pane.focus` has answered not-found: the
        # office is alive in the pane, and the generic focus lands (issue #41).
        self.info = _process_info(procs=[_office_proc()])
        self.generic_reply = {"type": "pane_info",
                              "pane": {"pane_id": OFFICE_PANE, "focused": True}}
        self.fail_on = {}                     # method -> exception to raise

        self._saved = (protocol.pane_list, protocol.request, actions._state,
                       actions._sock, sys.stderr)
        protocol.pane_list = lambda sock, timeout=5.0: list(self.panes)
        protocol.request = self._request
        actions._state = lambda: None        # no state.json in the test env
        actions._sock = lambda: SOCK
        self.err = sys.stderr = io.StringIO()

    def tearDown(self):
        (protocol.pane_list, protocol.request, actions._state,
         actions._sock, sys.stderr) = self._saved

    def _request(self, sock, method, params=None, **kw):
        self.calls.append((method, params))
        if method in self.fail_on:
            raise self.fail_on[method]
        if method == "plugin.pane.focus":
            if self.raise_on_focus is not None:
                raise self.raise_on_focus
            return self.reply
        if method == "pane.process_info":
            return self.info
        if method == "pane.focus":
            return self.generic_reply
        return {}

    def methods(self):
        return [method for method, _ in self.calls]

    # -- the pane is running ---------------------------------------------

    def test_confirmed_focus_opens_nothing_and_says_nothing(self):
        self.assertEqual(actions.action_open(), 0)
        self.assertEqual(self.methods(), ["plugin.pane.focus"])
        self.assertEqual(self.calls[0][1], {"pane_id": OFFICE_PANE})
        self.assertEqual(self.err.getvalue(), "")

    def test_unfocused_reply_falls_through_to_open(self):
        # Issue #20: the reply came back clean and the pane was not focused.
        self.reply = _focus_reply(False)
        self.assertEqual(actions.action_open(), 0)
        self.assertEqual(self.methods(),
                         ["plugin.pane.focus", "plugin.pane.open"])
        self.assertIn(OFFICE_PANE, self.err.getvalue())

    def test_unknown_reply_exits_zero_but_is_reported(self):
        # No second pane on a reply shape we do not recognise, but the plugin
        # log has to show that the focus went unverified.
        self.reply = {"type": "plugin_pane_focused"}
        self.assertEqual(actions.action_open(), 0)
        self.assertEqual(self.methods(), ["plugin.pane.focus"])
        self.assertIn("did not report", self.err.getvalue())

    def test_focus_error_is_reported_and_opens_nothing(self):
        # An error that is not about ownership says nothing about whether the
        # office is there, so opening would be a guess (issue #41).
        self.raise_on_focus = protocol.ProtocolError("internal", "boom")
        self.assertEqual(actions.action_open(), 1)
        self.assertEqual(self.methods(), ["plugin.pane.focus"])
        self.assertIn("internal", self.err.getvalue())

    def test_transport_failure_opens_nothing(self):
        self.raise_on_focus = OSError("herdr went away")
        self.assertEqual(actions.action_open(), 1)
        self.assertEqual(self.methods(), ["plugin.pane.focus"])

    # -- ownership was lost, the office is still running (issue #41) ------

    def _ownership_lost(self):
        self.raise_on_focus = protocol.ProtocolError(
            actions.PLUGIN_PANE_NOT_FOUND, "plugin pane not found")

    def test_unowned_but_live_office_is_focused_generically(self):
        # The regression this issue is about: a live handoff (which `herdr
        # update` performs) leaves the office running in a pane herdr no longer
        # calls ours. This used to open a second office beside the first.
        self._ownership_lost()
        self.assertEqual(actions.action_open(), 0)
        self.assertEqual(self.methods(),
                         ["plugin.pane.focus", "pane.process_info",
                          "pane.focus"])
        self.assertEqual(self.calls[-1][1], {"pane_id": OFFICE_PANE})
        self.assertNotIn("plugin.pane.open", self.methods())

    def test_a_pane_that_is_really_gone_still_opens_one(self):
        # The other thing `plugin_pane_not_found` means. The generic API is
        # what tells the two apart, and it is decisive here.
        self._ownership_lost()
        self.fail_on["pane.process_info"] = protocol.ProtocolError(
            actions.PANE_NOT_FOUND, "pane not found")
        self.assertEqual(actions.action_open(), 0)
        self.assertEqual(self.methods(),
                         ["plugin.pane.focus", "pane.process_info",
                          "plugin.pane.open"])

    def test_restored_frame_without_an_office_opens_one(self):
        # A pane wearing our label with only the shell a restart put back
        # (issue #39). Focusing it would move the user to a dead prompt, so
        # this must still open - the pane existing is not the question.
        self._ownership_lost()
        self.info = _process_info(procs=[_shell_proc()])
        self.assertEqual(actions.action_open(), 0)
        self.assertEqual(self.methods(),
                         ["plugin.pane.focus", "pane.process_info",
                          "plugin.pane.open"])
        self.assertIn("no office is running", self.err.getvalue())

    def test_unreadable_process_info_opens_nothing(self):
        self._ownership_lost()
        self.fail_on["pane.process_info"] = OSError("herdr went away")
        self.assertEqual(actions.action_open(), 1)
        self.assertNotIn("plugin.pane.open", self.methods())

    def test_failed_generic_focus_opens_nothing(self):
        self._ownership_lost()
        self.fail_on["pane.focus"] = protocol.ProtocolError("internal", "boom")
        self.assertEqual(actions.action_open(), 1)
        self.assertNotIn("plugin.pane.open", self.methods())

    def test_generic_focus_reporting_unfocused_falls_through_to_open(self):
        # Issue #20's rule, applied on the fallback path too: herdr positively
        # said the pane did not take the focus, and opening is the only
        # recovery the action has.
        self._ownership_lost()
        self.generic_reply = {"type": "pane_info",
                              "pane": {"pane_id": OFFICE_PANE,
                                       "focused": False}}
        self.assertEqual(actions.action_open(), 0)
        self.assertEqual(self.methods()[-1], "plugin.pane.open")

    # -- no pane is running ----------------------------------------------

    def test_no_running_pane_opens_one(self):
        self.panes = [{"pane_id": "w1Z:p1"}]
        self.assertEqual(actions.action_open(), 0)
        self.assertEqual(self.methods(), ["plugin.pane.open"])
        params = self.calls[0][1]
        self.assertEqual(params["entrypoint"], actions.office_entrypoint())
        self.assertIs(params["focus"], True)

    def test_open_failure_exits_nonzero(self):
        self.panes = []

        def boom(sock, method, params=None, **kw):
            self.calls.append((method, params))
            raise protocol.ProtocolError("platform_unsupported", "nope")
        protocol.request = boom
        self.assertEqual(actions.action_open(), 1)
        self.assertIn("open failed", self.err.getvalue())

    def test_pane_list_failure_still_opens(self):
        def boom(sock, timeout=5.0):
            raise OSError("herdr is down")
        protocol.pane_list = boom
        self.assertEqual(actions.action_open(), 0)
        self.assertEqual(self.methods(), ["plugin.pane.open"])


def _process_info(pane_id=OFFICE_PANE, shell_pid=100, procs=None):
    """The shape herdr 0.7.5 answers `pane.process_info` with."""
    return {"type": "pane_process_info",
            "process_info": {"pane_id": pane_id,
                             "shell_pid": shell_pid,
                             "foreground_process_group_id": shell_pid,
                             "foreground_processes":
                                 [dict(p) for p in (procs or [])]}}


def _shell_proc(pid=100):
    return {"pid": pid, "name": "zsh", "argv": ["/usr/bin/zsh"],
            "cmdline": "/usr/bin/zsh"}


def _office_proc(pid=100):
    return {"pid": pid, "name": "python3",
            "argv": ["python3", "-m", "office", "run"],
            "cmdline": "python3 -m office run"}


class OfficeFramesTest(unittest.TestCase):
    """Which panes the startup hook is willing to treat as its own."""

    def test_label_alone_identifies_a_frame(self):
        panes = [{"pane_id": "w1:p1"}, _office_pane("w1:p2")]
        self.assertEqual(actions.office_frames(panes, None), ["w1:p2"])

    def test_recorded_pane_without_the_label_is_not_a_frame(self):
        # The hook closes what it picks. A restart may have handed the recorded
        # id to an unrelated pane, so the record alone must never be enough.
        panes = [{"pane_id": OFFICE_PANE}]
        data = {"version": 1, "running": True, "updated_at": time.time(),
                "office_pane_id": OFFICE_PANE}
        self.assertEqual(actions.office_frames(panes, data), [])

    def test_recorded_pane_is_consulted_first(self):
        panes = [_office_pane("w1:p2"), _office_pane("w1:p9")]
        data = {"version": 1, "running": True, "updated_at": time.time(),
                "office_pane_id": "w1:p9"}
        self.assertEqual(actions.office_frames(panes, data),
                         ["w1:p9", "w1:p2"])


class RunsOfficeTest(unittest.TestCase):
    def test_office_argv_is_recognised(self):
        self.assertIs(actions.runs_office(
            _process_info(procs=[_office_proc()])), True)

    def test_windows_launcher_argv_is_recognised(self):
        proc = {"pid": 1, "argv": ["py", "-3", "-m", "office", "run"]}
        self.assertIs(actions.runs_office(_process_info(procs=[proc])), True)

    def test_cmdline_alone_is_enough(self):
        # Reading a live office as dead would end with this hook closing it.
        proc = {"pid": 1, "cmdline": "python3 -m office run"}
        self.assertIs(actions.runs_office(_process_info(procs=[proc])), True)

    def test_a_shell_is_not_an_office(self):
        self.assertIs(actions.runs_office(
            _process_info(procs=[_shell_proc()])), False)

    def test_unreadable_replies_are_not_an_office(self):
        for info in (None, {}, {"process_info": None},
                     {"process_info": {"foreground_processes": "nope"}}):
            self.assertIs(actions.runs_office(info), False)


class ReclaimableFrameTest(unittest.TestCase):
    def test_idle_restored_shell_is_reclaimable(self):
        self.assertIs(actions.reclaimable_frame(
            _process_info(procs=[_shell_proc(100)])), True)

    def test_busy_shell_is_not_reclaimable(self):
        # Something the user started: shell_pid stays, the foreground does not.
        busy = {"pid": 555, "name": "sleep", "argv": ["sleep", "30"]}
        self.assertIs(actions.reclaimable_frame(
            _process_info(shell_pid=100, procs=[busy])), False)

    def test_running_office_is_not_reclaimable(self):
        self.assertIs(actions.reclaimable_frame(
            _process_info(procs=[_office_proc(100)])), False)

    def test_unreadable_reply_is_not_reclaimable(self):
        # Closing a pane is not undoable, so anything unreadable means no.
        for info in (None, {}, _process_info(procs=[]),
                     _process_info(shell_pid=None, procs=[_shell_proc()])):
            self.assertIs(actions.reclaimable_frame(info), False)


class ActionStartupTest(unittest.TestCase):
    """The [[startup]] hook (issue #39), protocol and state file faked out."""

    def setUp(self):
        self.calls = []
        self.panes = [_office_pane()]
        self.info = _process_info(procs=[_shell_proc()])
        self.fail_on = {}                     # method -> exception to raise

        self._saved = (protocol.pane_list, protocol.request, actions._state,
                       actions._sock, sys.stderr)
        protocol.pane_list = lambda sock, timeout=5.0: list(self.panes)
        protocol.request = self._request
        actions._state = lambda: None
        actions._sock = lambda: SOCK
        self.err = sys.stderr = io.StringIO()

    def tearDown(self):
        (protocol.pane_list, protocol.request, actions._state,
         actions._sock, sys.stderr) = self._saved

    def _request(self, sock, method, params=None, **kw):
        self.calls.append((method, params))
        if method in self.fail_on:
            raise self.fail_on[method]
        if method == "pane.process_info":
            return self.info
        return {}

    def methods(self):
        return [method for method, _ in self.calls]

    # -- nothing to do ---------------------------------------------------

    def test_no_frame_means_the_user_had_it_closed(self):
        self.panes = [{"pane_id": "w1:p1"}]
        self.assertEqual(actions.action_startup(), 0)
        self.assertEqual(self.methods(), [])
        self.assertEqual(self.err.getvalue(), "")

    def test_live_office_is_left_alone(self):
        # The live-handoff path: the hook fires, the process survived.
        self.info = _process_info(procs=[_office_proc()])
        self.assertEqual(actions.action_startup(), 0)
        self.assertEqual(self.methods(), ["pane.process_info"])

    def test_busy_frame_is_not_closed(self):
        self.info = _process_info(shell_pid=100,
                                  procs=[{"pid": 555, "argv": ["vim"]}])
        self.assertEqual(actions.action_startup(), 0)
        self.assertEqual(self.methods(), ["pane.process_info"])
        self.assertIn("leaving them alone", self.err.getvalue())

    def test_unreadable_process_info_opens_nothing(self):
        self.fail_on["pane.process_info"] = protocol.ProtocolError("nope", "x")
        self.assertEqual(actions.action_startup(), 0)
        self.assertEqual(self.methods(), ["pane.process_info"])
        self.assertIn("process_info", self.err.getvalue())

    def test_pane_list_failure_is_reported_but_not_fatal(self):
        def boom(sock, timeout=5.0):
            raise OSError("herdr is down")
        protocol.pane_list = boom
        self.assertEqual(actions.action_startup(), 0)
        self.assertEqual(self.methods(), [])
        self.assertIn("pane.list failed", self.err.getvalue())

    # -- the restart case ------------------------------------------------

    def test_orphaned_frame_is_replaced_without_taking_focus(self):
        self.assertEqual(actions.action_startup(), 0)
        self.assertEqual(self.methods(),
                         ["pane.process_info", "pane.close", "plugin.pane.open"])
        self.assertEqual(self.calls[1][1], {"pane_id": OFFICE_PANE})
        params = self.calls[2][1]
        self.assertEqual(params["entrypoint"], actions.office_entrypoint())
        # Issue #21 by another road: the hook runs with the user's own pane
        # focused, and the office has to come back behind it.
        self.assertIs(params["focus"], False)

    def test_every_orphan_goes_and_only_one_office_comes_back(self):
        self.panes = [_office_pane("w1:p2"), _office_pane("w1:p3")]
        self.assertEqual(actions.action_startup(), 0)
        self.assertEqual(self.methods(),
                         ["pane.process_info", "pane.process_info",
                          "pane.close", "pane.close", "plugin.pane.open"])

    def test_a_live_office_beside_an_orphan_still_wins(self):
        self.panes = [_office_pane("w1:p2"), _office_pane("w1:p3")]

        def info(sock, method, params=None, **kw):
            self.calls.append((method, params))
            if method == "pane.process_info":
                if params["pane_id"] == "w1:p3":
                    return _process_info(procs=[_office_proc()])
                return _process_info(procs=[_shell_proc()])
            return {}
        protocol.request = info
        self.assertEqual(actions.action_startup(), 0)
        self.assertNotIn("pane.close", self.methods())
        self.assertNotIn("plugin.pane.open", self.methods())

    def test_failed_close_does_not_open_a_duplicate(self):
        self.fail_on["pane.close"] = protocol.ProtocolError("busy", "no")
        self.assertEqual(actions.action_startup(), 0)
        self.assertEqual(self.methods(), ["pane.process_info", "pane.close"])
        self.assertIn("close of", self.err.getvalue())

    def test_failed_open_exits_nonzero(self):
        self.fail_on["plugin.pane.open"] = protocol.ProtocolError("x", "no")
        self.assertEqual(actions.action_startup(), 1)
        self.assertIn("open failed", self.err.getvalue())


if __name__ == "__main__":
    unittest.main()
