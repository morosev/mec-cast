"""G-PCC round trips on the clouds mec-cast actually sends (ADR-0014).

The clouds come from the lidar client's own generators, and the edge's own
`process_cloud` judges the result, so "the codec does not change what the
edge computes" is tested against the real code rather than a copy of it.
"""

import numpy as np
import pytest

from mec_cast_codec.gpcc import CodecError, Encoder, decode, version
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


def test_the_library_loads():
    assert "api" in version()


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
    pts = list(clouds("lidar_scan"))
    enc = Encoder(0.001)
    packets = [enc.encode(p) for p in pts]
    for i in (2, 0, 1, 1):
        assert decode(packets[i]).shape == pts[i].shape


def test_a_frame_codes_the_same_whatever_came_before():
    """Every frame is frame 0 of its own stream: the encoder keeps no state
    from one frame to the next."""
    a, b = list(clouds("sphere", 2))
    enc = Encoder(0.001)
    first = enc.encode(b)
    enc.encode(a)
    assert enc.encode(b) == first


def test_two_interleaved_streams_decode_as_they_would_alone():
    """Several lidar instances publish on one topic, so the edge sees their
    frames interleaved. A long-lived reference decoder folds frames with
    equal counters into one, and has crashed on it; per-frame codecs do not."""
    a, b = list(clouds("lidar_scan", 10)), list(clouds("sphere", 10))
    ea, eb = Encoder(0.001), Encoder(0.001)
    for pa, pb in zip(a, b):
        for pts, data in ((pa, ea.encode(pa)), (pb, eb.encode(pb))):
            out = decode(data)
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


def test_a_negative_origin_codes_negative_coordinates():
    """A real lidar reports points around itself, negative included."""
    pts = next(clouds("sphere")) - 5.0
    assert pts.min() < 0
    out = decode(Encoder(0.001, origin_m=(-10.0, -10.0, -10.0)).encode(pts))
    assert max_error(out, pts) <= 0.001 + 1e-5


def test_a_point_below_the_origin_is_refused_not_clamped():
    pts = next(clouds("sphere")) - 5.0
    with pytest.raises(ValueError, match="below the coding origin"):
        Encoder(0.001).encode(pts)


def test_strided_input_is_read_in_place_and_codes_the_same():
    every_other = next(clouds("uniform_cube"))[::2]
    assert not every_other.flags.c_contiguous
    enc = Encoder(0.001)
    assert enc.encode(every_other) == enc.encode(np.ascontiguousarray(every_other))


@pytest.mark.parametrize("bad", [b"", b"not a point cloud", None])
def test_a_bad_frame_raises_and_the_next_one_decodes(bad):
    pts = next(clouds("sphere"))
    good = Encoder(0.001).encode(pts)
    if bad is None:
        bad = good[: len(good) // 2]  # a truncated frame
    with pytest.raises(CodecError):
        decode(bad)
    assert decode(good).shape == pts.shape


def test_the_input_is_checked_before_the_codec_sees_it():
    enc = Encoder(0.001)
    for wrong in (np.zeros((10, 3), np.float64), np.zeros((0, 3), np.float32),
                  np.zeros((10, 2), np.float32)):
        with pytest.raises(ValueError):
            enc.encode(wrong)
    with pytest.raises(ValueError):
        Encoder(None).encode(np.zeros((10, 3), np.float32))
    for precision in (0, -0.01):
        with pytest.raises(ValueError):
            Encoder(precision)


def test_a_bad_codec_option_fails_when_the_encoder_is_made():
    """Not at the first frame of a run."""
    with pytest.raises(CodecError):
        Encoder(0.001, extra=("noSuchOption=1",))
