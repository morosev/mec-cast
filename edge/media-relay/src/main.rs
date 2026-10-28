//! mec-cast-relay: the media profile's relay on the edge.
//!
//! A WebRTC sender publishes a stream with WHIP (`POST /whip/<stream>`, an SDP
//! offer in, an SDP answer out); a receiver plays it with WHEP
//! (`POST /whep/<stream>`). The relay forwards the sender's RTP packets to
//! every receiver of the stream as they are, without decoding: str0m's RTP
//! mode, one UDP socket, ICE-lite. Each frame is measured on the way
//! (`record`), and the mec-cast header extension carrying the sender's stamps
//! is written back unchanged (`ext`), so the receiver measures as it would
//! with no relay. The receiver's keyframe requests go back to the sender.
//!
//! Configuration, all by environment:
//!   RELAY_HTTP       WHIP/WHEP address           (default 0.0.0.0:8090)
//!   RELAY_UDP        media address               (default 0.0.0.0:8090)
//!   RELAY_PUBLIC_IP  the address clients reach the media address at; it is
//!                    the one ICE candidate offered (default: RELAY_UDP's IP,
//!                    which must then not be 0.0.0.0)
//!   ADMIN_URL        the admin service (ws://<infra>:8099/ws/node): the relay
//!                    joins it as node type `relay` and records the runs it
//!                    starts. Unset, it records RUN_ID from start to exit.
//!   RUN_ID, RUNS_DIR, LOGGING_URL   as every mec-cast node; samples go to
//!                    <RUNS_DIR>/<run>/relay/samples.csv
//!   PTP_DEVICE       PTP hardware clock to monitor (Linux; default none)
//!   RELAY_STATS_INTERVAL_MS  WebRTC stats per stream to relay/webrtc.csv and
//!                    the logging service, every so often (default 1000; 0 off)
//!
//! Synchronous throughout, like the rest of mec-cast's Rust (ADR-0007): the
//! HTTP server on its own thread, everything WebRTC on one loop thread.

mod ext;
mod record;
mod session;
mod stats;

use std::collections::HashMap;
use std::io::ErrorKind;
use std::net::{IpAddr, SocketAddr, UdpSocket};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::mpsc::{self, Receiver, Sender, TryRecvError};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use mec_cast_telemetry::admin::{self, AdminConfig, AdminHandle, Command};
use serde_json::{json, Value};
use str0m::change::SdpOffer;
use str0m::format::CodecSpec;
use str0m::media::{Direction, KeyframeRequestKind, MediaKind, Mid};
use str0m::net::{Protocol, Receive};
use str0m::rtp::{Extension, RtpPacket, RtpWrite, Ssrc};
use str0m::stats::{MediaEgressStats, MediaIngressStats};
use str0m::{Candidate, Event, Input, Output, Rtc};

use session::{Recording, Settings};
use stats::Row;

const DEFAULT_ADDR: &str = "0.0.0.0:8090";
/// Free in str0m's default extension map (ext::ID is 12).
const VIDEO_TIMING_ID: u8 = 9;

macro_rules! log {
    ($($arg:tt)*) => { eprintln!("[relay] {}", format!($($arg)*)) };
}

fn env_or(name: &str, default: &str) -> String {
    std::env::var(name)
        .ok()
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| default.to_string())
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Role {
    Publisher,
    Subscriber,
}

/// What the HTTP thread hands the loop.
enum Control {
    Add {
        id: u64,
        role: Role,
        stream: String,
        // Boxed: an Rtc is large, and the channel carries one per client.
        rtc: Box<Rtc>,
    },
    Remove(u64),
}

struct Client {
    id: u64,
    role: Role,
    stream: String,
    rtc: Box<Rtc>,
    /// Subscriber: the send stream per media kind it receives.
    tx: HashMap<MediaKind, (Mid, Ssrc)>,
    /// Publisher: the last receive SSRC per media kind, for keyframe requests.
    rx: HashMap<MediaKind, Ssrc>,
}

impl Client {
    /// Who a stats row is about: `publisher-1`, `subscriber-2`.
    fn label(&self) -> String {
        let role = match self.role {
            Role::Publisher => "publisher",
            Role::Subscriber => "subscriber",
        };
        format!("{role}-{}", self.id)
    }
}

/// A stats event of one client's stream, with what the row needs of it.
enum StreamStats {
    In(MediaIngressStats),
    Out(MediaEgressStats),
}

