const readline = require("readline");
const path = require("path");
const fs = require("fs");
const WebSocket = require("ws");
const { StatsLog } = require("./stats_log");
const { AdminClient } = require("./admin_client");

// --- File logging ---
const LOG_DIR = path.join(__dirname, "log");
fs.mkdirSync(LOG_DIR, { recursive: true });
const logStream = fs.createWriteStream(path.join(LOG_DIR, "client.log"), {
  flags: "w",
});

const origLog = console.log.bind(console);
const origError = console.error.bind(console);

function timestamp() {
  return new Date().toISOString();
}

console.log = (...args) => {
  origLog(...args);
  logStream.write(`[${timestamp()}] ${args.join(" ")}\n`);
};

console.error = (...args) => {
  origError(...args);
  logStream.write(`[${timestamp()}] ERROR: ${args.join(" ")}\n`);
};

process.on("exit", () => logStream.end());

const LOG_FILE = path.join(LOG_DIR, "client.log");

process.on("uncaughtException", (err) => {
  const msg = `[${timestamp()}] FATAL: ${err.stack || err.message}\n`;
  origError(msg.trim());
  try { fs.appendFileSync(LOG_FILE, msg); } catch {}
  process.exit(1);
});

process.on("unhandledRejection", (reason) => {
  console.error(`Unhandled rejection: ${reason}`);
});

let addon;
try {
  addon = require("./build/Release/webrtc_addon");
} catch (e) {
  console.error("Native addon not found. Build it first with: ./build.sh");
  console.error(e.message);
  process.exit(1);
}

// --- Config ---
// The config file: the first argument (node client.js sender.json), else
// client-config.json beside this script. A sender and a receiver on one
// machine each run with their own.
const CONFIG_PATH = process.argv[2]
  ? path.resolve(process.argv[2])
  : path.join(__dirname, "client-config.json");
let config = {};

function loadConfig() {
  if (!fs.existsSync(CONFIG_PATH)) return;
  try {
    config = JSON.parse(fs.readFileSync(CONFIG_PATH, "utf-8"));
  } catch (e) {
    console.error(`Failed to parse ${CONFIG_PATH}: ${e.message}`);
    process.exit(1);
  }

  // Peer to peer needs the signaling server and a name; the relay needs neither.
  if (config.auto_connect && config.mode !== "relay") {
    if (!config.server_address) {
      console.error(
        "Config error: auto_connect requires server_address to be set."
      );
      process.exit(1);
    }
    if (!config.username) {
      console.error(
        "Config error: auto_connect requires username to be set."
      );
      process.exit(1);
    }
  }
}

// --- State ---
// The admin (ADR-0015): with ADMIN_URL (or admin_url) and mode relay, the
// admin starts and stops this client's runs; see startAdmin below.
let admin = null;
// The run the admin started, and the config as loaded, which a run's own
// params override for its duration.
let adminRun = null;
let baseConfig = null;
let connectionState = "new";
let ws = null;
let pc = null;
// Relay mode (WHIP/WHEP): { verb: "whip"|"whep", stream, resource } while a
// session with the relay is open, else null.
let relay = null;
let myName = null;
let remotePeer = null;
let inCall = false;
// WebRTC's statistics to webrtc.csv and the logging service, while connected.
let statsLog = null;
let heartbeatTimer = null;
let heartbeatCheckTimer = null;
let lastPong = 0;
let statsTimer = null;
let delayTimer = null;
let delayCsvStream = null;
let prevStats = null;
let prevStatsTime = 0;
const HEARTBEAT_INTERVAL = 10000;
const HEARTBEAT_TIMEOUT = 30000;
const STATS_INTERVAL = 2000;

const rl = readline.createInterface({
  input: process.stdin,
  output: process.stdout,
});

function prompt() {
  rl.question("> ", (input) => {
    handleCommand(input.trim());
    if (input.trim() !== "quit") prompt();
  });
}

function log(msg) {
  console.log(`\n[${new Date().toLocaleTimeString()}] ${msg}`);
  process.stdout.write("> ");
}

// --- Event polling ---
let pollTimer = null;
// The window is drawn here, on Node's main thread: macOS allows windows only
// there. About 60 Hz; a frame not yet drawn when the next arrives is skipped.
let renderTimer = null;

