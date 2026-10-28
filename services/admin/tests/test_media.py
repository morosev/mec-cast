"""The media profile under the admin (ADR-0015).

A run's params.profile picks what it measures: the default robotics run
recruits the lidar client, the edge and the renderer; a media run recruits
the relay, the WebRTC sender and the receiver. The RAN sources serve both.
Two profiles share a zone (one gNB, one cell), so a start must reach only
the nodes of its run's profile -- a lidar client recording a media run
beside it would write a robotics CSV under a video run's id.
"""

from __future__ import annotations

import contextlib
import json

from mec_cast_admin import protocol as p
from mec_cast_admin.registry import Registry
from mec_cast_admin.store import Run
from mec_cast_admin.workflow import diagnose
from test_multizone import connect, recv, recv_type, report
from test_transport import _wait_manifest

MEDIA = (p.NodeType.RELAY, p.NodeType.SENDER, p.NodeType.RECEIVER)
HOSTS = {p.NodeType.RELAY: "mec01", p.NodeType.SENDER: "ue01", p.NodeType.RECEIVER: "ue02"}


def media_run(client, **params) -> str:
    response = client.post("/api/v1/runs", json={"params": {"profile": "media", **params}})
    assert response.status_code == 201, response.text
    return response.json()["run_id"]


def no_command(socket, node) -> None:
    """Nothing but keep-alives is waiting on this socket."""
    socket.send_text(json.dumps(p.build(p.MessageType.PONG, node_id=node)))
    for _ in range(5):
        frame = json.loads(socket.receive_text())
        assert frame["type"] != str(p.MessageType.COMMAND), frame
        if frame["type"] == str(p.MessageType.PING):
            return


class TestScope:
    def test_a_media_start_recruits_media_nodes_and_the_gnb_not_the_lidar(self, client):
        with contextlib.ExitStack() as stack:
            media = {t: connect(client, stack, t, HOSTS[t], "default") for t in MEDIA}
            gnb = connect(client, stack, p.NodeType.GNB, "gnb01", "default")
            lidar = connect(client, stack, p.NodeType.CLIENT, "ue03", "default")

            run_id = media_run(client, stream="cam", video_width=1280)
            client.post(f"/api/v1/runs/{run_id}/start")
            for _node, socket in [*media.values(), gnb]:
                _, payload = recv_type(socket, p.MessageType.COMMAND)
                assert payload.command == p.CommandType.RUN_START
                assert payload.run_id == run_id
                # The run's media knobs travel with the start.
                assert payload.args["stream"] == "cam"
            no_command(lidar[1], lidar[0])

    def test_a_robotics_start_does_not_recruit_a_sender(self, client):
        with contextlib.ExitStack() as stack:
            lidar = connect(client, stack, p.NodeType.CLIENT, "ue01", "default")
            sender = connect(client, stack, p.NodeType.SENDER, "ue02", "default")
            response = client.post("/api/v1/runs", json={})
            run_id = response.json()["run_id"]
            client.post(f"/api/v1/runs/{run_id}/start")
            recv_type(lidar[1], p.MessageType.COMMAND)
            no_command(sender[1], sender[0])

    def test_a_late_joiner_of_the_other_profile_is_not_offered_the_run(self, client):
        with contextlib.ExitStack() as stack:
            run_id = media_run(client)
            client.post(f"/api/v1/runs/{run_id}/start")
            for node_type, host, offered in (
                (p.NodeType.CLIENT, "ue03", False),
                (p.NodeType.RECEIVER, "ue02", True),
            ):
                node = p.node_id(node_type, host, 0)
                socket = stack.enter_context(client.websocket_connect("/ws/node"))
                socket.send_text(
                    json.dumps(
                        p.build(
                            p.MessageType.HELLO,
                            p.HelloPayload(
                                node_type=node_type, node_id=node, host=host, autostart=True
                            ),
                            node_id=node,
                        )
                    )
                )
                _, welcome = recv(socket)
                active = welcome.active_run
                assert (active is not None and active.run_id == run_id) is offered


class TestQuorum:
    def test_the_media_trio_brings_a_media_run_to_running(self, client, settings):
        with contextlib.ExitStack() as stack:
            nodes = {t: connect(client, stack, t, HOSTS[t], "default") for t in MEDIA}
            run_id = media_run(client)
            client.post(f"/api/v1/runs/{run_id}/start")
            for t, (node, socket) in nodes.items():
                report(socket, node, t, run_id)
            body = _wait_manifest(settings, run_id, lambda b: b.get("state") == "running")
            assert body["state"] == "running", "relay, sender and receiver are the quorum"

    def test_without_a_receiver_it_does_not_run(self, client, settings):
        with contextlib.ExitStack() as stack:
            nodes = {
                t: connect(client, stack, t, HOSTS[t], "default")
                for t in (p.NodeType.RELAY, p.NodeType.SENDER)
            }
            run_id = media_run(client)
            client.post(f"/api/v1/runs/{run_id}/start")
            for t, (node, socket) in nodes.items():
                report(socket, node, t, run_id)
            body = _wait_manifest(settings, run_id, lambda b: b.get("state") == "failed")
            assert body["state"] == "failed", "the start timed out without quorum"


