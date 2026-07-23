"""Config file loading (design.md section 8).

`HERDR_PLUGIN_CONFIG_DIR/config.toml`, entirely optional: with no file at all
every value falls back to its default, so the plugin is zero-config. Settings
are read once at startup - hot reload is an explicit non-goal (section 8), a
restart of the office pane picks up changes.

Parsing uses `tomllib` on Python 3.11+ and the strict `minitoml` subset reader
on 3.10 (design.md section 12 pins 3.10+; tomllib is 3.11+). Validation never
raises: a bad file degrades to defaults and collects human-readable warnings
which the office pane shows on its status line, so a typo can never stop the
fleet view from coming up. All warning text is ASCII (Windows cp932 consoles).
"""

import os
from dataclasses import dataclass, field
from typing import Tuple

from . import minitoml

try:                                              # Python 3.11+
    import tomllib as _tomllib
except ImportError:                               # pragma: no cover (py3.10)
    _tomllib = None

CONFIG_BASENAME = "config.toml"

FILTERS = ("agents", "all")
RENDERERS = ("auto", "unicode", "ascii", "kitty")
SOUNDS = ("none", "done", "request")
NAME_TEMPLATES = ("{name}", "{name:last-segment}")
FPS_MIN, FPS_MAX = 1, 10
THEMES = ("default",)

_TABLES = ("office", "escalation", "include")


@dataclass(frozen=True)
class Config:
    # [office]
    filter: str = "agents"
    renderer: str = "auto"
    fps: int = 2
    theme: str = "default"
    name_template: str = "{name}"
    # [escalation]
    blocked_threshold_s: float = 90.0
    renotify_interval_s: float = 300.0
    sound: str = "request"
    notify_done: bool = False
    # [include]
    workspaces: Tuple[str, ...] = ()
    exclude_agents: Tuple[str, ...] = ()
    # provenance
    path: str = ""
    warnings: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def force_renderer(self):
        """`renderer` as the Renderer's override argument (None == auto)."""
        return None if self.renderer == "auto" else self.renderer


def config_path(env=None):
    """Absolute path of the config file, or "" when no config dir is set."""
    env = os.environ if env is None else env
    directory = env.get("HERDR_PLUGIN_CONFIG_DIR")
    return os.path.join(directory, CONFIG_BASENAME) if directory else ""


def parse_toml(text: str) -> dict:
    """Parse TOML text with tomllib, falling back to the 3.10 subset reader."""
    if _tomllib is not None:
        return _tomllib.loads(text)
    return minitoml.loads(text)                   # pragma: no cover (py3.10)


