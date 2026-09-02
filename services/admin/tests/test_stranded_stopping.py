"""A run in `stopping` must never strand its cell.

The failure this covers happened in the lab: run 8 sat in `stopping` across
an admin restart with Start, Stop and Remove all disabled, holding its cell's
slot so no new run could begin. Both exits from `stopping` needed the nodes
-- every participant offline, or every report in -- and a participant that is
ONLINE but will never report satisfies neither. That is the normal state
after an admin restart, when nodes reconnect holding no such run.
"""

from __future__ import annotations

import contextlib
import time

from fastapi.testclient import TestClient

from mec_cast_admin.app import create_app
from mec_cast_admin.config import Settings
from mec_cast_admin.state import RunState
from test_multicell import connect, report, start_run


def settings_with(tmp_path, **overrides):
    base = dict(
        runs_dir=str(tmp_path),
        keepalive_s=0.05,
        offline_timeout_s=30.0,  # keep the node ONLINE: that is the bug
        start_timeout_s=0.5,
        stop_timeout_s=0.4,
        diagnostics_interval_s=0.05,
        ui_broadcast_min_interval_s=0.01,
        max_run_duration_s=0,
        min_free_gb_start=0,
        min_free_gb_abort=0,
    )
    base.update(overrides)
    return Settings(**base)


def state_of(client, run_id):
    body = client.get("/api/v1/state").json()
    for row in body["runs"]:
        if row["run_id"] == run_id:
            return row
    raise AssertionError(f"run {run_id} not in state")


def wait_for(client, run_id, want, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = state_of(client, run_id)
        if row["state"] == want:
            return row
        time.sleep(0.05)
    return state_of(client, run_id)


def stopping_run(client, stack, cell="default"):
    """Drive a run to `stopping` with its participants still online."""
    run_id = start_run(client, cell=cell)
    cn, cs = connect(client, stack, "client", "h1", cell)
    en, es = connect(client, stack, "edge", "h2", cell)
    assert client.post(f"/api/v1/runs/{run_id}/start").status_code == 200
    report(cs, cn, "client", run_id)
    report(es, en, "edge", run_id)
    wait_for(client, run_id, "running")
    assert client.post(f"/api/v1/runs/{run_id}/stop").status_code == 200
    # The nodes stay connected and simply never send a report for this run —
    # exactly what an admin restart leaves behind.
    return run_id


class TestStrandedStopping:
    def test_the_timeout_releases_a_run_whose_nodes_never_report(self, tmp_path):
        s = settings_with(tmp_path)
        with TestClient(create_app(s)) as client, contextlib.ExitStack() as stack:
            run_id = stopping_run(client, stack)
            assert state_of(client, run_id)["state"] == RunState.STOPPING
            row = wait_for(client, run_id, RunState.STOPPED)
            assert row["state"] == RunState.STOPPED, (
                "a run whose online participants never report must not sit in stopping forever"
            )

    def test_the_slot_is_released_so_the_next_run_can_start(self, tmp_path):
        """The consequence that actually blocked the lab: not the stuck row
        itself, but that nothing else could run."""
        s = settings_with(tmp_path)
        with TestClient(create_app(s)) as client, contextlib.ExitStack() as stack:
            stranded = stopping_run(client, stack)
            wait_for(client, stranded, RunState.STOPPED)

            nxt = start_run(client)
            reply = client.post(f"/api/v1/runs/{nxt}/start")
            assert reply.status_code == 200, reply.text

    def test_the_operator_can_force_it_without_waiting(self, tmp_path):
        """A second Stop is the escape hatch. Before, `stopping` allowed no
        action at all, so the UI offered nothing to click."""
        s = settings_with(tmp_path, stop_timeout_s=3600)
        with TestClient(create_app(s)) as client, contextlib.ExitStack() as stack:
            run_id = stopping_run(client, stack)
            row = state_of(client, run_id)
            assert row["state"] == RunState.STOPPING
            assert "stop" in row["allowed"], "no button = unrecoverable"

            assert client.post(f"/api/v1/runs/{run_id}/stop").status_code == 200
            assert state_of(client, run_id)["state"] == RunState.STOPPED

    def test_it_is_stopped_not_failed(self, tmp_path):
        """The run did stop and its CSVs are complete; only a report is
        missing. FAILED would misreport a good run in its own manifest."""
        s = settings_with(tmp_path)
        with TestClient(create_app(s)) as client, contextlib.ExitStack() as stack:
            run_id = stopping_run(client, stack)
            row = wait_for(client, run_id, RunState.STOPPED)
            assert row["state"] != RunState.FAILED
            assert row["state"] == RunState.STOPPED

    def test_a_normal_stop_still_resolves_by_reports_not_timeout(self, tmp_path):
        """The timeout must be the fallback, not the mechanism. With a long
        timeout a properly reporting run still stops promptly."""
        s = settings_with(tmp_path, stop_timeout_s=3600)
        with TestClient(create_app(s)) as client, contextlib.ExitStack() as stack:
            run_id = start_run(client)
            cn, cs = connect(client, stack, "client", "h1", "default")
            en, es = connect(client, stack, "edge", "h2", "default")
            assert client.post(f"/api/v1/runs/{run_id}/start").status_code == 200
            report(cs, cn, "client", run_id)
            report(es, en, "edge", run_id)
            wait_for(client, run_id, "running")
            client.post(f"/api/v1/runs/{run_id}/stop")
            report(cs, cn, "client", None)
            report(es, en, "edge", None)
            row = wait_for(client, run_id, RunState.STOPPED, timeout=3.0)
            assert row["state"] == RunState.STOPPED
