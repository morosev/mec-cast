# ADR-0007: A WebSocket control plane for run orchestration

- **Status:** Accepted
- **Date:** 2026-08-19
- **Amended:** 2026-08-30 — the one-run-at-a-time limit is per cell; see
  Consequences.

## Context

A measurement run is currently a shell variable. `scripts/run-experiment.sh`
mints a `RUN_ID`, exports it, and `docker compose up` bakes it into every
container's environment. Each node reads `os.environ["RUN_ID"]` once at
construction, builds a `tel.Recorder` around it, and records until the
container stops.

That is workable for one operator on one box and does not survive the lab.
Starting a run means a shell on the right host in the documented order
(`infra → edge → gnb → ue`); there is no registry of what is running, no way
to stop a run without stopping containers, and — the expensive part — no
signal when the pipeline is silently broken. The two failure modes named in
the node docstrings, a `reliability` QoS mismatch and a publisher with no
subscriber, both produce zero frames, zero errors and a full-length run that
is discovered worthless at analysis time.

The gNB collector is not a ROS node and never will be, so the control path
cannot be a ROS2 service or action. It must reach a Rust process on a host
where the ROS graph deliberately does not extend.

## Decision

Run lifecycle is owned by **`mec-cast-admin`**, a first-party FastAPI service
on the edge host, in-repo at `services/admin/`. It speaks **one versioned
JSON protocol over WebSocket** to every node: nodes dial the admin on startup,
retry every 30 s while it is unreachable, exchange 10 s keep-alives, report
status on every change, and send a `goodbye` carrying their final recorder
report on shutdown. The admin serves an operator page with a run table,
per-row start/stop/remove, and derived diagnostics.

**The env-`RUN_ID` path is not removed.** With `admin_url` empty every node
behaves exactly as it does today, and `tests/e2e/test_e2e_latency.py` stays
unchanged as the proof.

Port 8099, HTTP and WebSocket on the same listener.

## Rationale

| Alternative | Why it lost |
|---|---|
| ROS2 services or actions | The gNB collector is not and will not be a ROS node. The control plane must reach hosts the ROS graph does not cover. |
| HTTP polling from nodes | Simpler, but gives no server-initiated stop and no liveness signal — and liveness is most of the value. |
| Extend the logging service | A submodule with a deliberately frozen `extra="forbid"` schema and a read-mostly design. Control traffic does not belong in a log sink. |
| MQTT or Zenoh pub-sub | Adds a broker, and worse, would put control traffic on the very link under measurement. The admin belongs on the management LAN for the same reason PTP does (ADR-0003). |
| A second git submodule | Every protocol change touches the service and all three clients. A submodule makes that atomic commit impossible. |

WebSocket over plain HTTP because the server must initiate; JSON over a binary
codec because the control plane is low-rate and being able to read a frame in a
log is worth more than its bytes; versioned because the Python and Rust clients
ship on different cadences and a silent mismatch is the failure this whole
record exists to prevent.

## Consequences

- **Nodes hold a Recorder per run, not per process.** `start_run` builds one,
  `stop_run` shuts it down. This is safe — `telemetry/src/py.rs` has no global
  state — but the "One `Recorder` per process" docstring must be amended to
  "one *active* Recorder".
- **One non-terminal run at a time, platform-wide.** It falls directly out of
  one active Recorder per node process. Lifting it means concurrent Recorders
  and an ambiguous `runs/<id>/<site>/` layout.

  **Amended 2026-08-30: the limit is now per cell, not platform-wide.** The
  reasoning above survives intact — this is a change of scope, not a reversal.
  A node belongs to exactly one cell and each cell has at most one active run,
  so there is still one active Recorder per node process. The layout objection
  was independently removed first: directories are instance-suffixed
  (`pub-0`, `pub-1`) and `run.sites` is keyed by `node_id` rather than by a
  constant per-type site string, so `runs/<id>/` is unambiguous with any number
  of runs or instances. Both consequences this bullet warned about were
  therefore addressed before the limit was lifted, which is why the decision
  needed amending rather than replacing.
- **A stopped run is never restarted.** Restarting would append a second
  experiment into the first run's CSV.
- **No authentication.** Anyone who can reach 8099 can start and stop
  experiments. That is a step up from the logging service's read-mostly
  exposure and the same posture: bind to the management LAN, never to a
  UE-reachable interface.
- The protocol version is a contract between three implementations. Changing
  it means changing all of them in one commit — which is why the service is
  in-repo.
- `websockets >= 12` enters the ROS image; apt ships 10.4, so it is a pip
  install. `tungstenite` (synchronous, no TLS) enters `Cargo.lock` behind a
  default-on `admin` feature, adding no async runtime.
