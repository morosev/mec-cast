# The admin service

`mec-cast-admin` owns run lifecycle. Nodes subscribe to it over WebSocket on
startup, report status on every change, and take start and stop commands; an
operator page shows the run table and says what to do when the workflow is not
established.

It is additive. **A node with no `ADMIN_URL` behaves exactly as it always
has** — one Recorder built at startup from the environment's `RUN_ID` — and
`tests/e2e/test_e2e_latency.py` is the regression test that keeps it so.

Why it exists: [ADR-0007](../architecture/adr/0007-websocket-control-plane.md).
How runs are named and stored: [ADR-0008](../architecture/adr/0008-run-identity-and-store.md).

- [Running it](#running-it)
- [The run table](#the-run-table)
- [Run states](#run-states)
- [Diagnostics](#diagnostics)
- [The declared topology](#the-declared-topology)
- [The protocol](#the-protocol)
- [Where runs are stored](#where-runs-are-stored)
- [Security posture](#security-posture)

## Running it

Local, with the whole Profile A topology:

```bash
make up-admin
```

Then open `http://localhost:8099/admin`. No `RUN_ID` is needed — the admin
mints one per run.

In the lab the admin runs on the **edge** host, brought up by the existing role:

```bash
bash deploy/lab/deploy.sh edge <user@host>
```

The UE and gNB roles dial it at `ws://${EDGE_HOST}:8099/ws/node`. The
documented start order `infra → edge → gnb → ue` already brings it up before
the nodes that dial it, and a node that starts first simply retries every 30 s.

## The run table

| Column | Meaning |
|---|---|
| `#` | Monotonic run number, for talking about a run out loud |
| Run | Last eight characters of the id; click to copy the whole thing |
| Status | The state machine's current state |
| Participants | client / edge / gnb / render counts, red when a *required* role is missing |
| Findings | Count of errors currently detected for the active run |

**Add run** creates a run in `draft` with the workload it will carry —
`num_points`, `rate_hz`, `seed`, `reliability`, `qos_depth`. Those travel to
the nodes with `run.start`, so a run records the settings it actually used
rather than whatever the containers happened to be started with.

**Remove** takes the row out of the table. It never deletes measurement data:
`runs/<run_id>/` and its CSVs stay exactly where they are.

Buttons are enabled from an `allowed` list the server sends with each row. The
page never decides for itself, so what you can press and what the service will
accept cannot disagree.

## Run states

```
draft ──start──► starting ──quorum──► running ◄──recovered──► degraded
                    │                    │                       │
                    │ timeout            │ stop                  │ stop
                    ▼                    ▼                       ▼
                 failed              stopping ──reports──► stopped
```

| State | Meaning | Buttons |
|---|---|---|
| `draft` | Created, never started | Start, Remove |
| `starting` | Command sent, waiting for participants | Stop |
| `running` | At least one client and one edge are recording | Stop |

Quorum is one client and one edge. The gNB and the renderer are both
optional — a run with no RAN KPIs, or with nobody watching, is a legitimate
run, so neither absence degrades it. A renderer that is present but starved
*is* a fault: `WF_RENDER_STARVED`, whose remedy names the default that causes
it, since the edge's `publish_result` is off unless asked for.
| `degraded` | A participant went silent; the rest keep recording | Stop |
| `stopping` | Stop sent, waiting for the nodes to let go | — |
| `stopped` | Finished cleanly | Remove |
| `failed` | Never reached quorum, or everything went offline | Remove |

Two rules worth knowing:

- **One non-terminal run at a time.** A second start is refused with a message
  naming the run in the way. This falls out of one active Recorder per node
  process; see ADR-0007.
- **A stopped run never restarts.** Restarting would append a second
  experiment into the first run's CSV, since the recorder appends by design.

A node that dies mid-run puts the run in `degraded`. If it comes back it
rejoins the same run and appends to the same CSV, which is what the append
behaviour in `telemetry/src/recorder.rs` exists for. If everything goes offline
for 30 s, the run fails.

## Diagnostics

Findings are derived on every pass and never stored, so a condition that clears
simply stops being reported. Each carries a remedy — a diagnostic that says
something is wrong without saying what to do costs attention and returns
nothing.

| Code | What it means |
|---|---|
| `WF_EDGE_ABSENT` | Run active, no edge connected |
| `WF_CLIENT_ABSENT` | Run active, no client connected |
| `WF_EDGE_IDLE` | Edge connected but not subscribed |
| `WF_NO_PEER` | Client publishing, edge sees no publisher |
| `WF_QOS_MISMATCH` | Publisher and subscriber `reliability` differ |
| `WF_NO_FRAMES` | Client's frame count rising, edge's flat |
| `WF_GNB_ABSENT` | No gNB collector — a warning; the run is still valid |
| `WF_GNB_SILENT` | Collector bound but srsRAN is sending nothing |
| `WF_RUN_MISMATCH` | A node is recording a different run |
| `WF_RENDER_CROSS_HOST` | A renderer runs on a host with no lidar client — its `e2e_ns` is no longer the PTP-free round trip of ADR-0009, but an ordinary cross-host figure valid only under a reliable PTP lock |
| `WF_VERSION_SKEW` | A node is on a different commit from the admin |
| `WF_PARTICIPANT_LOST` | A participant stopped answering mid-run |
| `WF_LOGGING_UNREACHABLE` | Snapshots are not reaching the logging service |
| `WF_TOPOLOGY_UNDECLARED` | A node connected that the declared topology does not list |
| `WF_TOPOLOGY_MISSING` | A declared node has never connected |
| `WF_TOPOLOGY_CELL_MISMATCH` | A node reports a different cell from the one it is declared in |

The three `WF_TOPOLOGY_*` findings only appear once a topology is declared —
see below. Without one they are silent, which is the point: an undeclared
fleet is not a wrong fleet.

`WF_QOS_MISMATCH` and `WF_NO_FRAMES` are the failure modes the node docstrings
already call out as silent: both produce zero frames, zero errors, and a
full-length run discovered worthless at analysis time. This is the first thing
in the platform that can detect either.

## The declared topology

By default the admin knows the *rules* — one client and one edge for quorum,
a warning for a missing gNB, silence for a missing renderer — but not the
*fleet*. Declaring the fleet is opt-in:

```bash
cp deploy/lab/topology.example.yml deploy/lab/topology.yml
```

Edit it, restart the admin. Both compose files mount `deploy/lab` read-only
at `/etc/mec-cast`, so no other configuration is needed; point
`MECADM_TOPOLOGY_PATH` elsewhere if the service runs outside a container.

What declaring buys:

| Situation | Finding |
|---|---|
| A node connects that is not listed | `WF_TOPOLOGY_UNDECLARED` |
| A listed node never connects | `WF_TOPOLOGY_MISSING` |
| A node reports a cell other than its declared one | `WF_TOPOLOGY_CELL_MISMATCH` |

The first is the one that earns the file. A leftover container from an
earlier experiment can satisfy quorum and quietly join a run, and nothing
else in the platform would say so.

The file is read **once, at startup**. A topology change is a deployment
change; re-reading it live would let the rules shift under a run that is
already being judged against them.

It also carries optional `roles:` overrides, merged onto the defaults, with
`required` (counts toward quorum) and `absence` (error / warn / null)
separate. They have to be separate: the gNB is *not* required — a run with
no RAN KPIs is a real run — yet its absence is still worth reporting, since
it usually means the collector failed to start rather than that nobody
wanted RAN data.

**One source, three readers.** The role rules drive the quorum check, the
`WF_*_ABSENT` findings and the page's role chips. They used to be written
out separately in `orchestrator.py`, `workflow.py` and `admin.js`, with
nothing keeping them in agreement.

**On the page.** A declared topology adds a card listing every declared node
with a live online/absent marker, and the same thing as mermaid source in a
fold — copy-pasteable into any markdown file. The card is hidden entirely
when nothing is declared. The page renders no diagram itself: it has no
build step and loads no third-party script, and a mermaid bundle is a steep
price for a picture that is already readable as text.

## The protocol

JSON over WebSocket, one versioned envelope in both directions:

```json
{"v": 1, "type": "hello", "msg_id": "…", "ts_ns": 1712345678901234567,
 "node_id": "edge-mec01-0", "payload": { }}
```

`ts_ns` is CLOCK_REALTIME nanoseconds — the same clock the recorder stamps
samples with, so admin events and measurements share one timeline. `node_id` is
`<node_type>-<hostname>-<instance>`, stable across restarts, which is what makes
reconnection idempotent and lets an operator address one node out of many.

| Direction | Types |
|---|---|
| node → admin | `hello`, `status`, `ack`, `pong`, `goodbye` |
| admin → node | `welcome`, `command`, `ping`, `error` |

Commands are `run.start`, `run.stop`, `stream.start`, `stream.stop`,
`status.report`.

`hello` carries an optional `cell`, set from the node's `CELL` environment
variable or its `cell` parameter. Empty means the node did not say, which is
every deployment that has not declared a topology. Adding it needed **no
version bump**: payloads ignore unknown fields (`extra="ignore"`), so a node
that predates the field talks to a newer admin and vice versa. Envelopes are
the strict part — a different `v` is still rejected.

| Behaviour | Value |
|---|---|
| Keep-alive ping | every 10 s |
| Offline after | 30 s of silence |
| Reconnect attempt | every 30 s, ±10% jitter |

Any inbound frame counts as liveness, so a node sending frequent status need
not answer pings separately. An envelope on a different `v` is answered with an
`error` frame naming the supported version, then closed — visible in both logs
rather than a silent drop.

**Three implementations, one fixture.** The service
(`services/admin/src/mec_cast_admin/protocol.py`), the Python node client
(`ros2/src/mec_cast_admin_client/`) and the Rust node client
(`ran/collector/src/admin.rs`) are separate implementations. All three test
suites read `services/admin/tests/vectors.json`, so they cannot drift apart in
silence. **Change that file and all three when the protocol changes.**

## Where runs are stored

No database. Each run is `runs/<run_id>/run.json` — the same manifest
`scripts/run-experiment.sh` writes, with additive fields — plus an append-only
`runs/admin-journal.jsonl` of state transitions. On startup the admin globs the
manifests and replays the journal, so the table survives a restart and still
works when PostgreSQL is down.

Run ids are UUIDv7: still UUIDs, so `run_trace_id()` and the 16-byte envelope
`trace_id` are unaffected, but time-ordered so runs sort chronologically
everywhere.

**In the lab the manifest lives on the edge host while each node's CSVs live on
its own machine.** That was already true and simply unrecorded; the admin now
writes a `sites` map naming which host holds which directory.

Both the admin and `run-experiment.sh` write `run.json`. The `source` field
says which produced it.

## Security posture

**No authentication, no rate limiting.** Anyone who can reach port 8099 can
start and stop experiments — a wider blast radius than the logging service's
read-mostly exposure. Bind it to the management LAN only, never to a
UE-reachable or public interface.
