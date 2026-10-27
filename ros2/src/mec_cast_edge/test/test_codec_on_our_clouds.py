"""G-PCC round trips on the clouds mec-cast actually sends (ADR-0014).

The clouds come from the lidar client's own generators, and this package's
`process_cloud` judges the result, so "the codec does not change what the
edge computes" is tested against the real code rather than a copy of it.
These live here, not in mec_cast_codec, because the codec is a dependency of
the edge and of the lidar client: tests there importing them would make a
cycle colcon refuses.
"""

import numpy as np
import pytest

from mec_cast_codec.gpcc import Decoder, Encoder, decode
from mec_cast_edge.edge_node import VOXEL_SIZE, process_cloud
from mec_cast_lidar_client.publisher_node import PATTERNS, PointCloudPublisher

POINTS = 5000
FRAMES = 3
PRECISIONS = (0.001, 0.01)


def clouds(pattern: str, frames: int = FRAMES, num_points: int = POINTS):
    """The publisher's frames 0..frames-1, without a ROS node: the
    generators read only these attributes."""
    gen = PointCloudPublisher.__new__(PointCloudPublisher)
    gen.pattern, gen.num_points, gen.seed = pattern, num_points, 42
    gen.rng = np.random.default_rng(gen.seed)
    for seq in range(frames):
        gen.seq = seq
        yield gen.generate_points()


def by_column(points: np.ndarray) -> np.ndarray:
    """Each column sorted on its own. The decoder reorders points, but
    truncation is monotone per axis, so sorted columns pair each coordinate
    with its own quantised value."""
    return np.sort(points.astype(np.float64), axis=0)


def max_error(out: np.ndarray, points: np.ndarray) -> float:
    return float(np.abs(by_column(out) - by_column(points)).max())


def as_rows(indices: np.ndarray) -> np.ndarray:
    return indices[np.lexsort(indices.T[::-1])]


@pytest.mark.parametrize("precision", PRECISIONS)
@pytest.mark.parametrize("pattern", PATTERNS)
def test_uplink_keeps_every_point_within_the_precision(pattern, precision):
    enc = Encoder(precision)
    for pts in clouds(pattern):
        out = decode(enc.encode(pts))
        assert out.dtype == np.float32 and out.shape == pts.shape
        # Truncation to the step, plus float32 rounding of metres.
        assert max_error(out, pts) <= precision + 1e-5


@pytest.mark.parametrize("precision", PRECISIONS)
@pytest.mark.parametrize("pattern", PATTERNS)
def test_uplink_leaves_the_edges_voxels_unchanged(pattern, precision):
    """The precision divides VOXEL_SIZE and the codec truncates, so no point
    crosses a voxel boundary: the occupied voxels are identical."""
    assert round(VOXEL_SIZE / precision) * precision == pytest.approx(VOXEL_SIZE)
    enc = Encoder(precision)
    for pts in clouds(pattern):
        _, before = process_cloud(pts)
        _, after = process_cloud(decode(enc.encode(pts)))
        np.testing.assert_array_equal(as_rows(after), as_rows(before))


@pytest.mark.parametrize("pattern", PATTERNS)
def test_downlink_voxel_indices_are_lossless(pattern):
    enc = Encoder(None)
    for pts in clouds(pattern):
        _, voxels = process_cloud(pts)
        data = enc.encode(voxels)
        np.testing.assert_array_equal(as_rows(decode(data, dtype=np.int32)), as_rows(voxels))
        assert len(data) < voxels.nbytes


def test_lidar_scan_codes_well_below_raw():
    """A regression guard on the measurement in ADR-0014 (11.5x at 1 mm),
    loose enough not to fail on a codec version's small changes."""
    enc = Encoder(0.001)
    sizes = [len(enc.encode(p)) for p in clouds("lidar_scan")]
    assert max(sizes) < POINTS * 12 / 8


def test_every_frame_decodes_on_its_own_in_any_order():
    """No inter prediction, parameter sets in every frame: one decoder, or a
    fresh one, decodes any frame without those before it."""
    pts = list(clouds("lidar_scan"))
    enc = Encoder(0.001)
    packets = [enc.encode(p) for p in pts]
    dec = Decoder()
    for i in (2, 0, 1, 1):
        assert dec.decode(packets[i]).shape == pts[i].shape
        assert decode(packets[i]).shape == pts[i].shape


def test_a_frame_decodes_the_same_whatever_came_before():
    """The encoder keeps no state that changes a frame's points: only its
    frame counter differs from one frame to the next."""
    a, b = list(clouds("sphere", 2))
    enc = Encoder(0.001)
    first = decode(enc.encode(b))
    enc.encode(a)
    np.testing.assert_array_equal(decode(enc.encode(b)), first)


def test_one_decoder_takes_interleaved_streams():
    """Several lidar instances publish on one topic, each counting frames
    from 0, so the edge's one decoder sees their frames interleaved with
    equal counters. The reference decoder folded those into one frame, and
    crashed on it; our fork starts a new frame after each one ended."""
    a, b = list(clouds("lidar_scan", 10)), list(clouds("sphere", 10))
    ea, eb = Encoder(0.001), Encoder(0.001)
    dec = Decoder()
    for pa, pb in zip(a, b):
        for pts, data in ((pa, ea.encode(pa)), (pb, eb.encode(pb))):
            out = dec.decode(data)
            assert out.shape == pts.shape
            assert max_error(out, pts) <= 0.001 + 1e-5


def test_a_moving_shape_keeps_its_lower_edge():
    """The codec's default origin is the first frame's bounding box, and a
    later point below it was clamped onto it. The torus sinks below its
    first frame's box as it tumbles."""
    pts = list(clouds("torus", 4))
    assert (pts[3].min(axis=0) < pts[0].min(axis=0)).any()
    enc = Encoder(0.001)
    enc.encode(pts[0])
    assert max_error(decode(enc.encode(pts[3])), pts[3]) <= 0.001 + 1e-5
