"""Tier 2 - the kitty graphics overlay (design.md section 5).

The office pane draws its own frame as text on stdout; this adds one image on
top of it through `pane.graphics.set`, covering exactly the sprite rectangles
the Renderer reports and nothing else. Nameplates, status words, the island
headers and the borders stay text cells, which is what section 5 asks for and
also what keeps the office usable at any width - the layout is still computed
in cells, so nothing here has to know how big a cell is in pixels.

**One image, not one per desk.** `pane.graphics.set` carries no image id and
`pane.graphics.clear` takes only a pane, so a pane has a single graphics layer
and a second `set` replaces the first rather than adding to it. The overlay is
therefore composed as one image spanning the bounding box of every visible
sprite, transparent in the gaps so the text between desks shows through.

**Static, and deliberately so.** herdr 0.7.4 has no `pane.graphics.stream`
(design.md risk 6), so animating this would mean re-encoding and re-sending a
PNG every animation tick. The overlay is built at animation phase 0 and only
re-sent when the fleet actually changes. The animated tier 1 art is still drawn
underneath it, which costs nothing and means an outer terminal that silently
ignores kitty graphics leaves the user with a working animated office rather
than a blank one.

Geometry: tier 1 packs two sprite pixels into one text cell vertically, so a
cell is `scale` image pixels wide and `2 * scale` tall. That is the only place
pixels and cells meet.
"""

import threading

from . import png, protocol

# Image pixels per sprite pixel. 4 gives a visibly crisper character than the
# half-block cell it covers without making the payload interesting in size.
SCALE = 4

# Upper bound on the composed image, so a very large terminal cannot turn one
# redraw into a multi-megabyte encode. The scale drops instead.
MAX_IMAGE_PX = 1_500_000


class Overlay:
    """A composed image plus where it goes, ready for pane.graphics.set."""

    __slots__ = ("data", "width", "height", "placement")

    def __init__(self, data, width, height, placement):
        self.data = data
        self.width = width
        self.height = height
        self.placement = placement


def _fit_scale(grid_cols, grid_rows, scale=SCALE):
    """Largest scale down to 1 whose image fits MAX_IMAGE_PX, or 0 if none does.

    Returning 0 rather than clamping at 1 keeps the cap honest: a pane big
    enough that even one image pixel per sprite pixel busts the budget gets no
    overlay at all instead of a multi-megabyte encode on every redraw. It takes
    something like 1000x750 cells to get there, so in practice this only ever
    hands back SCALE - but "in practice" is not a bound.
    """
    while scale >= 1:
        if grid_cols * scale * grid_rows * 2 * scale <= MAX_IMAGE_PX:
            return scale
        scale -= 1
    return 0


