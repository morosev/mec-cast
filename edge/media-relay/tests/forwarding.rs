//! The relay end to end, over localhost: a str0m publisher (WHIP) and a str0m
//! subscriber (WHEP) talk to the real binary. Asserted:
//!
//! - every packet the publisher sends reaches the subscriber with its payload,
//!   and the mec-cast header extension -- the sender's capture and send
//!   stamps -- unchanged;
//! - the relay records the frames at site 3, with those stamps;
//! - and the streams' WebRTC stats, both legs, in webrtc.csv.
//!
//! No libwebrtc is involved, so this runs in plain `cargo test`. The real
//! clients are covered where libwebrtc is.

use std::io::{BufRead, BufReader, Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream, UdpSocket};
use std::process::{Child, Command};
use std::sync::{mpsc, Arc};
use std::time::{Duration, Instant};

use str0m::change::{SdpAnswer, SdpOffer};
use str0m::format::Codec;
use str0m::media::{Direction, MediaKind, Mid};
use str0m::net::{Protocol, Receive};
use str0m::rtp::{Extension, ExtensionSerializer, ExtensionValues, RtpWrite, Ssrc};
use str0m::{Candidate, Event, Input, Output, Rtc};

const URI: &str = "http://www.mec-cast.org/experiments/rtp-hdrext/send-timestamp-ns";
const FRAMES: u32 = 20;
const PACKETS_PER_FRAME: u32 = 3;

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
struct SendTimes(u64, u64);

#[derive(Debug)]
struct Ser;
impl ExtensionSerializer for Ser {
    fn write_to(&self, buf: &mut [u8], ev: &ExtensionValues) -> usize {
        let Some(t) = ev.user_values.get::<SendTimes>() else {
            return 0;
        };
        buf[0..8].copy_from_slice(&t.0.to_be_bytes());
        buf[8..16].copy_from_slice(&t.1.to_be_bytes());
        16
    }
    fn parse_value(&self, buf: &[u8], ev: &mut ExtensionValues) -> bool {
        if buf.len() < 16 {
            return false;
        }
        let a = u64::from_be_bytes(buf[0..8].try_into().unwrap());
        let b = u64::from_be_bytes(buf[8..16].try_into().unwrap());
        ev.user_values.set(SendTimes(a, b));
        true
    }
    fn is_video(&self) -> bool {
        true
    }
    fn is_audio(&self) -> bool {
        false
    }
}

fn free_port() -> u16 {
    TcpListener::bind("127.0.0.1:0")
        .unwrap()
        .local_addr()
        .unwrap()
        .port()
}

struct Relay {
    child: Child,
    port: u16,
    runs: std::path::PathBuf,
}

impl Relay {
    fn start() -> Self {
        Self::start_with(&[("RUN_ID", "relay-test")])
    }

    fn start_with(env: &[(&str, &str)]) -> Self {
        let port = free_port();
        let runs = std::env::temp_dir().join(format!("mec-cast-relay-test-{port}"));
        let _ = std::fs::remove_dir_all(&runs);
        let child = Command::new(env!("CARGO_BIN_EXE_mec-cast-relay"))
            .env("RELAY_HTTP", format!("127.0.0.1:{port}"))
            // As the lab runs it: media on every address, one candidate.
            .env("RELAY_UDP", format!("0.0.0.0:{port}"))
            .env("RELAY_PUBLIC_IP", "127.0.0.1")
            .env("RUNS_DIR", &runs)
            .env("RELAY_STATS_INTERVAL_MS", "100")
            .env_remove("LOGGING_URL")
            .env_remove("ADMIN_URL")
            .env_remove("RUN_ID")
            .envs(env.iter().copied())
            .spawn()
            .expect("the relay binary");
        let relay = Relay { child, port, runs };
        let deadline = Instant::now() + Duration::from_secs(10);
        while TcpStream::connect(("127.0.0.1", port)).is_err() {
            assert!(Instant::now() < deadline, "the relay never listened");
            std::thread::sleep(Duration::from_millis(50));
        }
        relay
    }

    /// Stop it as Ctrl-C would, so it flushes its samples.
    fn stop(&mut self) {
        let _ = Command::new("kill")
            .args(["-INT", &self.child.id().to_string()])
            .status();
        let _ = self.child.wait();
    }
}

impl Drop for Relay {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = std::fs::remove_dir_all(&self.runs);
    }
}

