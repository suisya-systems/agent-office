"""Unit tests for the single-shot actions (design.md section 6, issue #20).

`action_open` owns socket calls, so the protocol module is swapped out the way
test_reconciler does it and every request is recorded instead of sent. The
question these tests exist to answer is what the action does with the *reply*
to `plugin.pane.focus`: accepting it blindly is what let a focus that did
nothing exit 0 (issue #20).
"""

import io
import sys
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
        if method == "plugin.pane.focus":
            if self.raise_on_focus is not None:
                raise self.raise_on_focus
            return self.reply
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

    def test_focus_error_is_reported_and_falls_through_to_open(self):
        self.raise_on_focus = protocol.ProtocolError("plugin_pane_not_found",
                                                     "plugin pane not found")
        self.assertEqual(actions.action_open(), 0)
        self.assertEqual(self.methods(),
                         ["plugin.pane.focus", "plugin.pane.open"])
        self.assertIn("plugin_pane_not_found", self.err.getvalue())

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


if __name__ == "__main__":
    unittest.main()
