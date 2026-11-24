/* mec-cast admin page.
 *
 * The server pushes a full snapshot over /ws/ui on every change; the page
 * replaces its view wholesale. Full replacement rather than diffing removes a
 * class of bug and costs nothing at these row counts.
 *
 * Actions are REST, not WebSocket, so a rejected transition arrives as a 409
 * with the state machine's reason in the body — which is what the operator
 * needs to read.
 *
 * Buttons are never enabled by this file's own reasoning: each row carries an
 * `allowed` array from the server, and that is the only input.
 */
'use strict';

const API = '/api/v1';
const $ = (id) => document.getElementById(id);

// `selected` is deliberately outside the snapshot: the server pushes a full
// replacement on every change, and a selection that vanished whenever another
// node reported would be unusable. Ids that no longer exist are pruned on
// each render rather than accumulating.
// The page's scenarios: the run profiles (protocol.PROFILES). See below.
const SCENARIOS = ['robotics', 'media'];

const state = {
  snapshot: null, socket: null, pollTimer: null, selected: new Set(),
  scenario: savedScenario(),
};

/* ── scenarios ───────────────────────────────────────────────────────── */
// ROS (profile `robotics`) and WebRTC (profile `media`, ADR-0015) share the
// run lifecycle and little else, so the page shows one at a time: its runs,
// its nodes and their columns, its findings, its Add form. Which node types
// serve which scenario comes from the server (topology.roles[].profile), as
// for the role chips; the RAN sources serve both and show on both tabs, as
// do the findings that concern any run (finding.profile null).


// Remembered per browser: a convenience, so storage failing changes nothing.
function savedScenario() {
  try {
    const s = localStorage.getItem('mec-cast.admin.scenario');
    return SCENARIOS.includes(s) ? s : 'robotics';
  } catch {
    return 'robotics';
  }
}

function setScenario(scenario) {
  state.scenario = scenario;
  try { localStorage.setItem('mec-cast.admin.scenario', scenario); } catch { /* fine */ }
  // A selection made on the other tab must not be acted on from this one.
  state.selected.clear();
  applyScenario();
  if (state.snapshot) render(state.snapshot);
}

/** The scenario a node type serves, or null for both (the RAN sources). */
function roleProfile(snapshot, nodeType) {
  const spec = ((snapshot.topology || {}).roles || []).find((r) => r.role === nodeType);
  if (spec) return spec.profile ?? null;
  return MIDDLEWARE_TYPES.has(nodeType) ? 'robotics' : null;
}

const inScenario = (profile) => profile == null || profile === state.scenario;
const runProfile = (run) => run.profile || (run.params || {}).profile || 'robotics';
const ACTIVE = ['starting', 'running', 'degraded'];

function scenarioRuns(snapshot) {
  return shownRuns(snapshot).filter((r) => runProfile(r) === state.scenario);
}

function scenarioNodes(snapshot) {
  return (snapshot.nodes || []).filter((n) => inScenario(roleProfile(snapshot, n.node_type)));
}

function scenarioFindings(snapshot) {
  return (snapshot.findings || []).filter((f) => inScenario(f.profile));
}

/** The tabs: which is chosen, and how many runs are active on each. */
function renderTabs(snapshot) {
  for (const tab of document.querySelectorAll('#scenarioTabs [data-scenario]')) {
    tab.setAttribute('aria-selected', String(tab.dataset.scenario === state.scenario));
  }
  for (const badge of document.querySelectorAll('#scenarioTabs [data-count]')) {
    const n = (snapshot.runs || []).filter(
      (r) => !r.removed && runProfile(r) === badge.dataset.count && ACTIVE.includes(r.state)).length;
    badge.textContent = n ? String(n) : '';
    badge.title = n ? `${n} run(s) active` : '';
  }
}

/** The Add form and the header follow the tab. */
function applyScenario() {
  const media = state.scenario === 'media';
  for (const el of document.querySelectorAll('#addForm .robotics')) el.classList.toggle('hidden', media);
  for (const el of document.querySelectorAll('#addForm .media')) el.classList.toggle('hidden', !media);
  $('runsEmpty').innerHTML = media
    ? 'No WebRTC runs yet. Press <strong>Add run</strong> to create one.'
    : 'No ROS runs yet. Press <strong>Add run</strong> to create one.';
}

