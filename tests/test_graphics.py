"""Tier 2: the PNG encoder and the kitty graphics overlay (design.md 5).

The visual result of tier 2 cannot be asserted from here - it needs herdr's
`[experimental] kitty_graphics` *and* an outer terminal that speaks the kitty
graphics protocol. What can be pinned down is everything up to the socket, and
that is what these tests do: the bytes really are a PNG (decoded back with an
independent reader below, CRCs and all), the pixels really are the theme's
colours in the right places, the placement really covers the cells the renderer
said, and the sender really coalesces and falls back the way it claims.

`decode_png` is deliberately written against the *spec*, not against the
encoder, so that a bug in office/png.py cannot cancel itself out in the check.
"""

import queue
import struct
import unittest
import zlib

from office import graphics, png, sprites, themes
from office.renderer import Renderer
from office.state import OfficeState

SIGNATURE = b"\x89PNG\r\n\x1a\n"


def decode_png(data):
    """Minimal spec-side PNG reader for 8-bit RGBA, filter 0. Verifies CRCs.

    Returns (width, height, rows, chunk_tags) where rows is a list of rows of
    (r, g, b, a) tuples.
    """
    if data[:8] != SIGNATURE:
        raise AssertionError("bad PNG signature: %r" % data[:8])
    pos = 8
    idat = b""
    tags = []
    width = height = None
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        tag = data[pos + 4:pos + 8]
        payload = data[pos + 8:pos + 8 + length]
        (crc,) = struct.unpack(">I", data[pos + 8 + length:pos + 12 + length])
        if crc != zlib.crc32(tag + payload) & 0xFFFFFFFF:
            raise AssertionError("bad CRC on chunk %r" % tag)
        tags.append(tag)
        if tag == b"IHDR":
            (width, height, depth, ctype, comp, filt,
             interlace) = struct.unpack(">IIBBBBB", payload)
            if (depth, ctype, comp, filt, interlace) != (8, 6, 0, 0, 0):
                raise AssertionError("unexpected IHDR: %r" % (payload,))
        elif tag == b"IDAT":
            idat += payload
        pos += 12 + length
    raw = zlib.decompress(idat)
    stride = width * 4
    if len(raw) != (stride + 1) * height:
        raise AssertionError("IDAT is %d bytes, expected %d"
                             % (len(raw), (stride + 1) * height))
    rows = []
    for y in range(height):
        off = y * (stride + 1)
        if raw[off] != 0:
            raise AssertionError("row %d uses filter %d" % (y, raw[off]))
        line = raw[off + 1:off + 1 + stride]
        rows.append([tuple(line[x * 4:(x + 1) * 4]) for x in range(width)])
    return width, height, rows, tags


class PngEncoderTest(unittest.TestCase):
    def test_structure_and_chunk_order(self):
        data = png.Canvas(3, 2).to_png()
        self.assertTrue(data.startswith(SIGNATURE))
        width, height, _, tags = decode_png(data)
        self.assertEqual((width, height), (3, 2))
        self.assertEqual(tags[0], b"IHDR")
        self.assertEqual(tags[-1], b"IEND")
        self.assertIn(b"IDAT", tags)

    def test_a_fresh_canvas_is_fully_transparent(self):
        _, _, rows, _ = decode_png(png.Canvas(4, 3).to_png())
        for row in rows:
            self.assertEqual(row, [(0, 0, 0, 0)] * 4)

    def test_blit_round_trips_pixel_for_pixel(self):
        canvas = png.Canvas(4, 2)
        canvas.blit(1, 0, [[(10, 20, 30), (40, 50, 60)],
                           [(70, 80, 90), (100, 110, 120)]])
        _, _, rows, _ = decode_png(canvas.to_png())
        self.assertEqual(rows[0], [(0, 0, 0, 0), (10, 20, 30, 255),
                                   (40, 50, 60, 255), (0, 0, 0, 0)])
        self.assertEqual(rows[1], [(0, 0, 0, 0), (70, 80, 90, 255),
                                   (100, 110, 120, 255), (0, 0, 0, 0)])

    def test_scale_expands_each_pixel_into_a_square(self):
        canvas = png.Canvas(4, 4)
        canvas.blit(0, 0, [[(1, 2, 3), (4, 5, 6)]], scale=2)
        _, _, rows, _ = decode_png(canvas.to_png())
        for y in (0, 1):
            self.assertEqual(rows[y], [(1, 2, 3, 255), (1, 2, 3, 255),
                                       (4, 5, 6, 255), (4, 5, 6, 255)])
        for y in (2, 3):                       # only one source row was drawn
            self.assertEqual(rows[y], [(0, 0, 0, 0)] * 4)

    def test_out_of_bounds_blits_are_clipped_not_wrapped(self):
        # A run that overflowed the row would reappear at the start of the
        # next one, which is the classic flat-buffer bug.
        canvas = png.Canvas(3, 2)
        canvas.blit(2, 0, [[(9, 9, 9), (8, 8, 8), (7, 7, 7)]])
        canvas.blit(-2, 1, [[(1, 1, 1), (2, 2, 2), (3, 3, 3)]])
        _, _, rows, _ = decode_png(canvas.to_png())
        self.assertEqual(rows[0], [(0, 0, 0, 0), (0, 0, 0, 0), (9, 9, 9, 255)])
        self.assertEqual(rows[1], [(3, 3, 3, 255), (0, 0, 0, 0), (0, 0, 0, 0)])

    def test_blits_below_the_canvas_are_dropped(self):
        canvas = png.Canvas(2, 1)
        canvas.blit(0, 5, [[(1, 1, 1), (2, 2, 2)]])
        _, _, rows, _ = decode_png(canvas.to_png())
        self.assertEqual(rows[0], [(0, 0, 0, 0)] * 2)

    def test_rejects_nonsense(self):
        with self.assertRaises(ValueError):
            png.Canvas(0, 4)
        with self.assertRaises(ValueError):
            png.encode_rgba(2, 2, bytearray(3))          # wrong buffer length
        with self.assertRaises(ValueError):
            png.Canvas(1, 1).blit(0, 0, [[(0, 0, 0)]], scale=0)


