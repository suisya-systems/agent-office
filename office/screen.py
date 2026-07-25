"""Screen - the office pane's own terminal (design.md section 5).

Everything that talks to the real terminal lives here: the alternate screen,
the cursor, the window size, and the SIGWINCH plumbing behind it. The event
loop then deals only in "how big am I" and "here is a frame", and never in
escape codes - which also means the loop can be exercised in tests without a
tty, because constructing a Screen touches nothing until open() is called.

Frame *content* is the Renderer's job; this only carries bytes to stdout.
"""

import re
import shutil
import signal
import sys

from . import textwidth

ENTER = "\x1b[?1049h\x1b[?25l\x1b[2J"     # alt screen, hide cursor, clear
LEAVE = "\x1b[?25h\x1b[?1049l"            # show cursor, back to main screen
FALLBACK_SIZE = (100, 30)

# Every escape the office emits is a CSI sequence: cursor moves and erases
# from the renderer, colour from sprites, the alt-screen pair above.
CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
FILLER = "?"                              # what codecs.replace would have used


def _encodes(text, encoding):
    """True when the console can take `text` as it stands."""
    try:
        text.encode(encoding)
    except UnicodeEncodeError:
        return False
    return True


class Screen:
    def __init__(self, stream=None):
        self.stream = stream if stream is not None else sys.stdout
        # Starts dirty so the first pass through the loop always paints.
        self._resized = True

    # -- lifecycle ------------------------------------------------------

    def open(self):
        # Ask the stream to *raise* on a character it cannot encode, so the
        # substituting is ours to do. The codec's own `replace` spends one `?`
        # on a character that drew in two columns and slides the rest of the
        # line left (#28); `_write` catches the error and replaces by width
        # instead. Best effort either way: not every stream is a TextIOWrapper
        # (a StringIO under test is not), and a stream that arrives already
        # substituting is beyond our reach - as is the encoding itself, which
        # is what the terminal on the other end reads and not ours to change.
        reconfigure = getattr(self.stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="strict")
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
        """The frame with unencodable characters replaced, same display width.

        Issue #28. `str.encode(..., "replace")` preserves *characters*, not
        columns: it spends one `?` on a full-width character that drew in two,
        so every cell to the right of it slid one column left and the borders
        came apart - the frame survived, but only as wreckage. Each cluster
        that the console refuses is charged its own width here instead, so what
        lands on a cp932 terminal is as wide as what a UTF-8 one gets.

        The frame arrives with colour already in it, and an escape sequence is
        bytes the terminal never draws, so it is lifted out and carried across
        untouched rather than measured - `textwidth` is documented as taking
        plain text, never text with ANSI in it. The sequences are ASCII, so no
        console refuses them and none of this costs them anything.
        """
        encoding = getattr(self.stream, "encoding", None) or "ascii"
        out = []
        end = 0
        for escape in CSI.finditer(text):
            out.append(self._replace(text[end:escape.start()], encoding))
            out.append(escape.group())
            end = escape.end()
        out.append(self._replace(text[end:], encoding))
        return "".join(out)

    @classmethod
    def _replace(cls, text, encoding):
        """`text` with every cluster the codec refuses spending its own width.

        Zero-width clusters - a combining mark the console has no room for -
        are charged nothing and so vanish outright, which is the same
        arithmetic: they took no column to begin with.
        """
        if _encodes(text, encoding):          # the common run, unchanged
            return text
        return "".join(cls._unit(chunk, cols, encoding)
                       for chunk, cols in textwidth.units(text))

    @staticmethod
    def _unit(chunk, cols, encoding):
        """One cluster, rendered in whatever the console can take of it."""
        if _encodes(chunk, encoding):
            return chunk
        base = chunk[:1]
        # A mark the console refuses was invisible anyway, so keep the letter
        # under it and drop the rest - `cafe` reads better than `caf?`. Only
        # where the letter alone still draws as wide as the cluster did: a
        # variation selector widens its base, and dropping it would narrow the
        # line by the column the base no longer fills.
        if _encodes(base, encoding) and textwidth.char_width(base) == cols:
            return base
        return FILLER * cols

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
