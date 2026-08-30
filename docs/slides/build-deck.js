// mec-cast architecture deck. Plain, mostly black and white.
// Content is taken from docs/architecture/*, the ADRs, and component READMEs.
const pptx = require("pptxgenjs");
const pres = new pptx();

pres.layout = "LAYOUT_WIDE";           // 13.3 x 7.5 in — set BEFORE adding slides
pres.author = "mec-cast";
pres.title = "mec-cast platform architecture";

// ---- palette: grayscale only -------------------------------------------
const INK = "1A1A1A";      // primary text / borders
const SLATE = "444C52";    // secondary text
const GRAY = "76797C";     // captions
const RULE = "B9BDC0";     // thin borders
const TINT = "F1F2F3";     // card fill
const DEEP = "2B3136";     // emphasis block (the shared spine)
const WHITE = "FFFFFF";

const TITLE_FONT = "Cambria";
const BODY_FONT = "Calibri";

const M = 0.55;            // page margin
const W = 13.33;
const CW = W - 2 * M;      // usable content width = 12.23
const TOP = 1.75;          // first content row

// Evenly spaced columns across the content width. Hand arithmetic here
// previously pushed a box off the slide entirely.
function cols(n, gap) {
  const w = (CW - gap * (n - 1)) / n;
  return Array.from({ length: n }, (_, i) => ({ x: M + i * (w + gap), w }));
}

// ---- helpers ------------------------------------------------------------
function slideBase(title, kicker) {
  const s = pres.addSlide();
  s.background = { color: WHITE };
  s.addText(title, {
    x: M, y: 0.5, w: W - 2 * M, h: 0.6,
    fontFace: TITLE_FONT, fontSize: 32, bold: true, color: INK, margin: 0,
  });
  if (kicker) {
    s.addText(kicker, {
      x: M, y: 1.14, w: W - 2 * M, h: 0.5,
      fontFace: BODY_FONT, fontSize: 13, color: GRAY, italic: true, margin: 0,
    });
  }
  return s;
}

// A labelled component box.
function box(s, o) {
  s.addShape(pres.ShapeType.rect, {
    x: o.x, y: o.y, w: o.w, h: o.h,
    fill: { color: o.dark ? DEEP : (o.plain ? WHITE : TINT) },
    line: { color: o.dark ? DEEP : RULE, width: 1 },
  });
  const fg = o.dark ? WHITE : INK;
  const sub = o.dark ? "D5D8DA" : SLATE;
  s.addText(o.title, {
    x: o.x + 0.12, y: o.y + 0.1, w: o.w - 0.24, h: 0.3,
    fontFace: BODY_FONT, fontSize: o.ts || 13, bold: true, color: fg, margin: 0,
    align: o.align || "left",
  });
  if (o.lines && o.lines.length) {
    s.addText(o.lines.map((t, i) => ({
      text: t,
      options: { breakLine: i < o.lines.length - 1 },
    })), {
      x: o.x + 0.12, y: o.y + 0.42, w: o.w - 0.24, h: o.h - 0.52,
      fontFace: BODY_FONT, fontSize: o.fs || 10.5, color: sub, margin: 0,
      lineSpacingMultiple: 1.08, valign: "top",
    });
  }
}

// Arrow between boxes.
function arrow(s, x1, y1, x2, y2, label) {
  s.addShape(pres.ShapeType.line, {
    x: x1, y: y1, w: x2 - x1, h: y2 - y1,
    line: { color: SLATE, width: 1.25, endArrowType: "triangle" },
  });
  if (label) {
    s.addText(label, {
      x: x1 - 0.15, y: Math.min(y1, y2) - 0.32, w: (x2 - x1) + 0.3, h: 0.28,
      fontFace: BODY_FONT, fontSize: 9, color: GRAY, align: "center", margin: 0,
    });
  }
}

