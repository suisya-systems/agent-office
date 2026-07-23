"""state.json - runtime state shared with the single-shot actions (section 8).

The office pane writes `HERDR_PLUGIN_STATE_DIR/state.json` every 10 seconds and
whenever the desks change. It exists so the stateless `action-jump-blocked` and
`action-open` commands (design.md section 6) can be exact instead of guessing:

  * `blocked_since` lets jump-blocked pick the genuinely longest-blocked agent
    rather than falling back to a pane_id tiebreak.
  * `office_pane_id` lets action-open focus the running office pane rather than
    matching on a label that herdr 0.7.4 does not put in `pane.list` at all.

Times are stored as **wall-clock epoch seconds**, not the monotonic clock the
rest of the office runs on, because the file outlives the process: a monotonic
value is meaningless to a later process. `blocked_since_map()` converts back on
read, which is how section 7's restart inheritance works - an agent that was
already blocked before the office pane opened keeps its original blocked_since
instead of restarting the 90s countdown.

Readers must tolerate a partially-written or ancient file, so every read is
defensive and the writer replaces the file atomically via os.replace.
"""

import json
import os
import time

STATE_VERSION = 1
STATE_BASENAME = "state.json"
WRITE_INTERVAL_S = 10.0
# Beyond this the office is not considered to be running: it stopped writing
# (crash, kill, clean exit), so its recorded pane_id may have been recycled and
# its blocked_since values are no longer being kept up to date.
FRESH_S = 60.0
# How long an office outage may last while blocked_since is still inherited.
# The file only proves a desk was blocked as of `updated_at`; anything after
# that is unobserved, so across a long gap an agent may well have unblocked and
# reblocked. Inheriting then would fire an "overdue" toast at a desk that just
# blocked. A short gap (reopening the pane) is the case section 7 cares about.
SEED_MAX_GAP_S = 300.0


def state_path(env=None):
    """Absolute path of the state file, or "" when no state dir is set."""
    env = os.environ if env is None else env
    directory = env.get("HERDR_PLUGIN_STATE_DIR")
    return os.path.join(directory, STATE_BASENAME) if directory else ""


# -- reading -------------------------------------------------------------

