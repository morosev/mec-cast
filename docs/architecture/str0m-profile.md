# Profile B: WebRTC media through a str0m relay

The media profile measures WebRTC over 5G for teleoperation: a sender's video
to a receiver, both on the platform's patched libwebrtc, through a relay on
the edge. Decisions and their reasons:
[ADR-0015](adr/0015-media-profile-relay.md).

```
 sender (UE)                       relay (edge)                     receiver (UE)
 clients/webrtc_native  ──WHIP──►  edge/media-relay   ◄──WHEP──    clients/webrtc_native
 role send, libwebrtc     RTP ──►  str0m, RTP mode      ──► RTP    role recv, libwebrtc
 capture_ns, send_ns    (5G UL)    site 3: recv, fwd    (5G DL)    site 4: recv, render
```

## The pieces

| Piece | Where | What it does |
|---|---|---|
| Client | [`clients/webrtc_native`](../../clients/webrtc_native/README.md) | Node app with a C++ addon on the fork; `role: send` or `recv`, synthetic or camera video, SDL2 window or headless |
| libwebrtc | [`third_party/webrtc`](../../third_party/README.md) | the fork, prebuilt by its release workflow; pinned in `third_party/webrtc.lock`, fetched by `make fetch-libwebrtc` |
| Relay | [`edge/media-relay`](../../edge/README.md) | WHIP/WHEP, forwards RTP without decoding, records each frame |
| str0m | [`third_party/str0m`](../../third_party/README.md) | used unmodified: RTP mode and user header extensions are upstream features |

## The wire contract

The fork stamps every video packet with a 16-byte RTP header extension,
`http://www.mec-cast.org/experiments/rtp-hdrext/send-timestamp-ns` (RFC 8285
one-byte form): `capture_ns` then `send_ns`, big-endian, CLOCK_REALTIME. The
relay reads it and writes it back unchanged; the receiver reads it again.

## What is recorded

One sample per video frame per site, in the platform's CSV schema, joined on
`seq` = the frame's RTP timestamp. A frame's stamps are those of its last
packet.

| Site | Directory | `network_ns` | `processing_ns` | `e2e_ns` |
|---|---|---|---|---|
| 3, relay | `runs/<RUN_ID>/relay/` | sender to relay: the uplink | forwarding, tens of µs | capture to forwarding |
| 4, receiver | `runs/<RUN_ID>/receiver/` | sender to receiver, both legs | jitter buffer, decode, render | glass-to-glass |

The relay-to-receiver leg is the receiver's `recv_ns` minus the relay's
`process_done_ns` for the same frame. Every cross-host figure needs PTP on
both hosts.

### WebRTC's own statistics

Every node also records WebRTC's view of each RTP stream, once a second, in
`runs/<RUN_ID>/<node>/webrtc.csv` (`sender`, `relay`, `receiver`), and posts
the same rows to the logging service (logger `webrtc.stats`, `trace_id` the
run id). The clients take them from libwebrtc's `getStats()`, the relay from
str0m's per-stream stats. Counters are cumulative; rates are derived.

| Columns | `out` (this node sends) | `in` (this node receives) |
|---|---|---|
| `ts_ns`, `node`, `peer`, `direction`, `kind`, `mid`, `ssrc`, `codec` | | |
| `packets`, `bytes`, `retransmitted_packets` | sent | received |
| `packets_lost`, `loss_fraction`, `jitter_ms`, `rtt_ms` | as the far end reported (RTCP) | as measured here |
| `nacks`, `plis`, `firs` | received from the far end | sent to it |
| `frames`, `keyframes`, `fps`, `width`, `height` | encoded (sender) | decoded (receiver) |
| `bwe_bps`, `target_bps`, `quality_limitation` | sender: bandwidth estimate, encoder target, why it is limited | |
| `frames_dropped`, `jitter_buffer_ms`, `freezes`, `freeze_ms` | | receiver |

Columns a source does not have are left empty: str0m reports no frames, and
the relay runs no bandwidth estimation of its own.
[`tools/media_join.py`](../../tools/README.md) joins a run's frames across
the relay and receiver into their legs, and gives each frame the streams'
state at its capture.

Through the relay, a client that sends no audio (`send_audio: false`, or
`role: recv`) offers receive-only audio, so nothing but the measured video
crosses the link. Peer to peer it still adds a muted track, which sends
silent Opus frames.

## Running it

Under the admin, which is how the lab runs it: the relay, the sender and the
receiver join it as node types `relay`, `sender` and `receiver` and wait. A
run with profile `media` recruits them -- and the RAN sources, not the lidar
nodes -- and is running once all three are; its params (`stream`,
`video_source`, `video_width`, `video_height`, `video_fps`) apply to the
sender and receiver for the run. Each records only between the run's start
and stop, into the run's directory, and `run.json` records what was measured:
`media: {stream, video, libwebrtc, relay}`. The admin's media findings say
where the video stops: no WebRTC connection (`WF_MEDIA_NOT_CONNECTED`), a
stream nobody publishes (`WF_STREAM_MISMATCH`), frames lost on the uplink or
the downlink (`WF_MEDIA_NO_FRAMES`) -- [admin-service.md](../operations/admin-service.md#diagnostics).

Without `ADMIN_URL`, each records `RUN_ID` from start to exit.

## Peer to peer

The client still calls a peer directly through `edge/signaling`, the
baseline a relayed call is compared with.
