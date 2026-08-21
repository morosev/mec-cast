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

/// Control-plane client. Behind a feature so `--no-default-features` stays
/// free of the websocket dependency, which CI builds to prove.
#[cfg(feature = "admin")]
pub mod admin;

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

/// Everything one run owns: the recorder, the KPI batcher, and the trace id.
///
/// Extracted from `run` so a collector driven by the admin can start and stop
/// recording without restarting the process. `run` itself keeps one session
/// for its whole life, which is exactly what it did before.
pub struct RunSession {
    run_id: String,
    sender: mec_cast_telemetry::SampleSender,
    handle: mec_cast_telemetry::RecorderHandle,
    batcher: Batcher,
    trace_id: [u8; 16],
    report: RunReport,
}

impl RunSession {
    /// Open a session for `run_id`, writing under `cfg.out_dir`.
    pub fn start(run_id: &str, cfg: &CollectorConfig) -> std::io::Result<Self> {
        let mut rec_cfg =
            RecorderConfig::new(run_id.to_string(), "mec-cast-ran", cfg.out_dir.clone());
        rec_cfg.logging_url = None; // KPI entries go through the batcher instead
        let (sender, handle) = mec_cast_telemetry::spawn_recorder(rec_cfg, PtpMonitor::disabled())?;

        let mut trace_id = [0u8; 16];
        let src = run_id.as_bytes();
        let n = src.len().min(16);
        trace_id[..n].copy_from_slice(&src[..n]);

        Ok(Self {
            run_id: run_id.to_string(),
            sender,
            handle,
            batcher: Batcher::new(cfg),
            trace_id,
            report: RunReport::default(),
        })
    }

    pub fn run_id(&self) -> &str {
        &self.run_id
    }

    pub fn report(&self) -> RunReport {
        let mut report = self.report;
        report.batches_posted = self.batcher.batches_posted;
        report.post_failures = self.batcher.post_failures;
        report
    }

    /// Record one datagram: timing sample plus a KPI entry for the batch.
    pub fn record(&mut self, payload: &[u8], recv_ns: i64) {
        self.report.datagrams += 1;
        let mut envelope =
            TimingEnvelope::new(Modality::Generic, self.report.datagrams, self.trace_id);
        envelope.recv_ns = recv_ns;
        self.sender.try_record(Sample {
            envelope,
            kind: SampleKind::Event,
            site: SITE_RAN,
            payload_bytes: payload.len() as u32,
            aux_ns: 0,
        });
        match kpi_entry(payload, &self.run_id, recv_ns) {
            Some(entry) => self.batcher.push(entry),
            None => self.report.malformed += 1,
        }
    }

    pub fn maybe_flush(&mut self) {
        self.batcher.maybe_flush();
    }

    /// Flush, drain the recorder, and return the final accounting.
    pub fn stop(mut self) -> RunReport {
        self.batcher.flush();
        let mut report = self.report();
        drop(self.sender);
        report.samples_written = self.handle.shutdown().samples_written;
        report
    }
}

/// Receive loop. Returns when `stop` is set (checked between reads; the
/// socket must have a read timeout so the loop can observe it).
///
/// This is the standalone path: the run id comes from the environment and
/// recording begins immediately. Unchanged by the admin work.
pub fn run(
    socket: UdpSocket,
    cfg: CollectorConfig,
    stop: &AtomicBool,
) -> std::io::Result<RunReport> {
    socket.set_read_timeout(Some(Duration::from_millis(200)))?;
    let mut session = RunSession::start(&cfg.run_id.clone(), &cfg)?;
    let clock = RealtimeClock;
    let mut buf = vec![0u8; 65536];

    while !stop.load(Ordering::SeqCst) {
        match socket.recv_from(&mut buf) {
            Ok((len, _peer)) => session.record(&buf[..len], clock.now_ns()),
            Err(e)
                if e.kind() == std::io::ErrorKind::WouldBlock
                    || e.kind() == std::io::ErrorKind::TimedOut => {}
            Err(e) => return Err(e),
        }
        session.maybe_flush();
    }
    Ok(session.stop())
}

