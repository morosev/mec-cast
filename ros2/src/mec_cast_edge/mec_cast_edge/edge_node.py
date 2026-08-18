"""mec-cast MEC edge subscriber.

Receives `CloudWithTelemetry`, stamps `recv_ns` as the very first action in
the callback, runs a deterministic processing step (centroid + occupied
voxel count), stamps `process_done_ns`, and feeds the sample to the shared
telemetry recorder (per-frame CSV + periodic snapshots to the logging
service).

Parameters:
    reliability  (str, default best_effort)  must match the publisher's
    qos_depth    (int, default 10)

Environment:
    RUN_ID       experiment run id (must match the publisher's for joins)
    LOGGING_URL  mec-cast-logging-service base URL (optional)
    RUNS_DIR     base directory for per-run output (default ./runs)
"""

import os
import signal

import numpy as np
import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy

import mec_cast_telemetry as tel
from mec_cast_msgs.msg import CloudWithTelemetry

SITE_EDGE = 1
VOXEL_SIZE = 0.5


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
        self.reliability = str(self.get_parameter("reliability").value)
        self.qos_depth = int(self.get_parameter("qos_depth").value)

        self.run_id = os.environ.get("RUN_ID", "dev-run")
        runs_dir = os.environ.get("RUNS_DIR", "runs")
        self.recorder = tel.Recorder(
            self.run_id,
            "mec-cast-edge",
            os.path.join(runs_dir, self.run_id, "edge"),
            logging_url=os.environ.get("LOGGING_URL") or None,
        )
        self.frames = 0
        self.seq_gaps = 0
        self.last_seq: int | None = None
        self.sub = self.create_subscription(
            CloudWithTelemetry,
            "mec_cast/cloud",
            self.on_cloud,
            cloud_qos(self.reliability, self.qos_depth),
        )
        self.get_logger().info(
            f"edge up (run_id={self.run_id}, "
            f"qos={self.reliability}/KEEP_LAST({self.qos_depth}))"
        )

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
        report = self.recorder.shutdown()
        self.get_logger().info(
            f"edge done: frames={self.frames} seq_gaps={self.seq_gaps} report={report}"
        )


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