struct Stat {
    peer: String,
    kind: MediaKind,
    ssrc: Option<u32>,
    stats: StreamStats,
}

/// A publisher's packet on its way to the stream's subscribers.
struct Forward {
    stream: String,
    kind: MediaKind,
    spec: Option<CodecSpec>,
    pkt: RtpPacket,
}

fn main() {
    // str0m's diagnostics, when RUST_LOG asks for them (e.g. RUST_LOG=str0m=debug).
    if std::env::var_os("RUST_LOG").is_some() {
        tracing_subscriber::fmt()
            .with_env_filter(tracing_subscriber::EnvFilter::from_default_env())
            .with_writer(std::io::stderr)
            .init();
    }
    str0m::crypto::from_feature_flags().install_process_default();

    let http: SocketAddr = env_or("RELAY_HTTP", DEFAULT_ADDR)
        .parse()
        .expect("RELAY_HTTP: an address");
    let udp: SocketAddr = env_or("RELAY_UDP", DEFAULT_ADDR)
        .parse()
        .expect("RELAY_UDP: an address");
    let public_ip: IpAddr = match std::env::var("RELAY_PUBLIC_IP")
        .ok()
        .filter(|v| !v.is_empty())
    {
        Some(ip) => ip.parse().expect("RELAY_PUBLIC_IP: an IP address"),
        None if !udp.ip().is_unspecified() => udp.ip(),
        None => {
            log!("ERROR: set RELAY_PUBLIC_IP: the address clients reach {udp} at");
            std::process::exit(2);
        }
    };

    let socket = UdpSocket::bind(udp).expect("binding the media socket");
    let candidate_addr = SocketAddr::new(public_ip, socket.local_addr().unwrap().port());

    let stats_ms: u64 = env_or("RELAY_STATS_INTERVAL_MS", "1000")
        .parse()
        .expect("RELAY_STATS_INTERVAL_MS: milliseconds");
    let stats_interval = (stats_ms > 0).then(|| Duration::from_millis(stats_ms));
    let settings = Settings {
        runs_dir: env_or("RUNS_DIR", "runs").into(),
        logging_url: std::env::var("LOGGING_URL").ok().filter(|v| !v.is_empty()),
        ptp_device: std::env::var("PTP_DEVICE").ok().filter(|v| !v.is_empty()),
        stats_interval,
    };

    let stop = Arc::new(AtomicBool::new(false));
    {
        let stop = stop.clone();
        ctrlc::set_handler(move || stop.store(true, Ordering::SeqCst)).expect("a signal handler");
    }

    // Under the admin the relay records the runs it is told to; standalone,
    // RUN_ID from start to exit.
    let admin = std::env::var("ADMIN_URL")
        .ok()
        .filter(|v| !v.is_empty())
        .map(|url| {
            let cfg = AdminConfig::new(url.clone(), "relay", hostname(), 0);
            log!("admin at {url}, as {}", cfg.node_id);
            admin::spawn(cfg, stop.clone())
        });
    let recording = match admin {
        Some(_) => None,
        None => Some(Recording::open(&settings, &env_or("RUN_ID", "dev-run"))),
    };

    let (tx, rx) = mpsc::channel();
    let server = tiny_http::Server::http(http).expect("binding the WHIP/WHEP address");
    log!("WHIP http://{http}/whip/<stream>, WHEP http://{http}/whep/<stream>; media {candidate_addr}/udp");
    thread::spawn(move || serve(server, candidate_addr, stats_interval, tx));

    let node = Node {
        settings,
        recording,
        admin,
        public_addr: candidate_addr,
        frames_forwarded: 0,
        unclaimed_total: 0,
    };
    run(socket, candidate_addr, rx, node, &stop);
}

/// This machine's name, for the stable node id the admin addresses: the
/// short name, as the clients on the same host report it.
fn hostname() -> String {
    full_hostname()
        .split('.')
        .next()
        .unwrap_or_default()
        .to_string()
}

fn full_hostname() -> String {
    std::fs::read_to_string("/etc/hostname")
        .ok()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .or_else(|| std::env::var("HOSTNAME").ok().filter(|s| !s.is_empty()))
        .or_else(|| {
            std::process::Command::new("hostname")
                .output()
                .ok()
                .and_then(|o| String::from_utf8(o.stdout).ok())
                .map(|s| s.trim().to_string())
        })
        .unwrap_or_else(|| "unknown".into())
}