/// POST an SDP offer; the SDP answer back.
fn post_offer(port: u16, path: &str, sdp: &str) -> String {
    let mut s = TcpStream::connect(("127.0.0.1", port)).unwrap();
    write!(
        s,
        "POST {path} HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/sdp\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{sdp}",
        sdp.len()
    )
    .unwrap();
    let mut r = BufReader::new(s);
    let mut status = String::new();
    r.read_line(&mut status).unwrap();
    assert!(status.contains(" 201 "), "{path}: {status}");
    let mut len = 0usize;
    loop {
        let mut line = String::new();
        r.read_line(&mut line).unwrap();
        if line == "\r\n" {
            break;
        }
        if let Some(v) = line.to_ascii_lowercase().strip_prefix("content-length:") {
            len = v.trim().parse().unwrap();
        }
    }
    let mut body = vec![0u8; len];
    r.read_exact(&mut body).unwrap();
    String::from_utf8(body).unwrap()
}

struct Peer {
    rtc: Rtc,
    socket: UdpSocket,
    mid: Mid,
    connected: bool,
    received: Vec<(u32, Vec<u8>, Option<SendTimes>)>,
}

impl Peer {
    fn new(port: u16, path: &str, dir: Direction) -> Peer {
        let socket = UdpSocket::bind("127.0.0.1:0").unwrap();
        let mut rtc = Rtc::builder()
            .set_rtp_mode(true)
            .set_extension(12, Extension::UnknownUri(URI.into(), Arc::new(Ser)))
            .build(Instant::now());
        rtc.add_local_candidate(Candidate::host(socket.local_addr().unwrap(), "udp").unwrap());
        let mut api = rtc.sdp_api();
        let mid = api.add_media(MediaKind::Video, dir, None, None, None);
        let (offer, pending): (SdpOffer, _) = api.apply().unwrap();
        let answer = post_offer(port, path, &offer.to_sdp_string());
        rtc.sdp_api()
            .accept_answer(pending, SdpAnswer::from_sdp_string(&answer).unwrap())
            .unwrap();
        socket
            .set_read_timeout(Some(Duration::from_millis(2)))
            .unwrap();
        Peer {
            rtc,
            socket,
            mid,
            connected: false,
            received: Vec::new(),
        }
    }

    fn pump(&mut self) {
        loop {
            match self.rtc.poll_output().unwrap() {
                Output::Timeout(_) => break,
                Output::Transmit(t) => {
                    let _ = self.socket.send_to(&t.contents, t.destination);
                }
                Output::Event(Event::Connected) => self.connected = true,
                Output::Event(Event::RtpPacket(p)) => self.received.push((
                    p.header.timestamp,
                    p.payload.to_vec(),
                    p.header.ext_vals.user_values.get::<SendTimes>().copied(),
                )),
                Output::Event(_) => {}
            }
        }
        let mut buf = [0u8; 2000];
        if let Ok((n, source)) = self.socket.recv_from(&mut buf) {
            if let Ok(contents) = buf[..n].try_into() {
                let local: SocketAddr = self.socket.local_addr().unwrap();
                let _ = self.rtc.handle_input(Input::Receive(
                    Instant::now(),
                    Receive {
                        proto: Protocol::Udp,
                        source,
                        destination: local,
                        contents,
                    },
                ));
            }
        }
        let _ = self.rtc.handle_input(Input::Timeout(Instant::now()));
    }
}

/// Send `count` frames of PACKETS_PER_FRAME packets from the publisher,
/// frame numbers from `first`, each carrying its send stamps, and pump until
/// the subscriber has every packet. What was sent.
fn send_frames(
    publisher: &mut Peer,
    subscriber: &mut Peer,
    first: u32,
    count: u32,
) -> Vec<(u32, Vec<u8>, Option<SendTimes>)> {
    // The send stream the offer announced, as a real sender uses.
    let mid = publisher.mid;
    let ssrc: Ssrc = publisher
        .rtc
        .direct_api()
        .stream_tx_by_mid(mid, None)
        .expect("the announced stream")
        .ssrc();
    let pt = publisher
        .rtc
        .codec_config()
        .params()
        .iter()
        .find(|p| p.spec().codec == Codec::Vp8)
        .expect("VP8 negotiated")
        .pt();

    let mut sent = Vec::new();
    let already = subscriber.received.len();
    for frame in first..first + count {
        let ts = 90_000 + frame * 3000;
        let times = SendTimes(
            1_000_000_000 + u64::from(frame),
            2_000_000_000 + u64::from(frame),
        );
        for k in 0..PACKETS_PER_FRAME {
            let payload: Vec<u8> = vec![frame as u8, k as u8, 0xAB, 0xCD];
            let mut ev = ExtensionValues::default();
            ev.user_values.set(times);
            let seq = u64::from(1000 + frame * PACKETS_PER_FRAME + k);
            publisher
                .rtc
                .direct_api()
                .stream_tx(&ssrc)
                .unwrap()
                .write_rtp(
                    RtpWrite::new(pt, seq.into(), ts, Instant::now(), payload.clone())
                        .marker(k + 1 == PACKETS_PER_FRAME)
                        .ext_vals(ev),
                );
            sent.push((ts, payload, Some(times)));
        }
        let want = already + sent.len();
        pump_until(
            &mut [&mut *publisher, &mut *subscriber],
            &format!("frame {frame} to pass ({want} packets)"),
            |p| p[1].received.len() >= want,
        );
    }
    sent
}

