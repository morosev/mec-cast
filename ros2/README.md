# ROS2 workspace (Profile A)

A single colcon workspace holding all three ROS2 packages. **It is never
built on the host** — ROS2 runs only in containers
(`deploy/docker/ros.Dockerfile`), because the development machine is WSL
Ubuntu 25.x and ROS2 Jazzy targets 24.04.

| Package | Role | Runs on |
|---|---|---|
| `mec_cast_msgs` | `TimingEnvelope`, `CloudWithTelemetry` | both |
| `mec_cast_lidar_client` | Synthetic/real point-cloud source | UE |
| `mec_cast_edge` | Zenoh ingest, stamps arrival, computes latency | edge |

## Why one workspace across two deployment locations

The top level of this repo is organised by deployment location, and these
packages break that pattern deliberately. Splitting them would need
`colcon build --base-paths clients/… edge/… shared/…`, which fights rosdep,
ament resource indexing, and every ROS tutorial a new collaborator will
read. The packages are *named* by role; which one runs where is expressed
in `deploy/lab/compose.{ue,edge}.yml`. One documented exception beats a
clever layout that breaks default tooling.

## Transport

`rmw_zenoh_cpp`, not DDS — see
[ADR-0001](../docs/architecture/adr/0001-zenoh-over-dds.md). The
application code is vanilla ROS2; only the middleware differs.

The timing envelope rides **in-band** as a field of `CloudWithTelemetry`
rather than as a Zenoh attachment, because `rmw_zenoh` does not expose
per-publish attachments to the application layer.

## Test vectors

`mec_cast_lidar_client` generates deterministic clouds from a seed:

| Parameter | Default | Purpose |
|---|---|---|
| `seed` | 42 | Reproducible contents |
| `num_points` | 30000 | Payload size — the primary sweep variable |
| `rate_hz` | 10.0 | Publish rate |
| `pattern` | `uniform_cube` | `uniform_cube` or `rotating_plane` |

Fixing the seed across a size sweep means only the size varies.

## Tests

```bash
make test-ros2      # builds the image, runs colcon test in-container
```

`mec_cast_edge/test/test_pipeline_launch.py` is a `launch_testing` case:
it starts the router, client, and edge, then asserts messages flow, `seq`
is gapless, and `recv_ns > send_ns` over the active RMW.
