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

const state = { snapshot: null, socket: null, pollTimer: null };

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

function roleChips(run, nodes) {
  const participants = Object.values(run.participants || {});
  const counts = { client: 0, edge: 0, gnb: 0, render: 0 };
  for (const p of participants) if (p.role in counts) counts[p.role] += 1;
  // A run needs at least one client and one edge; a gNB and a renderer are
  // both optional — a run with no viewer attached is perfectly legitimate.
  const required = { client: true, edge: true, gnb: false, render: false };
  const active = ['starting', 'running', 'degraded'].includes(run.state);
  return `<span class="roles">${['client', 'edge', 'gnb', 'render'].map((role) => {
    const n = counts[role];
    const cls = !active ? '' : n > 0 ? 'on' : (required[role] ? 'off' : '');
    return `<span class="role ${cls}">${role} ${n}</span>`;
  }).join('')}</span>`;
}

function renderRuns(snapshot) {
  const body = $('runsTable').querySelector('tbody');
  const runs = snapshot.runs || [];
  $('runCount').textContent = runs.length ? `${runs.length}` : '';
  $('runsEmpty').classList.toggle('hidden', runs.length > 0);
  $('runsTable').classList.toggle('hidden', runs.length === 0);

  const findingsByRun = (snapshot.findings || []).filter((f) => f.severity === 'error').length;

  body.innerHTML = runs.map((run) => {
    const allowed = run.allowed || [];
    const isActive = run.run_id === snapshot.active_run_id;
    const button = (action, label, cls) =>
      `<button type="button" class="${cls || ''}" data-run="${esc(run.run_id)}" ` +
      `data-action="${action}" ${allowed.includes(action) ? '' : 'disabled'}>${label}</button>`;
    return `<tr>
      <td class="mono dim">${run.seq}</td>
      <td class="mono"><span class="copy" title="${esc(run.run_id)} — click to copy"
          data-copy="${esc(run.run_id)}">${esc(shortId(run.run_id))}</span></td>
      <td>${esc(run.label) || '<span class="dim">—</span>'}</td>
      <td><span class="pill ${esc(run.state)}">${esc(run.state)}</span></td>
      <td>${roleChips(run, snapshot.nodes)}</td>
      <td class="mono dim">${when(run.started_utc)}</td>
      <td class="mono">${isActive && findingsByRun ? `<span class="pill failed">${findingsByRun}</span>` : '<span class="dim">—</span>'}</td>
      <td class="actions">
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
}

function renderFindings(snapshot) {
  const findings = snapshot.findings || [];
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

function renderNodes(snapshot) {
  const nodes = snapshot.nodes || [];
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
      <td>${esc(n.node_type)}</td>
      <td><span class="pill ${live}">${esc(liveText)}</span></td>
      <td class="mono dim" title="${esc(n.run_id || '')}">${esc(shortId(n.run_id)) || '—'}</td>
      <td class="mono">${(n.peers || []).length}</td>
      <td class="mono dim" style="font-size:11px">${esc(counters)}</td>
      <td class="mono dim">${esc(n.version?.sha ? n.version.sha.slice(0, 7) : '—')}</td>
      <td class="mono dim">${n.silent_for_s}s</td>
    </tr>`;
  }).join('');
}

function render(snapshot) {
  state.snapshot = snapshot;
  const online = (snapshot.nodes || []).filter((n) => n.online).length;
  $('subtitle').textContent =
    `${(snapshot.runs || []).length} run(s) · ${online} node(s) online · ` +
    `protocol v${snapshot.protocol} · server ${snapshot.server_version}`;
  renderRuns(snapshot);
  renderFindings(snapshot);
  renderNodes(snapshot);
}

/* ── add form ────────────────────────────────────────────────────────── */

$('addToggle').addEventListener('click', () => $('addForm').classList.toggle('open'));
$('addCancel').addEventListener('click', () => $('addForm').classList.remove('open'));

$('addForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const params = {
    num_points: Number($('f_points').value),
    rate_hz: Number($('f_rate').value),
    seed: Number($('f_seed').value),
    reliability: $('f_reliability').value,
    qos_depth: Number($('f_depth').value),
  };
  try {
    await call('POST', '/runs', { label: $('f_label').value.trim(), params });
    $('addForm').classList.remove('open');
    $('f_label').value = '';
  } catch { /* the banner already says why */ }
});

startPolling();
connect();
