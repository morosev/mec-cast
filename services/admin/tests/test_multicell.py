"""Multi-cell: concurrent runs, and commands that reach only their own cell.

The fan-out test is the reason this file exists. `_broadcast_command` used to
target every online node, so a `run.stop` for one cell stopped the other
cell's nodes mid-measurement — the one change in M3 with real behaviour risk.
"""

from __future__ import annotations

import contextlib
import json

from mec_cast_admin import protocol as p
from mec_cast_admin.registry import Registry
from mec_cast_admin.state import RunState
from mec_cast_admin.store import Run
from mec_cast_admin.workflow import diagnose

# The helpers are duplicated from test_ws rather than imported: the tests
# directory is not a package, so a cross-module import only works by accident
# of sys.path and breaks under a different pytest import mode.


def send(socket, message_type, payload=None, node_id=None):
    socket.send_text(json.dumps(p.build(message_type, payload, node_id=node_id)))


def recv(socket, skip_pings: bool = True):
    for _ in range(100):
        envelope, payload = p.parse(json.loads(socket.receive_text()))
        if skip_pings and envelope.type is p.MessageType.PING:
            continue
        return envelope, payload
    raise AssertionError("only pings arrived")


def recv_type(socket, wanted: p.MessageType):
    for _ in range(100):
        envelope, payload = recv(socket)
        if envelope.type is wanted:
            return envelope, payload
    raise AssertionError(f"no {wanted} arrived")


def start_run(client, cell: str = "default", label: str = "") -> str:
    """Create a run in `cell` through the API and return its id.

    Through the API deliberately. An earlier version of these tests set the
    cell by reaching into the orchestrator, and that hid the fact that there
    was no way to do it from outside the process at all — the whole feature
    was unreachable for an operator while its tests passed.
    """
    response = client.post("/api/v1/runs", json={"label": label, "cell": cell})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["cell"] == cell
    return body["run_id"]


def connect(client, stack, node_type, host, cell, instance=0):
    """Open a node socket inside `stack` and complete the handshake."""
    node = p.node_id(node_type, host, instance)
    socket = stack.enter_context(client.websocket_connect("/ws/node"))
    send(
        socket,
        p.MessageType.HELLO,
        p.HelloPayload(node_type=node_type, node_id=node, host=host, cell=cell),
        node_id=node,
    )
    recv(socket)
    return node, socket


def report(socket, node, node_type, run_id, **extra):
    send(
        socket,
        p.MessageType.STATUS,
        p.StatusPayload(
            node_type=node_type,
            state=p.NodeState.RUNNING if run_id else p.NodeState.IDLE,
            run_id=run_id,
            **extra,
        ),
        node_id=node,
    )


class TestCommandScope:
    def test_a_stop_in_one_cell_does_not_stop_the_other(self, client):
        """The bug this milestone exists to fix.

        Before: `run.stop` went to every online node, so stopping cell A
        ended cell B's measurement too — silently, mid-run, with cell B's
        CSV simply ending early.
        """
        with contextlib.ExitStack() as stack:
            a_node, a_sock = connect(client, stack, p.NodeType.CLIENT, "ue-a", "cell-a")
            b_node, b_sock = connect(client, stack, p.NodeType.CLIENT, "ue-b", "cell-b")

            run_a = start_run(client, "cell-a", "A")
            run_b = start_run(client, "cell-b", "B")

            client.post(f"/api/v1/runs/{run_a}/start")
            recv_type(a_sock, p.MessageType.COMMAND)  # A's start reaches A
            report(a_sock, a_node, p.NodeType.CLIENT, run_a, streaming=True)

            client.post(f"/api/v1/runs/{run_b}/start")
            recv_type(b_sock, p.MessageType.COMMAND)  # B's start reaches B
            report(b_sock, b_node, p.NodeType.CLIENT, run_b, streaming=True)

            # Stop A. B must hear nothing at all.
            client.post(f"/api/v1/runs/{run_a}/stop")
            envelope, payload = recv_type(a_sock, p.MessageType.COMMAND)
            assert payload.command == p.CommandType.RUN_STOP
            assert payload.run_id == run_a

            # B's socket must have no command waiting. Any frame that does
            # arrive must be a keep-alive, never a command.
            b_sock.send_text(json.dumps(p.build(p.MessageType.PONG, node_id=b_node)))
            for _ in range(5):
                frame = json.loads(b_sock.receive_text())
                assert frame["type"] != str(p.MessageType.COMMAND), (
                    f"cell-b received {frame}; a stop in cell-a must not reach it"
                )
                if frame["type"] == str(p.MessageType.PING):
                    break

    def test_a_start_reaches_its_cell_not_the_others(self, client):
        with contextlib.ExitStack() as stack:
            a_node, a_sock = connect(client, stack, p.NodeType.CLIENT, "ue-a", "cell-a")
            b_node, b_sock = connect(client, stack, p.NodeType.CLIENT, "ue-b", "cell-b")

            run_a = start_run(client, "cell-a")
            client.post(f"/api/v1/runs/{run_a}/start")

            envelope, payload = recv_type(a_sock, p.MessageType.COMMAND)
            assert payload.run_id == run_a

            b_sock.send_text(json.dumps(p.build(p.MessageType.PONG, node_id=b_node)))
            for _ in range(5):
                frame = json.loads(b_sock.receive_text())
                assert frame["type"] != str(p.MessageType.COMMAND), (
                    "a start in cell-a must not recruit cell-b's nodes"
                )
                if frame["type"] == str(p.MessageType.PING):
                    break

    def test_a_node_with_no_cell_belongs_to_the_default_one(self, client):
        # Every deployment that has not declared a topology. It must keep
        # receiving commands exactly as it always did.
        with contextlib.ExitStack() as stack:
            node, socket = connect(client, stack, p.NodeType.EDGE, "mec01", "")
            run = start_run(client, "default")
            client.post(f"/api/v1/runs/{run}/start")
            envelope, payload = recv_type(socket, p.MessageType.COMMAND)
            assert payload.run_id == run