function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(() => {
    if (!pc) return;
    const events = pc.pollEvents();
    for (const evt of events) {
      handlePeerEvent(evt);
    }
  }, 50);
  renderTimer = setInterval(() => {
    if (pc) pc.renderPump();
  }, 16);
}

// The PeerConnection's options, from client-config.json. With a role, the
// role decides what is sent: a sender sends video (and audio if send_audio),
// a receiver sends nothing.
function mediaOptions(username) {
  const role = config.role;
  return {
    send_audio: role === "recv" ? false : config.send_audio !== false,
    send_video: role === "recv" ? false : role === "send" ? true : config.send_video !== false,
    username,
    video_source: config.video_source === "synthetic" ? "synthetic" : "camera",
    video_width: config.video_width || 640,
    video_height: config.video_height || 480,
    video_fps: config.video_fps || 30,
    video_seed: config.video_seed ?? 42,
    render: config.render !== false,
    relay: config.mode === "relay",
  };
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
  if (renderTimer) {
    clearInterval(renderTimer);
    renderTimer = null;
  }
}

function handlePeerEvent(evt) {
  const data = evt.data ? JSON.parse(evt.data) : {};

  switch (evt.type) {
    case "offer_created":
      pc.setLocalDescription(data.type, data.sdp);
      if (relay) {
        relayExchange(data.sdp);
        break;
      }
      sendSignal({ type: "offer", sdp: data.sdp });
      log("Offer created and sent to peer.");
      break;

    case "answer_created":
      pc.setLocalDescription(data.type, data.sdp);
      sendSignal({ type: "answer", sdp: data.sdp });
      log("Answer created and sent to peer.");
      break;

    case "ice_candidate":
      // The relay is ICE-lite: it learns our address from our checks, so
      // candidates are not sent to it.
      if (relay) break;
      sendSignal({
        type: "ice_candidate",
        candidate: data,
      });
      break;

    case "ice_connection_state":
      log(`ICE connection state: ${data.state}`);
      break;

    case "connection_state":
      connectionState = data.state;
      log(`Connection state: ${data.state}`);
      if (data.state === "connected") {
        log("P2P connection established!");
        startStatsLog();
        if (config.auto_stats_on && !statsTimer) {
          startStats();
          log("Stats display auto-enabled.");
        }
      } else if (data.state === "failed" || data.state === "disconnected") {
        log("P2P connection lost.");
      }
      break;

    case "ice_gathering_complete":
      log("ICE gathering complete.");
      break;

    case "local_description_set":
    case "remote_description_set":
      break;

    case "remote_audio_track":
      log("Remote audio track received — audio should be playing.");
      break;

    case "remote_video_track":
      log("Remote video track received — video window should open.");
      break;

    case "error":
      log(`Error: ${data.message}`);
      break;
  }
}

// --- Relay mode: WHIP to publish, WHEP to play (edge/media-relay) ---

function relayUrl() {
  return (config.relay_url || "http://localhost:8090").replace(/\/+$/, "");
}

// Start a session with the relay: our offer goes out once created.
function relayStart(verb, stream) {
  if (inCall) {
    log("Already in a session. Use 'end' first.");
    return;
  }
  relay = { verb, stream, resource: null };
  pc = new addon.PeerConnection(mediaOptions(config.username || verb));
  inCall = true;
  startPolling();
  pc.createOffer();
  log(`${verb === "whip" ? "Publishing" : "Playing"} stream "${stream}" via ${relayUrl()} ...`);
}

async function relayExchange(offerSdp) {
  const url = `${relayUrl()}/${relay.verb}/${encodeURIComponent(relay.stream)}`;
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/sdp" },
      body: offerSdp,
    });
    const body = await res.text();
    if (res.status !== 201) {
      log(`Relay refused the session (${res.status}): ${body.trim()}`);
      endCall();
      return;
    }
    if (!relay) return; // ended meanwhile
    relay.resource = res.headers.get("location");
    pc.setRemoteDescription("answer", body);
    log(`Relay answered; session ${relay.resource}.`);
  } catch (e) {
    log(`Relay unreachable at ${url}: ${e.message}`);
    endCall();
  }
}

