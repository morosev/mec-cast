"""Deterministic synthetic PointCloud2 publisher — the mec-cast test-vector
source.

Generates seeded, reproducible point clouds at a configurable size and rate,
stamps `capture_ns` at generation and `send_ns` immediately before publish
(both CLOCK_REALTIME via the shared telemetry clock), and records
sender-side samples through the mec_cast_telemetry recorder.

Parameters (all environment variables are demoted to defaults, so a laptop
run needs no exports — see MecCastNode for the shared set: run_id, runs_dir,
logging_url, admin_url, admin_autostart, admin_instance):
    seed        (int,   default 42)      RNG seed — same seed, same clouds
    num_points  (int,   default 30000)   points per frame (size sweep knob)
    rate_hz     (float, default 10.0)    frames per second
    pattern     (str,   default $PATTERN or uniform_cube)
    reliability (str,   default reliable)  must match the edge's
    qos_depth   (int,   default 10)

With no admin_url the node starts publishing immediately under its `run_id`
parameter — the standalone path. With an admin it sits idle until told to
stream: a robot must not start streaming the moment it powers on.
"""

import os

import numpy as np
from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import PointCloud2, PointField
from std_msgs.msg import Header

import mec_cast_telemetry as tel
from mec_cast_admin_client import protocol as ap
from mec_cast_admin_client.node_base import MecCastNode, spin
from mec_cast_admin_client.sites import SITE_PUBLISHER
from mec_cast_msgs.msg import CloudWithTelemetry, TimingEnvelope

import uuid

#: Cloud shapes. Every one is deterministic in (seed, seq), so a run is
#: reproducible frame for frame. They voxel-compress very differently, which
#: makes the choice an experimental variable and not only a visual one — see
#: ADR-0009 for what that does to the downlink.
PATTERNS = ("uniform_cube", "rotating_plane", "sphere", "lidar_scan")


