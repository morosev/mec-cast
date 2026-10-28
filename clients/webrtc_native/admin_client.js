// The admin control plane, for the media profile's sender and receiver
// (ADR-0015): the client joins the admin service as node type `sender` or
// `receiver`, publishes or plays when a media run starts, stops when it
// stops, and reports its state every few seconds. The protocol is the one
// every mec-cast node speaks (services/admin/src/mec_cast_admin/protocol.py);
// the Rust nodes' client is telemetry/src/admin.rs, the ROS nodes'
// ros2/src/mec_cast_admin_client.

const os = require("os");
const WebSocket = require("ws");

const PROTOCOL_VERSION = 1;
// The admin's requirement: retry every 30 s while it is unreachable, with
// jitter so a fleet restarted together does not reconnect in lockstep.
const RETRY_MS = 30000;
const STATUS_MS = 2000;

class AdminClient {
  // nodeType: "sender" | "receiver"; handlers: { onStart(runId, args),
  // onStop() -> report, status() -> payload fields }.
  constructor({ url, nodeType, instance = 0, version = {}, handlers, log }) {
    this.url = url;
    this.nodeType = nodeType;
    this.host = os.hostname().split(".")[0];
    this.nodeId = `${nodeType}-${this.host}-${instance}`;
    this.zone = process.env.ZONE || "";
    this.version = version;
    this.handlers = handlers;
    this.log = log;
    this.ws = null;
    this.retryTimer = null;
    this.statusTimer = null;
    this.closing = false;
    this.seq = 0;
  }

  start() {
    this.log(`Admin at ${this.url}, as ${this.nodeId}`);
    this.connect();
    this.statusTimer = setInterval(() => this.status(), STATUS_MS);
  }

  connect() {
    const ws = new WebSocket(this.url);
    this.ws = ws;
    ws.on("open", () => this.send("hello", this.helloPayload()));
    ws.on("message", (data) => this.onFrame(data));
    ws.on("error", () => {}); // `close` follows, and says it
    ws.on("close", () => {
      if (this.ws === ws) this.ws = null;
      if (this.closing) return;
      const wait = RETRY_MS * (0.9 + 0.2 * Math.random());
      this.log(`Admin unreachable; retrying in ${Math.round(wait / 1000)} s`);
      this.retryTimer = setTimeout(() => this.connect(), wait);
    });
  }

  helloPayload() {
    const s = this.handlers.status();
    return {
      node_type: this.nodeType,
      node_id: this.nodeId,
      host: this.host,
      zone: this.zone,
      pid: process.pid,
      version: this.version,
      state: s.state,
      run_id: s.run_id,
      autostart: true,
      params: s.params,
    };
  }

  onFrame(data) {
    let frame;
    try {
      frame = JSON.parse(data.toString());
    } catch {
      return;
    }
    if (frame.v !== PROTOCOL_VERSION) {
      // Loud but not fatal: an admin upgrade heals it.
      this.log(`Admin speaks protocol ${frame.v}; this client speaks ${PROTOCOL_VERSION}`);
      return;
    }
    const payload = frame.payload || {};
    switch (frame.type) {
      case "ping":
        this.send("pong", {});
        break;
      case "welcome":
        if (payload.active_run) {
          this.handlers.onStart(payload.active_run.run_id, payload.active_run.params || {});
        }
        this.status();
        break;
      case "command":
        this.onCommand(frame, payload);
        break;
      case "error":
        this.log(`Admin error ${payload.code}: ${payload.message}`);
        break;
      default:
        break;
    }
  }

  onCommand(frame, payload) {
    let ok = true;
    let error = null;
    let report = {};
    try {
      switch (payload.command) {
        case "run.start":
        case "stream.start":
          if (!payload.run_id) throw new Error("run.start without a run_id");
          this.handlers.onStart(payload.run_id, payload.args || {});
          break;
        case "run.stop":
        case "stream.stop":
          report = this.handlers.onStop() || {};
          break;
        case "status.report":
          break;
        default:
          throw new Error(`unknown command ${payload.command}`);
      }
    } catch (e) {
      ok = false;
      error = e.message;
      this.log(`Admin command ${payload.command} failed: ${e.message}`);
    }
    this.send("ack", { in_reply_to: frame.msg_id, ok, error });
    // The report travels with the status: an admin-driven stop leaves this
    // process alive, so it cannot wait for the goodbye.
    this.status(report);
  }

  status(report = {}) {
    this.send("status", { node_type: this.nodeType, ...this.handlers.status(), report });
  }

  send(type, payload) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    this.ws.send(JSON.stringify({
      v: PROTOCOL_VERSION,
      type,
      msg_id: `${this.nodeId}-${Date.now()}-${this.seq++}`,
      ts_ns: Number(process.hrtime.bigint() - HRTIME_AT_EPOCH_NS) + EPOCH_NS,
      node_id: this.nodeId,
      payload,
    }));
  }

  // Say goodbye, then close: the admin then knows the node left rather than
  // died (no WF_PARTICIPANT_LOST).
  stop(report = {}) {
    this.closing = true;
    clearInterval(this.statusTimer);
    clearTimeout(this.retryTimer);
    const s = this.handlers.status();
    this.send("goodbye", { reason: "shutdown", run_id: s.run_id, final_report: report });
    if (this.ws) this.ws.close();
  }
}

// ts_ns is CLOCK_REALTIME in ns: the admin compares it with its own clock
// (WF_CLOCK_OFFSET). Date.now() has ms resolution; anchored once and
// advanced on the monotonic clock, it has ns resolution and the same epoch.
const EPOCH_NS = Date.now() * 1e6;
const HRTIME_AT_EPOCH_NS = process.hrtime.bigint();

module.exports = { AdminClient };
