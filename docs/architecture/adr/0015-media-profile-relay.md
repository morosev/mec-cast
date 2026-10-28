# ADR-0015: The media profile: a patched libwebrtc client, prebuilt, through a str0m relay

- **Status:** Proposed
- **Date:** 2026-10-28

## Context

Profile B, WebRTC media for teleoperation, was a peer-to-peer console client
on our libwebrtc fork (`third_party/webrtc`, the `SendTimestampNs` RTP header
extension). It built only on Linux, only against a local ~20 GB WebRTC
checkout, needed a camera to measure anything, and had no point between the
two peers where the network could be measured. `str0m-profile.md` planned a
str0m SFU to replace it.

The research wants WebRTC's own behaviour over 5G -- its congestion control,
jitter buffer and codecs -- measured with the platform's timing, on Linux and
macOS clients, with a measuring point on the edge. It is a use case of its
own: it does not interoperate with the ROS profile.

## Decision

1. **A parallel system.** It shares the platform's telemetry (the CSV schema,
   logging snapshots, `run_id`), the RAN tap and xApp, and the lab's roles,
   PTP and deployment. Nothing of the ROS profile (middleware, codec, LiDAR)
   is involved.
2. **The clients keep the patched libwebrtc**, its extension included, since
   a library we control is what further research needs. The Node console app
   and its C++ addon stay; the app gains roles (`send`, `recv`), a seeded
   synthetic video source, SDL2 rendering in place of X11, and WHIP/WHEP.
3. **libwebrtc is prebuilt, on GitHub.** A workflow in the fork builds
   linux-x64 and macos-arm64 on a `webrtc-*` tag and attaches one archive per
   platform to the tag's release. mec-cast pins the tag and the archives'
   SHA-256 in `third_party/webrtc.lock`; `make fetch-libwebrtc` downloads and
   verifies. The library is built as Chromium builds it, with Chromium's own
   libc++, and the archive carries that clang and libc++: the client compiles
   its WebRTC-facing half with them and its Node-facing half with the system
   compiler, meeting at a C interface.
4. **A relay on the edge, str0m unmodified.** `edge/media-relay` takes a
   sender with WHIP and receivers with WHEP, and forwards RTP without
   decoding (str0m's RTP mode, one UDP port, ICE-lite). It writes the mec-cast
   extension back unchanged, as a str0m user extension, and passes keyframe
   requests to the sender. Clients dial out to it, so it works behind a NAT.
5. **Three measuring points per frame**, joined on the frame's RTP timestamp:
   - the sender's capture and send stamps, in the extension;
   - the relay (site 3): arrival and forwarding of the frame's last packet,
     so `network_ns` there is the uplink alone;
   - the receiver (site 4): arrival, decode, render; `e2e_ns` is
     glass-to-glass.
6. **Runs under the admin, as a profile.** A run's `params.profile` is
   `robotics` (the default) or `media`. The profile decides which node types
   a run recruits and what its quorum is: the relay, a sender and a receiver
   for a media run; the gNB tap and the xApp join runs of both. The relay
   and the clients join the admin as node types `relay`, `sender` and
   `receiver`, record a run between its `run.start` and `run.stop`, and take
   the stream and the video's format from the run's params. Without
   `ADMIN_URL` each still records `RUN_ID` standalone.

## Rationale

- **The fork over a stock WebRTC:** stamps at the RTP egress, every frame a
  timing frame, and the encode duration exposed -- none of which a stock
  library gives. Keeping it also keeps the door open to changing WebRTC
  itself, which a browser or a stock build closes.
- **Prebuilt over local builds:** a client builds in minutes from a 60-90 MB
  download. `use_custom_libcxx=false`, which would have let any compiler link
  the library, does not build this WebRTC: Chromium's sysroot lacks C++20
  library features it uses, and protobuf does not compile against Apple's
  libc++. Shipping Chromium's toolchain is the configuration upstream builds.
- **A relay over peer to peer:** the relay is the only place the uplink can be
  measured apart from the downlink, and it is where an operator's stream
  meets the edge. Forwarding without decoding keeps it out of the measurement:
  it forwards a frame in tens of microseconds.
- **str0m over a full SFU:** a sans-IO library with RTP mode and user
  extensions does exactly this, in a few hundred lines we own, without a fork.

## Consequences

- **No arm64 Linux clients:** Chromium ships no clang for arm64 Linux hosts.
- **No camera on macOS yet:** libwebrtc's capture module has none there; macOS
  sends synthetic video. Linux uses V4L2.
- **Clocks.** Every stamp is CLOCK_REALTIME. WebRTC's own clock is not
  CLOCK_MONOTONIC on macOS -- it stops during sleep -- so conversions take
  their offset against WebRTC's clock; the fork's extension was fixed to do so.
  One-way figures need PTP on both ends: a Mac rarely has it, the lab's Linux
  hosts and edge do.
- **Two mec-cast Cargo workspaces:** the relay builds against str0m, which the
  ROS and RAN images do not carry. The Rust admin client moved into the
  telemetry crate (feature `admin`) so the relay and the gNB collector share
  it.
- **A zone runs one profile at a time:** the robotics and media nodes of a zone
  share its gNB, and a zone has one active run. A media run beside a
  robotics run needs a second zone.
- **A fourth protocol implementation:** the Node client's
  (`clients/webrtc_native/admin_client.js`), tested against the shared
  fixture like the others.
- **The fork carries a CI workflow and a clock fix** beside the extension, and
  a change to it means a new tag, release and lock entry.
- **Peer-to-peer mode stays**, through `edge/signaling`, as the baseline a
  relayed call is compared with.
- **Supersedes the plan in `str0m-profile.md`** (a str0m SFU replacing the
  libwebrtc client): the client stays, and str0m relays.