/// Receive loop driven by the admin service.
///
/// Recording starts and stops on command rather than at process start, so the
/// collector can sit idle between runs. Datagrams arriving while idle are
/// counted but not recorded — and that count is exactly what lets the admin
/// tell "srsRAN is sending nothing" from "we are simply not recording".
#[cfg(feature = "admin")]
pub fn run_with_admin(
    socket: UdpSocket,
    cfg: CollectorConfig,
    stop: std::sync::Arc<AtomicBool>,
    admin_cfg: admin::AdminConfig,
) -> std::io::Result<RunReport> {
    use std::sync::mpsc::TryRecvError;

    socket.set_read_timeout(Some(Duration::from_millis(200)))?;
    let bind = socket
        .local_addr()
        .map(|a| a.to_string())
        .unwrap_or_default();
    let (handle, commands) = admin::spawn(admin_cfg, std::sync::Arc::clone(&stop));

    let clock = RealtimeClock;
    let mut buf = vec![0u8; 65536];
    let mut session: Option<RunSession> = None;
    let mut total = RunReport::default();
    let mut idle_datagrams: u64 = 0;
    let mut last_status = Instant::now();

    while !stop.load(Ordering::SeqCst) {
        match commands.try_recv() {
            Ok(admin::Command::Start { run_id }) => {
                if session.as_ref().map(RunSession::run_id) != Some(run_id.as_str()) {
                    if let Some(previous) = session.take() {
                        total = accumulate(total, previous.stop());
                    }
                    match RunSession::start(&run_id, &cfg) {
                        Ok(new) => {
                            eprintln!("[ran-collector] recording run {run_id}");
                            session = Some(new);
                            handle.set_identity("running", Some(&run_id));
                        }
                        Err(e) => eprintln!("[ran-collector] cannot start run {run_id}: {e}"),
                    }
                }
                send_status(
                    &handle,
                    &session,
                    &bind,
                    idle_datagrams,
                    serde_json::json!({}),
                );
            }
            Ok(admin::Command::Stop) => {
                let report = match session.take() {
                    Some(active) => {
                        let report = active.stop();
                        total = accumulate(total, report);
                        report
                    }
                    None => RunReport::default(),
                };
                handle.set_identity("idle", None);
                send_status(
                    &handle,
                    &session,
                    &bind,
                    idle_datagrams,
                    report_json(&report),
                );
            }
            Err(TryRecvError::Empty) => {}
            Err(TryRecvError::Disconnected) => break,
        }

        match socket.recv_from(&mut buf) {
            Ok((len, _peer)) => match session.as_mut() {
                Some(active) => active.record(&buf[..len], clock.now_ns()),
                None => idle_datagrams += 1,
            },
            Err(e)
                if e.kind() == std::io::ErrorKind::WouldBlock
                    || e.kind() == std::io::ErrorKind::TimedOut => {}
            Err(e) => return Err(e),
        }
        if let Some(active) = session.as_mut() {
            active.maybe_flush();
        }

        if last_status.elapsed() >= Duration::from_secs(2) {
            last_status = Instant::now();
            send_status(
                &handle,
                &session,
                &bind,
                idle_datagrams,
                serde_json::json!({}),
            );
        }
    }

    if let Some(active) = session.take() {
        total = accumulate(total, active.stop());
    }
    handle.goodbye(None, report_json(&total));
    let admin_report = handle.shutdown();
    eprintln!("[ran-collector] admin: {admin_report:?}");
    Ok(total)
}

#[cfg(feature = "admin")]
fn accumulate(mut total: RunReport, one: RunReport) -> RunReport {
    total.datagrams += one.datagrams;
    total.malformed += one.malformed;
    total.batches_posted += one.batches_posted;
    total.post_failures += one.post_failures;
    total.samples_written += one.samples_written;
    total
}

#[cfg(feature = "admin")]
fn report_json(report: &RunReport) -> Value {
    json!({
        "samples_written": report.samples_written,
        "datagrams": report.datagrams,
        "malformed": report.malformed,
        "batches_posted": report.batches_posted,
        "post_failures": report.post_failures,
    })
}

/// Peers are the UEs in the last metrics datagram; until one has been parsed
/// there are none to report.
#[cfg(feature = "admin")]
fn send_status(
    handle: &admin::AdminHandle,
    session: &Option<RunSession>,
    bind: &str,
    idle_datagrams: u64,
    report: Value,
) {
    let (state, run_id, counters) = match session {
        Some(active) => {
            let r = active.report();
            (
                "running",
                Some(active.run_id()),
                json!({
                    "datagrams": r.datagrams,
                    "malformed": r.malformed,
                    "batches_posted": r.batches_posted,
                    "post_failures": r.post_failures,
                }),
            )
        }
        None => (
            "idle",
            None,
            json!({"datagrams": idle_datagrams, "malformed": 0}),
        ),
    };
    handle.status(admin::status_payload(
        state,
        run_id,
        bind,
        Vec::new(),
        counters,
        report,
    ));
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
