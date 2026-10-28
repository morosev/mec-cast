//! WebRTC statistics at the relay: str0m's per-stream stats, one row per
//! stream every interval, in the column set the clients write too
//! (`clients/webrtc_native/stats_log.js`): `<RUNS_DIR>/<RUN_ID>/relay/webrtc.csv`,
//! and posted to the logging service (`LOGGING_URL`) as the RAN tap posts its
//! KPIs.
//!
//! - `in`: a publisher's stream reaching the relay -- the uplink as the relay
//!   sees it: jitter, loss, and the NACKs/PLIs/FIRs the relay sent back;
//! - `out`: the stream to a subscriber -- the downlink, with what the
//!   receiver reported about it (RTCP receiver reports), and the NACKs/PLIs/
//!   FIRs it sent.
//!
//! Counters are cumulative since the stream began. Posting runs on its own
//! thread behind a bounded channel: a slow logging service drops entries
//! (counted), never stalls forwarding.

use std::fs::File;
use std::io::{BufWriter, Write};
use std::path::Path;
use std::sync::mpsc::{sync_channel, SyncSender, TrySendError};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use serde_json::{json, Map, Value};
use str0m::media::MediaKind;
use str0m::stats::{MediaEgressStats, MediaIngressStats};

/// Keep in step with COLUMNS in clients/webrtc_native/stats_log.js.
pub const COLUMNS: [&str; 30] = [
    "ts_ns",
    "node",
    "peer",
    "direction",
    "kind",
    "mid",
    "ssrc",
    "codec",
    "packets",
    "bytes",
    "retransmitted_packets",
    "packets_lost",
    "loss_fraction",
    "jitter_ms",
    "rtt_ms",
    "nacks",
    "plis",
    "firs",
    "frames",
    "keyframes",
    "frames_dropped",
    "fps",
    "width",
    "height",
    "bwe_bps",
    "target_bps",
    "quality_limitation",
    "jitter_buffer_ms",
    "freezes",
    "freeze_ms",
];

/// One row, as named columns; absent columns are left empty.
pub struct Row(Map<String, Value>);

impl Row {
    fn new(ts_ns: i64, peer: &str, direction: &str, kind: MediaKind, mid: &str) -> Self {
        let mut m = Map::new();
        m.insert("ts_ns".into(), json!(ts_ns));
        m.insert("node".into(), json!("relay"));
        m.insert("peer".into(), json!(peer));
        m.insert("direction".into(), json!(direction));
        m.insert("kind".into(), json!(kind_name(kind)));
        m.insert("mid".into(), json!(mid));
        Row(m)
    }

    fn set(&mut self, k: &str, v: impl Into<Value>) {
        self.0.insert(k.into(), v.into());
    }

    fn opt(&mut self, k: &str, v: Option<impl Into<Value>>) {
        if let Some(v) = v {
            self.set(k, v);
        }
    }

    /// A publisher's stream arriving.
    pub fn ingress(
        ts_ns: i64,
        peer: &str,
        kind: MediaKind,
        ssrc: Option<u32>,
        s: &MediaIngressStats,
    ) -> Self {
        let mut r = Row::new(ts_ns, peer, "in", kind, &s.mid.to_string());
        r.opt("ssrc", ssrc);
        r.set("packets", s.packets);
        r.set("bytes", s.bytes);
        r.opt("loss_fraction", s.loss.map(round3));
        r.set("jitter_ms", round3(rtp_units_ms(s.jitter, kind)));
        r.opt("rtt_ms", s.rtt.map(ms));
        r.set("nacks", s.nacks);
        r.set("plis", s.plis);
        r.set("firs", s.firs);
        r
    }

    /// The stream going out to a subscriber.
    pub fn egress(
        ts_ns: i64,
        peer: &str,
        kind: MediaKind,
        ssrc: Option<u32>,
        s: &MediaEgressStats,
    ) -> Self {
        let mut r = Row::new(ts_ns, peer, "out", kind, &s.mid.to_string());
        r.opt("ssrc", ssrc);
        r.set("packets", s.packets);
        r.set("bytes", s.bytes);
        r.opt("loss_fraction", s.loss.map(round3));
        r.opt("rtt_ms", s.rtt.map(ms));
        if let Some(remote) = &s.remote {
            r.set("packets_lost", remote.packets_lost);
            r.set("jitter_ms", round3(rtp_units_ms(remote.jitter, kind)));
        }
        r.set("nacks", s.nacks);
        r.set("plis", s.plis);
        r.set("firs", s.firs);
        r
    }

    pub fn csv_line(&self) -> String {
        COLUMNS
            .iter()
            .map(|c| match self.0.get(*c) {
                None | Some(Value::Null) => String::new(),
                Some(Value::String(s)) => s.clone(),
                Some(v) => v.to_string(),
            })
            .collect::<Vec<_>>()
            .join(",")
    }
}

fn kind_name(kind: MediaKind) -> &'static str {
    match kind {
        MediaKind::Audio => "audio",
        MediaKind::Video => "video",
    }
}

