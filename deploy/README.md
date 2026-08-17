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

To run the components one per terminal instead of all at once — and for
database access, log access, restarts, and retention — see
[docs/guides/manual-operation.md](../docs/guides/manual-operation.md).

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

## What is this host running?

```bash
make version
```

Run it on any host, after any deploy. It reports the role, version and commit,
submodule pins, each running container with the commit its image was built
from, and PTP presence. It reads git, compose and the containers themselves —
never a file someone had to remember to update.

The line to watch for:

```
WARNING: a running image was built from a different commit than this checkout.
```

That is a host that was pulled but not redeployed, or redeployed from a stale
image. Measurements taken in that state cannot be attributed to the source in
front of you.

`deploy.sh` prints this report at the end of every deploy, and — because it
rsyncs without `.git` — leaves a `.deployed-version` stamp so a push-deployed
host can still answer. Full release and versioning story:
[RELEASING.md](../RELEASING.md).

## Published images (GHCR)

Every push to `main` that passes the test jobs publishes both images to
GitHub Container Registry:

```
ghcr.io/morosev/mec-cast-ros:main      ghcr.io/morosev/mec-cast-ran:main
ghcr.io/morosev/mec-cast-ros:sha-<7>   ghcr.io/morosev/mec-cast-ran:sha-<7>
```

The repository is public, so lab hosts pull without credentials.

**Why this matters beyond convenience.** `deploy.sh` builds on each host, so
the UE and the edge can end up running images that differ in base-image
digest or package versions — an uncontrolled variable sitting underneath
every measurement. Pulling one published digest removes it, and replaces a
~1.4 GB build per host with a pull.

Pin a run to an exact image rather than tracking `:main`:

```bash
docker pull ghcr.io/morosev/mec-cast-ros:sha-1a2b3c4
```

```bash
docker tag ghcr.io/morosev/mec-cast-ros:sha-1a2b3c4 mec-cast-ros
```

The compose files reference the local tag `mec-cast-ros`, so retagging is
all that is needed — no compose edits, and `deploy.sh` still works unchanged
for anyone who prefers to build.

No setup is needed. Because the workflow publishes with `GITHUB_TOKEN` from
a public repository, the packages inherit public visibility — verified by
pulling all four tags with `docker logout ghcr.io` first, which is exactly
what a lab host does.

## Why compose and not Kubernetes

Four hosts, each running one or two containers, operated by the people who
wrote them. Compose plus `rsync` is debuggable at 2am in the lab; a control
plane is not. Revisit if the host count grows past a handful or if the
platform ever needs to survive unattended.