def load(env=None) -> Config:
    """Load the config for this process. Never raises."""
    path = config_path(env)
    if not path:
        return Config()
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except FileNotFoundError:
        return Config()                           # zero-config: all defaults
    except OSError as exc:
        return Config(warnings=("config unreadable: %s (using defaults)"
                                % _ascii(exc),))
    try:
        data = parse_toml(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        return Config(path=path,
                      warnings=("config parse error: %s (using defaults)"
                                % _ascii(exc),))
    return from_mapping(data, path=path)


def from_mapping(data, path="") -> Config:
    """Validate a parsed mapping into a Config, collecting warnings."""
    warnings = []
    if not isinstance(data, dict):
        return Config(path=path,
                      warnings=("config root is not a table (using defaults)",))
    for name in data:
        if name not in _TABLES:
            warnings.append("unknown config section [%s] (ignored)"
                            % _ascii(name))

    office = _table(data, "office", warnings)
    escalation = _table(data, "escalation", warnings)
    include = _table(data, "include", warnings)

    defaults = Config()
    values = {
        "filter": _choice(office, "office", "filter", defaults.filter,
                          FILTERS, warnings),
        "renderer": _choice(office, "office", "renderer", defaults.renderer,
                            RENDERERS, warnings),
        "fps": _int_range(office, "office", "fps", defaults.fps,
                          FPS_MIN, FPS_MAX, warnings),
        "theme": _choice(office, "office", "theme", defaults.theme,
                         THEMES, warnings),
        "name_template": _choice(office, "office", "name_template",
                                 defaults.name_template, NAME_TEMPLATES,
                                 warnings),
        "blocked_threshold_s": _seconds(escalation, "escalation",
                                        "blocked_threshold_s",
                                        defaults.blocked_threshold_s,
                                        warnings, minimum=1.0),
        "renotify_interval_s": _seconds(escalation, "escalation",
                                        "renotify_interval_s",
                                        defaults.renotify_interval_s,
                                        warnings, minimum=0.0),
        "sound": _choice(escalation, "escalation", "sound", defaults.sound,
                         SOUNDS, warnings),
        "notify_done": _bool(escalation, "escalation", "notify_done",
                             defaults.notify_done, warnings),
        "workspaces": _str_list(include, "include", "workspaces", warnings),
        "exclude_agents": _str_list(include, "include", "exclude_agents",
                                    warnings),
    }
    _warn_unknown_keys(office, "office", warnings,
                       ("filter", "renderer", "fps", "theme", "name_template"))
    _warn_unknown_keys(escalation, "escalation", warnings,
                       ("blocked_threshold_s", "renotify_interval_s", "sound",
                        "notify_done"))
    _warn_unknown_keys(include, "include", warnings,
                       ("workspaces", "exclude_agents"))
    if values["renderer"] == "kitty":
        # design.md section 5: tier 2 is opt-in and not implemented yet
        # (issue #6). Fall back with a warning rather than a blank office.
        warnings.append("renderer=kitty is not implemented yet; using unicode")
        values["renderer"] = "unicode"
    return Config(path=path, warnings=tuple(warnings), **values)


# -- validation helpers --------------------------------------------------

def _ascii(value) -> str:
    """ASCII-safe rendering of arbitrary text for warnings (cp932 consoles)."""
    return str(value).encode("ascii", "replace").decode("ascii")


def _table(data, name, warnings) -> dict:
    value = data.get(name, {})
    if not isinstance(value, dict):
        warnings.append("[%s] is not a table (ignored)" % name)
        return {}
    return value


def _warn_unknown_keys(table, name, warnings, known):
    for key in table:
        if key not in known:
            warnings.append("unknown config key [%s].%s (ignored)"
                            % (name, _ascii(key)))


def _missing(table, key):
    return key not in table


def _choice(table, section, key, default, allowed, warnings):
    if _missing(table, key):
        return default
    value = table[key]
    if isinstance(value, str) and value in allowed:
        return value
    warnings.append("[%s].%s must be one of %s (using %r)"
                    % (section, key, "/".join(allowed), default))
    return default


def _int_range(table, section, key, default, low, high, warnings):
    if _missing(table, key):
        return default
    value = table[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        warnings.append("[%s].%s must be a number (using %r)"
                        % (section, key, default))
        return default
    clamped = max(low, min(high, int(value)))
    if clamped != value:
        warnings.append("[%s].%s clamped to %d..%d (using %d)"
                        % (section, key, low, high, clamped))
    return clamped


def _seconds(table, section, key, default, warnings, minimum=0.0):
    if _missing(table, key):
        return default
    value = table[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        warnings.append("[%s].%s must be a number of seconds (using %g)"
                        % (section, key, default))
        return default
    if value < minimum:
        warnings.append("[%s].%s must be >= %g (using %g)"
                        % (section, key, minimum, default))
        return default
    return float(value)


def _bool(table, section, key, default, warnings):
    if _missing(table, key):
        return default
    value = table[key]
    if isinstance(value, bool):
        return value
    warnings.append("[%s].%s must be true or false (using %r)"
                    % (section, key, default))
    return default


def _str_list(table, section, key, warnings):
    if _missing(table, key):
        return ()
    value = table[key]
    if not isinstance(value, (list, tuple)):
        warnings.append("[%s].%s must be a list of strings (ignored)"
                        % (section, key))
        return ()
    out = [item for item in value if isinstance(item, str)]
    if len(out) != len(value):
        warnings.append("[%s].%s: non-string entries ignored" % (section, key))
    return tuple(out)
