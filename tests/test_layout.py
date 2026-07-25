"""Tests for the layout model and the cursor movement built on it (#27).

The bug these pin down: vertical movement used to be `idx + dy * per_row` over
the flat `ordered_desks()` order, which is only the drawn grid when there is
exactly one workspace. Every island adds a room label and a blank row and
re-starts the row wrap, so with two workspaces on screen `down` landed on a
desk that was neither below the cursor nor in the room it looked like it was
in. The cases below are the five the design review asked for - two workspaces,
the island boundary, width sensitivity, scrolling, and a layout that went stale
between the last draw and the key - plus the model's own edges.
"""

import re
import unittest

from office.config import Config
from office.layout import MODE_COMPACT, MODE_FULL, build_compact, build_full
from office.office import Office
from office.renderer import MIN_COLS, Renderer
from office.state import OfficeState


def state_with(*sizes, rooms=True):
    """A state with one workspace per entry in `sizes`, that many desks each.

    Pane ids sort as `w<n>:p<i>` with i zero-padded, so `ordered_desks()` order
    is the obvious one and a test can name the desk it expects.
    """
    s = OfficeState()
    for w, count in enumerate(sizes, start=1):
        for i in range(count):
            s.ingest_pane({"pane_id": "w%d:p%02d" % (w, i),
                           "workspace_id": "w%d" % w,
                           "tab_id": "w%d:t1" % w,
                           "agent": "claude",
                           "display_agent": "w%dp%02d" % (w, i),
                           "agent_status": "working"})
        if rooms:
            s.set_room_label("w%d" % w, "room-%d" % w)
    return s


def rows_of(layout):
    """The layout as plain pane ids, one list per visual row."""
    return [[d.pane_id for d in row] for row in layout.rows]


class BuildTest(unittest.TestCase):
    def test_full_groups_by_island_and_wraps(self):
        lay = build_full(state_with(8, 5).islands(), per_row=6)
        self.assertEqual(lay.mode, MODE_FULL)
        self.assertEqual([g.workspace_id for g in lay.groups], ["w1", "w2"])
        self.assertEqual([g.label for g in lay.groups], ["room-1", "room-2"])
        self.assertEqual(rows_of(lay), [
            ["w1:p00", "w1:p01", "w1:p02", "w1:p03", "w1:p04", "w1:p05"],
            ["w1:p06", "w1:p07"],
            ["w2:p00", "w2:p01", "w2:p02", "w2:p03", "w2:p04"],
        ])

    def test_each_island_restarts_the_row_wrap(self):
        # The whole point: w2 starts a fresh row even though w1 left one
        # half-empty. A flat grid would have packed them together.
        lay = build_full(state_with(2, 2).islands(), per_row=3)
        self.assertEqual(rows_of(lay), [["w1:p00", "w1:p01"],
                                        ["w2:p00", "w2:p01"]])

    def test_per_row_is_floored_at_one(self):
        lay = build_full(state_with(3).islands(), per_row=0)
        self.assertEqual(rows_of(lay), [["w1:p00"], ["w1:p01"], ["w1:p02"]])

    def test_compact_is_one_desk_per_row(self):
        lay = build_compact(state_with(3).ordered_desks())
        self.assertEqual(lay.mode, MODE_COMPACT)
        self.assertEqual(rows_of(lay), [["w1:p00"], ["w1:p01"], ["w1:p02"]])

    def test_empty_layouts_are_falsy(self):
        self.assertFalse(build_full(OfficeState().islands(), per_row=6))
        self.assertFalse(build_compact(OfficeState().ordered_desks()))


