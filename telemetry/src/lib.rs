//! # mec-cast-telemetry
//!
//! The measurement spine of the mec-cast 5G industrial-communication
//! platform. Every transport profile (ROS2/Zenoh point clouds, WebRTC/str0m
//! media) and the RAN metrics collector depend on this crate; this crate
//! depends on none of them.
//!
//! It provides:
//!
//! - [`TimingEnvelope`] — the fixed 64-byte wire contract stamped at each
//!   pipeline stage (capture → send → recv → process-done),
//! - [`DelayStats`] — running delay statistics with exact windowed
//!   percentiles and emitted jitter (stddev),
//! - [`Clock`] implementations — `CLOCK_REALTIME`, `CLOCK_MONOTONIC`, a
//!   deterministic [`MockClock`], and (feature `linux-ptp`) a PTP Hardware
//!   Clock reader,
//! - [`PtpMonitor`] — per-snapshot clock-sync quality so analysis can filter
//!   by clock health,
//! - (from phase 2) an async, never-blocking recording pipeline that writes
//!   per-sample CSV and ships aggregated snapshots to the logging service.

mod envelope;
mod stats;

pub mod clock;
pub mod ffi;
pub mod ptp;
#[cfg(feature = "pyo3")]
mod py;
pub mod recorder;

#[cfg(all(target_os = "linux", feature = "linux-ptp"))]
pub use clock::PhcClock;
pub use clock::{Clock, ClockId, MockClock};
#[cfg(unix)]
pub use clock::{MonotonicClock, RealtimeClock};
pub use envelope::{EnvelopeError, Modality, TimingEnvelope, ENVELOPE_VERSION, ENVELOPE_WIRE_LEN};
pub use ptp::{PtpMonitor, PtpQuality, DEFAULT_THRESHOLD_NS};
pub use recorder::{
    spawn as spawn_recorder, RecorderConfig, RecorderHandle, RecorderReport, Sample, SampleKind,
    SampleSender,
};
pub use stats::{DelayStats, StatsSnapshot, DEFAULT_WINDOW};
