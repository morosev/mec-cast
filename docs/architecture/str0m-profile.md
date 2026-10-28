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

## Peer to peer

The client still calls a peer directly through `edge/signaling`, the
baseline a relayed call is compared with.
