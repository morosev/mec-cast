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
//!
//! The reports arrive over UDP or srsRAN's `remote_control` WebSocket,
//! whichever the gNB's release speaks — see [`source`].

/// Control-plane client. Behind a feature so `--no-default-features` stays
/// free of the websocket dependency, which CI builds to prove.
#[cfg(feature = "admin")]
pub mod admin;
pub mod source;

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

use mec_cast_telemetry::{
    Clock, Modality, PtpMonitor, RealtimeClock, RecorderConfig, Sample, SampleKind, TimingEnvelope,
};
use serde_json::{json, Value};

pub use source::{MetricsSource, SourceKind, Transport};

/// `site` tag for RAN samples in the shared CSV schema.
pub const SITE_RAN: u8 = 2;

pub struct CollectorConfig {
    pub run_id: String,
    pub logging_url: Option<String>,
    /// The runs base directory. Each run writes `<runs_dir>/<run_id>/ran/`,
    /// derived per session — a collector driven by the admin records many
    /// runs in one process, so the directory cannot be fixed at startup.
    pub runs_dir: PathBuf,
    /// PHC device for `ptp.reliable` (`PTP_DEVICE`). `None` disables it.
    pub ptp_device: Option<String>,
    /// Flush the KPI batch at this interval (or at `max_batch`).
    pub flush_interval: Duration,
    pub max_batch: usize,
}

impl CollectorConfig {
    pub fn new(run_id: impl Into<String>, runs_dir: impl Into<PathBuf>) -> Self {
        Self {
            run_id: run_id.into(),
            logging_url: None,
            runs_dir: runs_dir.into(),
            ptp_device: None,
            flush_interval: Duration::from_secs(1),
            max_batch: 100,
        }
    }
}

/// Where one run's RAN output lands: `<runs_dir>/<run_id>/ran/`. The leaf is
/// the one the admin's manifest already names for a gNB (`orchestrator.py`).
pub fn run_dir(runs_dir: &Path, run_id: &str) -> PathBuf {
    runs_dir.join(run_id).join("ran")
}

/// 16-byte trace id for a run: the UUID's bytes when `run_id` is a UUID —
/// exactly what the ROS nodes put on the wire (`run_trace_id` in
/// `publisher_node.py`) — otherwise its first 16 bytes, zero-padded.
pub fn trace_id(run_id: &str) -> [u8; 16] {
    let hex: Vec<u8> = run_id.bytes().filter(|b| *b != b'-').collect();
    let dashed_ok = run_id.len() == 36
        && [8, 13, 18, 23]
            .iter()
            .all(|&i| run_id.as_bytes()[i] == b'-');
    if (dashed_ok || run_id.len() == 32) && hex.len() == 32 {
        let mut out = [0u8; 16];
        let parsed = (0..16).all(|i| {
            std::str::from_utf8(&hex[2 * i..2 * i + 2])
                .ok()
                .and_then(|h| u8::from_str_radix(h, 16).ok())
                .map(|b| out[i] = b)
                .is_some()
        });
        if parsed {
            return out;
        }
    }
    let mut out = [0u8; 16];
    let src = run_id.as_bytes();
    let n = src.len().min(16);
    out[..n].copy_from_slice(&src[..n]);
    out
}

/// The gNB's own timestamp for a report, in ns since the epoch.
///
/// srsRAN has written it two ways: seconds as a number (`1754500000.123`,
/// older releases) and an ISO-8601 string with no zone
/// (`2025-11-04T15:51:26.845`, current docs). A zoneless string is read as
/// UTC; a gNB host on local time shows up as an offset of whole hours in
/// `network_ns`, which is loud rather than silent. Looked for at the top
/// level, then on the first entry of `cells`.
pub fn report_timestamp_ns(report: &Value) -> Option<i64> {
    let ts = report.get("timestamp").or_else(|| {
        report
            .get("cells")
            .and_then(Value::as_array)
            .and_then(|cells| cells.first())
            .and_then(|cell| cell.get("timestamp"))
    })?;
    match ts {
        // Rounded to the microsecond: an f64 holds epoch seconds to ~1e-7,
        // so nanoseconds would be noise (…123 s reads back as …122999808 ns).
        // srsRAN writes milliseconds.
        Value::Number(n) => n
            .as_f64()
            .filter(|s| s.is_finite() && *s > 0.0)
            .map(|s| (s * 1e6).round() as i64 * 1_000),
        Value::String(text) => parse_iso8601_ns(text),
        _ => None,
    }
}

