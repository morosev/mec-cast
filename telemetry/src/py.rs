//! Python bindings (feature `pyo3`).
//!
//! Exposes the minimum the Python edge/publisher nodes need:
//!
//! - `now_ns()` — `CLOCK_REALTIME` nanoseconds (the shared time base),
//! - `monotonic_ns()` — local monotonic nanoseconds,
//! - `encode_envelope(...)` / `decode_envelope(bytes)` — the 64-byte wire
//!   contract, for direct-Zenoh attachments,
//! - `Recorder` — the async recording pipeline; `record()` is the
//!   never-blocking hot-path call, `shutdown()` drains and returns the final
//!   accounting as a dict.
//!
//! The stats engine itself stays in Rust — Python only feeds samples in, so
//! there is exactly one implementation of the statistics.

use std::time::Duration;

use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict};

use crate::clock::{Clock, MonotonicClock, RealtimeClock};
use crate::envelope::{Modality, TimingEnvelope, ENVELOPE_WIRE_LEN};
use crate::ptp::PtpMonitor;
use crate::recorder::{self, RecorderConfig, RecorderHandle, Sample, SampleKind, SampleSender};

/// CLOCK_REALTIME in nanoseconds. Comparable across PTP-synced machines.
#[pyfunction]
fn now_ns() -> i64 {
    RealtimeClock.now_ns()
}

/// CLOCK_MONOTONIC in nanoseconds. Local intervals only.
#[pyfunction]
fn monotonic_ns() -> i64 {
    MonotonicClock.now_ns()
}

fn modality_from_u8(modality: u8) -> PyResult<Modality> {
    Modality::try_from(modality).map_err(|e| PyValueError::new_err(e.to_string()))
}

fn trace_id_from_bytes(trace_id: &[u8]) -> PyResult<[u8; 16]> {
    trace_id.try_into().map_err(|_| {
        PyValueError::new_err(format!("trace_id must be 16 bytes, got {}", trace_id.len()))
    })
}

/// Serialize a timing envelope to its 64-byte wire format.
#[pyfunction]
#[pyo3(signature = (capture_ns, send_ns, recv_ns, process_done_ns, seq, modality, trace_id))]
#[allow(clippy::too_many_arguments)]
fn encode_envelope(
    py: Python<'_>,
    capture_ns: i64,
    send_ns: i64,
    recv_ns: i64,
    process_done_ns: i64,
    seq: u64,
    modality: u8,
    trace_id: &[u8],
) -> PyResult<Py<PyBytes>> {
    let envelope = TimingEnvelope {
        capture_ns,
        send_ns,
        recv_ns,
        process_done_ns,
        seq,
        modality: modality_from_u8(modality)?,
        trace_id: trace_id_from_bytes(trace_id)?,
    };
    Ok(PyBytes::new(py, &envelope.to_bytes()).into())
}

/// Parse a 64-byte wire envelope into a dict.
#[pyfunction]
fn decode_envelope(py: Python<'_>, data: &[u8]) -> PyResult<Py<PyDict>> {
    let e =
        TimingEnvelope::from_bytes(data).map_err(|err| PyValueError::new_err(err.to_string()))?;
    let d = PyDict::new(py);
    d.set_item("capture_ns", e.capture_ns)?;
    d.set_item("send_ns", e.send_ns)?;
    d.set_item("recv_ns", e.recv_ns)?;
    d.set_item("process_done_ns", e.process_done_ns)?;
    d.set_item("seq", e.seq)?;
    d.set_item("modality", e.modality as u8)?;
    d.set_item("trace_id", PyBytes::new(py, &e.trace_id))?;
    Ok(d.into())
}

/// The async recording pipeline, Python-side handle.
///
/// One *active* `Recorder` per process — building a second after `shutdown()`
/// is safe and is how admin-driven runs work (ADR-0007). `record()` never
/// blocks: on a full queue it
/// counts a drop and returns False.
///
/// The SPSC producer is single-owner (`Send`, not `Sync`); a mutex makes the
/// pyclass `Sync` as pyo3 requires. It is uncontended in normal use and its
/// cost is dwarfed by the Python call overhead itself.
#[pyclass]
struct Recorder {
    inner: std::sync::Mutex<RecorderInner>,
}

struct RecorderInner {
    sender: Option<SampleSender>,
    handle: Option<RecorderHandle>,
    trace_id: [u8; 16],
}

#[pymethods]
impl Recorder {
    #[new]
    #[pyo3(signature = (run_id, service, out_dir, logging_url=None, snapshot_interval_s=2.0, queue_capacity=8192, stats_window=8192))]
    fn new(
        run_id: &str,
        service: &str,
        out_dir: &str,
        logging_url: Option<String>,
        snapshot_interval_s: f64,
        queue_capacity: usize,
        stats_window: usize,
    ) -> PyResult<Self> {
        let mut cfg = RecorderConfig::new(run_id, service, out_dir);
        cfg.logging_url = logging_url;
        cfg.snapshot_interval = Duration::from_secs_f64(snapshot_interval_s.max(0.05));
        cfg.queue_capacity = queue_capacity;
        cfg.stats_window = stats_window;

        // trace_id = first 16 bytes of run_id, zero-padded. UUID-string runs
        // can pass exact bytes per sample via record()'s trace_id override.
        let mut trace_id = [0u8; 16];
        let src = run_id.as_bytes();
        let n = src.len().min(16);
        trace_id[..n].copy_from_slice(&src[..n]);

        let (sender, handle) = recorder::spawn(cfg, PtpMonitor::disabled())
            .map_err(|e| PyRuntimeError::new_err(format!("failed to start recorder: {e}")))?;
        Ok(Self {
            inner: std::sync::Mutex::new(RecorderInner {
                sender: Some(sender),
                handle: Some(handle),
                trace_id,
            }),
        })
    }

