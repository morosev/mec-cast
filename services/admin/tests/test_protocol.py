"""The wire protocol: envelope validation, versioning, payload round-trips."""

from __future__ import annotations

import json
import pathlib

import pytest

from mec_cast_admin import protocol as p

VECTORS = pathlib.Path(__file__).parent / "vectors.json"


class TestEnvelope:
    def test_build_produces_a_complete_envelope(self):
        env = p.build(p.MessageType.PING, node_id="edge-mec01-0")
        assert env["v"] == p.PROTOCOL_VERSION
        assert env["type"] == "ping"
        assert env["node_id"] == "edge-mec01-0"
        assert env["ts_ns"] > 0
        assert env["msg_id"]

    def test_build_output_is_json_serialisable(self):
        # Every enum and nested model must survive json.dumps untouched.
        env = p.build(
            p.MessageType.STATUS,
            p.StatusPayload(node_type=p.NodeType.EDGE, state=p.NodeState.RUNNING),
            node_id="edge-mec01-0",
        )
        assert json.loads(json.dumps(env))["payload"]["node_type"] == "edge"

    def test_msg_ids_are_unique(self):
        assert p.build(p.MessageType.PING)["msg_id"] != p.build(p.MessageType.PING)["msg_id"]


class TestVersioning:
    @pytest.mark.parametrize("version", [0, 2, "1", None])
    def test_a_foreign_version_is_rejected(self, version):
        frame = p.build(p.MessageType.PING)
        frame["v"] = version
        with pytest.raises(p.ProtocolError) as excinfo:
            p.parse(frame)
        # The message must name what we speak, or the operator cannot act.
        assert str(p.PROTOCOL_VERSION) in str(excinfo.value)

    def test_a_missing_version_is_rejected(self):
        with pytest.raises(p.ProtocolError):
            p.parse({"type": "ping", "msg_id": "x", "ts_ns": 1})

    def test_a_non_object_frame_is_rejected(self):
        with pytest.raises(p.ProtocolError):
            p.parse(["not", "an", "object"])


class TestPayloads:
    def test_hello_round_trips(self):
        sent = p.HelloPayload(
            node_type=p.NodeType.CLIENT,
            node_id="client-ue01-0",
            host="ue01",
            pid=42,
            state=p.NodeState.IDLE,
            autostart=False,
            params={"rate_hz": 10.0, "num_points": 30000},
        )
        env, got = p.parse(p.build(p.MessageType.HELLO, sent, node_id=sent.node_id))
        assert env.type is p.MessageType.HELLO
        assert got == sent

    def test_status_round_trips_with_peers(self):
        sent = p.StatusPayload(
            node_type=p.NodeType.EDGE,
            state=p.NodeState.RUNNING,
            run_id="0190d1f2-0000-7000-8000-000000000000",
            subscribed=True,
            peers=[p.Peer(peer_id="/mec_cast_lidar_client", detail={"frames": 12})],
            counters={"frames": 1200, "seq_gaps": 2},
        )
        _, got = p.parse(p.build(p.MessageType.STATUS, sent))
        assert got == sent
        assert got.peers[0].detail["frames"] == 12

    def test_command_round_trips(self):
        sent = p.CommandPayload(
            command=p.CommandType.RUN_START,
            target_node_type=p.NodeType.CLIENT,
            run_id="0190d1f2-0000-7000-8000-000000000000",
            args={"rate_hz": 5.0},
        )
        _, got = p.parse(p.build(p.MessageType.COMMAND, sent))
        assert got == sent

    def test_goodbye_carries_the_final_report(self):
        sent = p.GoodbyePayload(
            reason="sigterm", final_report={"samples_written": 1421, "samples_dropped": 0}
        )
        _, got = p.parse(p.build(p.MessageType.GOODBYE, sent))
        assert got.final_report["samples_written"] == 1421

    def test_ping_and_pong_carry_no_payload_model(self):
        for message_type in (p.MessageType.PING, p.MessageType.PONG):
            env, got = p.parse(p.build(message_type))
            assert got is None
            assert env.type is message_type

    def test_unknown_payload_fields_are_ignored_within_a_version(self):
        # Forward compatibility: a newer node may add a field.
        frame = p.build(
            p.MessageType.STATUS,
            p.StatusPayload(node_type=p.NodeType.GNB, state=p.NodeState.RUNNING),
        )
        frame["payload"]["invented_later"] = {"nested": True}
        _, got = p.parse(frame)
        assert got.node_type is p.NodeType.GNB

    def test_a_malformed_payload_is_rejected_naming_the_type(self):
        frame = p.build(p.MessageType.HELLO, {"node_type": "toaster"})
        with pytest.raises(p.ProtocolError) as excinfo:
            p.parse(frame)
        assert "hello" in str(excinfo.value)

    def test_an_unknown_message_type_is_rejected(self):
        frame = p.build(p.MessageType.PING)
        frame["type"] = "sudo"
        with pytest.raises(p.ProtocolError):
            p.parse(frame)


class TestDirections:
    def test_every_message_type_has_exactly_one_direction(self):
        assert set(p.MessageType) == p.NODE_TO_ADMIN | p.ADMIN_TO_NODE
        assert not (p.NODE_TO_ADMIN & p.ADMIN_TO_NODE)


class TestNodeId:
    def test_node_id_is_stable_and_addressable(self):
        assert p.node_id(p.NodeType.EDGE, "mec01") == "edge-mec01-0"
        assert p.node_id(p.NodeType.CLIENT, "ue01", 2) == "client-ue01-2"

    def test_the_same_inputs_give_the_same_id_across_restarts(self):
        assert p.node_id("gnb", "gnb01", 0) == p.node_id(p.NodeType.GNB, "gnb01", 0)


class TestSharedVectors:
    """The fixture the Rust client's test reads too, so the two cannot drift."""

    @staticmethod
    def _load() -> dict:
        # Keys beginning with "_" are notes for humans, not frames.
        raw = json.loads(VECTORS.read_text())
        return {k: v for k, v in raw.items() if not k.startswith("_")}

    def test_every_vector_parses(self):
        vectors = self._load()
        assert vectors, "vectors.json must not be empty"
        for name, frame in vectors.items():
            env, _ = p.parse(frame)
            assert str(env.type) == frame["type"], name

    def test_the_vectors_cover_every_message_type(self):
        covered = {frame["type"] for frame in self._load().values()}
        assert covered == {str(t) for t in p.MessageType}