// Explanatory paragraph.
function note(s, x, y, w, h, lines, size) {
  s.addText(lines.map((t, i) => ({
    text: t,
    options: { breakLine: i < lines.length - 1, bold: /^[A-Z][^.]*:$/.test(t) },
  })), {
    x, y, w, h, fontFace: BODY_FONT, fontSize: size || 11.5, color: SLATE,
    margin: 0, lineSpacingMultiple: 1.15, valign: "top",
  });
}

function bullets(s, x, y, w, h, items, size) {
  s.addText(items.map((t, i) => ({
    text: t,
    options: { bullet: true, breakLine: i < items.length - 1 },
  })), {
    x, y, w, h, fontFace: BODY_FONT, fontSize: size || 11.5, color: SLATE,
    margin: 0, paraSpaceAfter: 5, valign: "top",
  });
}

// =========================================================== 1. ARCHITECTURE
{
  const s = slideBase(
    "Architecture overview",
    "An experimentation testbed for industrial communication over private 5G (srsRAN + Open5GS) — used to study large-payload transport, teleoperation latency, and peer-to-edge offload. Precise timing is one instrument, not the purpose."
  );

  const c = cols(3, 0.55);
  const y = TOP, h = 1.30;
  box(s, { x: c[0].x, y, w: c[0].w, h, title: "UE — robot compute", lines: [
    "LiDAR + 5G modem", "ROS2 client publishes PointCloud2",
    "stamps capture_ns, send_ns"] });
  box(s, { x: c[1].x, y, w: c[1].w, h, title: "5G network (lab)", lines: [
    "srsRAN O-CU / O-DU on USRP radios", "Open5GS core, UPF / N6",
    "the impairment under test"] });
  box(s, { x: c[2].x, y, w: c[2].w, h, title: "MEC edge server", lines: [
    "Zenoh ingest node", "stamps recv_ns, process_done_ns",
    "processes the cloud"] });

  arrow(s, c[0].x + c[0].w + 0.06, y + h / 2, c[1].x - 0.06, y + h / 2, "Uu");
  arrow(s, c[1].x + c[1].w + 0.06, y + h / 2, c[2].x - 0.06, y + h / 2, "UPF");

  const y2 = 3.26;
  box(s, { x: c[1].x, y: y2, w: c[1].w, h: 0.95, title: "ran-collector", lines: [
    "O-DU MAC / scheduler KPIs over UDP JSON"] });
  box(s, { x: c[2].x, y: y2, w: c[2].w, h: 0.95, title: "Outputs", lines: [
    "per-frame CSV  +  2 s aggregated snapshots"] });

  box(s, { x: M, y: 4.40, w: CW, h: 0.88, dark: true,
    title: "mec-cast-telemetry — the shared spine",
    lines: ["64-byte TimingEnvelope · DelayStats with exact percentiles · clocks and PTP monitor · lock-free recorder"],
    fs: 11 });

  box(s, { x: M, y: 5.46, w: CW, h: 0.78, plain: true,
    title: "Logging service — PostgreSQL", ts: 12,
    lines: ["Every component posts snapshots here; RUN_ID is the trace_id that joins UE, edge and RAN for one experiment."],
    fs: 11 });

  note(s, M, 6.42, CW, 0.5,
    ["PTP (ptp4l + phc2sys) disciplines every measuring host over the management LAN — never the 5G user plane, which carries no time sync."], 10.5);
}

