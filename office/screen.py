"""Screen - the office pane's own terminal (design.md section 5).

Everything that talks to the real terminal lives here: the alternate screen,
the cursor, the window size, and the SIGWINCH plumbing behind it. The event
loop then deals only in "how big am I" and "here is a frame", and never in
escape codes - which also means the loop can be exercised in tests without a
tty, because constructing a Screen touches nothing until open() is called.

Frame *content* is the Renderer's job; this only carries bytes to stdout.
"""

import shutil
import signal
import sys

ENTER = "\x1b[?1049h\x1b[?25l\x1b[2J"     # alt screen, hide cursor, clear
LEAVE = "\x1b[?25h\x1b[?1049l"            # show cursor, back to main screen
FALLBACK_SIZE = (100, 30)


class Screen:
    def __init__(self, stream=None):
        self.stream = stream if stream is not None else sys.stdout
        # Starts dirty so the first pass through the loop always paints.
        self._resized = True

    # -- lifecycle ------------------------------------------------------

    def open(self):
        # Best effort: ask the stream to substitute characters it cannot
        # encode rather than raise. Not every stream is a TextIOWrapper (a
        # StringIO under test is not), and the encoding itself is left alone -
        # what the terminal on the other end reads is not ours to change.
        reconfigure = getattr(self.stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="replace")
            except (ValueError, OSError):
                pass
        self._write(ENTER)

    def close(self):
        self._write(LEAVE)

    def write(self, frame):
        self._write(frame)

    def _write(self, text):
        try:
            self.stream.write(text)
        except UnicodeEncodeError:
            # A cp932 console cannot take a half-block, and tier 0 is no
            # guarantee either: pane titles and agent names come from herdr
            # and can hold anything. Losing a glyph beats losing the frame -
            # and beats the traceback landing on the alternate screen.
            self.stream.write(self._encodable(text))
        self.stream.flush()

    def _encodable(self, text):
        """The frame with unencodable characters replaced, same length."""
        encoding = getattr(self.stream, "encoding", None) or "ascii"
        return text.encode(encoding, "replace").decode(encoding, "replace")

    # -- geometry -------------------------------------------------------

    def size(self):
        columns, lines = shutil.get_terminal_size(FALLBACK_SIZE)
        return columns, lines

    @property
    def resized(self):
        """True while a resize is still waiting to be drawn."""
        return self._resized

    def clear_resized(self):
        self._resized = False

    def on_resize(self, *_):
        """SIGWINCH handler: flag only, never draw from a signal context."""
        self._resized = True

    def install_resize_handler(self):
        """Watch SIGWINCH where the platform and thread allow it."""
        sig = getattr(signal, "SIGWINCH", None)      # absent on Windows
        if sig is None:
            return
        try:
            signal.signal(sig, self.on_resize)
        except (OSError, ValueError):                # not the main thread
            pass
