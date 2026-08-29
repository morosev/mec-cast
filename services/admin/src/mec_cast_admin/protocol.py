"""The node/admin wire protocol.

One versioned JSON envelope carries every frame in both directions::

    {"v": 1, "type": "hello", "msg_id": "<uuid4>", "ts_ns": 1712345678901234567,
     "node_id": "edge-mec01-0", "payload": { }}

``ts_ns`` is CLOCK_REALTIME nanoseconds — the same clock the recorder stamps
samples with, so admin events and measurements share one timeline.

Three implementations must agree on this file: the service, the Python node
client (``ros2/src/mec_cast_admin_client``) and the Rust node client
(``ran/collector/src/admin.rs``). ``tests/vectors.json`` is the shared fixture
that keeps them honest; the Rust test reads the same file.

Compatibility rule: an envelope whose ``v`` differs is rejected outright, but
*payloads* ignore unknown fields. Within a version a newer node may add a field
without breaking an older admin; across versions nothing is assumed.
"""

from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

PROTOCOL_VERSION = 1


class NodeType(StrEnum):
    CLIENT = "client"
    EDGE = "edge"
    GNB = "gnb"
    RENDER = "render"


class MessageType(StrEnum):
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


NODE_TO_ADMIN = frozenset(
    {MessageType.HELLO, MessageType.STATUS, MessageType.ACK, MessageType.PONG, MessageType.GOODBYE}
)
ADMIN_TO_NODE = frozenset(
    {MessageType.WELCOME, MessageType.COMMAND, MessageType.PING, MessageType.ERROR}
)


class CommandType(StrEnum):
    RUN_START = "run.start"
    RUN_STOP = "run.stop"
    STREAM_START = "stream.start"
    STREAM_STOP = "stream.stop"
    STATUS_REPORT = "status.report"


class NodeState(StrEnum):
    """What a node reports about itself. Distinct from a *run's* state."""

    IDLE = "idle"
    RUNNING = "running"
    ERROR = "error"


class ProtocolError(ValueError):
    """A frame that cannot be interpreted under this protocol version."""


def now_ns() -> int:
    """CLOCK_REALTIME nanoseconds, matching ``mec_cast_telemetry.now_ns()``."""
    return time.time_ns()


def new_msg_id() -> str:
    return str(uuid.uuid4())


def node_id(node_type: NodeType | str, host: str, instance: int = 0) -> str:
    """``<node_type>-<host>-<instance>``.

    Stable across restarts, which is what makes reconnection idempotent and
    lets an operator address one node out of many.
    """
    return f"{node_type}-{host}-{instance}"


class _Payload(BaseModel):
    """Payloads tolerate unknown fields; see the compatibility rule above."""

    model_config = ConfigDict(extra="ignore")


class Envelope(BaseModel):
    model_config = ConfigDict(extra="ignore")

    v: int
    type: MessageType
    msg_id: str
    ts_ns: int
    node_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


# --- node -> admin payloads ------------------------------------------------


class Version(_Payload):
    sha: str = ""
    tag: str = ""


class HelloPayload(_Payload):
    node_type: NodeType
    node_id: str
    host: str = ""
    #: Which radio cell this node belongs to. Empty means "not declared",
    #: which is every deployment that has not written a topology file — the
    #: admin then treats it as the single default cell. Additive under the
    #: `extra="ignore"` rule, so a node that predates this field still
    #: connects to a newer admin and vice versa; no version bump.
    cell: str = ""
    pid: int = 0
    version: Version = Field(default_factory=Version)
    state: NodeState = NodeState.IDLE
    run_id: str | None = None
    autostart: bool = False
    params: dict[str, Any] = Field(default_factory=dict)


