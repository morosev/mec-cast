//! Async recording pipeline: ns-precision measurement that never blocks the
//! measured path.
//!
//! Design:
//!
//! ```text
//! hot path ──try_record()──► SPSC ring (rtrb, bounded) ──► writer thread
//!   never blocks/allocs         full ⇒ drop + count           │
//!                                                             ├─► samples.csv (BufWriter)
//!                                                             ├─► per-metric DelayStats
//!                                                             └─► every interval: snapshot JSON
//!                                                                   │  bounded channel (drop + count)
//!                                                                   ▼
//!                                                             uploader thread ──POST──► logging service
//! ```
//!
//! Invariants:
//! - `try_record` is O(1), lock-free, allocation-free; on a full ring it
//!   increments a drop counter and returns `false`.
//! - The writer thread owns the CSV file and all `DelayStats`; nothing is
//!   shared mutable.
//! - The uploader is a separate thread so a stalled logging service can
//!   never stall CSV writing; its input channel is bounded and drops (with a
//!   count) rather than backing up.
//! - Every snapshot reports its own losses (`drops`) and clock health
//!   (`ptp`), so analysis can trust or discard windows after the fact.

use std::fs;
use std::io::{BufWriter, Write as _};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use crate::envelope::TimingEnvelope;
use crate::ptp::PtpMonitor;
use crate::stats::DelayStats;

/// What one sample describes.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(u8)]
pub enum SampleKind {
    Frame = 0,
    Packet = 1,
    Event = 2,
}

impl SampleKind {
    pub fn as_str(&self) -> &'static str {
        match self {
            SampleKind::Frame => "frame",
            SampleKind::Packet => "packet",
            SampleKind::Event => "event",
        }
    }
}

/// One measurement sample: a stamped envelope plus local context. Fixed
/// size, `Copy`, no heap — safe to move through the ring on the hot path.
#[derive(Clone, Copy, Debug)]
pub struct Sample {
    pub envelope: TimingEnvelope,
    pub kind: SampleKind,
    /// Free-form producer tag (which pipeline site produced this sample);
    /// recorded to CSV for filtering.
    pub site: u8,
    /// Payload size (frame / packet bytes) for throughput analysis.
    pub payload_bytes: u32,
    /// Spare producer-defined duration (e.g. decode time). 0 when unused.
    pub aux_ns: i64,
}

/// Recorder configuration. `run_id` doubles as the logging-service
/// `trace_id`, joining samples across processes for one experiment run.
pub struct RecorderConfig {
    pub run_id: String,
    /// Logging-service `service` field, e.g. "mec-cast-edge".
    pub service: String,
    /// Directory for per-run output; `samples.csv` is created inside.
    pub out_dir: PathBuf,
    /// Base URL of the logging service (e.g. `http://localhost:8000`).
    /// `None` disables uploading; CSV is always written.
    pub logging_url: Option<String>,
    pub snapshot_interval: Duration,
    pub queue_capacity: usize,
    pub stats_window: usize,
}

impl RecorderConfig {
    pub fn new(
        run_id: impl Into<String>,
        service: impl Into<String>,
        out_dir: impl Into<PathBuf>,
    ) -> Self {
        Self {
            run_id: run_id.into(),
            service: service.into(),
            out_dir: out_dir.into(),
            logging_url: None,
            snapshot_interval: Duration::from_secs(2),
            queue_capacity: 8192,
            stats_window: 8192,
        }
    }
}

/// Hot-path handle. Single-owner (the ring is SPSC); methods take `&mut`.
pub struct SampleSender {
    producer: rtrb::Producer<Sample>,
    dropped: Arc<AtomicU64>,
    /// Derived delays that came out negative -- see `negative_delays`.
    negative: Arc<AtomicU64>,
}

impl SampleSender {
    /// Record a sample. Never blocks; returns `false` (and counts the drop)
    /// when the ring is full.
    #[inline]
    pub fn try_record(&mut self, sample: Sample) -> bool {
        match self.producer.push(sample) {
            Ok(()) => true,
            Err(_) => {
                self.dropped.fetch_add(1, Ordering::Relaxed);
                false
            }
        }
    }

    pub fn dropped_total(&self) -> u64 {
        self.dropped.load(Ordering::Relaxed)
    }

    /// Cross-host delays that computed to a negative value.
    ///
    /// A one-way delay cannot be negative: `recv_ns - send_ns` below zero
    /// means the sender's clock is AHEAD of the receiver's, which is
    /// unambiguous evidence that the two hosts are not synchronised. It is
    /// not a small error either -- the skew is the whole magnitude, so a
    /// single such sample can dominate a window's mean and standard
    /// deviation and make min meaningless.
    ///
    /// Non-zero here means every cross-host figure from this recorder is
    /// suspect, whatever `ptp.reliable` says.
    pub fn negative_delays(&self) -> u64 {
        self.negative.load(Ordering::Relaxed)
    }
}

