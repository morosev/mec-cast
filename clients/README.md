# Client components

Everything that runs on the **UE side** — the robot compute machine behind
the 5G modem.

| Component | Profile | Status |
|---|---|---|
| [`webrtc_native/`](webrtc_native/) | B — media | Working (legacy) |
| ROS2 lidar client | A — robotics | Working — lives in [`../ros2/src/mec_cast_lidar_client/`](../ros2/src/mec_cast_lidar_client/) |

## Why the ROS2 client is not in this directory

It is a ROS2 package and must sit inside the single colcon workspace at
`../ros2/`. Splitting the workspace across `clients/` and `edge/` would
require `colcon build --base-paths …` and fights rosdep and ament resource
indexing. The packages are named by role; deployment location is expressed
in `deploy/lab/compose.{ue,edge}.yml`. See the rationale in
[docs/architecture/overview.md](../docs/architecture/overview.md#repository-layout).

## `webrtc_native/`

Node.js console client with a C++ N-API addon linking the patched libwebrtc
fork. Requires `third_party/webrtc/src/out/release_x64/obj/libwebrtc.a`
(see [building libwebrtc](../docs/guides/building-libwebrtc.md)).

```bash
cd clients/webrtc_native && npm install
make build-client          # from the repo root
```

It keeps its own in-process `DelayMeasurement` rather than the shared Rust
telemetry crate. That is deliberate: it stays untouched until the str0m
profile reaches parity, at which point both retire together. Known defects
in its statistics layer are catalogued in
[ADR-0004](../docs/architecture/adr/0004-exact-percentiles.md).
