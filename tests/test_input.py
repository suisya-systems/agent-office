"""Key decoding on both platforms, driven from a unix CI.

The Windows console is not a terminal that speaks escape sequences: arrow keys
arrive as a two-character prefix/scan-code pair, and the CRT keeps *separate*
read-ahead buffers for the byte and wide reads, so `kbhit` answers "nothing
pending" while the second half of a key is still sitting there. Reading the
scan code on kbhit's word therefore leaks half a keypress into the next one -
which is what these tests exist to keep from coming back.

WindowsBackend takes its console calls as arguments so all of that is
exercisable here, where msvcrt does not exist at all.
"""

import os
import queue
import threading
import unittest

from office import input as input_mod


class FakeConsole:
    """The console input buffer: a script of characters, read one at a time.

    `kbhit_blind` reproduces the CRT quirk - after handing out a prefix byte
    kbhit reports an empty buffer even though the scan code is still queued.
    """

    def __init__(self, chars, kbhit_blind=False):
        self.chars = list(chars)
        self.kbhit_blind = kbhit_blind
        self.last_was_prefix = False

    def kbhit(self):
        if self.kbhit_blind and self.last_was_prefix:
            return False
        return bool(self.chars)

    def getwch(self):
        ch = self.chars.pop(0)
        self.last_was_prefix = ch in input_mod._WIN_PREFIX
        return ch


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _backend(chars, kbhit_blind=False, clock=None):
    console = FakeConsole(chars, kbhit_blind)
    clock = clock or FakeClock()
    return input_mod.WindowsBackend(kbhit=console.kbhit, getwch=console.getwch,
                                    sleep=clock.sleep,
                                    monotonic=clock.monotonic)


class WindowsBackendTest(unittest.TestCase):
    def test_the_arrow_block_decodes_to_the_shared_key_names(self):
        for prefix in input_mod._WIN_PREFIX:
            for scan, name in (("H", "up"), ("P", "down"), ("M", "right"),
                               ("K", "left"), ("G", "home"), ("O", "end")):
                got = _backend([prefix, scan]).keys(0.2)
                self.assertEqual(got, [name], "%s%s" % (ascii(prefix), scan))

    def test_a_scan_code_is_taken_even_when_kbhit_denies_it(self):
        """The regression this whole backend is shaped around.

        Consulting kbhit before reading the scan code left it in the buffer,
        so the next keypress came back as the *previous* key's tail: press up
        then down and the office moved up, then up again.
        """
        self.assertEqual(_backend(["\xe0", "H"], kbhit_blind=True).keys(0.2),
                         ["up"])

    def test_a_burst_of_arrows_splits_correctly(self):
        self.assertEqual(
            _backend(["\xe0", "H", "\xe0", "P"], kbhit_blind=True).keys(0.2),
            ["up", "down"])

    def test_an_unmapped_function_key_is_dropped_whole(self):
        """F1 is prefix + ';'. Letting the tail through would type a ';'."""
        self.assertEqual(_backend(["\x00", ";"]).keys(0.2), [])

    def test_the_simple_keys_match_the_unix_names(self):
        for ch, name in (("\r", "enter"), ("\t", "tab"), ("\x1b", "escape"),
                         ("\x03", "quit"), ("\x04", "quit")):
            self.assertEqual(_backend([ch]).keys(0.2), [name], ascii(ch))

    def test_printable_characters_come_through_as_themselves(self):
        self.assertEqual(_backend(["q", "?", "j"]).keys(0.2), ["q", "?", "j"])

    def test_a_vanished_console_ends_the_reader(self):
        """msvcrt returns U+FFFF instead of raising; a spin loop otherwise."""
        self.assertIsNone(_backend([input_mod._WIN_EOF]).keys(0.2))

    def test_a_vanished_console_mid_key_ends_the_reader(self):
        self.assertIsNone(_backend(["\xe0", input_mod._WIN_EOF]).keys(0.2))

    def test_an_idle_console_sleeps_rather_than_spins(self):
        clock = FakeClock()
        backend = _backend([], clock=clock)
        self.assertEqual(backend.keys(0.2), [])
        self.assertGreaterEqual(clock.now, 0.2)

    def test_opening_and_closing_leave_the_console_alone(self):
        """_getwch bypasses the line discipline, so there is no mode to save -
        and so a killed office cannot leave the console wedged."""
        backend = _backend([])
        backend.open()
        backend.close()