// Tell the relay the session is over; best effort.
function relayEnd() {
  if (!relay) return;
  const resource = relay.resource;
  relay = null;
  if (resource) {
    fetch(`${relayUrl()}${resource}`, { method: "DELETE" }).catch(() => {});
  }
}

// --- WebSocket signaling ---

function sendSignal(msg) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(msg));
  }
}

function handleSignalingMessage(msg) {
  switch (msg.type) {
    case "registered":
      log(`Connected to server as "${msg.name}".`);
      break;

    case "peer_joined":
      remotePeer = msg.name;
      log(`Peer joined: "${msg.name}". You can now type 'call' to start a call.`);
      break;

    case "peer_left":
      log(`Peer left: "${msg.name}".`);
      if (inCall) {
        endCall();
        log("Call ended (peer disconnected).");
      }
      remotePeer = null;
      break;

    case "offer":
      log(`Incoming call from "${remotePeer || "peer"}"!`);
      pendingOffer = msg;
      if (config.auto_answer) {
        log("Auto-answering call...");
        handleCommand("answer");
      } else {
        log('Type "answer" to accept the call.');
      }
      break;

    case "answer":
      if (pc) {
        pc.setRemoteDescription("answer", msg.sdp);
        log("Remote answer received. Connecting...");
      }
      break;

    case "ice_candidate":
      if (pc && msg.candidate) {
        pc.addIceCandidate(
          msg.candidate.sdpMid,
          msg.candidate.sdpMLineIndex,
          msg.candidate.candidate
        );
      }
      break;

    case "hangup":
      log("Remote peer ended the call.");
      endCall();
      break;

    case "pong":
      lastPong = Date.now();
      break;

    case "clock_sync_request": {
      // Respond with our timestamp for NTP-style offset estimation
      const t2 = Date.now() * 1000000; // Convert ms to ns (approximate)
      sendSignal({
        type: "clock_sync_response",
        t1_ns: msg.t1_ns,
        t2_ns: t2,
        t3_ns: Date.now() * 1000000
      });
      break;
    }

    case "clock_sync_response": {
      const t4 = Date.now() * 1000000;
      const offset = ((msg.t2_ns - msg.t1_ns) + (msg.t3_ns - t4)) / 2;
      const rtt = (t4 - msg.t1_ns) - (msg.t3_ns - msg.t2_ns);
      log(`Clock sync: offset=${(offset/1000000).toFixed(3)}ms, RTT=${(rtt/1000000).toFixed(3)}ms`);
      break;
    }

    case "error":
      log(`Server error: ${msg.message}`);
      break;
  }
}

let pendingOffer = null;

// --- Stats display ---

function formatBitrate(bytes, prevBytes, elapsedSec) {
  if (prevBytes === undefined || elapsedSec <= 0) return "—";
  const bps = ((bytes - prevBytes) * 8) / elapsedSec;
  if (bps >= 1000000) return (bps / 1000000).toFixed(1) + " Mbps";
  if (bps >= 1000) return (bps / 1000).toFixed(0) + " kbps";
  return bps.toFixed(0) + " bps";
}

function pad(str, len) {
  return str.length >= len ? str : str + " ".repeat(len - str.length);
}