const esc = (text) =>
  String(text ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

// UUIDv7 leads with a millisecond timestamp, so consecutive runs share their
// first bytes and a leading slice cannot tell them apart. The trailing bytes
// are the random ones. Ordering is already carried by the `seq` column.
const shortId = (id) => (id ? `…${id.slice(-8)}` : '');

async function copyId(id) {
  try {
    await navigator.clipboard.writeText(id);
  } catch {
    // Clipboard access needs a secure context; the full id is in the title.
  }
}

function when(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString(undefined, {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit',
  });
}

function showError(message) {
  const box = $('error');
  if (!message) { box.classList.add('hidden'); return; }
  box.textContent = message;
  box.classList.remove('hidden');
}

// Node types with a ROS middleware session (protocol.MIDDLEWARE_NODE_TYPES).
// A gNB's `transport` is its metrics feed, not a link, so the column skips it.
const MIDDLEWARE_TYPES = new Set(['client', 'edge', 'render']);

function middlewareOf(n) {
  const p = n.params || {};
  if (!MIDDLEWARE_TYPES.has(n.node_type) || !p.middleware) return '';
  const mw = p.transport ? `${p.middleware}/${p.transport}` : p.middleware;
  // The point-cloud codec rides along (ADR-0014); `none` is the plain case.
  return p.codec && p.codec !== 'none' ? `${mw} · ${p.codec}` : mw;
}

const MIDDLEWARE_NAMES = { zenoh: 'Zenoh', dds: 'Fast DDS' };

// The header badge: what the deployment runs on, per zone when there are
// several. "mixed" is WF_TRANSPORT_MISMATCH, so it is shown as a fault.
function middlewareBadge(snapshot) {
  const per = snapshot.middleware || {};
  const zones = Object.keys(per).sort();
  const badge = $('middleware');
  if (!zones.length || state.scenario !== 'robotics') { badge.hidden = true; return; }
  const label = (kind) => {
    if (kind === 'mixed') return 'mixed';
    const [name, link] = kind.split('/');
    return `${MIDDLEWARE_NAMES[name] || name}${link ? ` · ${link.toUpperCase()}` : ''}`;
  };
  badge.textContent = zones.length === 1
    ? label(per[zones[0]])
    : zones.map((z) => `${z}: ${label(per[z])}`).join('  |  ');
  badge.className = `pill ${zones.some((z) => per[z] === 'mixed') ? 'failed' : 'running'}`;
  badge.hidden = false;
}

/* ── transport ───────────────────────────────────────────────────────── */

function connect() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const socket = new WebSocket(`${proto}//${location.host}/ws/ui`);
  state.socket = socket;

  socket.onopen = () => {
    setLink(true);
    // The socket is the live path; polling is only the fallback.
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  };
  socket.onmessage = (event) => {
    try { render(JSON.parse(event.data)); } catch (err) { showError(err.message); }
  };
  socket.onclose = () => {
    setLink(false);
    startPolling();
    // 2 s here, not the nodes' 30 s: a browser tab is not a fleet.
    setTimeout(connect, 2000);
  };
  socket.onerror = () => socket.close();
}

function startPolling() {
  if (state.pollTimer) return;
  const poll = async () => {
    try {
      const response = await fetch(`${API}/state`);
      if (response.ok) render(await response.json());
    } catch { /* the reconnect timer will deal with it */ }
  };
  poll();
  state.pollTimer = setInterval(poll, 2000);
}

function setLink(live) {
  const pill = $('link');
  pill.textContent = live ? 'live' : 'reconnecting';
  pill.className = `pill ${live ? 'running' : 'failed'}`;
}

/* ── actions ─────────────────────────────────────────────────────────── */

async function call(method, path, body) {
  showError('');
  const response = await fetch(`${API}${path}`, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try { detail = (await response.json()).detail || detail; } catch { /* keep status */ }
    showError(detail);
    throw new Error(detail);
  }
  return response.json();
}

async function act(runId, action) {
  if (action === 'remove') {
    const ok = confirm(
      'Remove this run from the table?\n\n' +
      'Measurement data is kept: the CSVs and run.json stay on disk. Only the row goes.');
    if (!ok) return;
  }
  const path = action === 'remove' ? `/runs/${runId}` : `/runs/${runId}/${action}`;
  await call(action === 'remove' ? 'DELETE' : 'POST', path).catch(() => {});
}

