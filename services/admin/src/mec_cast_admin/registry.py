"""Who is connected, what they last said, and whether they are still alive.

The registry is the admin's view of the fleet. It is deliberately dumb: it
records what nodes report and when, and answers questions about liveness. It
decides nothing about runs — :mod:`state` does that, and :mod:`workflow` reads
this to explain what is wrong.

Liveness is by last-heard-from rather than by socket state, because a socket
can stay open on a wedged process. Any inbound frame counts, so a node emitting
frequent status need not answer pings separately.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .protocol import HelloPayload, NodeState, NodeType, StatusPayload


def _now() -> float:
    """Monotonic seconds. Liveness must not care that the wall clock moved."""
    return time.monotonic()


@dataclass
class NodeRecord:
    """One node, whether or not it is currently connected."""

    node_id: str
    node_type: NodeType
    host: str = ""
    #: Radio cell, as the node reported it. Empty means the node did not say,
    #: which is every deployment that has not declared a topology — treated
    #: as the single default cell everywhere it is read.
    cell: str = ""
    pid: int = 0
    version_sha: str = ""
    version_tag: str = ""
    autostart: bool = False

    state: NodeState = NodeState.IDLE
    run_id: str | None = None
    streaming: bool = False
    subscribed: bool = False
    peers: list[dict[str, Any]] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)
    last_error: str | None = None

    #: The address this node's control socket connects FROM, as the admin
    #: sees it. Used to repair a viewer link the node could only describe as
    #: "localhost": a node knows which PORT it serves on, but not which
    #: address an operator's browser can reach it at, and VIEWER_HOST is
    #: routinely unset. The admin does know, because the node dialled it.
    address: str = ""

    connected: bool = True
    last_seen: float = field(default_factory=_now)
    first_seen: float = field(default_factory=_now)
    streaming_since: float | None = None
    """When this node last started streaming. A client that has only just
    begun has no peer yet for innocent reasons, so diagnostics need a grace
    window measured from here rather than from connection time."""
    departed: bool = False
    """True once the node said goodbye. Distinguishes a clean exit from a
    crash, which is the difference between a warning and an alarm."""

    #: Counter snapshots from the previous diagnostics pass, so `workflow` can
    #: tell "frames rising" from "frames flat" without keeping its own history.
    previous_counters: dict[str, int] = field(default_factory=dict)

    #: admin CLOCK_REALTIME minus this node's, nanoseconds, from the `ts_ns`
    #: every envelope already carries. Positive means the node is behind.
    #:
    #: This is the cheapest clock check in the platform and it was being
    #: thrown away: `ts_ns` is the same CLOCK_REALTIME the recorder stamps
    #: samples with, so a node seconds adrift here produces samples wrong by
    #: exactly that much. The per-host checks cannot see it (each host is
    #: perfect against its own reference) and `verify-ptp.sh --peer` can, but
    #: only when a human remembers to run it. This runs on every frame.
    #:
    #: It carries one-way network delay as a positive bias — sub-millisecond
    #: on the management LAN, against a threshold in seconds.
    clock_offset_ns: int | None = None

    def is_online(self, timeout_s: float, now: float | None = None) -> bool:
        now = _now() if now is None else now
        return self.connected and (now - self.last_seen) <= timeout_s

    def streaming_for(self, seconds: float) -> bool:
        """True once this node has been streaming for at least ``seconds``."""
        if self.streaming_since is None:
            return False
        return (_now() - self.streaming_since) >= seconds

    def _usable_params(self) -> dict[str, Any]:
        """`params`, with a loopback viewer link repointed at this node.

        The render node builds `viewer_url` from `viewer_host`, which defaults
        to `localhost` because a process cannot know which of its addresses an
        operator's browser can reach. Served unaltered, the admin's "viewer"
        button sends the operator to their OWN machine.
        """
        params = dict(self.params)
        url = params.get("viewer_url")
        if url and self.address:
            for loopback in ("localhost", "127.0.0.1", "[::1]"):
                if f"//{loopback}:" in url:
                    params["viewer_url"] = (
                        url.replace(f"//{loopback}:", f"//{self.address}:")
                        .replace(
                            # The query carries the stream address too, and it is
                            # fetched by the same browser -- repairing only the
                            # page host gives a viewer that loads and never fills.
                            f"%2F%2F{loopback}%3A",
                            f"%2F%2F{self.address}%3A",
                        )
                        .replace(f"//{loopback}:", f"//{self.address}:")
                    )
                    break
        return params

    def to_dict(self, timeout_s: float) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "node_type": str(self.node_type),
            "host": self.host,
            "cell": self.cell,
            "version": {"sha": self.version_sha, "tag": self.version_tag},
            "state": str(self.state),
            "run_id": self.run_id,
            "streaming": self.streaming,
            "subscribed": self.subscribed,
            "peers": self.peers,
            "params": self._usable_params(),
            "counters": self.counters,
            "autostart": self.autostart,
            "last_error": self.last_error,
            "online": self.is_online(timeout_s),
            "departed": self.departed,
            "age_s": round(_now() - self.first_seen, 1),
            "silent_for_s": round(_now() - self.last_seen, 1),
            "clock_offset_ns": self.clock_offset_ns,
            "address": self.address,
        }


class Registry:
    """The connected fleet, keyed by the node's stable id.

    A node that reconnects reuses its record rather than creating a second one
    — that is the whole point of `node_id` being stable across restarts.
    """

    def __init__(self, offline_timeout_s: float = 30.0) -> None:
        self._nodes: dict[str, NodeRecord] = {}
        self._offline_timeout_s = offline_timeout_s

    # --- mutation ---------------------------------------------------------

    def on_hello(self, hello: HelloPayload) -> NodeRecord:
        record = self._nodes.get(hello.node_id)
        if record is None:
            record = NodeRecord(node_id=hello.node_id, node_type=hello.node_type)
            self._nodes[hello.node_id] = record

        record.node_type = hello.node_type
        record.host = hello.host
        record.cell = hello.cell
        record.pid = hello.pid
        record.version_sha = hello.version.sha
        record.version_tag = hello.version.tag
        record.state = hello.state
        record.run_id = hello.run_id
        record.autostart = hello.autostart
        record.params = dict(hello.params)
        record.connected = True
        record.departed = False
        record.last_seen = _now()
        return record

    def on_status(self, node_id: str, status: StatusPayload) -> NodeRecord | None:
        record = self._nodes.get(node_id)
        if record is None:
            return None
        record.state = status.state
        record.run_id = status.run_id
        if status.streaming and not record.streaming:
            record.streaming_since = _now()
        elif not status.streaming:
            record.streaming_since = None
        record.streaming = status.streaming
        record.subscribed = status.subscribed
        record.peers = [p.model_dump(mode="json") for p in status.peers]
        if status.params:
            record.params = dict(status.params)
        record.counters = dict(status.counters)
        record.last_error = status.last_error
        record.last_seen = _now()
        return record

    def note_clock(self, node_id: str, node_ts_ns: int, admin_ns: int) -> None:
        """Record how far this node's clock is from the admin's.

        Called for every node-originated frame, because every one of them
        carries `ts_ns` and the comparison costs a subtraction.
        """
        record = self._nodes.get(node_id)
        if record is not None and node_ts_ns:
            record.clock_offset_ns = admin_ns - int(node_ts_ns)

    def touch(self, node_id: str) -> None:
        """Any inbound frame proves the node is alive."""
        record = self._nodes.get(node_id)
        if record is not None:
            record.last_seen = _now()

    def on_goodbye(self, node_id: str) -> NodeRecord | None:
        record = self._nodes.get(node_id)
        if record is None:
            return None
        record.connected = False
        record.departed = True
        record.state = NodeState.IDLE
        record.streaming = False
        record.subscribed = False
        record.last_seen = _now()
        return record

    def on_disconnect(self, node_id: str) -> None:
        """The socket closed without a goodbye — a crash, a kill, or a network
        partition. The record is kept so the run can name what went missing."""
        record = self._nodes.get(node_id)
        if record is not None:
            record.connected = False

    def forget(self, node_id: str) -> None:
        self._nodes.pop(node_id, None)

    def snapshot_counters(self) -> None:
        """Roll `counters` into `previous_counters` for rate-of-change checks."""
        for record in self._nodes.values():
            record.previous_counters = dict(record.counters)

    # --- queries ----------------------------------------------------------

    def get(self, node_id: str) -> NodeRecord | None:
        return self._nodes.get(node_id)

    def all(self) -> list[NodeRecord]:
        return sorted(self._nodes.values(), key=lambda r: r.node_id)

    def online(self, node_type: NodeType | None = None) -> list[NodeRecord]:
        return [
            r
            for r in self.all()
            if r.is_online(self._offline_timeout_s)
            and (node_type is None or r.node_type == node_type)
        ]

    def offline(self) -> list[NodeRecord]:
        return [r for r in self.all() if not r.is_online(self._offline_timeout_s)]

    def participants_of(self, run_id: str) -> list[NodeRecord]:
        return [r for r in self.all() if r.run_id == run_id]

    def to_dict(self) -> list[dict[str, Any]]:
        return [r.to_dict(self._offline_timeout_s) for r in self.all()]
