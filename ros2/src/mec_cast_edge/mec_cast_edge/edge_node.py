"""mec-cast MEC edge subscriber.

Receives `CloudWithTelemetry`, stamps `recv_ns` as the very first action in
the callback, runs a deterministic processing step (centroid + occupied
voxel count), stamps `process_done_ns`, and feeds the sample to the shared
telemetry recorder (per-frame CSV + periodic snapshots to the logging
service).

Parameters:
    reliability      (str,  default reliable)  must match the publisher's
    qos_depth        (int,  default 10)
    admin_url        (str,  default $ADMIN_URL)  empty = standalone
    admin_autostart  (bool, default true)  join the active run on connect
    admin_instance   (int,  default 0)  distinguishes edges on one host

Environment:
    RUN_ID       experiment run id (must match the publisher's for joins)
    LOGGING_URL  mec-cast-logging-service base URL (optional)
    RUNS_DIR     base directory for per-run output (default ./runs)
    ADMIN_URL    admin service, e.g. ws://admin:8099/ws/node (optional)

With no `admin_url` the node behaves exactly as it always has: one Recorder
built at startup from the environment's RUN_ID, one subscription, recording
until the process stops. With an admin, the run lifecycle moves to the control
plane and the Recorder is built per run — see ADR-0007.
"""

import os
import signal
import socket

import numpy as np
import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy

import mec_cast_telemetry as tel
from mec_cast_admin_client import AdminClient
from mec_cast_admin_client import protocol as ap
from mec_cast_msgs.msg import CloudWithTelemetry

SITE_EDGE = 1
VOXEL_SIZE = 0.5
CLOUD_TOPIC = "mec_cast/cloud"

#: How often commands from the admin are applied. Everything the admin asks
#: for happens on the executor thread, in this callback.
ADMIN_POLL_S = 0.1
#: How often status is pushed when nothing else changed. Doubles as the
#: liveness signal, so the admin need not rely on pong alone.
STATUS_PERIOD_S = 2.0


def cloud_qos(reliability: str, depth: int) -> QoSProfile:
    """QoS for the point-cloud subscription. Must match the publisher's.

    Deliberately duplicated from the publisher rather than imported: these
    two nodes run on different hosts (UE and MEC edge), so a code-level
    dependency between them would be a lie about the deployment. The
    compatibility rule is the contract, and it is one-directional — a
    BEST_EFFORT publisher with a RELIABLE subscriber matches nothing and
    delivers no data at all, silently.
    """
    if reliability not in ("reliable", "best_effort"):
        raise ValueError(
            f"reliability must be 'reliable' or 'best_effort', got {reliability!r}"
        )
    return QoSProfile(
        reliability=(
            ReliabilityPolicy.RELIABLE
            if reliability == "reliable"
            else ReliabilityPolicy.BEST_EFFORT
        ),
        history=HistoryPolicy.KEEP_LAST,
        depth=depth,
    )


def process_cloud(points: np.ndarray) -> tuple[np.ndarray, int]:
    """Deterministic stand-in for real edge processing: centroid + number of
    occupied voxels at VOXEL_SIZE resolution."""
    centroid = points.mean(axis=0)
    voxels = np.unique(np.floor(points / VOXEL_SIZE).astype(np.int32), axis=0)
    return centroid, int(voxels.shape[0])


