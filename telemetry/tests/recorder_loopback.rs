//! Integration test: full recorder pipeline against a stub HTTP logging
//! service. Verifies the core accounting invariant
//! (`written + dropped == pushed`), CSV integrity, snapshot delivery, and
//! clean shutdown drain.
#![cfg(feature = "http")]

use std::io::{Read, Write};
use std::net::TcpListener;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use mec_cast_telemetry::{
    spawn_recorder, Modality, PtpMonitor, RecorderConfig, Sample, SampleKind, TimingEnvelope,
};

/// Minimal HTTP/1.1 server: accepts POSTs, stores bodies, replies 201.
fn start_stub_http() -> (String, Arc<Mutex<Vec<String>>>) {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind stub server");
    let addr = listener.local_addr().unwrap();
    let bodies = Arc::new(Mutex::new(Vec::new()));
    let bodies_srv = Arc::clone(&bodies);
    thread::spawn(move || {
        for stream in listener.incoming() {
            let Ok(mut stream) = stream else { continue };
            let bodies = Arc::clone(&bodies_srv);
            thread::spawn(move || {
                let mut buf = Vec::new();
                let mut chunk = [0u8; 4096];
                // Read until end of headers.
                let header_end = loop {
                    let n = match stream.read(&mut chunk) {
                        Ok(0) | Err(_) => return,
                        Ok(n) => n,
                    };
                    buf.extend_from_slice(&chunk[..n]);
                    if let Some(pos) = buf.windows(4).position(|w| w == b"\r\n\r\n") {
                        break pos + 4;
                    }
                };
                let headers = String::from_utf8_lossy(&buf[..header_end]).to_string();
                let content_length: usize = headers
                    .lines()
                    .find_map(|l| {
                        let (name, value) = l.split_once(':')?;
                        name.eq_ignore_ascii_case("content-length")
                            .then(|| value.trim().parse().ok())?
                    })
                    .unwrap_or(0);
                while buf.len() < header_end + content_length {
                    let n = match stream.read(&mut chunk) {
                        Ok(0) | Err(_) => return,
                        Ok(n) => n,
                    };
                    buf.extend_from_slice(&chunk[..n]);
                }
                let body = String::from_utf8_lossy(&buf[header_end..header_end + content_length])
                    .to_string();
                bodies.lock().unwrap().push(body);
                let resp = "HTTP/1.1 201 Created\r\nContent-Type: application/json\r\nContent-Length: 24\r\nConnection: close\r\n\r\n{\"accepted\":1,\"ids\":[1]}";
                let _ = stream.write_all(resp.as_bytes());
            });
        }
    });
    (format!("http://{addr}"), bodies)
}

fn make_sample(seq: u64, trace_id: [u8; 16]) -> Sample {
    let base = 1_700_000_000_000_000_000i64 + seq as i64 * 1_000_000;
    Sample {
        envelope: TimingEnvelope {
            capture_ns: base,
            send_ns: base + 2_000_000,          // sender pipeline: 2 ms
            recv_ns: base + 22_000_000,         // network: 20 ms
            process_done_ns: base + 25_000_000, // processing: 3 ms
            seq,
            modality: Modality::PointCloud,
            trace_id,
        },
        kind: SampleKind::Frame,
        site: 1,
        payload_bytes: 480_000,
        aux_ns: 0,
    }
}

