"""Tests for the terminal boundary (Screen) and the CLI dispatch table.

Both were extracted from larger modules; these cover the behaviour that moved
with them, none of which needs a real tty.
"""

import contextlib
import io
import unittest

from office import cli, screen


class ScreenTest(unittest.TestCase):
    def setUp(self):
        self.out = io.StringIO()
        self.screen = screen.Screen(self.out)

    def test_constructing_writes_nothing(self):
        # the office loop is testable without a tty precisely because of this
        self.assertEqual(self.out.getvalue(), "")

    def test_open_and_close_bracket_the_alternate_screen(self):
        self.screen.open()
        self.assertEqual(self.out.getvalue(), screen.ENTER)
        self.screen.close()
        self.assertTrue(self.out.getvalue().endswith(screen.LEAVE))

    def test_write_passes_the_frame_through_verbatim(self):
        self.screen.write("\x1b[Hframe")
        self.assertEqual(self.out.getvalue(), "\x1b[Hframe")

    def test_starts_dirty_so_the_first_pass_paints(self):
        self.assertTrue(self.screen.resized)

    def test_resize_flag_cycles(self):
        self.screen.clear_resized()
        self.assertFalse(self.screen.resized)
        self.screen.on_resize()               # as SIGWINCH would call it
        self.assertTrue(self.screen.resized)

    def test_size_returns_two_positive_ints(self):
        cols, rows = self.screen.size()
        self.assertGreater(cols, 0)
        self.assertGreater(rows, 0)

    def test_install_resize_handler_is_safe_off_the_main_thread(self):
        import threading
        errors = []

        def install():
            try:
                screen.Screen(io.StringIO()).install_resize_handler()
            except Exception as exc:          # noqa: BLE001
                errors.append(exc)

        thread = threading.Thread(target=install)
        thread.start()
        thread.join()
        self.assertEqual(errors, [])


class DispatchTest(unittest.TestCase):
    def test_every_documented_subcommand_is_dispatchable(self):
        for name in ("run", "action-open", "action-jump-blocked",
                     "config-check"):
            self.assertIn(name, cli.COMMANDS)
            self.assertIn(name, cli.USAGE)

    def _run(self, argv):
        """Dispatch with the usage text captured, not spilled into the run."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue() + err.getvalue()

    def test_help_exits_zero(self):
        code, text = self._run(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("usage:", text)

    def test_unknown_subcommand_exits_two(self):
        code, text = self._run(["nope"])
        self.assertEqual(code, 2)
        self.assertIn("unknown subcommand: nope", text)


if __name__ == "__main__":
    unittest.main()