/// Final accounting returned by [`RecorderHandle::shutdown`]. The key
/// invariant callers should assert in tests:
/// `samples_written + samples_dropped == samples produced`.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct RecorderReport {
    pub samples_written: u64,
    pub samples_dropped: u64,
    pub snapshots_built: u64,
    pub snapshots_posted: u64,
    pub snapshots_dropped: u64,
    pub post_failures: u64,
}

struct WriterOutcome {
    samples_written: u64,
    snapshots_built: u64,
    snapshots_dropped: u64,
}

/// Owns the background threads. Call [`shutdown`](Self::shutdown) (after
/// dropping / stopping producers) to drain, flush, and get final counts.
pub struct RecorderHandle {
    stop: Arc<AtomicBool>,
    dropped: Arc<AtomicU64>,
    writer: thread::JoinHandle<WriterOutcome>,
    #[cfg(feature = "http")]
    uploader: Option<thread::JoinHandle<(u64, u64)>>, // (posted, failures)
}

impl RecorderHandle {
    /// Signal the writer to finish, drain everything still in the ring,
    /// flush the CSV, post a final snapshot, and return final accounting.
    ///
    /// Producers should stop recording before this is called; samples pushed
    /// concurrently with shutdown may or may not be included.
    pub fn shutdown(self) -> RecorderReport {
        self.stop.store(true, Ordering::SeqCst);
        self.writer.thread().unpark();
        let w = self.writer.join().expect("recorder writer thread panicked");
        #[cfg(feature = "http")]
        let (posted, failures) = match self.uploader {
            Some(u) => u.join().expect("recorder uploader thread panicked"),
            None => (0, 0),
        };
        #[cfg(not(feature = "http"))]
        let (posted, failures) = (0u64, 0u64);
        RecorderReport {
            samples_written: w.samples_written,
            samples_dropped: self.dropped.load(Ordering::Relaxed),
            snapshots_built: w.snapshots_built,
            snapshots_posted: posted,
            snapshots_dropped: w.snapshots_dropped,
            post_failures: failures,
        }
    }
}

const CSV_HEADER: &str = "seq,modality,kind,site,capture_ns,send_ns,recv_ns,process_done_ns,\
                          payload_bytes,aux_ns,network_ns,e2e_ns,processing_ns,sender_ns";

/// Start the recorder: creates `out_dir`, opens `samples.csv`, spawns the
/// writer (and, with `http` + `logging_url`, the uploader).
///
/// `samples.csv` is opened for **append**. A run whose `RUN_ID` is reused —
/// restarting one container mid-experiment, say — adds to the file rather than
/// truncating it, because the earlier frames are measurement data and the
/// second incarnation is the same logical run. The header is written only when
/// the file is new, so appending leaves one header at the top.
pub fn spawn(
    cfg: RecorderConfig,
    ptp: PtpMonitor,
) -> std::io::Result<(SampleSender, RecorderHandle)> {
    fs::create_dir_all(&cfg.out_dir)?;
    let path = cfg.out_dir.join("samples.csv");
    let file = fs::OpenOptions::new()
        .append(true)
        .create(true)
        .open(&path)?;
    let needs_header = file.metadata()?.len() == 0;
    let mut csv = BufWriter::new(file);
    if needs_header {
        writeln!(csv, "{CSV_HEADER}")?;
    }

    let (producer, mut consumer) = rtrb::RingBuffer::<Sample>::new(cfg.queue_capacity.max(1));
    let dropped = Arc::new(AtomicU64::new(0));
    let negative = Arc::new(AtomicU64::new(0));
    let stop = Arc::new(AtomicBool::new(false));

    #[cfg(feature = "http")]
    let (upload_tx, uploader) = match &cfg.logging_url {
        Some(base) => {
            let (tx, rx) = std::sync::mpsc::sync_channel::<String>(64);
            let url = format!("{}/api/v1/logs", base.trim_end_matches('/'));
            (Some(tx), Some(thread::spawn(move || upload_loop(rx, &url))))
        }
        None => (None, None),
    };

    let writer = {
        let stop = Arc::clone(&stop);
        let dropped = Arc::clone(&dropped);
        let negative = Arc::clone(&negative);
        thread::spawn(move || {
            let mut state = WriterState::new(&cfg, ptp, dropped, negative);
            loop {
                let mut drained = 0usize;
                while let Ok(sample) = consumer.pop() {
                    state.write_sample(&mut csv, &sample);
                    drained += 1;
                }
                let stopping = stop.load(Ordering::SeqCst);
                if state.snapshot_due() || (stopping && state.rows_written > 0) {
                    #[cfg(feature = "http")]
                    state.emit_snapshot(upload_tx.as_ref());
                    #[cfg(not(feature = "http"))]
                    state.emit_snapshot();
                }
                if stopping && consumer.is_empty() {
                    break;
                }
                if drained == 0 {
                    thread::park_timeout(Duration::from_millis(50));
                } else {
                    let _ = csv.flush();
                }
            }
            let _ = csv.flush();
            WriterOutcome {
                samples_written: state.rows_written,
                snapshots_built: state.snapshots_built,
                snapshots_dropped: state.snapshots_dropped,
            }
        })
    };

    Ok((
        SampleSender {
            producer,
            dropped: Arc::clone(&dropped),
            negative: Arc::clone(&negative),
        },
        RecorderHandle {
            stop,
            dropped,
            writer,
            #[cfg(feature = "http")]
            uploader,
        },
    ))
}

