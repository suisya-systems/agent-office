"""Unit tests for config loading and the 3.10 TOML fallback (section 8/12).

`minitoml` is exercised directly, not only through `parse_toml`, so the
Python 3.10 path is covered even when the tests run on 3.11+ where
`config.parse_toml` prefers the stdlib `tomllib`.
"""

import os
import tempfile
import unittest

from office import config as config_mod
from office import minitoml
from office.config import Config, from_mapping, load

SAMPLE = """
# Agent Office configuration
[office]
filter = "all"
renderer = "ascii"
fps = 4
name_template = "{name:last-segment}"

[escalation]
blocked_threshold_s = 30
renotify_interval_s = 0
sound = "none"
notify_done = true

[include]
workspaces = ["claude-org/*", "solo"]
exclude_agents = [
    "codex",       # trailing comment inside an array
]
"""


class MiniTomlTest(unittest.TestCase):
    def parse(self, text):
        return minitoml.loads(text)

    def test_parses_the_documented_schema(self):
        data = self.parse(SAMPLE)
        self.assertEqual(data["office"], {
            "filter": "all", "renderer": "ascii", "fps": 4,
            "name_template": "{name:last-segment}"})
        self.assertEqual(data["escalation"], {
            "blocked_threshold_s": 30, "renotify_interval_s": 0,
            "sound": "none", "notify_done": True})
        self.assertEqual(data["include"]["workspaces"],
                         ["claude-org/*", "solo"])
        self.assertEqual(data["include"]["exclude_agents"], ["codex"])

    def test_matches_tomllib_on_the_sample(self):
        try:
            import tomllib
        except ImportError:                       # pragma: no cover (py3.10)
            self.skipTest("tomllib not available")
        self.assertEqual(self.parse(SAMPLE), tomllib.loads(SAMPLE))

    def test_types(self):
        data = self.parse('a = 1\nb = 1.5\nc = true\nd = false\n'
                          'e = "x"\nf = 1_000\ng = -2\n')
        self.assertEqual(data, {"a": 1, "b": 1.5, "c": True, "d": False,
                                "e": "x", "f": 1000, "g": -2})

    def test_hash_inside_string_is_not_a_comment(self):
        self.assertEqual(self.parse('a = "x # y"  # real comment'),
                         {"a": "x # y"})

    def test_literal_string_keeps_backslashes(self):
        self.assertEqual(self.parse(r"a = 'c:\path\n'"), {"a": r"c:\path\n"})

    def test_escapes(self):
        self.assertEqual(self.parse(r'a = "x\ty\nz\"q\\"'),
                         {"a": "x\ty\nz\"q\\"})
        self.assertEqual(self.parse(r'a = "\u0041"'), {"a": "A"})

    def test_multiline_array(self):
        self.assertEqual(
            self.parse('a = [\n  "x",\n  "y",\n]\n'), {"a": ["x", "y"]})

    def test_empty_array(self):
        self.assertEqual(self.parse("a = []"), {"a": []})

    def test_dotted_table_header(self):
        self.assertEqual(self.parse("[a.b]\nc = 1"), {"a": {"b": {"c": 1}}})

    def test_crlf_and_blank_lines(self):
        self.assertEqual(self.parse("\r\n[t]\r\n\r\na = 1\r\n"),
                         {"t": {"a": 1}})

    # -- strictness: never guess, always raise -----------------------

    def _rejects(self, text):
        with self.assertRaises(minitoml.TomlError):
            self.parse(text)

    def test_rejects_unsupported_and_malformed_input(self):
        self._rejects("a = 2026-07-24")            # dates
        self._rejects("a = {b = 1}")               # inline table
        self._rejects("[[a]]\nb = 1")              # array of tables
        self._rejects('a = "unterminated')
        self._rejects("a = [1, 2")                 # unterminated array
        self._rejects("just some words")           # no '='
        self._rejects("a = ")                      # no value
        self._rejects('a = """x"""')               # multi-line string
        self._rejects(r'a = "\q"')                 # unknown escape
        self._rejects("a = 1\na = 2")              # duplicate key
        self._rejects("[t]\nb = 1\n[t]\nc = 2")    # duplicate table
        self._rejects("a = 0x10")                  # hex ints unsupported


