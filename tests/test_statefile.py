"""Unit tests for state.json (design.md section 8) and the actions built on it."""

import json
import os
import tempfile
import unittest

from office import statefile
from office.cli import pick_blocked
from office.state import OfficeState


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def pane(pid, ws="w1", status="idle", agent="claude"):
    return {"pane_id": pid, "workspace_id": ws, "tab_id": ws + ":t1",
            "agent": agent, "agent_status": status}


class WriterTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, statefile.STATE_BASENAME)
        self.mono = Clock(1000.0)
        self.wall = Clock(1_700_000_000.0)
        self.state = OfficeState(now=self.mono)
        self.writer = statefile.StateWriter(self.path, office_pane_id="w1:p9",
                                            now=self.mono, wall=self.wall)

    def read(self):
        return statefile.read(self.path)

    def test_writes_and_reads_back(self):
        self.state.ingest_pane(pane("p1", status="blocked"))
        self.assertTrue(self.writer.maybe_write(self.state))
        data = self.read()
        self.assertEqual(data["version"], statefile.STATE_VERSION)
        self.assertTrue(data["running"])
        self.assertEqual(data["office_pane_id"], "w1:p9")
        self.assertEqual(len(data["desks"]), 1)
        desk = data["desks"][0]
        self.assertEqual(desk["pane_id"], "p1")
        self.assertEqual(desk["status"], "blocked")
        self.assertEqual(desk["blocked_since"], self.wall.t)

    def test_unchanged_desks_do_not_rewrite_until_the_interval(self):
        self.state.ingest_pane(pane("p1"))
        self.assertTrue(self.writer.maybe_write(self.state))
        self.mono.advance(1)
        self.assertFalse(self.writer.maybe_write(self.state))
        self.mono.advance(statefile.WRITE_INTERVAL_S)
        self.assertTrue(self.writer.maybe_write(self.state))

    def test_a_change_writes_immediately(self):
        self.state.ingest_pane(pane("p1"))
        self.writer.maybe_write(self.state)
        self.mono.advance(1)
        self.state.set_status("p1", "blocked")
        self.assertTrue(self.writer.maybe_write(self.state))
        self.assertEqual(self.read()["desks"][0]["status"], "blocked")

    def test_escalated_flag_is_recorded(self):
        self.state.ingest_pane(pane("p1", status="blocked"))
        self.writer.maybe_write(self.state, escalated={"p1"})
        self.assertTrue(self.read()["desks"][0]["escalated"])

    def test_workspace_label_is_recorded(self):
        self.state.ingest_pane(pane("p1"))
        self.state.set_room_label("w1", "room-one")
        self.writer.maybe_write(self.state)
        self.assertEqual(self.read()["desks"][0]["workspace_label"], "room-one")

    def test_write_stopped_drops_the_liveness_claim(self):
        self.state.ingest_pane(pane("p1", status="blocked"))
        self.writer.maybe_write(self.state)
        self.assertTrue(self.writer.write_stopped())
        data = self.read()
        self.assertFalse(data["running"])
        self.assertIsNone(data["office_pane_id"])
        self.assertEqual(len(data["desks"]), 1)      # data survives for jumps

    def test_unwritable_path_is_survivable(self):
        writer = statefile.StateWriter(os.path.join(self.dir.name, "f", "x", ""),
                                       now=self.mono, wall=self.wall)
        self.assertFalse(writer.maybe_write(self.state))

    def test_no_path_is_a_no_op(self):
        writer = statefile.StateWriter("")
        self.assertFalse(writer.maybe_write(self.state))
        self.assertFalse(writer.write_stopped())

    def test_blocked_desk_does_not_rewrite_on_every_tick(self):
        # regression: blocked_since was converted to an epoch inside the
        # change-detection snapshot, so two live clocks re-read per call made
        # every row differ by float jitter and rewrote the file every tick.
        writer = statefile.StateWriter(self.path)         # real clocks
        self.state.ingest_pane(pane("p1", status="blocked"))
        self.assertTrue(writer.maybe_write(self.state))
        for _ in range(5):
            self.assertFalse(writer.maybe_write(self.state))
        # and the value that lands on disk is still a wall-clock epoch
        self.assertGreater(self.read()["desks"][0]["blocked_since"], 0)

    def test_write_is_atomic_leaving_no_temp_files(self):
        self.state.ingest_pane(pane("p1"))
        self.writer.maybe_write(self.state)
        self.assertEqual(os.listdir(self.dir.name), [statefile.STATE_BASENAME])


class ReadTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, statefile.STATE_BASENAME)

    def write_raw(self, payload):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(payload if isinstance(payload, str)
                         else json.dumps(payload))

    def test_missing_file(self):
        self.assertIsNone(statefile.read(self.path))

    def test_no_path(self):
        self.assertIsNone(statefile.read(""))

    def test_corrupt_file(self):
        self.write_raw("{not json")
        self.assertIsNone(statefile.read(self.path))

    def test_wrong_version_is_rejected(self):
        self.write_raw({"version": 999, "desks": []})
        self.assertIsNone(statefile.read(self.path))

    def test_state_path_env(self):
        self.assertEqual(statefile.state_path({}), "")
        self.assertTrue(statefile.state_path(
            {"HERDR_PLUGIN_STATE_DIR": "/tmp/x"}).endswith("state.json"))


