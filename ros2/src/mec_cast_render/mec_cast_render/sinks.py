"""Where a received cloud goes to be drawn.

The render node's job is receive → stamp → record → hand off. Only the hand-off
knows about a renderer, so the renderer is one class and swapping it is not a
one-way door. Everything above the sink is measurement and stays identical
whichever sink is selected — including `null`, which measures a full round trip
and draws nothing.

`rerun` is the default in the lab. It is Apache-2.0, it renders large point
clouds well, and it plots scalars on the same timeline as the 3D view, so a
frame's glass-to-glass delay is visible beside the frame itself. Its Python API
has moved across releases; `RerunSink` imports lazily and probes for the call
it needs, so a version bump degrades to a clear error rather than an import
crash at node startup.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

import numpy as np

if TYPE_CHECKING:  # the runtime import is inside the functions that need it
    from sensor_msgs.msg import PointCloud2

#: Selectable via the `sink` parameter. Keep in sync with the node docstring.
SINKS = ("null", "rerun", "ros")


def make_pointcloud2(points: np.ndarray, stamp, frame_id: str) -> PointCloud2:
    """Build a PointCloud2 from an (N, 3) float32 array. Lives here because
    the `ros` sink is its only user.

    `sensor_msgs` is imported inside the function, not at module scope, so
    selecting the `null` sink needs no ROS message packages — which is what
    lets the sink table be unit-tested off a robot.
    """
    from sensor_msgs.msg import PointCloud2, PointField
    from std_msgs.msg import Header

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


class Sink(Protocol):
    """Draw a cloud. Called on the executor thread, so it must not block for
    longer than a frame interval — a slow sink shows up as `dropped_late`."""

    def draw(self, seq: int, points: np.ndarray, meta: dict) -> None: ...

    def close(self) -> None: ...


class NullSink:
    """Measures everything, draws nothing.

    Not a stub: this is how the round trip is exercised in CI and on any host
    without a GPU, and how you measure the return path's cost without a
    renderer's cost folded into it.
    """

    name = "null"

    def draw(self, seq: int, points: np.ndarray, meta: dict) -> None:
        pass

    def close(self) -> None:
        pass


class RerunSink:
    """Log the cloud to a Rerun viewer, with the latency beside it."""

    name = "rerun"

    def __init__(self, run_id: str, serve: bool = True, web_port: int = 9876,
                 grpc_port: int = 9877, viewer_host: str = "localhost",
                 rrd_path: str | None = None):
        try:
            import rerun as rr
        except ImportError as exc:  # pragma: no cover - depends on the image
            raise RuntimeError(
                "sink='rerun' needs the rerun-sdk package "
                "(pip install 'rerun-sdk'); use sink='null' to measure "
                "without drawing"
            ) from exc
        self.rr = rr
        self.viewer_host = viewer_host
        # recording_id = run_id keeps one viewer session per experiment, so
        # switching runs does not silently append to the previous one's timeline.
        rr.init("mec-cast-render", recording_id=run_id)
        self.rrd_path = rrd_path
        if serve:
            self._serve(web_port, grpc_port)
        if rrd_path:
            self._also_record(rrd_path)

    def _also_record(self, rrd_path: str) -> None:
        """Write the session to a .rrd beside samples.csv, as well as serving it.

        The live viewer depends on two ports, a browser that can run WebGPU or
        WebGL, and the operator being at the keyboard while the run happens. A
        file depends on none of that: drag it onto any Rerun viewer — including
        the one this node already serves — and replay the run afterwards. For a
        measurement testbed that is the more useful artefact, and it is what
        makes a headless lab UE worth pointing a renderer at.

        `set_sinks` replaces the sink set, so the gRPC sink has to be named
        again here or serving stops the moment the file sink is added.
        """
        rr = self.rr
        if not hasattr(rr, "set_sinks"):
            self.rr.get_global_data_recording().save(rrd_path)
            return
        sinks = []
        if getattr(self, "grpc_uri", None):
            sinks.append(rr.GrpcSink(url=self.grpc_uri))
        sinks.append(rr.FileSink(rrd_path))
        rr.set_sinks(*sinks)

    def _serve(self, web_port: int, grpc_port: int) -> None:
        """Serve the browsable viewer.

        Two servers, and both are needed: `serve_grpc` carries the log stream,
        `serve_web_viewer` serves the HTML/WASM page that connects back to it.
        The page runs in the operator's browser, so **both ports must be
        reachable from there**, not just the web one — publishing only 9876
        gives a page that loads and then never fills in.

        This spelling is specific to rerun 0.36; `serve_web` existed in
        earlier releases and is gone. The version is pinned in the Dockerfile
        for that reason, and the check below fails loudly rather than
        half-working if it drifts again.
        """
        rr = self.rr
        missing = [n for n in ("serve_grpc", "serve_web_viewer") if not hasattr(rr, n)]
        if missing:
            raise RuntimeError(
                f"rerun {getattr(rr, '__version__', '?')} lacks {', '.join(missing)}; "
                "this sink targets the 0.36 API pinned in deploy/docker/ros.Dockerfile. "
                "Use sink='ros' (RViz2/Foxglove) or sink='null' meanwhile."
            )
        uri = rr.serve_grpc(grpc_port=grpc_port, server_memory_limit="512MiB")
        self.grpc_uri = uri
        # open_browser defaults to True and there is no browser in a container.
        rr.serve_web_viewer(web_port=web_port, open_browser=False, connect_to=uri)

        # `connect_to` does not put the source into the served page — verified
        # against 0.36.2: the bare page loads a viewer with no data source and
        # never attempts a connection. The viewer does honour a `?url=` query
        # parameter, so build the address that actually works and let the node
        # log it, rather than handing the operator a URL that opens an empty
        # viewer and looks like the renderer is broken.
        #
        # The host in the query is resolved by the *browser*, not this
        # container, so it must be the address the operator reaches the UE on.
        # localhost is right when browsing on the same machine; override
        # `viewer_host` when the UE is a different box, as it is in the lab.
        source = f"rerun%2Bhttp://{self.viewer_host}:{grpc_port}/proxy"
        self.url = f"http://{self.viewer_host}:{web_port}/?url={source}"

    def _set_frame(self, seq: int) -> None:
        """`set_time(..., sequence=)` replaced `set_time_sequence` partway
        through rerun's 0.x line; support both."""
        if hasattr(self.rr, "set_time"):
            self.rr.set_time("frame", sequence=seq)
        else:
            self.rr.set_time_sequence("frame", seq)

    def _scalar(self, value: float):
        """Likewise `Scalar` -> `Scalars`."""
        ctor = getattr(self.rr, "Scalars", None) or self.rr.Scalar
        return ctor(value)

    def draw(self, seq: int, points: np.ndarray, meta: dict) -> None:
        rr = self.rr
        self._set_frame(seq)
        if points.shape[0] == 0:
            # An all-points-in-one-voxel frame degenerates to nothing to draw.
            # np.max on an empty array raises, so bail before colouring.
            rr.log("world/cloud", rr.Points3D(points))
            return
        # Colour by height so the structure is legible without a texture.
        z = points[:, 2]
        span = float(z.max() - z.min()) or 1.0
        t = ((z - z.min()) / span * 255).astype(np.uint8)
        colors = np.stack([t, np.full_like(t, 128), 255 - t], axis=1)
        rr.log("world/cloud", rr.Points3D(points, colors=colors, radii=0.05))
        # The reason this renderer was chosen: the number and the picture on
        # one timeline. e2e_ns is the PTP-free round trip.
        for key in ("e2e_ns", "network_ns"):
            if meta.get(key):
                rr.log(f"metrics/{key}", self._scalar(meta[key] / 1e6))

    def close(self) -> None:
        pass


