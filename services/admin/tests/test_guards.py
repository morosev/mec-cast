"""Safety guards for long-running and forgotten runs.

A forgotten run writes until the disk is full: at the 5,000-point default a
renderer's session.rrd alone grows ~4.6 GB/day. These are the guards that
stop that, and the point of testing them is that a guard which never fires is
indistinguishable from one that is not there.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from mec_cast_admin.app import create_app
from mec_cast_admin.config import Settings
from mec_cast_admin.state import RunState


def settings_with(tmp_path, **overrides):
    base = dict(
        runs_dir=str(tmp_path),
        keepalive_s=0.05,
        offline_timeout_s=0.5,
        start_timeout_s=0.5,
        diagnostics_interval_s=0.05,
        ui_broadcast_min_interval_s=0.01,
        max_run_duration_s=0,
        min_free_gb_start=0,
        min_free_gb_abort=0,
    )
    base.update(overrides)
    return Settings(**base)


class TestDiskHeadroom:
    def test_a_run_is_refused_when_the_disk_is_low(self, tmp_path):
        # A floor no volume can satisfy: the guard must refuse, and say the
        # number, rather than starting a run that dies mid-flight.
        s = settings_with(tmp_path, min_free_gb_start=10**9)
        with TestClient(create_app(s)) as client:
            run = client.post("/api/v1/runs", json={}).json()
            reply = client.post(f"/api/v1/runs/{run['run_id']}/start")
            assert reply.status_code == 409
            assert "free" in reply.json()["detail"].lower()
            assert "prune-runs" in reply.json()["detail"]

    def test_zero_disables_the_check(self, tmp_path):
        s = settings_with(tmp_path, min_free_gb_start=0)
        with TestClient(create_app(s)) as client:
            run = client.post("/api/v1/runs", json={}).json()
            assert client.post(f"/api/v1/runs/{run['run_id']}/start").status_code == 200

    def test_free_gb_reads_the_runs_volume(self, tmp_path):
        s = settings_with(tmp_path)
        with TestClient(create_app(s)) as client:
            free = client.app.state.orchestrator.free_gb()
        assert free is not None and free > 0


class TestMaxDuration:
    @pytest.mark.parametrize(
        "elapsed_h,limit_h,expect_stop",
        [(5.0, 4.0, True), (1.0, 4.0, False), (100.0, 0, False)],
        ids=["over the limit", "under it", "limit disabled"],
    )
    def test_recording_seconds_drives_the_decision(self, tmp_path, elapsed_h, limit_h, expect_stop):
        s = settings_with(tmp_path, max_run_duration_s=limit_h * 3600)
        with TestClient(create_app(s)) as client:
            orch = client.app.state.orchestrator
            run = orch.create_run()
            run.started_utc = (datetime.now(UTC) - timedelta(hours=elapsed_h)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )

            elapsed = orch.recording_seconds(run)
            assert elapsed == pytest.approx(elapsed_h * 3600, rel=0.01)

            limit = s.max_run_duration_s
            over = bool(limit) and elapsed > limit
            assert over is expect_stop

    def test_a_run_that_never_started_has_no_age(self, tmp_path):
        # started_utc is None before a start. Treating that as "infinitely
        # old" would auto-stop every draft run the moment it was created.
        s = settings_with(tmp_path)
        with TestClient(create_app(s)) as client:
            orch = client.app.state.orchestrator
            assert orch.recording_seconds(orch.create_run()) is None


class TestAutoStopIsClean:
    def test_the_watchdog_actually_stops_a_run_that_is_over_its_limit(self, tmp_path):
        """End to end: a run past the limit is stopped by the supervise pass.

        The point of the whole feature. Everything else here checks a piece of
        the decision; this checks that the pieces are wired to each other.
        """
        import time

        # A limit of one second, and a supervise pass every 50 ms.
        s = settings_with(tmp_path, max_run_duration_s=1)
        with TestClient(create_app(s)) as client:
            run = client.post("/api/v1/runs", json={}).json()
            assert client.post(f"/api/v1/runs/{run['run_id']}/start").status_code == 200

            # No nodes, so it sits in `starting` and the start timeout takes
            # it to `failed` -- which is terminal, and the watchdog must not
            # be what ends it. Backdate instead and drive one pass.
            orch = client.app.state.orchestrator
            live = orch.get_run(run["run_id"])
            live.state = RunState.RUNNING
            live.started_utc = "2020-01-01T00:00:00Z"

            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if orch.get_run(run["run_id"]).state is not RunState.RUNNING:
                    break
                time.sleep(0.05)

            assert orch.get_run(run["run_id"]).state is RunState.STOPPING

        journal = (tmp_path / "admin-journal.jsonl").read_text(encoding="utf-8")
        assert "auto-stop" in journal
        assert "over the" in journal

    def test_the_reason_lands_in_the_journal(self, tmp_path):
        # An auto-stopped run must be explainable afterwards: "why did this
        # end at 4 hours" has to be answerable from the journal alone.
        import asyncio

        s = settings_with(tmp_path)
        with TestClient(create_app(s)) as client:
            orch = client.app.state.orchestrator
            run = orch.create_run()
            asyncio.run(orch._auto_stop(run, "over the 4.0 h limit"))

        journal = (tmp_path / "admin-journal.jsonl").read_text(encoding="utf-8")
        assert "auto-stop" in journal
        assert "over the 4.0 h limit" in journal
