"""Layout - where every desk sits in the frame, as a value two callers share.

Cursor movement is a question about *visual adjacency*: "down" means the desk
drawn underneath the selected one. That used to be answered with a scalar -
`idx + dy * per_row` over the flat `ordered_desks()` order - which quietly
assumed the office was one uniform grid. It is not: the full view groups desks
by workspace and starts each island with a room label and ends it with a blank
row, so a workspace whose desk count is not a multiple of `per_row` shifts
every island below it out of step with the flat index (issue #27). With two
workspaces on screen, `down` landed on a desk that was neither below the cursor
nor in the same room.

So adjacency is modelled rather than computed. `build_full` / `build_compact`
turn a state snapshot into the same rows of desks the renderer draws, and the
result answers `move()` directly. Two consequences worth stating:

- **One source of truth.** `Renderer.layout()` builds the layout and `_full` /
  `_compact` draw *from it*, so the grid the cursor moves over and the grid on
  screen cannot drift apart. Fixing one scalar in two places is what created
  the bug.
- **Body coordinates, not screen coordinates.** Rows here are unscrolled: row
  0 is the first row of the first island whether or not it is currently in
  view. Movement near the top or bottom of a scrolled office therefore follows
  the layout, not the viewport, and the renderer scrolls afterwards to bring
  the new selection back into frame.

Both view modes produce a Layout, so the input path never branches on which
one is on screen. Compact is simply a layout one desk wide: vertical movement
is +/-1 row because every row holds exactly one desk, and horizontal movement
is +/-1 desk for the same reason.
"""

MODE_FULL = "full"
MODE_COMPACT = "compact"


class Group:
    """One island - a workspace's label and the rows its desks wrap onto.

    The renderer needs the label to draw the `[ room ]` header; navigation
    needs only the rows. Both come from the same object so a room cannot be
    drawn around a different set of desks than the cursor walks.
    """

    __slots__ = ("workspace_id", "label", "rows")

    def __init__(self, workspace_id, label, rows):
        self.workspace_id = workspace_id
        self.label = label
        self.rows = rows                      # list[list[Desk]]


class Layout:
    """Rows of desks in visual order, and the moves between them."""

    __slots__ = ("mode", "groups", "rows", "flat", "_cell", "_order")

    def __init__(self, mode, groups):
        self.mode = mode
        self.groups = list(groups)
        # Islands are consecutive on screen, so flattening the groups' rows
        # gives the visual row order directly - the label and blank rows
        # between them separate islands but hold no desks, and a cursor never
        # needs to stop on one.
        self.rows = [row for group in self.groups for row in group.rows]
        self.flat = [desk for row in self.rows for desk in row]
        self._cell = {}
        for r, row in enumerate(self.rows):
            for c, desk in enumerate(row):
                self._cell[desk.pane_id] = (r, c)
        self._order = {desk.pane_id: i for i, desk in enumerate(self.flat)}

    def __bool__(self):
        return bool(self.flat)

    def __len__(self):
        return len(self.flat)

    def position(self, pane_id):
        """(row, column) of a desk, or None when it is not in this layout."""
        return self._cell.get(pane_id)

    def move(self, pane_id, dx, dy):
        """The pane one step from `pane_id`, or None when there is nowhere.

        Vertical movement is by *visual row*: it steps to the adjacent row and
        keeps the column, clamped to that row's width when the row below is a
        short one (the last row of an island usually is). Because rows run
        across island boundaries, `down` from the last row of one workspace
        lands on the first row of the next - which is what it looks like on
        screen, room label and blank row notwithstanding.

        Horizontal movement is by reading order over the whole layout, which
        is the flat `ordered_desks()` order. Stepping off the end of a row
        therefore continues onto the next one rather than stopping, so left
        and right alone still reach every desk. Both axes clamp at the ends;
        nothing wraps around.

        An unknown `pane_id` - no selection yet, or a desk that vanished
        between the last draw and this key - lands on the first desk rather
        than doing nothing, so the cursor is never stranded off the layout.
        """
        if not self.flat:
            return None
        cell = self._cell.get(pane_id)
        if cell is None:
            return self.flat[0].pane_id
        row_idx, col_idx = cell
        if dy:
            row_idx = max(0, min(len(self.rows) - 1, row_idx + dy))
            row = self.rows[row_idx]
            pane_id = row[min(col_idx, len(row) - 1)].pane_id
        if dx:
            idx = self._order[pane_id] + dx
            pane_id = self.flat[max(0, min(len(self.flat) - 1, idx))].pane_id
        return pane_id


def build_full(islands, per_row):
    """The full view: one Group per workspace, desks wrapped `per_row` wide."""
    per_row = max(1, per_row)
    groups = []
    for workspace_id, label, desks in islands:
        rows = [desks[i:i + per_row] for i in range(0, len(desks), per_row)]
        groups.append(Group(workspace_id, label, rows))
    return Layout(MODE_FULL, groups)


def build_compact(desks):
    """The compact fallback: one desk per row, one island, no room labels.

    Rooms are named per row there rather than as headers, so there is no
    island structure to model - a single Group keeps the shape the renderer
    and `move()` both read.
    """
    return Layout(MODE_COMPACT, [Group(None, None, [[d] for d in desks])])
