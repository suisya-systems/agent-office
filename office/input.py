"""Input - raw-mode stdin key reader (design.md section 6).

Runs a thread that reads the office pane's own keystrokes, decodes them, and
pushes ("key", name) tuples onto the shared event queue.

**Why there are backends.** Reading a keypress is the one part of the office
that has no portable form. On linux/macOS it is cbreak + select + read on the
pane's stdin. On Windows `select` works only on sockets - pointing it at a
console handle raises WinError 10093 - so the same loop would kill the input
thread on its first pass and spill a traceback across the alternate screen.
Windows instead polls msvcrt, which reads CONIN$ directly: it needs no raw
mode (`_getwch` bypasses the cooked line discipline, so there is no terminal
state to save and nothing to corrupt if the process is killed) and it keeps
working when herdr hands the pane a pipe for stdin.

Both backends emit the same key names, so office.py never learns which one it
is talking to, and either can be swapped for a fake in tests.
"""

import os
import select
import sys
import threading
import time

try:
    import termios
    import tty
    _HAVE_TERMIOS = True
except ImportError:                               # pragma: no cover (non-unix)
    _HAVE_TERMIOS = False

# escape-sequence tail -> key name
_CSI = {
    "A": "up", "B": "down", "C": "right", "D": "left",
    "H": "home", "F": "end",
}
_SIMPLE = {
    "\r": "enter", "\n": "enter", "\t": "tab",
    "\x1b": "escape", "\x03": "quit", "\x04": "quit",
}

# Windows sends the arrow/navigation block as a two-character sequence: a
# prefix, then a scan code. Measured against a real console input buffer.
_WIN_PREFIX = ("\x00", "\xe0")
_WIN_EXTENDED = {
    "H": "up", "P": "down", "M": "right", "K": "left",
    "G": "home", "O": "end",
}
# msvcrt reports "no console at all" by handing back U+FFFF rather than
# raising, so this has to be recognised or the loop spins at full speed.
_WIN_EOF = "\uffff"

POLL_S = 0.01
TIMEOUT_S = 0.2


def _decode_stream(text, out):
    """Decode a terminal byte run (ESC sequences and all) into key names."""
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\x1b" and text[i + 1:i + 2] == "[":
            tail = text[i + 2:i + 3]
            if tail in _CSI:
                out.append(_CSI[tail])
                i += 3
                continue
            i += 2
            continue
        if ch in _SIMPLE:
            out.append(_SIMPLE[ch])
        elif ch.isprintable():
            out.append(ch)
        i += 1


class PosixBackend:
    """cbreak + select + read on a tty fd."""

    def __init__(self, fd=None):
        self._fd = fd
        self._saved = None

    @property
    def fd(self):
        # Resolved late: constructing an InputReader must not need a stdin.
        if self._fd is None:
            self._fd = sys.stdin.fileno()
        return self._fd

    def open(self):
        if _HAVE_TERMIOS and os.isatty(self.fd):
            self._saved = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)

    def close(self):
        if self._saved is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self._saved)
            self._saved = None

    def keys(self, timeout):
        """Key names ready within `timeout`; [] on timeout, None at EOF."""
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return []
        try:
            data = os.read(self.fd, 64)
        except OSError:
            return None
        if not data:
            return None
        names = []
        _decode_stream(data.decode("utf-8", "replace"), names)
        return names


class WindowsBackend:
    """msvcrt polling of the console input buffer.

    The dependencies are injected so the whole decoder - including the console
    quirks below - is exercised from the unix CI, where msvcrt does not exist.
    """

    def __init__(self, kbhit=None, getwch=None, sleep=None, monotonic=None,
                 poll_s=POLL_S):
        if kbhit is None or getwch is None:       # pragma: no cover (Windows)
            import msvcrt
            kbhit = kbhit or msvcrt.kbhit
            getwch = getwch or msvcrt.getwch
        self._kbhit = kbhit
        self._getwch = getwch
        self._sleep = sleep or time.sleep
        self._monotonic = monotonic or time.monotonic
        self._poll_s = poll_s

    def open(self):
        """Nothing to do: _getwch reads CONIN$ without touching its mode."""

    def close(self):
        """Nothing was changed, so nothing has to be put back."""

    def keys(self, timeout):
        deadline = self._monotonic() + timeout
        names = []
        while True:
            while self._kbhit():
                ch = self._getwch()
                if ch == _WIN_EOF:
                    return None               # the console went away
                if ch in _WIN_PREFIX:
                    # The scan code MUST be taken without consulting kbhit:
                    # the UCRT keeps separate read-ahead buffers for the byte
                    # and wide reads and kbhit only sees the byte one, so it
                    # answers "nothing pending" while half a key is still
                    # buffered - and that half then surfaces under the *next*
                    # keypress.
                    tail = self._getwch()
                    if tail == _WIN_EOF:
                        return None
                    name = _WIN_EXTENDED.get(tail)
                    if name:
                        names.append(name)
                    continue                  # unmapped function key: drop it
                if ch in _SIMPLE:
                    names.append(_SIMPLE[ch])
                elif ch.isprintable():
                    names.append(ch)
            if names:
                return names
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return []
            self._sleep(min(self._poll_s, remaining))


class NullBackend:
    """No console to read - park the thread instead of spinning on nothing."""

    def __init__(self, sleep=None):
        self._sleep = sleep or time.sleep

    def open(self):
        pass

    def close(self):
        pass

    def keys(self, timeout):
        self._sleep(timeout)
        return []


def _has_console():
    """True when this process can open the Windows console for reading."""
    try:
        fd = os.open("CONIN$", os.O_RDONLY)
    except OSError:
        return False
    os.close(fd)
    return True


def default_backend(fd=None):
    if os.name != "nt":
        return PosixBackend(fd)
    # os.isatty(0) is not the question on Windows: msvcrt reads CONIN$, so
    # keys still arrive when herdr gives the pane a pipe for stdin.
    return WindowsBackend() if _has_console() else NullBackend()


class InputReader:
    def __init__(self, out_queue, fd=None, backend=None):
        self.out = out_queue
        self.backend = backend if backend is not None else default_backend(fd)
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self.backend.open()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="office-input")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        self.backend.close()

    def _loop(self):
        while not self._stop.is_set():
            try:
                names = self.backend.keys(TIMEOUT_S)
            except OSError:
                break
            if names is None:
                break
            for name in names:
                if self._stop.is_set():
                    return
                self.out.put(("key", name))