class TestConcurrentRuns:
    def test_two_cells_can_run_at_once(self, client):
        run_a = start_run(client, "cell-a")
        run_b = start_run(client, "cell-b")
        assert client.post(f"/api/v1/runs/{run_a}/start").status_code == 200
        # The old rule refused this with 409: one active run per PLATFORM.
        assert client.post(f"/api/v1/runs/{run_b}/start").status_code == 200

        state = client.get("/api/v1/state").json()
        assert state["active_runs"] == {"cell-a": run_a, "cell-b": run_b}

    def test_one_cell_still_allows_only_one_run(self, client):
        # The within-cell limit stands: it falls out of one active Recorder
        # per node process (ADR-0007), which multi-cell does not change.
        run_1 = start_run(client, "cell-a")
        run_2 = start_run(client, "cell-a")
        assert client.post(f"/api/v1/runs/{run_1}/start").status_code == 200
        response = client.post(f"/api/v1/runs/{run_2}/start")
        assert response.status_code == 409
        assert "cell-a" in response.json()["detail"]

    def test_a_start_timeout_is_tracked_per_run(self, client):
        # One scalar `_starting_since` let a second start reset the first
        # run's timeout clock, so a stalled run could hang in `starting`
        # indefinitely as long as another cell kept starting runs.
        orch = client.app.state.orchestrator
        run_a = start_run(client, "cell-a")
        run_b = start_run(client, "cell-b")
        client.post(f"/api/v1/runs/{run_a}/start")
        client.post(f"/api/v1/runs/{run_b}/start")
        assert set(orch._starting_since) == {run_a, run_b}


class TestCellScopedDiagnostics:
    """A fault in one cell must not be reported against another."""

    def _registry_with_two_cells(self):
        registry = Registry()
        for node_type, host, cell in (
            (p.NodeType.CLIENT, "ue-a", "cell-a"),
            (p.NodeType.EDGE, "mec-a", "cell-a"),
            (p.NodeType.CLIENT, "ue-b", "cell-b"),
            (p.NodeType.EDGE, "mec-b", "cell-b"),
            (p.NodeType.GNB, "gnb-b", "cell-b"),
        ):
            node = p.node_id(node_type, host, 0)
            registry.on_hello(
                p.HelloPayload(node_type=node_type, node_id=node, host=host, cell=cell)
            )
            registry.on_status(
                node,
                p.StatusPayload(
                    node_type=node_type,
                    state=p.NodeState.RUNNING,
                    run_id="0190d1f2-0000-7000-8000-000000000000",
                    streaming=node_type is p.NodeType.CLIENT,
                    subscribed=node_type is p.NodeType.EDGE,
                    params={"reliability": "reliable"},
                ),
            )
        return registry

    def _run(self, cell: str) -> Run:
        run = Run(run_id="0190d1f2-0000-7000-8000-000000000000", seq=1, cell=cell)
        run.state = RunState.RUNNING
        return run

    def test_a_gnb_in_one_cell_does_not_satisfy_the_other(self):
        # cell-b has a gNB, cell-a does not. Diagnosing cell-a must still say
        # so — a flat partition would see "a gNB exists" and stay quiet.
        findings = diagnose(self._registry_with_two_cells(), self._run("cell-a"))
        assert "WF_GNB_ABSENT" in {f.code for f in findings}

    def test_clients_are_not_paired_against_other_cells_edges(self):
        from mec_cast_admin.registry import Registry

        # A QoS mismatch that only exists ACROSS cells is not a mismatch: the
        # two never exchange a frame. The cross-product produced N x M of
        # these the moment a second cell appeared.
        registry = Registry()
        for node_type, host, cell, reliability in (
            (p.NodeType.CLIENT, "ue-a", "cell-a", "reliable"),
            (p.NodeType.EDGE, "mec-a", "cell-a", "reliable"),
            (p.NodeType.CLIENT, "ue-b", "cell-b", "best_effort"),
            (p.NodeType.EDGE, "mec-b", "cell-b", "best_effort"),
        ):
            node = p.node_id(node_type, host, 0)
            registry.on_hello(
                p.HelloPayload(node_type=node_type, node_id=node, host=host, cell=cell)
            )
            registry.on_status(
                node,
                p.StatusPayload(
                    node_type=node_type,
                    state=p.NodeState.RUNNING,
                    run_id="0190d1f2-0000-7000-8000-000000000000",
                    streaming=node_type is p.NodeType.CLIENT,
                    subscribed=node_type is p.NodeType.EDGE,
                    peers=[p.Peer(peer_id="/mec_cast_lidar_client")],
                    params={"reliability": reliability},
                ),
            )
        # Each cell is internally consistent, so nothing should be reported
        # even though a cross-cell pair would look mismatched.
        for cell in ("cell-a", "cell-b"):
            codes = {f.code for f in diagnose(registry, self._run(cell))}
            assert "WF_QOS_MISMATCH" not in codes, cell

    def test_findings_carry_their_cell(self):
        findings = diagnose(self._registry_with_two_cells(), self._run("cell-a"))
        gnb = [f for f in findings if f.code == "WF_GNB_ABSENT"]
        assert gnb and gnb[0].cell == "cell-a"