class Peer(_Payload):
    """A counterpart this node is exchanging data with.

    For the edge, one publisher on the cloud topic. For the gNB, one UE from
    the last srsRAN metrics datagram.
    """

    peer_id: str
    first_seen_ns: int | None = None
    last_seen_ns: int | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class StatusPayload(_Payload):
    """One shape for all three node types.

    A single model rather than a discriminated union: every field below is
    meaningful for at least two of the three, and the diagnostics in
    ``workflow.py`` read them uniformly. Node-type specifics live in
    ``params``, ``counters`` and ``peers`` rather than in the schema.
    """

    node_type: NodeType
    state: NodeState
    run_id: str | None = None
    streaming: bool = False
    subscribed: bool = False
    peers: list[Peer] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)
    counters: dict[str, int] = Field(default_factory=dict)
    autostart: bool = False
    last_error: str | None = None
    report: dict[str, Any] = Field(
        default_factory=dict,
        description="The recorder's final accounting, sent once in the status that "
        "follows a stop. Empty at all other times. A run stopped from the admin "
        "leaves the node process alive, so this cannot wait for the goodbye frame.",
    )


class AckPayload(_Payload):
    in_reply_to: str
    ok: bool = True
    error: str | None = None


class GoodbyePayload(_Payload):
    reason: str = "shutdown"
    run_id: str | None = None
    final_report: dict[str, Any] = Field(default_factory=dict)


# --- admin -> node payloads ------------------------------------------------


class ActiveRun(_Payload):
    run_id: str
    label: str = ""
    params: dict[str, Any] = Field(default_factory=dict)


class WelcomePayload(_Payload):
    server_version: str
    protocol: int = PROTOCOL_VERSION
    keepalive_s: float = 10.0
    offline_timeout_s: float = 30.0
    active_run: ActiveRun | None = None


class CommandPayload(_Payload):
    command: CommandType
    target_node_type: NodeType | None = None
    run_id: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)


class ErrorPayload(_Payload):
    code: str
    message: str
    supported_protocol: int = PROTOCOL_VERSION


_PAYLOAD_MODELS: dict[MessageType, type[_Payload] | None] = {
    MessageType.HELLO: HelloPayload,
    MessageType.STATUS: StatusPayload,
    MessageType.ACK: AckPayload,
    MessageType.PONG: None,
    MessageType.GOODBYE: GoodbyePayload,
    MessageType.WELCOME: WelcomePayload,
    MessageType.COMMAND: CommandPayload,
    MessageType.PING: None,
    MessageType.ERROR: ErrorPayload,
}


def build(
    message_type: MessageType,
    payload: _Payload | dict[str, Any] | None = None,
    *,
    node_id: str | None = None,
) -> dict[str, Any]:
    """Build a complete envelope ready for ``json.dumps``."""
    if isinstance(payload, BaseModel):
        body = payload.model_dump(mode="json", exclude_none=False)
    else:
        body = dict(payload or {})
    return {
        "v": PROTOCOL_VERSION,
        "type": str(message_type),
        "msg_id": new_msg_id(),
        "ts_ns": now_ns(),
        "node_id": node_id,
        "payload": body,
    }


def parse(raw: dict[str, Any]) -> tuple[Envelope, _Payload | None]:
    """Validate an envelope and its payload.

    Raises:
        ProtocolError: on a version mismatch, an unknown type, or a payload
            that does not validate. The caller answers with an ``error`` frame
            rather than closing silently — a version mismatch must be visible.
    """
    if not isinstance(raw, dict):
        raise ProtocolError("frame is not a JSON object")

    version = raw.get("v")
    if version != PROTOCOL_VERSION:
        raise ProtocolError(
            f"unsupported protocol version {version!r}; this service speaks {PROTOCOL_VERSION}"
        )

    try:
        envelope = Envelope.model_validate(raw)
    except Exception as exc:  # pydantic ValidationError
        raise ProtocolError(f"malformed envelope: {exc}") from exc

    model = _PAYLOAD_MODELS.get(envelope.type)
    if model is None:
        return envelope, None
    try:
        return envelope, model.model_validate(envelope.payload)
    except Exception as exc:
        raise ProtocolError(f"malformed {envelope.type} payload: {exc}") from exc
