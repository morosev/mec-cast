//! srsRAN gNB metrics tap (Phase RAN-1: observe, don't control).
//!
//! The srsRAN Project gNB emits metrics as JSON datagrams over UDP
//! (`metrics: {addr, port}` in gnb.yml): per-UE MAC/scheduler KPIs (MCS,
//! PRB utilization, HARQ retx, BSR, CQI, SNR, throughput) plus cell
//! counters. This collector:
//!
//! 1. stamps every datagram's arrival on the shared telemetry clock and
//!    records it (kind=Event, site=SITE_RAN) to the per-run CSV — the
//!    arrival cadence itself is a health signal;
//! 2. wraps each parsed KPI object into a logging-service entry
//!    (`service: "mec-cast-ran"`, `trace_id: run_id`, KPIs under
//!    `context.kpi`) and POSTs them in 1 s batches.
//!
//! Parsing is deliberately lenient — the srsRAN metrics schema varies by
//! version, so anything that is a JSON object is forwarded verbatim under
//! `context.kpi`; only non-JSON datagrams are counted as malformed and
//! dropped.

use std::net::UdpSocket;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

use mec_cast_telemetry::{
    Clock, Modality, PtpMonitor, RealtimeClock, RecorderConfig, Sample, SampleKind, TimingEnvelope,
};
use serde_json::{json, Value};

/// `site` tag for RAN samples in the shared CSV schema.
pub const SITE_RAN: u8 = 2;

pub struct CollectorConfig {
    pub run_id: String,
    pub logging_url: Option<String>,
    pub out_dir: std::path::PathBuf,
    /// Flush the KPI batch at this interval (or at `max_batch`).
    pub flush_interval: Duration,
    pub max_batch: usize,
}

impl CollectorConfig {
    pub fn new(run_id: impl Into<String>, out_dir: impl Into<std::path::PathBuf>) -> Self {
        Self {
            run_id: run_id.into(),
            logging_url: None,
            out_dir: out_dir.into(),
            flush_interval: Duration::from_secs(1),
            max_batch: 100,
        }
    }
}

/// Final accounting for one collector run.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct RunReport {
    pub datagrams: u64,
    pub malformed: u64,
    pub batches_posted: u64,
    pub post_failures: u64,
    pub samples_written: u64,
}

/// Build one logging-service entry from a raw metrics datagram.
/// Returns `None` when the payload is not a JSON object.
pub fn kpi_entry(raw: &[u8], run_id: &str, recv_ns: i64) -> Option<Value> {
    let parsed: Value = serde_json::from_slice(raw).ok()?;
    if !parsed.is_object() {
        return None;
    }
    Some(json!({
        "level": "INFO",
        "service": "mec-cast-ran",
        "logger": "ran.collector",
        "message": "gnb metrics",
        "trace_id": run_id,
        "context": {
            "run_id": run_id,
            "recv_ns": recv_ns,
            "kpi": parsed,
        }
    }))
}

/// Bounded KPI batcher with time/size-based flushing.
struct Batcher {
    url: Option<String>,
    agent: ureq::Agent,
    buf: Vec<Value>,
    last_flush: Instant,
    flush_interval: Duration,
    max_batch: usize,
    batches_posted: u64,
    post_failures: u64,
}

impl Batcher {
    fn new(cfg: &CollectorConfig) -> Self {
        Self {
            url: cfg
                .logging_url
                .as_ref()
                .map(|base| format!("{}/api/v1/logs", base.trim_end_matches('/'))),
            agent: ureq::AgentBuilder::new()
                .timeout_connect(Duration::from_millis(500))
                .timeout(Duration::from_secs(2))
                .build(),
            buf: Vec::new(),
            last_flush: Instant::now(),
            flush_interval: cfg.flush_interval,
            max_batch: cfg.max_batch.max(1),
            batches_posted: 0,
            post_failures: 0,
        }
    }

    fn push(&mut self, entry: Value) {
        self.buf.push(entry);
        if self.buf.len() >= self.max_batch {
            self.flush();
        }
    }

    fn maybe_flush(&mut self) {
        if !self.buf.is_empty() && self.last_flush.elapsed() >= self.flush_interval {
            self.flush();
        }
    }

