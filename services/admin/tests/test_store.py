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


class TestRunOrdering:
    """Newest first, across both id schemes.

    The admin mints UUIDv7, which sorts chronologically. But
    `scripts/run-experiment.sh` mints its own with uuidgen -- UUIDv4, whose
    leading bytes are random -- and both writers' manifests land in the same
    directory. Ordering the table by run_id therefore sorted the script's runs
    arbitrarily among themselves and, because every v7 id begins `01`, above
    every admin-minted run as well. The newest run was reliably not at the top.
    """

    #: A v4 id whose leading hex is above any v7 id's, paired with the OLDEST
    #: timestamp, so id order and time order disagree by construction. Sorting
    #: by id puts this first; sorting by time puts it last.
    SCRIPT_RUN = "f9c9c1bc-13bb-4cf5-9c23-99c1040d8e23"
    ADMIN_OLDER = "01a04fae-50a5-7000-ad8e-5f539cdfc60f"
    ADMIN_NEWEST = "01a04fff-0000-7000-8000-000000000000"

    def _write(self, tmp_path, run_id, started, created=None):
        directory = tmp_path / run_id
        directory.mkdir()
        manifest = {"run_id": run_id, "seq": 0, "started_utc": started}
        if created:
            manifest["created_utc"] = created
        (directory / "run.json").write_text(json.dumps(manifest), encoding="utf-8")

    def test_newest_first_across_both_id_schemes(self, tmp_path, settings):
        from mec_cast_admin.orchestrator import Orchestrator

        # The script's manifests carry no created_utc at all; from_manifest
        # falls back to started_utc, which is what makes them orderable.
        self._write(tmp_path, self.SCRIPT_RUN, "2026-01-01T00:00:00Z")
        self._write(
            tmp_path, self.ADMIN_OLDER, "2026-06-01T00:00:00Z", created="2026-06-01T00:00:00Z"
        )
        self._write(
            tmp_path, self.ADMIN_NEWEST, "2026-09-01T00:00:00Z", created="2026-09-01T00:00:00Z"
        )

        store = JsonRunStore(tmp_path)
        orch = Orchestrator(settings, store)
        # Orchestrator.start() is async and launches the diagnostics and
        # broadcast tasks; the ordering under test needs neither, so the
        # loaded runs go in directly.
        orch._runs = store.load()
        got = [r.run_id for r in orch.visible_runs()]

        assert got == [self.ADMIN_NEWEST, self.ADMIN_OLDER, self.SCRIPT_RUN]

        # And not vacuous: the old key produces a different, wrong answer, so
        # this test fails against the behaviour it was written for.
        by_id = sorted(got, reverse=True)
        assert by_id != got
        assert by_id[0] == self.SCRIPT_RUN