class BlockedSinceMapTest(unittest.TestCase):
    def data(self, blocked_since):
        return {"version": statefile.STATE_VERSION, "updated_at": 500.0,
                "desks": [{"pane_id": "p1", "blocked_since": blocked_since}]}

    def test_epoch_converts_back_to_monotonic(self):
        # written 60 wall-seconds ago -> 60 monotonic-seconds ago
        out = statefile.blocked_since_map(self.data(940.0), wall_now=1000.0,
                                          mono_now=5000.0)
        self.assertEqual(out["p1"], 4940.0)

    def test_future_timestamps_are_clamped_to_now(self):
        out = statefile.blocked_since_map(self.data(2000.0), wall_now=1000.0,
                                          mono_now=5000.0)
        self.assertEqual(out["p1"], 5000.0)

    def test_desks_without_blocked_since_are_skipped(self):
        self.assertEqual(statefile.blocked_since_map(self.data(None)), {})

    def test_empty_and_malformed_inputs(self):
        self.assertEqual(statefile.blocked_since_map(None), {})
        self.assertEqual(statefile.blocked_since_map({"desks": ["junk"]}), {})
        self.assertEqual(statefile.blocked_epoch_map(None), {})

    def test_epoch_map_keeps_raw_values(self):
        self.assertEqual(statefile.blocked_epoch_map(self.data(940.0)),
                         {"p1": 940.0})


class LiveOfficePaneTest(unittest.TestCase):
    def data(self, **kw):
        base = {"version": statefile.STATE_VERSION, "updated_at": 1000.0,
                "running": True, "office_pane_id": "w1:p9"}
        base.update(kw)
        return base

    def test_fresh_and_running(self):
        self.assertEqual(
            statefile.live_office_pane_id(self.data(), wall_now=1010.0),
            "w1:p9")

    def test_stale_is_not_trusted(self):
        self.assertIsNone(statefile.live_office_pane_id(
            self.data(), wall_now=1000.0 + statefile.FRESH_S + 1))

    def test_stopped_is_not_trusted(self):
        self.assertIsNone(statefile.live_office_pane_id(
            self.data(running=False), wall_now=1010.0))

    def test_missing_pieces(self):
        self.assertIsNone(statefile.live_office_pane_id(None))
        self.assertIsNone(statefile.live_office_pane_id(
            self.data(updated_at="soon"), wall_now=1010.0))
        self.assertIsNone(statefile.live_office_pane_id(
            self.data(office_pane_id=None), wall_now=1010.0))


class PickBlockedTest(unittest.TestCase):
    PANES = [pane("pB", status="blocked"), pane("pA", status="blocked"),
             pane("pC", status="working")]

    def test_no_blocked_panes(self):
        self.assertIsNone(pick_blocked([pane("p1", status="idle")]))

    def test_pane_id_tiebreak_without_state(self):
        self.assertEqual(pick_blocked(self.PANES), "pA")

    def test_recorded_blocked_since_wins(self):
        # pB has been blocked longer, even though pA sorts first by id
        self.assertEqual(pick_blocked(self.PANES, {"pB": 10.0, "pA": 20.0}),
                         "pB")

    def test_recorded_panes_outrank_unknown_ones(self):
        self.assertEqual(pick_blocked(self.PANES, {"pB": 999.0}), "pB")

    def test_stale_entries_for_unblocked_panes_are_ignored(self):
        self.assertEqual(pick_blocked(self.PANES, {"pC": 1.0}), "pA")


class SeedBlockedSinceTest(unittest.TestCase):
    def setUp(self):
        self.mono = Clock(1000.0)
        self.state = OfficeState(now=self.mono)

    def test_older_recorded_value_is_adopted(self):
        self.state.ingest_pane(pane("p1", status="blocked"))
        self.assertEqual(self.state.desks["p1"].blocked_since, 1000.0)
        self.state.seed_blocked_since({"p1": 400.0})
        self.assertEqual(self.state.desks["p1"].blocked_since, 400.0)

    def test_newer_recorded_value_is_ignored(self):
        self.state.ingest_pane(pane("p1", status="blocked"))
        self.state.seed_blocked_since({"p1": 2000.0})
        self.assertEqual(self.state.desks["p1"].blocked_since, 1000.0)

    def test_only_applies_to_currently_blocked_desks(self):
        self.state.ingest_pane(pane("p1", status="working"))
        self.state.seed_blocked_since({"p1": 400.0, "ghost": 400.0})
        self.assertIsNone(self.state.desks["p1"].blocked_since)
        self.assertNotIn("ghost", self.state.desks)

    def test_empty_input(self):
        self.state.seed_blocked_since(None)
        self.state.seed_blocked_since({})


class RoundTripTest(unittest.TestCase):
    """The restart path end to end: write, reopen, inherit blocked_since."""

    def test_blocked_desk_keeps_its_countdown_across_a_restart(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = os.path.join(directory.name, statefile.STATE_BASENAME)
        mono, wall = Clock(1000.0), Clock(1_700_000_000.0)

        first = OfficeState(now=mono)
        first.ingest_pane(pane("p1", status="blocked"))
        statefile.StateWriter(path, now=mono, wall=wall).maybe_write(first)

        mono.advance(30)                       # office pane closed, 30s pass
        wall.advance(30)
        later_mono = Clock(9000.0)             # new process: new monotonic base
        second = OfficeState(now=later_mono)
        second.ingest_pane(pane("p1", status="blocked"))
        self.assertEqual(second.desks["p1"].blocked_since, 9000.0)

        seed = statefile.blocked_since_map(statefile.read(path),
                                           wall_now=wall.t, mono_now=9000.0)
        second.seed_blocked_since(seed)
        # 30s of the countdown already elapsed before this process started
        self.assertEqual(second.desks["p1"].blocked_since, 8970.0)


if __name__ == "__main__":
    unittest.main()
