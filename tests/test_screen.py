"""Tests for the terminal boundary (Screen) and the CLI dispatch table.

Both were extracted from larger modules; these cover the behaviour that moved
with them, none of which needs a real tty.
"""

import contextlib
import io
import unittest

from office import cli, screen, textwidth


class Cp932Stream(io.StringIO):
    """A console that refuses what it cannot encode, exactly where a real one
    does - inside write(), before anything has reached the terminal."""

    encoding = "cp932"

    def write(self, text):
        text.encode("cp932")
        return super().write(text)


class NarrowConsoleTest(unittest.TestCase):
    """A cp932 console must cost a glyph, never the frame.

    Picking tier 0 is not enough on its own: pane titles, agent names and room
    labels all come from herdr and can hold anything at all, so an ASCII frame
    can still carry a character the console cannot encode. Without the
    fallback the traceback lands on the alternate screen and the office is
    left as wreckage.
    """

    def setUp(self):
        self.out = Cp932Stream()
        self.screen = screen.Screen(self.out)

    def test_a_half_block_frame_costs_a_glyph_not_the_frame(self):
        self.screen.write("desk ▀▀ here")
        self.assertIn("desk ", self.out.getvalue())
        self.assertIn("here", self.out.getvalue())

    def test_the_replacement_keeps_the_frame_the_same_width(self):
        # A rocket draws in two columns; spending one `?` on it would slide
        # everything to its right one column left (#28).
        frame = "abc\U0001f680def"
        self.screen.write(frame)
        self.assertEqual(textwidth.width(self.out.getvalue()),
                         textwidth.width(frame))

    def test_every_line_of_a_mixed_frame_keeps_its_width(self):
        # Half-blocks a cp932 console cannot take, a name it can, and one it
        # cannot - the three ways a real frame meets a narrow console.
        frame = "\n".join(("▀▀ desk ▀▀",
                           "テスト agent",       # cp932 has these
                           "你好 \U0001f680 hi"))    # ...and not these
        self.screen.write(frame)
        written = self.out.getvalue().split("\n")
        self.assertEqual([textwidth.width(line) for line in written],
                         [textwidth.width(line) for line in frame.split("\n")])
        self.assertIn("テスト", written[1])   # kept, not filled

    def test_colour_survives_the_replacement(self):
        # The escape is invisible and must be carried across whole: mangle it
        # and its bytes land on the terminal as text, widening the line.
        frame = "\x1b[38;2;1;2;3m▀▀\x1b[0m"
        self.screen.write(frame)
        written = self.out.getvalue()
        self.assertEqual(written, "\x1b[38;2;1;2;3m??\x1b[0m")

    def test_a_zero_width_mark_costs_no_column(self):
        # The combining acute is not in cp932 either; it drew no column, so
        # it takes none - and the letter under it is kept, not filled over.
        frame = "étage"
        self.screen.write(frame)
        self.assertEqual(self.out.getvalue(), "etage")

    def test_a_selector_that_widens_its_base_is_filled_whole(self):
        # U+26A0 U+FE0F draws in two columns, U+26A0 alone in one, so keeping
        # the bare base would hand a column back. Fill the pair instead.
        self.screen.write("⚠️")
        self.assertEqual(self.out.getvalue(), "??")

    def test_an_encodable_frame_is_untouched(self):
        self.screen.write("\x1b[Hplain ascii")
        self.assertEqual(self.out.getvalue(), "\x1b[Hplain ascii")


class RealStreamTest(unittest.TestCase):
    """The console the office actually gets: a TextIOWrapper it may reconfigure.

    A StringIO cannot show this. `open()` used to hand the substituting to the
    codec, which meant the width-preserving replacement above never ran where
    it mattered - the frame reached a cp932 terminal one column short per
    full-width character it could not encode (#28).
    """

    def _open(self):
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="cp932", newline="")
        screen.Screen(stream).open()
        raw.truncate(0), raw.seek(0)              # drop the alt-screen preamble
        return raw, stream

    def test_open_leaves_the_stream_raising_so_we_do_the_replacing(self):
        _raw, stream = self._open()
        self.assertEqual(stream.errors, "strict")

    def test_a_full_width_character_costs_two_columns_on_the_wire(self):
        raw, stream = self._open()
        screen.Screen(stream).write("[你]")       # not in cp932
        self.assertEqual(raw.getvalue(), b"[??]")


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

    def test_open_survives_a_stream_that_cannot_be_reconfigured(self):
        self.screen.open()
        self.assertEqual(self.out.getvalue(), screen.ENTER)

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
