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
| Edge services (logging, admin) | [services/](../services/README.md) |
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

**deploy, or update a deployment** →
[operations/deploy-manual.md](operations/deploy-manual.md) — prerequisites,
per-machine setup, local and lab, the full lab command set, upgrading to a
release.

**operate a deployment that exists** →
[operations/admin-manual.md](operations/admin-manual.md) — a cheat sheet, what
`up`/`down` do, logs, container access, psql and pgAdmin, backup and restore,
retention, troubleshooting.

**run components by hand on a laptop** →
[guides/local-development.md](guides/local-development.md) — one container per
terminal, tmux, the renderer.

**run an experiment** → [guides/running-an-experiment.md](guides/running-an-experiment.md)

**set up the lab** → [operations/lab-topology.md](operations/lab-topology.md)
and [deploy/lab/ptp/](../deploy/lab/ptp/README.md)

**work with the logging submodule** → [operations/logging-submodule.md](operations/logging-submodule.md)

**build the WebRTC fork** → [guides/building-libwebrtc.md](guides/building-libwebrtc.md)
(20 GB, hours — opt-in, never in CI)

## Layout

```
architecture/   how the system is built, and why (incl. ADRs)
guides/         task-oriented how-tos — "I want to do X"
operations/     running a deployment: deploy, administer, recover
research/       experiment protocol, results log, paper notes
diagrams/       mermaid sources and the rendered artifacts
slides/         the generated deck
```

### What goes where

The rule, so a new page has one obvious home and duplication has nowhere to
hide. Every row's **Never** column is the one that does the work.

| Location | Answers | Never contains |
|---|---|---|
| Root `README.md` | What is this, why it exists, a 5-minute quickstart, where to go next | Operational detail, per-component build steps |
| Component `README.md` | What this component is, how to build and test **it**, its public surface | Cross-component workflow, deployment |
| `architecture/` | How it is built and **why** — ADRs record decisions expensive to revisit | How-to steps |
| `guides/` | Task-oriented: *I want to do X* | Fleet operations |
| `operations/` | Running a deployment: deploy, administer, recover | Rationale — link the ADR instead |
| `research/` | Experiment protocol, results log, paper notes | Anything about the code |

Two pairs are deliberately split rather than merged, because reference and
procedure rot at different rates: `operations/admin-service.md` is the control
plane's **reference** (protocol, states, findings) while
`operations/admin-manual.md` is the **procedures**; `operations/lab-topology.md`
is the lab's **reference** (roles, hosts, addressing) while
`operations/deploy-manual.md` is how to deploy it.