class FromMappingTest(unittest.TestCase):
    def test_defaults_when_empty(self):
        cfg = from_mapping({})
        self.assertEqual(cfg, Config())
        self.assertEqual(cfg.warnings, ())

    def test_full_sample(self):
        cfg = from_mapping(minitoml.loads(SAMPLE))
        self.assertEqual(cfg.warnings, ())
        self.assertEqual(cfg.filter, "all")
        self.assertEqual(cfg.renderer, "ascii")
        self.assertEqual(cfg.fps, 4)
        self.assertEqual(cfg.name_template, "{name:last-segment}")
        self.assertEqual(cfg.blocked_threshold_s, 30.0)
        self.assertEqual(cfg.renotify_interval_s, 0.0)
        self.assertEqual(cfg.sound, "none")
        self.assertTrue(cfg.notify_done)
        self.assertEqual(cfg.workspaces, ("claude-org/*", "solo"))
        self.assertEqual(cfg.exclude_agents, ("codex",))

    def test_force_renderer_property(self):
        self.assertIsNone(Config().force_renderer)
        self.assertEqual(Config(renderer="ascii").force_renderer, "ascii")

    def test_bad_values_fall_back_with_warnings(self):
        cfg = from_mapping({
            "office": {"filter": "nope", "renderer": 7, "fps": "fast",
                       "name_template": "{whatever}"},
            "escalation": {"blocked_threshold_s": -5, "sound": "airhorn",
                           "notify_done": "yes"},
            "include": {"workspaces": "not-a-list",
                        "exclude_agents": ["ok", 3]},
        })
        self.assertEqual(cfg.filter, "agents")
        self.assertEqual(cfg.renderer, "auto")
        self.assertEqual(cfg.fps, 2)
        self.assertEqual(cfg.name_template, "{name}")
        self.assertEqual(cfg.blocked_threshold_s, 90.0)
        self.assertEqual(cfg.sound, "request")
        self.assertFalse(cfg.notify_done)
        self.assertEqual(cfg.workspaces, ())
        self.assertEqual(cfg.exclude_agents, ("ok",))
        self.assertEqual(len(cfg.warnings), 9)

    def test_fps_is_clamped(self):
        self.assertEqual(from_mapping({"office": {"fps": 99}}).fps, 10)
        self.assertEqual(from_mapping({"office": {"fps": 0}}).fps, 1)

    def test_kitty_falls_back_to_unicode_with_a_warning(self):
        cfg = from_mapping({"office": {"renderer": "kitty"}})
        self.assertEqual(cfg.renderer, "unicode")
        self.assertTrue(any("kitty" in w for w in cfg.warnings))

    def test_unknown_keys_and_sections_are_reported(self):
        cfg = from_mapping({"office": {"colour": "red"}, "nope": {}})
        self.assertEqual(cfg, Config(warnings=cfg.warnings))
        self.assertEqual(sorted(cfg.warnings), [
            "unknown config key [office].colour (ignored)",
            "unknown config section [nope] (ignored)"])

    def test_warnings_are_ascii(self):
        cfg = from_mapping({"office": {"filter": "\u3042"},
                            "\u3044": {}})
        for warning in cfg.warnings:
            warning.encode("ascii")               # must not raise

    def test_non_table_section_is_ignored(self):
        cfg = from_mapping({"office": 5})
        self.assertEqual(cfg.filter, "agents")
        self.assertTrue(any("not a table" in w for w in cfg.warnings))

    def test_notify_done_rejects_truthy_non_bool(self):
        self.assertFalse(from_mapping({"escalation":
                                       {"notify_done": 1}}).notify_done)


class LoadTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.env = {"HERDR_PLUGIN_CONFIG_DIR": self.dir.name}

    def write(self, text):
        path = os.path.join(self.dir.name, config_mod.CONFIG_BASENAME)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def test_no_config_dir_is_zero_config(self):
        self.assertEqual(load({}), Config())

    def test_missing_file_is_zero_config(self):
        cfg = load(self.env)
        self.assertEqual(cfg, Config())
        self.assertEqual(cfg.warnings, ())

    def test_reads_a_real_file(self):
        path = self.write(SAMPLE)
        cfg = load(self.env)
        self.assertEqual(cfg.path, path)
        self.assertEqual(cfg.filter, "all")
        self.assertEqual(cfg.blocked_threshold_s, 30.0)

    def test_broken_file_degrades_to_defaults(self):
        self.write("this is not toml at all")
        cfg = load(self.env)
        self.assertEqual(cfg.filter, Config().filter)
        self.assertEqual(len(cfg.warnings), 1)
        self.assertIn("parse error", cfg.warnings[0])

    def test_non_utf8_file_degrades_to_defaults(self):
        path = os.path.join(self.dir.name, config_mod.CONFIG_BASENAME)
        with open(path, "wb") as handle:
            handle.write(b'[office]\nfilter = "\xff\xfe"\n')
        cfg = load(self.env)
        self.assertEqual(cfg.filter, "agents")
        self.assertTrue(cfg.warnings)

    def test_config_path(self):
        self.assertEqual(config_mod.config_path({}), "")
        self.assertTrue(config_mod.config_path(self.env).endswith("config.toml"))

    def test_parse_toml_agrees_with_minitoml(self):
        self.assertEqual(config_mod.parse_toml(SAMPLE), minitoml.loads(SAMPLE))


if __name__ == "__main__":
    unittest.main()
