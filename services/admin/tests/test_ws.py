"""The service end to end: WebSocket handshakes, commands, and the REST API.

No containers and no database — `TestClient` drives the real app.
"""

from __future__ import annotations

import json

from mec_cast_admin import protocol as p


def send(socket, message_type, payload=None, node_id=None):
    socket.send_text(json.dumps(p.build(message_type, payload, node_id=node_id)))


def recv(socket, skip_pings: bool = True) -> tuple[p.Envelope, object]:
    """Next frame, ignoring keep-alive pings by default.

    The service pings on a timer, so any frame a test waits for may be
    preceded by any number of pings. A real node handles them on a separate
    path; these tests do the same rather than depending on timing.
    """
    for _ in range(100):
        envelope, payload = p.parse(json.loads(socket.receive_text()))
        if skip_pings and envelope.type is p.MessageType.PING:
            continue
        return envelope, payload
    raise AssertionError("only pings arrived")


def recv_type(socket, wanted: p.MessageType) -> tuple[p.Envelope, object]:
    """Wait for one specific message type."""
    for _ in range(100):
        envelope, payload = recv(socket)
        if envelope.type is wanted:
            return envelope, payload
    raise AssertionError(f"no {wanted} arrived")


def hello(node_type=p.NodeType.EDGE, host="mec01", **kwargs):
    node = p.node_id(node_type, host)
    return node, p.HelloPayload(node_type=node_type, node_id=node, host=host, **kwargs)


class TestHandshake:
    def test_hello_is_answered_with_welcome(self, client):
        node, payload = hello()
        with client.websocket_connect("/ws/node") as socket:
            send(socket, p.MessageType.HELLO, payload, node_id=node)
            envelope, welcome = recv(socket)
            assert envelope.type is p.MessageType.WELCOME
            assert welcome.protocol == p.PROTOCOL_VERSION
            assert welcome.active_run is None
            # The node must not have to guess the keep-alive contract.
            assert welcome.keepalive_s > 0
            assert welcome.offline_timeout_s > welcome.keepalive_s

    def test_a_node_appears_in_the_state_after_hello(self, client):
        node, payload = hello()
        with client.websocket_connect("/ws/node") as socket:
            send(socket, p.MessageType.HELLO, payload, node_id=node)
            recv(socket)
            nodes = client.get("/api/v1/state").json()["nodes"]
            assert [n["node_id"] for n in nodes] == [node]
            assert nodes[0]["online"] is True

    def test_a_foreign_protocol_version_is_rejected_and_closed(self, client):
        with client.websocket_connect("/ws/node") as socket:
            frame = p.build(p.MessageType.PING)
            frame["v"] = 99
            socket.send_text(json.dumps(frame))
            envelope, error = recv(socket)
            assert envelope.type is p.MessageType.ERROR
            # The node must be able to log what went wrong, not just drop.
            assert "99" in error.message or "version" in error.message

    def test_a_frame_before_hello_is_refused(self, client):
        with client.websocket_connect("/ws/node") as socket:
            send(
                socket,
                p.MessageType.STATUS,
                p.StatusPayload(node_type=p.NodeType.EDGE, state=p.NodeState.IDLE),
            )
            envelope, error = recv(socket)
            assert envelope.type is p.MessageType.ERROR
            assert error.code == "no_hello"

    def test_an_admin_to_node_message_from_a_node_is_refused(self, client):
        with client.websocket_connect("/ws/node") as socket:
            send(socket, p.MessageType.WELCOME, p.WelcomePayload(server_version="x"))
            envelope, error = recv(socket)
            assert envelope.type is p.MessageType.ERROR
            assert error.code == "wrong_direction"

    def test_garbage_does_not_kill_the_connection(self, client):
        node, payload = hello()
        with client.websocket_connect("/ws/node") as socket:
            socket.send_text("{not json")
            envelope, _ = recv(socket)
            assert envelope.type is p.MessageType.ERROR
            # Still usable afterwards.
            send(socket, p.MessageType.HELLO, payload, node_id=node)
            assert recv(socket)[0].type is p.MessageType.WELCOME


