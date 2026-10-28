// WebRTC's own statistics, recorded: one row per RTP stream every interval,
// into <RUNS_DIR>/<RUN_ID>/<node>/webrtc.csv, and posted to the logging
// service (LOGGING_URL) the way the RAN tap posts its KPIs. The relay writes
// the same columns (edge/media-relay/src/stats.rs); the schema is documented
// in docs/architecture/str0m-profile.md.

const fs = require("fs");
const os = require("os");
const path = require("path");

// Keep in step with COLUMNS in edge/media-relay/src/stats.rs.
const COLUMNS = [
  "ts_ns", "node", "peer", "direction", "kind", "mid", "ssrc", "codec",
  "packets", "bytes", "retransmitted_packets", "packets_lost", "loss_fraction",
  "jitter_ms", "rtt_ms", "nacks", "plis", "firs",
  "frames", "keyframes", "frames_dropped", "fps", "width", "height",
  "bwe_bps", "target_bps", "quality_limitation",
  "jitter_buffer_ms", "freezes", "freeze_ms",
];

function csvField(v) {
  if (v === undefined || v === null) return "";
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : String(+v.toFixed(3));
  const s = String(v);
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

class StatsLog {
  // node: "sender", "receiver" or "peer"; peer: who the stream is with.
  constructor({ node, peer, intervalMs, log }) {
    this.node = node;
    this.peer = peer;
    this.intervalMs = intervalMs;
    this.log = log;
    this.runId = process.env.RUN_ID || "dev-run";
    this.dir = path.join(process.env.RUNS_DIR || "runs", this.runId, node);
    const base = (process.env.LOGGING_URL || "").replace(/\/+$/, "");
    this.url = base ? `${base}/api/v1/logs` : null;
    this.timer = null;
    this.fd = null;
    this.rowsWritten = 0;
    this.posts = { ok: 0, failed: 0, skipped: 0 };
    this.inFlight = false;
  }

  static enabled() {
    return process.env.MEC_CAST_TELEMETRY !== "0";
  }

  start(pc) {
    if (this.timer || !this.intervalMs) return;
    fs.mkdirSync(this.dir, { recursive: true });
    const file = path.join(this.dir, "webrtc.csv");
    const fresh = !fs.existsSync(file) || fs.statSync(file).size === 0;
    // Synchronous: `quit` exits the process right after stop(), and a
    // stream's buffered tail would be lost. One small write a second.
    this.fd = fs.openSync(file, "a");
    if (fresh) fs.writeSync(this.fd, COLUMNS.join(",") + "\n");
    this.timer = setInterval(() => this.sample(pc), this.intervalMs);
    this.log(`WebRTC stats every ${this.intervalMs} ms to ${file}` +
      (this.url ? ` and ${this.url}` : ""));
  }

  sample(pc) {
    let report;
    try {
      report = JSON.parse(pc.getStatsRows());
    } catch {
      return;
    }
    if (!report.rows || report.rows.length === 0) return;
    const rows = report.rows.map((r) => ({
      ts_ns: report.ts_ns, node: this.node, peer: this.peer, ...r,
    }));
    fs.writeSync(this.fd, rows.map((r) => COLUMNS.map((c) => csvField(r[c])).join(",") + "\n").join(""));
    this.rowsWritten += rows.length;
    this.post(rows, report.ts_ns);
  }

  // One entry per sample. A post still in flight means the service is slow:
  // skip rather than queue, and count it -- the CSV has every row.
  post(rows, tsNs) {
    if (!this.url) return;
    if (this.inFlight) {
      this.posts.skipped++;
      return;
    }
    this.inFlight = true;
    const entry = {
      level: "INFO",
      service: "mec-cast-webrtc-client",
      host: os.hostname(),
      logger: "webrtc.stats",
      message: "webrtc stats",
      trace_id: this.runId,
      context: { run_id: this.runId, node: this.node, ts_ns: tsNs, webrtc: rows },
    };
    fetch(this.url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify([entry]),
      signal: AbortSignal.timeout(2000),
    })
      .then((res) => { res.ok ? this.posts.ok++ : this.posts.failed++; })
      .catch(() => { this.posts.failed++; })
      .finally(() => { this.inFlight = false; });
  }

  stop() {
    if (!this.timer) return;
    clearInterval(this.timer);
    this.timer = null;
    fs.closeSync(this.fd);
    this.fd = null;
    const p = this.posts;
    this.log(`WebRTC stats: ${this.rowsWritten} rows` +
      (this.url ? `, ${p.ok} posted, ${p.failed} failed, ${p.skipped} skipped` : ""));
  }
}

module.exports = { StatsLog, COLUMNS };