class TestManifest:
    def test_run_json_records_what_the_media_run_measured(self, client, settings):
        reported = {
            p.NodeType.RELAY: {"public_addr": "10.45.0.1:8090", "out_leaf": "relay"},
            p.NodeType.SENDER: {
                "stream": "cam",
                "video": {"source": "synthetic", "width": 640, "height": 480, "fps": 30},
                "libwebrtc": "webrtc-c7f637a-4",
                "out_leaf": "sender",
            },
            p.NodeType.RECEIVER: {
                "stream": "cam",
                "libwebrtc": "webrtc-c7f637a-4",
                "out_leaf": "receiver",
            },
        }
        with contextlib.ExitStack() as stack:
            nodes = {t: connect(client, stack, t, HOSTS[t], "default") for t in MEDIA}
            run_id = media_run(client, stream="cam")
            client.post(f"/api/v1/runs/{run_id}/start")
            for t, (node, socket) in nodes.items():
                report(socket, node, t, run_id, params=reported[t])
            body = _wait_manifest(
                settings, run_id, lambda b: len(b.get("sites", {})) == 3 and b.get("media")
            )
        assert body["profile"] == "media"
        assert body["workload"]["modality"] == "video"
        assert body["media"] == {
            "stream": "cam",
            "video": {"source": "synthetic", "width": 640, "height": 480, "fps": 30},
            "libwebrtc": "webrtc-c7f637a-4",
            "relay": "10.45.0.1:8090",
        }
        assert {s["path"].rsplit("/", 1)[1] for s in body["sites"].values()} == {
            "relay",
            "sender",
            "receiver",
        }

    def test_a_robotics_run_has_no_media_section(self):
        run = Run(run_id="r", seq=1)
        manifest = run.to_manifest()
        assert manifest["profile"] == "robotics" and manifest["media"] is None


class TestParams:
    def test_an_unknown_profile_is_refused(self, client):
        response = client.post("/api/v1/runs", json={"params": {"profile": "audio"}})
        assert response.status_code == 422

    def test_a_bad_video_size_is_refused(self, client):
        response = client.post(
            "/api/v1/runs", json={"params": {"profile": "media", "video_width": 0}}
        )
        assert response.status_code == 422


def _media_node(reg, node_type, host, state=p.NodeState.RUNNING, **status):
    node = p.node_id(node_type, host, 0)
    reg.on_hello(p.HelloPayload(node_type=node_type, node_id=node, host=host))
    reg.on_status(node, p.StatusPayload(node_type=node_type, state=state, run_id="r1", **status))
    return node


def _active_media_run() -> Run:
    from mec_cast_admin.state import RunState

    return Run(run_id="r1", seq=1, params={"profile": "media"}, state=RunState.RUNNING)


def codes(findings) -> list[str]:
    return [f.code for f in findings]


class TestFindings:
    def test_a_media_run_lacks_no_lidar_and_names_its_own_missing_roles(self):
        reg = Registry()
        _media_node(reg, p.NodeType.RELAY, "mec01")
        found = codes(diagnose(reg, _active_media_run()))
        assert "WF_CLIENT_ABSENT" not in found and "WF_EDGE_ABSENT" not in found
        assert "WF_SENDER_ABSENT" in found and "WF_RECEIVER_ABSENT" in found
        assert "WF_RELAY_ABSENT" not in found

    def test_a_failed_webrtc_connection_is_an_error(self):
        reg = Registry()
        _media_node(reg, p.NodeType.SENDER, "ue01", params={"connection": "failed"})
        found = [
            f for f in diagnose(reg, _active_media_run()) if f.code == "WF_MEDIA_NOT_CONNECTED"
        ]
        assert [f.subject for f in found] == ["sender-ue01-0"]
        assert "RELAY_PUBLIC_IP" in found[0].remedy

    def test_a_receiver_on_an_unpublished_stream(self):
        reg = Registry()
        _media_node(reg, p.NodeType.SENDER, "ue01", params={"stream": "cam"})
        _media_node(reg, p.NodeType.RECEIVER, "ue02", params={"stream": "default"})
        found = [f for f in diagnose(reg, _active_media_run()) if f.code == "WF_STREAM_MISMATCH"]
        assert [f.subject for f in found] == ["receiver-ue02-0"]

    def test_frames_that_stop_at_the_relay(self):
        reg = Registry()
        sender = _media_node(reg, p.NodeType.SENDER, "ue01", counters={"frames": 10})
        relay = _media_node(reg, p.NodeType.RELAY, "mec01", counters={"frames": 5})
        reg.snapshot_counters()
        for node, node_type, frames in (
            (sender, p.NodeType.SENDER, 40),
            (relay, p.NodeType.RELAY, 5),
        ):
            reg.on_status(
                node,
                p.StatusPayload(
                    node_type=node_type,
                    state=p.NodeState.RUNNING,
                    run_id="r1",
                    counters={"frames": frames},
                ),
            )
        found = [f for f in diagnose(reg, _active_media_run()) if f.code == "WF_MEDIA_NO_FRAMES"]
        assert [f.subject for f in found] == [relay]