    /// Record one sample. Returns False when the queue was full (the drop is
    /// counted and reported in snapshots and the final report).
    #[pyo3(signature = (seq, modality, capture_ns=0, send_ns=0, recv_ns=0, process_done_ns=0, payload_bytes=0, site=0, kind=0, trace_id=None))]
    #[allow(clippy::too_many_arguments)]
    fn record(
        &self,
        seq: u64,
        modality: u8,
        capture_ns: i64,
        send_ns: i64,
        recv_ns: i64,
        process_done_ns: i64,
        payload_bytes: u32,
        site: u8,
        kind: u8,
        trace_id: Option<&[u8]>,
    ) -> PyResult<bool> {
        let mut inner = self.inner.lock().expect("recorder mutex poisoned");
        let default_trace_id = inner.trace_id;
        let sender = inner
            .sender
            .as_mut()
            .ok_or_else(|| PyRuntimeError::new_err("recorder already shut down"))?;
        let kind = match kind {
            0 => SampleKind::Frame,
            1 => SampleKind::Packet,
            2 => SampleKind::Event,
            other => {
                return Err(PyValueError::new_err(format!(
                    "unknown sample kind {other}"
                )))
            }
        };
        let trace_id = match trace_id {
            Some(bytes) => trace_id_from_bytes(bytes)?,
            None => default_trace_id,
        };
        let sample = Sample {
            envelope: TimingEnvelope {
                capture_ns,
                send_ns,
                recv_ns,
                process_done_ns,
                seq,
                modality: modality_from_u8(modality)?,
                trace_id,
            },
            kind,
            site,
            payload_bytes,
            aux_ns: 0,
        };
        Ok(sender.try_record(sample))
    }

    /// Samples dropped so far because the queue was full.
    fn dropped_total(&self) -> PyResult<u64> {
        let inner = self.inner.lock().expect("recorder mutex poisoned");
        inner
            .sender
            .as_ref()
            .map(|s| s.dropped_total())
            .ok_or_else(|| PyRuntimeError::new_err("recorder already shut down"))
    }

    /// Derived delays that came out negative -- unsynchronised clocks.
    ///
    /// Non-zero means the sender's clock is ahead of the receiver's, so every
    /// cross-host figure from this recorder is wrong by the skew. Reported to
    /// the admin, which raises WF_CLOCK_SKEW on it.
    fn negative_delays(&self) -> PyResult<u64> {
        let inner = self.inner.lock().expect("recorder mutex poisoned");
        inner
            .sender
            .as_ref()
            .map(|s| s.negative_delays())
            .ok_or_else(|| PyRuntimeError::new_err("recorder already shut down"))
    }

    /// Stop, drain, flush, and return the final accounting.
    fn shutdown(&self, py: Python<'_>) -> PyResult<Py<PyDict>> {
        let handle = {
            let mut inner = self.inner.lock().expect("recorder mutex poisoned");
            let handle = inner
                .handle
                .take()
                .ok_or_else(|| PyRuntimeError::new_err("recorder already shut down"))?;
            inner.sender.take(); // drop the producer first so the drain is total
            handle
        };
        let report = py.allow_threads(|| handle.shutdown());
        let d = PyDict::new(py);
        d.set_item("samples_written", report.samples_written)?;
        d.set_item("samples_dropped", report.samples_dropped)?;
        d.set_item("snapshots_built", report.snapshots_built)?;
        d.set_item("snapshots_posted", report.snapshots_posted)?;
        d.set_item("snapshots_dropped", report.snapshots_dropped)?;
        d.set_item("post_failures", report.post_failures)?;
        Ok(d.into())
    }
}

#[pymodule]
fn mec_cast_telemetry(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(now_ns, m)?)?;
    m.add_function(wrap_pyfunction!(monotonic_ns, m)?)?;
    m.add_function(wrap_pyfunction!(encode_envelope, m)?)?;
    m.add_function(wrap_pyfunction!(decode_envelope, m)?)?;
    m.add_class::<Recorder>()?;
    m.add("ENVELOPE_WIRE_LEN", ENVELOPE_WIRE_LEN)?;
    m.add("MODALITY_POINTCLOUD", Modality::PointCloud as u8)?;
    m.add("MODALITY_VIDEO", Modality::Video as u8)?;
    m.add("MODALITY_AUDIO", Modality::Audio as u8)?;
    m.add("MODALITY_GENERIC", Modality::Generic as u8)?;
    Ok(())
}
