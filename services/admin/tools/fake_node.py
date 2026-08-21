#!/usr/bin/env python3
"""A protocol-speaking stub node, for developing the admin page without ROS2.

It implements exactly what a real node does — subscribe on startup, retry every
30 s, answer pings, act on commands, report status on every change, say goodbye
on exit — so the operator page can be built and demonstrated before any node is
touched.

    python tools/fake_node.py --type client --host ue01
    python tools/fake_node.py --type edge --host mec01 --autostart
    python tools/fake_node.py --type gnb --host gnb01 --autostart

Add ``--misbehave qos`` to make a client claim best_effort while the edge is
reliable, which is how the WF_QOS_MISMATCH diagnostic is exercised.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import random
import sys
import time

import websockets

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mec_cast_admin import protocol as p  # noqa: E402

RETRY_S = 30.0


class FakeNode:
    def __init__(self, args: argparse.Namespace) -> None:
        self.node_type = p.NodeType(args.type)
        self.node_id = p.node_id(self.node_type, args.host, args.instance)
        self.host = args.host
        self.autostart = args.autostart
        self.url = args.url
        self.misbehave = args.misbehave
        self.retry_s = args.retry

        self.state = p.NodeState.IDLE
        self.run_id: str | None = None
        self.counters: dict[str, int] = {}
        self.stopping = False

    # --- what a real node would actually be doing -------------------------

    def params(self) -> dict:
        if self.node_type is p.NodeType.CLIENT:
            reliability = "best_effort" if self.misbehave == "qos" else "reliable"
            return {
                "num_points": 30000,
                "rate_hz": 10.0,
                "seed": 42,
                "pattern": "uniform_cube",
                "reliability": reliability,
                "qos_depth": 10,
            }
        if self.node_type is p.NodeType.EDGE:
            return {"reliability": "reliable", "qos_depth": 10}
        return {"bind": "0.0.0.0:55555"}

    def peers(self) -> list[p.Peer]:
        if self.state is not p.NodeState.RUNNING:
            return []
        if self.node_type is p.NodeType.EDGE:
            if self.misbehave == "nopeer":
                return []
            return [p.Peer(peer_id="/mec_cast_lidar_client", last_seen_ns=p.now_ns())]
        if self.node_type is p.NodeType.GNB:
            return [p.Peer(peer_id="rnti=17921", detail={"pci": 1, "cqi": 15})]
        return []

    def tick_counters(self) -> None:
        if self.state is not p.NodeState.RUNNING:
            return
        if self.node_type is p.NodeType.CLIENT:
            self.counters["frames_published"] = self.counters.get("frames_published", 0) + 10
            self.counters["samples_dropped"] = self.counters.get("samples_dropped", 0)
        elif self.node_type is p.NodeType.EDGE:
            grow = 0 if self.misbehave == "noframes" else 10
            self.counters["frames"] = self.counters.get("frames", 0) + grow
        else:
            grow = 0 if self.misbehave == "silent" else 5
            self.counters["datagrams"] = self.counters.get("datagrams", 0) + grow
            self.counters["malformed"] = 0

    def status(self) -> p.StatusPayload:
        return p.StatusPayload(
            node_type=self.node_type,
            state=self.state,
            run_id=self.run_id,
            streaming=self.node_type is p.NodeType.CLIENT and self.state is p.NodeState.RUNNING,
            subscribed=self.node_type is p.NodeType.EDGE and self.state is p.NodeState.RUNNING,
            peers=self.peers(),
            params=self.params(),
            counters=self.counters,
            autostart=self.autostart,
        )

    # --- the connection ---------------------------------------------------

    async def run(self) -> None:
        while not self.stopping:
            try:
                await self._session()
            except Exception as exc:
                print(
                    f"[{self.node_id}] admin unreachable ({exc}); retrying in {self.retry_s:.0f}s",
                    flush=True,
                )
            if self.stopping:
                break
            # Jitter so a fleet does not stampede when the admin restarts.
            await asyncio.sleep(self.retry_s * random.uniform(0.9, 1.1))

    async def _session(self) -> None:
        async with websockets.connect(self.url) as socket:
            await self._send(
                socket,
                p.MessageType.HELLO,
                p.HelloPayload(
                    node_type=self.node_type,
                    node_id=self.node_id,
                    host=self.host,
                    pid=os.getpid(),
                    version=p.Version(sha=os.environ.get("VCS_REF", "fake000"), tag="fake"),
                    state=self.state,
                    run_id=self.run_id,
                    autostart=self.autostart,
                    params=self.params(),
                ),
            )
            print(f"[{self.node_id}] connected to {self.url}", flush=True)
            reporter = asyncio.create_task(self._report_loop(socket))
            try:
                async for raw in socket:
                    await self._on_frame(socket, json.loads(raw))
            finally:
                reporter.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await reporter

    async def _on_frame(self, socket, raw: dict) -> None:
        try:
            envelope, payload = p.parse(raw)
        except p.ProtocolError as exc:
            print(f"[{self.node_id}] rejected a frame: {exc}", flush=True)
            return

        if envelope.type is p.MessageType.PING:
            await self._send(socket, p.MessageType.PONG)
        elif envelope.type is p.MessageType.WELCOME:
            active = payload.active_run
            if active and self.autostart:
                self._start(active.run_id)
                await self._send(socket, p.MessageType.STATUS, self.status())
        elif envelope.type is p.MessageType.COMMAND:
            await self._on_command(socket, envelope, payload)
        elif envelope.type is p.MessageType.ERROR:
            print(f"[{self.node_id}] admin says: {payload.message}", flush=True)

    async def _on_command(self, socket, envelope, payload: p.CommandPayload) -> None:
        if payload.command in (p.CommandType.RUN_START, p.CommandType.STREAM_START):
            self._start(payload.run_id)
        elif payload.command in (p.CommandType.RUN_STOP, p.CommandType.STREAM_STOP):
            self._stop()
        await self._send(
            socket, p.MessageType.ACK, p.AckPayload(in_reply_to=envelope.msg_id, ok=True)
        )
        await self._send(socket, p.MessageType.STATUS, self.status())
        print(f"[{self.node_id}] {payload.command} -> {self.state}", flush=True)

    def _start(self, run_id: str | None) -> None:
        self.run_id = run_id
        self.state = p.NodeState.RUNNING
        self.counters = {}

    def _stop(self) -> None:
        self.state = p.NodeState.IDLE

    async def _report_loop(self, socket) -> None:
        while True:
            await asyncio.sleep(2.0)
            before = dict(self.counters)
            self.tick_counters()
            if self.counters != before or self.state is not p.NodeState.RUNNING:
                await self._send(socket, p.MessageType.STATUS, self.status())

    async def _send(self, socket, message_type: p.MessageType, payload=None) -> None:
        await socket.send(json.dumps(p.build(message_type, payload, node_id=self.node_id)))

    async def goodbye(self) -> None:
        self.stopping = True
        with contextlib.suppress(Exception):
            async with websockets.connect(self.url, open_timeout=1) as socket:
                await self._send(
                    socket,
                    p.MessageType.GOODBYE,
                    p.GoodbyePayload(
                        reason="sigterm",
                        run_id=self.run_id,
                        final_report={
                            "samples_written": self.counters.get("frames_published", 0)
                            or self.counters.get("frames", 0)
                            or self.counters.get("datagrams", 0),
                            "samples_dropped": 0,
                        },
                    ),
                )


def main() -> int:
    parser = argparse.ArgumentParser(description="A fake mec-cast node.")
    parser.add_argument("--type", choices=[str(t) for t in p.NodeType], required=True)
    parser.add_argument("--host", default=f"fake{int(time.time()) % 100:02d}")
    parser.add_argument("--instance", type=int, default=0)
    parser.add_argument("--url", default="ws://127.0.0.1:8099/ws/node")
    parser.add_argument("--autostart", action="store_true")
    parser.add_argument("--retry", type=float, default=RETRY_S)
    parser.add_argument(
        "--misbehave",
        choices=["qos", "nopeer", "noframes", "silent"],
        default=None,
        help="Provoke a specific diagnostic.",
    )
    args = parser.parse_args()

    node = FakeNode(args)
    try:
        asyncio.run(node.run())
    except KeyboardInterrupt:
        asyncio.run(node.goodbye())
        print(f"[{node.node_id}] goodbye", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
