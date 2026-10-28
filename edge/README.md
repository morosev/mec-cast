# MEC server components

Everything that runs on the **edge (MEC) application server**, behind the
UPF.

| Component | Profile | Status |
|---|---|---|
| [`media-relay/`](media-relay/) | B — media | Working — [design](../docs/architecture/str0m-profile.md) |
| [`signaling/`](signaling/) | B — media | Working — peer-to-peer calls |
| Zenoh ingest node | A — robotics | Working — lives in [`../ros2/src/mec_cast_edge/`](../ros2/src/mec_cast_edge/) |
| Zenoh router | A — robotics | `rmw_zenohd`, run from the ROS image |
| [`../services/admin/`](../services/admin/) | control plane | Working — run orchestration on :8099, [guide](../docs/operations/admin-service.md) |

The ROS2 ingest node sits in the colcon workspace for the same reason the
lidar client does — see [clients/README.md](../clients/README.md).

## `media-relay/`

The media profile's relay ([ADR-0015](../docs/architecture/adr/0015-media-profile-relay.md)).
A WebRTC sender publishes with WHIP, receivers play with WHEP, and the relay
forwards the sender's RTP to them as it is, without decoding: str0m in RTP
mode, ICE-lite, one UDP port. It writes the sender's timing extension back
unchanged and records every frame at site 3 in
`<RUNS_DIR>/<RUN_ID>/relay/samples.csv`: `network_ns` is the uplink alone,
`processing_ns` the forwarding (tens of µs).

| Endpoint | |
|---|---|
| `POST /whip/<stream>` | SDP offer in; 201, SDP answer and `Location: /resource/<id>` out |
| `POST /whep/<stream>` | the same, to receive |
| `DELETE /resource/<id>` | leave |
| `GET /health` | `ok` |

| Variable | Default | |
|---|---|---|
| `RELAY_HTTP` | `0.0.0.0:8090` | WHIP/WHEP, TCP |
| `RELAY_UDP` | `0.0.0.0:8090` | media, UDP |
| `RELAY_PUBLIC_IP` | `RELAY_UDP`'s address | the one ICE candidate offered; required when listening on 0.0.0.0 |
| `RUN_ID`, `RUNS_DIR`, `LOGGING_URL` | | as every node |
| `PTP_DEVICE` | none | PHC to monitor (Linux) |

It is a Cargo workspace of its own, since the ROS and RAN images do not carry
str0m:

```bash
git submodule update --init third_party/str0m
make build-relay test-relay
RELAY_PUBLIC_IP=192.168.0.30 RUN_ID=dev edge/media-relay/target/release/mec-cast-relay
```

Advertise a real interface address even on one machine: libwebrtc
gathers no loopback candidates, so its clients cannot reach a relay at
127.0.0.1.

In the lab it is the `relay` role ([deploy manual](../docs/operations/deploy-manual.md)).

## `signaling/`

Node.js WebSocket signaling server for the WebRTC profile: registration,
SDP offer/answer relay, ICE candidate relay, heartbeat, and an NTP-style
clock-sync exchange. Two peers maximum. No authentication — lab use only,
do not expose it.

```bash
cd edge/signaling && npm install && npm start
```