// ================================================== 2. DEPLOYMENT — LOCAL
{
  const s = slideBase(
    "Deployment — local (development)",
    "One docker network, no hardware. Network impairment is simulated so the pipeline can be exercised end to end on a laptop."
  );

  const c = cols(4, 0.42);
  const h = 1.15, row1 = TOP;
  box(s, { x: c[0].x, y: row1, w: c[0].w, h, title: "lidar-client", lines: ["synthetic PointCloud2", "seed · num_points · rate_hz"] });
  box(s, { x: c[1].x, y: row1, w: c[1].w, h, title: "netem sidecar", lines: ["shares the client netns", "delay · jitter · loss"] });
  box(s, { x: c[2].x, y: row1, w: c[2].w, h, title: "zenoh-router", lines: ["the rendezvous point", "port 7447"] });
  box(s, { x: c[3].x, y: row1, w: c[3].w, h, title: "edge", lines: ["ingest + processing", "writes CSV"] });

  for (let i = 0; i < 3; i++) {
    arrow(s, c[i].x + c[i].w + 0.05, row1 + h / 2, c[i + 1].x - 0.05, row1 + h / 2);
  }

  const half = cols(2, 0.43);
  const row2 = 3.15;
  box(s, { x: half[0].x, y: row2, w: half[0].w, h: 1.0, title: "logging + postgres", lines: [
    "postgres:16 has no host port — reachable only inside the compose network"] });
  box(s, { x: half[1].x, y: row2, w: half[1].w, h: 1.0, title: "runs/  (bind mount)", lines: [
    "per-frame CSV lands on the host and survives docker compose down"] });

  note(s, half[0].x, 4.40, half[0].w, 2.3, [
    "Bring it all up:",
    "make up-local",
    "make logs        make down",
    "",
    "Or one container per terminal:",
    "$COMPOSE up --no-deps <service>",
  ], 11.5);

  bullets(s, half[1].x, 4.40, half[1].w, 2.3, [
    "Two compose files are always passed together so the containers share one network.",
    "The netem sidecar impairs the client's egress — modelling the 5G uplink without touching host networking.",
    "Same containers as the lab; only the composition differs.",
  ], 11.5);
}

// ==================================================== 3. DEPLOYMENT — LAB
{
  const s = slideBase(
    "Deployment — lab testbed",
    "Four roles on four hosts. The impairment is now the real 5G uplink, and clock discipline becomes a hard requirement."
  );

  const c = cols(4, 0.42);
  const h = 1.30, y = TOP;
  box(s, { x: c[0].x, y, w: c[0].w, h, title: "ue", lines: ["lidar-client", "behind the 5G modem", "needs EDGE_HOST"] });
  box(s, { x: c[1].x, y, w: c[1].w, h, title: "gnb", lines: ["ran-collector", "beside the srsRAN O-DU", "UDP :55555"] });
  box(s, { x: c[2].x, y, w: c[2].w, h, title: "edge", lines: ["zenoh-router + edge", "the MEC app server", "behind the UPF"] });
  box(s, { x: c[3].x, y, w: c[3].w, h, title: "infra", lines: ["logging + postgres", "pgdata volume", "deploy this first"] });

  for (let i = 0; i < 3; i++) {
    arrow(s, c[i].x + c[i].w + 0.05, y + h / 2, c[i + 1].x - 0.05, y + h / 2);
  }

  box(s, { x: M, y: 3.28, w: CW, h: 0.85, dark: true,
    title: "PTP grandmaster — management / backhaul LAN",
    lines: ["ptp4l + phc2sys on every measuring host. srsRAN and Open5GS implement no 5G-TSN, so the user plane cannot carry time sync."],
    fs: 11 });

  const h3 = cols(2, 0.43);
  note(s, h3[0].x, 4.38, h3[0].w, 2.4, [
    "Deploy one role to one host:",
    "bash deploy/lab/deploy.sh infra ops@infra-host",
    "",
    "Start order: infra → edge → gnb → ue",
    "Everything posts to the logging service, and the UE dials the edge's router.",
  ], 11.5);

  bullets(s, h3[1].x, 4.38, h3[1].w, 2.4, [
    "The UE dials out across the UPF — no multicast discovery is possible on a cellular user plane.",
    "Each role mounts /dev/ptp0 so timestamps come from the disciplined hardware clock.",
    "Every snapshot records ptp.reliable, so analysis can filter by clock health after the fact.",
  ], 11.5);
}

