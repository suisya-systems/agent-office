"""A minimal RGBA PNG encoder, stdlib only (`zlib` + `struct`).

design.md section 12 pins the plugin to the standard library, so tier 2 cannot
reach for Pillow to turn the sprite grids into an image. That is affordable
here because the office needs exactly one corner of PNG: 8-bit RGBA, no
interlacing, no ancillary chunks, filter type 0 on every row. That is the
smallest thing every PNG decoder is required to understand.

**Why RGBA and not indexed.** The tier 2 overlay is a single image covering the
whole desk area, and everything that is not a sprite - nameplates, status
words, borders, the gaps between desks - has to stay readable underneath it
(design.md section 5: only the sprite part moves into graphics). Transparency
is what buys that, so the alpha channel is the whole point rather than a
luxury. The cost is size, and zlib takes most of it back: the image is large
flat runs of one colour and one big transparent field.
"""

import struct
import zlib

SIGNATURE = b"\x89PNG\r\n\x1a\n"

TRANSPARENT = (0, 0, 0, 0)

_MAX_DIM = 1 << 16          # sanity bound; a terminal-sized overlay is far less


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def encode_rgba(width: int, height: int, buf, level: int = 6) -> bytes:
    """Encode a row-major RGBA byte buffer (len == width*height*4) as a PNG."""
    if width <= 0 or height <= 0:
        raise ValueError("png: dimensions must be positive")
    if width > _MAX_DIM or height > _MAX_DIM:
        raise ValueError("png: dimensions out of range")
    stride = width * 4
    if len(buf) != stride * height:
        raise ValueError("png: buffer is %d bytes, expected %d"
                         % (len(buf), stride * height))
    raw = bytearray()
    for y in range(height):
        raw.append(0)                              # filter type 0 (None)
        raw += buf[y * stride:(y + 1) * stride]
    ihdr = struct.pack(">IIBBBBB",
                       width, height,
                       8,      # bit depth
                       6,      # colour type 6 == truecolour with alpha
                       0, 0, 0)  # deflate / adaptive filtering / no interlace
    return (SIGNATURE
            + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(bytes(raw), level))
            + _chunk(b"IEND", b""))


class Canvas:
    """A fixed-size RGBA pixel buffer, transparent until something is drawn.

    Deliberately a flat `bytearray` rather than a list of pixel tuples: the
    office redraws the whole overlay whenever the fleet changes, and a
    per-pixel Python object would dominate that cost on a large screen.
    """

    def __init__(self, width: int, height: int):
        if width <= 0 or height <= 0:
            raise ValueError("canvas: dimensions must be positive")
        self.width = width
        self.height = height
        self.buf = bytearray(width * height * 4)   # all zero == transparent

    def blit(self, x: int, y: int, pixel_rows, scale: int = 1,
             alpha: int = 255) -> None:
        """Draw `pixel_rows` (rows of (r,g,b)) at (x,y), each pixel `scale`^2.

        Rows are built once and written `scale` times, which is what keeps the
        upscale cheap. Anything falling outside the canvas is clipped rather
        than wrapping into the next row.
        """
        if scale < 1:
            raise ValueError("canvas: scale must be >= 1")
        for sy, row in enumerate(pixel_rows):
            line = bytearray()
            for pixel in row:
                line += bytes((pixel[0], pixel[1], pixel[2], alpha)) * scale
            self._write_row_runs(x, y + sy * scale, line, scale)

    def _write_row_runs(self, x: int, top: int, line: bytearray,
                        repeat: int) -> None:
        # Horizontal clip once, then repeat the (already clipped) run.
        start = x
        left_trim = 0
        if start < 0:
            left_trim = -start * 4
            start = 0
        width_px = (len(line) - left_trim) // 4
        if width_px <= 0:
            return
        if start + width_px > self.width:
            width_px = self.width - start
        if width_px <= 0:
            return
        run = bytes(line[left_trim:left_trim + width_px * 4])
        for dy in range(repeat):
            oy = top + dy
            if 0 <= oy < self.height:
                off = (oy * self.width + start) * 4
                self.buf[off:off + len(run)] = run

    def to_png(self, level: int = 6) -> bytes:
        return encode_rgba(self.width, self.height, self.buf, level)