function startStats() {
  if (statsTimer) return;
  prevStats = null;
  prevStatsTime = 0;
  statsTimer = setInterval(() => {
    if (!pc || !inCall) return;
    let raw;
    try {
      raw = JSON.parse(pc.getStats());
    } catch {
      return;
    }
    const now = Date.now();
    const elapsed = prevStatsTime ? (now - prevStatsTime) / 1000 : 0;
    if (!prevStats) {
      prevStats = raw;
      prevStatsTime = now;
      return;
    }

    const lines = [];

    // Common
    const rtt = raw.rtt_ms >= 0 ? `${Math.round(raw.rtt_ms)} ms` : "—";
    const vCodec = (raw.out_video_codec || raw.in_video_codec || "").split("/").pop() || "";
    const aCodec = (raw.out_audio_codec || raw.in_audio_codec || "").split("/").pop() || "";
    const codecs = [vCodec, aCodec].filter(Boolean).join(", ") || "—";
    lines.push(`  RTT: ${rtt}  Codecs: ${codecs}`);

    // Video send
    if (raw.out_video_bytes_sent !== undefined) {
      const br = formatBitrate(raw.out_video_bytes_sent, prevStats.out_video_bytes_sent, elapsed);
      const res = (raw.out_video_width && raw.out_video_height)
        ? `${raw.out_video_width}x${raw.out_video_height}` : "—";
      const fps = raw.out_video_fps !== undefined ? `${Math.round(raw.out_video_fps)} fps` : "—";
      lines.push(`  Video ↑  ${pad(br, 10)} ${pad(res, 10)} ${fps}`);
    }

    // Video receive
    if (raw.in_video_bytes_received !== undefined) {
      const br = formatBitrate(raw.in_video_bytes_received, prevStats.in_video_bytes_received, elapsed);
      const res = (raw.in_video_width && raw.in_video_height)
        ? `${raw.in_video_width}x${raw.in_video_height}` : "—";
      const fps = raw.in_video_fps !== undefined ? `${Math.round(raw.in_video_fps)} fps` : "—";
      const loss = raw.in_video_packets_lost !== undefined ? `Loss: ${raw.in_video_packets_lost}` : "";
      const jitter = raw.in_video_jitter !== undefined
        ? `Jitter: ${(raw.in_video_jitter * 1000).toFixed(0)} ms` : "";
      lines.push(`  Video ↓  ${pad(br, 10)} ${pad(res, 10)} ${pad(fps, 8)} ${pad(loss, 10)} ${jitter}`);
    }

    // Audio send
    if (raw.out_audio_bytes_sent !== undefined) {
      const br = formatBitrate(raw.out_audio_bytes_sent, prevStats.out_audio_bytes_sent, elapsed);
      lines.push(`  Audio ↑  ${br}`);
    }

    // Audio receive
    if (raw.in_audio_bytes_received !== undefined) {
      const br = formatBitrate(raw.in_audio_bytes_received, prevStats.in_audio_bytes_received, elapsed);
      const loss = raw.in_audio_packets_lost !== undefined ? `Loss: ${raw.in_audio_packets_lost}` : "";
      const jitter = raw.in_audio_jitter !== undefined
        ? `Jitter: ${(raw.in_audio_jitter * 1000).toFixed(0)} ms` : "";
      lines.push(`  Audio ↓  ${pad(br, 10)} ${" ".repeat(19)} ${pad(loss, 10)} ${jitter}`);
    }

    log("┌─ STATS ─────────────────────────────────────────────────────────");
    for (const l of lines) log(l);
    log("└────────────────────────────────────────────────────────────────");

    prevStats = raw;
    prevStatsTime = now;
  }, STATS_INTERVAL);
}

function stopStats() {
  if (statsTimer) {
    clearInterval(statsTimer);
    statsTimer = null;
  }
  prevStats = null;
  prevStatsTime = 0;
}

// --- Delay measurement display ---

const DELAY_INTERVAL = 2000;

function formatNs(ns) {
  if (ns === 0 || ns === undefined) return "—";
  const abs = Math.abs(ns);
  if (abs >= 1000000000) return (ns / 1000000000).toFixed(3) + " s";
  if (abs >= 1000000) return (ns / 1000000).toFixed(2) + " ms";
  if (abs >= 1000) return (ns / 1000).toFixed(1) + " µs";
  return ns + " ns";
}

function formatStatLine(name, stat) {
  if (!stat || stat.count === 0) return `  ${name}: — (no data)`;
  return `  ${name}: ${formatNs(stat.last_ns)} ` +
    `(avg: ${formatNs(Math.round(stat.avg_ns))}, ` +
    `min: ${formatNs(stat.min_ns)}, ` +
    `max: ${formatNs(stat.max_ns)}, ` +
    `p99: ${formatNs(stat.p99_ns)}, ` +
    `n=${stat.count})`;
}