/// Everything the writer thread owns.
// Without the `http` feature several fields are carried but only consumed by
// the JSON snapshot builder; keep the struct shape identical across features.
#[cfg_attr(not(feature = "http"), allow(dead_code))]
struct WriterState {
    run_id: String,
    service: String,
    ptp: PtpMonitor,
    dropped: Arc<AtomicU64>,
    negative: Arc<AtomicU64>,
    stats_network: DelayStats,
    stats_e2e: DelayStats,
    stats_processing: DelayStats,
    stats_sender: DelayStats,
    rows_written: u64,
    snapshots_built: u64,
    snapshots_dropped: u64,
    dropped_at_last_snapshot: u64,
    next_snapshot: Instant,
    snapshot_interval: Duration,
    seq_first: Option<u64>,
    seq_last: Option<u64>,
}

impl WriterState {
    fn new(
        cfg: &RecorderConfig,
        ptp: PtpMonitor,
        dropped: Arc<AtomicU64>,
        negative: Arc<AtomicU64>,
    ) -> Self {
        Self {
            run_id: cfg.run_id.clone(),
            service: cfg.service.clone(),
            ptp,
            dropped,
            negative,
            stats_network: DelayStats::with_window(cfg.stats_window),
            stats_e2e: DelayStats::with_window(cfg.stats_window),
            stats_processing: DelayStats::with_window(cfg.stats_window),
            stats_sender: DelayStats::with_window(cfg.stats_window),
            rows_written: 0,
            snapshots_built: 0,
            snapshots_dropped: 0,
            dropped_at_last_snapshot: 0,
            next_snapshot: Instant::now() + cfg.snapshot_interval,
            snapshot_interval: cfg.snapshot_interval,
            seq_first: None,
            seq_last: None,
        }
    }

    fn snapshot_due(&self) -> bool {
        Instant::now() >= self.next_snapshot
    }

    fn write_sample(&mut self, csv: &mut BufWriter<fs::File>, s: &Sample) {
        let e = &s.envelope;
        // A derived delay is defined only when both of its stamps are set.
        let derived = |a: i64, b: i64| -> Option<i64> {
            if a != 0 && b != 0 {
                Some(b - a)
            } else {
                None
            }
        };
        let network = derived(e.send_ns, e.recv_ns);
        let e2e = derived(e.capture_ns, e.process_done_ns);
        let processing = derived(e.recv_ns, e.process_done_ns);
        let sender = derived(e.capture_ns, e.send_ns);

        // A negative one-way delay is physically impossible: it means the
        // sender's clock is ahead of the receiver's. Count it, and keep it out
        // of the statistics -- the CSV still carries the raw value, because
        // that is the record of what was actually measured, but a single
        // -11 s sample would otherwise set the window's min and drag its mean
        // and stddev with it.
        // All four, not only the cross-host ones: a same-host delay going
        // negative means the clock stepped mid-frame, which is equally
        // impossible and equally worth knowing.
        for d in [network, e2e, processing, sender] {
            if matches!(d, Some(v) if v < 0) {
                self.negative.fetch_add(1, Ordering::Relaxed);
            }
        }

        if let Some(v) = network.filter(|v| *v >= 0) {
            self.stats_network.record(v);
        }
        if let Some(v) = e2e.filter(|v| *v >= 0) {
            self.stats_e2e.record(v);
        }
        if let Some(v) = processing.filter(|v| *v >= 0) {
            self.stats_processing.record(v);
        }
        if let Some(v) = sender.filter(|v| *v >= 0) {
            self.stats_sender.record(v);
        }
        self.seq_first.get_or_insert(e.seq);
        self.seq_last = Some(e.seq);

        let opt = |v: Option<i64>| v.map(|x| x.to_string()).unwrap_or_default();
        let _ = writeln!(
            csv,
            "{},{},{},{},{},{},{},{},{},{},{},{},{},{}",
            e.seq,
            e.modality.as_str(),
            s.kind.as_str(),
            s.site,
            e.capture_ns,
            e.send_ns,
            e.recv_ns,
            e.process_done_ns,
            s.payload_bytes,
            s.aux_ns,
            opt(network),
            opt(e2e),
            opt(processing),
            opt(sender),
        );
        self.rows_written += 1;
    }

