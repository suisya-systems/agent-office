"""Themes and per-agent characters (design.md sections 8 and 11)."""

import unittest

from office import sprites, themes
from office.renderer import Renderer
from office.state import OfficeState


class ThemeTest(unittest.TestCase):
    def test_every_theme_defines_every_key(self):
        """A missing key would be a KeyError mid-render, on that theme only."""
        for name in themes.NAMES:
            theme = themes.get(name)
            self.assertEqual(set(theme.palette), set(themes.DEFAULT_PALETTE),
                             "palette keys differ for %s" % name)
            self.assertEqual(set(theme.ui), set(themes.DEFAULT_UI),
                             "ui keys differ for %s" % name)

    def test_every_colour_is_a_valid_rgb_triple(self):
        for name in themes.NAMES:
            theme = themes.get(name)
            for source in (theme.palette, theme.ui):
                for key, value in source.items():
                    self.assertEqual(len(value), 3, "%s.%s" % (name, key))
                    for channel in value:
                        self.assertIsInstance(channel, int)
                        self.assertTrue(0 <= channel <= 255,
                                        "%s.%s = %r" % (name, key, value))

    def test_default_is_the_pre_theme_palette(self):
        # The setting is opt-in: someone who never writes a config file must
        # see exactly the colours they saw before themes existed.
        self.assertEqual(themes.get().palette, themes.DEFAULT_PALETTE)
        self.assertEqual(themes.get().ui, themes.DEFAULT_UI)

    def test_an_unknown_name_resolves_to_default(self):
        theme = themes.get("no-such-theme")
        self.assertEqual(theme.name, "default")
        self.assertEqual(theme.palette, themes.DEFAULT_PALETTE)

    def test_themes_actually_differ(self):
        seen = {name: themes.get(name).palette["floor_a"]
                for name in themes.NAMES}
        self.assertEqual(len(set(seen.values())), len(themes.NAMES))

    def test_default_is_listed_first(self):
        self.assertEqual(themes.NAMES[0], themes.DEFAULT_NAME)


class ThemedRenderTest(unittest.TestCase):
    def state(self):
        s = OfficeState()
        s.ingest_pane({"pane_id": "p1", "workspace_id": "w1",
                       "agent": "claude", "agent_status": "blocked"})
        return s

    def test_each_theme_renders_a_frame(self):
        for name in themes.NAMES:
            for tier in (0, 1, 2):
                frame = Renderer(tier=tier, truecolor=True,
                                 theme=name).render(self.state(), 120, 40)
                self.assertIn("AGENT OFFICE", frame)

    def test_theme_reaches_tier0_text_colours(self):
        """tier 0 has no sprite pixels, so `ui` is all a theme can change."""
        plain = Renderer(tier=0, truecolor=True, theme="default").render(
            self.state(), 120, 40)
        night = Renderer(tier=0, truecolor=True, theme="midnight").render(
            self.state(), 120, 40)
        self.assertNotEqual(plain, night)

    def test_256_colour_mode_emits_no_truecolor_escapes(self):
        # Theme colours are 24-bit tuples; on a 256-colour terminal they have
        # to be quantised rather than written out raw.
        frame = Renderer(tier=0, truecolor=False, theme="midnight").render(
            self.state(), 120, 40)
        self.assertNotIn("\x1b[38;2;", frame)
        self.assertIn("\x1b[38;5;", frame)

    def test_a_theme_object_can_be_passed_directly(self):
        theme = themes.get("daylight")
        self.assertIs(Renderer(tier=1, theme=theme).theme, theme)