class VerticalMoveTest(unittest.TestCase):
    """w1 wraps to two rows of 6+2, w2 is a single row of 5, per_row = 6."""

    def setUp(self):
        self.lay = build_full(state_with(8, 5).islands(), per_row=6)

    def down(self, pane_id):
        return self.lay.move(pane_id, 0, 1)

    def up(self, pane_id):
        return self.lay.move(pane_id, 0, -1)

    def test_down_within_an_island(self):
        self.assertEqual(self.down("w1:p00"), "w1:p06")

    def test_down_across_the_island_boundary(self):
        # The regression. Flat index of w1:p06 is 6; the old scalar added
        # per_row and landed on flat 12 = w2:p04 - last desk of the next room,
        # four columns to the right of the cursor.
        self.assertEqual(self.down("w1:p06"), "w2:p00")
        self.assertEqual(self.down("w1:p07"), "w2:p01")

    def test_up_across_the_island_boundary(self):
        self.assertEqual(self.up("w2:p00"), "w1:p06")

    def test_column_clamps_to_a_shorter_row(self):
        # Nothing is drawn under w2:p03, so the cursor takes the rightmost
        # desk of the row above rather than falling off it.
        self.assertEqual(self.up("w2:p03"), "w1:p07")
        self.assertEqual(self.down("w1:p03"), "w1:p07")

    def test_vertical_clamps_at_both_ends(self):
        self.assertEqual(self.up("w1:p00"), "w1:p00")
        self.assertEqual(self.down("w2:p04"), "w2:p04")

    def test_multi_row_step_clamps_rather_than_wrapping(self):
        self.assertEqual(self.lay.move("w1:p00", 0, 99), "w2:p00")
        self.assertEqual(self.lay.move("w2:p04", 0, -99), "w1:p04")


class HorizontalMoveTest(unittest.TestCase):
    def setUp(self):
        self.lay = build_full(state_with(8, 5).islands(), per_row=6)

    def test_right_walks_reading_order(self):
        self.assertEqual(self.lay.move("w1:p00", 1, 0), "w1:p01")

    def test_right_continues_onto_the_next_row(self):
        self.assertEqual(self.lay.move("w1:p05", 1, 0), "w1:p06")

    def test_right_crosses_the_island_boundary(self):
        self.assertEqual(self.lay.move("w1:p07", 1, 0), "w2:p00")
        self.assertEqual(self.lay.move("w2:p00", -1, 0), "w1:p07")

    def test_horizontal_clamps_at_both_ends(self):
        self.assertEqual(self.lay.move("w1:p00", -5, 0), "w1:p00")
        self.assertEqual(self.lay.move("w2:p04", 5, 0), "w2:p04")


class MoveEdgeTest(unittest.TestCase):
    def test_empty_layout_has_nowhere_to_go(self):
        lay = build_full(OfficeState().islands(), per_row=6)
        self.assertIsNone(lay.move(None, 0, 1))
        self.assertIsNone(lay.move("gone", 1, 0))

    def test_unknown_pane_lands_on_the_first_desk(self):
        lay = build_full(state_with(4).islands(), per_row=6)
        self.assertEqual(lay.move(None, 0, 1), "w1:p00")
        self.assertEqual(lay.move("w9:p99", 1, 0), "w1:p00")

    def test_no_step_stays_put(self):
        lay = build_full(state_with(4).islands(), per_row=6)
        self.assertEqual(lay.move("w1:p02", 0, 0), "w1:p02")

    def test_position_reports_the_cell(self):
        lay = build_full(state_with(8, 5).islands(), per_row=6)
        self.assertEqual(lay.position("w1:p07"), (1, 1))
        self.assertEqual(lay.position("w2:p00"), (2, 0))
        self.assertIsNone(lay.position("nope"))

    def test_compact_moves_one_desk_per_step_on_both_axes(self):
        lay = build_compact(state_with(3, 3).ordered_desks())
        self.assertEqual(lay.move("w1:p01", 0, 1), "w1:p02")
        self.assertEqual(lay.move("w1:p02", 0, 1), "w2:p00")
        self.assertEqual(lay.move("w1:p01", 1, 0), "w1:p02")
        self.assertEqual(lay.move("w1:p00", 0, -1), "w1:p00")


