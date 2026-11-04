#!/usr/bin/env python3
"""Convert a recorded lidar dataset into the lidar client's replay format.

    python3 tools/lidar_dataset.py ford ~/IJRR-Dataset-1/SCANS datasets/ford-campus-1
    python3 tools/lidar_dataset.py ford <SCANS> <out> --first 100 --count 600

The lidar client replays it with `pattern=dataset` (ros2/README.md). Reading
the source format while a run is on would put its decoding -- a Ford scan is
6 MB of zlib-compressed doubles -- inside the frame period and the sender
delay, so it is converted once, here.

Sources:

- ``ford`` -- the Ford Campus Vision and Lidar Dataset (Pandey, McBride and
  Eustice, IJRR 2011): ``SCANS/ScanNNNN.mat``, MATLAB v5 files, one Velodyne
  HDL-64E sweep each, ``SCAN.XYZ`` (3 x N metres, vehicle frame) and
  ``SCAN.timestamp_laser`` (microseconds). 10 Hz, about 78 000 points a sweep.

The replay format (version 1), one directory:

- ``points.f32`` -- every frame's points, float32 little-endian, x y z
  interleaved, frames back to back;
- ``index.json`` -- ``{"format": "mec-cast-lidar-replay", "version": 1,
  "source", "frame_id", "units", "frames": [[scan, offset, count, t_us], ...]}``
  where ``offset`` and ``count`` are in points and ``t_us`` is the recorded
  sweep time.

Standard library only, so it runs where numpy and scipy do not. The data is
the dataset's, under its own terms: keep it out of the repository
(``datasets/`` is ignored by git, docker and the deploy).
"""

from __future__ import annotations

import argparse
import array
import json
import os
import re
import struct
import sys
import time
import zlib
from pathlib import Path

FORMAT = "mec-cast-lidar-replay"
VERSION = 1

# MAT v5 data types and array classes (the MATLAB "MAT-File Format" spec).
MI_INT8, MI_INT32, MI_UINT32, MI_DOUBLE, MI_MATRIX, MI_COMPRESSED = 1, 5, 6, 9, 14, 15
MX_STRUCT = 2
_NUMERIC = {1: "b", 2: "B", 3: "h", 4: "H", 5: "i", 6: "I", 7: "f", 9: "d", 12: "q", 13: "Q"}


class MatError(ValueError):
    """A file that is not the MAT v5 layout this reader understands."""