def _state():
    s = OfficeState()
    s.ingest_pane({"pane_id": "w1:p1", "workspace_id": "w1", "tab_id": "w1:t1",
                   "agent": "claude", "agent_status": "working"})
    s.ingest_pane({"pane_id": "w1:p2", "workspace_id": "w1", "tab_id": "w1:t1",
                   "agent": "codex", "agent_status": "blocked"})
    s.set_room_label("w1", "room-one")
    return s


class OverlayTest(unittest.TestCase):
    def renderer(self, theme="default"):
        return Renderer(tier=2, truecolor=True, theme=theme)

    def test_nothing_to_draw_is_no_overlay(self):
        self.assertIsNone(graphics.build_overlay([], self.renderer().art))

    def test_placement_covers_exactly_the_reported_cells(self):
        r = self.renderer()
        r.render(_state(), 120, 40)
        overlay = graphics.build_overlay(r.sprite_boxes, r.art)
        boxes = r.sprite_boxes
        self.assertTrue(boxes)
        place = overlay.placement
        self.assertEqual(place["viewport_col"], min(b[1] for b in boxes))
        self.assertEqual(place["viewport_row"], min(b[0] for b in boxes))
        self.assertEqual(place["grid_cols"],
                         max(b[1] + sprites.DESK_W for b in boxes)
                         - place["viewport_col"])
        self.assertEqual(place["grid_rows"],
                         max(b[0] + sprites.DESK_ROWS for b in boxes)
                         - place["viewport_row"])

    def test_image_size_matches_the_cell_rectangle(self):
        # A text cell is one sprite pixel wide and two tall (tier 1 folds two
        # pixel rows into one row of half blocks), so the image has to be
        # grid_cols x scale by grid_rows x 2 x scale to line up.
        r = self.renderer()
        r.render(_state(), 120, 40)
        overlay = graphics.build_overlay(r.sprite_boxes, r.art)
        width, height, _, _ = decode_png(overlay.data)
        self.assertEqual((width, height), (overlay.width, overlay.height))
        self.assertEqual(width,
                         overlay.placement["grid_cols"] * graphics.SCALE)
        self.assertEqual(height,
                         overlay.placement["grid_rows"] * 2 * graphics.SCALE)

    def test_pixels_are_the_theme_palette(self):
        """The desk row is solid 'D', so it must be the theme's desk colour."""
        for theme_name in themes.NAMES:
            r = self.renderer(theme_name)
            r.render(_state(), 120, 40)
            overlay = graphics.build_overlay(r.sprite_boxes, r.art)
            _, _, rows, _ = decode_png(overlay.data)
            palette = themes.get(theme_name).palette
            box_row, box_col = r.sprite_boxes[0][0], r.sprite_boxes[0][1]
            origin_y = (box_row - overlay.placement["viewport_row"]) * 2 * graphics.SCALE
            origin_x = (box_col - overlay.placement["viewport_col"]) * graphics.SCALE
            # grid row 8 is the desk top: "DDDDDDDDDDDDDDDD"
            y = origin_y + 8 * graphics.SCALE
            expected = tuple(palette["desk"]) + (255,)
            for dx in range(sprites.DESK_W * graphics.SCALE):
                self.assertEqual(rows[y][origin_x + dx], expected,
                                 "%s: desk pixel %d" % (theme_name, dx))

    def test_gaps_between_desks_stay_transparent(self):
        """The text between desks has to show through (design.md section 5)."""
        r = self.renderer()
        r.render(_state(), 120, 40)
        boxes = sorted(r.sprite_boxes, key=lambda b: b[1])
        overlay = graphics.build_overlay(r.sprite_boxes, r.art)
        _, _, rows, _ = decode_png(overlay.data)
        first, second = boxes[0], boxes[1]
        gap_start = first[1] + sprites.DESK_W
        self.assertLess(gap_start, second[1])          # there really is a gap
        x = (gap_start - overlay.placement["viewport_col"]) * graphics.SCALE
        y = (first[0] - overlay.placement["viewport_row"]) * 2 * graphics.SCALE
        self.assertEqual(rows[y][x], (0, 0, 0, 0))

    def test_two_themes_produce_different_images(self):
        r1, r2 = self.renderer("default"), self.renderer("midnight")
        r1.render(_state(), 120, 40)
        r2.render(_state(), 120, 40)
        self.assertNotEqual(graphics.build_overlay(r1.sprite_boxes, r1.art).data,
                            graphics.build_overlay(r2.sprite_boxes, r2.art).data)

    def test_agents_produce_different_images(self):
        r = self.renderer()
        one = graphics.build_overlay(
            [(1, 1, "working", "claude", False)], r.art).data
        two = graphics.build_overlay(
            [(1, 1, "working", "gemini", False)], r.art).data
        self.assertNotEqual(one, two)

    def test_scale_drops_rather_than_encoding_a_huge_image(self):
        self.assertEqual(graphics._fit_scale(10, 10), graphics.SCALE)
        big = graphics._fit_scale(500, 500)
        self.assertLess(big, graphics.SCALE)
        self.assertGreaterEqual(big, 1)

    def test_every_scale_that_is_offered_actually_fits_the_cap(self):
        for cols in (1, 17, 80, 300, 500, 900, 2000):
            for rows in (1, 6, 30, 200, 800, 2000):
                scale = graphics._fit_scale(cols, rows)
                if scale:
                    self.assertLessEqual(
                        cols * scale * rows * 2 * scale, graphics.MAX_IMAGE_PX,
                        "%dx%d at scale %d" % (cols, rows, scale))

    def test_a_pane_too_big_even_at_scale_1_gets_no_overlay(self):
        # Clamping at scale 1 would have let the cap be exceeded anyway, which
        # is the one thing the cap exists to prevent.
        self.assertEqual(graphics._fit_scale(2000, 2000), 0)
        # 4015 x 200 cells: 1.6M pixels at scale 1, over the 1.5M cap.
        r = self.renderer()
        self.assertIsNone(graphics.build_overlay(
            [(1, 1, "working", "claude", False),
             (195, 4000, "working", "claude", False)], r.art))


