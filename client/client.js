const readline = require("readline");
const path = require("path");
const fs = require("fs");
const WebSocket = require("ws");

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
const CONFIG_PATH = path.join(__dirname, "client-config.json");
let config = {};

function loadConfig() {
  if (!fs.existsSync(CONFIG_PATH)) return;
  try {
    config = JSON.parse(fs.readFileSync(CONFIG_PATH, "utf-8"));
  } catch (e) {
    console.error(`Failed to parse ${CONFIG_PATH}: ${e.message}`);
    process.exit(1);
  }

  if (config.auto_connect) {
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
let ws = null;
let pc = null;
let myName = null;
let remotePeer = null;
let inCall = false;
let heartbeatTimer = null;
let heartbeatCheckTimer = null;
let lastPong = 0;
let statsTimer = null;
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

function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(() => {
    if (!pc) return;
    const events = pc.pollEvents();
    for (const evt of events) {
      handlePeerEvent(evt);
    }
  }, 50);
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

function handlePeerEvent(evt) {
  const data = evt.data ? JSON.parse(evt.data) : {};

  switch (evt.type) {
    case "offer_created":
      pc.setLocalDescription(data.type, data.sdp);
      sendSignal({ type: "offer", sdp: data.sdp });
      log("Offer created and sent to peer.");
      break;

    case "answer_created":
      pc.setLocalDescription(data.type, data.sdp);
      sendSignal({ type: "answer", sdp: data.sdp });
      log("Answer created and sent to peer.");
      break;

    case "ice_candidate":
      sendSignal({
        type: "ice_candidate",
        candidate: data,
      });
      break;

    case "ice_connection_state":
      log(`ICE connection state: ${data.state}`);
      break;

    case "connection_state":
      log(`Connection state: ${data.state}`);
      if (data.state === "connected") {
        log("P2P connection established!");
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
      pc = new addon.PeerConnection(0, myName);
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
      pc = new addon.PeerConnection(1, myName);
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
      sendSignal({ type: "hangup" });
      endCall();
      log("Call ended.");
      break;
    }

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

    case "help": {
      console.log(`
Commands:
  connect <name> [server]  - Connect to signaling server (default: ws://localhost:8080)
  call                     - Start a call (you send audio + video)
  answer                   - Answer an incoming call (you see/hear caller)
  end                      - End the current call
  disconnect               - Disconnect from server
  status                   - Show current status
  stats on|off             - Toggle live WebRTC stats display (every 2s)
  audioinfo                - Show audio device diagnostics
  videoinfo                - Show video track diagnostics
  quit                     - Exit
`);
      break;
    }

    case "quit": {
      if (inCall) {
        sendSignal({ type: "hangup" });
        endCall();
      }
      stopHeartbeat();
      if (ws) ws.close();
      stopPolling();
      rl.close();
      process.exit(0);
      break;
    }

    default:
      if (cmd) log('Unknown command. Type "help" for available commands.');
      break;
  }
}

function endCall() {
  inCall = false;
  stopPolling();
  stopStats();
  if (pc) {
    pc.close();
    pc = null;
  }
  pendingOffer = null;
}

// --- Main ---
loadConfig();
console.log("=== WebRTC Demo Client (Linux) ===");
console.log('Type "help" for available commands.\n');

if (config.auto_connect) {
  console.log(
    `Auto-connecting to ${config.server_address} as "${config.username}"...`
  );
  doConnect(config.username, config.server_address);
}

prompt();
