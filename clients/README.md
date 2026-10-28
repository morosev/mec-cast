# Client components

Everything that runs on the **UE side** — the robot compute machine behind
the 5G modem.

| Component | Profile | Status |
|---|---|---|
| [`webrtc_native/`](webrtc_native/) | B — media | Working — sender and receiver, Linux x86-64 and macOS |
| ROS2 lidar client | A — robotics | Working — lives in [`../ros2/src/mec_cast_lidar_client/`](../ros2/src/mec_cast_lidar_client/) |

## Why the ROS2 client is not in this directory

It is a ROS2 package and must sit inside the single colcon workspace at
`../ros2/`. Splitting the workspace across `clients/` and `edge/` would
require `colcon build --base-paths …` and fights rosdep and ament resource
indexing. The packages are named by role; deployment location is expressed
in `deploy/lab/compose.{ue,edge}.yml`. See the rationale in
[docs/architecture/overview.md](../docs/architecture/overview.md#repository-layout).

## `webrtc_native/`

Node.js console client with a C++ N-API addon on the patched libwebrtc fork,
used prebuilt ([ADR-0015](../docs/architecture/adr/0015-media-profile-relay.md)).
A sender or a receiver through the [media relay](../edge/README.md#media-relay),
or a peer-to-peer call through `edge/signaling`.

```bash
make fetch-libwebrtc build-client   # from the repo root
make test-legacy                    # peer-to-peer call + delay report
```

It feeds **both** measurement paths:

- its original in-process `DelayMeasurement`, which still backs the
  interactive `delay report` / `delay log` commands (known defects
  catalogued in [ADR-0004](../docs/architecture/adr/0004-exact-percentiles.md));
- the shared Rust telemetry crate over its C ABI: the receiver writes
  `runs/<RUN_ID>/receiver/samples.csv` (site 4) in the same schema as
  Profile A, joining the relay's samples on the frame's RTP timestamp.

Environment (identical to the ROS2 nodes): `RUN_ID`, `RUNS_DIR`,
`LOGGING_URL`, plus `MEC_CAST_TELEMETRY=0` to disable.
