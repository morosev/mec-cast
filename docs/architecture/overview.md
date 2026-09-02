# mec-cast Platform Architecture

mec-cast is an **experimentation testbed for industrial communication over
private 5G**, built on srsRAN and Open5GS. It runs real workloads across a
real radio network so that design choices can be tested rather than argued.

It is used to investigate large data transmission in industrial systems,
minimal latency for teleoperation, and fast communication between nearby
peers and edge processing. Supporting that: reproducible workloads, RAN-level
observability, transport comparison under identical conditions, controlled
impairment, and PTP-disciplined timing across hosts — **timing is the
instrument that makes the other findings trustworthy, not the purpose.**

Two workload profiles share one telemetry spine:

- **Profile A — robotics (ROS2 + Zenoh):** LiDAR point clouds from a UE
  (robot compute + 5G modem) across an srsRAN/Open5GS lab network to a MEC
  edge server.
- **Profile B — media (WebRTC):** today the legacy libwebrtc client
  (`clients/webrtc_native/`, `edge/signaling/`, `third_party/webrtc/`); planned retarget onto str0m
  (see [str0m-profile.md](str0m-profile.md)).

```
 ROBOT / UE                       5G RAN + CORE (lab)             EDGE (MEC server)
┌─────────────────────┐                                         ┌──────────────────────────┐
│ LiDAR → ROS2 node   │   Uu    ┌────────┐   ┌─────────┐        │ edge node (rmw_zenoh)    │
│  capture_ns stamp   ├─5G modem┤ srsRAN ├───┤ Open5GS ├─UPF/N6─┤  recv_ns stamp           │
│  (compress: later)  │ (USRP)  │O-DU/CU │   │  core   │        │  process → done_ns stamp │
│  send_ns stamp      │         └───┬────┘   └─────────┘        │  ├─► runs/<id>/samples.csv
│  rmw_zenoh publish  │             │ metrics UDP/JSON          │  └─► snapshots → logging │
│                     │                                         │                          │
│  render node        │◄─── mec_cast/result (ADR-0009) ─────────┤  publish_result          │
│  process_done_ns    │     round trip: ONE clock, no PTP       │  → voxel cloud back down │
└─────────┬───────────┘             ▼                           └────────────┬─────────────┘
          │                  ran-collector ──────────────────────────────────┤
          │                                                                  ▼
          │        PTP, management/backhaul LAN — NOT the 5G user      logging service
          └──────  plane. Every host on the SAME grandmaster           (FastAPI+Postgres)
```

## Repository layout

Organised by **deployment location**, not by technology — the top level
mirrors where code runs in the testbed.

```
clients/          Client components (run on the UE)
  webrtc_native/    legacy Node + C++ addon over the libwebrtc fork
edge/             MEC server components
  signaling/        WebSocket signaling server (Node)
  (str0m SFU lands here when implemented — Profile B target)
ros2/             Single colcon workspace (see exception below)
  src/mec_cast_msgs/          shared TimingEnvelope + CloudWithTelemetry
  src/mec_cast_lidar_client/  ROS2 client node — runs on the UE
  src/mec_cast_edge/          Zenoh ingest layer — runs on the edge
  src/mec_cast_render/        draws the edge's result — runs on the UE
  src/mec_cast_admin_client/  control-plane WebSocket client — all nodes
telemetry/        Shared Rust crate + PyO3 bindings — the spine
ran/collector/    O-DU MAC scheduler metrics tap
services/logging/ Logging service (submodule)
services/admin/   Admin service: run orchestration (in-repo)
third_party/      Vendored forks, excluded from workspace + build contexts
  webrtc/src        forked libwebrtc (submodule) + gclient glue
  str0m             forked str0m (submodule) — sans-IO WebRTC library
deploy/           Dockerfiles, compose topologies, lab roles, PTP configs
tests/            Cross-component e2e; legacy shell harness
scripts/          bootstrap, release, experiment runner
docs/             This tree
```

**Why `ros2/` is one workspace even though it spans client and edge:**
splitting it would require `colcon build --base-paths …` across three
locations and fights rosdep, ament resource indexing, and every ROS
tutorial a new collaborator will read. The packages are *named* by role;
which one runs where is a launch and container concern, expressed in
`deploy/lab/compose.{ue,edge}.yml`. One documented exception beats a
clever layout that breaks default tooling.

## The telemetry spine (`telemetry/`)

Rust crate `mec-cast-telemetry`. Everything depends on it; it depends on
nothing else in the repo.

- **TimingEnvelope** — fixed 64-byte little-endian wire contract
  (`capture_ns, send_ns, recv_ns, process_done_ns, seq, modality,
  trace_id`). Rides as a ROS message field today (`mec_cast_msgs`), as a
  Zenoh attachment or str0m RTP header extension later.
- **DelayStats** — Welford mean/stddev (jitter is emitted), exact
  percentiles over a sliding window (no estimator error under multimodal
  5G/HARQ latency).
- **Clocks** — `RealtimeClock` (the shared time base), `MonotonicClock`,
  `MockClock` for tests, `PhcClock` (`/dev/ptpN`, feature `linux-ptp`).
- **PtpMonitor** — every snapshot carries `{offset_ns, reliable}` so
  analysis filters windows by clock health after the fact. Same-host runs
  honestly report `reliable: false`.
- **Recorder** — the async pipeline: hot-path `try_record()` into a bounded
  lock-free SPSC ring (never blocks, drops are counted), a writer thread
  (per-sample CSV + per-metric stats), an uploader thread (2 s snapshot
  batches to the logging service). Invariant: `written + dropped == pushed`.
- **PyO3 bindings** (`telemetry/python/`) — the Python ROS nodes use the
  same stats engine; there is exactly one implementation.