/// What the loop keeps besides its clients: the run being recorded, if any,
/// and the admin connection, if any.
struct Node {
    settings: Settings,
    recording: Option<Recording>,
    admin: Option<(AdminHandle, Receiver<Command>)>,
    public_addr: SocketAddr,
    /// Video frames forwarded since start, recorded or not: what tells the
    /// admin the relay is passing video (WF_MEDIA_NO_FRAMES).
    frames_forwarded: u64,
    unclaimed_total: u64,
}

impl Node {
    /// Follow the admin's commands. A start opens the run's recording
    /// (closing any other); a stop closes it and reports.
    fn follow_admin(&mut self, clients: &[Client]) {
        let Some((_, commands)) = &self.admin else {
            return;
        };
        let mut pending = Vec::new();
        while let Ok(c) = commands.try_recv() {
            pending.push(c);
        }
        for command in pending {
            match command {
                Command::Start { run_id } => {
                    if self.recording.as_ref().map(|r| r.run_id.as_str()) == Some(run_id.as_str()) {
                        continue;
                    }
                    if let Some(previous) = self.recording.take() {
                        previous.close();
                    }
                    self.recording = Some(Recording::open(&self.settings, &run_id));
                    if let Some((h, _)) = &self.admin {
                        h.set_identity("running", Some(&run_id));
                    }
                    self.status(clients, json!({}));
                }
                Command::Stop => {
                    let report = self.recording.take().map(Recording::close);
                    if let Some((h, _)) = &self.admin {
                        h.set_identity("idle", None);
                    }
                    self.status(clients, report.unwrap_or_else(|| json!({})));
                }
            }
        }
    }

    fn status(&self, clients: &[Client], report: Value) {
        let Some((handle, _)) = &self.admin else {
            return;
        };
        let mut streams: HashMap<&str, (u32, u32)> = HashMap::new();
        for c in clients {
            let e = streams.entry(c.stream.as_str()).or_default();
            match c.role {
                Role::Publisher => e.0 += 1,
                Role::Subscriber => e.1 += 1,
            }
        }
        let streams: serde_json::Map<String, Value> = streams
            .into_iter()
            .map(|(name, (p, s))| (name.to_string(), json!({"publishers": p, "subscribers": s})))
            .collect();
        let peers: Vec<Value> = clients
            .iter()
            .map(|c| {
                json!({
                    "peer_id": c.label(),
                    "detail": {"stream": c.stream, "connected": c.rtc.is_connected()},
                })
            })
            .collect();
        let (state, run_id) = match &self.recording {
            Some(r) => ("running", Some(r.run_id.as_str())),
            None => ("idle", None),
        };
        let recorded = self
            .recording
            .as_ref()
            .map_or(0, |r| r.frames.frames_recorded);
        handle.status(admin::status_payload(
            "relay",
            state,
            run_id,
            json!({
                "public_addr": self.public_addr.to_string(),
                "streams": streams,
                "out_leaf": "relay",
            }),
            peers,
            json!({
                "frames": self.frames_forwarded,
                "frames_recorded": recorded,
                "clients": clients.len(),
                "unclaimed": self.unclaimed_total,
            }),
            report,
        ));
    }

    fn shutdown(mut self) {
        let report = self.recording.take().map(Recording::close);
        if let Some((handle, _)) = self.admin.take() {
            handle.goodbye(None, report.unwrap_or_else(|| json!({})));
            // Give the socket thread a moment to send it.
            thread::sleep(Duration::from_millis(200));
            handle.shutdown();
        }
    }
}

// --- WHIP / WHEP -------------------------------------------------------------

fn serve(
    server: tiny_http::Server,
    candidate: SocketAddr,
    stats_interval: Option<Duration>,
    tx: Sender<Control>,
) {
    let next_id = AtomicU64::new(1);
    for mut request in server.incoming_requests() {
        let method = request.method().as_str().to_string();
        let path = request.url().split('?').next().unwrap_or("").to_string();
        let parts: Vec<&str> = path.trim_matches('/').split('/').collect();
        let response = match (method.as_str(), parts.as_slice()) {
            ("POST", [verb @ ("whip" | "whep"), stream]) => {
                let role = if *verb == "whip" {
                    Role::Publisher
                } else {
                    Role::Subscriber
                };
                let mut body = String::new();
                let _ = request.as_reader().read_to_string(&mut body);
                let id = next_id.fetch_add(1, Ordering::SeqCst);
                match answer(&body, candidate, stats_interval) {
                    Ok((rtc, sdp)) => {
                        let _ = tx.send(Control::Add {
                            id,
                            role,
                            stream: stream.to_string(),
                            rtc: Box::new(rtc),
                        });
                        log!("{verb} {stream}: client {id}");
                        text(201, sdp, "application/sdp")
                            .with_header(header("Location", &format!("/resource/{id}")))
                    }
                    Err(e) => {
                        log!("{verb} {stream}: refused: {e}");
                        text(400, e, "text/plain")
                    }
                }
            }
            ("DELETE", ["resource", id]) => match id.parse() {
                Ok(id) => {
                    let _ = tx.send(Control::Remove(id));
                    text(200, String::new(), "text/plain")
                }
                Err(_) => text(400, "bad resource id".into(), "text/plain"),
            },
            ("GET", ["health"]) => text(200, "ok\n".into(), "text/plain"),
            _ => text(404, "not found\n".into(), "text/plain"),
        };
        let _ = request.respond(response);
    }
}

