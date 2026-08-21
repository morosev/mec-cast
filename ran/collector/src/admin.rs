//! Admin control-plane client for the gNB collector.
//!
//! Synchronous throughout, because this crate is: one thread owns the socket,
//! a bounded channel carries frames out, another carries commands back, and a
//! `stop` flag is observed through a read timeout. That is the same shape the
//! telemetry recorder already uses for its writer and uploader threads
//! (`telemetry/src/recorder.rs`), so nothing new has to be reasoned about.
//!
//! No async runtime enters the dependency tree. See ADR-0007.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{sync_channel, Receiver, SyncSender, TrySendError};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::Duration;

use serde_json::{json, Value};
use tungstenite::stream::MaybeTlsStream;
use tungstenite::{connect, Message, WebSocket};

/// The concrete socket `connect` hands back with TLS compiled out.
type Socket = WebSocket<MaybeTlsStream<std::net::TcpStream>>;

/// Wire protocol version. Must match `services/admin/.../protocol.py`;
/// `services/admin/tests/vectors.json` is the shared fixture that proves it.
pub const PROTOCOL_VERSION: u64 = 1;

const OUTBOUND_CAPACITY: usize = 64;
const INBOUND_CAPACITY: usize = 16;
const READ_TIMEOUT: Duration = Duration::from_millis(200);
/// The requirement: retry every 30 s while the admin is unreachable.
pub const RETRY: Duration = Duration::from_secs(30);

/// What the admin asked this collector to do.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Command {
    /// Begin recording under this run id.
    Start { run_id: String },
    /// Stop recording and report.
    Stop,
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct AdminReport {
    pub frames_sent: u64,
    pub frames_dropped: u64,
    pub reconnects: u64,
}

pub struct AdminConfig {
    pub url: String,
    pub node_id: String,
    pub host: String,
    pub version_sha: String,
    pub version_tag: String,
    pub retry: Duration,
}

impl AdminConfig {
    pub fn new(url: impl Into<String>, host: impl Into<String>, instance: u32) -> Self {
        let host = host.into();
        Self {
            url: url.into(),
            node_id: format!("gnb-{host}-{instance}"),
            host,
            version_sha: std::env::var("VCS_REF").unwrap_or_default(),
            version_tag: std::env::var("VERSION").unwrap_or_default(),
            retry: RETRY,
        }
    }
}

/// Handle held by the main loop. Sending never blocks.
pub struct AdminHandle {
    outbound: SyncSender<String>,
    node_id: String,
    /// Latest identity, read by the socket thread when it (re)connects so a
    /// reconnect announces the run actually being recorded.
    identity: Arc<Mutex<Identity>>,
    dropped: Arc<Mutex<u64>>,
    thread: Option<JoinHandle<AdminReport>>,
}

#[derive(Clone, Default)]
struct Identity {
    state: String,
    run_id: Option<String>,
}

pub fn now_ns() -> i64 {
    use mec_cast_telemetry::{Clock, RealtimeClock};
    RealtimeClock.now_ns()
}

fn envelope(message_type: &str, node_id: &str, payload: Value) -> String {
    json!({
        "v": PROTOCOL_VERSION,
        "type": message_type,
        // A uuid crate would be one dependency for one field; the admin only
        // needs this to be unique enough to correlate an ack.
        "msg_id": format!("{}-{}", node_id, now_ns()),
        "ts_ns": now_ns(),
        "node_id": node_id,
        "payload": payload,
    })
    .to_string()
}

impl AdminHandle {
    /// Queue a status frame. Drops rather than blocking: the control plane
    /// must never be able to stall the metrics loop.
    pub fn status(&self, payload: Value) {
        self.send(envelope("status", &self.node_id, payload));
    }

    pub fn ack(&self, in_reply_to: &str, ok: bool) {
        self.send(envelope(
            "ack",
            &self.node_id,
            json!({"in_reply_to": in_reply_to, "ok": ok, "error": Value::Null}),
        ));
    }

    pub fn goodbye(&self, run_id: Option<&str>, final_report: Value) {
        self.send(envelope(
            "goodbye",
            &self.node_id,
            json!({"reason": "shutdown", "run_id": run_id, "final_report": final_report}),
        ));
    }

    pub fn set_identity(&self, state: &str, run_id: Option<&str>) {
        if let Ok(mut identity) = self.identity.lock() {
            identity.state = state.to_string();
            identity.run_id = run_id.map(str::to_string);
        }
    }

    fn send(&self, frame: String) {
        match self.outbound.try_send(frame) {
            Ok(()) => {}
            Err(TrySendError::Full(_)) | Err(TrySendError::Disconnected(_)) => {
                if let Ok(mut dropped) = self.dropped.lock() {
                    *dropped += 1;
                }
            }
        }
    }

    /// Join the socket thread. The caller must already have set `stop`.
    pub fn shutdown(mut self) -> AdminReport {
        match self.thread.take() {
            Some(thread) => thread.join().unwrap_or_default(),
            None => AdminReport::default(),
        }
    }
}

/// Start the client. Returns the handle and the command stream.
///
/// The thread reconnects for as long as `stop` is clear, so a collector
/// started before the admin simply joins when the admin appears.
pub fn spawn(cfg: AdminConfig, stop: Arc<AtomicBool>) -> (AdminHandle, Receiver<Command>) {
    let (outbound_tx, outbound_rx) = sync_channel::<String>(OUTBOUND_CAPACITY);
    let (inbound_tx, inbound_rx) = sync_channel::<Command>(INBOUND_CAPACITY);
    let identity = Arc::new(Mutex::new(Identity {
        state: "idle".to_string(),
        run_id: None,
    }));
    let dropped = Arc::new(Mutex::new(0u64));

    let node_id = cfg.node_id.clone();
    let thread_identity = Arc::clone(&identity);
    let thread =
        thread::spawn(move || socket_loop(cfg, stop, outbound_rx, inbound_tx, thread_identity));

    (
        AdminHandle {
            outbound: outbound_tx,
            node_id,
            identity,
            dropped,
            thread: Some(thread),
        },
        inbound_rx,
    )
}

