"""G-PCC point-cloud coding through libtmc3codec, the C interface of our fork
of MPEG's reference codec (third_party/mpeg-pcc-tmc13, ADR-0014).

Two shapes of cloud cross the 5G link, and each has its coder:

- **positions in metres** (the uplink): float32 (N, 3), quantised to
  ``precision_m`` — 0.001 or 0.01. The codec truncates, so a precision that
  divides the edge's voxel size leaves its result unchanged.
- **integer grid indices** (the downlink's voxels): int32 (N, 3), coded
  losslessly — ``precision_m=None``.

**Every frame is a stream of its own.** Each `Encoder.encode` and each
`decode` call creates the codec, codes one frame and destroys it, which adds
0.5 to 0.8 ms to a few ms of coding. Every frame is then frame 0 of a
one-frame stream, which is what the reference software handles best -- a
single-frame file -- and which frames dropped by design (best-effort,
KEEP_LAST(1)) need anyway: no inter prediction, parameter sets in every
frame. It also avoids two faults of the reference decoder, found while
testing this module:

- A long-lived decoder recognises a new frame only by a change of the frame
  counter, so streams interleaved on one topic -- several lidar instances,
  each counting from 0 -- fold into one frame, and it has crashed on that.
- A fresh decoder handed a frame whose counter is not 0 reads an
  uninitialised member (PredGeomPredictor::globalMotionEnabled, set only on
  frame 0) and crashes, intermittently, on Linux.

Coordinates are coded relative to a fixed origin (``origin_m``). The codec's
default takes the origin from the first frame's bounding box and silently
clamps any later point below it onto it, so a moving shape loses its lower
edge; a point below the origin raises here instead.

The library is loaded with ctypes. Arrays go in without a copy and the GIL is
released during every call. Since no codec outlives a call, `encode` and
`decode` may run on any thread.

No ROS imports here, so this module is tested wherever the library is.
"""

from __future__ import annotations

import ctypes as C
import os
from functools import lru_cache

import numpy as np

#: The library name the ROS image installs (/usr/local/lib). Set
#: MEC_CAST_TMC3CODEC to a path to use another build, e.g. on a laptop.
LIBRARY = "libtmc3codec.so"

_UINT16, _INT32, _FLOAT32, _FLOAT64 = 2, 3, 4, 5
_OK, _NO_FRAME = 0, 1


class CodecError(RuntimeError):
    """A call into libtmc3codec failed; the message is the codec's own."""


class _Array(C.Structure):
    _fields_ = [("data", C.c_void_p), ("dtype", C.c_int),
                ("stride", C.c_int64), ("offset", C.c_int64 * 3)]


class _Cloud(C.Structure):
    _fields_ = [("num_points", C.c_int64), ("position", _Array),
                ("colour", _Array), ("reflectance", _Array)]


class _FrameInfo(C.Structure):
    _fields_ = [("num_points", C.c_int64), ("position", _Array),
                ("colour", _Array), ("reflectance", _Array),
                ("frame_num", C.c_int32), ("scale", C.c_double),
                ("origin", C.c_double * 3)]


