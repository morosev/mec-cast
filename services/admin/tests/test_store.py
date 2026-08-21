"""Run identity and the on-disk store."""

from __future__ import annotations

import json
import uuid

from mec_cast_admin.state import RunState
from mec_cast_admin.store import JsonRunStore, Run, uuid7


class TestUuid7:
    def test_it_is_a_valid_uuid_of_version_7(self):
        parsed = uuid.UUID(uuid7())
        assert parsed.version == 7
        assert parsed.variant == uuid.RFC_4122

    def test_ids_sort_chronologically(self):
        # The whole reason for v7 over v4: `ls runs/` and the table agree with
        # the clock without a separate timestamp.
        ids = [uuid7() for _ in range(50)]
        assert ids == sorted(ids)

    def test_ids_minted_in_one_millisecond_still_sort(self):
        # A tight loop lands many ids in the same millisecond, where only the
        # RFC 9562 counter keeps them ordered.
        import time as _time

        start = _time.time_ns() // 1_000_000
        ids = [uuid7() for _ in range(200)]
        assert _time.time_ns() // 1_000_000 - start <= 2, "loop was too slow to prove anything"
        assert ids == sorted(ids)

    def test_ids_are_unique(self):
        assert len({uuid7() for _ in range(1000)}) == 1000

    def test_it_stays_parseable_by_the_publisher_trace_id_helper(self):
        # run_trace_id() in publisher_node.py does uuid.UUID(run_id).bytes.
        assert len(uuid.UUID(uuid7()).bytes) == 16


class TestManifest:
    def test_round_trips_through_disk(self, tmp_path):
        store = JsonRunStore(tmp_path)
        run = Run(run_id=uuid7(), seq=1, label="baseline", params={"rate_hz": 10.0})
        run.state = RunState.RUNNING
        store.save(run)

        loaded = store.load()[run.run_id]
        assert loaded.label == "baseline"
        assert loaded.state is RunState.RUNNING
        assert loaded.params["rate_hz"] == 10.0

    def test_a_manifest_from_the_experiment_script_still_loads(self, tmp_path):
        # The script writes no `state`, `seq` or `params`; it describes a run
        # that already happened. Two writers share this file by design.
        run_id = uuid7()
        directory = tmp_path / run_id
        directory.mkdir()
        (directory / "run.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "tag": "baseline",
                    "started_utc": "2026-08-18T09:00:00Z",
                    "host": "devbox",
                    "duration_s": 60,
                    "workload": {
                        "num_points": 30000,
                        "rate_hz": 10.0,
                        "seed": 42,
                        "modality": "pointcloud",
                    },
                    "transport": "rmw_zenoh_cpp",
                }
            )
        )
        loaded = JsonRunStore(tmp_path).load()[run_id]
        assert loaded.source == "script"
        assert loaded.state is RunState.STOPPED
        assert loaded.label == "baseline"
        assert loaded.params["num_points"] == 30000

    def test_the_admin_manifest_keeps_the_scripts_shape(self, tmp_path):
        # Anything reading the script's fields must still work on ours.
        store = JsonRunStore(tmp_path)
        run = Run(run_id=uuid7(), seq=2, label="t", params={"num_points": 1, "rate_hz": 2.0})
        store.save(run)
        raw = json.loads((tmp_path / run.run_id / "run.json").read_text())
        for key in ("run_id", "tag", "started_utc", "host", "workload", "transport"):
            assert key in raw, key
        assert raw["workload"]["num_points"] == 1

    def test_an_unreadable_manifest_is_skipped_not_fatal(self, tmp_path):
        good = Run(run_id=uuid7(), seq=1)
        JsonRunStore(tmp_path).save(good)
        broken = tmp_path / uuid7()
        broken.mkdir()
        (broken / "run.json").write_text("{ this is not json")

        runs = JsonRunStore(tmp_path).load()
        assert good.run_id in runs
        assert len(runs) == 1

    def test_saving_is_atomic(self, tmp_path):
        # No .tmp file may survive a save, or `load` would try to parse it.
        store = JsonRunStore(tmp_path)
        run = Run(run_id=uuid7(), seq=1)
        store.save(run)
        store.save(run)
        assert not list((tmp_path / run.run_id).glob("*.tmp"))

    def test_loading_an_absent_directory_is_empty_not_an_error(self, tmp_path):
        assert JsonRunStore(tmp_path / "nope").load() == {}


class TestJournal:
    def test_transitions_are_appended(self, tmp_path):
        store = JsonRunStore(tmp_path)
        store.journal("run-a", "start", {"to": "starting"})
        store.journal("run-a", "auto", {"from": "starting", "to": "running"})
        lines = store.journal_path.read_text().strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[1])["to"] == "running"

    def test_the_journal_wins_over_a_stale_manifest(self, tmp_path):
        # The case it exists for: the process died between the transition and
        # the manifest rewrite.
        store = JsonRunStore(tmp_path)
        run = Run(run_id=uuid7(), seq=1)
        store.save(run)  # written as draft
        store.journal(run.run_id, "start", {"to": "running"})
        assert JsonRunStore(tmp_path).load()[run.run_id].state is RunState.RUNNING

    def test_a_half_written_line_is_skipped(self, tmp_path):
        # Expected after a hard kill: the last line may be truncated.
        store = JsonRunStore(tmp_path)
        run = Run(run_id=uuid7(), seq=1)
        store.save(run)
        store.journal(run.run_id, "start", {"to": "running"})
        with store.journal_path.open("a") as fh:
            fh.write('{"run_id": "trunc')
        assert JsonRunStore(tmp_path).load()[run.run_id].state is RunState.RUNNING

    def test_an_unknown_state_in_the_journal_is_ignored(self, tmp_path):
        store = JsonRunStore(tmp_path)
        run = Run(run_id=uuid7(), seq=1)
        store.save(run)
        store.journal(run.run_id, "start", {"to": "teleported"})
        assert JsonRunStore(tmp_path).load()[run.run_id].state is RunState.DRAFT