    #[cfg(feature = "http")]
    fn emit_snapshot(&mut self, tx: Option<&std::sync::mpsc::SyncSender<String>>) {
        self.next_snapshot = Instant::now() + self.snapshot_interval;
        self.snapshots_built += 1;
        let dropped_total = self.dropped.load(Ordering::Relaxed);
        let dropped_delta = dropped_total - self.dropped_at_last_snapshot;
        self.dropped_at_last_snapshot = dropped_total;
        if let Some(tx) = tx {
            let body = self.snapshot_json(dropped_total, dropped_delta);
            if tx.try_send(body).is_err() {
                self.snapshots_dropped += 1;
            }
        }
    }

    #[cfg(not(feature = "http"))]
    fn emit_snapshot(&mut self) {
        self.next_snapshot = Instant::now() + self.snapshot_interval;
        self.snapshots_built += 1;
        let dropped_total = self.dropped.load(Ordering::Relaxed);
        self.dropped_at_last_snapshot = dropped_total;
    }

    /// One `LogEntryCreate` in a batch array. The service schema is
    /// `extra="forbid"`: only known top-level keys; everything custom lives
    /// under `context`. `timestamp` is omitted — the service stamps
    /// ingestion time.
    #[cfg(feature = "http")]
    fn snapshot_json(&self, dropped_total: u64, dropped_delta: u64) -> String {
        use serde_json::json;

        let stats_json = |s: &DelayStats| match s.snapshot() {
            Some(v) => json!({
                "count": v.count,
                "last_ns": v.last_ns,
                "min_ns": v.min_ns,
                "max_ns": v.max_ns,
                "mean_ns": v.mean_ns,
                "stddev_ns": v.stddev_ns,
                "p50_ns": v.p50_ns,
                "p90_ns": v.p90_ns,
                "p99_ns": v.p99_ns,
            }),
            None => serde_json::Value::Null,
        };
        let ptp = self.ptp.poll();
        let level = if dropped_delta > 0 { "WARNING" } else { "INFO" };
        let message = if dropped_delta > 0 {
            format!("latency snapshot ({dropped_delta} samples dropped)")
        } else {
            "latency snapshot".to_string()
        };
        let entry = json!([{
            "level": level,
            "service": self.service,
            "host": hostname(),
            "logger": "telemetry.recorder",
            "message": message,
            "trace_id": self.run_id,
            "context": {
                "run_id": self.run_id,
                "interval_s": self.snapshot_interval.as_secs_f64(),
                "metrics": {
                    "network": stats_json(&self.stats_network),
                    "e2e": stats_json(&self.stats_e2e),
                    "processing": stats_json(&self.stats_processing),
                    "sender": stats_json(&self.stats_sender),
                },
                "drops": {
                    "samples_total": dropped_total,
                    "samples_delta": dropped_delta,
                    "snapshots": self.snapshots_dropped,
                },
                "ptp": {
                    "offset_ns": ptp.offset_ns,
                    "reliable": ptp.reliable,
                },
                "seq": {
                    "first": self.seq_first,
                    "last": self.seq_last,
                },
                "rows_written": self.rows_written,
            }
        }]);
        entry.to_string()
    }
}

#[cfg(feature = "http")]
fn upload_loop(rx: std::sync::mpsc::Receiver<String>, url: &str) -> (u64, u64) {
    let agent = ureq::AgentBuilder::new()
        .timeout_connect(Duration::from_millis(500))
        .timeout(Duration::from_secs(2))
        .build();
    let mut posted = 0u64;
    let mut failures = 0u64;
    // Ends when the writer thread drops the sender side.
    for body in rx {
        match agent
            .post(url)
            .set("Content-Type", "application/json")
            .send_string(&body)
        {
            Ok(_) => posted += 1,
            Err(_) => failures += 1,
        }
    }
    (posted, failures)
}

#[cfg(feature = "http")]
fn hostname() -> String {
    let mut buf = [0u8; 256];
    // SAFETY: buf is a valid writable buffer of the stated length.
    let rc = unsafe { libc::gethostname(buf.as_mut_ptr() as *mut libc::c_char, buf.len() - 1) };
    if rc != 0 {
        return "unknown".to_string();
    }
    let len = buf.iter().position(|&b| b == 0).unwrap_or(buf.len());
    String::from_utf8_lossy(&buf[..len]).into_owned()
}