// ================================================== 4. EDGE SERVICES
{
  const s = slideBase(
    "Edge services — logging and admin",
    "Two first-party services on the edge host. Logging is where aggregated results land; admin is where runs are started and stopped."
  );

  box(s, { x: M, y: TOP, w: 5.90, h: 2.50, title: "HTTP API  (/api/v1)", lines: [
    "POST /logs      one entry or a batch",
    "GET  /logs      filter by service, level, time, trace_id",
    "GET  /stats     counts by level and service",
    "GET  /health/ready   503 when the database is down",
    "",
    "Batch insert is one round trip regardless of size:",
    "INSERT … SELECT FROM unnest(…)",
  ], fs: 11 });

  box(s, { x: M + 6.33, y: TOP, w: 5.90, h: 2.50, title: "Data model — log_entries", lines: [
    "timestamp · level · service · host · logger · message",
    "context   JSONB, GIN indexed — all metrics live here",
    "trace_id  = RUN_ID, joins every component of one run",
    "severity  numeric mirror of level, so min_level ranks",
    "",
    "Keyset pagination on (timestamp, id) — a walk through a",
    "busy table never skips or duplicates rows.",
  ], fs: 11 });

  box(s, { x: M, y: 4.45, w: 5.90, h: 1.50, title: "Admin — run control plane  (:8099)", lines: [
    "Nodes subscribe over WebSocket on startup and retry every",
    "30 s. Runs are created, started and stopped from a page;",
    "state machine, keep-alive, and workflow diagnostics.",
  ], fs: 11 });

  box(s, { x: M + 6.33, y: 4.45, w: 5.90, h: 1.50, title: "Operational posture — both", lines: [
    "No authentication on either — bind them to the management",
    "LAN only. Admin can start and stop experiments, so its",
    "blast radius is wider than the log store's.",
  ], fs: 11 });

  note(s, M, 6.15, CW, 0.55, [
    "Per-frame samples do NOT go here — that firehose goes to CSV under runs/<RUN_ID>/. Logging holds 2-second aggregated snapshots; admin holds run manifests as files, no database, so the page still works when PostgreSQL is down.",
  ], 11);
}

// ===================================================== 5. ROS2 CLIENT
{
  const s = slideBase(
    "ROS2 on the UE — client and renderer",
    "Two nodes on the robot. Vanilla ROS2 application code; only the middleware underneath is unusual."
  );

  const y = TOP, h = 1.25;
  const q = cols(4, 0.30);
  box(s, { x: q[0].x, y, w: q[0].w, h, title: "1 — generate", lines: ["deterministic cloud from a seed", "capture_ns stamped"] });
  box(s, { x: q[1].x, y, w: q[1].w, h, title: "2 — publish", lines: ["CloudWithTelemetry", "send_ns stamped last"] });
  box(s, { x: q[2].x, y, w: q[2].w, h, title: "3 — record", lines: ["sender-side CSV", "snapshots to logging"] });
  box(s, { x: q[3].x, y, w: q[3].w, h, title: "4 — render", lines: ["mec_cast/result from the edge", "process_done_ns stamped"] });
  arrow(s, q[0].x + q[0].w + 0.03, y + h / 2, q[1].x - 0.03, y + h / 2);
  arrow(s, q[1].x + q[1].w + 0.03, y + h / 2, q[2].x - 0.03, y + h / 2);
  arrow(s, q[2].x + q[2].w + 0.03, y + h / 2, q[3].x - 0.03, y + h / 2);

  box(s, { x: M, y: 3.15, w: 5.9, h: 1.95, title: "Test vectors are a controlled variable", lines: [
    "seed         42            reproducible contents",
    "num_points   5000          payload size — the main sweep",
    "rate_hz      10.0          publish rate",
    "pattern      lidar_scan    ten shapes: cube, sphere,",
    "                           torus, helix, wireframe, swarm…",
    "                           1.4x to 23.5x voxel compression,",
    "                           which sets the return payload too",
  ], fs: 11 });

  bullets(s, M + 6.4, 3.15, 5.83, 1.95, [
    "The timing envelope rides in-band as a message field, because rmw_zenoh does not expose per-publish attachments to the application layer.",
    "The renderer is optional and off by default, so the one-way path stays byte-for-byte comparable with campaigns recorded before it existed.",
  ], 11.5);

  note(s, M, 5.40, CW, 1.4, [
    "Both stamps that bound the round trip are taken on this host: capture_ns by the client, process_done_ns by the renderer, off one CLOCK_REALTIME. That makes the renderer's e2e_ns the only end-to-end latency figure in the platform that owes nothing to PTP discipline, and an independent check on the clock offset the one-way metrics depend on.",
  ], 11.5);
}

