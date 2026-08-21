# MEC server components

Everything that runs on the **edge (MEC) application server**, behind the
UPF.

| Component | Profile | Status |
|---|---|---|
| [`signaling/`](signaling/) | B — media | Working (legacy) |
| str0m SFU | B — media | Planned — [design](../docs/architecture/str0m-profile.md) |
| Zenoh ingest node | A — robotics | Working — lives in [`../ros2/src/mec_cast_edge/`](../ros2/src/mec_cast_edge/) |
| Zenoh router | A — robotics | `rmw_zenohd`, run from the ROS image |
| [`../services/admin/`](../services/admin/) | control plane | Working — run orchestration on :8099, [guide](../docs/operations/admin-service.md) |

The ROS2 ingest node sits in the colcon workspace for the same reason the
lidar client does — see [clients/README.md](../clients/README.md).

## `signaling/`

Node.js WebSocket signaling server for the WebRTC profile: registration,
SDP offer/answer relay, ICE candidate relay, heartbeat, and an NTP-style
clock-sync exchange. Two peers maximum. No authentication — lab use only,
do not expose it.

```bash
cd edge/signaling && npm install && npm start
```

## str0m SFU (planned)

The Rust SFU will land here as a workspace member when implemented. It is a
MEC server component, so it belongs under `edge/`; the str0m **library
fork** it consumes is vendored separately at
[`../third_party/str0m/`](../third_party/README.md).

str0m is sans-IO, so this process owns its own UDP sockets and can stamp
egress and ingress precisely — which is the entire reason the current
libwebrtc tree had to be forked. Replacing that fork is the point of this
component.

Design and migration plan:
[docs/architecture/str0m-profile.md](../docs/architecture/str0m-profile.md).
