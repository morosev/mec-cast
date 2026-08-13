//! C ABI for producers that are not Rust.
//!
//! This exists for the legacy WebRTC client (Profile B), whose per-frame hot
//! path lives in a C++ N-API addon linked against a patched libwebrtc. It
//! links this crate as a `staticlib` and pushes one sample per rendered
//! frame, so media runs land in the same CSV schema and the same logging
//! service as Profile A.
//!
//! Contract notes:
//!
//! - Every function tolerates NULL handles and returns a failure value
//!   rather than dereferencing.
//! - `extern "C"` aborts on unwind, so nothing here may panic. Fallible work
//!   returns NULL / `false`.
//! - The handle is **not** thread-safe: the ring is SPSC and `SampleSender`
//!   is single-owner. Call `mct_record` from one thread only (for the addon,
//!   that is the single WebRTC render callback thread).
//! - `trace_id` is derived from `run_id` exactly as the Python binding does
//!   (first 16 bytes, zero-padded), so both profiles of one run join.

use std::ffi::{c_char, CStr};

use crate::{
    recorder, Modality, PtpMonitor, RecorderConfig, RecorderHandle, Sample, SampleKind,
    SampleSender, TimingEnvelope,
};

/// Opaque recorder handle passed back to C.
pub struct MctRecorder {
    sender: SampleSender,
    handle: Option<RecorderHandle>,
    trace_id: [u8; 16],
}

/// Final accounting, mirrors [`crate::RecorderReport`].
#[repr(C)]
#[derive(Clone, Copy, Debug, Default)]
pub struct MctReport {
    pub samples_written: u64,
    pub samples_dropped: u64,
    pub snapshots_built: u64,
    pub snapshots_posted: u64,
    pub snapshots_dropped: u64,
    pub post_failures: u64,
}

/// Borrow a C string; `None` for NULL or invalid UTF-8.
///
/// # Safety
/// `p` must be NULL or a valid NUL-terminated C string.
unsafe fn opt_str<'a>(p: *const c_char) -> Option<&'a str> {
    if p.is_null() {
        return None;
    }
    CStr::from_ptr(p).to_str().ok()
}

/// Derive the run's trace_id the same way the Python binding does.
fn trace_id_from_run(run_id: &str) -> [u8; 16] {
    let mut trace_id = [0u8; 16];
    let src = run_id.as_bytes();
    let n = src.len().min(16);
    trace_id[..n].copy_from_slice(&src[..n]);
    trace_id
}

/// Start a recorder. Returns NULL on failure (bad arguments, or the output
/// directory / CSV could not be created).
///
/// `logging_url` may be NULL to write CSV only. `snapshot_interval_s <= 0`
/// selects the 2 s default.
///
/// # Safety
/// All pointer arguments must be NULL or valid NUL-terminated C strings.
/// The returned pointer must be released with [`mct_recorder_stop`].
#[no_mangle]
pub unsafe extern "C" fn mct_recorder_start(
    run_id: *const c_char,
    service: *const c_char,
    out_dir: *const c_char,
    logging_url: *const c_char,
    snapshot_interval_s: f64,
) -> *mut MctRecorder {
    let (Some(run_id), Some(service), Some(out_dir)) =
        (opt_str(run_id), opt_str(service), opt_str(out_dir))
    else {
        return std::ptr::null_mut();
    };

    let mut cfg = RecorderConfig::new(run_id, service, out_dir);
    cfg.logging_url = opt_str(logging_url)
        .filter(|s| !s.is_empty())
        .map(Into::into);
    if snapshot_interval_s > 0.0 {
        cfg.snapshot_interval = std::time::Duration::from_secs_f64(snapshot_interval_s.max(0.05));
    }

    match recorder::spawn(cfg, PtpMonitor::disabled()) {
        Ok((sender, handle)) => Box::into_raw(Box::new(MctRecorder {
            sender,
            handle: Some(handle),
            trace_id: trace_id_from_run(run_id),
        })),
        Err(_) => std::ptr::null_mut(),
    }
}

/// Record one sample. Returns `false` when the recorder is NULL, the
/// modality byte is unknown, or the queue was full — a full queue is counted
/// and surfaced in snapshots and the final report.
///
/// Never blocks and never allocates; safe to call from a media callback.
///
/// # Safety
/// `r` must be NULL or a pointer from [`mct_recorder_start`] that has not yet
/// been passed to [`mct_recorder_stop`], used from a single thread.
#[no_mangle]
#[allow(clippy::too_many_arguments)]
pub unsafe extern "C" fn mct_record(
    r: *mut MctRecorder,
    modality: u8,
    seq: u64,
    capture_ns: i64,
    send_ns: i64,
    recv_ns: i64,
    process_done_ns: i64,
    payload_bytes: u32,
    aux_ns: i64,
    site: u8,
) -> bool {
    let Some(rec) = r.as_mut() else {
        return false;
    };
    let Ok(modality) = Modality::try_from(modality) else {
        return false;
    };

    let mut envelope = TimingEnvelope::new(modality, seq, rec.trace_id);
    envelope.capture_ns = capture_ns;
    envelope.send_ns = send_ns;
    envelope.recv_ns = recv_ns;
    envelope.process_done_ns = process_done_ns;

    rec.sender.try_record(Sample {
        envelope,
        kind: SampleKind::Frame,
        site,
        payload_bytes,
        aux_ns,
    })
}