fn header(name: &str, value: &str) -> tiny_http::Header {
    tiny_http::Header::from_bytes(name.as_bytes(), value.as_bytes()).expect("a valid header")
}

fn text(
    status: u16,
    body: String,
    content_type: &str,
) -> tiny_http::Response<std::io::Cursor<Vec<u8>>> {
    tiny_http::Response::from_string(body)
        .with_status_code(status)
        .with_header(header("Content-Type", content_type))
}

/// An Rtc for one client, and the SDP answer to its offer.
fn answer(
    offer: &str,
    candidate: SocketAddr,
    stats_interval: Option<Duration>,
) -> Result<(Rtc, String), String> {
    let offer = SdpOffer::from_sdp_string(offer).map_err(|e| format!("not an SDP offer: {e}"))?;
    let mut rtc = Rtc::builder()
        .set_ice_lite(true)
        .set_rtp_mode(true)
        .set_extension(ext::ID, ext::extension())
        .set_extension(VIDEO_TIMING_ID, Extension::VideoTiming)
        .set_stats_interval(stats_interval)
        .build(Instant::now());
    let c = Candidate::host(candidate, "udp").map_err(|e| format!("candidate: {e}"))?;
    rtc.add_local_candidate(c);
    let answer = rtc
        .sdp_api()
        .accept_offer(offer)
        .map_err(|e| format!("offer refused: {e}"))?;
    Ok((rtc, answer.to_sdp_string()))
}

// --- the loop ----------------------------------------------------------------