/* ── render ──────────────────────────────────────────────────────────── */

function roleChips(run, nodes, topology) {
  // Which roles exist and which are required comes from the server's
  // topology spec — the same source the quorum rule and the WF_*_ABSENT
  // findings read, so the page cannot disagree with the service about what
  // the fleet needs. The fallback covers a snapshot from an older server.
  // Only the roles of the run's profile, and the RAN sources that serve both:
  // a media run lacks no lidar client (ADR-0015).
  const roles = ((topology && topology.roles) || [
    { role: 'client', required: true }, { role: 'edge', required: true },
    { role: 'gnb', required: false }, { role: 'render', required: false },
    { role: 'xapp', required: false },
  ]).filter((r) => !r.profile || r.profile === (run.profile || 'robotics'));
  const participants = Object.values(run.participants || {});
  const counts = {};
  for (const r of roles) counts[r.role] = 0;
  for (const p of participants) if (p.role in counts) counts[p.role] += 1;
  const active = ['starting', 'running', 'degraded'].includes(run.state);
  return `<span class="roles">${roles.map((r) => {
    const n = counts[r.role];
    const cls = !active ? '' : n > 0 ? 'on' : (r.required ? 'off' : '');
    return `<span class="role ${cls}">${esc(r.role)} ${n}</span>`;
  }).join('')}</span>`;
}

// A zone chip, shown only once a deployment actually has more than one.
// Every single-zone deployment would otherwise gain a column of identical
// "default" labels that carry no information.
function zoneChip(zone, snapshot) {
  const zones = new Set((snapshot.nodes || []).map((n) => n.zone || 'default'));
  for (const r of snapshot.runs || []) zones.add(r.zone || 'default');
  if (zones.size < 2) return '';
  return `<span class="pill draft">${esc(zone || 'default')}</span>`;
}

// Populate the Add-run zone selector from whatever zones actually exist —
// the declared ones, plus any a live node reports. Hidden entirely while
// there is only one, so a single-zone deployment's form is unchanged.
function renderZoneChoices(snapshot) {
  const zones = new Set((snapshot.topology || {}).zones || []);
  for (const n of snapshot.nodes || []) zones.add(n.zone || 'default');
  if (!zones.size) zones.add('default');

  const field = $('f_zone_field');
  const select = $('f_zone');
  if (!field || !select) return;
  field.classList.toggle('hidden', zones.size < 2);

  const wanted = [...zones].sort().join('\u0000');
  if (select.dataset.zones === wanted) return;   // no churn while typing
  select.dataset.zones = wanted;
  const previous = select.value;
  select.innerHTML = [...zones].sort()
    .map((c) => `<option value="${esc(c)}">${esc(c)}</option>`).join('');
  if (zones.has(previous)) select.value = previous;
}

function renderTopology(snapshot) {
  const topology = snapshot.topology || {};
  const card = $('topologyCard');
  // Hidden entirely when nothing is declared. An empty card inviting someone
  // to wonder what is missing is worse than no card: declaring a topology is
  // opt-in, and not declaring one is not a deficiency.
  if (!card) return;
  card.classList.toggle('hidden', !topology.declared);
  if (!topology.declared) return;

  $('topologySource').textContent = topology.source || '';
  const online = new Set((snapshot.nodes || []).filter((n) => n.online).map((n) => n.node_id));
  const body = $('topologyTable').querySelector('tbody');
  body.innerHTML = (topology.nodes || []).map((n) => {
    const up = online.has(n.node_id);
    return `<tr>
      <td><code>${esc(n.node_id)}</code></td>
      <td>${esc(n.zone)}</td>
      <td>${esc(n.role)}</td>
      <td>${esc(n.host)}</td>
      <td><span class="pill ${up ? 'running' : 'draft'}">${up ? 'online' : 'absent'}</span></td>
    </tr>`;
  }).join('');

  // The mermaid source, shown as text. The page has no build step and loads
  // no third-party script (see the module header), so rendering it here
  // would mean shipping a bundler and a CDN dependency for a diagram that is
  // readable as-is and pastes straight into a markdown fence.
  const pre = $('topologyMermaid');
  if (pre) pre.textContent = topology.mermaid || '';
}

