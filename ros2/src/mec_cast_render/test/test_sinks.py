"""The sink table: selection, failure modes, and the null path.

These are deliberately not ROS tests. The point of `sinks.py` is that the
measurement path does not depend on a renderer, so the parts that decide
*which* renderer runs must be exercisable without one — and without ROS
message packages, which is why `sensor_msgs` is imported lazily.
"""

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
    return build_sink(kind, node=node, run_id="test-run", serve=False, address="")


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
