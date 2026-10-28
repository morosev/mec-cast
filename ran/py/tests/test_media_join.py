"""tools/media_join.py: the frame legs across sites, and the as-of WebRTC state."""

import csv
import importlib.util
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("media_join", REPO / "tools" / "media_join.py")
media_join = importlib.util.module_from_spec(spec)
spec.loader.exec_module(media_join)

S = 1_000_000_000
MS = 1_000_000
SAMPLES = [
    "seq",
    "kind",
    "capture_ns",
    "recv_ns",
    "process_done_ns",
    "network_ns",
    "processing_ns",
    "e2e_ns",
]


def write(path, header, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def stats_row(ts, node, direction, **kw):
    row = {c: "" for c in media_join_columns()}
    row.update(ts_ns=ts, node=node, peer="relay", direction=direction, kind="video")
    row.update(kw)
    return [row[c] for c in media_join_columns()]


def media_join_columns():
    js = (REPO / "clients" / "webrtc_native" / "stats_log.js").read_text()
    body = js[js.index("const COLUMNS = [") : js.index("];", js.index("const COLUMNS = ["))]
    return body.split('"')[1::2]


def make_run(tmp_path):
    run = tmp_path / "run"
    # Frame 3000: captured at 10 s, at the relay 2 ms later, forwarded in
    # 50 us, at the receiver 1 ms after that, rendered 15 ms later.
    cap = 10 * S
    write(
        run / "relay" / "samples.csv",
        SAMPLES,
        [
            [3000, "frame", cap, cap + 2 * MS, cap + 2 * MS + 50_000, 1 * MS, 50_000, 2 * MS],
            [6000, "frame", cap + 33 * MS, 0, 0, 0, 0, 0],  # never reached the receiver
        ],
    )
    write(
        run / "receiver" / "samples.csv",
        SAMPLES,
        [
            [3000, "frame", cap, cap + 3 * MS + 50_000, cap + 18 * MS, 2 * MS, 15 * MS, 18 * MS],
        ],
    )
    cols = media_join_columns()
    write(
        run / "sender" / "webrtc.csv",
        cols,
        [
            stats_row(9 * S, "sender", "out", bytes=0, target_bps=1_000_000),
            stats_row(int(9.5 * S), "sender", "out", bytes=125_000, target_bps=2_000_000),
            stats_row(11 * S, "sender", "out", bytes=500_000, target_bps=3_000_000),
        ],
    )
    write(
        run / "receiver" / "webrtc.csv",
        cols,
        [
            stats_row(5 * S, "receiver", "in", bytes=0, packets_lost=0, jitter_ms=4),
            stats_row(6 * S, "receiver", "in", bytes=10, packets_lost=3, jitter_ms=5),
        ],
    )
    return run


def test_a_frame_splits_into_its_legs(tmp_path):
    run = make_run(tmp_path)
    frames = media_join.join_frames(
        media_join.load_frames(run / "relay" / "samples.csv"),
        media_join.load_frames(run / "receiver" / "samples.csv"),
    )
    assert len(frames) == 1, "only frames both sites saw"
    f = frames[0]
    assert (f["uplink"], f["relay"], f["downlink"], f["receiver"], f["e2e"]) == (
        1 * MS,
        50_000,
        1 * MS,
        15 * MS,
        18 * MS,
    )


def test_the_state_is_the_latest_row_before_capture_with_rates(tmp_path):
    run = make_run(tmp_path)
    frames = media_join.join_frames(
        media_join.load_frames(run / "relay" / "samples.csv"),
        media_join.load_frames(run / "receiver" / "samples.csv"),
    )
    series = media_join.load_stats(run)
    media_join.attach_state(frames, series, None)
    f = frames[0]
    # The 9.5 s row, not the 11 s one: as of capture.
    assert f["send_target_bps"] == "2000000"
    # 125 kB in 0.5 s.
    assert f["send_bitrate_bps"] == 2_000_000
    # The receiver's last row is 4 s old against a 1 s period: stale, blank.
    assert f["downlink_jitter_ms"] == ""
    media_join.attach_state(frames, series, 5 * S)
    assert f["downlink_jitter_ms"] == "5"
    assert f["downlink_lost_per_s"] == 3.0


def test_main_writes_the_table(tmp_path, capsys):
    run = make_run(tmp_path)
    out = tmp_path / "joined.csv"
    assert media_join.main([str(run), "-o", str(out)]) == 0
    assert "1 frames seen" in capsys.readouterr().out
    rows = list(csv.DictReader(out.open()))
    assert rows[0]["seq"] == "3000" and rows[0]["e2e_ns"] == str(18 * MS)
