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
    num_points  (int,   default 5000)    points per frame (size sweep knob)
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
PATTERNS = (
    "uniform_cube",
    "rotating_plane",
    "sphere",
    "lidar_scan",
    "torus",
    "helix",
    "wave",
    "cylinder",
    "cube_edges",
    "swarm",
)
#: Every generator keeps its points inside this box, so one camera position
#: suits all of them and voxel counts stay comparable between patterns.
EXTENT = 10.0
CENTRE = 5.0
ADMIN_POLL_S = 0.1
STATUS_PERIOD_S = 2.0


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
        self.declare_parameter("num_points", 5000)
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

        for name in ("lidar_scan", "torus", "helix", "wave", "cylinder",
                     "cube_edges", "swarm"):
            if self.pattern == name:
                return getattr(self, name)()

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
        # Centred in the box like every other pattern. It was on the origin,
        # which put it half out of frame whenever the viewer was framed for
        # the others — harmless while it was the only alternative to the
        # cube, wrong now that a dropdown puts all ten side by side.
        return np.ascontiguousarray(pts @ rot.T + CENTRE, dtype=np.float32)

    def _phase(self) -> float:
        """One revolution every 360 frames, so motion is visible but slow."""
        return (self.seq % 360) * (np.pi / 180.0)

    def torus(self) -> np.ndarray:
        """A ring tumbling about the x axis. Voxelises to a closed tube."""
        n = self.num_points
        u = self.rng.random(n, dtype=np.float32) * (2 * np.pi)
        v = self.rng.random(n, dtype=np.float32) * (2 * np.pi)
        R, r = 3.0, 1.0
        x = (R + r * np.cos(v)) * np.cos(u)
        y = (R + r * np.cos(v)) * np.sin(u)
        z = r * np.sin(v)
        a = self._phase()
        y, z = y * np.cos(a) - z * np.sin(a), y * np.sin(a) + z * np.cos(a)
        return np.ascontiguousarray(
            np.stack([x, y, z], -1) + CENTRE, dtype=np.float32)

    def helix(self) -> np.ndarray:
        """Two strands winding up the box — a deliberately sparse shape, so
        the voxel count stays low however many points are asked for."""
        n = max(self.num_points // 2, 1)
        t = np.linspace(0.0, 6 * np.pi, n, dtype=np.float32) + self._phase()
        z = np.linspace(0.5, EXTENT - 0.5, n, dtype=np.float32)
        out = []
        for offset in (0.0, np.pi):
            out.append(np.stack([
                CENTRE + 3.0 * np.cos(t + offset),
                CENTRE + 3.0 * np.sin(t + offset),
                z,
            ], -1))
        return np.ascontiguousarray(np.concatenate(out), dtype=np.float32)

    def wave(self) -> np.ndarray:
        """A rippling sheet. Same point count as rotating_plane and a similar
        voxel count, but the surface moves through z rather than rotating."""
        side = max(int(np.sqrt(self.num_points)), 2)
        g = np.linspace(0.0, EXTENT, side, dtype=np.float32)
        xs, ys = np.meshgrid(g, g)
        a = self._phase()
        zs = CENTRE + 1.5 * np.sin(xs * 0.8 + a) * np.cos(ys * 0.8 + a)
        return np.ascontiguousarray(
            np.stack([xs, ys, zs], -1).reshape(-1, 3), dtype=np.float32)

    def cylinder(self) -> np.ndarray:
        """The inside of a fluted duct, rotating. The closest static shape to
        an industrial scene — a pipe or a conveyor tunnel."""
        n = self.num_points
        x = self.rng.random(n, dtype=np.float32) * EXTENT
        th = self.rng.random(n, dtype=np.float32) * (2 * np.pi)
        # Flutes make the rotation visible; a smooth pipe would look static.
        r = 3.0 + 0.4 * np.sin(3.0 * th + self._phase())
        return np.ascontiguousarray(np.stack([
            x, CENTRE + r * np.cos(th), CENTRE + r * np.sin(th)
        ], -1), dtype=np.float32)

    def cube_edges(self) -> np.ndarray:
        """Points along the 12 edges of the box — a wireframe. The sparsest
        pattern here: whatever the point count, the voxels are one deep along
        twelve lines, so it compresses hardest of all."""
        n = self.num_points
        lo, hi = 0.5, EXTENT - 0.5
        corners = np.array([[lo, lo, lo], [hi, hi, hi]], dtype=np.float32)
        edges = []
        for axis in range(3):
            for i in (0, 1):
                for j in (0, 1):
                    a = np.empty(3, dtype=np.float32)
                    b = np.empty(3, dtype=np.float32)
                    other = [k for k in range(3) if k != axis]
                    a[axis], b[axis] = lo, hi
                    a[other[0]] = b[other[0]] = corners[i, other[0]]
                    a[other[1]] = b[other[1]] = corners[j, other[1]]
                    edges.append((a, b))
        per = max(n // len(edges), 1)
        t = np.linspace(0.0, 1.0, per, dtype=np.float32)[:, None]
        pts = [a + (b - a) * t for a, b in edges]
        return np.ascontiguousarray(np.concatenate(pts), dtype=np.float32)

    def swarm(self) -> np.ndarray:
        """Eight drifting blobs — several objects in the scene rather than
        one. Voxelises into separated clusters, unlike every other pattern."""
        k = 8
        n = self.num_points
        # Cluster centres are fixed for the run; only their drift moves.
        base = np.random.default_rng(self.seed).random((k, 3)).astype(np.float32)
        base = base * (EXTENT - 3.0) + 1.5
        a = self._phase()
        drift = 0.8 * np.stack([
            np.sin(a + np.arange(k, dtype=np.float32)),
            np.cos(a + np.arange(k, dtype=np.float32)),
            np.sin(2 * a + np.arange(k, dtype=np.float32)),
        ], -1)
        per = max(n // k, 1)
        blobs = [
            (base[i] + drift[i])
            + self.rng.standard_normal((per, 3)).astype(np.float32) * 0.45
            for i in range(k)
        ]
        pts = np.concatenate(blobs)
        return np.ascontiguousarray(np.clip(pts, 0.0, EXTENT), dtype=np.float32)

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
