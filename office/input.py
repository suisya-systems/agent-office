"""Input - raw-mode stdin key reader (design.md section 6).

Runs a thread that reads the office pane's own stdin, decodes keys, and pushes
("key", name) tuples onto the shared event queue. Linux/macOS only (termios);
Windows is out of scope for Stage 2 core.
"""

import os
import select
import sys
import threading

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


class InputReader:
    def __init__(self, out_queue, fd=None):
        self.out = out_queue
        self.fd = fd if fd is not None else sys.stdin.fileno()
        self._stop = threading.Event()
        self._thread = None
        self._saved = None

    def start(self):
        if _HAVE_TERMIOS and os.isatty(self.fd):
            self._saved = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="office-input")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        if self._saved is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self._saved)
            self._saved = None

    def _loop(self):
        while not self._stop.is_set():
            r, _, _ = select.select([self.fd], [], [], 0.2)
            if not r:
                continue
            try:
                data = os.read(self.fd, 64)
            except OSError:
                break
            if not data:
                break
            self._decode(data.decode("utf-8", "replace"))

    def _decode(self, s):
        i = 0
        while i < len(s):
            ch = s[i]
            if ch == "\x1b" and s[i + 1:i + 2] == "[":
                tail = s[i + 2:i + 3]
                if tail in _CSI:
                    self.out.put(("key", _CSI[tail]))
                    i += 3
                    continue
                i += 2
                continue
            if ch in _SIMPLE:
                self.out.put(("key", _SIMPLE[ch]))
            elif ch.isprintable():
                self.out.put(("key", ch))
            i += 1
