"""The sink table: selection, failure modes, and the null path.

These are deliberately not ROS tests. The point of `sinks.py` is that the
measurement path does not depend on a renderer, so the parts that decide
*which* renderer runs must be exercisable without one — and without ROS
message packages, which is why `sensor_msgs` is imported lazily.
"""

import shutil
import subprocess
import time

import numpy as np
import pytest

from mec_cast_render.sinks import SINKS, NullSink, build_sink


def cloud(n: int = 16) -> np.ndarray:
    return np.arange(n * 3, dtype=np.float32).reshape(n, 3)


class FakePublisher:
    def __init__(self):
        self.published = []

    def publish(self, msg):
        self.published.append(msg)


class FakeClock:
    def now(self):
        class _T:
            def to_msg(self_inner):
                from builtin_interfaces.msg import Time

                return Time(sec=0, nanosec=0)

        return _T()


class FakeNode:
    """Just enough Node for the `ros` sink: it only ever publishes."""

    def __init__(self):
        self.pub = FakePublisher()
        self.destroyed = False

    def create_publisher(self, msg_type, topic, depth):
        self.topic = topic
        return self.pub

    def destroy_publisher(self, pub):
        self.destroyed = True

    def get_clock(self):
        return FakeClock()


def make(kind: str, node=None):
    # serve=False: constructing the sink must not bind ports in a test.
    return build_sink(kind, node=node, run_id="test-run", serve=False)


class TestSelection:
    def test_null_is_the_default_and_needs_nothing(self):
        sink = make("null")
        assert isinstance(sink, NullSink)
        sink.draw(0, cloud(), {"e2e_ns": 1_000_000})
        sink.close()

    def test_an_unknown_sink_is_rejected_by_name(self):
        with pytest.raises(ValueError) as exc:
            make("holodeck")
        # The message must name what was asked for and what is on offer:
        # this is a typo in a compose file at 2am, not a programming error.
        assert "holodeck" in str(exc.value)
        for kind in SINKS:
            assert kind in str(exc.value)

    def test_every_advertised_sink_is_constructible_or_says_why(self):
        # No entry in SINKS may fail with an obscure error. Either it builds,
        # or it explains what is missing — `rerun` is not installed in the
        # ROS image, and the operator must be told that in words.
        for kind in SINKS:
            try:
                make(kind, node=FakeNode()).close()
            except RuntimeError as exc:
                assert "rerun" in str(exc), f"{kind}: unhelpful message {exc!r}"
            except (ImportError, AttributeError) as exc:
                pytest.fail(f"sink {kind!r} failed opaquely: {exc!r}")


class TestRosSink:
    """The escape hatch: republish plainly so RViz2 or Foxglove can attach."""

    def test_it_publishes_a_plain_pointcloud2_on_the_render_topic(self):
        node = FakeNode()
        sink = make("ros", node=node)
        assert node.topic == "mec_cast/render/cloud"

        sink.draw(7, cloud(16), {"e2e_ns": 1_000_000})
        assert len(node.pub.published) == 1
        msg = node.pub.published[0]
        # A renderer downstream reads width/point_step, so they must be right.
        assert msg.width == 16 and msg.height == 1
        assert msg.point_step == 12 and msg.row_step == 12 * 16
        assert [f.name for f in msg.fields] == ["x", "y", "z"]
        assert len(msg.data) == 16 * 12

    def test_closing_releases_the_publisher(self):
        node = FakeNode()
        sink = make("ros", node=node)
        sink.close()
        assert node.destroyed
        # Idempotent: stop_run may be called on an already-stopped node.
        sink.close()


class TestNullSinkIsRealMeasurement:
    def test_it_accepts_the_shapes_the_node_actually_sends(self):
        # NullSink is how CI measures a full round trip, so it must tolerate
        # everything the hot path hands it — including an empty cloud, which
        # is what an all-points-in-one-voxel frame degenerates to.
        sink = make("null")
        for points in (cloud(0), cloud(1), cloud(10_000)):
            sink.draw(0, points, {})
        sink.draw(0, cloud(), {"e2e_ns": None, "network_ns": 0})
        sink.close()