#[test]
fn pipeline_accounts_for_every_sample_and_posts_snapshots() {
    let (url, bodies) = start_stub_http();
    let dir = std::env::temp_dir().join(format!("mec-cast-loopback-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);

    let run_id = "test-run-0001";
    let mut cfg = RecorderConfig::new(run_id, "mec-cast-test", &dir);
    cfg.logging_url = Some(url);
    cfg.snapshot_interval = Duration::from_millis(100);
    cfg.queue_capacity = 1024; // small on purpose: force the drop path

    let (mut sender, handle) = spawn_recorder(cfg, PtpMonitor::disabled()).expect("spawn recorder");

    const PUSHED: u64 = 50_000;
    let trace_id = *b"testrun000000001";
    for seq in 0..PUSHED {
        sender.try_record(make_sample(seq, trace_id));
    }

    // Let at least one timed snapshot fire while draining continues.
    thread::sleep(Duration::from_millis(300));
    drop(sender);
    let report = handle.shutdown();

    // 1. The accounting invariant: nothing vanishes silently.
    assert_eq!(
        report.samples_written + report.samples_dropped,
        PUSHED,
        "written {} + dropped {} != pushed {}",
        report.samples_written,
        report.samples_dropped,
        PUSHED
    );
    assert!(report.samples_written > 0, "writer wrote nothing");

    // 2. CSV integrity: header + exactly one line per written sample.
    let csv = std::fs::read_to_string(dir.join("samples.csv")).expect("read samples.csv");
    let mut lines = csv.lines();
    let header = lines.next().expect("csv header");
    assert!(header.starts_with("seq,modality,kind,site,capture_ns"));
    assert_eq!(lines.clone().count() as u64, report.samples_written);
    // Spot-check a data row: derived delays present and correct sign.
    let row = lines.next().expect("first data row");
    let cols: Vec<&str> = row.split(',').collect();
    assert_eq!(cols.len(), 14, "row has all columns: {row}");
    assert_eq!(cols[1], "pointcloud");
    assert_eq!(cols[10], "20000000", "network delay column");
    assert_eq!(cols[13], "2000000", "sender pipeline column");

    // 3. Snapshots reached the (stub) logging service.
    assert!(report.snapshots_built >= 1, "no snapshots built");
    assert_eq!(report.post_failures, 0);
    let bodies = bodies.lock().unwrap();
    assert!(
        report.snapshots_posted >= 1 && !bodies.is_empty(),
        "no snapshot arrived at the stub (posted={}, received={})",
        report.snapshots_posted,
        bodies.len()
    );
    let last: serde_json::Value = serde_json::from_str(bodies.last().unwrap()).expect("valid JSON");
    let entry = &last[0];
    assert_eq!(entry["service"], "mec-cast-test");
    assert_eq!(entry["trace_id"], run_id);
    assert_eq!(entry["logger"], "telemetry.recorder");
    let ctx = &entry["context"];
    assert_eq!(
        ctx["ptp"]["reliable"], false,
        "disabled PTP must report unreliable"
    );
    assert!(ctx["drops"]["samples_total"].is_u64());
    let network = &ctx["metrics"]["network"];
    assert_eq!(
        network["p50_ns"], 20_000_000,
        "p50 of a constant 20ms delay"
    );
    assert!(network["stddev_ns"].is_number(), "jitter must be emitted");

    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn recorder_without_logging_url_still_writes_csv() {
    let dir = std::env::temp_dir().join(format!("mec-cast-loopback-nolog-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);

    let cfg = RecorderConfig::new("run-nolog", "mec-cast-test", &dir);
    let (mut sender, handle) = spawn_recorder(cfg, PtpMonitor::disabled()).expect("spawn");
    for seq in 0..100 {
        assert!(sender.try_record(make_sample(seq, [0u8; 16])));
    }
    drop(sender);
    let report = handle.shutdown();

    assert_eq!(report.samples_written, 100);
    assert_eq!(report.samples_dropped, 0);
    assert_eq!(report.snapshots_posted, 0);
    let csv = std::fs::read_to_string(dir.join("samples.csv")).unwrap();
    assert_eq!(csv.lines().count(), 101);

    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn reusing_a_run_id_appends_instead_of_destroying_earlier_frames() {
    // Restarting a component mid-experiment reuses RUN_ID, so it reopens the
    // same samples.csv. Truncating there would silently destroy measurement
    // data that exists nowhere else — the logging service only ever receives
    // aggregates.
    let dir = std::env::temp_dir().join(format!("mec-cast-loopback-reuse-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);

    let mut written = 0u64;
    for run in 0..2 {
        let cfg = RecorderConfig::new("run-reused", "mec-cast-test", &dir);
        let (mut sender, handle) = spawn_recorder(cfg, PtpMonitor::disabled()).expect("spawn");
        for seq in 0..50 {
            assert!(sender.try_record(make_sample(run * 50 + seq, [0u8; 16])));
        }
        drop(sender);
        written += handle.shutdown().samples_written;
    }

    assert_eq!(written, 100);
    let csv = std::fs::read_to_string(dir.join("samples.csv")).unwrap();
    let lines: Vec<&str> = csv.lines().collect();

    // One header, then every frame from both incarnations.
    assert_eq!(lines.iter().filter(|l| l.starts_with("seq,")).count(), 1);
    assert_eq!(lines.len(), 101);

    let seqs: Vec<u64> = lines[1..]
        .iter()
        .map(|l| l.split(',').next().unwrap().parse().unwrap())
        .collect();
    assert_eq!(seqs, (0..100).collect::<Vec<u64>>());

    let _ = std::fs::remove_dir_all(&dir);
}

/// Unsynchronised clocks: the sender's stamp is AHEAD of the receiver's, so
/// `recv_ns - send_ns` is negative. That is physically impossible for a
/// one-way delay and is the signature of hosts whose clocks disagree -- the
/// case that produced `network_ns=-11231127381` in the lab.
///
/// Two things must hold. The count must rise, because it is what the admin
/// raises WF_CLOCK_SKEW on. And the value must stay OUT of the statistics: a
/// single -11 s sample would otherwise become the window's min and drag its
/// mean, making a broken run look merely slow.
#[test]
fn negative_delays_are_counted_and_kept_out_of_the_stats() {
    let dir = std::env::temp_dir().join(format!("mec-cast-skew-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);

    let cfg = RecorderConfig::new("run-skew", "mec-cast-test", &dir);
    let (mut sender, handle) = spawn_recorder(cfg, PtpMonitor::disabled()).expect("spawn recorder");

    let trace = [7u8; 16];
    // Ten healthy frames, then ten from a sender whose clock is 11 s ahead.
    for seq in 0..10 {
        assert!(sender.try_record(make_sample(seq, trace)));
    }
    for seq in 10..20 {
        let mut s = make_sample(seq, trace);
        s.envelope.recv_ns = s.envelope.send_ns - 11_000_000_000;
        s.envelope.process_done_ns = s.envelope.recv_ns + 3_000_000;
        assert!(sender.try_record(s));
    }

    // Give the writer thread time to drain before reading the counter.
    thread::sleep(Duration::from_millis(300));
    let negative = sender.negative_delays();
    let report = handle.shutdown();

    assert!(
        negative >= 10,
        "expected at least the 10 skewed frames counted, got {negative}"
    );
    assert_eq!(
        report.samples_dropped, 0,
        "nothing should have been dropped"
    );

    // The raw values stay in the CSV -- it records what was measured.
    let csv = std::fs::read_to_string(dir.join("samples.csv")).expect("csv");
    assert!(
        csv.contains("-11"),
        "the negative delay must still appear in the CSV"
    );
}
