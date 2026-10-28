//! One run's recording at the relay: the per-frame samples (`record`) and the
//! WebRTC stats (`stats`), under `<RUNS_DIR>/<run_id>/relay/`.
//!
//! The relay forwards whether or not it records. Standalone it records one
//! run, `RUN_ID`, for its whole life; under the admin it opens a recording at
//! `run.start` and closes it at `run.stop`, so one process serves run after
//! run, each into its own directory.

use std::path::PathBuf;
use std::time::Duration;

use mec_cast_telemetry::{monitor_from_device, spawn_recorder, RecorderConfig, RecorderHandle};
use serde_json::{json, Value};

use crate::record::{self, FrameRecorder};
use crate::stats::StatsLog;

macro_rules! log {
    ($($arg:tt)*) => { eprintln!("[relay] {}", format!($($arg)*)) };
}

/// What every recording is opened with: the process's environment.
pub struct Settings {
    pub runs_dir: PathBuf,
    pub logging_url: Option<String>,
    pub ptp_device: Option<String>,
    pub stats_interval: Option<Duration>,
}

pub struct Recording {
    pub run_id: String,
    pub frames: FrameRecorder,
    pub stats: Option<StatsLog>,
    recorder: Option<RecorderHandle>,
}

impl Recording {
    pub fn open(settings: &Settings, run_id: &str) -> Self {
        let out_dir = settings.runs_dir.join(run_id).join("relay");
        let mut cfg = RecorderConfig::new(run_id, "mec-cast-relay", &out_dir);
        cfg.logging_url = settings.logging_url.clone();
        let (ptp, ptp_ok, ptp_why) = monitor_from_device(settings.ptp_device.as_deref());
        if !ptp_ok {
            log!("PTP monitor off: {ptp_why}");
        }
        let (samples, recorder) = match spawn_recorder(cfg, ptp) {
            Ok((s, h)) => (Some(s), Some(h)),
            Err(e) => {
                log!("WARNING: recording off, {}: {e}", out_dir.display());
                (None, None)
            }
        };
        let stats = settings
            .stats_interval
            .map(|every| StatsLog::new(&out_dir, run_id, settings.logging_url.as_deref(), every));
        log!("run {run_id}: recording to {}", out_dir.display());
        Recording {
            run_id: run_id.to_string(),
            frames: FrameRecorder::new(samples, record::trace_id(run_id)),
            stats,
            recorder,
        }
    }

    /// Flush everything; the report the admin keeps in run.json.
    pub fn close(self) -> Value {
        let frames = self.frames.frames_recorded;
        let mut report = json!({"frames": frames});
        if let Some(log) = self.stats {
            let rows = log.rows_written;
            let dropped = log.posts_dropped;
            let (posted, failed) = log.finish();
            report["webrtc_rows"] = json!(rows);
            report["webrtc_posts"] = json!(posted);
            report["webrtc_post_failures"] = json!(failed + dropped);
        }
        if let Some(h) = self.recorder {
            let r = h.shutdown();
            report["samples_written"] = json!(r.samples_written);
            report["samples_dropped"] = json!(r.samples_dropped);
            report["snapshots_posted"] = json!(r.snapshots_posted);
        }
        log!("run {}: closed, {report}", self.run_id);
        report
    }
}