// ========================================================= 6. EDGE
{
  const s = slideBase(
    "Edge — mec_cast_edge",
    "Runs on the MEC application server. The first line of the callback is a timestamp; everything else follows from it."
  );

  const y = TOP, h = 1.25;
  box(s, { x: M, y, w: 2.85, h, title: "recv_ns", lines: ["stamped on arrival", "before any work"] });
  box(s, { x: M + 3.35, y, w: 2.85, h, title: "process", lines: ["centroid + voxel count", "deterministic"] });
  box(s, { x: M + 6.7, y, w: 2.85, h, title: "process_done_ns", lines: ["stamped after work"] });
  box(s, { x: M + 10.05, y, w: 2.18, h, title: "record", lines: ["CSV + snapshot", "then optionally reply"] });
  arrow(s, M + 2.91, y + h / 2, M + 3.29, y + h / 2);
  arrow(s, M + 6.26, y + h / 2, M + 6.64, y + h / 2);
  arrow(s, M + 9.61, y + h / 2, M + 9.99, y + h / 2);

  box(s, { x: M, y: 3.15, w: 5.9, h: 2.0, title: "Derived per sample", lines: [
    "network      = recv_ns − send_ns",
    "e2e          = process_done_ns − capture_ns",
    "processing   = process_done_ns − recv_ns",
    "sender       = send_ns − capture_ns",
    "",
    "Statistics: Welford mean and stddev, exact windowed percentiles.",
  ], fs: 11 });

  bullets(s, M + 6.4, 3.15, 5.83, 2.0, [
    "The hot path never blocks: samples go into a bounded lock-free ring, and a writer thread drains it.",
    "A full ring drops the sample and counts it — dropping is always preferable to perturbing the measurement.",
    "A separate uploader thread posts snapshots, so a stalled logging service cannot stall CSV writing.",
    "With publish_result on, the voxel cloud already computed goes back to the UE on mec_cast/result — after record(), so the edge's own sample is never delayed for the renderer's benefit.",
  ], 11.5);

  note(s, M, 5.45, CW, 1.4, [
    "Percentiles are exact over a sliding window, computed by sorting a copy off the hot path — not a streaming estimator. P² was rejected because its error is unbounded on multimodal distributions, and 5G latency under HARQ retransmission is exactly that.",
  ], 11.5);
}