/// `YYYY-MM-DDTHH:MM:SS[.frac][Z|±HH:MM]`, no dependencies.
fn parse_iso8601_ns(text: &str) -> Option<i64> {
    let text = text.trim();
    let (date, time) = text.split_once(['T', ' '])?;
    let mut d = date.split('-');
    let (y, mo, da): (i64, i64, i64) = (
        d.next()?.parse().ok()?,
        d.next()?.parse().ok()?,
        d.next()?.parse().ok()?,
    );
    // Zone suffix, if any.
    let (clock, offset_s) = if let Some(stripped) = time.strip_suffix('Z') {
        (stripped, 0)
    } else if let Some(pos) = time.rfind(['+', '-']).filter(|&p| p >= 8) {
        let (clock, zone) = time.split_at(pos);
        let sign = if zone.starts_with('-') { -1 } else { 1 };
        let mut z = zone[1..].split(':');
        let zh: i64 = z.next()?.parse().ok()?;
        let zm: i64 = z.next().unwrap_or("0").parse().ok()?;
        (clock, sign * (zh * 3600 + zm * 60))
    } else {
        (time, 0)
    };
    let mut c = clock.split(':');
    let h: i64 = c.next()?.parse().ok()?;
    let mi: i64 = c.next()?.parse().ok()?;
    let sec_text = c.next()?;
    let (whole, frac) = sec_text.split_once('.').unwrap_or((sec_text, ""));
    let sec: i64 = whole.parse().ok()?;
    let frac_ns: i64 = if frac.is_empty() {
        0
    } else {
        let digits: String = frac.chars().take(9).collect();
        let scale = 10i64.pow(9 - digits.len() as u32);
        digits.parse::<i64>().ok()? * scale
    };
    if !(1..=12).contains(&mo) || !(1..=31).contains(&da) || h > 23 || mi > 59 || sec > 60 {
        return None;
    }
    // Howard Hinnant's days_from_civil.
    let y_adj = if mo <= 2 { y - 1 } else { y };
    let era = y_adj.div_euclid(400);
    let yoe = y_adj - era * 400;
    let mp = (mo + 9) % 12;
    let doy = (153 * mp + 2) / 5 + da - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    let days = era * 146_097 + doe - 719_468;
    let secs = days * 86_400 + h * 3600 + mi * 60 + sec - offset_s;
    Some(secs * 1_000_000_000 + frac_ns)
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
    out_dir: PathBuf,
    sender: mec_cast_telemetry::SampleSender,
    handle: mec_cast_telemetry::RecorderHandle,
    batcher: Batcher,
    trace_id: [u8; 16],
    /// Clock health for the KPI entries. The recorder gets a disabled one:
    /// its snapshots are never uploaded here (`logging_url = None`), so the
    /// entries are where `ptp` has to be.
    ptp: PtpMonitor,
    ptp_enabled: bool,
    ptp_error: String,
    report: RunReport,
}

