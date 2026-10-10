//! Where srsRAN's JSON metrics come from.
//!
//! srsRAN has exported the same JSON over two transports, depending on the
//! release:
//!
//! * **UDP** — `metrics: {addr, port}` in gnb.yml pushes one datagram per
//!   report. Releases up to 24.x.
//! * **WebSocket** — 25.04 dropped `addr/port`. With `metrics.enable_json`
//!   and a `remote_control` block, the gNB serves a WebSocket; a client sends
//!   `{"cmd":"metrics_subscribe"}` and receives every report as a text frame.
//!
//! Which one a lab gNB speaks is a property of its build, so it is
//! configuration, not code: `GNB_METRICS_SOURCE=udp|ws|auto`. `auto` listens
//! on both and locks onto whichever delivers first, so a gNB that somehow
//! sends on both still produces exactly one row per report.
//!
//! Every source is polled with a short timeout and returns `Ok(None)` when
//! nothing arrived, which is what lets the receive loops observe their stop
//! flag and the admin's commands — the same trick the UDP loop always used.

use std::net::UdpSocket;
use std::time::Duration;

use serde_json::{json, Value};

/// Default poll slice for a single source.
pub const POLL: Duration = Duration::from_millis(200);

/// srsRAN's documented `remote_control` port, on the gNB's own host.
pub const DEFAULT_WS: &str = "127.0.0.1:8001";

/// The transport a report actually arrived on.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Transport {
    Udp,
    Ws,
}

impl Transport {
    pub fn as_str(self) -> &'static str {
        match self {
            Transport::Udp => "udp",
            Transport::Ws => "ws",
        }
    }
}

/// One stream of metrics reports.
pub trait MetricsSource: Send {
    /// Wait up to one poll slice for a report. `Ok(None)` means nothing
    /// arrived; errors are fatal to the collector (a socket that cannot be
    /// read at all), never "the gNB is not there yet".
    fn next(&mut self) -> std::io::Result<Option<Vec<u8>>>;

    /// The transport delivering reports, once known.
    fn transport(&self) -> Option<Transport>;

    /// Configuration and live state, for the admin's status `params`.
    fn describe(&self) -> Value;
}

/// Which source the environment asked for.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SourceKind {
    Udp,
    Ws,
    Auto,
}

impl SourceKind {
    pub fn parse(raw: &str) -> Result<Self, String> {
        match raw.trim().to_ascii_lowercase().as_str() {
            "" | "auto" => Ok(SourceKind::Auto),
            "udp" => Ok(SourceKind::Udp),
            "ws" | "websocket" => Ok(SourceKind::Ws),
            other => Err(format!(
                "GNB_METRICS_SOURCE={other:?}: expected udp, ws or auto"
            )),
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            SourceKind::Udp => "udp",
            SourceKind::Ws => "ws",
            SourceKind::Auto => "auto",
        }
    }
}

/// Build the configured source.
///
/// `ws` is `host:port` or a `ws://` URL. A build without the `ws` feature
/// refuses `ws` outright and treats `auto` as UDP, saying so.
pub fn open(kind: SourceKind, udp_bind: &str, ws: &str) -> std::io::Result<Box<dyn MetricsSource>> {
    match kind {
        SourceKind::Udp => Ok(Box::new(UdpSource::bind(udp_bind, POLL)?)),
        #[cfg(feature = "ws")]
        SourceKind::Ws => Ok(Box::new(WsSource::new(ws, POLL))),
        #[cfg(feature = "ws")]
        SourceKind::Auto => Ok(Box::new(AutoSource::new(
            UdpSource::bind(udp_bind, AUTO_POLL)?,
            WsSource::new(ws, AUTO_POLL),
        ))),
        #[cfg(not(feature = "ws"))]
        SourceKind::Ws => {
            let _ = ws;
            Err(std::io::Error::new(
                std::io::ErrorKind::Unsupported,
                "GNB_METRICS_SOURCE=ws but this build has no `ws` feature",
            ))
        }
        #[cfg(not(feature = "ws"))]
        SourceKind::Auto => {
            let _ = ws;
            eprintln!("[ran-collector] built without `ws`: auto means udp only");
            Ok(Box::new(UdpSource::bind(udp_bind, POLL)?))
        }
    }
}