// ======================================================== 7. ZENOH
{
  const s = slideBase(
    "Zenoh communication layer",
    "rmw_zenoh rather than raw DDS. The application stays vanilla ROS2; only the middleware swaps."
  );

  const y = TOP, h = 1.20;
  box(s, { x: M, y, w: 3.4, h, title: "UE session", lines: ["dials out to the router"] });
  box(s, { x: M + 3.9, y, w: 4.3, h, title: "zenoh router (edge host)", lines: ["unicast rendezvous — no multicast anywhere"] });
  box(s, { x: M + 8.7, y, w: 3.53, h, title: "edge session", lines: ["subscribes via the router"] });
  arrow(s, M + 3.46, y + h / 2, M + 3.84, y + h / 2);
  arrow(s, M + 8.26, y + h / 2, M + 8.64, y + h / 2);

  const ty = 3.20;
  box(s, { x: M, y: ty, w: 3.9, h: 2.35, title: "Concern on a 5G link", lines: [
    "", "Discovery across NAT / UPF", "", "Data-path NAT traversal", "", "Large samples (~2 MB)",
  ], fs: 11 });
  box(s, { x: M + 4.15, y: ty, w: 3.9, h: 2.35, title: "DDS", lines: [
    "", "SPDP is multicast — the cellular user plane drops it",
    "", "announces private-IP locators the peer cannot reach",
    "", "RTPS fragments; one lost fragment loses the sample",
  ], fs: 10.5 });
  box(s, { x: M + 8.3, y: ty, w: 3.93, h: 2.35, title: "Zenoh", lines: [
    "", "router-based unicast dial-out, traverses NAT natively",
    "", "the UE dials out — an outbound connection just works",
    "", "large payloads over lossy links; here udp/7447?rel=1 —",
    "retransmit, no congestion control, no TLS",
  ], fs: 10.5 });

  note(s, M, 5.75, CW, 1.2, [
    "DDS is the more popular and more mature industrial standard — but that popularity is measured on local networks, and this path is NAT'd, lossy and uplink-constrained. Because the edge subscriber depends only on the telemetry crate, \"Zenoh versus DDS over 5G\" remains a measurable result rather than a closed door.",
  ], 11.5);
}

// ============================================= 8. PROFILE B — CURRENT
{
  const s = slideBase(
    "Profile B — WebRTC, current model",
    "The platform's second transport profile: real-time media over WebRTC, measured with the same telemetry spine."
  );

  const y = TOP, h = 1.30;
  box(s, { x: M, y, w: 3.75, h, title: "Native client", lines: ["Node.js console app", "C++ N-API addon"] });
  box(s, { x: M + 4.25, y, w: 3.75, h, title: "Signaling server", lines: ["WebSocket, JSON", "offer / answer / ICE"] });
  box(s, { x: M + 8.5, y, w: 3.73, h, title: "Peer client", lines: ["P2P after ICE", "renders + measures"] });
  arrow(s, M + 3.81, y + h / 2, M + 4.19, y + h / 2);
  arrow(s, M + 8.06, y + h / 2, M + 8.44, y + h / 2);

  box(s, { x: M, y: 3.28, w: 5.90, h: 1.95, title: "How the timing gets across", lines: [
    "A patched libwebrtc fork adds a custom 16-byte RTP header",
    "extension carrying capture_ns and send_ns, and forces every",
    "frame to be a timing frame — upstream only samples",
    "periodically, which is useless per-frame.",
  ], fs: 11 });

  box(s, { x: M + 6.33, y: 3.28, w: 5.90, h: 1.95, title: "Attached to the shared spine", lines: [
    "The addon links the telemetry crate over a C ABI and records",
    "one sample per rendered frame, so media runs land in the same",
    "CSV schema and the same database as the point-cloud profile.",
    "One RUN_ID joins both.",
  ], fs: 11 });

  note(s, M, 5.45, CW, 1.4, [
    "The cost of this approach: the fork is a ~20 GB source tree that takes hours to build, and the measurement points are only as good as the places the patch can reach. Encode duration, for example, arrives quantised to whole milliseconds in a system that otherwise measures in nanoseconds.",
  ], 11.5);
}

