"""mec-cast MEC edge subscriber.

Receives `CloudWithTelemetry`, stamps `recv_ns` as the very first action in
the callback, runs a deterministic processing step (centroid + occupied
voxel count), stamps `process_done_ns`, and feeds the sample to the shared
telemetry recorder (per-frame CSV + periodic snapshots to the logging
service).

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

import mec_cast_telemetry as tel
from mec_cast_msgs.msg import CloudWithTelemetry

SITE_EDGE = 1
VOXEL_SIZE = 0.5


def process_cloud(points: np.ndarray) -> tuple[np.ndarray, int]:
    """Deterministic stand-in for real edge processing: centroid + number of
    occupied voxels at VOXEL_SIZE resolution."""
    centroid = points.mean(axis=0)
    voxels = np.unique(np.floor(points / VOXEL_SIZE).astype(np.int32), axis=0)
    return centroid, int(voxels.shape[0])


class EdgeNode(Node):
    def __init__(self) -> None:
        super().__init__("mec_cast_edge")
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
            CloudWithTelemetry, "mec_cast/cloud", self.on_cloud, 10
        )
        self.get_logger().info(f"edge up (run_id={self.run_id})")

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
