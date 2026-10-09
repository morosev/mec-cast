"""A node that joins while a run is `stopping` must not record it.

The failure this covers happened locally (2026-10-09, `make up-ran-admin`):
the gNB collector's first admin connect failed, its retry landed in the one
second the run spent in `stopping`, and the welcome offered that run as
`active_run`. The `run.stop` broadcast had already gone out, so nothing ever
told the collector to stop; it sat `running` against a stopped run with no
end, listed in the manifest's participants but not its sites.
"""

from __future__ import annotations

import contextlib
import json
import time

from fastapi.testclient import TestClient

from mec_cast_admin import protocol as p
from mec_cast_admin.app import create_app
from mec_cast_admin.state import RunState
from test_multicell import recv_type, report, send, start_run
from test_stranded_stopping import settings_with, state_of, stopping_run


def hello(client, stack, node_type, host, cell="default", autostart=True):
    """Handshake and return the node id, socket and WELCOME payload."""
    node = p.node_id(node_type, host, 0)
    socket = stack.enter_context(client.websocket_connect("/ws/node"))
    send(
        socket,
        p.MessageType.HELLO,
        p.HelloPayload(
            node_type=node_type, node_id=node, host=host, cell=cell, autostart=autostart
        ),
        node_id=node,
    )
    _, welcome = recv_type(socket, p.MessageType.WELCOME)
    return node, socket, welcome


def journal_events(tmp_path, run_id):
    path = tmp_path / "admin-journal.jsonl"
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [r for r in rows if r.get("run_id") == run_id]


class TestLateJoiner:
    def test_a_stopping_run_is_not_offered_in_the_welcome(self, tmp_path):
        s = settings_with(tmp_path, stop_timeout_s=3600)
        with TestClient(create_app(s)) as client, contextlib.ExitStack() as stack:
            run_id = stopping_run(client, stack)
            assert state_of(client, run_id)["state"] == RunState.STOPPING

            node, _, welcome = hello(client, stack, "gnb", "ran")

            assert welcome.active_run is None, (
                "a node joining after run.stop went out is never told to stop"
            )
            run = client.app.state.orchestrator.get_run(run_id)
            assert node not in run.participants

    def test_an_active_run_is_still_offered(self, tmp_path):
        """The guard must not cost the ordinary late join it exists beside."""
        s = settings_with(tmp_path, stop_timeout_s=3600)
        with TestClient(create_app(s)) as client, contextlib.ExitStack() as stack:
            run_id = start_run(client)
            assert client.post(f"/api/v1/runs/{run_id}/start").status_code == 200

            node, _, welcome = hello(client, stack, "gnb", "ran")

            assert welcome.active_run is not None
            assert welcome.active_run.run_id == run_id
            assert node in client.app.state.orchestrator.get_run(run_id).participants

    def test_a_node_recording_an_ended_run_is_told_to_stop_once(self, tmp_path):
        """The belt to the welcome's braces: however a node came to be
        recording a finished run, its own status says so, and the admin
        answers with a stop instead of leaving it there indefinitely."""
        s = settings_with(tmp_path, stop_timeout_s=3600)
        with TestClient(create_app(s)) as client, contextlib.ExitStack() as stack:
            run_id = stopping_run(client, stack)
            # The operator's second Stop: stopping -> stopped.
            assert client.post(f"/api/v1/runs/{run_id}/stop").status_code == 200
            assert state_of(client, run_id)["state"] == RunState.STOPPED
            # Connected only now, so no broadcast stop can reach it: the only
            # stop it can receive is the one its own status earns.
            node, sock, welcome = hello(client, stack, "gnb", "ran")
            assert welcome.active_run is None
            manifest_before = dict(client.app.state.orchestrator.get_run(run_id).sites)

            # Checked through the journal before reading the socket: without
            # the fix no frame ever comes, and a bare receive would hang.
            report(sock, node, "gnb", run_id)
            report(sock, node, "gnb", run_id)  # a second status earns no second stop
            time.sleep(0.3)
            stops = [e for e in journal_events(tmp_path, run_id) if e["event"] == "late-stop"]
            assert [e["node_id"] for e in stops] == [node]

            _, command = recv_type(sock, p.MessageType.COMMAND)
            assert command.command is p.CommandType.RUN_STOP
            assert command.run_id == run_id

            run = client.app.state.orchestrator.get_run(run_id)
            assert node not in run.sites and run.sites == manifest_before, (
                "a stopped run's manifest is frozen"
            )