function startDelay() {
  if (delayTimer) return;
  delayTimer = setInterval(() => {
    if (!pc || !inCall) return;
    let report;
    try {
      report = JSON.parse(pc.getDelayReport());
    } catch {
      return;
    }
    if (!report || !report.total_frames) return;

    const lines = [];
    lines.push(`  Frames: ${report.total_frames} | PTP Sync: ${report.ptp_reliable ? "✓ RELIABLE" : "✗ UNRELIABLE"}`);
    lines.push("");
    if (report.network) lines.push(formatStatLine("Network (one-way)", report.network));
    if (report.glass_to_glass) lines.push(formatStatLine("Glass-to-glass   ", report.glass_to_glass));
    if (report.encoding) lines.push(formatStatLine("Encoding         ", report.encoding));
    if (report.decoding) lines.push(formatStatLine("Decoding         ", report.decoding));
    if (report.jitter_buffer) lines.push(formatStatLine("Jitter buffer    ", report.jitter_buffer));
    if (report.sender_pipeline) lines.push(formatStatLine("Sender pipeline  ", report.sender_pipeline));
    if (report.receiver_pipeline) lines.push(formatStatLine("Receiver pipeline", report.receiver_pipeline));

    if (report.latest) {
      lines.push("");
      lines.push("  Latest frame:");
      lines.push(`    Network: ${formatNs(report.latest.network_ns)} | G2G: ${formatNs(report.latest.glass_to_glass_ns)}`);
      lines.push(`    Encode: ${formatNs(report.latest.encoding_ns)} | Decode: ${formatNs(report.latest.decoding_ns)}`);
      lines.push(`    Jitter buf: ${formatNs(report.latest.jitter_buffer_ns)}`);
    }

    log("┌─ DELAY REPORT ──────────────────────────────────────────────────");
    for (const l of lines) log(l);
    log("└─────────────────────────────────────────────────────────────────");

    // Write to CSV if logging is active
    if (delayCsvStream && report.latest) {
      const r = report.latest;
      delayCsvStream.write(
        `${Date.now()},${r.frame_id},${r.network_ns},${r.glass_to_glass_ns},` +
        `${r.encoding_ns},${r.decoding_ns},${r.jitter_buffer_ns},` +
        `${r.sender_pipeline_ns},${r.receiver_pipeline_ns}\n`
      );
    }
  }, DELAY_INTERVAL);
}

function stopDelay() {
  if (delayTimer) {
    clearInterval(delayTimer);
    delayTimer = null;
  }
}

function startDelayCsv(filename) {
  const csvPath = path.join(LOG_DIR, filename || "delay_report.csv");
  delayCsvStream = fs.createWriteStream(csvPath, { flags: "w" });
  delayCsvStream.write(
    "timestamp_ms,frame_id,network_ns,glass_to_glass_ns," +
    "encoding_ns,decoding_ns,jitter_buffer_ns," +
    "sender_pipeline_ns,receiver_pipeline_ns\n"
  );
  log(`Delay CSV logging started: ${csvPath}`);
}

function stopDelayCsv() {
  if (delayCsvStream) {
    delayCsvStream.end();
    delayCsvStream = null;
    log("Delay CSV logging stopped.");
  }
}

// --- Heartbeat ---

function startHeartbeat() {
  lastPong = Date.now();
  heartbeatTimer = setInterval(() => {
    sendSignal({ type: "ping" });
  }, HEARTBEAT_INTERVAL);
  heartbeatCheckTimer = setInterval(() => {
    if (Date.now() - lastPong > HEARTBEAT_TIMEOUT) {
      log("Server heartbeat timeout — connection lost.");
      stopHeartbeat();
      if (inCall) endCall();
      if (ws) {
        ws.terminate();
        ws = null;
      }
      myName = null;
      remotePeer = null;
    }
  }, HEARTBEAT_INTERVAL);
}

function stopHeartbeat() {
  if (heartbeatTimer) {
    clearInterval(heartbeatTimer);
    heartbeatTimer = null;
  }
  if (heartbeatCheckTimer) {
    clearInterval(heartbeatCheckTimer);
    heartbeatCheckTimer = null;
  }
}

// --- Connection helper ---

function doConnect(name, server) {
  myName = name;
  ws = new WebSocket(server);
  ws.on("open", () => {
    sendSignal({ type: "register", name: myName });
    startHeartbeat();
  });
  ws.on("message", (data) => {
    try {
      handleSignalingMessage(JSON.parse(data));
    } catch {}
  });
  ws.on("close", () => {
    stopHeartbeat();
    log("Disconnected from server.");
    ws = null;
  });
  ws.on("error", (err) => {
    stopHeartbeat();
    log(`Connection error: ${err.message}`);
    ws = null;
  });
}

