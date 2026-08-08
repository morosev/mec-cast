//! Replay integration test: feed recorded srsRAN metric datagrams into the
//! collector over a real UDP socket, with a stub HTTP logging service, and
//! verify accounting, forwarding, and the arrival CSV — no lab required.

use std::io::{Read, Write};
use std::net::{TcpListener, UdpSocket};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use ran_collector::{run, CollectorConfig};

fn start_stub_http() -> (String, Arc<Mutex<Vec<String>>>) {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind stub");
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
                bodies.lock().unwrap().push(
                    String::from_utf8_lossy(&buf[header_end..header_end + content_length])
                        .to_string(),
                );
                let _ = stream.write_all(
                    b"HTTP/1.1 201 Created\r\nContent-Length: 24\r\nConnection: close\r\n\r\n{\"accepted\":1,\"ids\":[1]}",
                );
            });
        }
    });
    (format!("http://{addr}"), bodies)
}

#[test]
fn replay_fixture_end_to_end() {
    let (url, bodies) = start_stub_http();
    let dir = std::env::temp_dir().join(format!("ran-collector-replay-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);

    let socket = UdpSocket::bind("127.0.0.1:0").expect("bind collector socket");
    let collector_addr = socket.local_addr().unwrap();

    let mut cfg = CollectorConfig::new("replay-run", &dir);
    cfg.logging_url = Some(url);
    cfg.flush_interval = Duration::from_millis(100);

    let stop = Arc::new(AtomicBool::new(false));
    let collector = {
        let stop = Arc::clone(&stop);
        thread::spawn(move || run(socket, cfg, &stop).expect("collector run"))
    };

    // Replay the fixture (one datagram per line) plus one malformed datagram.
    let fixture = include_str!("../testdata/srsran_metrics.jsonl");
    let tx = UdpSocket::bind("127.0.0.1:0").unwrap();
    let mut sent = 0u64;
    for line in fixture.lines().filter(|l| !l.trim().is_empty()) {
        tx.send_to(line.as_bytes(), collector_addr).unwrap();
        sent += 1;
    }
    tx.send_to(b"### not json ###", collector_addr).unwrap();
    sent += 1;

    // Let the collector ingest and flush, then stop it.
    thread::sleep(Duration::from_millis(400));
    stop.store(true, Ordering::SeqCst);
    let report = collector.join().expect("collector thread");

    assert_eq!(report.datagrams, sent);
    assert_eq!(report.malformed, 1);
    assert_eq!(
        report.samples_written, sent,
        "every arrival lands in the CSV"
    );
    assert!(report.batches_posted >= 1, "no KPI batch was posted");
    assert_eq!(report.post_failures, 0);

    // Forwarded entries carry the KPIs verbatim under context.kpi.
    let bodies = bodies.lock().unwrap();
    let all: Vec<serde_json::Value> = bodies
        .iter()
        .flat_map(|b| {
            serde_json::from_str::<serde_json::Value>(b)
                .expect("valid batch JSON")
                .as_array()
                .expect("batch is an array")
                .clone()
        })
        .collect();
    assert_eq!(
        all.len() as u64,
        sent - 1,
        "all well-formed datagrams forwarded"
    );
    for entry in &all {
        assert_eq!(entry["service"], "mec-cast-ran");
        assert_eq!(entry["trace_id"], "replay-run");
        assert!(entry["context"]["recv_ns"].as_i64().unwrap() > 0);
    }
    assert_eq!(
        all[0]["context"]["kpi"]["ue_list"][0]["ue_container"]["rnti"],
        17921
    );

    // Arrival CSV exists with one row per datagram.
    let csv = std::fs::read_to_string(dir.join("samples.csv")).expect("csv");
    assert_eq!(csv.lines().count() as u64, 1 + sent);

    let _ = std::fs::remove_dir_all(&dir);
}