class EdgeNode(Node):
    def __init__(self) -> None:
        super().__init__("mec_cast_edge")
        self.declare_parameter("reliability", "reliable")
        self.declare_parameter("qos_depth", 10)
        self.declare_parameter("admin_url", os.environ.get("ADMIN_URL", ""))
        self.declare_parameter("admin_autostart", True)
        self.declare_parameter("admin_instance", 0)
        self.reliability = str(self.get_parameter("reliability").value)
        self.qos_depth = int(self.get_parameter("qos_depth").value)

        self.runs_dir = os.environ.get("RUNS_DIR", "runs")
        self.logging_url = os.environ.get("LOGGING_URL") or None

        self.run_id: str | None = None
        self.recorder: tel.Recorder | None = None
        self.sub = None
        self.frames = 0
        self.seq_gaps = 0
        self.last_seq: int | None = None

        self.admin = AdminClient(
            node_type=ap.NodeType.EDGE,
            host=socket.gethostname(),
            url=str(self.get_parameter("admin_url").value),
            instance=int(self.get_parameter("admin_instance").value),
            version_sha=os.environ.get("VCS_REF", ""),
            version_tag=os.environ.get("VERSION", ""),
            pid=os.getpid(),
        )
        self.autostart = bool(self.get_parameter("admin_autostart").value)
        self._last_status: dict | None = None

        if self.admin.enabled:
            self.admin.update_identity(autostart=self.autostart, params=self.params())
            self.admin.start()
            self.create_timer(ADMIN_POLL_S, self._drain_admin)
            self.create_timer(STATUS_PERIOD_S, self._report_status)
            self.get_logger().info(
                f"edge up (admin={self.admin.url}, node_id={self.admin.node_id}, "
                f"qos={self.reliability}/KEEP_LAST({self.qos_depth}))"
            )
        else:
            # Standalone: the environment names the run, exactly as before.
            self.start_run(os.environ.get("RUN_ID", "dev-run"))
            self.get_logger().info(
                f"edge up (run_id={self.run_id}, "
                f"qos={self.reliability}/KEEP_LAST({self.qos_depth}))"
            )

    # --- run lifecycle ----------------------------------------------------

    def params(self) -> dict:
        return {"reliability": self.reliability, "qos_depth": self.qos_depth}

    @property
    def running(self) -> bool:
        return self.recorder is not None

    def start_run(self, run_id: str) -> None:
        """Build a Recorder for this run and subscribe.

        Subscribing here rather than in __init__ is what makes a stopped edge
        genuinely absent from the ROS graph, which is also what lets the admin
        tell "no subscriber" from "subscriber that is not receiving".
        """
        if self.running:
            if run_id == self.run_id:
                return
            self.stop_run()

        self.run_id = run_id
        self.frames = 0
        self.seq_gaps = 0
        self.last_seq = None
        self.recorder = tel.Recorder(
            run_id,
            "mec-cast-edge",
            os.path.join(self.runs_dir, run_id, "edge"),
            logging_url=self.logging_url,
        )
        self.sub = self.create_subscription(
            CloudWithTelemetry,
            CLOUD_TOPIC,
            self.on_cloud,
            cloud_qos(self.reliability, self.qos_depth),
        )
        self.get_logger().info(f"edge recording run {run_id}")

    def stop_run(self) -> dict:
        """Unsubscribe, then drain the recorder. Order matters: a callback
        firing into a shut-down recorder would raise."""
        if not self.running:
            return {}
        if self.sub is not None:
            self.destroy_subscription(self.sub)
            self.sub = None
        report = self.recorder.shutdown()
        self.recorder = None
        self.get_logger().info(
            f"edge stopped run {self.run_id}: frames={self.frames} "
            f"seq_gaps={self.seq_gaps} report={report}"
        )
        return report

    # --- admin ------------------------------------------------------------

    def _drain_admin(self) -> None:
        """Apply whatever the admin asked for, on the executor thread."""
        for frame in self.admin.poll():
            payload = frame.get("payload") or {}
            if frame["type"] == ap.MessageType.WELCOME:
                active = payload.get("active_run")
                if active and self.autostart:
                    self.start_run(active["run_id"])
                self._report_status(force=True)
            elif frame["type"] == ap.MessageType.COMMAND:
                self._apply_command(frame, payload)

    def _apply_command(self, frame: dict, payload: dict) -> None:
        command = payload.get("command")
        ok, error, stop_report = True, None, None
        try:
            if command in (ap.CommandType.RUN_START, ap.CommandType.STREAM_START):
                run_id = payload.get("run_id")
                if not run_id:
                    raise ValueError("run.start without a run_id")
                self.start_run(run_id)
            elif command in (ap.CommandType.RUN_STOP, ap.CommandType.STREAM_STOP):
                stop_report = self.stop_run()
            elif command != ap.CommandType.STATUS_REPORT:
                raise ValueError(f"unknown command {command!r}")
        except Exception as exc:  # a bad command must not kill the node
            ok, error = False, str(exc)
            self.get_logger().error(f"admin command {command} failed: {exc}")
        self.admin.publish_ack(frame["msg_id"], ok=ok, error=error)
        # The report travels with the status: an admin-driven stop leaves this
        # process alive, so it must not wait for the goodbye frame.
        self._report_status(force=True, report=stop_report)

    def peers(self) -> list[dict]:
        """Who is publishing to us, from the ROS graph.

        The graph already knows, so this needs no change to the wire format —
        and `CloudWithTelemetry` carries no sender identity by design, since
        `TimingEnvelope` is a pinned 64-byte contract shared with the C ABI.
        """
        if not self.running:
            return []
        try:
            infos = self.get_publishers_info_by_topic(CLOUD_TOPIC)
        except Exception:
            return []
        return [ap.peer(info.node_name or "unknown") for info in infos]

    def _report_status(self, force: bool = False, report: dict | None = None) -> None:
        if not self.admin.enabled:
            return
        payload = ap.status_payload(
            node_type=ap.NodeType.EDGE,
            state=ap.NodeState.RUNNING if self.running else ap.NodeState.IDLE,
            run_id=self.run_id,
            subscribed=self.sub is not None,
            peers=self.peers(),
            params=self.params(),
            counters={"frames": self.frames, "seq_gaps": self.seq_gaps},
            autostart=self.autostart,
            report=report or {},
        )
        # Status on every change, per the protocol — but a periodic resend
        # doubles as liveness, so an unchanged payload still goes out on the
        # timer rather than being suppressed entirely.
        if force or payload != self._last_status:
            self._last_status = payload
            self.admin.update_identity(
                state=payload["state"], run_id=self.run_id, params=self.params()
            )
            self.admin.publish_status(payload)

    def on_cloud(self, msg: CloudWithTelemetry) -> None:
        recv_ns = tel.now_ns()  # first line: the arrival stamp
        env = msg.envelope

        n = msg.cloud.width * msg.cloud.height
        points = np.frombuffer(msg.cloud.data, dtype=np.float32).reshape(n, 3)
        centroid, voxel_count = process_cloud(points)
        process_done_ns = tel.now_ns()

        if self.last_seq is not None and env.seq != self.last_seq + 1:
            self.seq_gaps += 1
        self.last_seq = env.seq

        self.recorder.record(
            seq=env.seq,
            modality=env.modality,
            capture_ns=env.capture_ns,
            send_ns=env.send_ns,
            recv_ns=recv_ns,
            process_done_ns=process_done_ns,
            payload_bytes=len(msg.cloud.data),
            site=SITE_EDGE,
            trace_id=bytes(env.trace_id),
        )
        self.frames += 1
        # Parseable progress line — the launch test and humans both read it.
        self.get_logger().info(
            f"processed seq={env.seq} n={n} voxels={voxel_count} "
            f"centroid=({centroid[0]:.2f},{centroid[1]:.2f},{centroid[2]:.2f}) "
            f"network_ns={recv_ns - env.send_ns}"
        )

    def finish(self) -> None:
        """Stop producing, drain, then say goodbye. Tolerant of no active run:
        an admin-driven node may be idle when it is told to exit."""
        report = self.stop_run()
        self.admin.goodbye(reason="shutdown", run_id=self.run_id, final_report=report)
        self.get_logger().info(f"edge done: report={report}")


def main(args=None) -> None:
    rclpy.init(args=args)
    # docker stop sends SIGTERM: exit spin cleanly so the recorder flushes.
    signal.signal(signal.SIGTERM, lambda *_: rclpy.shutdown())
    node = EdgeNode()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.finish()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