- **C ABI** (`telemetry/src/ffi.rs`, `telemetry/include/`) — the legacy
  WebRTC C++ addon links the crate as a `staticlib` and records one sample
  per rendered frame, so Profile B lands in the same CSV schema and logging
  service. It runs *alongside* the addon's in-process `DelayMeasurement`,
  which still backs the interactive `delay report` output.

Derived metrics per sample: `network = recv − send`,
`e2e = process_done − capture`, `processing = process_done − recv`,
`sender = send − capture`.

## Data routing

| Data | Destination | Why |
|---|---|---|
| Per-frame samples | `runs/<run_id>/<site>/samples.csv` | firehose; analyzed with pandas; Parquet later |
| 2 s aggregated snapshots | logging service (`service=mec-cast-{pub,edge,ran}`) | queryable in Postgres (`context` JSONB) |
| RAN KPIs | logging service (`service=mec-cast-ran`, `context.kpi`) | joined to app latency by `trace_id` |

`trace_id = RUN_ID` (one UUID per experiment run) joins everything across
publisher, edge, and RAN.

## Control plane

Data flows UE → edge and everything reports to the logging service. Run
*lifecycle* is a separate concern, and since ADR-0007 it has its own path:
`mec-cast-admin` on the infra host, speaking JSON over WebSocket to every node.

```
              ┌──────────── mec-cast-admin (infra, :8099) ───────────┐
              │  run table · state machine · workflow diagnostics    │
              └───▲──────────────────▲───────────────────────▲───────┘
                  │ ws               │ ws                    │ ws
            lidar-client          edge node            ran-collector
               (UE)               (edge)                  (gNB)
```

Nodes subscribe on startup and retry every 30 s, so start order does not
matter. The control plane sits on the management LAN, never on the link under
measurement — the same reasoning that keeps PTP off the user plane (ADR-0003).

A node with no `ADMIN_URL` records under the environment's `RUN_ID` exactly as
before. The admin is additive.

## Clock synchronization

Cross-machine one-way metrics are only meaningful with synchronized clocks:

- **Lab testbed:** every measuring host — UE-compute, edge, and the O-DU
  host — disciplined against **one grandmaster** on the
  **management/backhaul LAN**. `ptp4l` feeds the NIC clock; `phc2sys` or
  chrony with a PHC refclock feeds `CLOCK_REALTIME` from it, and a VM guest
  may take its PHC from `ptp_kvm` instead. The mechanism is per host and does
  not matter; **the shared grandmaster is the invariant**, and it is the one
  thing no per-host check can confirm — see
  [timing-model.md](timing-model.md). The 5G user plane cannot carry sync —
  srsRAN/Open5GS implement no 5G-TSN (DS-TT/NW-TT).
- **Local dev (containers on one host):** all containers share the kernel
  clock, so deltas are valid; `ptp.reliable=false` is recorded honestly.
- Analysis must gate on the recorded `ptp` field, not assume.

## RAN observability

The RAN runs srsRAN Project with the O-RAN functional split — **O-CU**
(RRC / PDCP) and **O-DU** (RLC / MAC / upper PHY). The MAC scheduler lives
in the O-DU, and it is the scheduler that decides when this UE may
transmit — which is why its KPIs are the ones worth correlating against
application latency. The radio is a USRP driven over UHD, not a 7.2
fronthaul O-RU.

- **Phase RAN-1 (implemented):** `ran/collector` binds the UDP socket that
  srsRAN's `metrics: {addr, port}` (gnb.yml) points at, stamps arrivals,
  forwards KPI objects leniently (schema drift-proof) to the logging
  service. Replayable offline from `ran/collector/testdata/`. If the lab
  ever splits O-CU and O-DU into separate processes, that config path moves
  with the DU and the collector's target moves with it.
- **Phase RAN-2 (deferred):** near-RT RIC (FlexRIC) + E2SM-KPM xApp via
  srsRAN's E2 agent for standardized O-RAN KPI subscription; later E2SM-RC
  for scheduling/slicing control experiments.

## Local development topology (no hardware)

Everything ROS runs in containers (`deploy/docker/ros.Dockerfile`, host WSL Ubuntu
is not a ROS2 target platform):

```
zenoh-router ◄── publisher (+ netem sidecar: delay/jitter/loss) ──► edge
                          │                                          │
                          └────────── logging service ◄──────────────┘
```

- The netem sidecar shares the publisher's network namespace — impairing
  its egress models the 5G uplink leg without touching host networking.
- Test vectors are deterministic seeded synthetic clouds
  (`mec_cast_lidar_client`: seed, num_points, rate_hz, pattern) so runs
  are reproducible and payload size is a controlled experiment variable.
  Four patterns — `uniform_cube`, `sphere`, `lidar_scan`, `rotating_plane` —
  voxel-compress between 1.2x and 6.6x, so the shape also selects how much
  the return path carries.
- Test tiers: `cargo test --workspace` (pure logic) → in-container
  `colcon test` (node logic over the active RMW) → host `pytest tests/e2e`
  (full topology + netem + logging assertions).

## Profile independence

`ros2/*`, the future str0m SFU under `edge/`, and `ran/collector` depend on
`telemetry/`; profiles never import each other. Shared artifacts are only:
the envelope wire format, the CSV schema, and the logging `context` shape.
Runs from both profiles land in the same Postgres and the same notebooks,
distinguished by `service` and `modality`.

Vendored third-party forks live under `third_party/` (`webrtc/src`,
`str0m`) and are excluded from both the Cargo workspace and every docker
build context.

The legacy libwebrtc path (`clients/webrtc_native/` + `third_party/webrtc/`)
remains untouched until the str0m profile reaches parity — see
[str0m-profile.md](str0m-profile.md).
