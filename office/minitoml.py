"""Minimal TOML reader for Python 3.10 (design.md section 12).

design.md section 12 pins Python 3.10+ and stdlib-only, but `tomllib` only
landed in 3.11. On 3.11+ `config.py` uses tomllib; on 3.10 it falls back to
this module, which reads the strict subset the Agent Office config schema uses
(section 8): plain tables, string / integer / float / boolean scalars, and
single- or multi-line arrays of those.

It is deliberately strict: anything it does not recognise (dates, inline
tables, arrays of tables, multi-line strings) raises TomlError with a line
number instead of guessing. A config that silently means something other than
what the user wrote is worse than one that refuses to load.
"""

_ESCAPES = {
    "n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f",
    '"': '"', "\\": "\\",
}


class TomlError(ValueError):
    pass


def loads(text: str) -> dict:
    """Parse a TOML document (subset) into nested dicts."""
    root = {}
    table = root
    table_path = ()
    seen_tables = set()
    lines = text.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        lineno = i + 1
        line = _strip_comment(lines[i], lineno).strip()
        i += 1
        if not line:
            continue
        if line.startswith("["):
            if line.startswith("[["):
                raise TomlError("line %d: arrays of tables are not supported"
                                % lineno)
            if not line.endswith("]"):
                raise TomlError("line %d: unterminated table header" % lineno)
            table_path = _split_key(line[1:-1].strip(), lineno)
            if table_path in seen_tables:
                raise TomlError("line %d: table [%s] defined twice"
                                % (lineno, ".".join(table_path)))
            seen_tables.add(table_path)
            table = _descend(root, table_path, lineno)
            continue
        key_text, sep, value_text = line.partition("=")
        if not sep:
            raise TomlError("line %d: expected 'key = value'" % lineno)
        key_path = _split_key(key_text.strip(), lineno)
        value_text = value_text.strip()
        # A bracket left open means a multi-line array; keep pulling lines in.
        while _bracket_depth(value_text, lineno) > 0:
            if i >= len(lines):
                raise TomlError("line %d: unterminated array" % lineno)
            value_text += " " + _strip_comment(lines[i], i + 1).strip()
            i += 1
        target = _descend(table, key_path[:-1], lineno)
        name = key_path[-1]
        if name in target:
            raise TomlError("line %d: key %r defined twice" % (lineno, name))
        target[name] = _parse_value(value_text, lineno)
    return root


# -- lexing helpers ------------------------------------------------------

def _strip_comment(line: str, lineno: int) -> str:
    """Drop a trailing '#' comment, respecting quoted spans."""
    out = []
    quote = None
    escaped = False
    for ch in line:
        if quote is not None:
            out.append(ch)
            if escaped:
                escaped = False
            elif quote == '"' and ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            continue
        if ch == "#":
            break
        if ch in ('"', "'"):
            quote = ch
        out.append(ch)
    if quote is not None:
        raise TomlError("line %d: unterminated string" % lineno)
    return "".join(out)


def _bracket_depth(text: str, lineno: int) -> int:
    """Net '[' minus ']' outside of quoted spans (negative clamps to 0)."""
    depth = 0
    quote = None
    escaped = False
    for ch in text:
        if quote is not None:
            if escaped:
                escaped = False
            elif quote == '"' and ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            continue
        if ch in ('"', "'"):
            quote = ch
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
    if quote is not None:
        raise TomlError("line %d: unterminated string" % lineno)
    return max(0, depth)


def _split_key(text: str, lineno: int):
    """Split a (possibly dotted, possibly quoted) key into a tuple of parts."""
    if not text:
        raise TomlError("line %d: empty key" % lineno)
    parts = []
    current = []
    quote = None
    escaped = False
    for ch in text:
        if quote is not None:
            if escaped:
                current.append(ch)
                escaped = False
            elif quote == '"' and ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            else:
                current.append(ch)
            continue
        if ch in ('"', "'"):
            quote = ch
        elif ch == ".":
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    if quote is not None:
        raise TomlError("line %d: unterminated quoted key" % lineno)
    parts.append("".join(current).strip())
    if not all(parts):
        raise TomlError("line %d: malformed key %r" % (lineno, text))
    return tuple(parts)


def _descend(table: dict, path, lineno: int) -> dict:
    for part in path:
        nxt = table.setdefault(part, {})
        if not isinstance(nxt, dict):
            raise TomlError("line %d: %r is not a table" % (lineno, part))
        table = nxt
    return table


# -- value parsing -------------------------------------------------------

def _parse_value(text: str, lineno: int):
    text = text.strip()
    if not text:
        raise TomlError("line %d: missing value" % lineno)
    if text[0] in ('"', "'"):
        return _parse_string(text, lineno)
    if text[0] == "[":
        return _parse_array(text, lineno)
    if text in ("true", "false"):
        return text == "true"
    if text.startswith("{"):
        raise TomlError("line %d: inline tables are not supported" % lineno)
    digits = text.replace("_", "")
    try:
        return int(digits, 10)
    except ValueError:
        pass
    try:
        return float(digits)
    except ValueError:
        pass
    raise TomlError("line %d: unsupported value %r" % (lineno, text))


def _parse_string(text: str, lineno: int) -> str:
    quote = text[0]
    if text.startswith(quote * 3):
        raise TomlError("line %d: multi-line strings are not supported" % lineno)
    if len(text) < 2 or text[-1] != quote:
        raise TomlError("line %d: unterminated string" % lineno)
    body = text[1:-1]
    if quote == "'":                       # literal string: no escapes at all
        if "'" in body:
            raise TomlError("line %d: stray quote in literal string" % lineno)
        return body
    out = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\":
            nxt = body[i + 1:i + 2]
            if nxt in ("u", "U"):
                width = 4 if nxt == "u" else 8
                hexits = body[i + 2:i + 2 + width]
                if len(hexits) != width:
                    raise TomlError("line %d: truncated \\%s escape"
                                    % (lineno, nxt))
                try:
                    out.append(chr(int(hexits, 16)))
                except ValueError:
                    raise TomlError("line %d: bad \\%s escape" % (lineno, nxt))
                i += 2 + width
                continue
            if nxt not in _ESCAPES:
                raise TomlError("line %d: unknown escape %r" % (lineno, nxt))
            out.append(_ESCAPES[nxt])
            i += 2
            continue
        if ch == '"':
            raise TomlError("line %d: unescaped quote in string" % lineno)
        out.append(ch)
        i += 1
    return "".join(out)


def _parse_array(text: str, lineno: int) -> list:
    if not text.endswith("]"):
        raise TomlError("line %d: unterminated array" % lineno)
    items = []
    current = []
    depth = 0
    quote = None
    escaped = False
    for ch in text[1:-1]:
        if quote is not None:
            current.append(ch)
            if escaped:
                escaped = False
            elif quote == '"' and ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            continue
        if ch in ('"', "'"):
            quote = ch
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        elif ch == "," and depth == 0:
            items.append("".join(current))
            current = []
            continue
        current.append(ch)
    if quote is not None:
        raise TomlError("line %d: unterminated string in array" % lineno)
    items.append("".join(current))
    return [_parse_value(item, lineno) for item in items if item.strip()]