// --- Commands ---

function handleCommand(input) {
  const parts = input.split(/\s+/);
  const cmd = parts[0]?.toLowerCase();

  switch (cmd) {
    case "connect": {
      if (ws) {
        log("Already connected. Disconnect first.");
        break;
      }
      const name = parts[1] || config.username;
      const server = parts[2] || config.server_address || "ws://localhost:8080";
      if (!name) {
        log("Usage: connect <yourname> [server_url]");
        break;
      }
      doConnect(name, server);
      break;
    }

    case "call": {
      if (!ws) {
        log("Not connected. Use 'connect <name>' first.");
        break;
      }
      if (!remotePeer) {
        log("No peer available. Wait for another client to connect.");
        break;
      }
      if (inCall) {
        log("Already in a call. Use 'end' to hang up first.");
        break;
      }
      pc = new addon.PeerConnection(mediaOptions(myName));
      inCall = true;
      startPolling();
      pc.createOffer();
      log(`Calling "${remotePeer}"...`);
      break;
    }

    case "answer": {
      if (!ws) {
        log("Not connected.");
        break;
      }
      if (!pendingOffer) {
        log("No incoming call to answer.");
        break;
      }
      pc = new addon.PeerConnection(mediaOptions(myName));
      inCall = true;
      startPolling();
      pc.setRemoteDescription("offer", pendingOffer.sdp);
      pc.createAnswer();
      pendingOffer = null;
      log("Answering call...");
      break;
    }

    case "end": {
      if (!inCall) {
        log("Not in a call.");
        break;
      }
      if (!relay) sendSignal({ type: "hangup" });
      endCall();
      log("Call ended.");
      break;
    }

    case "publish":
      relayStart("whip", parts[1] || config.stream || "default");
      break;

    case "play":
      relayStart("whep", parts[1] || config.stream || "default");
      break;

    case "disconnect": {
      if (!ws) {
        log("Not connected.");
        break;
      }
      if (inCall) {
        sendSignal({ type: "hangup" });
        endCall();
      }
      stopHeartbeat();
      ws.close();
      ws = null;
      myName = null;
      remotePeer = null;
      log("Disconnected.");
      break;
    }

    case "status": {
      log(
        `Name: ${myName || "(none)"} | ` +
          `Server: ${ws ? "connected" : "disconnected"} | ` +
          `Peer: ${remotePeer || "(none)"} | ` +
          `Call: ${inCall ? "active" : "inactive"}`
      );
      break;
    }

    case "audioinfo": {
      if (!pc) {
        log("No active PeerConnection. Start a call first.");
        break;
      }
      try {
        const info = JSON.parse(pc.getAudioInfo());
        log("Audio info: " + JSON.stringify(info, null, 2));
      } catch (e) {
        log("Failed to get audio info: " + e.message);
      }
      break;
    }

    case "videoinfo": {
      if (!pc) {
        log("No active PeerConnection. Start a call first.");
        break;
      }
      try {
        const info = JSON.parse(pc.getVideoInfo());
        log("Video info: " + JSON.stringify(info, null, 2));
      } catch (e) {
        log("Failed to get video info: " + e.message);
      }
      break;
    }

    case "stats": {
      const arg = parts[1]?.toLowerCase();
      if (arg === "on") {
        if (!pc || !inCall) {
          log("Not in a call. Start a call first.");
        } else {
          startStats();
          log("Stats display enabled (every 2s). Type 'stats off' to stop.");
        }
      } else if (arg === "off") {
        stopStats();
        log("Stats display disabled.");
      } else {
        log("Usage: stats on | stats off");
      }
      break;
    }

    case "delay": {
      const arg = parts[1]?.toLowerCase();
      if (arg === "on") {
        if (!pc || !inCall) {
          log("Not in a call. Start a call first.");
        } else {
          startDelay();
          log("Delay reporting enabled (every 2s). Type 'delay off' to stop.");
        }
      } else if (arg === "off") {
        stopDelay();
        stopDelayCsv();
        log("Delay reporting disabled.");
      } else if (arg === "report") {
        if (!pc) {
          log("No active PeerConnection.");
        } else {
          try {
            const report = JSON.parse(pc.getDelayReport());
            log("Delay report:\n" + JSON.stringify(report, null, 2));
          } catch (e) {
            log("Failed to get delay report: " + e.message);
          }
        }
      } else if (arg === "log") {
        const filename = parts[2] || "delay_report.csv";
        startDelayCsv(filename);
        if (!delayTimer) {
          startDelay();
          log("Delay reporting also enabled for CSV logging.");
        }
      } else if (arg === "reset") {
        if (pc) {
          pc.resetDelayStats();
          log("Delay statistics reset.");
        } else {
          log("No active PeerConnection.");
        }
      } else if (arg === "ptp") {
        if (!pc) {
          log("No active PeerConnection.");
        } else {
          try {
            const ptp = JSON.parse(pc.getPtpStatus());
            log("PTP status:\n" + JSON.stringify(ptp, null, 2));
          } catch (e) {
            log("Failed to get PTP status: " + e.message);
          }
        }
      } else {
        log("Usage: delay on|off|report|log [file]|reset|ptp");
      }
      break;
    }

    case "help": {
      console.log(`
Commands, through the relay (edge/media-relay):
  publish [stream]         - Send to the relay (WHIP), stream "default" unless named
  play [stream]            - Receive from the relay (WHEP)
  end                      - End the session or call

Commands, peer to peer through the signaling server:
  connect <name> [server]  - Connect to signaling server (default: ws://localhost:8080)
  call                     - Start a call with the remote peer
  answer                   - Answer an incoming call
  disconnect               - Disconnect from server
  status                   - Show current status
  stats on|off             - Toggle live WebRTC stats display (every 2s)
  delay on|off             - Toggle nanosecond delay reporting (every 2s)
  delay report             - Show one-shot delay measurement snapshot
  delay log [file]         - Start logging delay data to CSV file
  delay reset              - Reset delay statistics
  delay ptp                - Show PTP sync status
  audioinfo                - Show audio device diagnostics
  videoinfo                - Show video track diagnostics
  quit                     - Exit

Config (client-config.json):
  mode: p2p|relay          - With auto_connect: relay publishes or plays at
                             start, p2p connects to the signaling server
  role: send|recv          - What this client does: send video, or receive it
  relay_url, stream        - The relay (default http://localhost:8090) and
                             stream name (default "default")
  admin_url                - The admin (ws://<infra>:8099/ws/node; or ADMIN_URL):
                             it starts and stops this client's runs (relay mode)
  instance                 - This client's instance on its host (default 0)
  stats_interval_ms: N     - WebRTC stats to runs/<RUN_ID>/<node>/webrtc.csv
                             every N ms while connected (default 1000; 0 off)
  send_audio: true|false   - Send microphone audio (default: true)
  send_video: true|false   - Send video (default: true)
  video_source: camera|synthetic - Camera, or deterministic synthetic frames
                             (default: camera)
  video_width, video_height, video_fps, video_seed - Source format and seed
                             (default: 640, 480, 30, 42)
  render: true|false       - Show received video in a window (default: true)
`);
      break;
    }

    case "quit": {
      const report = adminRun ? adminStop() : {};
      if (inCall) {
        sendSignal({ type: "hangup" });
        endCall();
      }
      stopHeartbeat();
      if (ws) ws.close();
      stopPolling();
      rl.close();
      if (admin) {
        // The goodbye needs a moment on the wire before the process ends.
        admin.stop(report);
        setTimeout(() => process.exit(0), 300);
      } else {
        process.exit(0);
      }
      break;
    }

    default:
      if (cmd) log('Unknown command. Type "help" for available commands.');
      break;
  }
}

