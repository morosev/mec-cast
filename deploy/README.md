# Deployment

Three environments, one mechanism (docker compose), different composition.

| Environment | What | Entry point |
|---|---|---|
| **local** | Everything on one box, netem-impaired | `make up-local` |
| **lab** | Four real hosts across the 5G testbed | `bash deploy/lab/deploy.sh <role> <user@host>` |
| **ci** | Subset — no radio, no libwebrtc | `.github/workflows/platform.yml` |

## Layout

```
docker/          Dockerfiles + the ROS image entrypoint and zenoh configs
  ros.Dockerfile   telemetry wheel (stage 1) + ROS2 Jazzy + colcon (stage 2)
  ran.Dockerfile   srsRAN metrics collector
compose/         Local topology
  local.yml        zenoh router, lidar client, netem sidecar, edge
  logging.yml      logging service + postgres
lab/             Per-role compose files for the real testbed
  compose.{ue,edge,infra,gnb}.yml
  deploy.sh        rsync + build + up, per role
  ptp/             ptp4l/phc2sys units, config, and verification
```

## Local

```bash
make up-local        # RUN_ID is generated if unset
make logs
make down
```

Knobs: `RUN_ID`, `NETEM_DELAY`, `NETEM_JITTER`, `NETEM_LOSS`, `NUM_POINTS`,
`RATE_HZ`, `SEED`.

The `netem` sidecar shares the lidar client's network namespace and impairs
its egress. That models the 5G uplink leg without touching host networking,
which is what lets the whole topology run unprivileged on a laptop.

## Lab

See [docs/operations/lab-topology.md](../docs/operations/lab-topology.md).
Deploy `infra` first. `RUN_ID` must be identical across all roles — it is
the `trace_id` that correlates UE, edge, and RAN records for one experiment.

## Why compose and not Kubernetes

Four hosts, each running one or two containers, operated by the people who
wrote them. Compose plus `rsync` is debuggable at 2am in the lab; a control
plane is not. Revisit if the host count grows past a handful or if the
platform ever needs to survive unattended.