class RosSink:
    """Republish as a plain `sensor_msgs/PointCloud2`.

    Costs almost nothing and makes the renderer choice non-binding: RViz2,
    Foxglove or anything else that speaks ROS can attach to
    `mec_cast/render/cloud` without this node knowing they exist.
    """

    name = "ros"

    def __init__(self, node, topic: str = "mec_cast/render/cloud"):
        from sensor_msgs.msg import PointCloud2

        self.node = node
        self.pub = node.create_publisher(PointCloud2, topic, 1)

    def draw(self, seq: int, points: np.ndarray, meta: dict) -> None:
        self.pub.publish(
            make_pointcloud2(points, self.node.get_clock().now().to_msg(), "mec_cast_render")
        )

    def close(self) -> None:
        if self.pub is not None:
            self.node.destroy_publisher(self.pub)
            self.pub = None


def build_sink(kind: str, *, node, run_id: str, serve: bool = False,
               web_port: int = 9876, grpc_port: int = 9877,
               viewer_host: str = "localhost", rrd_path: str | None = None) -> Sink:
    if kind == "null":
        return NullSink()
    if kind == "rerun":
        return RerunSink(run_id=run_id, serve=serve, web_port=web_port,
                         grpc_port=grpc_port, viewer_host=viewer_host,
                         rrd_path=rrd_path)
    if kind == "ros":
        return RosSink(node)
    raise ValueError(f"unknown sink {kind!r}, expected one of {SINKS}")