// --- UDP ------------------------------------------------------------------

pub struct UdpSource {
    socket: UdpSocket,
    bind: String,
    buf: Vec<u8>,
}

impl UdpSource {
    pub fn bind(addr: &str, poll: Duration) -> std::io::Result<Self> {
        let socket = UdpSocket::bind(addr)?;
        Self::from_socket(socket, poll)
    }

    /// The bound address, e.g. to send to a socket bound on port 0.
    pub fn describe_addr(&self) -> String {
        self.bind.clone()
    }

    /// Wrap an already-bound socket (tests bind port 0 and need the address).
    pub fn from_socket(socket: UdpSocket, poll: Duration) -> std::io::Result<Self> {
        socket.set_read_timeout(Some(poll))?;
        let bind = socket
            .local_addr()
            .map(|a| a.to_string())
            .unwrap_or_default();
        Ok(Self {
            socket,
            bind,
            buf: vec![0u8; 65536],
        })
    }
}

impl MetricsSource for UdpSource {
    fn next(&mut self) -> std::io::Result<Option<Vec<u8>>> {
        match self.socket.recv_from(&mut self.buf) {
            Ok((len, _peer)) => Ok(Some(self.buf[..len].to_vec())),
            Err(e)
                if e.kind() == std::io::ErrorKind::WouldBlock
                    || e.kind() == std::io::ErrorKind::TimedOut =>
            {
                Ok(None)
            }
            Err(e) => Err(e),
        }
    }

    fn transport(&self) -> Option<Transport> {
        Some(Transport::Udp)
    }

    fn describe(&self) -> Value {
        json!({"source": "udp", "metrics_transport": "udp", "udp": self.bind})
    }
}

// --- WebSocket ------------------------------------------------------------

#[cfg(feature = "ws")]
pub use ws::WsSource;

#[cfg(feature = "ws")]
mod ws {
    use std::net::{TcpStream, ToSocketAddrs};
    use std::time::{Duration, Instant};

    use serde_json::{json, Value};
    use tungstenite::stream::MaybeTlsStream;
    use tungstenite::{Message, WebSocket};

    use super::{MetricsSource, Transport};

    /// How long to wait between connection attempts. The gNB is routinely
    /// started after the collector, and restarted under it.
    const RETRY: Duration = Duration::from_secs(2);
    /// A TCP connect to a host that is down otherwise blocks for the
    /// kernel's SYN timeout — over a minute, with the stop flag unobserved.
    const CONNECT_TIMEOUT: Duration = Duration::from_secs(1);

    type Socket = WebSocket<MaybeTlsStream<TcpStream>>;

    /// Client of srsRAN's `remote_control` WebSocket.
    pub struct WsSource {
        url: String,
        authority: String,
        poll: Duration,
        socket: Option<Socket>,
        next_attempt: Instant,
        connects: u64,
        last_error: Option<String>,
    }

    impl WsSource {
        /// `target` is `host:port` or `ws://host:port[/path]`.
        pub fn new(target: &str, poll: Duration) -> Self {
            let url = if target.contains("://") {
                target.to_string()
            } else {
                format!("ws://{target}")
            };
            let authority = url
                .split_once("://")
                .map(|(_, rest)| rest)
                .unwrap_or(&url)
                .split('/')
                .next()
                .unwrap_or_default()
                .to_string();
            Self {
                url,
                authority,
                poll,
                socket: None,
                next_attempt: Instant::now(),
                connects: 0,
                last_error: None,
            }
        }

        pub fn connected(&self) -> bool {
            self.socket.is_some()
        }