function startStatsLog() {
  if (statsLog || !pc || !StatsLog.enabled()) return;
  const role = config.role;
  statsLog = new StatsLog({
    node: role === "send" ? "sender" : role === "recv" ? "receiver" : "peer",
    peer: relay ? "relay" : remotePeer || "peer",
    intervalMs: config.stats_interval_ms ?? 1000,
    log,
  });
  statsLog.start(pc);
}

function endCall() {
  connectionState = "new";
  if (statsLog) {
    statsLog.stop();
    statsLog = null;
  }
  relayEnd();
  inCall = false;
  stopPolling();
  stopStats();
  stopDelay();
  stopDelayCsv();
  if (pc) {
    pc.close();
    pc = null;
  }
  pendingOffer = null;
}

// --- The admin -----------------------------------------------------------
//
// A media run's start publishes (sender) or plays (receiver) under the run's
// id; its stop ends the session and reports. The run's params override the
// config for its duration: stream, video_source, video_width, video_height,
// video_fps. While a run is on and the session is down -- the relay was not
// up yet, or restarted -- it is retried every few seconds.

const RUN_PARAMS = ["stream", "video_source", "video_width", "video_height", "video_fps"];
const RETRY_SESSION_MS = 5000;
let sessionRetry = null;

function libwebrtcRelease() {
  try {
    return fs.readFileSync(path.join(__dirname, "build/Release/libwebrtc.txt"), "utf-8").trim();
  } catch {
    return null;
  }
}