fn pump_until(peers: &mut [&mut Peer], what: &str, done: impl Fn(&[&mut Peer]) -> bool) {
    let deadline = Instant::now() + Duration::from_secs(15);
    while !done(peers) {
        assert!(Instant::now() < deadline, "timed out waiting for {what}");
        for p in peers.iter_mut() {
            p.pump();
        }
    }
}

#[test]
fn the_relay_forwards_packets_with_the_send_stamps_and_records_each_frame() {
    if std::env::var_os("RUST_LOG").is_some() {
        let _ = tracing_subscriber::fmt()
            .with_env_filter(tracing_subscriber::EnvFilter::from_default_env())
            .with_writer(std::io::stderr)
            .try_init();
    }
    str0m::crypto::from_feature_flags().install_process_default();
    let mut relay = Relay::start();

    let mut publisher = Peer::new(relay.port, "/whip/test", Direction::SendOnly);
    let mut subscriber = Peer::new(relay.port, "/whep/test", Direction::RecvOnly);
    pump_until(
        &mut [&mut publisher, &mut subscriber],
        "both peers to connect",
        |p| p.iter().all(|p| p.connected),
    );

    let sent = send_frames(&mut publisher, &mut subscriber, 0, FRAMES);

    let mut got = subscriber.received.clone();
    got.sort();
    let mut want = sent.clone();
    want.sort();
    assert_eq!(got, want, "payloads and send stamps arrive unchanged");

    // Long enough for a few stats intervals, and the receiver reports they
    // carry on the subscriber's leg.
    let settle = Instant::now() + Duration::from_millis(1500);
    while Instant::now() < settle {
        publisher.pump();
        subscriber.pump();
    }

    relay.stop();
    let csv = std::fs::read_to_string(relay.runs.join("relay-test/relay/samples.csv"))
        .expect("the relay's samples");
    let rows: Vec<&str> = csv.lines().skip(1).collect();
    assert_eq!(rows.len() as u32, FRAMES, "one sample per frame:\n{csv}");
    // seq,modality,kind,site,capture_ns,send_ns,...
    let first: Vec<&str> = rows[0].split(',').collect();
    assert_eq!(first[3], "3", "site 3 is the relay");
    assert_eq!(first[4], "1000000000", "capture_ns from the extension");
    assert_eq!(first[5], "2000000000", "send_ns from the extension");

    let stats = std::fs::read_to_string(relay.runs.join("relay-test/relay/webrtc.csv"))
        .expect("the relay's webrtc stats");
    let mut lines = stats.lines();
    let header: Vec<&str> = lines.next().unwrap().split(',').collect();
    let col = |name: &str| header.iter().position(|c| *c == name).unwrap();
    let rows: Vec<Vec<&str>> = lines.map(|l| l.split(',').collect()).collect();
    for (direction, peer) in [("in", "publisher-"), ("out", "subscriber-")] {
        let last = rows
            .iter()
            .rev()
            .find(|r| r[col("direction")] == direction)
            .unwrap_or_else(|| panic!("no {direction} row:\n{stats}"));
        assert!(last[col("peer")].starts_with(peer), "{direction}: {last:?}");
        assert_eq!(last[col("kind")], "video");
        assert_eq!(last[col("node")], "relay");
        let packets: u64 = last[col("packets")].parse().unwrap();
        assert_eq!(
            packets,
            u64::from(FRAMES * PACKETS_PER_FRAME),
            "{direction} packets"
        );
    }
}

// --- under the admin ---------------------------------------------------------