class FakeProtocol:
    """Stands in for office.protocol inside the graphics module."""

    def __init__(self, fail_with=None):
        self.calls = []
        self.fail_with = fail_with
        self.ProtocolError = graphics.protocol.ProtocolError

    def pane_graphics_set(self, sock, pane_id, data, w, h, placement,
                          timeout=5.0):
        self.calls.append(("set", pane_id, w, h, placement))
        if self.fail_with:
            raise self.fail_with

    def pane_graphics_clear(self, sock, pane_id, timeout=5.0):
        self.calls.append(("clear", pane_id))
        if self.fail_with:
            raise self.fail_with

    def pane_graphics_info(self, sock, pane_id, timeout=5.0):
        self.calls.append(("info", pane_id))
        if self.fail_with:
            raise self.fail_with
        return {}


class ProbeTest(unittest.TestCase):
    def setUp(self):
        self.real = graphics.protocol
        self.addCleanup(setattr, graphics, "protocol", self.real)

    def test_ok_when_the_server_answers(self):
        graphics.protocol = FakeProtocol()
        self.assertEqual(graphics.probe("/s", "p1"), (True, "ok"))

    def test_feature_disabled_is_reported_as_the_reason(self):
        graphics.protocol = FakeProtocol(
            fail_with=self.real.ProtocolError("feature_disabled", "nope"))
        ok, reason = graphics.probe("/s", "p1")
        self.assertFalse(ok)
        self.assertEqual(reason, "feature_disabled")

    def test_cell_size_unavailable_also_falls_back(self):
        """Measured on herdr 0.7.4 under WSL with kitty_graphics *enabled*.

        `info` refuses with this code while `pane.graphics.set` still answers
        `ok`, so a successful set is no proof anything rendered. The gate is
        "info succeeded", not "info did not say feature_disabled".
        """
        graphics.protocol = FakeProtocol(
            fail_with=self.real.ProtocolError("cell_size_unavailable",
                                              "host cell size is unavailable"))
        ok, reason = graphics.probe("/s", "p1")
        self.assertFalse(ok)
        self.assertEqual(reason, "cell_size_unavailable")

    def test_transport_failure_is_not_fatal(self):
        graphics.protocol = FakeProtocol(fail_with=OSError("no socket"))
        ok, reason = graphics.probe("/s", "p1")
        self.assertFalse(ok)
        self.assertIn("no socket", reason)

    def test_without_a_pane_id_there_is_nothing_to_draw_on(self):
        graphics.protocol = FakeProtocol()
        self.assertEqual(graphics.probe("/s", None)[0], False)