/** Runs the table should show: removed ones only when the switch is on. */
function shownRuns(snapshot) {
  const all = snapshot.runs || [];
  return $('showRemoved')?.checked ? all : all.filter((r) => !r.removed);
}

function renderRuns(snapshot) {
  const body = $('runsTable').querySelector('tbody');
  const runs = scenarioRuns(snapshot);
  $('runCount').textContent = runs.length ? `${runs.length}` : '';
  $('runsEmpty').classList.toggle('hidden', runs.length > 0);
  $('runsTable').classList.toggle('hidden', runs.length === 0);

  const findingsByRun = scenarioFindings(snapshot).filter((f) => f.severity === 'error').length;

  body.innerHTML = runs.map((run) => {
    const allowed = run.allowed || [];
    const activeIds = Object.values(snapshot.active_runs || {});
    const isActive = activeIds.length
      ? activeIds.includes(run.run_id)
      : run.run_id === snapshot.active_run_id;
    const button = (action, label, cls) =>
      `<button type="button" class="${cls || ''}" data-run="${esc(run.run_id)}" ` +
      `data-action="${action}" ${allowed.includes(action) ? '' : 'disabled'}>${label}</button>`;
    const picked = state.selected.has(run.run_id) ? 'checked' : '';
    return `<tr class="${run.removed ? 'removed' : ''}">
      <td class="pick"><input type="checkbox" class="rowpick"
          data-run="${esc(run.run_id)}" ${picked}></td>
      <td class="mono dim">${run.seq}</td>
      <td class="mono"><span class="copy" title="${esc(run.run_id)} — click to copy"
          data-copy="${esc(run.run_id)}">${esc(shortId(run.run_id))}</span></td>
      <td>${esc(run.label) || '<span class="dim">—</span>'} ${zoneChip(run.zone, snapshot)}</td>
      <td><span class="pill ${esc(run.state)}">${esc(run.state)}</span></td>
      <td>${roleChips(run, snapshot.nodes, snapshot.topology)}</td>
      <td class="mono dim">${when(run.started_utc)}</td>
      <td class="mono">${isActive && findingsByRun ? `<span class="pill failed">${findingsByRun}</span>` : '<span class="dim">—</span>'}</td>
      <td class="actions">
        <a class="svc" href="${esc(runLogsUrl(snapshot, run.run_id))}"
           target="_blank" rel="noopener"
           title="open this run's session in the logging dashboard">logs ↗</a>
        ${button('start', 'Start', 'primary')}
        ${button('stop', 'Stop')}
        ${button('remove', 'Remove', 'danger')}
      </td>
    </tr>`;
  }).join('');

  for (const el of body.querySelectorAll('button[data-action]')) {
    el.addEventListener('click', () => act(el.dataset.run, el.dataset.action));
  }
  for (const el of body.querySelectorAll('[data-copy]')) {
    el.addEventListener('click', () => copyId(el.dataset.copy));
  }

  const live = new Set(runs.map((r) => r.run_id));
  for (const id of [...state.selected]) if (!live.has(id)) state.selected.delete(id);

  for (const el of body.querySelectorAll('.rowpick')) {
    el.addEventListener('change', () => {
      if (el.checked) state.selected.add(el.dataset.run);
      else state.selected.delete(el.dataset.run);
      syncSelection();
    });
  }
  syncSelection();
}

/** Reflect the selection in the bulk bar and the select-all box. */
function syncSelection() {
  const runs = state.snapshot ? scenarioRuns(state.snapshot).map((r) => r.run_id) : [];
  const n = state.selected.size;

  $('bulkBar').classList.toggle('hidden', n === 0);
  $('bulkCount').textContent = n === 1 ? '1 run selected' : `${n} runs selected`;

  const all = $('selectAll');
  // Indeterminate rather than a half-truth: an unchecked box next to a
  // partial selection invites a click that silently clears it.
  all.checked = runs.length > 0 && n === runs.length;
  all.indeterminate = n > 0 && n < runs.length;
}

/** Remove every selected run, one request each.
 *
 *  There is no bulk endpoint, and adding one would need a partial-failure
 *  contract. Sequential DELETEs give each run the same 409-with-a-reason the
 *  single-row button gives, and a run that refuses does not stop the rest.
 */
