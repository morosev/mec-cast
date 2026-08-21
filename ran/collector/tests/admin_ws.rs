//! The admin client against a stub server, and against the shared protocol
//! fixture.
//!
//! `services/admin/tests/vectors.json` is read by the admin service's own
//! tests, by the Python node client's tests, and by this one. Three
//! implementations of one protocol cannot drift apart while all three assert
//! against the same file.
#![cfg(feature = "admin")]

use std::net::TcpListener;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{channel, Sender};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use ran_collector::admin::{self, Command};
use serde_json::Value;

/// The fixture the Python side asserts against too.
fn vectors() -> serde_json::Map<String, Value> {
    let path = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../services/admin/tests/vectors.json"
    );
    let raw = std::fs::read_to_string(path)
        .unwrap_or_else(|e| panic!("shared vectors missing at {path}: {e}"));
    let parsed: Value = serde_json::from_str(&raw).expect("vectors.json is not valid JSON");
    parsed
        .as_object()
        .expect("vectors.json is not an object")
        .iter()
        .filter(|(name, _)| !name.starts_with('_'))
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect()
}

#[test]
fn the_shared_fixture_is_reachable_and_populated() {
    let v = vectors();
    assert!(!v.is_empty(), "no frames in vectors.json");
    // Every frame must be on the version this crate speaks, or the constants
    // have drifted apart.
    for (name, frame) in &v {
        assert_eq!(
            frame.get("v").and_then(Value::as_u64),
            Some(admin::PROTOCOL_VERSION),
            "{name} is not protocol v{}",
            admin::PROTOCOL_VERSION
        );
    }
}

#[test]
fn every_admin_to_node_frame_in_the_fixture_is_understood() {
    // The frames this crate must interpret. Node-to-admin frames are ours to
    // produce, not to parse.
    for (name, frame) in vectors() {
        let kind = frame.get("type").and_then(Value::as_str).unwrap();
        if !matches!(kind, "welcome" | "command" | "ping" | "error") {
            continue;
        }
        let text = serde_json::to_string(&frame).unwrap();
        // Parsing must not panic on any recorded shape.
        let parsed: Value = serde_json::from_str(&text).unwrap();
        assert_eq!(
            parsed.get("type").and_then(Value::as_str),
            Some(kind),
            "{name}"
        );
        // The fields this crate reads must be where it expects them.
        match kind {
            "command" => {
                let payload = parsed.get("payload").unwrap();
                assert!(
                    payload.get("command").and_then(Value::as_str).is_some(),
                    "{name}"
                );
                assert!(payload.get("run_id").is_some(), "{name}");
            }
            "welcome" => {
                assert!(
                    parsed.get("payload").unwrap().get("active_run").is_some(),
                    "{name}"
                );
            }
            _ => {}
        }
    }
}

#[test]
fn the_fixture_covers_the_commands_this_crate_acts_on() {
    let kinds: Vec<String> = vectors()
        .values()
        .filter_map(|f| {
            f.get("payload")
                .and_then(|p| p.get("command"))
                .and_then(Value::as_str)
                .map(str::to_string)
        })
        .collect();
    assert!(
        kinds.iter().any(|k| k == "run.start"),
        "no run.start vector"
    );
    assert!(kinds.iter().any(|k| k == "run.stop"), "no run.stop vector");
}

// --- against a stub server -------------------------------------------------

/// A WebSocket server just capable enough to test a client against, in the
/// spirit of `tests/replay.rs`'s stub HTTP listener.
fn start_stub(received: Sender<Value>, to_send: Vec<String>) -> String {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind stub");
    let addr = listener.local_addr().unwrap();
    thread::spawn(move || {
        let Ok((stream, _)) = listener.accept() else {
            return;
        };
        let Ok(mut socket) = tungstenite::accept(stream) else {
            return;
        };
        for frame in &to_send {
            let _ = socket.send(tungstenite::Message::Text(frame.clone()));
        }
        while let Ok(message) = socket.read() {
            if let tungstenite::Message::Text(text) = message {
                if let Ok(value) = serde_json::from_str::<Value>(&text) {
                    if received.send(value).is_err() {
                        return;
                    }
                }
            }
        }
    });
    format!("ws://{addr}")
}

