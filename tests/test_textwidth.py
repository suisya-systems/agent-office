"""Unit tests for display-width measurement and cutting (issue #25).

The renderer's whole layout rests on these five functions, so they are pinned
here directly rather than only through the frames they produce.
"""

import unittest

from office import textwidth as tw

# "henkou" - four full-width characters, eight columns, and the exact shape of
# the bug: len() says 4, the terminal draws 8.
CJK = "変更反映"
# "e" + COMBINING ACUTE ACCENT: two characters, one column, one glyph.
COMBINING = "é"


class CharWidthTest(unittest.TestCase):
    def test_ascii_is_one(self):
        for ch in "aZ0 -_/":
            self.assertEqual(tw.char_width(ch), 1, ch)

    def test_east_asian_wide_and_fullwidth_are_two(self):
        for ch in ("変",           # W: CJK ideograph
                   "あ",           # W: hiragana
                   "Ａ",           # F: fullwidth latin A
                   "、"):          # W: ideographic comma
            self.assertEqual(tw.char_width(ch), 2, repr(ch))

    def test_combining_marks_are_zero(self):
        self.assertEqual(tw.char_width("́"), 0)   # Mn
        self.assertEqual(tw.char_width("⃣"), 0)   # Me
        self.assertEqual(tw.char_width("​"), 0)   # Cf: zero-width space

    def test_ambiguous_is_treated_as_one(self):
        """A deliberate choice, not an oversight - see the module docstring."""
        self.assertEqual(tw.char_width("±"), 1)   # PLUS-MINUS, EAW 'A'


class WidthTest(unittest.TestCase):
    def test_ascii_matches_len(self):
        self.assertEqual(tw.width("working"), len("working"))

    def test_cjk_is_twice_its_character_count(self):
        self.assertEqual(len(CJK), 4)
        self.assertEqual(tw.width(CJK), 8)

    def test_mixed(self):
        self.assertEqual(tw.width("ab" + CJK + "c"), 2 + 8 + 1)

    def test_combining_mark_costs_nothing(self):
        self.assertEqual(tw.width(COMBINING), 1)

    def test_empty(self):
        self.assertEqual(tw.width(""), 0)


class TruncateTest(unittest.TestCase):
    def test_shorter_than_the_limit_is_untouched(self):
        self.assertEqual(tw.truncate("abc", 10), "abc")
        self.assertEqual(tw.truncate(CJK, 10), CJK)

    def test_ascii_cut(self):
        self.assertEqual(tw.truncate("abcdef", 3), "abc")

    def test_a_full_width_character_is_never_split(self):
        """The core of the fix: landing mid-character drops it whole.

        Cutting CJK to an odd budget must give back the even prefix, one column
        short, rather than a half character that draws one column over.
        """
        self.assertEqual(tw.truncate(CJK, 5), CJK[:2])
        self.assertLessEqual(tw.width(tw.truncate(CJK, 5)), 5)

    def test_every_budget_stays_within_itself(self):
        for text in ("abcdef", CJK, "ab" + CJK, CJK + "ab", COMBINING + CJK):
            for limit in range(0, 14):
                cut = tw.truncate(text, limit)
                self.assertLessEqual(tw.width(cut), limit,
                                     "%r at %d -> %r" % (text, limit, cut))
                self.assertTrue(text.startswith(cut))

    def test_zero_and_negative_limits(self):
        self.assertEqual(tw.truncate("abc", 0), "")
        self.assertEqual(tw.truncate("abc", -1), "")

    def test_a_combining_mark_stays_with_its_base_character(self):
        # Budget 1 fits the "e"; the accent is free and must come along, or it
        # would be orphaned onto whatever the next chunk starts with.
        self.assertEqual(tw.truncate(COMBINING + "x", 1), COMBINING)


