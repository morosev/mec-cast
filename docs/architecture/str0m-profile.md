# Profile B: media over str0m (planned)

The planned replacement for the libwebrtc-based media path: a Rust
`mec-cast-media` workspace member built on
[str0m](https://github.com/algesten/str0m), consuming the fork vendored at
[`third_party/str0m/`](../../third_party/README.md).

When implemented, the SFU itself lands under `edge/` — it is a MEC server
component. `third_party/str0m/` holds only the library fork.

## Why str0m

str0m is **sans-IO**: the application owns the UDP socket and the event
loop. That means `send_ns` can be stamped at the actual socket write and
`recv_ns` at the socket read — the precision that currently requires a
patched libwebrtc tree (`third_party/webrtc/src`,
`SendTimestampNsExtension`) falls out of the architecture for free.

Note the tension this creates with vendoring a *fork* of str0m: the
original argument for str0m was that no fork would be needed. Keep the
fork's delta as small as possible — ideally limited to header-extension
registration and anything upstream will not accept — so the option of
returning to an unforked crates.io dependency stays open.

## Contract with the platform

- Depends on `mec-cast-telemetry` only; no coupling to Profile A.
- Carries `TimingEnvelope::to_bytes()` (the shared 64-byte wire format) as
  an RTP header extension — the same negotiation model as the existing
  extension (`http://www.mec-cast.org/experiments/rtp-hdrext/...`), RFC 8285
  one-byte format. Note: 64 bytes exceeds the one-byte-format 16-byte
  element limit, so either the two-byte header format is used or the
  extension carries the envelope's timestamp fields only (16 bytes:
  capture_ns + send_ns, as today) with seq/trace_id inferred per-session.
  Decide at implementation time; the CSV/snapshot contract is unaffected.
- Reuses the existing signaling server at `edge/signaling/` over WebSocket.
- Feeds the identical recorder: `service: "mec-cast-media"`,
  `modality: video|audio`, same CSV schema, same snapshot shape — directly
  comparable with Profile A runs in the same Postgres.

## Migration plan

1. Loopback str0m sender/receiver with the envelope extension; verify
   against the telemetry loopback test pattern.
2. Interop with the existing signaling server; parity call flow
   (connect/call/answer/end).
3. Side-by-side latency comparison vs the libwebrtc client on the lab
   testbed; retire `clients/webrtc_native/` + `third_party/webrtc/` when
   parity is demonstrated.

Until then, the legacy path (`clients/webrtc_native/`, `edge/signaling/`,
`third_party/webrtc/`) stays buildable and untouched.