def _elements(buf: bytes):
    """(type, payload) for each data element in ``buf``."""
    i = 0
    while i + 8 <= len(buf):
        t, n = struct.unpack_from("<II", buf, i)
        if t >> 16:  # small element: size in the upper half, payload inline
            n, t = t >> 16, t & 0xFFFF
            yield t, buf[i + 4 : i + 4 + n]
            i += 8
        else:
            yield t, buf[i + 8 : i + 8 + n]
            i += 8 + ((n + 7) // 8) * 8


def _numeric(t: int, payload: bytes) -> array.array:
    code = _NUMERIC.get(t)
    if code is None:
        raise MatError(f"unsupported numeric type {t}")
    a = array.array(code)
    a.frombytes(payload)
    if sys.byteorder != "little":
        a.byteswap()
    return a


def _matrix(payload: bytes):
    """A miMATRIX: (name, value). Structs become dicts of their fields,
    numeric arrays (dims, array.array) in MATLAB's column-major order."""
    els = list(_elements(payload))
    flags = struct.unpack_from("<I", els[0][1])[0]
    dims = tuple(_numeric(MI_INT32, els[1][1]))
    name = els[2][1].decode("ascii", "replace")
    if flags & 0xFF == MX_STRUCT:
        field_len = struct.unpack_from("<i", els[3][1])[0]
        raw = els[4][1]
        fields = [
            raw[k : k + field_len].split(b"\0")[0].decode() for k in range(0, len(raw), field_len)
        ]
        # A 1x1 struct: one value per field. Larger struct arrays (the Ford
        # files' per-camera `Cam`) are not needed and are skipped.
        if dims != (1, 1):
            return name, None
        out = {}
        for f, (_, d) in zip(fields, els[5:]):
            try:
                out[f] = _matrix(d)[1]
            except (MatError, struct.error, IndexError, ValueError):
                out[f] = None  # cut off: see read_mat
        return name, out
    if len(els) < 4:
        return name, None
    t, data = els[3]
    try:
        return name, (dims, _numeric(t, data))
    except MatError:
        return name, None


def read_mat(path: Path) -> tuple[dict, bool]:
    """The top-level variables of a MAT v5 file, and whether its compressed
    data ended early.

    Five of the Ford run-1 scans (0111, 1281, 1895, 2379, 3718) are their
    declared size but their zlib streams stop short of the end marker. What
    decodes is sound, and holds XYZ and the timestamps -- the struct's first
    fields -- whole; only the trailing camera projections are cut. So a
    stream that ends early is read as far as it goes, and the caller decides
    whether what it needs survived.
    """
    raw = path.read_bytes()
    if not raw.startswith(b"MATLAB 5.0 MAT-file"):
        raise MatError(f"{path}: not a MAT v5 file (v7.3 files are HDF5; convert those first)")
    out, cut = {}, False
    for t, data in _elements(raw[128:]):
        if t == MI_COMPRESSED:
            inflate = zlib.decompressobj()
            data = inflate.decompress(data)
            cut = cut or not inflate.eof
            t, data = next(_elements(data))
        if t == MI_MATRIX:
            name, value = _matrix(data)
            out[name] = value
    return out, cut


def ford_scan(path: Path) -> tuple[array.array, int, bool]:
    """One Ford sweep: float32 x y z interleaved, its time in us, and whether
    it was recovered from a file whose data ends early."""
    variables, cut = read_mat(path)
    scan = variables.get("SCAN")
    if not isinstance(scan, dict) or scan.get("XYZ") is None:
        raise MatError(f"{path}: no SCAN.XYZ" + (" (the file's data ends early)" if cut else ""))
    (rows, cols), xyz = scan["XYZ"]
    if rows != 3 or len(xyz) != rows * cols:
        raise MatError(f"{path}: SCAN.XYZ is {rows}x{cols} with {len(xyz)} values, expected 3xN")
    if cut and scan.get("timestamp_laser") is None:
        raise MatError(f"{path}: the file's data ends before SCAN.timestamp_laser")
    # MATLAB stores 3xN column-major: x1 y1 z1 x2 ... -- already interleaved.
    points = array.array("f", xyz)
    t = scan.get("timestamp_laser")
    t_us = int(t[1][0]) if t else 0
    return points, t_us, cut


SOURCES = {
    "ford": {
        "glob": "Scan*.mat",
        "read": ford_scan,
        "source": "Ford Campus Vision and Lidar Dataset (IJRR 2011), Velodyne HDL-64E",
        "frame_id": "vehicle",
    },
}


def scan_number(path: Path) -> int:
    m = re.search(r"(\d+)", path.stem)
    return int(m.group(1)) if m else -1


def convert(kind: str, src: Path, out: Path, first: int = 0, count: int | None = None) -> dict:
    spec = SOURCES[kind]
    files = sorted(src.glob(spec["glob"]), key=scan_number)
    if not files:
        raise SystemExit(f"{src}: no {spec['glob']} files")
    files = files[first : None if count is None else first + count]
    out.mkdir(parents=True, exist_ok=True)
    tmp = out / "points.f32.part"
    frames = []
    recovered = []
    offset = 0
    started = time.monotonic()
    with tmp.open("wb") as f:
        for i, path in enumerate(files):
            points, t_us, cut = spec["read"](path)
            if cut:
                recovered.append(scan_number(path))
                print(f"  {path.name}: data ends early; XYZ and timestamp whole, kept",
                      file=sys.stderr)
            if sys.byteorder != "little":
                points.byteswap()
            f.write(points.tobytes())
            n = len(points) // 3
            frames.append([scan_number(path), offset, n, t_us])
            offset += n
            if (i + 1) % 100 == 0 or i + 1 == len(files):
                rate = (i + 1) / max(time.monotonic() - started, 1e-9)
                print(f"  {i + 1}/{len(files)} sweeps, {offset * 12 / 1e9:.2f} GB, {rate:.1f}/s",
                      file=sys.stderr)
    index = {
        "format": FORMAT,
        "version": VERSION,
        "source": spec["source"],
        "source_path": str(src),
        "frame_id": spec["frame_id"],
        "units": "m",
        "dtype": "float32",
        # Scans read from files whose compressed data ends early, their points
        # and timestamp whole (read_mat): kept, and named here.
        "recovered": recovered,
        "frames": frames,
    }
    os.replace(tmp, out / "points.f32")
    (out / "index.json").write_text(json.dumps(index, separators=(",", ":")) + "\n")
    return index


def summary(index: dict) -> str:
    frames = index["frames"]
    counts = sorted(f[2] for f in frames)
    span = (frames[-1][3] - frames[0][3]) / 1e6 if len(frames) > 1 else 0.0
    rate = (len(frames) - 1) / span if span > 0 else 0.0
    return (
        f"{len(frames)} frames (scans {frames[0][0]}..{frames[-1][0]}), "
        f"{span:.1f} s at {rate:.2f} Hz, points per frame {counts[0]}..{counts[-1]} "
        f"(median {counts[len(counts) // 2]})"
        + (f", {len(index['recovered'])} recovered from short files" if index.get("recovered") else "")
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("kind", choices=sorted(SOURCES), help="source dataset")
    ap.add_argument("src", type=Path, help="the dataset's scan directory")
    ap.add_argument("out", type=Path, help="output directory, e.g. datasets/ford-campus-1")
    ap.add_argument("--first", type=int, default=0, help="skip this many sweeps")
    ap.add_argument("--count", type=int, help="convert at most this many")
    args = ap.parse_args(argv)
    index = convert(args.kind, args.src, args.out, args.first, args.count)
    print(f"{args.out}: {summary(index)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