class TestRerunActuallyStreams:
    """A viewer attached to the rerun sink must RECEIVE something.

    This exists because everything else about the sink passed while the live
    stream was dead. `serve_grpc()` installs a sink and `set_sinks()` replaces
    the set rather than adding to it, so re-adding a `GrpcSink` — which
    *connects to* a server rather than *being* one — left the node a client of
    its own proxy. The file sink kept working, so `session.rrd` grew, the node
    logged a line per frame, and every viewer sat empty.

    Nothing observable from the node caught that. The only test that could is
    one which stands where a viewer stands and counts the bytes: a viewer
    received 12 bytes, an empty header, against 2.7 MB when serving worked.

    Needs the rerun SDK and the `rerun` binary, both present in the ROS image
    and neither on a bare host, so it skips rather than fails elsewhere.
    """

    PORT = 19_957
    #: An empty .rrd is ~12 bytes of header; this test's 60 small frames
    #: produce ~26 KB. The threshold sits between the two with a wide margin
    #: on both sides, so the test fails on "nothing arrived" rather than on
    #: how many frames happened to land before the viewer attached.
    MIN_BYTES = 5_000

    def _sink(self, tmp_path):
        # build_sink directly, not the `make` helper: that helper pins
        # serve=False so the other tests never bind a port, and this is the
        # one test whose whole point is that the port gets served.
        return build_sink(
            "rerun",
            node=FakeNode(),
            run_id="stream-test",
            serve=True,
            web_port=self.PORT + 1,
            grpc_port=self.PORT,
            rrd_path=str(tmp_path / "session.rrd"),
        )

    def test_a_viewer_receives_the_stream_and_the_file_is_written(self, tmp_path):
        pytest.importorskip("rerun", reason="rerun SDK is only in the ROS image")
        if shutil.which("rerun") is None:
            pytest.skip("the rerun viewer binary is only in the ROS image")

        sink = self._sink(tmp_path)
        try:
            for seq in range(60):
                sink.draw(seq, cloud(256), {"e2e_ns": 1_000_000})
                time.sleep(0.01)

            received = tmp_path / "received.rrd"
            subprocess.run(
                ["timeout", "20", "rerun", "--port", "auto",
                 f"rerun+http://localhost:{self.PORT}/proxy",
                 "--save", str(received)],
                capture_output=True,
                check=False,
            )
        finally:
            sink.close()

        got = received.stat().st_size if received.exists() else 0
        assert got > self.MIN_BYTES, (
            f"a viewer attached to the sink received {got} bytes. The stream is "
            "dead: check that set_sinks() is given a GrpcServerSink (which "
            "serves) and not a GrpcSink (which connects to a server)."
        )

        # The file must still be written — the two sinks replace each other if
        # they are installed in separate set_sinks calls, and losing either is
        # the failure this guards.
        rrd = tmp_path / "session.rrd"
        assert rrd.exists() and rrd.stat().st_size > self.MIN_BYTES, (
            "the .rrd was not written; serving and recording must share one "
            "sink set or one silently replaces the other"
        )


class TestRrdCap:
    """The .rrd stops growing at its cap, and nothing else stops with it.

    This file is the largest thing a long run writes -- 3.2 MB/min measured at
    the 5,000-point default -- so a forgotten run fills a disk with it. The
    cap is what makes a forgotten run survivable, and a cap that does not fire
    is indistinguishable from no cap.
    """

    #: Small enough that a handful of frames passes it, above the ~12-byte
    #: empty-file header so the check is about growth and not about existing.
    CAP_MB = 0.05

    def test_the_file_stops_growing_once_it_is_over_the_cap(self, tmp_path):
        rr = pytest.importorskip("rerun")
        assert rr is not None

        rrd = tmp_path / "session.rrd"
        sink = build_sink(
            "rerun", node=None, run_id="cap-test", serve=False,
            rrd_path=str(rrd), rrd_max_mb=self.CAP_MB,
        )
        try:
            # The size check runs every 50 frames, so drive well past that.
            for seq in range(400):
                sink.draw(seq, cloud(2000), {"e2e_ns": 1_000_000})
            capped_at = rrd.stat().st_size if rrd.exists() else 0

            for seq in range(400, 800):
                sink.draw(seq, cloud(2000), {"e2e_ns": 1_000_000})
        finally:
            sink.close()

        final = rrd.stat().st_size
        assert capped_at > 0, "nothing was written at all"
        # Some slack: the sink is dropped between size checks, so a little
        # more can land after the threshold is crossed.
        assert final <= capped_at * 1.5, (
            f"file kept growing after the cap: {capped_at} -> {final}"
        )

    def test_zero_lifts_the_cap(self, tmp_path):
        rr = pytest.importorskip("rerun")
        assert rr is not None

        rrd = tmp_path / "session.rrd"
        sink = build_sink(
            "rerun", node=None, run_id="nocap-test", serve=False,
            rrd_path=str(rrd), rrd_max_mb=0,
        )
        try:
            for seq in range(200):
                sink.draw(seq, cloud(2000), {"e2e_ns": 1_000_000})
            early = rrd.stat().st_size
            for seq in range(200, 800):
                sink.draw(seq, cloud(2000), {"e2e_ns": 1_000_000})
        finally:
            sink.close()

        assert rrd.stat().st_size > early, "uncapped file should keep growing"