impl RunSession {
    /// Open a session for `run_id`, writing under `<cfg.runs_dir>/<run_id>/ran/`.
    pub fn start(run_id: &str, cfg: &CollectorConfig) -> std::io::Result<Self> {
        let out_dir = run_dir(&cfg.runs_dir, run_id);
        let mut rec_cfg = RecorderConfig::new(run_id.to_string(), "mec-cast-ran", out_dir.clone());
        rec_cfg.logging_url = None; // KPI entries go through the batcher instead
        let (sender, handle) = mec_cast_telemetry::spawn_recorder(rec_cfg, PtpMonitor::disabled())?;

        let (ptp, ptp_enabled, ptp_error) =
            mec_cast_telemetry::monitor_from_device(cfg.ptp_device.as_deref());
        if cfg.ptp_device.as_deref().is_some_and(|d| !d.is_empty()) && !ptp_enabled {
            eprintln!(
                "[ran-collector] PTP_DEVICE set but the monitor is off ({ptp_error}); \
                 every entry will carry ptp.reliable=false"
            );
        }

        Ok(Self {
            run_id: run_id.to_string(),
            out_dir,
            sender,
            handle,
            batcher: Batcher::new(cfg),
            trace_id: trace_id(run_id),
            ptp,
            ptp_enabled,
            ptp_error,
            report: RunReport::default(),
        })
    }

    pub fn out_dir(&self) -> &Path {
        &self.out_dir
    }

