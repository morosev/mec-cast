"""A run must not fail before its nodes have had a chance to report.

Seen in the lab: admin plus one node, the RIC-side xApp. The run went
`starting -> failed` 2 s after Start, well inside the 30 s start timeout.
`participants_of` counts a node only once a status carrying the run_id has
been processed, so a supervise pass between run.start and that first status
saw zero participants, read "all offline", and failed the run. The xApp
then reported the run as running, for a run the admin had already failed.
"""

from __future__ import annotations

import contextlib
import time

from fastapi.testclient import TestClient

from mec_cast_admin.app import create_app
from mec_cast_admin.state import RunState
from test_multicell import connect, report, start_run
from test_stranded_stopping import settings_with, state_of, wait_for


class TestSlowFirstStatus:
    def test_a_node_slow_to_report_does_not_fail_the_run(self, tmp_path):
        s = settings_with(tmp_path, start_timeout_s=5.0)
        with TestClient(create_app(s)) as client, contextlib.ExitStack() as stack:
            run_id = start_run(client)
            cn, cs = connect(client, stack, "client", "h1", "default")
            en, es = connect(client, stack, "edge", "h2", "default")
            assert client.post(f"/api/v1/runs/{run_id}/start").status_code == 200
            # Many supervise passes (every 0.05 s) before anyone reports.
            time.sleep(0.5)
            assert state_of(client, run_id)["state"] == RunState.STARTING, (
                "no participant had reported yet; that is not 'all offline'"
            )
            report(cs, cn, "client", run_id)
            report(es, en, "edge", run_id)
            assert wait_for(client, run_id, RunState.RUNNING)["state"] == RunState.RUNNING

    def test_the_start_timeout_still_bounds_a_run_nobody_joins(self, tmp_path):
        s = settings_with(tmp_path, start_timeout_s=0.3)
        with TestClient(create_app(s)) as client:
            run_id = start_run(client)
            assert client.post(f"/api/v1/runs/{run_id}/start").status_code == 200
            assert wait_for(client, run_id, RunState.FAILED)["state"] == RunState.FAILED

    def test_a_participant_that_reported_and_went_offline_still_fails_it(self, tmp_path):
        """Once a participant has been seen, all-offline judges as before."""
        s = settings_with(tmp_path, start_timeout_s=3600, offline_timeout_s=0.3)
        with TestClient(create_app(s)) as client, contextlib.ExitStack() as stack:
            run_id = start_run(client)
            cn, cs = connect(client, stack, "client", "h1", "default")
            assert client.post(f"/api/v1/runs/{run_id}/start").status_code == 200
            report(cs, cn, "client", run_id)
            # Stay silent (no edge, so no quorum) until the client times out.
            assert wait_for(client, run_id, RunState.FAILED)["state"] == RunState.FAILED
