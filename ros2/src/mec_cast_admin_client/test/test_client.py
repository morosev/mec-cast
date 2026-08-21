"""The admin client, against a real WebSocket server in-process.

The analogue of ``ran/collector/tests/replay.rs``, which stands up a stub
listener rather than mocking the transport. Nothing here needs ROS2: the
client is deliberately free of rclpy so that it can be tested like this.
"""

from __future__ import annotations

import json
import pathlib
import queue
import sys
import threading
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from mec_cast_admin_client import protocol as p  # noqa: E402
from mec_cast_admin_client.client import AdminClient  # noqa: E402

websockets_server = pytest.importorskip("websockets.sync.server")

#: The fixture the admin service's own tests read. If the two protocol
#: implementations disagree about a field name, this file is where it shows.
VECTORS = (
    pathlib.Path(__file__).resolve().parents[4]
    / "services"
    / "admin"
    / "tests"
    / "vectors.json"
)


class StubAdmin:
    """A minimal admin: records what arrives, sends what it is told to."""

    def __init__(self) -> None:
        self.received: queue.Queue[dict] = queue.Queue()
        self._to_send: queue.Queue[dict] = queue.Queue()
        self._server = None
        self._thread: threading.Thread | None = None
        self.connections = 0

    def _handler(self, socket) -> None:
        self.connections += 1
        try:
            while True:
                try:
                    raw = socket.recv(timeout=0.05)
                    self.received.put(json.loads(raw))
                except TimeoutError:
                    pass
                try:
                    socket.send(json.dumps(self._to_send.get_nowait()))
                except queue.Empty:
                    pass
        except Exception:
            return

    def start(self) -> str:
        self._server = websockets_server.serve(self._handler, "127.0.0.1", 0)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        port = self._server.socket.getsockname()[1]
        return f"ws://127.0.0.1:{port}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()

    def send(self, frame: dict) -> None:
        self._to_send.put(frame)

    def wait_for(self, message_type: str, timeout: float = 5.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                frame = self.received.get(timeout=0.1)
            except queue.Empty:
                continue
            if frame.get("type") == message_type:
                return frame
        raise AssertionError(f"no {message_type} within {timeout}s")


@pytest.fixture
def admin():
    stub = StubAdmin()
    stub.url = stub.start()
    yield stub
    stub.stop()


@pytest.fixture
def client(admin):
    c = AdminClient(
        node_type=p.NodeType.EDGE, host="mec01", url=admin.url, retry_s=0.2, pid=123
    )
    yield c
    c.stop(timeout=1.0)


class TestSubscription:
    def test_it_says_hello_on_connect(self, admin, client):
        client.update_identity(params={"reliability": "reliable"}, autostart=True)
        client.start()
        hello = admin.wait_for(p.MessageType.HELLO)
        assert hello["v"] == p.PROTOCOL_VERSION
        assert hello["node_id"] == "edge-mec01-0"
        payload = hello["payload"]
        assert payload["node_type"] == "edge"
        assert payload["host"] == "mec01"
        assert payload["autostart"] is True
        assert payload["params"]["reliability"] == "reliable"

    def test_hello_reflects_state_at_connect_not_at_construction(self, admin, client):
        # A node that reconnects mid-run must announce the run it is actually
        # recording, or the admin reconciles against a stale picture.
        client.update_identity(state=p.NodeState.RUNNING, run_id="run-42")
        client.start()
        payload = admin.wait_for(p.MessageType.HELLO)["payload"]
        assert payload["state"] == "running"
        assert payload["run_id"] == "run-42"

    def test_it_answers_pings(self, admin, client):
        client.start()
        admin.wait_for(p.MessageType.HELLO)
        admin.send(p.build(p.MessageType.PING))
        assert admin.wait_for(p.MessageType.PONG)["node_id"] == "edge-mec01-0"

    def test_it_reconnects_after_the_server_drops_it(self, admin, client):
        client.start()
        admin.wait_for(p.MessageType.HELLO)
        first = admin.connections

        admin.stop()
        time.sleep(0.1)
        admin.url = admin.start()
        client.url = admin.url  # same endpoint in production; a new port here

        admin.wait_for(p.MessageType.HELLO, timeout=6.0)
        assert admin.connections >= 1 and first >= 1


class TestCommands:
    def test_commands_reach_the_node_through_poll(self, admin, client):
        client.start()
        admin.wait_for(p.MessageType.HELLO)
        admin.send(p.build(p.MessageType.COMMAND, {
            "command": p.CommandType.RUN_START, "run_id": "run-1",
            "args": {"rate_hz": 5.0}}))

        deadline = time.monotonic() + 5.0
        frames = []
        while time.monotonic() < deadline and not frames:
            frames = [f for f in client.poll() if f["type"] == p.MessageType.COMMAND]
            time.sleep(0.05)
        assert frames, "command never arrived"
        assert frames[0]["payload"]["command"] == "run.start"
        assert frames[0]["payload"]["args"]["rate_hz"] == 5.0

    def test_welcome_reaches_the_node(self, admin, client):
        client.start()
        admin.wait_for(p.MessageType.HELLO)
        admin.send(p.build(p.MessageType.WELCOME, {
            "server_version": "0.1.0", "protocol": 1,
            "active_run": {"run_id": "run-9", "label": "x", "params": {}}}))

        deadline = time.monotonic() + 5.0
        got = None
        while time.monotonic() < deadline and got is None:
            for frame in client.poll():
                if frame["type"] == p.MessageType.WELCOME:
                    got = frame
            time.sleep(0.05)
        assert got and got["payload"]["active_run"]["run_id"] == "run-9"

    def test_a_foreign_version_is_ignored_without_killing_the_client(self, admin, client):
        client.start()
        admin.wait_for(p.MessageType.HELLO)
        bad = p.build(p.MessageType.COMMAND, {"command": "run.start"})
        bad["v"] = 99
        admin.send(bad)
        time.sleep(0.3)
        # Still alive and still answering.
        admin.send(p.build(p.MessageType.PING))
        assert admin.wait_for(p.MessageType.PONG)

    def test_status_is_delivered(self, admin, client):
        client.start()
        admin.wait_for(p.MessageType.HELLO)
        client.publish_status(p.status_payload(
            node_type=p.NodeType.EDGE, state=p.NodeState.RUNNING,
            subscribed=True, counters={"frames": 12}))
        status = admin.wait_for(p.MessageType.STATUS)
        assert status["payload"]["counters"]["frames"] == 12
        assert status["payload"]["subscribed"] is True


class TestGoodbye:
    def test_goodbye_carries_the_final_report(self, admin, client):
        client.start()
        admin.wait_for(p.MessageType.HELLO)
        client.goodbye(reason="sigterm", run_id="run-1",
                       final_report={"samples_written": 99}, timeout=1.0)
        frame = admin.wait_for(p.MessageType.GOODBYE)
        assert frame["payload"]["final_report"]["samples_written"] == 99
        assert frame["payload"]["reason"] == "sigterm"


class TestBackpressure:
    def test_a_full_queue_drops_rather_than_blocking(self):
        # The control plane must never be able to stall a measurement node.
        client = AdminClient(node_type=p.NodeType.CLIENT, host="ue01",
                             url="ws://127.0.0.1:1", retry_s=99)
        payload = p.status_payload(node_type=p.NodeType.CLIENT, state=p.NodeState.IDLE)
        started = time.monotonic()
        for _ in range(500):
            client.publish_status(payload)
        assert time.monotonic() - started < 1.0
        assert client.dropped_outbound > 0

    def test_a_client_with_no_url_is_inert(self):
        client = AdminClient(node_type=p.NodeType.CLIENT, host="ue01", url="")
        assert client.enabled is False
        client.start()  # a no-op, spawns no thread
        assert client.publish_status({}) is False
        assert client.poll() == []
        client.goodbye()  # must not raise


class TestProtocolAgreement:
    """The service and this module are separate implementations. These read
    the service's own fixture so they cannot drift apart in silence."""

    def test_the_vectors_file_is_where_both_sides_expect(self):
        assert VECTORS.exists(), f"shared vectors missing at {VECTORS}"

    def test_every_admin_to_node_vector_parses(self):
        vectors = {
            name: frame
            for name, frame in json.loads(VECTORS.read_text()).items()
            if not name.startswith("_")
        }
        for name, frame in vectors.items():
            if frame["type"] in (p.MessageType.WELCOME, p.MessageType.COMMAND,
                                 p.MessageType.PING, p.MessageType.ERROR):
                assert p.parse(frame)["type"] == frame["type"], name

    def test_our_frames_match_the_recorded_shapes(self):
        vectors = json.loads(VECTORS.read_text())
        expected = set(vectors["hello_client"]["payload"])
        ours = set(p.hello_payload(
            node_type=p.NodeType.CLIENT, node_id="client-ue01-0",
            host="ue01", pid=1))
        assert ours == expected

        expected_status = set(vectors["status_edge"]["payload"])
        ours_status = set(p.status_payload(
            node_type=p.NodeType.EDGE, state=p.NodeState.RUNNING))
        assert ours_status == expected_status

        expected_goodbye = set(vectors["goodbye"]["payload"])
        assert set(p.goodbye_payload()) == expected_goodbye

    def test_the_envelope_shape_matches(self):
        recorded = json.loads(VECTORS.read_text())["ping"]
        ours = p.build(p.MessageType.PING)
        assert set(ours) == set(recorded)