class TestRunLifecycle:
    def test_create_start_stop(self, client):
        created = client.post("/api/v1/runs", json={"label": "t", "params": {"rate_hz": 10.0}})
        assert created.status_code == 201
        run = created.json()
        assert run["state"] == "draft"
        assert run["allowed"] == ["start", "remove"]

        started = client.post(f"/api/v1/runs/{run['run_id']}/start")
        assert started.status_code == 200
        assert started.json()["state"] == "starting"
        assert started.json()["allowed"] == ["stop"]

        stopped = client.post(f"/api/v1/runs/{run['run_id']}/stop")
        assert stopped.json()["state"] == "stopping"

    def test_only_one_run_may_be_active(self, client):
        first = client.post("/api/v1/runs", json={"label": "a"}).json()
        second = client.post("/api/v1/runs", json={"label": "b"}).json()
        client.post(f"/api/v1/runs/{first['run_id']}/start")

        clash = client.post(f"/api/v1/runs/{second['run_id']}/start")
        assert clash.status_code == 409
        # The operator must be told which run is in the way.
        assert first["run_id"] in clash.json()["detail"]

    def test_an_illegal_transition_is_a_409_naming_the_state(self, client):
        run = client.post("/api/v1/runs", json={}).json()
        response = client.post(f"/api/v1/runs/{run['run_id']}/stop")
        assert response.status_code == 409
        assert "draft" in response.json()["detail"]

    def test_removing_a_run_hides_it_but_keeps_the_manifest(self, client, settings):
        run = client.post("/api/v1/runs", json={"label": "gone"}).json()
        assert client.delete(f"/api/v1/runs/{run['run_id']}").status_code == 200
        assert client.get("/api/v1/state").json()["runs"] == []
        # Measurement data is never deleted by a button.
        import pathlib

        assert (pathlib.Path(settings.runs_dir) / run["run_id"] / "run.json").exists()

    def test_an_unknown_run_is_a_409_not_a_crash(self, client):
        assert client.post("/api/v1/runs/nope/start").status_code == 409


class TestCommands:
    def test_starting_a_run_commands_the_connected_nodes(self, client):
        node, payload = hello()
        with client.websocket_connect("/ws/node") as socket:
            send(socket, p.MessageType.HELLO, payload, node_id=node)
            recv(socket)  # welcome

            run = client.post("/api/v1/runs", json={"params": {"rate_hz": 4.0}}).json()
            client.post(f"/api/v1/runs/{run['run_id']}/start")

            envelope, command = recv_type(socket, p.MessageType.COMMAND)
            assert envelope.type is p.MessageType.COMMAND
            assert command.command is p.CommandType.RUN_START
            assert command.run_id == run["run_id"]
            # The workload travels with the command, not the environment.
            assert command.args["rate_hz"] == 4.0

    def test_a_node_joining_mid_run_is_told_about_it(self, client):
        run = client.post("/api/v1/runs", json={"label": "already going"}).json()
        client.post(f"/api/v1/runs/{run['run_id']}/start")

        node, payload = hello()
        with client.websocket_connect("/ws/node") as socket:
            send(socket, p.MessageType.HELLO, payload, node_id=node)
            _, welcome = recv(socket)
            assert welcome.active_run is not None
            assert welcome.active_run.run_id == run["run_id"]

    def test_status_updates_the_view(self, client):
        node, payload = hello()
        with client.websocket_connect("/ws/node") as socket:
            send(socket, p.MessageType.HELLO, payload, node_id=node)
            recv(socket)
            send(
                socket,
                p.MessageType.STATUS,
                p.StatusPayload(
                    node_type=p.NodeType.EDGE,
                    state=p.NodeState.RUNNING,
                    subscribed=True,
                    counters={"frames": 7},
                ),
                node_id=node,
            )
            # Poll the REST view rather than racing the broadcast task.
            for _ in range(50):
                nodes = client.get("/api/v1/state").json()["nodes"]
                if nodes and nodes[0]["counters"].get("frames") == 7:
                    break
            assert nodes[0]["subscribed"] is True
            assert nodes[0]["state"] == "running"

    def test_stopping_completes_when_nodes_release_the_run(self, client):
        # A run stopped from the page leaves the nodes alive, so `stopping`
        # cannot wait for goodbye. It completes when no participant is still
        # recording — otherwise the single active-run slot is held forever.
        run = client.post("/api/v1/runs", json={}).json()
        node, payload = hello()
        with client.websocket_connect("/ws/node") as socket:
            send(socket, p.MessageType.HELLO, payload, node_id=node)
            recv(socket)
            client.post(f"/api/v1/runs/{run['run_id']}/start")
            recv_type(socket, p.MessageType.COMMAND)  # run.start
            send(
                socket,
                p.MessageType.STATUS,
                p.StatusPayload(
                    node_type=p.NodeType.EDGE,
                    state=p.NodeState.RUNNING,
                    run_id=run["run_id"],
                    subscribed=True,
                ),
                node_id=node,
            )

            client.post(f"/api/v1/runs/{run['run_id']}/stop")
            recv_type(socket, p.MessageType.COMMAND)  # run.stop
            # The node reports idle, carrying its recorder report.
            send(
                socket,
                p.MessageType.STATUS,
                p.StatusPayload(
                    node_type=p.NodeType.EDGE,
                    state=p.NodeState.IDLE,
                    run_id=None,
                    report={"samples_written": 42},
                ),
                node_id=node,
            )

            # No sleep inside the websocket context: TestClient pumps its
            # portal from this thread, and blocking it deadlocks the app.
            for _ in range(200):
                runs = client.get("/api/v1/state").json()["runs"]
                if runs[0]["state"] == "stopped":
                    break
        assert runs[0]["state"] == "stopped"
        assert runs[0]["allowed"] == ["remove"]
        assert runs[0]["reports"][node]["samples_written"] == 42

    def test_goodbye_records_the_final_report(self, client):
        run = client.post("/api/v1/runs", json={}).json()
        client.post(f"/api/v1/runs/{run['run_id']}/start")
        node, payload = hello()
        with client.websocket_connect("/ws/node") as socket:
            send(socket, p.MessageType.HELLO, payload, node_id=node)
            # The run was already started, so this node was never sent a
            # command: it learns about the active run from the welcome.
            _, welcome = recv_type(socket, p.MessageType.WELCOME)
            assert welcome.active_run.run_id == run["run_id"]
            send(
                socket,
                p.MessageType.GOODBYE,
                p.GoodbyePayload(
                    reason="sigterm", run_id=run["run_id"], final_report={"samples_written": 99}
                ),
                node_id=node,
            )

        for _ in range(50):
            runs = client.get("/api/v1/state").json()["runs"]
            if runs and runs[0]["reports"]:
                break
        assert runs[0]["reports"][node]["samples_written"] == 99


