"""Replaying a recorded dataset: the frame a seq maps to, subsampling, the
end of a non-looping run, and refusing what is not a replay directory.

The format is written here directly, as tools/lidar_dataset.py writes it
(that script is outside the image's build context; its own tests are in
ran/py/tests/test_lidar_dataset.py).
"""

import json

import numpy as np
import pytest

from mec_cast_lidar_client.replay import Replay, ReplayError, resolve


def write_dataset(path, counts=(5, 7, 6), version=1):
    """Frame i's points are all (i, i, i) plus a ramp, so a frame is
    recognisable by its value."""
    path.mkdir(parents=True)
    frames, offset, chunks = [], 0, []
    for i, n in enumerate(counts):
        pts = np.full((n, 3), float(i), dtype="<f4")
        pts[:, 1] += np.arange(n, dtype="<f4")
        chunks.append(pts)
        frames.append([100 + i, offset, n, 1_000_000 + 100_000 * i])
        offset += n
    (path / "points.f32").write_bytes(np.concatenate(chunks).tobytes())
    (path / "index.json").write_text(json.dumps({
        "format": "mec-cast-lidar-replay", "version": version, "source": "test",
        "frame_id": "vehicle", "units": "m", "dtype": "float32", "frames": frames,
    }))
    return path


def test_a_seq_replays_its_frame_and_wraps(tmp_path):
    r = Replay(write_dataset(tmp_path / "d"))
    assert len(r) == 3
    assert [r.frame(k)[0, 0] for k in range(5)] == [0.0, 1.0, 2.0, 0.0, 1.0]
    assert r.frame(1).shape == (7, 3) and r.frame(1).dtype == np.float32
    assert r.scan_of(2) == 102, "the source's own scan number, to look it up"


def test_start_offsets_the_run(tmp_path):
    r = Replay(write_dataset(tmp_path / "d"), start=2)
    assert [r.frame(k)[0, 0] for k in range(3)] == [2.0, 0.0, 1.0]


def test_without_loop_the_run_ends(tmp_path):
    r = Replay(write_dataset(tmp_path / "d"), start=1, loop=False)
    assert r.frame(1)[0, 0] == 2.0
    assert r.frame(2) is None and r.index_of(2) is None
    r.warm(5)  # past the end: nothing to read, and no error


def test_max_points_keeps_an_even_subsample(tmp_path):
    r = Replay(write_dataset(tmp_path / "d", counts=(101,)), max_points=5)
    ys = r.frame(0)[:, 1].tolist()
    assert ys == [0.0, 25.0, 50.0, 75.0, 100.0], "first to last, evenly spaced"
    assert Replay(write_dataset(tmp_path / "e", counts=(3,)), max_points=5).frame(0).shape == (3, 3)


def test_what_is_not_a_replay_is_refused(tmp_path):
    with pytest.raises(ReplayError, match="no dataset"):
        Replay(tmp_path / "missing")
    with pytest.raises(ReplayError, match="expected"):
        Replay(write_dataset(tmp_path / "v2", version=2))
    d = write_dataset(tmp_path / "short")
    (d / "points.f32").write_bytes((d / "points.f32").read_bytes()[:-12])
    with pytest.raises(ReplayError, match="interrupted"):
        Replay(d)
    with pytest.raises(ReplayError, match="dataset_start"):
        Replay(write_dataset(tmp_path / "s"), start=3)


def test_a_name_resolves_under_datasets_dir(tmp_path, monkeypatch):
    write_dataset(tmp_path / "ford-1")
    monkeypatch.setenv("DATASETS_DIR", str(tmp_path))
    assert resolve("ford-1") == tmp_path / "ford-1"
    assert resolve(str(tmp_path / "ford-1")) == tmp_path / "ford-1"
    with pytest.raises(ReplayError, match="needs a dataset"):
        resolve("")