fn wait_for(rx: &std::sync::mpsc::Receiver<Value>, kind: &str, timeout: Duration) -> Value {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if let Ok(frame) = rx.recv_timeout(Duration::from_millis(100)) {
            if frame.get("type").and_then(Value::as_str) == Some(kind) {
                return frame;
            }
        }
    }
    panic!("no {kind} frame within {timeout:?}");
}

#[test]
fn it_subscribes_on_startup_and_delivers_commands() {
    let (tx, rx) = channel();
    let start = serde_json::json!({
        "v": admin::PROTOCOL_VERSION, "type": "command",
        "msg_id": "m1", "ts_ns": 1, "node_id": Value::Null,
        "payload": {"command": "run.start", "run_id": "run-abc", "args": {}}
    })
    .to_string();
    let url = start_stub(tx, vec![start]);

    let stop = Arc::new(AtomicBool::new(false));
    let mut cfg = admin::AdminConfig::new(url, "gnb01", 0);
    cfg.retry = Duration::from_millis(200);
    let (handle, commands) = admin::spawn(cfg, Arc::clone(&stop));

    // Subscribing on startup is the requirement; hello is how it does it.
    let hello = wait_for(&rx, "hello", Duration::from_secs(5));
    assert_eq!(hello["node_id"], "gnb-gnb01-0");
    assert_eq!(hello["payload"]["node_type"], "gnb");
    assert_eq!(hello["v"], admin::PROTOCOL_VERSION);

    let command = commands
        .recv_timeout(Duration::from_secs(5))
        .expect("command never reached the collector");
    assert_eq!(
        command,
        Command::Start {
            run_id: "run-abc".into()
        }
    );

    stop.store(true, Ordering::SeqCst);
    handle.shutdown();
}

#[test]
fn it_answers_pings_and_says_goodbye() {
    let (tx, rx) = channel();
    let ping = serde_json::json!({
        "v": admin::PROTOCOL_VERSION, "type": "ping",
        "msg_id": "p1", "ts_ns": 1, "node_id": Value::Null, "payload": {}
    })
    .to_string();
    let url = start_stub(tx, vec![ping]);

    let stop = Arc::new(AtomicBool::new(false));
    let mut cfg = admin::AdminConfig::new(url, "gnb01", 0);
    cfg.retry = Duration::from_millis(200);
    let (handle, _commands) = admin::spawn(cfg, Arc::clone(&stop));

    wait_for(&rx, "hello", Duration::from_secs(5));
    wait_for(&rx, "pong", Duration::from_secs(5));

    handle.goodbye(Some("run-abc"), serde_json::json!({"samples_written": 7}));
    let goodbye = wait_for(&rx, "goodbye", Duration::from_secs(5));
    assert_eq!(goodbye["payload"]["final_report"]["samples_written"], 7);

    stop.store(true, Ordering::SeqCst);
    handle.shutdown();
}

#[test]
fn an_unreachable_admin_is_retried_not_fatal() {
    // Nothing is listening on this port. The client must keep trying rather
    // than failing the collector, which has measurements to take regardless.
    let stop = Arc::new(AtomicBool::new(false));
    let mut cfg = admin::AdminConfig::new("ws://127.0.0.1:1", "gnb01", 0);
    cfg.retry = Duration::from_millis(100);
    let (handle, _commands) = admin::spawn(cfg, Arc::clone(&stop));

    thread::sleep(Duration::from_millis(400));
    // Queueing while disconnected must not block or panic.
    handle.status(serde_json::json!({"node_type": "gnb", "state": "idle"}));

    stop.store(true, Ordering::SeqCst);
    let report = handle.shutdown();
    assert_eq!(report.frames_sent, 0);
}
