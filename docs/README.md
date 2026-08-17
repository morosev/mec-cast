# mec-cast documentation

Cross-cutting material only. **Component-specific docs live next to the
code** — that is what keeps them from rotting:

| Component | Its README |
|---|---|
| Telemetry crate | [telemetry/](../telemetry/README.md) |
| ROS2 packages | [ros2/](../ros2/README.md) |
| Client components | [clients/](../clients/README.md) |
| MEC server components | [edge/](../edge/README.md) |
| RAN metrics tap | [ran/collector/](../ran/collector/README.md) |
| Third-party forks | [third_party/](../third_party/README.md) |
| Deployment | [deploy/](../deploy/README.md) |
| Releasing (legacy client only) | [RELEASING.md](../RELEASING.md) |

## I want to…

**understand the system** → [architecture/overview.md](architecture/overview.md)
— the two profiles, the telemetry spine, data routing, clock model.

**know why something is the way it is** → [architecture/adr/](architecture/adr/README.md)
— why Zenoh and not DDS, why Rust, why PTP off the user plane, why exact
percentiles, why no RIC yet. Read these before proposing to change any of it.

**trust a number** → [architecture/timing-model.md](architecture/timing-model.md)
— what each metric measures, and precisely when it is valid.

**work on the media profile** → [architecture/str0m-profile.md](architecture/str0m-profile.md)
— the planned str0m SFU, its wire contract, and the migration plan.

**see or edit a diagram** → [diagrams/](diagrams/README.md) — editable
Mermaid sources for the architecture and lab-deployment overviews, plus two
detailed data-flow diagrams (measurement lifecycle and runtime topology).

**run components by hand, reach the database, or maintain a deployment** →
[guides/manual-operation.md](guides/manual-operation.md) — one container per
terminal, docker/compose vocabulary, psql and pgAdmin access, logs, restarts,
retention and backup. Dev and lab.

**run an experiment** → [guides/running-an-experiment.md](guides/running-an-experiment.md)

**set up the lab** → [operations/lab-topology.md](operations/lab-topology.md)
and [deploy/lab/ptp/](../deploy/lab/ptp/README.md)

**work with the logging submodule** → [operations/logging-submodule.md](operations/logging-submodule.md)

**build the WebRTC fork** → [guides/building-libwebrtc.md](guides/building-libwebrtc.md)
(20 GB, hours — opt-in, never in CI)

## Layout

```
architecture/   how the system is built, and why (incl. ADRs)
guides/         task-oriented how-tos
operations/     running the lab: topology, runbooks, troubleshooting
research/       experiment protocol, results log, paper notes
```