class TestRunCell:
    """A run's cell has to be reachable from outside the process."""

    def test_the_cell_round_trips_through_the_api(self, client):
        run = client.post("/api/v1/runs", json={"label": "x", "cell": "cell-a"}).json()
        assert run["cell"] == "cell-a"
        row = [
            r for r in client.get("/api/v1/state").json()["runs"] if r["run_id"] == run["run_id"]
        ][0]
        assert row["cell"] == "cell-a"

    def test_omitting_the_cell_gives_the_default_one(self, client):
        # Every existing caller, and every deployment with no topology.
        run = client.post("/api/v1/runs", json={}).json()
        assert run["cell"] == "default"

    def test_an_undeclared_cell_is_refused_when_a_topology_exists(self, client, tmp_path):
        # A typo would otherwise create a run no node can ever join, which
        # looks exactly like a broken deployment rather than a typo.
        import mec_cast_admin.topology as topo

        orch = client.app.state.orchestrator
        path = tmp_path / "topology.yml"
        path.write_text("nodes:\n  - {role: edge, host: mec01, cell: cell-a}\n", encoding="utf-8")
        orch.topology = topo.load(path)

        response = client.post("/api/v1/runs", json={"cell": "cell-typo"})
        assert response.status_code == 409
        assert "cell-a" in response.json()["detail"]

    def test_any_cell_is_allowed_without_a_declared_topology(self, client):
        # Nothing to check against, so nothing to refuse.
        assert client.post("/api/v1/runs", json={"cell": "whatever"}).status_code == 201


class TestNoCrossCellFalsePositives:
    """Concurrent runs must not make each other look broken."""

    def test_a_node_on_its_own_cells_run_is_not_a_mismatch(self, client):
        # The live-run false positive: with two runs active, every node in
        # the cell NOT being diagnosed was reported as recording the wrong
        # run — two WF_RUN_MISMATCH errors for a perfectly healthy fleet.
        with contextlib.ExitStack() as stack:
            a_node, a_sock = connect(client, stack, p.NodeType.CLIENT, "ue-a", "cell-a")
            b_node, b_sock = connect(client, stack, p.NodeType.CLIENT, "ue-b", "cell-b")
            run_a = start_run(client, "cell-a")
            run_b = start_run(client, "cell-b")
            client.post(f"/api/v1/runs/{run_a}/start")
            client.post(f"/api/v1/runs/{run_b}/start")
            recv_type(a_sock, p.MessageType.COMMAND)
            recv_type(b_sock, p.MessageType.COMMAND)
            report(a_sock, a_node, p.NodeType.CLIENT, run_a, streaming=True)
            report(b_sock, b_node, p.NodeType.CLIENT, run_b, streaming=True)

            orch = client.app.state.orchestrator
            for _ in range(200):
                orch._refresh_findings()
                if orch.registry.get(a_node).run_id == run_a:
                    break
            codes = [f["code"] for f in orch._findings]
            assert "WF_RUN_MISMATCH" not in codes, orch._findings

    def test_both_cells_are_diagnosed_not_just_the_first(self, client):
        # Diagnosing only the first active run left the other cell with no
        # diagnosis at all, so a genuinely broken cell-b stayed silent.
        with contextlib.ExitStack() as stack:
            connect(client, stack, p.NodeType.CLIENT, "ue-a", "cell-a")
            connect(client, stack, p.NodeType.EDGE, "mec-a", "cell-a")
            connect(client, stack, p.NodeType.CLIENT, "ue-b", "cell-b")
            run_a = start_run(client, "cell-a")
            run_b = start_run(client, "cell-b")
            client.post(f"/api/v1/runs/{run_a}/start")
            client.post(f"/api/v1/runs/{run_b}/start")

            orch = client.app.state.orchestrator
            orch._refresh_findings()
            cells = {f["cell"] for f in orch._findings if f["code"] == "WF_EDGE_ABSENT"}
            # cell-b has no edge; cell-a does. Only cell-b should say so.
            assert cells == {"cell-b"}, orch._findings
