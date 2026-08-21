# mec-cast-telemetry

The measurement spine. Every profile depends on this crate; it depends on
nothing else in the repo — not ROS, not Zenoh, not WebRTC, not srsRAN.
That one-way dependency rule is what keeps the two transport profiles
comparable and independent.

## What it owns

| Module | Responsibility |
|---|---|
| `envelope` | 64-byte `TimingEnvelope` wire contract + codec |
| `stats` | `DelayStats` — Welford mean/stddev, exact windowed percentiles |
| `clock` | `Clock` trait: `Phc`, `Realtime`, `Monotonic`, `Mock` |
| `ptp` | `PtpMonitor` — PHC offset and sync quality |
| `recorder` | Lock-free hot path → writer thread → CSV + HTTP snapshots |
| `sink` | CSV, HTTP (logging service), Parquet (feature) |
| `py` | PyO3 bindings (feature) |
| `ffi` | C ABI for non-Rust producers (the legacy WebRTC addon) |

## The hot-path contract

`try_record()` must never block, allocate, lock, or log. It pushes an
80-byte POD `Sample` into an SPSC ring (8192 slots ≈ 640 KiB) and returns.
On a full ring it increments an atomic drop counter and returns `false` —
**dropping a sample is always preferable to perturbing the measurement**.
Drop counts appear in every snapshot, so loss is visible rather than silent.

Two threads drain it: a writer (CSV + statistics) and an uploader (HTTP), so
a stalled logging service cannot stall CSV writing.

## Statistics

Percentiles are **exact over the last N samples**, computed by sorting a
copy at snapshot time on the writer thread — not a streaming estimator.
P² was rejected because its error is unbounded on multimodal distributions,
and 5G latency under HARQ is exactly that. See
[ADR-0004](../docs/architecture/adr/0004-exact-percentiles.md), which also
catalogues the defects in the C++ predecessor that this crate fixes.

## Features

| Feature | Default | Effect |
|---|---|---|
| `http` | ✅ | `ureq` sink posting snapshots to the logging service |
| `serde` | via `http` | JSON for envelopes and snapshots |
| `pyo3` | — | Python bindings |
| `extension-module` | — | Required only when building the wheel; keeps `cargo test` from linking libpython |
| `linux-ptp` | — | `PhcClock` + `PTP_SYS_OFFSET` ioctls |
| `parquet` | — | arrow-rs sink (deferred; CSV is sufficient below ~10⁶ rows) |

## Build and test

```bash
make test-rust                              # unit + proptest + integration
cargo test --workspace --all-features
cd telemetry/python && maturin develop --release   # wheel into the venv
make test-python
```

`tests/proptest_stats.rs` checks ring-derived percentiles against a naive
sort and Welford stddev against a two-pass computation.
`tests/recorder_loopback.rs` drives 50k samples through the pipeline against
a stub HTTP server and asserts `rows_written + dropped == pushed`.

## Recorder lifetime

One **active** `Recorder` per process. Building a second after `shutdown()` is
safe and is how admin-driven runs work: the node creates a Recorder when a run
starts and drains it when the run stops, rather than holding one for the life
of the process (ADR-0007).

`samples.csv` is opened for **append**, so a component restarting mid-run adds
to the run's record instead of erasing it. The cost is that one file may span
several incarnations and `seq` restarts at 0 each time — sort by `capture_ns`
across a restart.

## Consumers

Three bindings, one implementation of the statistics:

| Consumer | Binding | Crate type |
|---|---|---|
| `ran/collector` | native Rust | `rlib` |
| ROS2 nodes (Profile A) | PyO3 wheel | `cdylib` |
| Legacy WebRTC addon (Profile B) | C ABI, [`include/mec_cast_telemetry.h`](include/mec_cast_telemetry.h) | `staticlib` |

The C ABI is deliberately minimal — start, record, dropped-count, stop —
and every entry point tolerates NULL. `extern "C"` aborts on unwind, so
nothing in `ffi.rs` may panic; fallible calls return NULL or `false`.
A recorder is single-producer: call `mct_record` from one thread only.

`trace_id` is derived identically in the Python and C bindings (first 16
bytes of `run_id`, zero-padded), so one `RUN_ID` joins media and
point-cloud samples in the same query.

## Snapshot schema

Snapshots must match the logging service's `LogEntryCreate` exactly — it is
`extra="forbid"`, so any stray top-level field is a runtime 422. Everything
custom belongs under `context`. See
[docs/operations/logging-submodule.md](../docs/operations/logging-submodule.md).
