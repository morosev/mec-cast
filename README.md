# mec-cast

A measurement platform for **5G industrial communication**. Its value is not
any single transport but the **PTP-grade, per-frame latency measurement
discipline** applied across transports.

Two profiles share one Rust telemetry spine:

- **Profile A — robotics:** ROS2 LiDAR point clouds over `rmw_zenoh`, from a
  UE (robot compute + 5G modem) across an srsRAN/Open5GS lab network to a
  MEC edge server, plus an srsRAN MAC metrics tap.
- **Profile B — media:** WebRTC. Today the legacy libwebrtc client; the
  planned retarget onto [str0m](docs/architecture/str0m-profile.md) removes
  the need for a patched WebRTC fork.

```
 ROBOT / UE                       5G RAN + CORE (lab)             EDGE (MEC server)
┌─────────────────────┐                                         ┌─────────────────────────┐
│ LiDAR → ROS2 node   │   Uu    ┌────────┐   ┌─────────┐        │ edge node (rmw_zenoh)   │
│  capture_ns stamp   ├─5G modem┤ srsRAN ├───┤ Open5GS ├─UPF/N6─┤  recv_ns stamp          │
│  send_ns stamp      │ (USRP)  │ gNB    │   │  core   │        │  process → done_ns      │
└─────────┬───────────┘         └───┬────┘   └─────────┘        └────────────┬────────────┘
          │                         │ metrics UDP/JSON                       │
          │                   ran-collector ─────────────────────────────────┤
          │        PTP (management LAN, NOT the 5G user plane)               ▼
          └─────────────────────────────────────────────────────  logging service + CSV
```

## Quick start

```bash
bash scripts/bootstrap-dev.sh   # rust, venv, wheel, submodules
make test                       # fast tests, no docker
make test-all                   # adds containers + full netem e2e
```

```bash
bash scripts/run-experiment.sh -d 60 -n 30000 -t "baseline"
```

Run `make help` for all targets.

## Components

| # | Component | Location |
|---|---|---|
| 1 | **Client components** (UE side) | [`clients/`](clients/README.md) |
| 1.i | ROS2 lidar client node | [`ros2/src/mec_cast_lidar_client/`](ros2/README.md) |
| 1.ii | WebRTC native client | [`clients/webrtc_native/`](clients/webrtc_native/README.md) |
| 2 | **MEC server components** | [`edge/`](edge/README.md) |
| 2.i | Zenoh ingest layer for ROS2 | [`ros2/src/mec_cast_edge/`](ros2/README.md) |
| 2.ii | str0m SFU (planned) | [design](docs/architecture/str0m-profile.md) |
| 3 | **Telemetry** — the shared spine | [`telemetry/`](telemetry/README.md) |
| 4 | **Logging service** (submodule) | [`services/logging/`](docs/operations/logging-submodule.md) |
| 5 | **Third-party, extended** | [`third_party/`](third_party/README.md) — [webrtc](docs/guides/building-libwebrtc.md), [str0m](docs/architecture/str0m-profile.md) |
| — | RAN metrics tap | [`ran/collector/`](ran/collector/README.md) |
| — | Deployment | [`deploy/`](deploy/README.md) |

The top level is organised by **deployment location**, so the tree mirrors
where code runs in the testbed. `ros2/` is one deliberate exception: it is a
single colcon workspace spanning client and edge, because splitting it
fights rosdep and ament for cosmetic gain.

## Build system

Five toolchains (cargo, maturin, npm, colcon, gn+ninja) — no single build
tool owns them. The [`Makefile`](Makefile) is a thin façade that only
*delegates*; build logic stays in each component's native tool, and CI calls
the same targets so local and CI cannot drift.

`make build-libwebrtc` is opt-in: ~20 GB and hours, never in CI.

## Documentation

[`docs/`](docs/README.md) holds cross-cutting material; component docs live
next to their code. Start with
[architecture/overview.md](docs/architecture/overview.md), and read the
[ADRs](docs/architecture/adr/README.md) before proposing to change a major
design decision — they record why Zenoh beat DDS, why the telemetry core is
Rust, why PTP stays off the 5G user plane, why percentiles are exact, and
why there is no RIC yet.

## Status

| Area | State |
|---|---|
| Telemetry crate (+ PyO3, C ABI) | Working, tested |
| ROS2 + Zenoh profile | Working; netem e2e green |
| WebRTC profile → telemetry | Wired over the C ABI; needs a camera to confirm |
| RAN metrics tap | Working against a captured fixture |
| Logging service submodule | Wired at `services/logging` |
| str0m fork vendored | `third_party/str0m` (v0.21.0) |
| str0m SFU implementation | Not started — [design](docs/architecture/str0m-profile.md) |
| Draco compression | Not started |

## Development note

AI coding tools were used in generating and restructuring parts of this
project — including source, tests, deployment configuration, and
documentation. All of it is reviewed and validated by the maintainers
before use, and the test suites described above are the evidence: the
telemetry statistics, the ROS2 pipeline, and the end-to-end latency path
are covered by automated tests rather than accepted on trust.

Measurement results should be judged on the reproducibility artifacts —
`runs/<run_id>/run.json`, the per-frame CSV, and PTP sync status — not on
the provenance of the code that produced them.

## License

MIT — see [LICENSE](LICENSE).
