"""Smoke tests for the mec_cast_telemetry Python bindings.

Run after `maturin develop --features pyo3,http`:

    pytest telemetry/python/tests -v
"""

import time
import uuid
from pathlib import Path

import pytest

import mec_cast_telemetry as tel


def test_clocks():
    a = tel.now_ns()
    b = tel.now_ns()
    assert b >= a
    assert a > 1_500_000_000_000_000_000  # realtime: after 2017
    m1 = tel.monotonic_ns()
    m2 = tel.monotonic_ns()
    assert m2 >= m1


def test_envelope_round_trip():
    trace = uuid.uuid4().bytes
    wire = tel.encode_envelope(
        capture_ns=100,
        send_ns=200,
        recv_ns=-300,  # negative must survive (unsynced clocks)
        process_done_ns=400,
        seq=7,
        modality=tel.MODALITY_POINTCLOUD,
        trace_id=trace,
    )
    assert isinstance(wire, bytes)
    assert len(wire) == tel.ENVELOPE_WIRE_LEN
    decoded = tel.decode_envelope(wire)
    assert decoded["capture_ns"] == 100
    assert decoded["send_ns"] == 200
    assert decoded["recv_ns"] == -300
    assert decoded["process_done_ns"] == 400
    assert decoded["seq"] == 7
    assert decoded["modality"] == tel.MODALITY_POINTCLOUD
    assert decoded["trace_id"] == trace


def test_envelope_rejects_garbage():
    with pytest.raises(ValueError):
        tel.decode_envelope(b"too short")
    with pytest.raises(ValueError):
        tel.encode_envelope(0, 0, 0, 0, 0, 99, b"\x00" * 16)  # bad modality
    with pytest.raises(ValueError):
        tel.encode_envelope(0, 0, 0, 0, 0, 0, b"short")  # bad trace_id


def test_recorder_end_to_end(tmp_path: Path):
    out = tmp_path / "run-py"
    rec = tel.Recorder("py-smoke-run", "mec-cast-test-py", str(out))

    base = tel.now_ns()
    for seq in range(500):
        ok = rec.record(
            seq=seq,
            modality=tel.MODALITY_POINTCLOUD,
            capture_ns=base + seq * 1_000_000,
            send_ns=base + seq * 1_000_000 + 2_000_000,
            recv_ns=base + seq * 1_000_000 + 22_000_000,
            process_done_ns=base + seq * 1_000_000 + 25_000_000,
            payload_bytes=480_000,
            site=1,
        )
        assert ok

    # Give the writer thread a moment, then drain.
    time.sleep(0.1)
    report = rec.shutdown()

    assert report["samples_written"] + report["samples_dropped"] == 500
    assert report["samples_written"] > 0

    csv = (out / "samples.csv").read_text()
    lines = csv.strip().splitlines()
    assert lines[0].startswith("seq,modality,kind,site,capture_ns")
    assert len(lines) == 1 + report["samples_written"]
    # Derived network delay = 20 ms on every row.
    assert lines[1].split(",")[10] == "20000000"

    # Second shutdown must raise, not hang or crash.
    with pytest.raises(RuntimeError):
        rec.shutdown()
    with pytest.raises(RuntimeError):
        rec.record(seq=0, modality=0)


def test_recorder_counts_drops(tmp_path: Path):
    rec = tel.Recorder(
        "py-drop-run",
        "mec-cast-test-py",
        str(tmp_path / "run-drop"),
        queue_capacity=64,
    )
    pushed = 50_000
    for seq in range(pushed):
        rec.record(seq=seq, modality=tel.MODALITY_GENERIC)
    report = rec.shutdown()
    assert report["samples_written"] + report["samples_dropped"] == pushed