def read(path):
    """Return the parsed state file, or None if absent/unusable."""
    if not path:
        return None
    try:
        with open(path, "rb") as handle:
            data = json.loads(handle.read().decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
        return None
    return data


def age_s(data, wall_now=None):
    """Seconds since the file was last written, or None if unknowable."""
    if not data:
        return None
    updated_at = data.get("updated_at")
    if not isinstance(updated_at, (int, float)):
        return None
    wall_now = time.time() if wall_now is None else wall_now
    return wall_now - updated_at


def is_live(data, wall_now=None):
    """True while an office process is actively writing this file."""
    if not data or not data.get("running"):
        return False
    age = age_s(data, wall_now)
    return age is not None and age <= FRESH_S


def blocked_since_map(data, wall_now=None, mono_now=None,
                      max_age_s=SEED_MAX_GAP_S):
    """Recorded blocked_since values, converted back to the monotonic clock.

    Only desks that were blocked when the file was written have one. The result
    is clamped to the present so a clock jump can never place a desk's
    blocked_since in the future (which would read as a negative elapsed time).

    A file older than `max_age_s` yields nothing: see SEED_MAX_GAP_S for why a
    long unobserved gap makes these timestamps unsafe to inherit.
    """
    if not data:
        return {}
    wall_now = time.time() if wall_now is None else wall_now
    mono_now = time.monotonic() if mono_now is None else mono_now
    if max_age_s is not None:
        age = age_s(data, wall_now)
        if age is None or age > max_age_s:
            return {}
    out = {}
    for desk in data.get("desks") or []:
        if not isinstance(desk, dict):
            continue
        pane_id = desk.get("pane_id")
        blocked_since = desk.get("blocked_since")
        if not pane_id or not isinstance(blocked_since, (int, float)):
            continue
        out[pane_id] = min(mono_now, mono_now - (wall_now - blocked_since))
    return out


def blocked_epoch_map(data):
    """Recorded blocked_since values as raw epoch seconds (single-shot use)."""
    if not data:
        return {}
    out = {}
    for desk in data.get("desks") or []:
        if not isinstance(desk, dict):
            continue
        pane_id = desk.get("pane_id")
        blocked_since = desk.get("blocked_since")
        if pane_id and isinstance(blocked_since, (int, float)):
            out[pane_id] = float(blocked_since)
    return out


def live_office_pane_id(data, wall_now=None):
    """The running office pane's id, or None if there isn't a fresh one.

    A file whose `updated_at` has gone stale means the office pane stopped
    writing (crash, kill), so its pane_id is no longer trustworthy - herdr may
    have handed that id to something else since.
    """
    if not is_live(data, wall_now):
        return None
    return data.get("office_pane_id") or None


# -- writing -------------------------------------------------------------

class StateWriter:
    """Debounced, atomic writer for state.json (10s cadence + on change)."""

    def __init__(self, path, office_pane_id=None, interval_s=WRITE_INTERVAL_S,
                 now=None, wall=None):
        self.path = path
        self.office_pane_id = office_pane_id
        self.interval_s = interval_s
        self._now = now or time.monotonic
        self._wall = wall or time.time
        self._last_write = None
        self._last_desks = None

    def maybe_write(self, state, escalated=(), force=False) -> bool:
        """Write if the desks changed or the interval elapsed. Never raises."""
        if not self.path:
            return False
        desks = self._desk_rows(state, escalated)
        now = self._now()
        due = (self._last_write is None
               or now - self._last_write >= self.interval_s)
        if not force and not due and desks == self._last_desks:
            return False
        if self._write({
            "version": STATE_VERSION,
            "updated_at": self._wall(),
            "running": True,
            "office_pane_id": self.office_pane_id,
            "pid": os.getpid(),
            "desks": self._to_epoch_rows(desks),
        }):
            self._last_write = now
            self._last_desks = desks
            return True
        return False

    def write_stopped(self, state=None, escalated=()) -> bool:
        """Final write on shutdown: fresh data, minus the liveness claim.

        `state` must be passed whenever it is still available: reusing the last
        periodic snapshot would stamp up-to-10-second-old desks with a current
        `updated_at`, and a restart inside SEED_MAX_GAP_S would then inherit
        blocked_since values that no longer describe the fleet.
        """
        if not self.path:
            return False
        rows = (self._desk_rows(state, escalated) if state is not None
                else (self._last_desks or []))
        return self._write({
            "version": STATE_VERSION,
            "updated_at": self._wall(),
            "running": False,
            "office_pane_id": None,
            "pid": os.getpid(),
            "desks": self._to_epoch_rows(rows),
        })

    # -- internals ---------------------------------------------------

    def _desk_rows(self, state, escalated):
        """Snapshot rows used for change detection.

        blocked_since stays on the *monotonic* clock here. Converting it to an
        epoch at this point would re-read two independent clocks on every call,
        and their sub-microsecond drift would make every row compare unequal -
        rewriting state.json on every animation tick instead of every 10s.
        """
        escalated = set(escalated)
        rows = []
        for desk in state.ordered_desks():
            rows.append({
                "pane_id": desk.pane_id,
                "workspace_id": desk.workspace_id,
                "workspace_label": state.rooms.get(desk.workspace_id,
                                                   desk.workspace_id),
                "display_name": desk.display_name,
                "agent": desk.agent,
                "status": desk.status,
                "blocked_since": desk.blocked_since,      # monotonic
                "escalated": desk.pane_id in escalated,
            })
        return rows

    def _to_epoch_rows(self, rows):
        """Same rows with blocked_since moved onto the wall clock, for disk."""
        wall_now, mono_now = self._wall(), self._now()
        return [dict(row,
                     blocked_since=_to_epoch(row["blocked_since"], wall_now,
                                             mono_now))
                for row in rows]

    def _write(self, payload) -> bool:
        tmp = "%s.tmp.%d" % (self.path, os.getpid())
        try:
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
                handle.write("\n")
            os.replace(tmp, self.path)
            return True
        except OSError:
            # A missing/unwritable state dir must never take the office down;
            # the file is a convenience for the single-shot actions.
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return False


def _to_epoch(monotonic_value, wall_now, mono_now):
    if monotonic_value is None:
        return None
    return wall_now - (mono_now - monotonic_value)