/// A WebSocket admin just capable enough to drive the relay: frames queued on
/// the returned sender go out, frames from the relay come back on the receiver.
fn stub_admin() -> (
    String,
    mpsc::Sender<String>,
    mpsc::Receiver<serde_json::Value>,
) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let url = format!("ws://{}", listener.local_addr().unwrap());
    let (out_tx, out_rx) = mpsc::channel::<String>();
    let (in_tx, in_rx) = mpsc::channel();
    std::thread::spawn(move || {
        let Ok((stream, _)) = listener.accept() else {
            return;
        };
        stream
            .set_read_timeout(Some(Duration::from_millis(20)))
            .unwrap();
        let Ok(mut socket) = tungstenite::accept(stream) else {
            return;
        };
        loop {
            while let Ok(frame) = out_rx.try_recv() {
                let _ = socket.send(tungstenite::Message::Text(frame));
            }
            match socket.read() {
                Ok(tungstenite::Message::Text(text)) => {
                    if let Ok(v) = serde_json::from_str(&text) {
                        if in_tx.send(v).is_err() {
                            return;
                        }
                    }
                }
                Ok(_) => {}
                Err(tungstenite::Error::Io(e))
                    if matches!(
                        e.kind(),
                        std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut
                    ) => {}
                Err(_) => return,
            }
        }
    });
    (url, out_tx, in_rx)
}

fn command(name: &str, run_id: &str) -> String {
    serde_json::json!({
        "v": 1, "type": "command", "msg_id": name, "ts_ns": 1, "node_id": null,
        "payload": {"command": name, "run_id": run_id, "args": {}},
    })
    .to_string()
}

/// The first frame from the relay that `pred` accepts, pumping the peers
/// meanwhile so their connections stay up.
fn await_frame(
    rx: &mpsc::Receiver<serde_json::Value>,
    peers: &mut [&mut Peer],
    what: &str,
    pred: impl Fn(&serde_json::Value) -> bool,
) -> serde_json::Value {
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        assert!(Instant::now() < deadline, "no {what} from the relay");
        while let Ok(v) = rx.try_recv() {
            if pred(&v) {
                return v;
            }
        }
        for p in peers.iter_mut() {
            p.pump();
        }
    }
}

#[test]
fn under_the_admin_the_relay_records_exactly_the_run_it_was_told_to() {
    str0m::crypto::from_feature_flags().install_process_default();
    let (url, to_relay, from_relay) = stub_admin();
    let mut relay = Relay::start_with(&[("ADMIN_URL", &url), ("RUN_ID", "ignored")]);

    let hello = await_frame(&from_relay, &mut [], "hello", |v| v["type"] == "hello");
    assert_eq!(hello["payload"]["node_type"], "relay");
    assert_eq!(hello["payload"]["state"], "idle");

    let mut publisher = Peer::new(relay.port, "/whip/test", Direction::SendOnly);
    let mut subscriber = Peer::new(relay.port, "/whep/test", Direction::RecvOnly);
    pump_until(
        &mut [&mut publisher, &mut subscriber],
        "both peers to connect",
        |p| p.iter().all(|p| p.connected),
    );

    // Before the run: forwarded, not recorded.
    send_frames(&mut publisher, &mut subscriber, 0, 5);

    to_relay.send(command("run.start", "admin-run")).unwrap();
    await_frame(
        &from_relay,
        &mut [&mut publisher, &mut subscriber],
        "a running status",
        |v| v["type"] == "status" && v["payload"]["run_id"] == "admin-run",
    );
    send_frames(&mut publisher, &mut subscriber, 5, FRAMES);

    to_relay.send(command("run.stop", "admin-run")).unwrap();
    let stopped = await_frame(
        &from_relay,
        &mut [&mut publisher, &mut subscriber],
        "the stop's report",
        |v| v["type"] == "status" && v["payload"]["report"]["frames"].is_u64(),
    );
    assert_eq!(stopped["payload"]["state"], "idle");
    assert_eq!(stopped["payload"]["report"]["frames"], FRAMES);
    // What the status says the relay is doing: its forwarding, both peers.
    assert_eq!(stopped["payload"]["counters"]["frames"], 5 + FRAMES);
    assert_eq!(stopped["payload"]["peers"].as_array().unwrap().len(), 2);

    relay.stop();
    let csv = std::fs::read_to_string(relay.runs.join("admin-run/relay/samples.csv"))
        .expect("the run's samples");
    assert_eq!(
        csv.lines().count() as u32,
        1 + FRAMES,
        "only the run's frames:\n{csv}"
    );
    assert!(
        relay.runs.join("admin-run/relay/webrtc.csv").exists(),
        "and its WebRTC stats"
    );
    assert!(
        !relay.runs.join("ignored").exists(),
        "RUN_ID is the admin's to name"
    );
}
