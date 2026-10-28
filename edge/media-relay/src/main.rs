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
//!   RUN_ID, RUNS_DIR, LOGGING_URL   as every mec-cast node; samples go to
//!                    <RUNS_DIR>/<RUN_ID>/relay/samples.csv
//!   PTP_DEVICE       PTP hardware clock to monitor (Linux; default none)
//!
//! Synchronous throughout, like the rest of mec-cast's Rust (ADR-0007): the
//! HTTP server on its own thread, everything WebRTC on one loop thread.

mod ext;
mod record;

use std::collections::HashMap;
use std::io::ErrorKind;
use std::net::{IpAddr, SocketAddr, UdpSocket};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::mpsc::{self, Receiver, Sender, TryRecvError};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use mec_cast_telemetry::{monitor_from_device, spawn_recorder, RecorderConfig};
use str0m::change::SdpOffer;
use str0m::format::CodecSpec;
use str0m::media::{Direction, KeyframeRequestKind, MediaKind, Mid};
use str0m::net::{Protocol, Receive};
use str0m::rtp::{Extension, RtpPacket, RtpWrite, Ssrc};
use str0m::{Candidate, Event, Input, Output, Rtc};

use record::FrameRecorder;

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

    // The recorder: the same CSV and snapshots as every other node.
    let run_id = env_or("RUN_ID", "dev-run");
    let out_dir = std::path::Path::new(&env_or("RUNS_DIR", "runs"))
        .join(&run_id)
        .join("relay");
    let mut cfg = RecorderConfig::new(&run_id, "mec-cast-relay", &out_dir);
    cfg.logging_url = std::env::var("LOGGING_URL").ok().filter(|v| !v.is_empty());
    let ptp_device = std::env::var("PTP_DEVICE").ok();
    let (ptp, ptp_ok, ptp_why) = monitor_from_device(ptp_device.as_deref());
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
    log!("run {run_id}: samples to {}", out_dir.display());

    let stop = Arc::new(AtomicBool::new(false));
    {
        let stop = stop.clone();
        ctrlc::set_handler(move || stop.store(true, Ordering::SeqCst)).expect("a signal handler");
    }

    let (tx, rx) = mpsc::channel();
    let server = tiny_http::Server::http(http).expect("binding the WHIP/WHEP address");
    log!("WHIP http://{http}/whip/<stream>, WHEP http://{http}/whep/<stream>; media {candidate_addr}/udp");
    thread::spawn(move || serve(server, candidate_addr, tx));

    let recorder_out = FrameRecorder::new(samples, record::trace_id(&run_id));
    run(socket, candidate_addr, rx, recorder_out, &stop);

    if let Some(h) = recorder {
        let r = h.shutdown();
        log!(
            "stopped: {} samples written, {} dropped, {} snapshots posted",
            r.samples_written,
            r.samples_dropped,
            r.snapshots_posted
        );
    }
}

// --- WHIP / WHEP -------------------------------------------------------------

fn serve(server: tiny_http::Server, candidate: SocketAddr, tx: Sender<Control>) {
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
                match answer(&body, candidate) {
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
fn answer(offer: &str, candidate: SocketAddr) -> Result<(Rtc, String), String> {
    let offer = SdpOffer::from_sdp_string(offer).map_err(|e| format!("not an SDP offer: {e}"))?;
    let mut rtc = Rtc::builder()
        .set_ice_lite(true)
        .set_rtp_mode(true)
        .set_extension(ext::ID, ext::extension())
        .set_extension(VIDEO_TIMING_ID, Extension::VideoTiming)
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
    mut frames: FrameRecorder,
    stop: &AtomicBool,
) {
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
                Err(TryRecvError::Disconnected) => return,
            }
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
        let mut timeout = Instant::now() + Duration::from_millis(100);
        for c in clients.iter_mut() {
            timeout = timeout.min(drain(c, &socket, &mut forwards, &mut keyframes));
        }

        for f in forwards {
            forward(&f, &mut clients, &mut frames);
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
    log!("stopping, {} frames recorded", frames.frames_recorded);
}

/// Poll a client until it waits; returns when it next needs a timeout.
fn drain(
    c: &mut Client,
    socket: &UdpSocket,
    forwards: &mut Vec<Forward>,
    keyframes: &mut Vec<(String, MediaKind, KeyframeRequestKind)>,
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
                _ => {}
            },
        }
    }
}

/// Write one publisher packet to every subscriber of its stream.
fn forward(f: &Forward, clients: &mut [Client], frames: &mut FrameRecorder) {
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
    if f.kind == MediaKind::Video {
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
}
