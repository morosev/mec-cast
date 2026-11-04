"""Recorded lidar sweeps, replayed frame by frame: `pattern=dataset`.

Reads the replay format `tools/lidar_dataset.py` writes -- `points.f32`
(float32 x y z, frames back to back) and `index.json` -- through a memory
map, so a 3.6 GB recording costs no memory until a frame is read, and the
page cache keeps what a looping run reads again.

Frame k of a run is sweep `start + k`, wrapping at the end when `loop` is set:
deterministic in (dataset, start, seq), as the seeded patterns are in
(seed, seq), so a run is reproducible frame for frame. `max_points` keeps an
even subsample of each sweep -- the payload-size knob on real geometry; 0
keeps every point.

Reading a frame touches about 1 MB of the map. `warm()` reads the next one's
pages after a publish, so the frame read inside the next capture-to-send span
comes from memory rather than the disk.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np

FORMAT = "mec-cast-lidar-replay"
VERSION = 1


class ReplayError(ValueError):
    """A dataset that cannot be replayed: missing, or not this format."""


def resolve(dataset: str, base: str | None = None) -> Path:
    """A dataset by path, or by name under DATASETS_DIR (default `datasets`)."""
    if not dataset:
        raise ReplayError("pattern=dataset needs a dataset: set `dataset` (or DATASET)")
    path = Path(dataset)
    if path.is_absolute() or (path / "index.json").exists():
        return path
    return Path(base or os.environ.get("DATASETS_DIR") or "datasets") / dataset


class Replay:
    def __init__(self, path: str | Path, *, start: int = 0, loop: bool = True,
                 max_points: int = 0) -> None:
        self.path = Path(path)
        index_path = self.path / "index.json"
        try:
            index = json.loads(index_path.read_text())
        except FileNotFoundError:
            raise ReplayError(
                f"no dataset at {self.path} (no index.json); convert one with "
                "tools/lidar_dataset.py"
            ) from None
        if index.get("format") != FORMAT or index.get("version") != VERSION:
            raise ReplayError(
                f"{index_path}: format {index.get('format')!r} v{index.get('version')}, "
                f"expected {FORMAT!r} v{VERSION}"
            )
        self.source = str(index.get("source") or self.path.name)
        self.frames = [tuple(f) for f in index["frames"]]
        if not self.frames:
            raise ReplayError(f"{index_path}: no frames")
        total = self.frames[-1][1] + self.frames[-1][2]
        points = self.path / "points.f32"
        expected = total * 12
        actual = points.stat().st_size if points.exists() else -1
        if actual != expected:
            raise ReplayError(
                f"{points}: {actual} bytes, index.json says {expected} -- an interrupted "
                "conversion? Convert again."
            )
        self.points = np.memmap(points, dtype="<f4", mode="r", shape=(total, 3))
        if not 0 <= start < len(self.frames):
            raise ReplayError(f"dataset_start {start} outside 0..{len(self.frames) - 1}")
        self.start = start
        self.loop = loop
        self.max_points = max(0, int(max_points))

    def __len__(self) -> int:
        return len(self.frames)

    def index_of(self, seq: int) -> int | None:
        """Which frame of the dataset a run's seq replays; None once a
        non-looping run has replayed the last."""
        k = self.start + seq
        if k < len(self.frames):
            return k
        return k % len(self.frames) if self.loop else None

    def scan_of(self, seq: int) -> int | None:
        """The source's own scan number for a seq: what to look up in the
        original dataset."""
        k = self.index_of(seq)
        return None if k is None else self.frames[k][0]

    def frame(self, seq: int) -> np.ndarray | None:
        """The points of a run's frame `seq`, float32 (n, 3), or None past
        the end of a non-looping run."""
        k = self.index_of(seq)
        if k is None:
            return None
        _, offset, count, _ = self.frames[k]
        rows = self.points[offset : offset + count]
        if self.max_points and count > self.max_points:
            # Evenly spaced, so the subsample keeps every part of the sweep
            # rather than its first beams.
            rows = rows[np.linspace(0, count - 1, self.max_points).astype(np.int64)]
        return np.ascontiguousarray(rows, dtype=np.float32)

    def warm(self, seq: int) -> None:
        """Read frame `seq`'s pages into memory, discarding the result."""
        k = self.index_of(seq)
        if k is None:
            return
        _, offset, count, _ = self.frames[k]
        # One value per 4 KiB page is enough to fault the page in.
        self.points[offset : offset + count : 341].sum()

    def describe(self) -> dict:
        return {
            "dataset": self.path.name,
            "dataset_source": self.source,
            "dataset_frames": len(self.frames),
            "dataset_start": self.start,
            "dataset_loop": self.loop,
            "dataset_points": self.max_points,
        }