class RendererLayoutTest(unittest.TestCase):
    """`Renderer.layout` must describe the frame `Renderer.render` draws."""

    def setUp(self):
        self.r = Renderer(tier=1)

    def test_width_decides_the_wrap(self):
        s = state_with(8)
        # block_w + 1 == 19 columns per desk: 4 across at MIN_COLS, 6 at 120.
        self.assertEqual(self.r.per_row(80), 4)
        self.assertEqual(self.r.per_row(120), 6)
        self.assertEqual([len(row) for row in self.r.layout(s, 80, 40).rows],
                         [4, 4])
        self.assertEqual([len(row) for row in self.r.layout(s, 120, 40).rows],
                         [6, 2])

    def test_below_min_cols_is_compact(self):
        s = state_with(8)
        self.assertTrue(self.r.is_compact(79, 40))
        self.assertFalse(self.r.is_compact(80, 40))
        self.assertEqual(self.r.layout(s, 79, 40).mode, MODE_COMPACT)
        self.assertEqual(self.r.layout(s, 80, 40).mode, MODE_FULL)

    def test_below_min_rows_is_compact(self):
        s = state_with(8)
        self.assertEqual(self.r.layout(s, 120, 23).mode, MODE_COMPACT)
        self.assertEqual(self.r.layout(s, 120, 24).mode, MODE_FULL)

    def test_a_pane_narrower_than_one_desk_still_answers(self):
        # Under MIN_FRAME_COLS the frame is composed at the floor, and the
        # layout has to be built for that same floored width or the cursor
        # would move over a grid nobody is looking at.
        s = state_with(4)
        for cols in (1, 10, 19, 20):
            lay = self.r.layout(s, cols, 5)
            self.assertEqual(lay.mode, MODE_COMPACT)
            self.assertEqual([len(row) for row in lay.rows], [1, 1, 1, 1])
        self.assertEqual(self.r.per_row(1), self.r.per_row(20))

    def test_the_desk_width_disjunct_is_defensive_only(self):
        """`block_w + 1 > cols` cannot fire at any shipped desk width.

        Stated rather than asserted-around, because the clause reads as if it
        were the narrow-pane guard and it is not: cols is floored at 20 and
        MIN_COLS is 80, so `cols < MIN_COLS` has already fired by the time a
        desk block 80 columns wide would be needed to reach it. It earns its
        place only if desk_w ever becomes configurable.
        """
        for tier in (0, 1):
            r = Renderer(tier=tier)
            self.assertLess(r.block_w + 1, MIN_COLS)
            # The fallback below MIN_COLS is real all the same, on every tier.
            self.assertTrue(r.is_compact(MIN_COLS - 1, 40))
            self.assertFalse(r.is_compact(MIN_COLS, 40))

    def test_layout_rows_match_the_drawn_rows(self):
        """The invariant that makes the fix a fix: desks the layout puts on
        one row are drawn on one frame line, and rows run top to bottom.

        Swept over both tiers and several widths, because `per_row` depends on
        `block_w` and `block_w` is tier-specific: an invariant checked at one
        size and one tier would not notice the two coming apart.
        """
        s = state_with(8, 5)
        for tier in (0, 1):
            r = Renderer(tier=tier)
            for cols, rows in ((80, 60), (100, 60), (120, 60), (200, 60)):
                with self.subTest(tier=tier, cols=cols):
                    lay = r.layout(s, cols, rows)
                    self.assertEqual(lay.mode, MODE_FULL)
                    self._assert_rows_drawn(lay, r.render(s, cols, rows))

    def test_compact_rows_match_the_drawn_rows(self):
        """The same invariant for the other view mode - which is the half the
        old scalar never modelled at all."""
        s = state_with(8, 5)
        lay = self.r.layout(s, 60, 60)
        self.assertEqual(lay.mode, MODE_COMPACT)
        self._assert_rows_drawn(lay, self.r.render(s, 60, 60))

    def _assert_rows_drawn(self, lay, frame):
        lines = frame.split("\r\n")
        firsts = []
        for row in lay.rows:
            hits = [self._line_of(lines, d) for d in row]
            self.assertNotIn(None, hits, row[0].pane_id)
            self.assertEqual(len(set(hits)), 1, row[0].pane_id)
            firsts.append(hits[0])
        self.assertEqual(firsts, sorted(firsts))
        self.assertEqual(len(set(firsts)), len(firsts))

    @staticmethod
    def _line_of(lines, desk):
        for i, line in enumerate(lines):
            if desk.display_name in line:
                return i
        return None