async function bulkRemove() {
  const ids = [...state.selected];
  if (!ids.length) return;
  const ok = confirm(
    `Remove ${ids.length} run(s) from the table?\n\n` +
    'Measurement data is kept: the CSVs and run.json stay on disk. Only the rows go.');
  if (!ok) return;

  const failed = [];
  for (const id of ids) {
    try {
      await call('DELETE', `/runs/${id}`);
      state.selected.delete(id);
    } catch {
      failed.push(id);
    }
  }
  if (failed.length) {
    showError(`${failed.length} of ${ids.length} could not be removed — see the row's own state.`);
  }
  syncSelection();
}

function renderFindings(snapshot) {
  const findings = scenarioFindings(snapshot);
  $('findingCount').textContent = findings.length ? `${findings.length}` : '';
  const box = $('findings');
  if (!findings.length) {
    box.innerHTML = '<div class="empty">Nothing wrong that this service can see.</div>';
    return;
  }
  box.innerHTML = findings.map((f) => `
    <div class="finding ${esc(f.severity)}">
      <div>
        <div><strong>${esc(f.subject)}</strong> — ${esc(f.message)}</div>
        <div class="remedy">${esc(f.remedy)}</div>
      </div>
      <code style="margin-left:auto">${esc(f.code)}</code>
    </div>`).join('');
}

