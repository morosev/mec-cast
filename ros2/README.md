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
| `mec_cast_admin_client` | WebSocket client, shared node base (`MecCastNode`), site codes | all |
| `mec_cast_render` | Draws the edge's result, measures the round trip (ADR-0009) | UE |
| `mec_cast_ue` | UE agent: N lidar + M render instances in one process | UE |

## The return path

`mec_cast_edge` is a terminal consumer by default. With `publish_result:=true`
it republishes its voxel-downsampled result on `mec_cast/result`, and
`mec_cast_render` draws it on the UE.

That is worth more than a picture. The returned envelope carries the original
`capture_ns` forward, and the renderer stamps `process_done_ns` on the **same
host that captured the frame** — so its `e2e_ns` is a round trip measured on
one clock, and the only latency figure here that does not depend on PTP. See
[ADR-0009](../docs/architecture/adr/0009-render-return-path.md).

Both halves are off by default: `publish_result` is false, and the renderer's
`sink` is `null` (measures everything, draws nothing). Set `sink:=rerun` to
see the cloud, or `sink:=ros` to republish plainly for RViz2 or Foxglove.

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

## `mec_cast_admin_client`

The control-plane client both nodes share, the way they already share
`mec_cast_msgs`. A background thread owns the WebSocket and moves dicts
through queues; **it never touches rclpy**, and commands are applied from a
normal timer on the executor thread. That keeps `rclpy.spin` single-threaded —
no MultiThreadedExecutor, no callback groups.

New parameters on both nodes:

| Parameter | Default | Meaning |
|---|---|---|
| `admin_url` | `$ADMIN_URL`, else empty | Empty = standalone, unchanged behaviour |
| `admin_autostart` | client `false`, edge `true` | Join the active run on connect |
| `admin_instance` | `0` | Distinguishes several nodes of one type per host |

With an admin, the Recorder is built **per run** rather than per process:
`start_run` creates it and the timer or subscription, `stop_run` destroys them
in that order so nothing fires into a shut-down recorder. See
[docs/operations/admin-service.md](../docs/operations/admin-service.md).