// ============================================== 9. PROFILE B — PLANNED
{
  const s = slideBase(
    "Profile B — planned: str0m with an SFU",
    "Replacing the patched fork with a sans-IO library, so precise timestamps fall out of the architecture instead of a patch."
  );

  const y = TOP, h = 1.30;
  box(s, { x: M, y, w: 3.4, h, title: "Publisher", lines: ["encodes and sends"] });
  box(s, { x: M + 3.9, y, w: 4.3, h, dark: true, title: "SFU on the MEC edge", lines: [
    "str0m state machine, owns its UDP socket", "selective forwarding — one upstream, many down"], fs: 10.5 });
  box(s, { x: M + 8.7, y, w: 3.53, h, title: "Subscribers", lines: ["many receivers", "no mesh, no re-encode"] });
  arrow(s, M + 3.46, y + h / 2, M + 3.84, y + h / 2);
  arrow(s, M + 8.26, y + h / 2, M + 8.64, y + h / 2);

  bullets(s, M, 3.28, 5.90, 2.1, [
    "Sans-IO: the application owns the socket and the event loop.",
    "send_ns is stamped at the actual socket write, recv_ns at the socket read.",
    "No forked media stack to maintain, and no 20 GB build.",
  ], 11.5);

  bullets(s, M + 6.33, 3.28, 5.90, 2.1, [
    "An SFU scales to many viewers — the natural shape for teleoperation and multi-operator monitoring.",
    "Feeds the identical recorder, so media and point-cloud runs stay directly comparable.",
    "The SFU is a MEC server component; the library fork stays vendored separately.",
  ], 11.5);

  note(s, M, 5.60, CW, 1.3, [
    "Open design point: the 64-byte envelope exceeds the 16-byte limit of the one-byte RTP extension format, so either the two-byte format is used or the extension carries the timestamps only, with the rest inferred per session. The CSV and snapshot contract is unaffected either way.",
  ], 11.5);
}

// ================================================= 10. APPLICATIONS
{
  const s = slideBase(
    "Applications and future work",
    "What a per-frame, clock-disciplined latency platform over private 5G is actually for."
  );

  const cw = 3.87, ch = 2.05, y = 1.72;
  box(s, { x: M, y, w: cw, h: ch, title: "Industrial robotics", lines: [
    "ROS2 fleets on private 5G instead of cabling or Wi-Fi.",
    "Offloading perception to a MEC server lets robots carry",
    "less compute — but only if the latency budget is known",
    "rather than hoped for.",
  ], fs: 11 });
  box(s, { x: M + cw + 0.31, y, w: cw, h: ch, title: "Teleoperation", lines: [
    "Remote driving, cranes, mining, inspection. The operator",
    "needs a defensible glass-to-glass figure and its tail —",
    "a p99 that occasionally doubles is a safety property,",
    "not a statistic.",
  ], fs: 11 });
  box(s, { x: M + 2 * (cw + 0.31), y, w: cw, h: ch, title: "Vehicle to vehicle / V2X", lines: [
    "Cooperative perception: vehicles sharing point clouds",
    "through an edge server. Sensor sharing is only useful",
    "if the data arrives inside the reaction window, which",
    "is exactly what this measures.",
  ], fs: 11 });

  const y2 = 3.92;
  box(s, { x: M, y: y2, w: cw, h: ch, title: "Other latency-critical uses", lines: [
    "Closed-loop motion control and AGV coordination",
    "Drone swarms and remote inspection",
    "AR / VR assistance on the factory floor",
    "Haptics and force feedback, where budgets are tightest",
  ], fs: 11 });
  box(s, { x: M + cw + 0.31, y: y2, w: cw, h: ch, title: "Near-term platform work", lines: [
    "Draco compression as a measured variable",
    "Latency versus payload-size sweeps",
    "Zenoh versus DDS over the same 5G link",
    "Real LiDAR replacing the synthetic source",
  ], fs: 11 });
  box(s, { x: M + 2 * (cw + 0.31), y: y2, w: cw, h: ch, dark: true, title: "Research directions", lines: [
    "E2 / near-RT RIC xApp for standardised RAN KPIs",
    "RAN-aware scheduling: prioritise the sensor QoS flow",
    "Network slicing per application class",
    "Correlating MAC-layer events with application tails",
  ], fs: 11 });

  note(s, M, 6.10, CW, 0.55, [
    "The common thread: each of these needs a number someone is willing to defend. Reproducibility artifacts — run identifiers, per-frame CSV and recorded clock health — are what turn a demonstration into evidence.",
  ], 11 );
}

const out = process.argv[2] || "mec-cast-architecture.pptx";
pres.writeFile({ fileName: out }).then(() => console.log("wrote " + out));
