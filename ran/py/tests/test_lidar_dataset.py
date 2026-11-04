"""tools/lidar_dataset.py: a Ford-shaped MAT v5 scan in, the replay format out."""

import array
import importlib.util
import json
import pathlib
import struct
import zlib

REPO = pathlib.Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("lidar_dataset", REPO / "tools" / "lidar_dataset.py")
lidar_dataset = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lidar_dataset)


def element(t, payload):
    pad = (-len(payload)) % 8
    return struct.pack("<II", t, len(payload)) + payload + b"\0" * pad


def matrix(name, cls, dims, body):
    flags = element(6, struct.pack("<II", cls, 0))
    d = element(5, struct.pack(f"<{len(dims)}i", *dims))
    n = element(1, name.encode())
    return element(14, flags + d + n + body)


def double(name, rows, cols, values):
    return matrix(name, 6, (rows, cols), element(9, struct.pack(f"<{len(values)}d", *values)))


def ford_scan(path, xyz, t_us):
    """SCAN = struct(XYZ=3xN, timestamp_laser=t, X_wv), the Ford files'
    leading fields; their camera projections, last, are left out."""
    fields = ["XYZ", "timestamp_laser", "X_wv"]
    width = 32
    names = b"".join(f.encode().ljust(width, b"\0") for f in fields)
    body = (
        element(5, struct.pack("<i", width))
        + element(1, names)
        + double("", 3, len(xyz) // 3, xyz)
        + double("", 1, 1, [float(t_us)])
        + double("", 6, 1, [0.0] * 6)
    )
    scan = matrix("SCAN", 2, (1, 1), body)
    text = b"MATLAB 5.0 MAT-file, test".ljust(116, b" ")
    header = text + b"\0" * 8 + struct.pack("<HH", 0x0100, 0x4D49)
    path.write_bytes(header + element(15, zlib.compress(scan)))


def test_ford_scans_become_one_replay(tmp_path):
    src = tmp_path / "SCANS"
    src.mkdir()
    ford_scan(src / "Scan0076.mat", [1.0, 2.0, 3.0, 4.0, 5.0, 6.0], 2_000_000)
    ford_scan(src / "Scan0075.mat", [-1.5, 0.25, 9.0], 1_900_000)
    index = lidar_dataset.convert("ford", src, tmp_path / "out")

    assert index["format"] == "mec-cast-lidar-replay" and index["version"] == 1
    assert index["frames"] == [[75, 0, 1, 1_900_000], [76, 1, 2, 2_000_000]], "in scan order"
    pts = array.array("f")
    pts.frombytes((tmp_path / "out" / "points.f32").read_bytes())
    assert pts.tolist() == [-1.5, 0.25, 9.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    assert json.loads((tmp_path / "out" / "index.json").read_text()) == index
    assert not (tmp_path / "out" / "points.f32.part").exists()


def test_first_and_count_select_a_window(tmp_path):
    src = tmp_path / "SCANS"
    src.mkdir()
    for i in range(5):
        ford_scan(src / f"Scan{i:04d}.mat", [float(i)] * 3, i)
    index = lidar_dataset.convert("ford", src, tmp_path / "out", first=1, count=2)
    assert [f[0] for f in index["frames"]] == [1, 2]
    assert "2 frames (scans 1..2)" in lidar_dataset.summary(index)


def test_a_file_whose_data_ends_early_keeps_its_sweep(tmp_path):
    """As five of the Ford scans: the zlib stream stops short, after XYZ and
    the timestamp, inside what follows them."""
    src = tmp_path / "SCANS"
    src.mkdir()
    ford_scan(src / "Scan0001.mat", [1.0, 2.0, 3.0], 7)
    raw = (src / "Scan0001.mat").read_bytes()
    t, n = struct.unpack_from("<II", raw, 128)
    full = zlib.decompress(raw[136 : 136 + n])
    # Cut the stream: compress all but the last bytes, never finishing it.
    z = zlib.compressobj()
    cut = z.compress(full[: len(full) - 20]) + z.flush(zlib.Z_SYNC_FLUSH)
    (src / "Scan0001.mat").write_bytes(raw[:128] + struct.pack("<II", t, len(cut)) + cut)
    index = lidar_dataset.convert("ford", src, tmp_path / "out")
    assert index["frames"] == [[1, 0, 1, 7]]
    assert index["recovered"] == [1]


def test_a_file_that_is_not_mat_v5_is_refused(tmp_path):
    bad = tmp_path / "Scan0001.mat"
    bad.write_bytes(b"\x89HDF\r\n" + b"\0" * 200)
    try:
        lidar_dataset.read_mat(bad)
    except lidar_dataset.MatError as exc:
        assert "v7.3" in str(exc)
    else:
        raise AssertionError("an HDF5 file read as MAT v5")