    /// `(enabled, error)` for the admin status.
    pub fn ptp_status(&self) -> (bool, &str) {
        (self.ptp_enabled, &self.ptp_error)
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

    /// Record one report: timing sample plus a KPI entry for the batch.
    ///
    /// The gNB's own timestamp, when the report has one, goes in `send_ns`,
    /// so the CSV's `network_ns` is the metrics pipeline's lag — same host
    /// in the lab, so valid without PTP. Arrival stays in `recv_ns`.
    pub fn record(&mut self, payload: &[u8], recv_ns: i64) {
        self.report.datagrams += 1;
        let entry = kpi_entry(payload, &self.run_id, recv_ns);
        let gnb_ts_ns = entry
            .as_ref()
            .and_then(|e| report_timestamp_ns(&e["context"]["kpi"]));

        let mut envelope =
            TimingEnvelope::new(Modality::Generic, self.report.datagrams, self.trace_id);
        envelope.send_ns = gnb_ts_ns.unwrap_or(0);
        envelope.recv_ns = recv_ns;
        self.sender.try_record(Sample {
            envelope,
            kind: SampleKind::Event,
            site: SITE_RAN,
            payload_bytes: payload.len() as u32,
            aux_ns: 0,
        });
        match entry {
            Some(mut entry) => {
                let ptp = self.ptp.poll();
                entry["context"]["gnb_ts_ns"] = json!(gnb_ts_ns);
                entry["context"]["ptp"] = json!({
                    "offset_ns": ptp.offset_ns,
                    "reliable": ptp.reliable,
                });
                self.batcher.push(entry);
            }
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
    mut source: Box<dyn MetricsSource>,
    cfg: CollectorConfig,
    stop: &AtomicBool,
) -> std::io::Result<RunReport> {
    let mut session = RunSession::start(&cfg.run_id.clone(), &cfg)?;
    let clock = RealtimeClock;

    while !stop.load(Ordering::SeqCst) {
        if let Some(report) = source.next()? {
            session.record(&report, clock.now_ns());
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
    mut source: Box<dyn MetricsSource>,
    cfg: CollectorConfig,
    stop: std::sync::Arc<AtomicBool>,
    admin_cfg: admin::AdminConfig,
) -> std::io::Result<RunReport> {
    use std::sync::mpsc::TryRecvError;

    let (handle, commands) = admin::spawn(admin_cfg, std::sync::Arc::clone(&stop));

    let clock = RealtimeClock;
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
                    source.as_ref(),
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
                    source.as_ref(),
                    idle_datagrams,
                    report_json(&report),
                );
            }
            Err(TryRecvError::Empty) => {}
            Err(TryRecvError::Disconnected) => break,
        }

        if let Some(report) = source.next()? {
            match session.as_mut() {
                Some(active) => active.record(&report, clock.now_ns()),
                None => idle_datagrams += 1,
            }
        }
        if let Some(active) = session.as_mut() {
            active.maybe_flush();
        }

        if last_status.elapsed() >= Duration::from_secs(2) {
            last_status = Instant::now();
            send_status(
                &handle,
                &session,
                source.as_ref(),
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
    source: &dyn MetricsSource,
    idle_datagrams: u64,
    report: Value,
) {
    // `bind` predates the WebSocket source and stays: it is what an operator
    // reads to know where to point srsRAN, so it names whichever endpoint the
    // collector is actually using (or both, while auto is still listening).
    let mut params = source.describe();
    let bind = match (
        params["udp"].as_str(),
        params["ws"].as_str(),
        source.transport(),
    ) {
        (_, Some(ws), Some(Transport::Ws)) => ws.to_string(),
        (Some(udp), _, Some(Transport::Udp)) => udp.to_string(),
        (Some(udp), Some(ws), None) => format!("{udp} | {ws}"),
        (Some(udp), None, _) => udp.to_string(),
        (None, Some(ws), _) => ws.to_string(),
        (None, None, _) => String::new(),
    };
    params["bind"] = json!(bind);
    if let Some(active) = session {
        let (enabled, error) = active.ptp_status();
        params["ptp_enabled"] = json!(enabled);
        params["ptp_error"] = json!(error);
        params["out_dir"] = json!(active.out_dir().display().to_string());
    }
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
        params,
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
    fn a_uuid_run_id_becomes_its_bytes_as_the_ros_nodes_do() {
        let id = trace_id("01a12130-4ca3-7000-9336-2633eada131d");
        assert_eq!(id[0], 0x01);
        assert_eq!(id[1], 0xa1);
        assert_eq!(id[15], 0x1d);
        // Uppercase from macOS uuidgen is the same UUID.
        assert_eq!(trace_id("01A12130-4CA3-7000-9336-2633EADA131D"), id);
    }

    #[test]
    fn a_non_uuid_run_id_keeps_the_prefix_fallback() {
        let id = trace_id("dev-run");
        assert_eq!(&id[..7], b"dev-run");
        assert!(id[7..].iter().all(|b| *b == 0));
    }

    #[test]
    fn each_run_writes_its_own_directory() {
        let base = Path::new("runs");
        assert_eq!(run_dir(base, "a"), Path::new("runs/a/ran"));
        assert_ne!(run_dir(base, "a"), run_dir(base, "b"));
    }

    #[test]
    fn both_srsran_timestamp_shapes_parse() {
        let old = serde_json::json!({"timestamp": 1754500000.123});
        assert_eq!(report_timestamp_ns(&old), Some(1_754_500_000_123_000_000));
        let new = serde_json::json!({"timestamp": "2025-11-04T15:51:26.845"});
        assert_eq!(report_timestamp_ns(&new), Some(1_762_271_486_845_000_000));
        let nested = serde_json::json!({"cells": [{"timestamp": "2025-11-04T15:51:26.845Z"}]});
        assert_eq!(
            report_timestamp_ns(&nested),
            Some(1_762_271_486_845_000_000)
        );
        let zoned = serde_json::json!({"timestamp": "2025-11-04T17:51:26.845+02:00"});
        assert_eq!(report_timestamp_ns(&zoned), Some(1_762_271_486_845_000_000));
        assert_eq!(report_timestamp_ns(&serde_json::json!({})), None);
        assert_eq!(
            report_timestamp_ns(&serde_json::json!({"timestamp": "soon"})),
            None
        );
    }

    #[test]
    fn kpi_entry_is_lenient_about_unknown_schemas() {
        // Schema drift across srsRAN versions must not break ingestion.
        let raw = br#"{"totally":{"new":{"schema":true}},"v":"99.9"}"#;
        let entry = kpi_entry(raw, "r", 1).unwrap();
        assert_eq!(entry["context"]["kpi"]["totally"]["new"]["schema"], true);
    }
}