class CharacterTest(unittest.TestCase):
    def test_known_agents_each_get_their_own_look(self):
        grids = {}
        for agent in ("claude", "codex", "gemini", "cursor", "droid", None):
            grids[agent] = tuple(sprites.grid_for("working", 0, agent))
        self.assertEqual(len(set(grids.values())), len(grids))

    def test_unknown_agent_gets_the_default_character(self):
        self.assertIs(sprites.character_for("no-such-agent"),
                      sprites.DEFAULT_CHARACTER)
        self.assertIs(sprites.character_for(None), sprites.DEFAULT_CHARACTER)
        self.assertIs(sprites.character_for(""), sprites.DEFAULT_CHARACTER)

    def test_agent_names_are_matched_leniently(self):
        claude = sprites.CHARACTERS["claude"]
        self.assertIs(sprites.character_for("Claude"), claude)
        self.assertIs(sprites.character_for(" claude "), claude)
        self.assertIs(sprites.character_for("claude-code"), claude)

    def test_the_state_patch_always_wins_over_the_character(self):
        """The raised hand and bubble are how "blocked" reads - never lose them.

        droid's helmet occupies row 1 columns 4-6 and the escalated bubble
        occupies row 1 columns 2-5, so this is a real overlap, not a
        hypothetical one.
        """
        for agent in list(sprites.CHARACTERS) + [None]:
            for state in ("blocked", "blocked_escalated"):
                grid = sprites.grid_for(state, 0, agent)
                self.assertEqual(grid[3][8], "S", "%s/%s hand" % (agent, state))
                self.assertEqual(grid[4][8], "B", "%s/%s arm" % (agent, state))
                bubble = "W!W" if state == "blocked" else "WXXW"
                self.assertTrue(grid[2].startswith(".." + bubble),
                                "%s/%s bubble: %r" % (agent, state, grid[2]))

    def test_an_empty_desk_has_no_occupant_and_no_headgear(self):
        for agent in list(sprites.CHARACTERS) + [None]:
            grid = sprites.grid_for("empty", 0, agent)
            for row in grid[:5]:
                self.assertNotIn("H", row)
                self.assertNotIn("S", row)

    def test_grids_keep_their_shape_for_every_combination(self):
        known = set(".BMSH") | set(sprites.SPRITE_COLORS)
        for agent in list(sprites.CHARACTERS) + [None, "mystery"]:
            for state in sprites.STATE_PATCHES:
                for frame in (0, 1):
                    grid = sprites.grid_for(state, frame, agent)
                    self.assertEqual(len(grid), sprites.DESK_H)
                    for row in grid:
                        self.assertEqual(len(row), sprites.DESK_W)
                        self.assertTrue(set(row) <= known,
                                        "%s/%s: %r" % (agent, state, row))

    def test_unknown_status_greys_out_the_headgear_too(self):
        """A character must not stay colourful while its status is unknown."""
        palette = themes.DEFAULT_PALETTE
        rows = sprites.desk_pixel_rows("unknown", 0, palette, "gemini")
        grid = sprites.grid_for("unknown", 0, "gemini")
        grey = palette["shirt_unknown"]
        for y, line in enumerate(grid):
            for x, char in enumerate(line):
                if char == "H":
                    self.assertEqual(rows[y][x], grey)

    def test_tier0_swaps_the_head_glyph(self):
        heads = set()
        for agent in ("claude", "codex", "gemini", "cursor", "droid"):
            lines = sprites.desk_tier0_lines("working", 0, agent)
            heads.add(lines[1].strip())
        self.assertEqual(len(heads), 5)

    def test_tier0_art_keeps_its_width_and_placeholder_is_consumed(self):
        for agent in list(sprites.CHARACTERS) + [None]:
            for state in sprites.ASCII_ART:
                for frame in (0, 1):
                    for line in sprites.desk_tier0_lines(state, frame, agent):
                        self.assertEqual(len(line), sprites.ASCII_W)
                        self.assertNotIn("@", line)

    def test_the_agent_reaches_the_rendered_frame(self):
        def frame_for(agent, tier):
            s = OfficeState()
            s.ingest_pane({"pane_id": "p1", "workspace_id": "w1",
                           "agent": agent, "agent_status": "working"})
            return Renderer(tier=tier, truecolor=True).render(s, 120, 40)

        for tier in (0, 1):
            self.assertNotEqual(frame_for("claude", tier),
                                frame_for("gemini", tier))


if __name__ == "__main__":
    unittest.main()
