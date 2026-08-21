"""The node side of the admin wire protocol.

A deliberate re-implementation of ``services/admin/src/mec_cast_admin/protocol.py``
using only the standard library. The service can afford pydantic; the ROS image
installs only the telemetry wheel and ``websockets``, and adding a validation
framework to a node to send nine message shapes would not pay for itself.

The two implementations are held together by
``services/admin/tests/vectors.json``, which both test suites read. If this
file and the service's disagree about a field name, that fixture fails.

Keep the two in step. See ADR-0007.
"""

from __future__ import annotations

import time
import uuid

PROTOCOL_VERSION = 1


class NodeType:
    CLIENT = "client"
    EDGE = "edge"
    GNB = "gnb"


class MessageType:
    # node -> admin
    HELLO = "hello"
    STATUS = "status"
    ACK = "ack"
    PONG = "pong"
    GOODBYE = "goodbye"
    # admin -> node
    WELCOME = "welcome"
    COMMAND = "command"
    PING = "ping"
    ERROR = "error"


class CommandType:
    RUN_START = "run.start"
    RUN_STOP = "run.stop"
    STREAM_START = "stream.start"
    STREAM_STOP = "stream.stop"
    STATUS_REPORT = "status.report"


class NodeState:
    IDLE = "idle"
    RUNNING = "running"
    ERROR = "error"


class ProtocolError(ValueError):
    """A frame that cannot be interpreted under this protocol version."""


def now_ns() -> int:
    """CLOCK_REALTIME nanoseconds — the clock the recorder stamps samples with.

    ``mec_cast_telemetry.now_ns()`` would be the same value, but importing the
    telemetry wheel here would tie the control plane to the measurement spine
    for one function. ADR-0002 keeps that boundary.
    """
    return time.time_ns()


def node_id(node_type: str, host: str, instance: int = 0) -> str:
    """``<node_type>-<host>-<instance>``, stable across restarts."""
    return f"{node_type}-{host}-{instance}"


def build(message_type: str, payload: dict | None = None, node_id: str | None = None) -> dict:
    """A complete envelope, ready for ``json.dumps``."""
    return {
        "v": PROTOCOL_VERSION,
        "type": message_type,
        "msg_id": str(uuid.uuid4()),
        "ts_ns": now_ns(),
        "node_id": node_id,
        "payload": dict(payload or {}),
    }


def parse(raw: object) -> dict:
    """Validate an inbound envelope.

    Returns the envelope as a dict. Payload fields are read by the caller:
    unknown ones are ignored, which is what lets a newer admin add a field
    without breaking an older node.

    Raises:
        ProtocolError: on a version mismatch or a structurally invalid frame.
    """
    if not isinstance(raw, dict):
        raise ProtocolError("frame is not a JSON object")
    version = raw.get("v")
    if version != PROTOCOL_VERSION:
        raise ProtocolError(
            f"unsupported protocol version {version!r}; this node speaks {PROTOCOL_VERSION}"
        )
    message_type = raw.get("type")
    if not isinstance(message_type, str):
        raise ProtocolError("frame has no type")
    payload = raw.get("payload")
    if payload is not None and not isinstance(payload, dict):
        raise ProtocolError("payload is not an object")
    raw.setdefault("payload", {})
    return raw


def hello_payload(
    *,
    node_type: str,
    node_id: str,
    host: str,
    pid: int,
    version_sha: str = "",
    version_tag: str = "",
    state: str = NodeState.IDLE,
    run_id: str | None = None,
    autostart: bool = False,
    params: dict | None = None,
) -> dict:
    return {
        "node_type": node_type,
        "node_id": node_id,
        "host": host,
        "pid": pid,
        "version": {"sha": version_sha, "tag": version_tag},
        "state": state,
        "run_id": run_id,
        "autostart": autostart,
        "params": params or {},
    }


def status_payload(
    *,
    node_type: str,
    state: str,
    run_id: str | None = None,
    streaming: bool = False,
    subscribed: bool = False,
    peers: list | None = None,
    params: dict | None = None,
    counters: dict | None = None,
    autostart: bool = False,
    last_error: str | None = None,
    report: dict | None = None,
) -> dict:
    return {
        "node_type": node_type,
        "state": state,
        "run_id": run_id,
        "streaming": streaming,
        "subscribed": subscribed,
        "peers": peers or [],
        "params": params or {},
        "counters": counters or {},
        "autostart": autostart,
        "last_error": last_error,
        # The recorder's final accounting, sent once after a run stops. A run
        # stopped by the admin leaves the node alive, so this cannot wait for
        # the goodbye frame.
        "report": report or {},
    }


def peer(peer_id: str, *, first_seen_ns: int | None = None,
         last_seen_ns: int | None = None, detail: dict | None = None) -> dict:
    return {
        "peer_id": peer_id,
        "first_seen_ns": first_seen_ns,
        "last_seen_ns": last_seen_ns,
        "detail": detail or {},
    }


def goodbye_payload(
    *, reason: str = "shutdown", run_id: str | None = None, final_report: dict | None = None
) -> dict:
    return {"reason": reason, "run_id": run_id, "final_report": final_report or {}}


def ack_payload(in_reply_to: str, ok: bool = True, error: str | None = None) -> dict:
    return {"in_reply_to": in_reply_to, "ok": ok, "error": error}