function adminStart(runId, args) {
  if (adminRun === runId) return;
  if (adminRun) adminStop();
  config = { ...baseConfig };
  for (const key of RUN_PARAMS) if (args[key] !== undefined) config[key] = args[key];
  // The addon's recorder and stats_log.js read RUN_ID when a session opens.
  process.env.RUN_ID = runId;
  adminRun = runId;
  log(`Admin: run ${runId} started`);
  adminSession();
}

function adminSession() {
  if (!adminRun || inCall) return;
  handleCommand(config.role === "recv" ? "play" : "publish");
}

function adminStop() {
  if (!adminRun) return {};
  const report = statsLog ? statsLog.report() : {};
  log(`Admin: run ${adminRun} stopped`);
  adminRun = null;
  if (inCall) endCall();
  config = { ...baseConfig };
  return report;
}

function adminStatus() {
  const running = Boolean(adminRun && inCall);
  const connected = connectionState === "connected";
  const video = (statsLog ? statsLog.lastRows : []).find((r) => r.kind === "video") || {};
  const counters = {};
  for (const key of ["frames", "packets", "bytes", "packets_lost", "nacks", "plis", "freezes"]) {
    if (typeof video[key] === "number") counters[key] = video[key];
  }
  const params = {
    role: config.role,
    stream: config.stream || "default",
    relay_url: relayUrl(),
    connection: inCall ? connectionState : "closed",
    libwebrtc: libwebrtcRelease(),
    out_leaf: config.role === "recv" ? "receiver" : "sender",
  };
  if (config.role !== "recv") {
    params.video = {
      source: config.video_source === "synthetic" ? "synthetic" : "camera",
      width: config.video_width || 640,
      height: config.video_height || 480,
      fps: config.video_fps || 30,
    };
  }
  return {
    state: running ? "running" : "idle",
    run_id: running ? adminRun : null,
    streaming: config.role !== "recv" && connected,
    subscribed: config.role === "recv" && connected,
    params,
    counters,
    last_error: inCall && ["failed", "disconnected"].includes(connectionState)
      ? `WebRTC connection ${connectionState}` : null,
  };
}

function startAdmin(url) {
  if (config.mode !== "relay" || !["send", "recv"].includes(config.role)) {
    console.error("Config error: the admin drives relay mode only: set mode relay and role send or recv.");
    process.exit(1);
  }
  baseConfig = { ...config };
  admin = new AdminClient({
    url,
    nodeType: config.role === "recv" ? "receiver" : "sender",
    instance: config.instance || 0,
    version: { sha: process.env.VCS_REF || "", tag: process.env.VERSION || "" },
    handlers: { onStart: adminStart, onStop: adminStop, status: adminStatus },
    log,
  });
  admin.start();
  sessionRetry = setInterval(adminSession, RETRY_SESSION_MS);
}

// --- Main ---
loadConfig();
console.log("=== mec-cast WebRTC client ===");
console.log('Type "help" for available commands.\n');

const adminUrl = process.env.ADMIN_URL ?? config.admin_url;
if (adminUrl) {
  startAdmin(adminUrl);
} else if (config.auto_connect && config.mode === "relay") {
  // Relay mode: a sender publishes, a receiver plays, at once.
  handleCommand(config.role === "recv" ? "play" : "publish");
} else if (config.auto_connect) {
  console.log(
    `Auto-connecting to ${config.server_address} as "${config.username}"...`
  );
  doConnect(config.username, config.server_address);
}

prompt();
