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
| `WF_VERSION_SKEW` | A node is on a different commit from the admin |
| `WF_PARTICIPANT_LOST` | A participant stopped answering mid-run |
| `WF_LOGGING_UNREACHABLE` | Snapshots are not reaching the logging service |

`WF_QOS_MISMATCH` and `WF_NO_FRAMES` are the failure modes the node docstrings
already call out as silent: both produce zero frames, zero errors, and a
full-length run discovered worthless at analysis time. This is the first thing
in the platform that can detect either.

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