@lru_cache(maxsize=1)
def library() -> C.CDLL:
    """Load libtmc3codec once per process, with every signature declared:
    ctypes would otherwise pass handles as 32-bit ints."""
    lib = C.CDLL(os.environ.get("MEC_CAST_TMC3CODEC", LIBRARY))
    handle, out = C.c_void_p, C.POINTER(C.c_void_p)
    sigs = {
        "tmc3_version": ([], C.c_char_p),
        "tmc3_api_version": ([], C.c_int),
        "tmc3_last_error": ([], C.c_char_p),
        "tmc3_encoder_create": ([C.POINTER(C.c_char_p), C.c_int, out], C.c_int),
        "tmc3_encoder_encode": ([handle, C.POINTER(_Cloud), out, out], C.c_int),
        "tmc3_encoder_destroy": ([handle], None),
        "tmc3_packet_data": ([handle], C.c_void_p),
        "tmc3_packet_size": ([handle], C.c_size_t),
        "tmc3_packet_release": ([handle], None),
        "tmc3_decoder_create": ([C.POINTER(C.c_char_p), C.c_int, out], C.c_int),
        "tmc3_decoder_push": ([handle, C.c_char_p, C.c_size_t], C.c_int),
        "tmc3_decoder_end_frame": ([handle], C.c_int),
        "tmc3_decoder_flush": ([handle], C.c_int),
        "tmc3_decoder_next_frame": ([handle, out], C.c_int),
        "tmc3_decoder_destroy": ([handle], None),
        "tmc3_frame_get_info": ([handle, C.POINTER(_FrameInfo)], C.c_int),
        "tmc3_frame_get_positions": ([handle, C.POINTER(_Array)], C.c_int),
        "tmc3_frame_release": ([handle], None),
    }
    for name, (args, res) in sigs.items():
        fn = getattr(lib, name)
        fn.argtypes, fn.restype = args, res
    return lib


def version() -> str:
    """The codec's version string, and the C interface's version."""
    lib = library()
    return f"{(lib.tmc3_version() or b'').decode()} api {lib.tmc3_api_version()}"


def _check(status: int) -> int:
    if status < 0:
        message = library().tmc3_last_error() or b"unknown error"
        raise CodecError(f"tmc3 error {status}: {message.decode()}")
    return status


def _options(options: list[str]) -> tuple:
    if not options:
        return None, 0
    return (C.c_char_p * len(options))(*[o.encode() for o in options]), len(options)


#: The sequence box size the encoder defaults to, (1 << 21) - 1 per axis.
_SEQ_SIZE = (1 << 21) - 1


def _scale(precision_m: float | None) -> float:
    if precision_m is not None and not precision_m > 0:
        raise ValueError(f"precision_m must be positive or None, got {precision_m!r}")
    return 1.0 if precision_m is None else 1.0 / precision_m


def encoder_options(precision_m: float | None,
                    origin_m: tuple[float, float, float] = (0.0, 0.0, 0.0)) -> list[str]:
    """The tmc3 options for one of the two clouds.

    - The bitstream declares metres (srcUnit, srcUnitLength), so a decoder
      asked for metres returns metres; without it the decoder returns the
      coding units, millimetres at 1 mm.
    - A fixed origin, not one taken from the first frame (autoSeqBbox=0,
      seqOrigin).
    - Duplicate points are kept, not merged, so the decoded cloud has as many
      points as the one sent.
    - Every frame intra: no inter prediction, a random access point each.
    """
    scale = _scale(precision_m)
    origin = ",".join(str(int(np.floor(o * scale))) for o in origin_m)
    return [
        f"inputScale={scale:.12g}",
        "srcUnit=metre",
        "srcUnitLength=1",
        "autoSeqBbox=0",
        f"seqOrigin={origin}",
        # Required with autoSeqBbox=0. Left unset, the reference encoder takes
        # the box's maximum from a size of 0 before applying its default, and
        # signals a box of size 0. This is the default it means to apply; the
        # size is metadata, not a clamp.
        f"seqSizeWhd={_SEQ_SIZE},{_SEQ_SIZE},{_SEQ_SIZE}",
        "mergeDuplicatedPoints=0",
        "interPredictionEnabled=0",
        "randomAccessPeriod=1",
    ]


#: What the decoder needs to return metres. Options the bitstream carries
#: (precision, units) need not be repeated: the decoder reads them from it.
DECODER_OPTIONS = ["outputScaling=1", "outputUnitLength=1"]


