"""Deterministic synthetic PointCloud2 publisher — the mec-cast test-vector
source.

Generates seeded, reproducible point clouds at a configurable size and rate,
stamps `capture_ns` at generation and `send_ns` immediately before publish
(both CLOCK_REALTIME via the shared telemetry clock), and records
sender-side samples through the mec_cast_telemetry recorder.

Parameters:
    seed        (int,   default 42)      RNG seed — same seed, same clouds
    num_points  (int,   default 30000)   points per frame (size sweep knob)
    rate_hz     (float, default 10.0)    frames per second
    pattern     (str,   default uniform_cube)  uniform_cube | rotating_plane

Environment:
    RUN_ID       experiment run id (trace_id + output dir name)
    LOGGING_URL  mec-cast-logging-service base URL (optional)
    RUNS_DIR     base directory for per-run output (default ./runs)
"""

import os
import signal
import uuid

import numpy as np
import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from sensor_msgs.msg import PointCloud2, PointField
from std_msgs.msg import Header

import mec_cast_telemetry as tel
from mec_cast_msgs.msg import CloudWithTelemetry, TimingEnvelope

SITE_PUBLISHER = 0


def run_trace_id(run_id: str) -> bytes:
    """16-byte trace id: UUID bytes when run_id parses as a UUID, else
    zero-padded/truncated UTF-8."""
    try:
        return uuid.UUID(run_id).bytes
    except ValueError:
        raw = run_id.encode()[:16]
        return raw + b"\x00" * (16 - len(raw))


def make_pointcloud2(points: np.ndarray, stamp, frame_id: str) -> PointCloud2:
    """Build a PointCloud2 from an (N, 3) float32 array without per-point
    Python overhead."""
    assert points.dtype == np.float32 and points.ndim == 2 and points.shape[1] == 3
    msg = PointCloud2()
    msg.header = Header(stamp=stamp, frame_id=frame_id)
    msg.height = 1
    msg.width = points.shape[0]
    msg.fields = [
        PointField(name="x", offset=0, datatype=PointField.FLOAT32, count=1),
        PointField(name="y", offset=4, datatype=PointField.FLOAT32, count=1),
        PointField(name="z", offset=8, datatype=PointField.FLOAT32, count=1),
    ]
    msg.is_bigendian = False
    msg.point_step = 12
    msg.row_step = 12 * points.shape[0]
    msg.data = points.tobytes()
    msg.is_dense = True
    return msg


class PointCloudPublisher(Node):
    def __init__(self) -> None:
        super().__init__("mec_cast_lidar_client")
        self.declare_parameter("seed", 42)
        self.declare_parameter("num_points", 30000)
        self.declare_parameter("rate_hz", 10.0)
        self.declare_parameter("pattern", "uniform_cube")

        self.seed = int(self.get_parameter("seed").value)
        self.num_points = int(self.get_parameter("num_points").value)
        self.rate_hz = float(self.get_parameter("rate_hz").value)
        self.pattern = str(self.get_parameter("pattern").value)
        if self.pattern not in ("uniform_cube", "rotating_plane"):
            raise ValueError(f"unknown pattern {self.pattern!r}")

        self.run_id = os.environ.get("RUN_ID", "dev-run")
        self.trace_id = run_trace_id(self.run_id)
        runs_dir = os.environ.get("RUNS_DIR", "runs")

        self.recorder = tel.Recorder(
            self.run_id,
            "mec-cast-pub",
            os.path.join(runs_dir, self.run_id, "pub"),
            logging_url=os.environ.get("LOGGING_URL") or None,
        )

        self.rng = np.random.default_rng(self.seed)
        self.seq = 0
        self.pub = self.create_publisher(CloudWithTelemetry, "mec_cast/cloud", 10)
        self.timer = self.create_timer(1.0 / self.rate_hz, self.publish_frame)
        self.get_logger().info(
            f"publishing {self.num_points} pts @ {self.rate_hz} Hz "
            f"(pattern={self.pattern}, seed={self.seed}, run_id={self.run_id})"
        )

    def generate_points(self) -> np.ndarray:
        if self.pattern == "uniform_cube":
            # Fully deterministic sequence: frame k is draw k of the seeded RNG.
            return (self.rng.random((self.num_points, 3), dtype=np.float32) * 10.0).astype(
                np.float32
            )
        # rotating_plane: a flat sheet rotating with frame index — compresses
        # very differently from noise, deterministic per (seed, seq).
        angle = (self.seq % 360) * np.pi / 180.0
        side = int(np.sqrt(self.num_points))
        xs, ys = np.meshgrid(
            np.linspace(-5, 5, side, dtype=np.float32),
            np.linspace(-5, 5, side, dtype=np.float32),
        )
        zs = np.zeros_like(xs)
        pts = np.stack([xs, ys, zs], axis=-1).reshape(-1, 3)
        rot = np.array(
            [
                [np.cos(angle), 0.0, np.sin(angle)],
                [0.0, 1.0, 0.0],
                [-np.sin(angle), 0.0, np.cos(angle)],
            ],
            dtype=np.float32,
        )
        return np.ascontiguousarray(pts @ rot.T, dtype=np.float32)

    def publish_frame(self) -> None:
        capture_ns = tel.now_ns()
        points = self.generate_points()

        msg = CloudWithTelemetry()
        msg.cloud = make_pointcloud2(points, self.get_clock().now().to_msg(), "lidar")
        env = TimingEnvelope()
        env.capture_ns = capture_ns
        env.seq = self.seq
        env.modality = tel.MODALITY_POINTCLOUD
        env.trace_id = list(self.trace_id)

        # Stamp as late as possible: the gap send_ns -> edge recv_ns is the
        # network (+ middleware) leg.
        env.send_ns = tel.now_ns()
        msg.envelope = env
        self.pub.publish(msg)

        self.recorder.record(
            seq=self.seq,
            modality=tel.MODALITY_POINTCLOUD,
            capture_ns=capture_ns,
            send_ns=env.send_ns,
            payload_bytes=len(msg.cloud.data),
            site=SITE_PUBLISHER,
            trace_id=self.trace_id,
        )
        self.seq += 1

    def finish(self) -> None:
        report = self.recorder.shutdown()
        self.get_logger().info(f"recorder report: {report}")


def main(args=None) -> None:
    rclpy.init(args=args)
    # docker stop sends SIGTERM: exit spin cleanly so the recorder flushes.
    signal.signal(signal.SIGTERM, lambda *_: rclpy.shutdown())
    node = PointCloudPublisher()
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