    fn flush(&mut self) {
        self.last_flush = Instant::now();
        if self.buf.is_empty() {
            return;
        }
        let batch = Value::Array(std::mem::take(&mut self.buf));
        let Some(url) = &self.url else { return };
        match self
            .agent
            .post(url)
            .set("Content-Type", "application/json")
            .send_string(&batch.to_string())
        {
            Ok(_) => self.batches_posted += 1,
            Err(_) => self.post_failures += 1,
        }
    }
}

/// Receive loop. Returns when `stop` is set (checked between reads; the
/// socket must have a read timeout so the loop can observe it).
pub fn run(
    socket: UdpSocket,
    cfg: CollectorConfig,
    stop: &AtomicBool,
) -> std::io::Result<RunReport> {
    socket.set_read_timeout(Some(Duration::from_millis(200)))?;

    let mut rec_cfg = RecorderConfig::new(cfg.run_id.clone(), "mec-cast-ran", cfg.out_dir.clone());
    rec_cfg.logging_url = None; // KPI entries go through the batcher instead
    let (mut sender, handle) = mec_cast_telemetry::spawn_recorder(rec_cfg, PtpMonitor::disabled())?;

    let mut batcher = Batcher::new(&cfg);
    let clock = RealtimeClock;
    let mut trace_id = [0u8; 16];
    let src = cfg.run_id.as_bytes();
    let n = src.len().min(16);
    trace_id[..n].copy_from_slice(&src[..n]);

    let mut report = RunReport::default();
    let mut buf = vec![0u8; 65536];
    while !stop.load(Ordering::SeqCst) {
        match socket.recv_from(&mut buf) {
            Ok((len, _peer)) => {
                let recv_ns = clock.now_ns();
                report.datagrams += 1;
                let mut envelope =
                    TimingEnvelope::new(Modality::Generic, report.datagrams, trace_id);
                envelope.recv_ns = recv_ns;
                sender.try_record(Sample {
                    envelope,
                    kind: SampleKind::Event,
                    site: SITE_RAN,
                    payload_bytes: len as u32,
                    aux_ns: 0,
                });
                match kpi_entry(&buf[..len], &cfg.run_id, recv_ns) {
                    Some(entry) => batcher.push(entry),
                    None => report.malformed += 1,
                }
            }
            Err(e)
                if e.kind() == std::io::ErrorKind::WouldBlock
                    || e.kind() == std::io::ErrorKind::TimedOut => {}
            Err(e) => return Err(e),
        }
        batcher.maybe_flush();
    }
    batcher.flush();
    drop(sender);
    let rec_report = handle.shutdown();
    report.batches_posted = batcher.batches_posted;
    report.post_failures = batcher.post_failures;
    report.samples_written = rec_report.samples_written;
    Ok(report)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn kpi_entry_wraps_valid_json() {
        let raw = br#"{"ue_list":[{"pci":1,"rnti":17921,"dl_mcs":27,"cqi":15}]}"#;
        let entry = kpi_entry(raw, "run-1", 123).unwrap();
        assert_eq!(entry["service"], "mec-cast-ran");
        assert_eq!(entry["trace_id"], "run-1");
        assert_eq!(entry["context"]["recv_ns"], 123);
        assert_eq!(entry["context"]["kpi"]["ue_list"][0]["dl_mcs"], 27);
    }

    #[test]
    fn kpi_entry_rejects_non_json_and_non_objects() {
        assert!(kpi_entry(b"not json at all", "r", 0).is_none());
        assert!(kpi_entry(b"[1,2,3]", "r", 0).is_none());
        assert!(kpi_entry(b"42", "r", 0).is_none());
    }

    #[test]
    fn kpi_entry_is_lenient_about_unknown_schemas() {
        // Schema drift across srsRAN versions must not break ingestion.
        let raw = br#"{"totally":{"new":{"schema":true}},"v":"99.9"}"#;
        let entry = kpi_entry(raw, "r", 1).unwrap();
        assert_eq!(entry["context"]["kpi"]["totally"]["new"]["schema"], true);
    }
}
