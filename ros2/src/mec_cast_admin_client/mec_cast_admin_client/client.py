"""A WebSocket client for the admin service, safe to use from a ROS2 node.

Threading contract, which is the whole design:

* One background thread owns the socket and does all blocking I/O.
* **That thread never touches rclpy.** It moves dicts through queues and
  nothing else.
* The node drains inbound frames from a normal rclpy timer, so every callback,
  publisher, subscription and Recorder call stays on the executor thread.

That keeps ``rclpy.spin(node)`` single-threaded, which is how every node in
this repo already works — no MultiThreadedExecutor, no callback groups, no
reentrancy to reason about.

Outbound status uses a bounded queue that drops rather than blocks, the same
discipline as the telemetry recorder's ring: the control plane must never be
able to stall a measurement node.
"""

from __future__ import annotations

import json
import logging
import queue
import random
import threading

from . import protocol as p

logger = logging.getLogger(__name__)

DEFAULT_RETRY_S = 30.0
_OUTBOUND_CAPACITY = 64
_INBOUND_CAPACITY = 64


class AdminClient:
    """Subscribes to the admin service and relays commands to the node.

    Args:
        node_type: one of :class:`protocol.NodeType`.
        host: this machine's name; forms part of the stable ``node_id``.
        url: ``ws://host:8099/ws/node``. Empty disables the client entirely,
            which is what keeps the standalone env-``RUN_ID`` path unchanged.
        cell: the radio cell this node sits in. Empty unless the deployment
            declares one; the admin then treats it as the single default cell.
        instance: distinguishes several nodes of one type on one host.
        retry_s: reconnect interval. Injected so tests need not wait 30 s.
    """

    def __init__(
        self,
        *,
        node_type: str,
        host: str,
        url: str,
        cell: str = "",
        instance: int = 0,
        retry_s: float = DEFAULT_RETRY_S,
        version_sha: str = "",
        version_tag: str = "",
        pid: int = 0,
    ) -> None:
        self.node_type = node_type
        self.node_id = p.node_id(node_type, host, instance)
        self.host = host
        self.url = url
        self.retry_s = retry_s

        self._identity = {
            "node_type": node_type,
            "node_id": self.node_id,
            "host": host,
            "cell": cell,
            "pid": pid,
            "version_sha": version_sha,
            "version_tag": version_tag,
            "state": p.NodeState.IDLE,
            "run_id": None,
            "autostart": False,
            "params": {},
        }
        self._identity_lock = threading.Lock()

        self._outbound: queue.Queue[str] = queue.Queue(maxsize=_OUTBOUND_CAPACITY)
        self._inbound: queue.Queue[dict] = queue.Queue(maxsize=_INBOUND_CAPACITY)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._connected = threading.Event()
        self.dropped_outbound = 0

    # --- lifecycle --------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """False when no ``admin_url`` was configured: the standalone path."""
        return bool(self.url)

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name=f"admin-client-{self.node_id}", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    # --- called from the node's executor thread ---------------------------

    def update_identity(self, **fields) -> None:
        """Record what a fresh ``hello`` should say.

        The socket thread reads this when it (re)connects, so a node that
        reconnects mid-run announces the run it is actually recording rather
        than the state it had at boot.
        """
        with self._identity_lock:
            self._identity.update(fields)

    def publish_status(self, payload: dict) -> bool:
        """Queue a status frame. Never blocks; drops and counts when full."""
        return self._enqueue(p.build(p.MessageType.STATUS, payload, node_id=self.node_id))

    def publish_ack(self, in_reply_to: str, ok: bool = True, error: str | None = None) -> bool:
        return self._enqueue(
            p.build(
                p.MessageType.ACK,
                p.ack_payload(in_reply_to, ok=ok, error=error),
                node_id=self.node_id,
            )
        )

    def poll(self) -> list[dict]:
        """Every frame received since the last call, oldest first.

        Call this from a rclpy timer. Returns envelopes; the node reads
        ``["type"]`` and ``["payload"]``.
        """
        frames: list[dict] = []
        while True:
            try:
                frames.append(self._inbound.get_nowait())
            except queue.Empty:
                return frames

    def goodbye(self, *, reason: str = "shutdown", run_id: str | None = None,
                final_report: dict | None = None, timeout: float = 1.0) -> None:
        """Send a parting frame and stop.

        Best effort with a short deadline: shutdown must not hang because the
        admin is unreachable. ``docker stop`` gives us ten seconds; we use one.
        """
        if not self.enabled:
            return
        frame = p.build(
            p.MessageType.GOODBYE,
            p.goodbye_payload(reason=reason, run_id=run_id, final_report=final_report),
            node_id=self.node_id,
        )
        self._enqueue(frame)
        # Give the socket thread a moment to flush before it is torn down.
        deadline = threading.Event()
        deadline.wait(timeout if self.connected else 0.0)
        self.stop(timeout=timeout)

    # --- the socket thread ------------------------------------------------

    def _enqueue(self, frame: dict) -> bool:
        if not self.enabled:
            return False
        try:
            self._outbound.put_nowait(json.dumps(frame))
            return True
        except queue.Full:
            self.dropped_outbound += 1
            return False

    def _run(self) -> None:
        # Imported here so that a node with no admin_url never needs the
        # dependency present at all.
        from websockets.sync.client import connect

        while not self._stop.is_set():
            try:
                with connect(self.url, open_timeout=5) as socket:
                    self._connected.set()
                    logger.info("admin: connected to %s", self.url)
                    self._session(socket)
            except Exception as exc:
                logger.info("admin: %s unreachable (%s); retrying in %.0fs",
                            self.url, exc, self.retry_s)
            finally:
                self._connected.clear()
            if self._stop.is_set():
                break
            # Jitter so a fleet does not stampede when the admin restarts.
            self._stop.wait(self.retry_s * random.uniform(0.9, 1.1))

    def _session(self, socket) -> None:
        with self._identity_lock:
            identity = dict(self._identity)
        socket.send(json.dumps(
            p.build(p.MessageType.HELLO, p.hello_payload(**identity), node_id=self.node_id)))

        while not self._stop.is_set():
            # Drain anything the node queued while we were away or busy.
            while True:
                try:
                    socket.send(self._outbound.get_nowait())
                except queue.Empty:
                    break

            try:
                raw = socket.recv(timeout=0.2)
            except TimeoutError:
                continue
            except Exception:
                return  # closed; the outer loop reconnects

            try:
                envelope = p.parse(json.loads(raw))
            except (ValueError, p.ProtocolError) as exc:
                # A version mismatch must be loud and must not be fatal: the
                # node keeps retrying so an admin upgrade heals it.
                logger.warning("admin: rejected a frame: %s", exc)
                continue

            message_type = envelope["type"]
            if message_type == p.MessageType.PING:
                socket.send(json.dumps(p.build(p.MessageType.PONG, node_id=self.node_id)))
                continue
            if message_type == p.MessageType.ERROR:
                logger.warning("admin: %s", envelope["payload"].get("message"))
                continue
            if message_type in (p.MessageType.WELCOME, p.MessageType.COMMAND):
                try:
                    self._inbound.put_nowait(envelope)
                except queue.Full:
                    # The node is not draining. Dropping a command is bad, but
                    # blocking the socket thread is worse.
                    logger.warning("admin: inbound queue full, dropped %s", message_type)
