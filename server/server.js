const WebSocket = require("ws");
const path = require("path");
const fs = require("fs");

// --- File logging ---
const LOG_DIR = path.join(__dirname, "log");
fs.mkdirSync(LOG_DIR, { recursive: true });
const logStream = fs.createWriteStream(path.join(LOG_DIR, "server.log"), {
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

const PORT = 8080;
const wss = new WebSocket.Server({ port: PORT });

// Track connected peers: Map<ws, { name: string }>
const peers = new Map();

function log(msg) {
  console.log(`[${new Date().toLocaleTimeString()}] ${msg}`);
}

function broadcast(sender, message) {
  for (const [ws] of peers) {
    if (ws !== sender && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(message));
    }
  }
}

function getOtherPeer(sender) {
  for (const [ws] of peers) {
    if (ws !== sender && ws.readyState === WebSocket.OPEN) {
      return ws;
    }
  }
  return null;
}

wss.on("connection", (ws) => {
  ws.on("message", (data) => {
    let msg;
    try {
      msg = JSON.parse(data);
    } catch {
      return;
    }

    switch (msg.type) {
      case "register": {
        if (peers.size >= 2) {
          ws.send(
            JSON.stringify({
              type: "error",
              message: "Server full. Only 2 peers allowed.",
            })
          );
          ws.close();
          return;
        }
        peers.set(ws, { name: msg.name });
        log(`User connected: "${msg.name}" (${peers.size}/2 peers)`);
        ws.send(JSON.stringify({ type: "registered", name: msg.name }));

        // Notify existing peers about the new user
        broadcast(ws, { type: "peer_joined", name: msg.name });

        // Notify new user about existing peers
        for (const [otherWs, otherPeer] of peers) {
          if (otherWs !== ws) {
            ws.send(
              JSON.stringify({ type: "peer_joined", name: otherPeer.name })
            );
          }
        }
        break;
      }

      case "offer":
      case "answer":
      case "ice_candidate":
      case "hangup": {
        const peer = peers.get(ws);
        if (!peer) return;
        const other = getOtherPeer(ws);
        if (other) {
          log(`Relaying "${msg.type}" from "${peer.name}"`);
          other.send(JSON.stringify(msg));
        }
        break;
      }
    }
  });

  ws.on("close", () => {
    const peer = peers.get(ws);
    if (peer) {
      log(
        `User disconnected: "${peer.name}" (${peers.size - 1}/2 peers)`
      );
      peers.delete(ws);
      broadcast(ws, { type: "peer_left", name: peer.name });
    }
  });

  ws.on("error", (err) => {
    log(`WebSocket error: ${err.message}`);
  });
});

log(`Signaling server listening on ws://localhost:${PORT}`);
log("Waiting for peers to connect (max 2)...");