class TestUiSocket:
    def test_the_ui_receives_a_snapshot_on_connect(self, client):
        with client.websocket_connect("/ws/ui") as socket:
            snapshot = json.loads(socket.receive_text())
            assert snapshot["protocol"] == p.PROTOCOL_VERSION
            assert "runs" in snapshot and "nodes" in snapshot and "findings" in snapshot

    def test_every_run_row_carries_its_allowed_actions(self, client):
        # The contract the page depends on: it never computes enablement.
        client.post("/api/v1/runs", json={})
        with client.websocket_connect("/ws/ui") as socket:
            snapshot = json.loads(socket.receive_text())
        assert snapshot["runs"]
        for row in snapshot["runs"]:
            assert isinstance(row["allowed"], list)


class TestPersistence:
    def test_runs_survive_a_restart(self, settings):
        from fastapi.testclient import TestClient

        from mec_cast_admin.app import create_app

        with TestClient(create_app(settings)) as first:
            run = first.post("/api/v1/runs", json={"label": "durable"}).json()

        with TestClient(create_app(settings)) as second:
            runs = second.get("/api/v1/state").json()["runs"]
        assert [r["run_id"] for r in runs] == [run["run_id"]]
        assert runs[0]["label"] == "durable"

    def test_a_run_left_mid_flight_is_marked_failed_on_restart(self, settings):
        # Its participants are gone; pretending it is still running would keep
        # the single active-run slot occupied forever.
        from fastapi.testclient import TestClient

        from mec_cast_admin.app import create_app

        with TestClient(create_app(settings)) as first:
            run = first.post("/api/v1/runs", json={}).json()
            first.post(f"/api/v1/runs/{run['run_id']}/start")

        with TestClient(create_app(settings)) as second:
            runs = second.get("/api/v1/state").json()["runs"]
            assert runs[0]["state"] == "failed"
            # And the slot is free again.
            fresh = second.post("/api/v1/runs", json={}).json()
            assert second.post(f"/api/v1/runs/{fresh['run_id']}/start").status_code == 200


class TestHealth:
    def test_health_reports_the_protocol_version(self, client):
        body = client.get("/health").json()
        assert body["status"] == "ok"
        assert body["protocol"] == p.PROTOCOL_VERSION

    def test_readiness_answers(self, client):
        assert client.get("/health/ready").status_code == 200