class SenderTest(unittest.TestCase):
    """The sender's bookkeeping, driven synchronously (no thread started)."""

    def setUp(self):
        self.real = graphics.protocol
        self.addCleanup(setattr, graphics, "protocol", self.real)
        self.q = queue.Queue()

    def sender(self, fake):
        graphics.protocol = fake
        return graphics.GraphicsSender("/s", self.q, "p1")

    def drain(self):
        out = []
        while True:
            try:
                out.append(self.q.get_nowait())
            except queue.Empty:
                return out

    def test_only_the_newest_request_survives(self):
        sender = self.sender(FakeProtocol())
        sender.set_boxes((("a",),), None)
        sender.set_boxes((("b",),), None)
        sender.clear()
        self.assertEqual(sender._pending, ("clear", None))

    def test_every_outcome_is_reported_including_repeats(self):
        """Regression: swallowing a repeated failure killed the retry.

        The loop decides from these reports whether the overlay is on screen.
        A second identical failure that never arrived left it believing the
        re-send had worked, so retrying stopped after the second attempt and
        the overlay stayed missing. Rate limiting belongs to the loop's
        backoff alone.
        """
        fake = FakeProtocol()
        sender = self.sender(fake)
        sender._run(("clear", None))
        sender._run(("clear", None))
        self.assertEqual(self.drain(), [("graphics", (True, ""))] * 2)
        fake.fail_with = self.real.ProtocolError("busy", "server busy")
        sender._run(("clear", None))
        sender._run(("clear", None))
        self.assertEqual(self.drain(), [("graphics", (False, "busy"))] * 2)

    def test_a_transport_error_is_caught_and_reported(self):
        sender = self.sender(FakeProtocol(fail_with=OSError("broken pipe")))
        sender._run(("clear", None))
        (kind, (ok, message)), = self.drain()
        self.assertEqual(kind, "graphics")
        self.assertFalse(ok)
        self.assertIn("broken pipe", message)

    def test_sending_boxes_composes_and_calls_set(self):
        fake = FakeProtocol()
        sender = self.sender(fake)
        r = Renderer(tier=2, truecolor=True)
        r.render(_state(), 120, 40)
        sender._run(("set", (r.sprite_boxes, r.art)))
        self.assertEqual(len(fake.calls), 1)
        name, pane_id, width, height, placement = fake.calls[0]
        self.assertEqual((name, pane_id), ("set", "p1"))
        self.assertGreater(width, 0)
        self.assertEqual(placement["grid_cols"] * graphics.SCALE, width)

    def test_a_failed_set_takes_the_stale_image_down(self):
        """Regression: a wrong overlay outlived the frame it belonged to.

        The previous image stays on the pane when a `set` fails, but the text
        under it has already been redrawn - reflowed by a resize, or replaced
        by the help overlay. Sprites in the wrong place are worse than none,
        since the tier 1 art underneath stands on its own.
        """
        fake = FakeProtocol(
            fail_with=self.real.ProtocolError("busy", "server busy"))
        sender = self.sender(fake)
        r = Renderer(tier=2, truecolor=True)
        r.render(_state(), 120, 40)
        sender._run(("set", (r.sprite_boxes, r.art)))
        self.assertEqual([c[0] for c in fake.calls], ["set", "clear"])
        (_, (ok, message)), = self.drain()
        self.assertFalse(ok)
        self.assertEqual(message, "busy")

    def test_a_failed_clear_is_not_retried_on_the_spot(self):
        # Nothing further to try, and the loop's backoff owns the retry.
        fake = FakeProtocol(fail_with=OSError("gone"))
        sender = self.sender(fake)
        sender._run(("clear", None))
        self.assertEqual([c[0] for c in fake.calls], ["clear"])

    def test_boxes_that_compose_to_nothing_clear_instead(self):
        fake = FakeProtocol()
        sender = self.sender(fake)
        sender._run(("set", ((), None)))
        self.assertEqual(fake.calls, [("clear", "p1")])


if __name__ == "__main__":
    unittest.main()