        fn connect(&mut self) -> Result<Socket, String> {
            let addr = self
                .authority
                .to_socket_addrs()
                .map_err(|e| format!("{}: {e}", self.authority))?
                .next()
                .ok_or_else(|| format!("{}: no address", self.authority))?;
            let stream = TcpStream::connect_timeout(&addr, CONNECT_TIMEOUT)
                .map_err(|e| format!("{}: {e}", self.authority))?;
            stream
                .set_read_timeout(Some(self.poll))
                .map_err(|e| e.to_string())?;
            let (mut socket, _response) =
                tungstenite::client(self.url.as_str(), MaybeTlsStream::Plain(stream))
                    .map_err(|e| format!("{}: handshake: {e}", self.url))?;
            socket
                .send(Message::Text(r#"{"cmd":"metrics_subscribe"}"#.into()))
                .map_err(|e| format!("{}: subscribe: {e}", self.url))?;
            Ok(socket)
        }

        fn drop_socket(&mut self, why: String) {
            if self.socket.take().is_some() {
                eprintln!("[ran-collector] ws {}: {why}; reconnecting", self.url);
            }
            self.last_error = Some(why);
            self.next_attempt = Instant::now() + RETRY;
        }
    }

    /// A report, or a reply to one of our commands? srsRAN answers commands
    /// with an object carrying `cmd`; reports never have one.
    fn is_report(raw: &[u8]) -> bool {
        match serde_json::from_slice::<Value>(raw) {
            Ok(Value::Object(map)) => !map.contains_key("cmd"),
            // Not ours to judge: the session counts it as malformed, which is
            // the accounting a non-JSON UDP datagram already gets.
            _ => true,
        }
    }

    impl MetricsSource for WsSource {
        fn next(&mut self) -> std::io::Result<Option<Vec<u8>>> {
            if self.socket.is_none() {
                if Instant::now() < self.next_attempt {
                    // Nothing to read; spend the slice rather than spinning.
                    std::thread::sleep(self.poll);
                    return Ok(None);
                }
                match self.connect() {
                    Ok(socket) => {
                        self.connects += 1;
                        self.last_error = None;
                        eprintln!("[ran-collector] ws {}: subscribed", self.url);
                        self.socket = Some(socket);
                    }
                    Err(why) => {
                        self.drop_socket(why);
                        std::thread::sleep(self.poll);
                        return Ok(None);
                    }
                }
            }

            let Some(socket) = self.socket.as_mut() else {
                return Ok(None);
            };
            let read = socket.read();
            // tungstenite queues the pong for a ping; flushing sends it.
            let _ = socket.flush();
            match read {
                Ok(Message::Text(text)) => {
                    let raw = text.as_bytes().to_vec();
                    Ok(is_report(&raw).then_some(raw))
                }
                Ok(Message::Binary(raw)) => Ok(is_report(&raw).then_some(raw)),
                Ok(Message::Close(_)) => {
                    self.drop_socket("closed by the gNB".into());
                    Ok(None)
                }
                Ok(_) => Ok(None),
                Err(tungstenite::Error::Io(e))
                    if e.kind() == std::io::ErrorKind::WouldBlock
                        || e.kind() == std::io::ErrorKind::TimedOut =>
                {
                    Ok(None)
                }
                Err(e) => {
                    self.drop_socket(e.to_string());
                    Ok(None)
                }
            }
        }

        fn transport(&self) -> Option<Transport> {
            Some(Transport::Ws)
        }

        fn describe(&self) -> Value {
            json!({
                "source": "ws",
                "metrics_transport": "ws",
                "ws": self.url,
                "ws_connected": self.socket.is_some(),
                "ws_connects": self.connects,
                "ws_last_error": self.last_error,
            })
        }
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn targets_are_normalised_to_urls() {
            let s = WsSource::new("10.0.0.5:8001", Duration::from_millis(10));
            assert_eq!(s.url, "ws://10.0.0.5:8001");
            assert_eq!(s.authority, "10.0.0.5:8001");
            let s = WsSource::new("ws://gnb:8001/metrics", Duration::from_millis(10));
            assert_eq!(s.authority, "gnb:8001");
        }

        #[test]
        fn command_replies_are_not_reports() {
            assert!(!is_report(br#"{"cmd":"metrics_subscribe"}"#));
            assert!(is_report(br#"{"cells":[]}"#));
            assert!(is_report(b"not json"));
        }
    }
}

// --- auto -----------------------------------------------------------------

/// Each transport gets half the usual slice while auto is still listening to
/// both, so one loop iteration costs no more than a single source would.
#[cfg(feature = "ws")]
const AUTO_POLL: Duration = Duration::from_millis(100);

/// Listens on UDP **and** subscribes over WebSocket until one delivers, then
/// keeps only that one.
///
/// Locking matters: a gNB configured for both would otherwise be recorded
/// twice, and every rate and count downstream would double without anything
/// looking wrong.
#[cfg(feature = "ws")]
pub struct AutoSource {
    udp: Option<UdpSource>,
    ws: Option<WsSource>,
    locked: Option<Transport>,
    udp_bind: String,
    ws_url: Value,
}

#[cfg(feature = "ws")]
impl AutoSource {
    pub fn new(udp: UdpSource, ws: WsSource) -> Self {
        let udp_bind = udp.bind.clone();
        let ws_url = ws.describe()["ws"].clone();
        Self {
            udp: Some(udp),
            ws: Some(ws),
            locked: None,
            udp_bind,
            ws_url,
        }
    }

    fn lock(&mut self, transport: Transport) {
        self.locked = Some(transport);
        match transport {
            Transport::Udp => self.ws = None,
            Transport::Ws => self.udp = None,
        }
        eprintln!(
            "[ran-collector] auto: srsRAN is sending over {}; using only that",
            transport.as_str()
        );
    }
}

#[cfg(feature = "ws")]
impl MetricsSource for AutoSource {
    fn next(&mut self) -> std::io::Result<Option<Vec<u8>>> {
        if let Some(udp) = self.udp.as_mut() {
            if let Some(report) = udp.next()? {
                if self.locked.is_none() {
                    self.lock(Transport::Udp);
                }
                return Ok(Some(report));
            }
        }
        if let Some(ws) = self.ws.as_mut() {
            if let Some(report) = ws.next()? {
                if self.locked.is_none() {
                    self.lock(Transport::Ws);
                }
                return Ok(Some(report));
            }
        }
        Ok(None)
    }

    fn transport(&self) -> Option<Transport> {
        self.locked
    }

    fn describe(&self) -> Value {
        let mut d = json!({
            "source": "auto",
            "metrics_transport": self.locked.map(Transport::as_str),
            "udp": self.udp_bind,
            "ws": self.ws_url,
        });
        if let Some(ws) = &self.ws {
            let live = ws.describe();
            d["ws_connected"] = live["ws_connected"].clone();
            d["ws_last_error"] = live["ws_last_error"].clone();
        }
        d
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn source_kinds_parse() {
        assert_eq!(SourceKind::parse("").unwrap(), SourceKind::Auto);
        assert_eq!(SourceKind::parse("AUTO").unwrap(), SourceKind::Auto);
        assert_eq!(SourceKind::parse("udp").unwrap(), SourceKind::Udp);
        assert_eq!(SourceKind::parse("websocket").unwrap(), SourceKind::Ws);
        assert!(SourceKind::parse("tcp").is_err());
    }

    #[test]
    fn udp_returns_none_when_idle() {
        let mut s = UdpSource::bind("127.0.0.1:0", Duration::from_millis(10)).unwrap();
        assert!(s.next().unwrap().is_none());
        assert_eq!(s.transport(), Some(Transport::Udp));
    }

    #[test]
    fn the_metrics_transport_is_not_reported_as_a_zenoh_link() {
        // The admin reads `params.transport` as a node's Zenoh link; the
        // collector has none, so its own udp/ws must not wear that name.
        let s = UdpSource::bind("127.0.0.1:0", Duration::from_millis(10)).unwrap();
        let d = s.describe();
        assert_eq!(d["metrics_transport"], "udp");
        assert!(d.get("transport").is_none());
    }
}