class Encoder:
    """Codes one cloud per call into one self-contained frame.

    ``precision_m`` quantises metres (float32 input); None codes integer
    indices losslessly (int32 input). No point may lie below ``origin_m``
    (in the input's units: metres, or indices). ``extra`` options are tmc3
    options appended after ours, for experiments.

    The options are checked here, by creating the codec once, so a bad
    configuration fails when a run starts rather than at its first frame.
    """

    def __init__(self, precision_m: float | None,
                 origin_m: tuple[float, float, float] = (0.0, 0.0, 0.0),
                 extra: tuple[str, ...] = ()) -> None:
        self.precision_m = precision_m
        self.origin_m = np.asarray(origin_m, dtype=np.float64)
        self._dtype = np.int32 if precision_m is None else np.float32
        self._options = encoder_options(precision_m, origin_m) + list(extra)
        library().tmc3_encoder_destroy(self._create())

    def _create(self) -> C.c_void_p:
        handle = C.c_void_p()
        opts, n = _options(self._options)
        _check(library().tmc3_encoder_create(opts, n, C.byref(handle)))
        return handle

    def encode(self, points: np.ndarray) -> bytes:
        if points.ndim != 2 or points.shape[1] != 3 or points.dtype != self._dtype:
            raise ValueError(
                f"expected an (N, 3) {np.dtype(self._dtype).name} array, "
                f"got {points.shape} {points.dtype}"
            )
        if len(points) == 0:
            raise ValueError("an empty cloud cannot be coded")
        low = points.min(axis=0)
        if np.any(low < self.origin_m):
            raise ValueError(
                f"a point lies below the coding origin {self.origin_m.tolist()}: "
                f"min {low.tolist()}. The codec would clamp it onto the origin."
            )
        # The codec reads the array in place; a strided view is passed as such.
        # A reversed view has negative strides, which tmc3_array cannot say.
        pts = points if min(points.strides) >= 0 else np.ascontiguousarray(points)
        stride = pts.strides[0]
        offsets = (0, pts.strides[1], 2 * pts.strides[1])
        dtype = _INT32 if self._dtype is np.int32 else _FLOAT32
        cloud = _Cloud(len(pts), _Array(pts.ctypes.data, dtype, stride, offsets),
                       _Array(), _Array())
        lib = library()
        handle = self._create()
        try:
            packet = C.c_void_p()
            _check(lib.tmc3_encoder_encode(handle, C.byref(cloud), C.byref(packet), None))
            try:
                return C.string_at(lib.tmc3_packet_data(packet), lib.tmc3_packet_size(packet))
            finally:
                lib.tmc3_packet_release(packet)
        finally:
            lib.tmc3_encoder_destroy(handle)


def decode(data: bytes, dtype=np.float32) -> np.ndarray:
    """Decode one coded frame with a decoder of its own.

    Returns float32 metres, or int32 indices with ``dtype=np.int32`` for a
    cloud coded with ``precision_m=None``. Raises CodecError when the data is
    not one whole frame.
    """
    lib = library()
    handle = C.c_void_p()
    opts, n = _options(DECODER_OPTIONS)
    _check(lib.tmc3_decoder_create(opts, n, C.byref(handle)))
    try:
        _check(lib.tmc3_decoder_push(handle, data, len(data)))
        # Ends the stream: the frame is output, and a partial data unit left
        # over is an error rather than data kept for a next call.
        _check(lib.tmc3_decoder_flush(handle))
        frame = C.c_void_p()
        if _check(lib.tmc3_decoder_next_frame(handle, C.byref(frame))) == _NO_FRAME:
            raise CodecError("the data held no complete frame")
        try:
            info = _FrameInfo()
            _check(lib.tmc3_frame_get_info(frame, C.byref(info)))
            # Positions arrive as float; indices are exact integers in float64.
            as_float = np.float64 if dtype == np.int32 else np.float32
            out = np.empty((info.num_points, 3), dtype=as_float)
            dst = _Array(out.ctypes.data, _FLOAT64 if as_float is np.float64 else _FLOAT32,
                         0, (0, 0, 0))
            _check(lib.tmc3_frame_get_positions(frame, C.byref(dst)))
        finally:
            lib.tmc3_frame_release(frame)
    finally:
        lib.tmc3_decoder_destroy(handle)
    if dtype == np.int32:
        return np.rint(out).astype(np.int32)
    return out