fn run(
    socket: UdpSocket,
    local: SocketAddr,
    rx: Receiver<Control>,
    mut node: Node,
    stop: &AtomicBool,
) {
    let mut last_status = Instant::now() - Duration::from_secs(10);
    let mut clients: Vec<Client> = Vec::new();
    let mut buf = vec![0u8; 2000];
    // Datagrams no client claimed: reported every few seconds, since a
    // client that sends media no session recognises shows up nowhere else.
    let mut unclaimed: u64 = 0;
    let mut unclaimed_report = Instant::now();
    // Every datagram is handed to str0m as arriving at the candidate's
    // address: a socket bound to 0.0.0.0 reports that as its own, which
    // matches no candidate, and str0m would drop every STUN check.

    while !stop.load(Ordering::SeqCst) {
        loop {
            match rx.try_recv() {
                Ok(Control::Add {
                    id,
                    role,
                    stream,
                    rtc,
                }) => clients.push(Client {
                    id,
                    role,
                    stream,
                    rtc,
                    tx: HashMap::new(),
                    rx: HashMap::new(),
                }),
                Ok(Control::Remove(id)) => {
                    if let Some(c) = clients.iter_mut().find(|c| c.id == id) {
                        c.rtc.disconnect();
                    }
                }
                Err(TryRecvError::Empty) => break,
                Err(TryRecvError::Disconnected) => {
                    node.shutdown();
                    return;
                }
            }
        }
        node.follow_admin(&clients);
        if last_status.elapsed() >= Duration::from_secs(2) {
            last_status = Instant::now();
            node.status(&clients, json!({}));
        }
        clients.retain(|c| {
            let alive = c.rtc.is_alive();
            if !alive {
                log!("client {} ({:?} {}) gone", c.id, c.role, c.stream);
            }
            alive
        });

        // Drain every client's output: packets to send, events to act on.
        let mut forwards = Vec::new();
        let mut keyframes: Vec<(String, MediaKind, KeyframeRequestKind)> = Vec::new();
        let mut stat_events = Vec::new();
        let mut timeout = Instant::now() + Duration::from_millis(100);
        for c in clients.iter_mut() {
            timeout = timeout.min(drain(
                c,
                &socket,
                &mut forwards,
                &mut keyframes,
                &mut stat_events,
            ));
        }
        if let Some(rec) = node.recording.as_mut() {
            if let Some(log) = rec.stats.as_mut() {
                for s in stat_events {
                    let row = match &s.stats {
                        StreamStats::In(i) => Row::ingress(
                            rec.frames.realtime_ns(i.timestamp),
                            &s.peer,
                            s.kind,
                            s.ssrc,
                            i,
                        ),
                        StreamStats::Out(o) => Row::egress(
                            rec.frames.realtime_ns(o.timestamp),
                            &s.peer,
                            s.kind,
                            s.ssrc,
                            o,
                        ),
                    };
                    log.record(row);
                }
                log.maybe_flush(Instant::now());
            }
        }

        for f in forwards {
            let forwarded = forward(&f, &mut clients, node.recording.as_mut());
            if forwarded && f.kind == MediaKind::Video && f.pkt.header.marker {
                node.frames_forwarded += 1;
            }
        }
        for (stream, kind, req) in keyframes {
            for c in clients
                .iter_mut()
                .filter(|c| c.role == Role::Publisher && c.stream == stream)
            {
                if let Some(ssrc) = c.rx.get(&kind).copied() {
                    if let Some(s) = c.rtc.direct_api().stream_rx(&ssrc) {
                        s.request_keyframe(req);
                    }
                }
            }
        }

        let wait = timeout
            .saturating_duration_since(Instant::now())
            .max(Duration::from_millis(1));
        socket.set_read_timeout(Some(wait)).expect("a read timeout");
        buf.resize(2000, 0);
        match socket.recv_from(&mut buf) {
            Ok((n, source)) => {
                let now = Instant::now();
                if let Ok(contents) = buf[..n].try_into() {
                    let input = Input::Receive(
                        now,
                        Receive {
                            proto: Protocol::Udp,
                            source,
                            destination: local,
                            contents,
                        },
                    );
                    if let Some(c) = clients.iter_mut().find(|c| c.rtc.accepts(&input)) {
                        let _ = c.rtc.handle_input(input);
                    } else {
                        unclaimed += 1;
                        node.unclaimed_total += 1;
                    }
                }
            }
            Err(e) if matches!(e.kind(), ErrorKind::WouldBlock | ErrorKind::TimedOut) => {}
            Err(e) => log!("socket: {e}"),
        }
        let now = Instant::now();
        for c in clients.iter_mut() {
            let _ = c.rtc.handle_input(Input::Timeout(now));
        }
        if unclaimed > 0 && now.duration_since(unclaimed_report) > Duration::from_secs(5) {
            log!("{unclaimed} datagrams claimed by no client");
            unclaimed = 0;
            unclaimed_report = now;
        }
    }
    log!("stopping, {} video frames forwarded", node.frames_forwarded);
    node.shutdown();
}