class ClusterTest(unittest.TestCase):
    """Characters that bind to the one before them and must not be split."""

    def test_an_emoji_presentation_selector_widens_its_base(self):
        # U+26A0 alone draws in one cell; with U+FE0F after it, two. Counting
        # the selector as a zero-width mark under-measured it by a column.
        self.assertEqual(tw.width("⚠"), 1)
        self.assertEqual(tw.width("⚠️"), 2)
        self.assertEqual(tw.width("▶️"), 2)
        self.assertEqual(tw.width("⚙️"), 2)

    def test_a_text_presentation_selector_narrows_its_base(self):
        self.assertEqual(tw.width("▶︎"), 1)

    def test_a_selector_is_never_separated_from_its_base(self):
        self.assertEqual(tw.truncate("⚠️abc", 1), "")
        self.assertEqual(tw.truncate("⚠️abc", 2), "⚠️")
        self.assertEqual(tw.truncate("⚠️abc", 3), "⚠️a")

    def test_a_joined_sequence_stays_whole(self):
        family = "👨‍💻"                              # man + ZWJ + computer
        self.assertEqual(len(family), 3)
        # Charged as both bases: over-measuring cuts a column early, which is
        # the safe way to be wrong about a box.
        self.assertEqual(tw.width(family), 4)
        # Never cut so as to leave the joiner dangling.
        for limit in range(0, 5):
            cut = tw.truncate(family, limit)
            self.assertNotIn("‍", cut[-1:], "limit %d -> %r" % (limit, cut))
            self.assertIn(cut, ("", family), "limit %d -> %r" % (limit, cut))

    def test_a_skin_tone_modifier_rides_with_its_base(self):
        thumb = "👍🏽"
        for limit in range(0, 4):
            cut = tw.truncate(thumb, limit)
            self.assertIn(cut, ("", thumb), "limit %d -> %r" % (limit, cut))

    def test_clusters_never_push_a_line_over_its_budget(self):
        for text in ("⚠️⚠️⚠️", "a⚠️b", "👨‍💻x", "👍🏽👍🏽", "é⚠️変"):
            for limit in range(0, 12):
                self.assertLessEqual(tw.width(tw.truncate(text, limit)), limit,
                                     "%r at %d" % (text, limit))


class PadCenterTest(unittest.TestCase):
    def test_pad_reaches_exactly_the_limit(self):
        for text in ("ab", CJK, "", COMBINING, "abcdefghij"):
            self.assertEqual(tw.width(tw.pad(text, 8)), 8, repr(text))

    def test_pad_truncates_overlong_input(self):
        self.assertEqual(tw.pad(CJK, 4), CJK[:2])

    def test_center_reaches_exactly_the_limit(self):
        for text in ("ab", "abc", CJK, "", "あbc"):
            self.assertEqual(tw.width(tw.center(text, 9)), 9, repr(text))

    def test_center_places_full_width_text_by_column(self):
        # 8 columns of CJK in a 16-column plate: 4 spaces either side. Counting
        # characters would have given 6 and pushed the text off centre.
        self.assertEqual(tw.center(CJK, 16), "    " + CJK + "    ")

    def test_center_gives_an_odd_column_of_slack_to_the_right(self):
        self.assertEqual(tw.center("ab", 5), " ab  ")


class WrapTest(unittest.TestCase):
    def test_always_returns_exactly_the_requested_line_count(self):
        for text in ("", "a", CJK, CJK * 4, "abcdefghijklmnop"):
            for lines in (1, 2, 3):
                self.assertEqual(len(tw.wrap(text, 8, lines)), lines,
                                 "%r x%d" % (text, lines))

    def test_short_text_leaves_the_later_lines_blank(self):
        self.assertEqual(tw.wrap("abc", 8, 2), ["abc", ""])

    def test_wraps_full_width_text_on_columns(self):
        # 8 CJK characters = 16 columns; an 8-column plate takes 4 per row.
        text = CJK * 2
        self.assertEqual(tw.wrap(text, 8, 2), [CJK, CJK])

    def test_one_line_is_a_plain_truncation(self):
        self.assertEqual(tw.wrap(CJK * 2, 8, 1), [CJK])

    def test_overflow_past_the_last_line_is_dropped(self):
        self.assertEqual(tw.wrap("abcdefghi", 4, 2), ["abcd", "efgh"])

    def test_no_line_ever_exceeds_the_limit(self):
        for text in ("abcdefghi", CJK * 5, "ab" + CJK * 3, COMBINING * 9):
            for part in tw.wrap(text, 7, 3):
                self.assertLessEqual(tw.width(part), 7, repr(part))

    def test_a_space_at_the_break_does_not_shift_the_next_line(self):
        self.assertEqual(tw.wrap("abcdef ghi", 6, 2), ["abcdef", "ghi"])

    def test_an_ideographic_space_at_the_break_is_stripped_too(self):
        """U+3000 is the space a Japanese title actually contains, and it is
        two columns wide - left in place it would push the whole continuation
        row off centre."""
        self.assertEqual(tw.wrap("変更　確認", 4, 2), ["変更", "確認"])

    def test_an_impossible_limit_does_not_spin(self):
        self.assertEqual(tw.wrap("abc", 0, 2), ["", ""])


if __name__ == "__main__":
    unittest.main()