class _Screen:
    """A screen stand-in whose size the test can change between calls."""

    def __init__(self, cols=120, rows=40):
        self.cols, self.rows = cols, rows
        self.frames = []

    def size(self):
        return self.cols, self.rows

    def write(self, frame):
        self.frames.append(frame)


def make_office(state, screen):
    office = Office("/nonexistent.sock", "self-pane", tier=1, truecolor=True,
                    config=Config())
    office.state = state
    office.screen = screen
    return office


class OfficeMoveTest(unittest.TestCase):
    """The keys, end to end through the office loop's dispatch."""

    def test_down_crosses_islands_by_layout_not_by_flat_offset(self):
        s = state_with(8, 5)
        office = make_office(s, _Screen(120, 40))
        s.select("w1:p06")
        office._handle(("key", "down"))
        self.assertEqual(s.selected_pane_id, "w2:p00")   # was w2:p04

    def test_vim_keys_agree_with_the_arrows(self):
        s = state_with(8, 5)
        office = make_office(s, _Screen(120, 40))
        s.select("w1:p06")
        office._handle(("key", "j"))
        self.assertEqual(s.selected_pane_id, "w2:p00")
        office._handle(("key", "k"))
        self.assertEqual(s.selected_pane_id, "w1:p06")
        office._handle(("key", "l"))
        self.assertEqual(s.selected_pane_id, "w1:p07")
        office._handle(("key", "h"))
        self.assertEqual(s.selected_pane_id, "w1:p06")

    def test_movement_on_an_empty_office_is_a_no_op(self):
        s = OfficeState()
        office = make_office(s, _Screen(120, 40))
        office._handle(("key", "down"))
        self.assertIsNone(s.selected_pane_id)

    def test_a_resize_between_draw_and_key_moves_over_the_new_layout(self):
        """Stale layout, geometry half: the last frame was a 6-wide grid, the
        pane is now too narrow for the full view, and the key has to follow
        the compact layout the *next* frame will have."""
        s = state_with(8, 5)
        screen = _Screen(120, 40)
        office = make_office(s, screen)
        office._draw()                                # full: rows of 6
        s.select("w1:p00")
        screen.cols, screen.rows = 60, 20             # -> compact
        office._handle(("key", "down"))
        self.assertEqual(s.selected_pane_id, "w1:p01")

    def test_a_narrower_pane_wraps_the_cursor_differently(self):
        """Width sensitivity, on a fleet where it is also visible.

        Two workspaces of 6 and 5. At 120 columns room-one fits on one row, so
        `down` leaves the island; at 80 it wraps to 4 + 2, so the same key
        stays inside it - and the step from room-one's second row is where the
        old flat formula went wrong (it reached w2:p02, two desks along).
        """
        s = state_with(6, 5)
        screen = _Screen(120, 40)
        office = make_office(s, screen)

        s.select("w1:p00")
        office._handle(("key", "down"))               # per_row 6: one row
        self.assertEqual(s.selected_pane_id, "w2:p00")

        screen.cols = 80                              # per_row 6 -> 4
        s.select("w1:p00")
        office._handle(("key", "down"))
        self.assertEqual(s.selected_pane_id, "w1:p04")
        office._handle(("key", "down"))
        self.assertEqual(s.selected_pane_id, "w2:p00")   # was w2:p02

    def test_desks_closing_between_draw_and_key_re_wrap_the_cursor(self):
        """Stale layout, data half: closing desks re-wraps the rows.

        The office drew room-one as 6 + 2; four of its desks then close, so
        room-one is now a single row of 4 and w1:p07 has moved from column 1 of
        the second row to column 3 of the first. `down` has to follow the
        layout as it stands (w2:p03) - a remembered one would still believe
        column 1 and land on w2:p01, which is a desk that exists, so nothing
        downstream would catch it.
        """
        s = state_with(8, 5)
        office = make_office(s, _Screen(120, 40))
        office._draw()
        for pane_id in ("w1:p00", "w1:p01", "w1:p02", "w1:p03"):
            s.remove_pane(pane_id)
        s.select("w1:p07")
        office._handle(("key", "down"))
        self.assertEqual(s.selected_pane_id, "w2:p03")   # not w2:p01

    def test_the_cursor_is_never_left_on_a_closed_desk(self):
        s = state_with(8, 5)
        office = make_office(s, _Screen(120, 40))
        office._draw()
        s.select("w2:p04")
        for pane_id in list(s.desks):
            if pane_id not in ("w1:p00", "w1:p01", "w1:p02"):
                s.remove_pane(pane_id)
        # _fix_selection already re-homed the cursor; the key must still land
        # on a desk that exists rather than off the end of the old grid.
        office._handle(("key", "right"))
        self.assertEqual(s.selected_pane_id, "w1:p01")
        self.assertIn(s.selected_pane_id, s.desks)

    def test_movement_under_the_help_overlay_uses_the_office_layout(self):
        s = state_with(8, 5)
        office = make_office(s, _Screen(120, 40))
        office.show_help = True
        s.select("w1:p06")
        office._handle(("key", "down"))
        self.assertEqual(s.selected_pane_id, "w2:p00")