def build_overlay(boxes, art, scale=SCALE):
    """Compose the overlay for `boxes`, or None when there is nothing to draw.

    `boxes` are (row, col, visual, agent, focused) in absolute frame cells, as
    recorded by the Renderer. Pixels are asked for here rather than carried in
    the boxes so that a frame whose layout has not changed costs nothing: the
    caller compares the (cheap, scalar) boxes and only builds when they differ.
    """
    boxes = list(boxes)
    if not boxes:
        return None
    rendered = []
    for row, col, visual, agent, focused in boxes:
        pixels = art.pixels(visual, 0, agent, focused)
        rendered.append((row, col, pixels))

    min_col = min(col for _, col, _ in rendered)
    min_row = min(row for row, _, _ in rendered)
    max_col = max(col + len(pixels[0]) for _, col, pixels in rendered)
    max_row = max(row + len(pixels) // 2 for row, _, pixels in rendered)
    grid_cols = max_col - min_col
    grid_rows = max_row - min_row
    if grid_cols <= 0 or grid_rows <= 0:
        return None

    scale = _fit_scale(grid_cols, grid_rows, scale)
    if not scale:
        return None                 # too big to be worth drawing; tier 1 shows
    canvas = png.Canvas(grid_cols * scale, grid_rows * 2 * scale)
    for row, col, pixels in rendered:
        canvas.blit((col - min_col) * scale, (row - min_row) * 2 * scale,
                    pixels, scale)
    return Overlay(canvas.to_png(), canvas.width, canvas.height,
                   {"viewport_col": min_col, "viewport_row": min_row,
                    "grid_cols": grid_cols, "grid_rows": grid_rows})


def probe(sock_path, pane_id, timeout=2.0):
    """Ask herdr whether this pane can take graphics. Returns (ok, reason).

    Never raises: a failed probe is an answer ("no"), not an error the office
    should die of. The caller turns any "no" into a tier 1 fallback plus a
    visible warning (design.md section 5: an explicit `renderer = "kitty"`
    degrades loudly rather than silently).

    Two refusals were measured against herdr 0.7.4, and *any* failure counts as
    "no", not just the first one:

    * `feature_disabled` - `[experimental] kitty_graphics` is off, which is the
      out-of-the-box state. Nothing will ever render.
    * `cell_size_unavailable` - the feature is on but herdr cannot get the
      outer terminal's cell size in pixels (observed under WSL). It has no way
      to place an image, so nothing useful will render either.

    The second one is why this gate is "info succeeded" rather than the
    narrower "info did not say feature_disabled" that section 5 first sketched:
    with kitty_graphics enabled but no cell size, `pane.graphics.set` still
    answers `ok` while `info` refuses. A successful `set` is therefore not
    evidence that anything appeared on screen, and `info` is the only honest
    signal available.
    """
    if not pane_id:
        return False, "HERDR_PANE_ID not set"
    try:
        protocol.pane_graphics_info(sock_path, pane_id, timeout=timeout)
    except protocol.ProtocolError as exc:
        return False, exc.code or "error"
    except Exception as exc:                               # noqa: BLE001
        return False, "%s" % exc
    return True, "ok"


class GraphicsSender:
    """Composes and ships the overlay on its own thread (design.md section 2).

    Same split as the Notifier and the Commander: the render loop decides
    *what* should be on screen and returns immediately, this thread does the
    encoding and the socket round-trip. Both halves of that matter here - a
    full-screen PNG encode is tens of milliseconds and the `set` call is a
    round-trip, and neither belongs in a loop that is trying to hold 2 FPS.

    Only the newest request survives: a pending overlay that has not been sent
    yet is simply replaced, because an office two states out of date is of no
    use to anyone. That makes this a slot rather than the queue the other
    feeder threads use.

    Reports back through the shared office queue:
      ("graphics", (ok, message))   only when the outcome *changes*, so a
                                    persistent failure says so once instead of
                                    every tick.
    """

    def __init__(self, sock_path, out_queue, pane_id):
        self.sock_path = sock_path
        self.out = out_queue
        self.pane_id = pane_id
        self._cv = threading.Condition()
        self._pending = None            # ("set", Overlay) | ("clear", None)
        self._stopping = False
        self._thread = None
        self._last_report = None        # last (ok, message) handed to the loop

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="office-graphics")
        self._thread.start()

    def stop(self):
        with self._cv:
            self._stopping = True
            self._cv.notify()
        if self._thread:
            self._thread.join(timeout=2.0)

    # -- requests (return immediately) -----------------------------------

    def set_boxes(self, boxes, art):
        """Draw these sprite boxes. Composition happens on the thread."""
        self._submit(("set", (boxes, art)))

    def clear(self):
        """Take the overlay down (help overlay, compact view, shutdown)."""
        self._submit(("clear", None))

    def _submit(self, item):
        with self._cv:
            self._pending = item
            self._cv.notify()

    # -- worker ----------------------------------------------------------

    def _loop(self):
        while True:
            with self._cv:
                while self._pending is None and not self._stopping:
                    self._cv.wait()
                if self._stopping:
                    return
                item = self._pending
                self._pending = None
            self._run(item)

    def _run(self, item):
        kind, payload = item
        try:
            if kind == "clear":
                protocol.pane_graphics_clear(self.sock_path, self.pane_id)
            else:
                boxes, art = payload
                overlay = build_overlay(boxes, art)
                if overlay is None:
                    protocol.pane_graphics_clear(self.sock_path, self.pane_id)
                else:
                    protocol.pane_graphics_set(
                        self.sock_path, self.pane_id, overlay.data,
                        overlay.width, overlay.height, overlay.placement)
        except protocol.ProtocolError as exc:
            self._report(False, exc.code or "error")
            return
        except Exception as exc:                           # noqa: BLE001
            self._report(False, "%s" % exc)
            return
        self._report(True, "")

    def _report(self, ok, message):
        current = (ok, message)
        if current == self._last_report:
            return                      # unchanged: do not retell every tick
        self._last_report = current
        self.out.put(("graphics", current))


def clear_now(sock_path, pane_id, timeout=2.0):
    """Best-effort synchronous clear, for shutdown after the thread is gone."""
    if not pane_id:
        return
    try:
        protocol.pane_graphics_clear(sock_path, pane_id, timeout=timeout)
    except Exception:                                      # noqa: BLE001
        pass                            # the pane is going away regardless
