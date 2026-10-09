//! The WebSocket source against a stub of srsRAN's `remote_control` server,
//! and `auto` against a gNB that sends on both transports at once.
//!
//! The stub does what the srsRAN docs describe: accept, wait for
//! `{"cmd":"metrics_subscribe"}`, answer it, then push one report per frame.
#![cfg(feature = "ws")]

use std::io::{Read, Write};
use std::net::{TcpListener, UdpSocket};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use ran_collector::source::{AutoSource, UdpSource, WsSource};
use ran_collector::{run, CollectorConfig};
use serde_json::Value;

const FIXTURE: &str = include_str!("../testdata/srsran_ws_metrics.synthetic.jsonl");

fn reports() -> Vec<&'static str> {
    FIXTURE.lines().filter(|l| !l.trim().is_empty()).collect()
}

/// srsRAN's remote_control, as far as a metrics client sees it.
fn start_stub_gnb(reports: Vec<&'static str>) -> String {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind stub gnb");
    let addr = listener.local_addr().unwrap();
    thread::spawn(move || {
        let Ok((stream, _)) = listener.accept() else {
            return;
        };
        let Ok(mut socket) = tungstenite::accept(stream) else {
            return;
        };
        // Nothing is pushed until the client subscribes.
        loop {
            match socket.read() {
                Ok(tungstenite::Message::Text(t)) if t.contains("metrics_subscribe") => break,
                Ok(_) => {}
                Err(_) => return,
            }
        }
        let _ = socket.send(tungstenite::Message::Text(
            r#"{"cmd":"metrics_subscribe"}"#.into(),
        ));
        for report in reports {
            let _ = socket.send(tungstenite::Message::Text(report.into()));
            thread::sleep(Duration::from_millis(20));
        }
        // Hold the connection open; the collector stops on its own flag.
        while socket.read().is_ok() {}
    });
    format!("{addr}")
}

/// Minimal HTTP sink for the KPI batches (same idea as tests/replay.rs).
fn start_stub_http() -> (String, Arc<Mutex<Vec<Value>>>) {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind stub http");
    let addr = listener.local_addr().unwrap();
    let entries = Arc::new(Mutex::new(Vec::new()));
    let sink = Arc::clone(&entries);
    thread::spawn(move || {
        for stream in listener.incoming() {
            let Ok(mut stream) = stream else { continue };
            let mut buf = Vec::new();
            let mut chunk = [0u8; 8192];
            let (header_end, length) = loop {
                let n = match stream.read(&mut chunk) {
                    Ok(0) | Err(_) => break (0, 0),
                    Ok(n) => n,
                };
                buf.extend_from_slice(&chunk[..n]);
                if let Some(pos) = buf.windows(4).position(|w| w == b"\r\n\r\n") {
                    let headers = String::from_utf8_lossy(&buf[..pos]).to_ascii_lowercase();
                    let length = headers
                        .lines()
                        .find_map(|l| l.strip_prefix("content-length:"))
                        .and_then(|v| v.trim().parse().ok())
                        .unwrap_or(0);
                    break (pos + 4, length);
                }
            };
            while header_end > 0 && buf.len() < header_end + length {
                match stream.read(&mut chunk) {
                    Ok(0) | Err(_) => break,
                    Ok(n) => buf.extend_from_slice(&chunk[..n]),
                }
            }
            if let Ok(Value::Array(batch)) =
                serde_json::from_slice::<Value>(&buf[header_end..header_end + length])
            {
                sink.lock().unwrap().extend(batch);
            }
            let _ = stream.write_all(
                b"HTTP/1.1 201 Created\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}",
            );
        }
    });
    (format!("http://{addr}"), entries)
}

fn temp_dir(name: &str) -> std::path::PathBuf {
    let dir = std::env::temp_dir().join(format!("ran-collector-{name}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    dir
}

#[test]
fn the_websocket_source_records_every_report_and_no_command_reply() {
    let gnb = start_stub_gnb(reports());
    let (url, entries) = start_stub_http();
    let dir = temp_dir("ws");

    let mut cfg = CollectorConfig::new("ws-run", &dir);
    cfg.logging_url = Some(url);
    cfg.flush_interval = Duration::from_millis(50);

    let stop = Arc::new(AtomicBool::new(false));
    let source = Box::new(WsSource::new(&gnb, Duration::from_millis(50)));
    let collector = {
        let stop = Arc::clone(&stop);
        thread::spawn(move || run(source, cfg, &stop).expect("collector run"))
    };
    thread::sleep(Duration::from_millis(800));
    stop.store(true, Ordering::SeqCst);
    let report = collector.join().unwrap();

    let n = reports().len() as u64;
    assert_eq!(
        report.datagrams, n,
        "the subscribe reply must not count as a report"
    );
    assert_eq!(report.malformed, 0);
    assert_eq!(report.samples_written, n);

    let entries = entries.lock().unwrap();
    assert_eq!(entries.len() as u64, n);
    assert_eq!(
        entries[0]["context"]["kpi"]["cells"][0]["ue_list"][0]["rnti"],
        17921
    );
    // The ISO timestamp of the current format is parsed, not just carried.
    assert_eq!(
        entries[0]["context"]["gnb_ts_ns"].as_i64(),
        Some(1_762_271_486_845_000_000)
    );

    let csv = std::fs::read_to_string(dir.join("ws-run/ran/samples.csv")).unwrap();
    assert_eq!(csv.lines().count() as u64, 1 + n);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn auto_locks_onto_one_transport_when_the_gnb_sends_on_both() {
    let gnb = start_stub_gnb(reports());
    let dir = temp_dir("auto");

    let udp = UdpSource::bind("127.0.0.1:0", Duration::from_millis(20)).unwrap();
    let udp_addr = udp.describe_addr();
    let source = Box::new(AutoSource::new(
        udp,
        WsSource::new(&gnb, Duration::from_millis(20)),
    ));

    // The same reports over UDP too, as a gNB configured for both would.
    let tx = UdpSocket::bind("127.0.0.1:0").unwrap();
    for report in reports() {
        tx.send_to(report.as_bytes(), &udp_addr).unwrap();
    }

    let cfg = CollectorConfig::new("auto-run", &dir);
    let stop = Arc::new(AtomicBool::new(false));
    let collector = {
        let stop = Arc::clone(&stop);
        thread::spawn(move || run(source, cfg, &stop).expect("collector run"))
    };
    thread::sleep(Duration::from_millis(800));
    stop.store(true, Ordering::SeqCst);
    let report = collector.join().unwrap();

    assert_eq!(
        report.datagrams,
        reports().len() as u64,
        "each report recorded once, not once per transport"
    );
    let _ = std::fs::remove_dir_all(&dir);
}