SCROLL_HINT = re.compile(r"\(scroll: (\d+)-(\d+) of (\d+)\)")
ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


class ScrollMoveTest(unittest.TestCase):
    """Movement is in unscrolled body coordinates, and the frame follows it.

    The office here is 20 desks over two rooms at per_row 6: 44 body lines
    against 38 visible, so it is genuinely scrolling and a cursor that moved
    by visible-frame row instead of layout row would drift.
    """

    def _office(self):
        s = state_with(14, 6)
        screen = _Screen(120, 40)
        return make_office(s, screen), s, screen

    @staticmethod
    def _scroll_top(frame):
        """First body line on screen, 1-based, from the header's scroll hint."""
        match = SCROLL_HINT.search(ANSI.sub("", frame))
        return int(match.group(1)) if match else 1

    def test_scrolled_office_still_moves_by_layout_row(self):
        office, s, _ = self._office()
        s.select("w1:p00")
        # w1 wraps to rows p00-05 / p06-11 / p12-13, then room 2 starts.
        office._handle(("key", "down"))
        self.assertEqual(s.selected_pane_id, "w1:p06")
        office._handle(("key", "down"))
        self.assertEqual(s.selected_pane_id, "w1:p12")
        office._handle(("key", "down"))               # into room 2
        self.assertEqual(s.selected_pane_id, "w2:p00")

    def test_the_selection_is_visible_after_the_next_draw(self):
        office, s, screen = self._office()
        s.select("w1:p00")
        seen = []
        for _ in range(5):
            office._handle(("key", "down"))
            office._draw()
            desk = s.selected_desk()
            seen.append(desk.pane_id)
            self.assertIn(desk.display_name, screen.frames[-1],
                          "%s scrolled out of view" % desk.pane_id)
        self.assertEqual(seen[-1], "w2:p00")

    def test_moving_back_up_scrolls_the_office_back(self):
        office, s, screen = self._office()
        s.select("w2:p05")
        office._draw()
        bottom_top = self._scroll_top(screen.frames[-1])
        self.assertGreater(bottom_top, 1)             # really scrolled down
        for _ in range(3):
            office._handle(("key", "up"))
        office._draw()
        # The column clamps to the short row (p12/p13) on the way up and stays
        # there - there is no goal column - so the cursor lands on p01, not p05.
        self.assertEqual(s.selected_pane_id, "w1:p01")
        self.assertEqual(self._scroll_top(screen.frames[-1]), 1)
        self.assertIn("w1p01", ANSI.sub("", screen.frames[-1]))