/// RTCP jitter is in RTP timestamp units: 90 kHz for video, 48 kHz (Opus)
/// for audio.
fn rtp_units_ms(units: u32, kind: MediaKind) -> f64 {
    let hz = match kind {
        MediaKind::Video => 90_000.0,
        MediaKind::Audio => 48_000.0,
    };
    f64::from(units) * 1000.0 / hz
}

fn ms(d: Duration) -> f64 {
    round3(d.as_secs_f64() * 1000.0)
}

fn round3(v: impl Into<f64>) -> f64 {
    (v.into() * 1000.0).round() / 1000.0
}

/// The uploader thread's input, and its (posted, failed) at the end.
type Uploader = (SyncSender<Value>, JoinHandle<(u64, u64)>);

/// The CSV and the logging-service uploader.
pub struct StatsLog {
    csv: Option<BufWriter<File>>,
    run_id: String,
    pending: Vec<Value>,
    last_post: Instant,
    post_interval: Duration,
    upload: Option<Uploader>,
    pub rows_written: u64,
    pub posts_dropped: u64,
}

impl StatsLog {
    pub fn new(
        dir: &Path,
        run_id: &str,
        logging_url: Option<&str>,
        post_interval: Duration,
    ) -> Self {
        let csv = std::fs::create_dir_all(dir)
            .and_then(|_| File::create(dir.join("webrtc.csv")))
            .map(BufWriter::new)
            .and_then(|mut w| writeln!(w, "{}", COLUMNS.join(",")).map(|_| w));
        let csv = match csv {
            Ok(w) => Some(w),
            Err(e) => {
                eprintln!("[relay] WARNING: webrtc.csv off, {}: {e}", dir.display());
                None
            }
        };
        let upload = logging_url.map(|base| {
            let url = format!("{}/api/v1/logs", base.trim_end_matches('/'));
            let (tx, rx) = sync_channel::<Value>(8);
            let handle = thread::spawn(move || {
                let agent = ureq::AgentBuilder::new()
                    .timeout_connect(Duration::from_millis(500))
                    .timeout(Duration::from_secs(2))
                    .build();
                let (mut ok, mut failed) = (0, 0);
                for batch in rx {
                    match agent
                        .post(&url)
                        .set("Content-Type", "application/json")
                        .send_string(&batch.to_string())
                    {
                        Ok(_) => ok += 1,
                        Err(_) => failed += 1,
                    }
                }
                (ok, failed)
            });
            (tx, handle)
        });
        StatsLog {
            csv,
            run_id: run_id.to_string(),
            pending: Vec::new(),
            last_post: Instant::now(),
            post_interval,
            upload,
            rows_written: 0,
            posts_dropped: 0,
        }
    }

    pub fn record(&mut self, row: Row) {
        if let Some(w) = self.csv.as_mut() {
            // A full disk must not stop forwarding.
            let _ = writeln!(w, "{}", row.csv_line());
        }
        self.rows_written += 1;
        if self.upload.is_some() {
            self.pending.push(Value::Object(row.0));
        }
    }

    /// Once per post interval: flush the CSV, hand the rows to the uploader.
    pub fn maybe_flush(&mut self, now: Instant) {
        if now.duration_since(self.last_post) < self.post_interval {
            return;
        }
        self.last_post = now;
        if let Some(w) = self.csv.as_mut() {
            let _ = w.flush();
        }
        if self.pending.is_empty() {
            return;
        }
        let rows = std::mem::take(&mut self.pending);
        let Some((tx, _)) = &self.upload else { return };
        let entry = json!([{
            "level": "INFO",
            "service": "mec-cast-relay",
            "logger": "webrtc.stats",
            "message": "webrtc stats",
            "trace_id": self.run_id,
            "context": {"run_id": self.run_id, "node": "relay", "webrtc": rows},
        }]);
        match tx.try_send(entry) {
            Ok(()) => {}
            Err(TrySendError::Full(_)) | Err(TrySendError::Disconnected(_)) => {
                self.posts_dropped += 1
            }
        }
    }

    /// Flush and stop the uploader; (posted, failed).
    pub fn finish(mut self) -> (u64, u64) {
        self.last_post = Instant::now() - self.post_interval;
        self.maybe_flush(Instant::now());
        match self.upload.take() {
            Some((tx, handle)) => {
                drop(tx);
                handle.join().unwrap_or((0, 0))
            }
            None => (0, 0),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rtcp_jitter_converts_by_clock_rate() {
        assert_eq!(rtp_units_ms(90, MediaKind::Video), 1.0);
        assert_eq!(rtp_units_ms(48, MediaKind::Audio), 1.0);
    }

    #[test]
    fn the_client_writes_the_same_columns() {
        let js = include_str!("../../../clients/webrtc_native/stats_log.js");
        let start = js
            .find("const COLUMNS = [")
            .expect("COLUMNS in stats_log.js");
        let end = start + js[start..].find("];").unwrap();
        let theirs: Vec<String> = js[start..end]
            .split('"')
            .skip(1)
            .step_by(2)
            .map(str::to_string)
            .collect();
        assert_eq!(theirs, COLUMNS, "stats_log.js and stats.rs disagree");
    }
}