// The ports a renderer serves on, parsed from the URL it reported, so the
// tunnel hint stays right when RENDER_INSTANCES>1 shifts them by 2 per
// instance.
function viewerPorts(url) {
  const page = url.match(/:(\d+)\//);
  const stream = url.match(/%3A(\d+)%2Fproxy|:(\d+)\/proxy/);
  return {
    page: page ? page[1] : '9876',
    stream: stream ? (stream[1] || stream[2]) : '9877',
  };
}

// The command that makes the link work.
//
// rerun's gRPC proxy sends access-control-allow-origin only for LOOPBACK
// origins -- measured on 0.36.3: localhost and 127.0.0.1 are echoed, a
// routable address gets no header at all. So a viewer served on the lab
// network loads its page and is then refused its own stream. Forwarding both
// ports makes the browser's origin localhost, which rerun accepts.
function tunnelCommand(n) {
  const url = (n.params || {}).viewer_url;
  if (!url || !n.address) return '';
  const { page, stream } = viewerPorts(url);
  return `ssh -L ${page}:localhost:${page} -L ${stream}:localhost:${stream} ${n.address}`;
}

function viewerCell(n) {
  // Only a renderer that is online and actually serving reports a URL, so the
  // link appears exactly when there is something at the other end.
  const url = n.online ? (n.params || {}).viewer_url : null;
  if (!url) return '';
  const tunnel = tunnelCommand(n);
  // The link points at localhost on purpose and needs the tunnel first; say
  // so in the tooltip rather than let it look broken.
  const hint = tunnel
    ? ` title="Needs an SSH tunnel first:\n${esc(tunnel)}"`
    : '';
  return ` <a class="svc" href="${esc(url)}" target="_blank" rel="noopener"${hint}>viewer ↗</a>`;
}

function loggingBase(snapshot) {
  // The service tells us its browser-facing address; empty means "same host
  // as this page, port 8000", which is right locally and overridable with
  // MECADM_LOGGING_PUBLIC_URL for the lab, where logging is on another host.
  // A base URL with no path — callers append their own.
  const raw = snapshot.logging_url
    || `${location.protocol}//${location.hostname}:8000`;
  return raw.replace(/\/+$/, '');
}

function runLogsUrl(snapshot, runId) {
  // The dashboard, not the raw API. It reads ?run= and opens that session —
  // charts, percentiles and the glass-to-glass series — instead of handing
  // the operator a page of JSON. The run id is the trace_id, which is the
  // join key across every component, so one session covers client, edge,
  // renderer and RAN.
  return `${loggingBase(snapshot)}/dashboard?run=${encodeURIComponent(runId)}`;
}

function serviceLinks(snapshot) {
  // The dashboard with no run selected — it opens the newest session. This
  // pointed at /docs while the static assets were missing from the checked-out
  // submodule commit and the root 404'd; they are present again, so the
  // operator-facing page is the right destination.
  const lg = $('loggingLink');
  lg.href = `${loggingBase(snapshot)}/dashboard`;
  lg.hidden = false;

  // Viewer: whichever renderer is online and serving. There is normally one;
  // with several, the first is linked and every row carries its own.
  // The rerun viewer is the ROS renderer's: shown on that tab only.
  const node = state.scenario === 'robotics' && (snapshot.nodes || []).find(
    (n) => n.online && (n.params || {}).viewer_url);
  const vw = $('viewerLink');
  const tun = $('viewerTunnel');
  if (node) {
    vw.href = node.params.viewer_url;
    vw.hidden = false;
    // Shown beside the link because the link cannot work without it, and an
    // operator who clicks first sees a page that loads and never fills.
    const cmd = tunnelCommand(node);
    if (tun) {
      tun.textContent = cmd;
      tun.hidden = !cmd;
    }
  } else {
    vw.hidden = true;
    if (tun) tun.hidden = true;
  }
}

// The media nodes' own columns, from what each reports in params
// (ADR-0015): the relay its streams and address, a client its stream, its
// WebRTC connection, its video and the libwebrtc it was built on.
function streamOf(n) {
  const p = n.params || {};
  if (n.node_type === 'relay') {
    const streams = Object.entries(p.streams || {});
    return streams.length
      ? streams.map(([s, c]) => `${s}: ${c.publishers}→${c.subscribers}`).join(', ')
      : 'none';
  }
  return p.stream || '';
}

function connectionOf(n) {
  const p = n.params || {};
  if (n.node_type === 'relay') return p.public_addr || '';
  return p.connection || '';
}

function videoOf(n) {
  const v = (n.params || {}).video;
  return v ? `${v.width}×${v.height} @ ${v.fps} · ${v.source}` : '';
}

const CONNECTION_PILL = {
  connected: 'running', connecting: 'starting', new: 'draft', closed: 'draft',
  failed: 'failed', disconnected: 'failed',
};

const NODE_COLUMNS = {
  robotics: [
    ['Middleware', (n) => `<td class="mono" title="Middleware/link the node runs on — fixed at deployment — and its point-cloud codec, when not none">${esc(middlewareOf(n) || '—')}</td>`],
    ['Peers', (n) => `<td class="mono">${(n.peers || []).length}</td>`],
  ],
  media: [
    ['Stream', (n) => `<td class="mono" title="A client's stream; the relay's streams as publishers→subscribers">${esc(streamOf(n) || '—')}</td>`],
    ['Connection', (n) => {
      const c = connectionOf(n);
      if (!c) return '<td class="dim">—</td>';
      return n.node_type === 'relay'
        ? `<td class="mono" title="The address the relay offers its clients">${esc(c)}</td>`
        : `<td><span class="pill ${CONNECTION_PILL[c] || 'draft'}" title="The client's WebRTC connection to the relay">${esc(c)}</span></td>`;
    }],
    ['Video', (n) => `<td class="mono dim">${esc(videoOf(n) || '—')}</td>`],
    ['libwebrtc', (n) => `<td class="mono dim">${esc((n.params || {}).libwebrtc || '—')}</td>`],
  ],
};

function renderNodes(snapshot) {
  const nodes = scenarioNodes(snapshot);
  const own = NODE_COLUMNS[state.scenario];
  const head = ['Node', 'Type', 'State', 'Run', ...own.map(([name]) => name), 'Counters', 'Version', 'Seen'];
  const headRow = $('nodesTable').querySelector('thead tr');
  const wanted = head.join('|');
  if (headRow.dataset.cols !== wanted) {
    headRow.dataset.cols = wanted;
    headRow.innerHTML = head.map((h) => `<th>${esc(h)}</th>`).join('');
  }
  const body = $('nodesTable').querySelector('tbody');
  $('nodesEmpty').classList.toggle('hidden', nodes.length > 0);
  $('nodesTable').classList.toggle('hidden', nodes.length === 0);

  body.innerHTML = nodes.map((n) => {
    const live = n.online ? 'running' : (n.departed ? 'stopped' : 'failed');
    const liveText = n.online ? n.state : (n.departed ? 'left' : 'lost');
    const counters = Object.entries(n.counters || {})
      .map(([k, v]) => `${k}=${v}`).join(' ') || '—';
    return `<tr>
      <td class="mono">${esc(n.node_id)}</td>
      <td>${esc(n.node_type)} ${zoneChip(n.zone, snapshot)}</td>
      <td><span class="pill ${live}">${esc(liveText)}</span></td>
      <td class="mono dim" title="${esc(n.run_id || '')}">${esc(shortId(n.run_id)) || '—'}</td>
      ${own.map(([, cell]) => cell(n)).join('')}
      <td class="mono dim" style="font-size:11px">${esc(counters)}${viewerCell(n)}</td>
      <td class="mono dim">${esc(n.version?.sha ? n.version.sha.slice(0, 7) : '—')}</td>
      <td class="mono dim">${n.silent_for_s}s</td>
    </tr>`;
  }).join('');
}

/** The relay's address, on the WebRTC tab: what every client must reach. */
function relayBadge(snapshot) {
  const badge = $('relayBadge');
  const relays = (snapshot.nodes || []).filter((n) => n.node_type === 'relay' && n.online);
  if (state.scenario !== 'media' || !relays.length) { badge.hidden = true; return; }
  badge.textContent = `relay ${relays.map((n) => (n.params || {}).public_addr || n.host).join(', ')}`;
  badge.className = 'pill running';
  badge.hidden = false;
}

function render(snapshot) {
  state.snapshot = snapshot;
  const online = scenarioNodes(snapshot).filter((n) => n.online).length;
  $('subtitle').textContent =
    `${scenarioRuns(snapshot).length} run(s) · ${online} node(s) online · ` +
    `protocol v${snapshot.protocol} · server ${snapshot.server_version}`;
  renderTabs(snapshot);
  renderRuns(snapshot);
  renderFindings(snapshot);
  renderNodes(snapshot);
  renderTopology(snapshot);
  renderZoneChoices(snapshot);
  serviceLinks(snapshot);
  middlewareBadge(snapshot);
  relayBadge(snapshot);
}

/* ── add form ────────────────────────────────────────────────────────── */

$('showRemoved').addEventListener('change', () => {
  // Selection is dropped: a row that just disappeared must not stay selected
  // and be acted on by the next bulk press.
  state.selected.clear();
  // Whole view, not just the table: the subtitle counts runs too, and the two
  // disagreeing is worse than either being wrong.
  if (state.snapshot) render(state.snapshot);
});

$('bulkRemove').addEventListener('click', bulkRemove);
$('selectAll').addEventListener('change', (event) => {
  const ids = state.snapshot ? scenarioRuns(state.snapshot).map((r) => r.run_id) : [];
  if (event.target.checked) for (const id of ids) state.selected.add(id);
  else state.selected.clear();
  renderRuns(state.snapshot || { runs: [] });
});

$('addToggle').addEventListener('click', () => $('addForm').classList.toggle('open'));
// The precision is the uplink coder's; it means nothing without a codec.
$('f_codec').addEventListener('change', () => {
  $('f_precision').disabled = $('f_codec').value === 'none';
});
for (const tab of document.querySelectorAll('#scenarioTabs [data-scenario]')) {
  tab.addEventListener('click', () => setScenario(tab.dataset.scenario));
}
$('addCancel').addEventListener('click', () => $('addForm').classList.remove('open'));

$('addForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const [width, height] = $('f_vsize').value.split('x').map(Number);
  // The tab is the profile: a run is created in the scenario on show.
  const params = state.scenario === 'media' ? {
    profile: 'media',
    stream: $('f_stream').value.trim() || 'default',
    video_source: $('f_vsource').value,
    video_width: width,
    video_height: height,
    video_fps: Number($('f_vfps').value),
  } : {
    num_points: Number($('f_points').value),
    pattern: $('f_pattern').value,
    rate_hz: Number($('f_rate').value),
    seed: Number($('f_seed').value),
    reliability: $('f_reliability').value,
    qos_depth: Number($('f_depth').value),
    codec: $('f_codec').value,
  };
  if (params.codec && params.codec !== 'none') params.codec_precision_m = Number($('f_precision').value);
  try {
    const zone = $('f_zone').value || 'default';
    await call('POST', '/runs', { label: $('f_label').value.trim(), zone, params });
    $('addForm').classList.remove('open');
    $('f_label').value = '';
  } catch { /* the banner already says why */ }
});

applyScenario();
startPolling();
connect();
