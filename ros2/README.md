# ROS2 workspace (Profile A)

A single colcon workspace holding all seven ROS2 packages. **It is never
built on the host** — ROS2 runs only in containers
(`deploy/docker/ros.Dockerfile`), because the development machine is WSL
Ubuntu 25.x and ROS2 Jazzy targets 24.04.

| Package | Role | Runs on |
|---|---|---|
| `mec_cast_msgs` | `TimingEnvelope`, `CloudWithTelemetry`, `CompressedCloudWithTelemetry` | both |
| `mec_cast_codec` | G-PCC encode/decode through `libtmc3codec` (ADR-0014); no ROS imports | both |
| `mec_cast_lidar_client` | Synthetic/real point-cloud source | UE |
| `mec_cast_edge` | ROS ingest, stamps arrival, computes latency | edge |
| `mec_cast_admin_client` | WebSocket client, shared node base (`MecCastNode`), site codes | all |
| `mec_cast_render` | Draws the edge's result, measures the round trip (ADR-0009) | UE |
| `mec_cast_ue` | UE agent: N lidar + M render instances in one process | UE |

## The return path

`mec_cast_edge` is a terminal consumer by default. With `publish_result:=true`
it republishes its voxel-downsampled result on `mec_cast/result`, and
`mec_cast_render` draws it on the UE.

Both compose topologies set it through the **`PUBLISH_RESULT` environment
variable** rather than this parameter, because a ROS parameter is typed:
`-p publish_result:=1` is an INTEGER against a BOOL default and the node exits
at `declare_parameter`. The environment variable accepts `1` or `true`, in any
case. The parameter form above is for driving the node by hand.

That is worth more than a picture. The returned envelope carries the original
`capture_ns` forward, and the renderer stamps `process_done_ns` on the **same
host that captured the frame** — so its `e2e_ns` is a round trip measured on
one clock, and the only latency figure here that does not depend on PTP. See
[ADR-0009](../docs/architecture/adr/0009-render-return-path.md).

Both halves are off by default: `publish_result` is false, and the renderer's
`sink` is `null` (measures everything, draws nothing). Set `sink:=rerun` to
see the cloud, or `sink:=ros` to republish plainly for RViz2 or Foxglove.

**The result can travel G-PCC coded** ([ADR-0014](../docs/architecture/adr/0014-gpcc-point-cloud-codec.md)).
With `codec=gpcc` the edge sends its voxel indices, coded losslessly, as
`CompressedCloudWithTelemetry` on `mec_cast/result_gpcc`, and the renderer
decodes them and draws the voxel centres. The `lidar_scan` result shrinks from
6.6 kB to about 160 B a frame. Encoding falls inside the edge's sending, before
`send_ns`; decoding inside the renderer's `processing_ns`.

`codec` is a run parameter (`params.codec` in the admin's run, recorded in
`run.json`), and otherwise the nodes' `codec` parameter or the `CODEC`
environment variable. Edge and renderer must agree: each listens or publishes
only on its codec's topic, so a mismatched pair exchanges nothing.

## Why one workspace across two deployment locations

The top level of this repo is organised by deployment location, and these
packages break that pattern deliberately. Splitting them would need
`colcon build --base-paths clients/… edge/… shared/…`, which fights rosdep,
ament resource indexing, and every ROS tutorial a new collaborator will
read. The packages are *named* by role; which one runs where is expressed
in `deploy/lab/compose.{ue,edge}.yml`. One documented exception beats a
clever layout that breaks default tooling.

## Transport

`rmw_zenoh_cpp` (TCP or QUIC) or Fast DDS, `rmw_fastrtps_cpp` (UDP or TCP),
chosen at deployment with `MIDDLEWARE` and `MIDDLEWARE_LINK` — see
[ADR-0013](../docs/architecture/adr/0013-two-middlewares.md) and
[middleware.md](../docs/architecture/middleware.md). The application code is
vanilla ROS2 and never names a middleware; the image's entrypoint configures
the RMW. Each node reports what it runs to the admin (`params.middleware`,
`params.transport`). `make test-ros2` runs the launch tests under both.

The timing envelope rides **in-band** as a field of `CloudWithTelemetry`
rather than as a Zenoh attachment, because `rmw_zenoh` does not expose
per-publish attachments to the application layer.

## Test vectors

`mec_cast_lidar_client` generates deterministic clouds from a seed:

| Parameter | Default | Purpose |
|---|---|---|
| `seed` | 42 | Reproducible contents |
| `num_points` | 5000 | Payload size — the primary sweep variable |
| `rate_hz` | 10.0 | Publish rate |
| `pattern` | `uniform_cube` | Ten shapes: `uniform_cube`, `sphere`, `lidar_scan`, `rotating_plane`, `torus`, `helix`, `wave`, `cylinder`, `cube_edges`, `swarm` — see `workload.patterns` in `docs/_facts.yml` for how much each compresses on the downlink (1.4x to 23.5x) |

Fixing the seed across a size sweep means only the size varies.

## Tests

```bash
make test-ros2      # builds the image, runs colcon test in-container
```

`mec_cast_edge/test/test_pipeline_launch.py` is a `launch_testing` case:
it starts the router, client, and edge, then asserts messages flow, `seq`
is gapless, and `recv_ns > send_ns` over the active RMW.

## `mec_cast_admin_client`

The control-plane client every node shares, the way they already share
`mec_cast_msgs`. A background thread owns the WebSocket and moves dicts
through queues; **it never touches rclpy**, and commands are applied from a
normal timer on the executor thread. That keeps `rclpy.spin` single-threaded —
no MultiThreadedExecutor, no callback groups.

`MecCastNode` (in this package) is the base every measurement node derives
from, so these parameters exist on all of them. Each environment variable is
demoted to a *default*, which is what makes a node fully drivable from the
command line with nothing exported:

| Parameter | Default | Meaning |
|---|---|---|
| `run_id` | `$RUN_ID`, else `dev-run` | Standalone only; ignored when an admin is attached |
| `runs_dir` | `$RUNS_DIR`, else `runs` | Base output directory |
| `logging_url` | `$LOGGING_URL`, else empty | Empty disables snapshot posting; CSV is written regardless |
| `admin_url` | `$ADMIN_URL`, else empty | Empty = standalone, unchanged behaviour |
| `admin_autostart` | client `false`, edge/render `true` | Join the active run on connect |
| `admin_instance` | `0` | Distinguishes several nodes of one type per host |
| `zone` | `$ZONE`, else empty | Which zone (a gNB, its UEs and its edge) the node reports; empty = the single default zone |

An empty `run_id` or `runs_dir` falls back the way an absent one does. That is
not cosmetic: compose sets `RUN_ID: ${RUN_ID:-}`, so a node reading the
environment naively would record to a bare `pub-0` directory directly
under `runs/` — outside any run directory,
with every unnamed run appending to the same files.

With an admin, the Recorder is built **per run** rather than per process:
`start_run` creates it and the timer or subscription, `stop_run` destroys them
in that order so nothing fires into a shut-down recorder. See
[docs/operations/admin-service.md](../docs/operations/admin-service.md).
