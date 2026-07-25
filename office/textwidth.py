"""Display width - how many terminal columns a string actually occupies.

Issue #25. Every layout decision in the renderer used to count Python
characters, which is only right for the Latin half of Unicode: a Japanese
nameplate counted 12 and drew 24, so it walked straight out of its own box and
took the desk borders with it.

The rules here are the terminal's, not Python's:

  * East Asian Width `W` (wide) and `F` (fullwidth) occupy two columns - CJK,
    kana, fullwidth punctuation and most emoji.
  * Combining marks (`Mn`/`Me`) and format characters (`Cf`) occupy none: they
    are drawn on top of, or between, the characters around them.
  * A variation selector *changes* the character before it rather than adding
    to it. U+FE0F (emoji presentation) makes its base two columns wide even
    when the base alone is one - `warning sign` U+26A0 draws in one cell, and
    U+26A0 U+FE0F draws in two. Measuring per code point gets this wrong in
    the one direction that matters, so text is measured in *units*: a base
    character plus everything bound to it (see `units`).

`A` (ambiguous) is deliberately treated as one column. It is genuinely
terminal- and font-dependent, and one column is what a terminal in a UTF-8
locale does by default. Note that the renderer's own chrome - the half-block
`▀`, the box-drawing borders, the compact view's `●` - is all ambiguous-width,
so a terminal configured to draw ambiguous characters *wide* (an option some
CJK-oriented terminals offer, and which some Japanese users turn on) will
misdraw the pixel art itself, not only this measurement. The plugin assumes
ambiguous == 1 throughout; that assumption is older than this module.

Two known limits, both erring towards over-measuring, which cuts text one
column early rather than letting it overflow a box:

  * Emoji ZWJ sequences and skin-tone modifiers are held together so they are
    never split, but a joined sequence is charged the sum of its bases. A
    terminal that renders the sequence as a single glyph draws it narrower
    than measured.
  * `unicodedata` ships with the interpreter, so a code point assigned after
    the CI matrix's older Python can measure differently on 3.10 than on 3.12.

**These functions take plain text, never text with ANSI in it.** An escape
sequence is invisible, but its bytes are not all zero-width by this measure,
so callers must budget and truncate the visible text first and add colour to
the result (see `Renderer._fit`). That ordering is the whole reason the helper
can stay this small.
"""

import unicodedata

_ZERO_WIDTH_CATEGORIES = frozenset(("Mn", "Me", "Cf", "Cc"))
_WIDE = frozenset(("W", "F"))

VS16 = "️"                  # emoji presentation: forces the base wide
VS15 = "︎"                  # text presentation: forces the base narrow
ZWJ = "‍"                   # binds the next character into this cluster
_MODIFIER_FIRST, _MODIFIER_LAST = 0x1F3FB, 0x1F3FF     # skin tone modifiers


def char_width(ch):
    """Columns occupied by a single character in isolation: 0, 1 or 2.

    Isolation is the caveat: a character followed by a variation selector or a
    modifier is not measured by this function alone. Use `width`.
    """
    if unicodedata.category(ch) in _ZERO_WIDTH_CATEGORIES:
        return 0
    return 2 if unicodedata.east_asian_width(ch) in _WIDE else 1


def _binds_to_previous(ch):
    """True for a character that belongs to the cluster before it.

    Control characters are zero-width but bind to nothing: a newline is a
    boundary, not an accent, and a cluster that swallowed one could not be
    replaced or cut without taking the line break with it.
    """
    if unicodedata.category(ch) == "Cc":
        return False
    return (char_width(ch) == 0
            or _MODIFIER_FIRST <= ord(ch) <= _MODIFIER_LAST)


def units(text):
    """Yield `(chunk, columns)` - one cluster at a time, never splittable.

    A chunk is a base character plus every character bound to it: combining
    marks, variation selectors, skin-tone modifiers, and anything a zero-width
    joiner pulls in. Truncation works in these units, so it can never leave a
    dangling accent, a joiner with nothing after it, or a base separated from
    the selector that decides how wide it draws.
    """
    i, n = 0, len(text)
    while i < n:
        start = i
        cols = char_width(text[i])
        i += 1
        while i < n and _binds_to_previous(text[i]):
            ch = text[i]
            i += 1
            if ch == VS16:
                cols = 2
            elif ch == VS15:
                cols = 1
            elif ch == ZWJ and i < n:
                # The joiner binds the next character in; charge it too. A
                # terminal that fuses the sequence into one glyph draws this
                # narrower than measured, which is the safe way to be wrong.
                cols += char_width(text[i])
                i += 1
        yield text[start:i], cols


def width(text):
    """Columns occupied by `text` when the terminal draws it."""
    return sum(cols for _chunk, cols in units(text))


def cut(text, limit):
    """The fitting prefix of `text` and the number of columns it occupies.

    `truncate` is this without the measurement. Callers that then have to pad,
    centre or subtract from a budget want the width back rather than walking
    the result a second time, which on a redraw is the whole string again.
    """
    if limit <= 0:
        return "", 0
    used = 0
    out = []
    for chunk, cols in units(text):
        if cols and used + cols > limit:
            break
        used += cols
        out.append(chunk)
    return "".join(out), used


def truncate(text, limit):
    """The longest prefix of `text` that fits in `limit` columns.

    Never emits a partial cluster: a full-width character that would cross the
    boundary is dropped whole, so the result can be one column narrower than
    `limit` rather than one column over it.
    """
    return cut(text, limit)[0]


def pad(text, limit):
    """`text` truncated to `limit` columns, then space-filled to exactly that."""
    text, used = cut(text, limit)
    return text + " " * (limit - used)


def center(text, limit):
    """`text` truncated to `limit` columns and centred within them.

    An odd column of slack goes to the right, and full-width text can leave an
    odd remainder even when the string looks symmetrical - hence padding
    computed from the visible width rather than from `len`.
    """
    text, used = cut(text, limit)
    slack = limit - used
    left = slack // 2
    return " " * left + text + " " * (slack - left)


def wrap(text, limit, lines):
    """Split `text` into exactly `lines` chunks of at most `limit` columns.

    Greedy on display width and not word-aware on purpose: the callers are
    nameplates, and a Japanese title has no spaces to break on. Anything past
    the last chunk is dropped, and short input is padded out with empty strings
    so the caller always gets a fixed number of rows to place.
    """
    out = []
    rest = text
    while rest and len(out) < lines:
        piece = truncate(rest, limit)
        if not piece:                             # limit too small to advance
            break
        out.append(piece)
        # Whitespace at the break would push the continuation row off centre
        # for no visible gain; the break itself already says "same name".
        # lstrip() with no argument also takes U+3000, the ideographic space a
        # Japanese title is far more likely to hold than an ASCII one.
        rest = rest[len(piece):].lstrip()
    return out + [""] * (lines - len(out))