def cloud_qos(reliability: str, depth: int) -> QoSProfile:
    """QoS for the point-cloud topic.

    `best_effort` is the sensor-data convention and the only setting that
    lets a late frame be *dropped* rather than retransmitted — which is what
    a latency-critical stream wants, and what makes an unreliable transport
    (unreliable datagrams via `mixed_rel`) behave differently from a reliable one.
    Under `reliable`, every transport is asked to guarantee delivery, so the
    wire protocol barely matters; see ADR-0006.

    Publisher and subscriber must agree: a BEST_EFFORT publisher and a
    RELIABLE subscriber are an incompatible pair and no data flows at all.
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


class PointCloudPublisher(MecCastNode):
    NODE_TYPE = ap.NodeType.CLIENT
    SERVICE = "mec-cast-pub"
    SITE = SITE_PUBLISHER
    OUT_LEAF = "pub"

    def __init__(self, *, node_name: str | None = None,
                 parameter_overrides: list | None = None) -> None:
        # A robot must not start streaming the moment it powers on: the
        # operator decides. The edge and renderer default the other way.
        super().__init__(
            "mec_cast_lidar_client",
            node_name=node_name,
            parameter_overrides=parameter_overrides,
            autostart_default=os.environ.get("ADMIN_AUTOSTART", "").lower() == "true",
        )
        self.declare_parameter("seed", 42)
        self.declare_parameter("num_points", 30000)
        self.declare_parameter("rate_hz", 10.0)
        self.declare_parameter("pattern", os.environ.get("PATTERN", "uniform_cube"))
        self.declare_parameter("reliability", "reliable")
        self.declare_parameter("qos_depth", 10)

        self.seed = int(self.get_parameter("seed").value)
        self.num_points = int(self.get_parameter("num_points").value)
        self.rate_hz = float(self.get_parameter("rate_hz").value)
        self.pattern = str(self.get_parameter("pattern").value)
        self.reliability = str(self.get_parameter("reliability").value)
        self.qos_depth = int(self.get_parameter("qos_depth").value)
        if self.pattern not in PATTERNS:
            raise ValueError(f"unknown pattern {self.pattern!r}, expected one of {PATTERNS}")

        self.trace_id = b"\x00" * 16
        self.timer = None
        self.rng = np.random.default_rng(self.seed)
        self.seq = 0
        self.frames_published = 0

        # The publisher exists before any run; only the timer is per-run.
        self.pub = self.create_publisher(
            CloudWithTelemetry,
            "mec_cast/cloud",
            cloud_qos(self.reliability, self.qos_depth),
        )
        self._start()
        if not self.admin.enabled:
            self.get_logger().info(
                f"publishing {self.num_points} pts @ {self.rate_hz} Hz "
                f"(pattern={self.pattern}, seed={self.seed}, run_id={self.run_id}, "
                f"qos={self.reliability}/KEEP_LAST({self.qos_depth}))"
            )

    # --- run lifecycle ----------------------------------------------------

    def params(self) -> dict:
        return {
            "num_points": self.num_points,
            "rate_hz": self.rate_hz,
            "seed": self.seed,
            "pattern": self.pattern,
            "reliability": self.reliability,
            "qos_depth": self.qos_depth,
        }

    def counters(self) -> dict:
        dropped = self.recorder.dropped_total() if self.recorder is not None else 0
        return {
            "frames_published": self.frames_published,
            "seq_last": max(self.seq - 1, 0),
            "samples_dropped": dropped,
        }

    def _status_extra(self) -> dict:
        return {"streaming": self.streaming}

    @property
    def streaming(self) -> bool:
        return self.running

    def start_run(self, run_id: str, args: dict | None = None) -> None:
        """Build a Recorder for this run and start the publish timer.

        Workload knobs arrive with the command rather than from the
        environment, so a run records the settings it actually used.
        """
        if self.streaming:
            if run_id == self.run_id:
                return
            self.stop_run()

        for key in ("num_points", "rate_hz", "seed", "pattern"):
            if args and key in args and args[key] is not None:
                setattr(self, key, type(getattr(self, key))(args[key]))
        if self.pattern not in PATTERNS:
            raise ValueError(f"unknown pattern {self.pattern!r}, expected one of {PATTERNS}")

        self.trace_id = run_trace_id(run_id)
        # A run is reproducible from its seed, so the sequence restarts with it.
        self.rng = np.random.default_rng(self.seed)
        self.seq = 0
        self.frames_published = 0
        self._make_recorder(run_id)
        self.timer = self.create_timer(1.0 / self.rate_hz, self.publish_frame)
        self.get_logger().info(
            f"streaming run {run_id}: {self.num_points} pts @ {self.rate_hz} Hz "
            f"(pattern={self.pattern}, seed={self.seed})"
        )

    def stop_run(self) -> dict:
        """Stop the timer first, then drain: publish_frame must not fire into
        a shut-down recorder."""
        if not self.streaming:
            return {}
        if self.timer is not None:
            self.destroy_timer(self.timer)
            self.timer = None
        report = self._close_recorder()
        self.get_logger().info(
            f"stopped run {self.run_id}: frames={self.frames_published} report={report}"
        )
        return report

    # --- the workload -----------------------------------------------------

    def generate_points(self) -> np.ndarray:
        if self.pattern == "uniform_cube":
            # Fully deterministic sequence: frame k is draw k of the seeded RNG.
            return (self.rng.random((self.num_points, 3), dtype=np.float32) * 10.0).astype(
                np.float32
            )
        if self.pattern == "sphere":
            # Points on a sphere's surface: normalised Gaussians are uniform
            # on the shell. Voxelises to a hollow shell rather than a solid,
            # so it compresses far better than the cube at the same count.
            v = self.rng.standard_normal((self.num_points, 3)).astype(np.float32)
            v /= np.linalg.norm(v, axis=1, keepdims=True)
            return np.ascontiguousarray(v * 4.0 + 5.0, dtype=np.float32)

        if self.pattern == "lidar_scan":
            return self.lidar_scan()

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

    def lidar_scan(self) -> np.ndarray:
        """A spinning multi-beam sweep inside a 10 m room.

        The closest thing here to what the platform actually carries: rings of
        returns off walls, floor and ceiling, rotating with the frame index.
        Rays leave a sensor at the centre and are cut against the box with the
        slab method, so every point is a real surface hit rather than free
        space — which is what makes it voxelise like a scan instead of a fog.
        """
        beams = 16
        per_beam = max(1, self.num_points // beams)
        elev = np.linspace(-15.0, 15.0, beams, dtype=np.float32) * (np.pi / 180.0)
        # One degree of azimuth per frame: a full revolution every 360 frames.
        azim = (
            np.linspace(0.0, 2 * np.pi, per_beam, endpoint=False, dtype=np.float32)
            + (self.seq % 360) * (np.pi / 180.0)
        )
        el, az = np.meshgrid(elev, azim, indexing="ij")
        d = np.stack(
            [np.cos(el) * np.cos(az), np.cos(el) * np.sin(az), np.sin(el)], axis=-1
        ).reshape(-1, 3).astype(np.float32)

        origin = np.array([5.0, 5.0, 5.0], dtype=np.float32)
        # Slab method against [0, 10]^3. A ray parallel to an axis never meets
        # that pair of planes, so its t must not win the minimum.
        parallel = np.abs(d) < 1e-6
        safe = np.where(parallel, 1e-6, d)
        bound = np.where(d > 0.0, 10.0, 0.0).astype(np.float32)
        t = np.where(parallel, np.inf, (bound - origin) / safe)
        t = np.min(t, axis=1, keepdims=True)
        return np.ascontiguousarray(origin + d * t, dtype=np.float32)

    def publish_frame(self) -> None:
        if self.recorder is None:
            # The run was stopped between this tick being scheduled and fired.
            return
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
            site=self.SITE,
            trace_id=self.trace_id,
        )
        self.seq += 1
        self.frames_published += 1


def main(args=None) -> None:
    spin(PointCloudPublisher, args)


if __name__ == "__main__":
    main()