@unittest.skipIf(os.name == "nt", "select() on Windows takes sockets only")
class PosixBackendTest(unittest.TestCase):
    def setUp(self):
        self.read_fd, self.write_fd = os.pipe()
        self.addCleanup(self._close, "read_fd")
        self.addCleanup(self._close, "write_fd")
        self.backend = input_mod.PosixBackend(self.read_fd)

    def _close(self, attr):
        fd = getattr(self, attr, None)
        if fd is not None:
            setattr(self, attr, None)
            os.close(fd)

    def _send(self, data):
        os.write(self.write_fd, data)

    def test_an_escape_sequence_decodes_to_an_arrow(self):
        self._send(b"\x1b[A")
        self.assertEqual(self.backend.keys(1.0), ["up"])

    def test_printable_characters_come_through_as_themselves(self):
        self._send(b"qj")
        self.assertEqual(self.backend.keys(1.0), ["q", "j"])

    def test_nothing_to_read_is_not_the_end(self):
        self.assertEqual(self.backend.keys(0.01), [])

    def test_a_closed_stream_ends_the_reader(self):
        self._close("write_fd")
        self.assertIsNone(self.backend.keys(1.0))


class FakeBackend:
    """A backend that hands over one scripted batch, then reports EOF."""

    def __init__(self, batches):
        self.batches = list(batches)
        self.opened = 0
        self.closed = 0
        self.done = threading.Event()

    def open(self):
        self.opened += 1

    def close(self):
        self.closed += 1

    def keys(self, _timeout):
        if self.batches:
            return self.batches.pop(0)
        self.done.set()
        return None


class InputReaderTest(unittest.TestCase):
    def test_decoded_keys_reach_the_queue(self):
        out = queue.Queue()
        backend = FakeBackend([["up"], ["q"]])
        reader = input_mod.InputReader(out, backend=backend)
        reader.start()
        self.assertTrue(backend.done.wait(2.0))
        reader.stop()
        self.assertEqual([out.get_nowait(), out.get_nowait()],
                         [("key", "up"), ("key", "q")])

    def test_the_backend_is_opened_and_closed_once(self):
        backend = FakeBackend([])
        reader = input_mod.InputReader(queue.Queue(), backend=backend)
        reader.start()
        self.assertTrue(backend.done.wait(2.0))
        reader.stop()
        self.assertEqual((backend.opened, backend.closed), (1, 1))

    def test_a_reader_with_no_console_still_stops(self):
        reader = input_mod.InputReader(queue.Queue(),
                                       backend=input_mod.NullBackend(
                                           sleep=lambda _s: None))
        reader.start()
        reader.stop()


class BackendChoiceTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, input_mod, "_has_console",
                        input_mod._has_console)
        self.addCleanup(setattr, input_mod, "WindowsBackend",
                        input_mod.WindowsBackend)
        self.addCleanup(setattr, os, "name", os.name)
        # Constructing the real one would import msvcrt, which is the whole
        # reason this branch cannot be reached on the CI it has to run on.
        input_mod.WindowsBackend = lambda: "windows-backend"

    def test_unix_gets_the_select_backend(self):
        os.name = "posix"
        self.assertIsInstance(input_mod.default_backend(3),
                              input_mod.PosixBackend)

    def test_windows_with_a_console_gets_the_polling_backend(self):
        os.name = "nt"
        input_mod._has_console = lambda: True
        self.assertEqual(input_mod.default_backend(), "windows-backend")

    def test_windows_without_a_console_reads_nothing(self):
        """herdr can hand the pane no console at all; msvcrt then returns
        U+FFFF forever, which would be a full-speed loop on nothing."""
        os.name = "nt"
        input_mod._has_console = lambda: False
        self.assertIsInstance(input_mod.default_backend(),
                              input_mod.NullBackend)


if __name__ == "__main__":
    unittest.main()
