"""The G-PCC wrapper on its own: options, input checks, origins, bad frames.

Round trips on mec-cast's own clouds, judged by the edge's processing, are in
mec_cast_edge/test/test_codec_on_our_clouds.py: this package is a dependency
of the edge and the lidar client, so its tests cannot import them.
"""

import numpy as np
import pytest

from mec_cast_codec.gpcc import CodecError, Encoder, decode, version


def cloud(n: int = 5000, low: float = 0.0, seed: int = 1) -> np.ndarray:
    """A shell of radius 4 about (5, 5, 5), shifted by `low`."""
    v = np.random.default_rng(seed).standard_normal((n, 3)).astype(np.float32)
    v /= np.linalg.norm(v, axis=1, keepdims=True)
    return np.ascontiguousarray(v * 4.0 + 5.0 + low, dtype=np.float32)


def max_error(out: np.ndarray, points: np.ndarray) -> float:
    """Per column, sorted: the decoder reorders points, and truncation is
    monotone per axis."""
    a, b = (np.sort(x.astype(np.float64), axis=0) for x in (out, points))
    return float(np.abs(a - b).max())


def test_the_library_loads():
    assert "api" in version()


def test_a_negative_origin_codes_negative_coordinates():
    """A real lidar reports points around itself, negative included."""
    pts = cloud(low=-5.0)
    assert pts.min() < 0
    out = decode(Encoder(0.001, origin_m=(-10.0, -10.0, -10.0)).encode(pts))
    assert max_error(out, pts) <= 0.001 + 1e-5


def test_a_point_below_the_origin_is_refused_not_clamped():
    pts = cloud(low=-5.0)
    with pytest.raises(ValueError, match="below the coding origin"):
        Encoder(0.001).encode(pts)


def test_strided_input_is_read_in_place_and_codes_the_same():
    every_other = cloud()[::2]
    assert not every_other.flags.c_contiguous
    enc = Encoder(0.001)
    assert enc.encode(every_other) == enc.encode(np.ascontiguousarray(every_other))


@pytest.mark.parametrize("bad", [b"", b"not a point cloud", None])
def test_a_bad_frame_raises_and_the_next_one_decodes(bad):
    pts = cloud()
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