/// Samples dropped so far because the queue was full. 0 if `r` is NULL.
///
/// # Safety
/// Same as [`mct_record`].
#[no_mangle]
pub unsafe extern "C" fn mct_dropped_total(r: *const MctRecorder) -> u64 {
    match r.as_ref() {
        Some(rec) => rec.sender.dropped_total(),
        None => 0,
    }
}

/// Drain, flush, join the background threads, and free the recorder. Writes
/// final counts to `out` when it is non-NULL. The handle is invalid after
/// this call.
///
/// # Safety
/// `r` must be NULL or a pointer from [`mct_recorder_start`] not already
/// stopped. `out` must be NULL or point to a writable `MctReport`.
#[no_mangle]
pub unsafe extern "C" fn mct_recorder_stop(r: *mut MctRecorder, out: *mut MctReport) {
    if r.is_null() {
        return;
    }
    let mut rec = Box::from_raw(r);
    let report = rec.handle.take().map(RecorderHandle::shutdown);

    if let Some(out) = out.as_mut() {
        *out = match report {
            Some(rep) => MctReport {
                samples_written: rep.samples_written,
                samples_dropped: rep.samples_dropped,
                snapshots_built: rep.snapshots_built,
                snapshots_posted: rep.snapshots_posted,
                snapshots_dropped: rep.snapshots_dropped,
                post_failures: rep.post_failures,
            },
            None => MctReport::default(),
        };
    }
    // rec dropped here, releasing the sender.
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::ffi::CString;

    fn cs(s: &str) -> CString {
        CString::new(s).unwrap()
    }

    #[test]
    fn trace_id_matches_python_binding_derivation() {
        // Short ids are zero-padded; long ones truncate at 16 bytes.
        assert_eq!(&trace_id_from_run("abc")[..3], b"abc");
        assert_eq!(trace_id_from_run("abc")[3..], [0u8; 13]);
        let long = trace_id_from_run("0123456789abcdefGHIJ");
        assert_eq!(&long, b"0123456789abcdef");
    }

    #[test]
    fn start_record_stop_roundtrip() {
        let dir = std::env::temp_dir().join(format!("mct-ffi-{}", std::process::id()));
        let (run, svc, out) = (cs("run-1"), cs("mec-cast-media"), cs(dir.to_str().unwrap()));

        let r = unsafe {
            mct_recorder_start(
                run.as_ptr(),
                svc.as_ptr(),
                out.as_ptr(),
                std::ptr::null(),
                0.0,
            )
        };
        assert!(!r.is_null(), "recorder should start");

        for seq in 0..10u64 {
            let ok = unsafe {
                mct_record(
                    r, 1, /* Video */
                    seq, 1_000, 2_000, 3_000, 4_000, 1234, 0, 7,
                )
            };
            assert!(ok, "sample {seq} should be accepted");
        }

        // Unknown modality is rejected rather than aborting.
        assert!(!unsafe { mct_record(r, 99, 0, 0, 0, 0, 0, 0, 0, 0) });

        let mut report = MctReport::default();
        unsafe { mct_recorder_stop(r, &mut report) };
        assert_eq!(report.samples_written, 10);
        assert_eq!(report.samples_dropped, 0);

        let csv = std::fs::read_to_string(dir.join("samples.csv")).unwrap();
        assert_eq!(csv.lines().count(), 11, "header + 10 rows");
        assert!(csv.lines().nth(1).unwrap().contains("video"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn null_handles_are_tolerated() {
        assert!(!unsafe { mct_record(std::ptr::null_mut(), 1, 0, 0, 0, 0, 0, 0, 0, 0) });
        assert_eq!(unsafe { mct_dropped_total(std::ptr::null()) }, 0);
        unsafe { mct_recorder_stop(std::ptr::null_mut(), std::ptr::null_mut()) };

        let bad = unsafe {
            mct_recorder_start(
                std::ptr::null(),
                std::ptr::null(),
                std::ptr::null(),
                std::ptr::null(),
                0.0,
            )
        };
        assert!(bad.is_null(), "NULL arguments must fail cleanly");
    }
}