/// Poll a client until it waits; returns when it next needs a timeout.
fn drain(
    c: &mut Client,
    socket: &UdpSocket,
    forwards: &mut Vec<Forward>,
    keyframes: &mut Vec<(String, MediaKind, KeyframeRequestKind)>,
    stats: &mut Vec<Stat>,
) -> Instant {
    loop {
        let out = match c.rtc.poll_output() {
            Ok(o) => o,
            Err(e) => {
                log!("client {}: {e}", c.id);
                c.rtc.disconnect();
                return Instant::now();
            }
        };
        match out {
            Output::Timeout(t) => return t,
            Output::Transmit(t) => {
                let _ = socket.send_to(&t.contents, t.destination);
            }
            Output::Event(e) => match e {
                Event::IceConnectionStateChange(s) => {
                    log!("client {} ({:?}): ICE {s:?}", c.id, c.role)
                }
                Event::MediaAdded(m) => {
                    // A subscriber receives. Accepting its offer already
                    // created a send stream per media and announced its SSRC
                    // in the answer: forward on that one, so the receiver
                    // maps the packets by SSRC even once they stop carrying
                    // the mid. A stream of our own would be unannounced.
                    if c.role == Role::Subscriber && m.direction == Direction::SendOnly {
                        let ssrc = match c.rtc.direct_api().stream_tx_by_mid(m.mid, None) {
                            Some(s) => s.ssrc(),
                            None => {
                                let ssrc = Ssrc::new();
                                c.rtc
                                    .direct_api()
                                    .declare_stream_tx(ssrc, None, m.mid, None);
                                ssrc
                            }
                        };
                        c.tx.insert(m.kind, (m.mid, ssrc));
                    }
                }
                Event::RtpPacket(pkt) if c.role == Role::Publisher => {
                    // The mid travels in a header extension only until the
                    // receiver has acknowledged it; after that the SSRC's
                    // receive stream knows it.
                    let ssrc = pkt.header.ssrc;
                    let mid = pkt
                        .header
                        .ext_vals
                        .mid
                        .or_else(|| c.rtc.direct_api().stream_rx(&ssrc).map(|s| s.mid()));
                    let Some(kind) = mid.and_then(|m| c.rtc.media(m)).map(|m| m.kind()) else {
                        continue;
                    };
                    c.rx.insert(kind, pkt.header.ssrc);
                    let pt = pkt.header.payload_type;
                    let spec = c
                        .rtc
                        .codec_config()
                        .params()
                        .iter()
                        .find(|p| p.pt() == pt)
                        .map(|p| p.spec());
                    forwards.push(Forward {
                        stream: c.stream.clone(),
                        kind,
                        spec,
                        pkt,
                    });
                }
                Event::KeyframeRequest(req) if c.role == Role::Subscriber => {
                    let kind = c
                        .rtc
                        .media(req.mid)
                        .map(|m| m.kind())
                        .unwrap_or(MediaKind::Video);
                    keyframes.push((c.stream.clone(), kind, req.kind));
                }
                Event::MediaIngressStats(s) => {
                    let kind = c
                        .rtc
                        .media(s.mid)
                        .map(|m| m.kind())
                        .unwrap_or(MediaKind::Video);
                    stats.push(Stat {
                        peer: c.label(),
                        kind,
                        ssrc: c.rx.get(&kind).map(|s| **s),
                        stats: StreamStats::In(s),
                    });
                }
                Event::MediaEgressStats(s) => {
                    let kind = c
                        .rtc
                        .media(s.mid)
                        .map(|m| m.kind())
                        .unwrap_or(MediaKind::Video);
                    stats.push(Stat {
                        peer: c.label(),
                        kind,
                        ssrc: c.tx.get(&kind).map(|(_, s)| **s),
                        stats: StreamStats::Out(s),
                    });
                }
                _ => {}
            },
        }
    }
}

/// Write one publisher packet to every subscriber of its stream, and record
/// it when a run is being recorded. Whether any subscriber took it.
fn forward(f: &Forward, clients: &mut [Client], recording: Option<&mut Recording>) -> bool {
    let mut forwarded = false;
    for c in clients
        .iter_mut()
        .filter(|c| c.role == Role::Subscriber && c.stream == f.stream)
    {
        let Some((mid, ssrc)) = c.tx.get(&f.kind).copied() else {
            continue;
        };
        // The subscriber's payload type for the same codec.
        let Some(spec) = f.spec.as_ref() else {
            continue;
        };
        let Some(pt) = c
            .rtc
            .codec_config()
            .params()
            .iter()
            .find(|p| p.spec() == *spec)
            .or_else(|| {
                c.rtc
                    .codec_config()
                    .params()
                    .iter()
                    .find(|p| p.spec().codec == spec.codec)
            })
            .map(|p| p.pt())
        else {
            continue;
        };
        let mut ext_vals = f.pkt.header.ext_vals.clone();
        ext_vals.mid = Some(mid);
        ext_vals.rid = None;
        let mut direct = c.rtc.direct_api();
        let Some(stream) = direct.stream_tx(&ssrc) else {
            continue;
        };
        stream.write_rtp(
            RtpWrite::new(
                pt,
                f.pkt.seq_no,
                f.pkt.header.timestamp,
                f.pkt.timestamp,
                f.pkt.payload.clone(),
            )
            .marker(f.pkt.header.marker)
            .ext_vals(ext_vals),
        );
        forwarded = true;
    }
    if let (MediaKind::Video, Some(rec)) = (f.kind, recording) {
        let frames = &mut rec.frames;
        let done = if forwarded {
            frames.realtime_ns(Instant::now())
        } else {
            0
        };
        frames.packet(
            *f.pkt.header.ssrc,
            f.pkt.header.timestamp,
            f.pkt.header.marker,
            f.pkt.timestamp,
            done,
            f.pkt.payload.len(),
            f.pkt
                .header
                .ext_vals
                .user_values
                .get::<ext::SendTimes>()
                .copied(),
        );
    }
    forwarded
}