fn socket_loop(
    cfg: AdminConfig,
    stop: Arc<AtomicBool>,
    outbound: Receiver<String>,
    inbound: SyncSender<Command>,
    identity: Arc<Mutex<Identity>>,
) -> AdminReport {
    let mut report = AdminReport::default();

    while !stop.load(Ordering::SeqCst) {
        match connect(&cfg.url) {
            Ok((mut socket, _response)) => {
                report.reconnects += 1;
                // The same trick the UDP loop uses: a read timeout is what
                // lets a blocking socket observe the stop flag.
                set_read_timeout(&socket);
                let hello = {
                    let identity = identity.lock().map(|i| i.clone()).unwrap_or_default();
                    envelope(
                        "hello",
                        &cfg.node_id,
                        json!({
                            "node_type": "gnb",
                            "node_id": cfg.node_id,
                            "host": cfg.host,
                            "pid": std::process::id(),
                            "version": {"sha": cfg.version_sha, "tag": cfg.version_tag},
                            "state": identity.state,
                            "run_id": identity.run_id,
                            "autostart": true,
                            "params": {},
                        }),
                    )
                };
                if socket.send(Message::Text(hello)).is_ok() {
                    session(&mut socket, &stop, &outbound, &inbound, &cfg, &mut report);
                }
                let _ = socket.close(None);
            }
            Err(_) => { /* unreachable; fall through to the retry wait */ }
        }

        if stop.load(Ordering::SeqCst) {
            break;
        }
        // Wait in short slices so shutdown is not delayed by the retry.
        let deadline = std::time::Instant::now() + cfg.retry;
        while std::time::Instant::now() < deadline && !stop.load(Ordering::SeqCst) {
            thread::sleep(Duration::from_millis(200));
        }
    }
    report
}

/// A read timeout is what lets a blocking socket observe the stop flag —
/// exactly the trick the UDP receive loop in `lib.rs` already uses.
fn set_read_timeout(socket: &Socket) {
    if let MaybeTlsStream::Plain(stream) = socket.get_ref() {
        let _ = stream.set_read_timeout(Some(READ_TIMEOUT));
    }
}

fn session(
    socket: &mut Socket,
    stop: &AtomicBool,
    outbound: &Receiver<String>,
    inbound: &SyncSender<Command>,
    cfg: &AdminConfig,
    report: &mut AdminReport,
) {
    while !stop.load(Ordering::SeqCst) {
        // Anything the main loop queued while we were away or busy.
        while let Ok(frame) = outbound.try_recv() {
            if socket.send(Message::Text(frame)).is_err() {
                return;
            }
            report.frames_sent += 1;
        }

        match socket.read() {
            Ok(Message::Text(text)) => {
                if let Some(command) = interpret(&text, socket, cfg) {
                    // Dropping a command is bad; blocking this thread is worse.
                    let _ = inbound.try_send(command);
                }
            }
            Ok(Message::Close(_)) => return,
            Ok(_) => {}
            Err(tungstenite::Error::Io(e))
                if e.kind() == std::io::ErrorKind::WouldBlock
                    || e.kind() == std::io::ErrorKind::TimedOut => {}
            Err(_) => return,
        }
    }
}

/// Parse one inbound frame, answering pings inline.
fn interpret(text: &str, socket: &mut Socket, cfg: &AdminConfig) -> Option<Command> {
    let value: Value = serde_json::from_str(text).ok()?;
    if value.get("v").and_then(Value::as_u64) != Some(PROTOCOL_VERSION) {
        // Loud but not fatal: the node keeps retrying so an admin upgrade heals it.
        eprintln!(
            "admin: unsupported protocol version {:?}; this node speaks {PROTOCOL_VERSION}",
            value.get("v")
        );
        return None;
    }
    let payload = value.get("payload");
    match value.get("type").and_then(Value::as_str)? {
        "ping" => {
            let _ = socket.send(Message::Text(envelope("pong", &cfg.node_id, json!({}))));
            None
        }
        "welcome" => payload
            .and_then(|p| p.get("active_run"))
            .filter(|v| !v.is_null())
            .and_then(|run| run.get("run_id"))
            .and_then(Value::as_str)
            .map(|run_id| Command::Start {
                run_id: run_id.to_string(),
            }),
        "command" => {
            let payload = payload?;
            let run_id = payload.get("run_id").and_then(Value::as_str);
            match payload.get("command").and_then(Value::as_str)? {
                "run.start" | "stream.start" => run_id.map(|r| Command::Start {
                    run_id: r.to_string(),
                }),
                "run.stop" | "stream.stop" => Some(Command::Stop),
                _ => None,
            }
        }
        _ => None,
    }
}

/// The status payload shape the admin expects from a gNB collector.
pub fn status_payload(
    state: &str,
    run_id: Option<&str>,
    bind: &str,
    peers: Vec<Value>,
    counters: Value,
    report: Value,
) -> Value {
    json!({
        "node_type": "gnb",
        "state": state,
        "run_id": run_id,
        "streaming": false,
        "subscribed": false,
        "peers": peers,
        "params": {"bind": bind},
        "counters": counters,
        "autostart": true,
        "last_error": Value::Null,
        "report": report,
    })
}
