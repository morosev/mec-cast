# WebRTC P2P Demo (Linux)

A simple WebRTC peer-to-peer **bidirectional audio + video** call demo with a Node.js
signaling server and console clients using native C++ WebRTC bindings. Each client
can independently send or receive audio and video, controlled via configuration.
Video is displayed in an X11 window.

## Table of Contents

- [Architecture](#architecture)
- [Prerequisites](#prerequisites)
- [Getting Started](#getting-started)
  - [1. Create the webrtc folder and install depot_tools](#1-create-the-webrtc-folder-and-install-depot_tools)
  - [2. Checkout WebRTC source](#2-checkout-webrtc-source)
  - [3. Install Linux build dependencies](#3-install-linux-build-dependencies)
  - [4. Apply the send-timestamp-ns patch](#4-apply-the-send-timestamp-ns-patch)
  - [5. Build WebRTC](#5-build-webrtc)
  - [6. Install dependencies and build the demo](#6-install-dependencies-and-build-the-demo)
- [Running](#running)
- [Client Commands](#client-commands)
- [Client Configuration](#client-configuration)
- [Nanosecond Delay Measurement (PTP)](#nanosecond-delay-measurement-ptp)
  - [Delay Metrics](#delay-metrics)
  - [Custom RTP Header Extension](#custom-rtp-header-extension)
  - [PTP and Clock Synchronization](#ptp-and-clock-synchronization)
  - [Limitations Without PTP](#limitations-without-ptp)
  - [PTP Requirements](#ptp-requirements)
- [Logging](#logging)
- [Example Session](#example-session)
- [Signaling Protocol](#signaling-protocol)
  - [Keep-Alive / Heartbeat](#keep-alive--heartbeat)
- [Testing](#testing)
- [License](#license)

## Architecture

```
┌──────────┐     WebSocket      ┌──────────┐     WebSocket      ┌──────────┐
│ Client A ├───────────────────►│  Server  │◄───────────────────┤ Client B │
│ (Node.js │   JSON signaling   │ (Node.js)│   JSON signaling   │ (Node.js │
│ + C++)   ├────────────────────┤          ├────────────────────┤  + C++)  │
└────┬─────┘                    └──────────┘                    └────┬─────┘
     │                                                               │
     │            P2P Bidirectional Audio + Video (after ICE)        │
     └───────────────────────────────────────────────────────────────┘
```

The client uses a **split-compilation** approach with a C ABI boundary:
- `webrtc_core.cc` — compiled with Chromium's clang++ and libc++ to match `libwebrtc.a` ABI
- `addon.cc` / `peer_connection_wrapper.cc` — compiled with Chromium's clang++ using Node.js N-API headers
- Communication between the two layers uses only C types (`const char*`, `int`, opaque pointers)

## Prerequisites

- Linux (Ubuntu 22.04+ or equivalent)
- Node.js 18+ with npm
- Git
- X11 development libraries (`libx11-dev`)

## Getting Started

### 1. Create the webrtc folder and install depot_tools

```bash
mkdir webrtc && cd webrtc
git clone https://chromium.googlesource.com/chromium/tools/depot_tools.git
export PATH="$PWD/depot_tools:$PATH"
```

### 2. Checkout WebRTC source

```bash
fetch --nohooks webrtc
gclient sync
```

### 3. Install Linux build dependencies

```bash
cd src
sudo ./build/install-build-deps.sh --no-prompt
```

### 4. Apply the send-timestamp-ns patch

The delay measurement system requires a custom RTP header extension in WebRTC.
Apply the provided patch before building:

```bash
cd src
git apply ../../webrtc-send-timestamp-ns.patch
```

The patch adds a `SendTimestampNsExtension` class that carries a 16-byte payload
(capture timestamp + send timestamp, each 8 bytes) in every RTP video packet,
enabling one-way delay measurement and full pipeline breakdown with
PTP-synchronized clocks. It also forces every frame to carry timing metadata
for continuous encode/decode measurement.

### 5. Build WebRTC

```bash
gn gen out/release_x64 --args='is_debug=false rtc_include_tests=false proprietary_codecs=true ffmpeg_branding="Chrome"'
ninja -C out/release_x64 webrtc
```

This produces `out/release_x64/obj/libwebrtc.a`.

### 6. Install dependencies and build the demo

```bash
# Server
cd server
npm install

# Client
cd ../client
npm install
npx node-gyp install
chmod +x build.sh
./build.sh
```

`build.sh` uses Chromium's clang++ to compile the native addon
(`build/Release/webrtc_addon.node`), linking against `libwebrtc.a` and the
libc++ runtime objects from the WebRTC build output.

## Running

Open **3 terminal windows**:

### Terminal 1: Server
```bash
cd server
npm start
```
The server listens on port 8080 and logs each client connection/disconnection.

### Terminal 2: Client A
```bash
cd client
npm start
```

### Terminal 3: Client B
```bash
cd client
npm start
```

## Client Commands

| Command | Description |
|---------|-------------|
| `connect <name> [server]` | Connect to signaling server (default: `ws://localhost:8080`) |
| `call` | Start a call (sends audio + video from mic/camera) |
| `answer` | Answer an incoming call (plays audio, opens video window) |
| `end` | End the current call |
| `disconnect` | Disconnect from server |
| `status` | Show current connection status |
| `stats on\|off` | Toggle live WebRTC stats display (every 2s) |
| `delay on\|off` | Toggle nanosecond delay reporting (every 2s) |
| `delay report` | Show one-shot delay measurement snapshot |
| `delay log [file]` | Start logging delay data to CSV file |
| `delay reset` | Reset delay statistics |
| `delay ptp` | Show PTP synchronization status |
| `audioinfo` | Show audio track diagnostics |
| `videoinfo` | Show video track diagnostics |
| `help` | Show available commands |
| `quit` | Exit the client |

## Client Configuration

The client can be configured via `client/client-config.json` to automate
connection and call setup. All properties are optional — when omitted or set to
defaults, the client behaves as a fully manual interactive console.

```json
{
  "server_address": "ws://localhost:8080",
  "username": "alice",
  "auto_connect": true,
  "auto_answer": true,
  "auto_stats_on": false,
  "send_audio": true,
  "send_video": true
}
```

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `server_address` | string | `"ws://localhost:8080"` | WebSocket URL of the signaling server |
| `username` | string | `""` | Name to register with on the server |
| `auto_connect` | bool | `false` | Connect to the server automatically on startup |
| `auto_answer` | bool | `false` | Automatically answer incoming calls |
| `auto_stats_on` | bool | `false` | Enable live stats display when a call connects |
| `send_audio` | bool | `true` | Capture and send microphone audio |
| `send_video` | bool | `true` | Capture and send camera video |

**Validation rules:**
- `auto_connect` requires both `server_address` and `username` to be set.

When the config file is absent, the client starts in fully manual mode.

## Nanosecond Delay Measurement (PTP)

The system includes precise delay measurement using **PTP (IEEE 1588) synchronized
clocks**. This enables nanosecond-resolution one-way delay measurement between
sender and receiver in a 5G testbed environment.

### Delay Metrics

The system measures seven delay components across the entire video pipeline.
Each metric is computed per-frame and aggregated into running statistics
(last, min, max, average, p99, count).

| Metric | Description |
|--------|-------------|
| **Network (one-way)** | RTP packet transit time (requires PTP sync) |
| **Glass-to-glass** | Camera capture → display render (end-to-end) |
| **Encoding** | Time spent in the video encoder |
| **Decoding** | Time spent in the video decoder |
| **Jitter buffer** | Time waiting in the receiver jitter buffer |
| **Sender pipeline** | Capture → RTP send |
| **Receiver pipeline** | RTP receive → render |

**Network (one-way):** Measured as `send_ns − receive_ns` where `send_ns` is
stamped by the sender at packet departure (in `rtp_sender_egress`) and
`receive_ns` is recorded on the receiver when the RTP packet arrives. Both
timestamps use `CLOCK_REALTIME`, so this metric requires PTP-synchronized clocks
to produce meaningful absolute values. On loopback or same-machine tests, the
offset is near-zero and this effectively measures kernel/stack processing time.

**Glass-to-glass:** The total end-to-end latency from frame capture on the sender
to frame display on the receiver: `render_ns − capture_ns`. The `capture_ns`
timestamp is taken at the moment the camera driver delivers a frame to WebRTC's
video capture module; it is converted from the monotonic `capture_time()` to
`CLOCK_REALTIME` at packet egress and transmitted via the header extension. The
`render_ns` is taken locally when `OnFrame()` delivers the decoded frame to the
renderer. This metric requires PTP sync for cross-machine accuracy.

**Encoding:** The time the video encoder (VP8/VP9/H.264/AV1) takes to compress
a single frame. WebRTC's `VideoTimingExtension` carries `encode_start_delta_ms`
and `encode_finish_delta_ms` (relative to capture time) in every packet. On the
receiver, the decoder extracts these and exposes them as `encode_duration_ms` on
the decoded `VideoFrame`. Every frame is forced to be a "timing frame" so this
data is always available (not just periodic samples).

**Decoding:** The time the video decoder takes to decompress a frame. Measured
locally on the receiver using WebRTC's `processing_time()` field on the decoded
`VideoFrame`, which records the monotonic timestamps when the frame entered and
exited the decoder (`generic_decoder.cc`). Converted to `CLOCK_REALTIME` for
consistency with other metrics.

**Jitter buffer:** The time a frame spends waiting in the receiver's jitter
buffer before being released to the decoder: `decode_start_ns − receive_ns`.
This captures the buffering delay introduced to smooth out network jitter.
Higher values indicate more aggressive buffering or variable network conditions.

**Sender pipeline:** The total sender-side processing time from frame capture to
RTP packet departure: `send_ns − capture_ns`. This encompasses encoding,
packetization, and pacer queue delay. The pacer introduces intentional delay to
smooth burst transmissions and comply with bandwidth estimates.

**Receiver pipeline:** The total receiver-side processing time from packet
arrival to frame display: `render_ns − receive_ns`. This encompasses jitter
buffer wait, decoding, and any rendering queue delay.

### Custom RTP Header Extension

The delay measurement system uses a custom RTP header extension to transport
sender-side timestamps to the receiver. The extension uses the **one-byte header
format** (RFC 8285) which supports extension elements of 1–16 bytes with IDs 1–14.

**Extension URI:** `http://www.mec-cast.org/experiments/rtp-hdrext/send-timestamp-ns`

**Wire format (16 bytes):**

```
 0                   1                   2                   3
 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|         capture_ns (high 32 bits, big-endian)                 |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|         capture_ns (low 32 bits, big-endian)                  |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|         send_ns (high 32 bits, big-endian)                    |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|         send_ns (low 32 bits, big-endian)                     |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
```

- **capture_ns** (bytes 0–7): `CLOCK_REALTIME` nanoseconds at frame capture.
  Computed at packet egress by converting the monotonic `capture_time()` stored
  on the `RtpPacketToSend` to real-time using the offset
  `CLOCK_REALTIME − CLOCK_MONOTONIC` sampled at that instant.
- **send_ns** (bytes 8–15): `CLOCK_REALTIME` nanoseconds at packet departure.
  Stamped in `rtp_sender_egress.cc` just before the packet is handed to the
  transport layer.

**Why 16 bytes?** The one-byte RTP header extension format encodes element length
in a 4-bit field as `L` where actual length = `L + 1`, giving a maximum of
16 bytes per element. Our extension uses exactly this maximum to carry two
64-bit nanosecond timestamps. Using 64-bit nanoseconds (rather than 32-bit
milliseconds) provides sub-microsecond resolution necessary for PTP-grade
measurements and avoids wrap-around issues (a 64-bit ns counter won't wrap
for ~584 years).

**RTP header integration:** The extension is negotiated in SDP via the standard
`a=extmap` mechanism. WebRTC assigns it an ID (typically 12) during offer/answer
negotiation. In the RTP packet, it appears as:

```
RTP Header (12 bytes) → [CSRC if any] → Extension Header:
  ┌─────────────────────────────────────────┐
  │ 0xBEDE (one-byte ext magic) | length=4  │  ← 4 = 16 bytes / 4 (32-bit words)
  ├─────────────────────────────────────────┤
  │ ID(4b) | L=15(4b) | 16 bytes of data   │  ← L=15 means 16 bytes
  └─────────────────────────────────────────┘
```

The extension adds 20 bytes of overhead per RTP packet (4-byte extension header +
16 bytes payload). At 30 fps video with ~3 packets per frame, this amounts to
~1.8 KB/s of additional bandwidth — negligible compared to the video bitrate.

**Direction:** The extension is registered as `kSendRecv` so both peers in a
bidirectional call stamp their outgoing packets and extract timestamps from
incoming packets, enabling delay measurement in both directions simultaneously.

### PTP and Clock Synchronization

The delay measurement system relies on **synchronized wall-clock time** between
sender and receiver. Without clock synchronization, one-way metrics (network,
glass-to-glass, sender/receiver pipeline) are meaningless because timestamps from
different machines cannot be compared.

**How PTP is used:**

1. The PTP daemon (`ptp4l`) synchronizes the NIC's hardware clock (PHC) to a
   grandmaster clock on the network via IEEE 1588 Precision Time Protocol.
2. `phc2sys` continuously disciplines `CLOCK_REALTIME` from the PHC, maintaining
   sub-microsecond alignment between the system clock and the PTP time domain.
3. Our code calls `clock_gettime(CLOCK_REALTIME)` for all timestamps. When PTP
   is active, this clock is phase-locked to the grandmaster with nanosecond-level
   accuracy, making cross-machine timestamp comparisons valid.
4. The `PtpMonitor` module periodically reads the PHC offset (via `ioctl` on
   `/dev/ptp0`) and reports synchronization quality. The delay report shows
   "PTP Sync: ✓ RELIABLE" when the measured offset is below the configured
   threshold (default 1 µs).

**Timing points in the pipeline:**

```
Sender                                          Receiver
──────                                          ────────
Camera grab ──→ capture_ns (CLOCK_REALTIME)
    │
    ▼
Encoder (VP8/H264)
    │
    ▼
Packetizer + Pacer
    │
    ▼
RTP Egress ───→ send_ns (CLOCK_REALTIME)
    │
    │ ~~~ Network ~~~
    ▼
RTP Arrival ──→ receive_time (CLOCK_MONOTONIC → CLOCK_REALTIME)
    │
    ▼
Jitter Buffer (wait)
    │
    ▼
Decoder (VP8/H264)
    │
    ▼
OnFrame() ────→ render_ns (CLOCK_REALTIME)
```

### Limitations Without PTP

When PTP hardware is not available (no `/dev/ptp0`), the system falls back to
`CLOCK_REALTIME` without hardware disciplining. This has several implications:

| Limitation | Impact |
|-----------|--------|
| **No accurate one-way delay** | Network delay values are unreliable because `CLOCK_REALTIME` on two machines may differ by milliseconds (NTP) or more. Values may even be negative. |
| **Glass-to-glass is approximate** | Without PTP, the capture_ns and render_ns are on different time bases. On the same machine (loopback test), this works fine. |
| **Sender/receiver pipeline split is invalid** | Cross-machine sender_pipeline and receiver_pipeline values include clock offset error. |
| **Local-only metrics still work** | Encoding, decoding, and jitter buffer delays are measured entirely on one machine using monotonic clocks, so they remain accurate regardless of PTP. |
| **Same-machine testing is valid** | When both clients run on the same host, they share `CLOCK_REALTIME` and all metrics are accurate (the "PTP Sync: ✗ UNRELIABLE" warning can be ignored). |

**Signaling-based NTP fallback:** The system includes a signaling-based clock
synchronization protocol (`clock_sync_request`/`clock_sync_response` messages)
that estimates clock offset using NTP-style round-trip measurement via the
WebSocket connection. This provides ~1–5 ms accuracy for development use, but
is insufficient for production measurements where sub-microsecond precision is
required.

**Recommendation:** For production measurements in a 5G MEC testbed, always use
PTP with hardware timestamping. For development and functional testing, the
same-machine loopback setup provides accurate relative measurements without any
PTP infrastructure.

### PTP Requirements

For accurate one-way delay measurement, both nodes must have PTP-synchronized
clocks. The system uses the PTP Hardware Clock (PHC) exposed at `/dev/ptp0`.

**Hardware needed:**
- NIC with IEEE 1588 hardware timestamping support (e.g., Intel i210, i225,
  X710, or Mellanox ConnectX)
- PTP Grandmaster clock or GPS-disciplined time source on the network
- (For best results in 5G testbed) Dedicated PTP infrastructure with hardware
  timestamping throughout

**Software setup:**

1. Install `linuxptp`:
   ```bash
   sudo apt install linuxptp
   ```

2. Start PTP synchronization on your NIC (e.g., `eth0`):
   ```bash
   sudo ptp4l -i eth0 -m -2
   ```

3. Discipline the system clock from the PTP Hardware Clock:
   ```bash
   sudo phc2sys -s /dev/ptp0 -c CLOCK_REALTIME -O 0 -m
   ```

4. Verify synchronization (offset should be <1µs):
   ```bash
   # Watch ptp4l output for "master offset" values
   # Watch phc2sys output for "offset" values
   ```

**Expected accuracy:**

| Setup | Accuracy |
|-------|----------|
| PTP with HW timestamping (same switch) | 10–100 ns |
| PTP with HW timestamping (multiple hops) | 100–500 ns |
| PTP with SW timestamping | 1–10 µs |
| No PTP (software NTP fallback) | 1–5 ms |

## Logging

All screen output and errors are mirrored to log files, recreated on each
application start:

- **Client:** `client/log/client.log`
- **Server:** `server/log/server.log`

WebRTC native-layer logs are written separately to
`client/log/<username>_webrtc.log`.

## Example Session

**Client A:**
```
> connect alice
[12:00:01] Connected to server as "alice".
```

**Client B:**
```
> connect bob
[12:00:05] Connected to server as "bob".
[12:00:05] Peer joined: "alice". You can now type 'call' to start a call.
```

**Client A sees:**
```
[12:00:05] Peer joined: "bob". You can now type 'call' to start a call.
```

**Client A:**
```
> call
[12:00:10] Calling "bob"...
[12:00:10] Offer created and sent to peer.
```

**Client B sees:**
```
[12:00:10] Incoming call from "alice"!
[12:00:10] Type "answer" to accept the call.
> answer
[12:00:12] Answering call...
[12:00:12] Answer created and sent to peer.
[12:00:13] P2P connection established!
```

**To end the call:**
```
> end
[12:00:30] Call ended.
```

## Signaling Protocol

All messages are JSON over WebSocket:

| Message | Direction | Purpose |
|---------|-----------|---------|
| `{ type: "register", name }` | Client → Server | Register with server |
| `{ type: "registered", name }` | Server → Client | Registration confirmed |
| `{ type: "peer_joined", name }` | Server → Client | Another peer connected |
| `{ type: "peer_left", name }` | Server → Client | Peer disconnected |
| `{ type: "offer", sdp }` | Client → Server → Client | SDP offer |
| `{ type: "answer", sdp }` | Client → Server → Client | SDP answer |
| `{ type: "ice_candidate", candidate }` | Client → Server → Client | ICE candidate |
| `{ type: "hangup" }` | Client → Server → Client | End call |
| `{ type: "ping" }` | Client → Server | Heartbeat ping (every 10s) |
| `{ type: "pong" }` | Server → Client | Heartbeat pong response |
| `{ type: "clock_sync_request", t1_ns }` | Client → Server → Client | NTP-style clock sync request |
| `{ type: "clock_sync_response", t1_ns, t2_ns, t3_ns }` | Client → Server → Client | Clock sync response |

### Keep-Alive / Heartbeat

The client sends a `ping` message to the server every **10 seconds**. The server
responds with a `pong`. If the server receives no ping from a client within
30 seconds (3 missed intervals), it considers the client dead, removes it, and
notifies the remaining peer via `peer_left`. If the client receives no pong
within 30 seconds, it logs a timeout notification and transitions to the
disconnected state.

## Testing

### Local E2E Test

Run a full end-to-end test locally (server + 2 clients + call + delay report):

```bash
./tests/e2e_local.sh [duration_seconds]
```

Default duration is 10 seconds. The script:
1. Starts the signaling server on port 8080
2. Connects two clients (alice and bob)
3. Alice calls bob (auto-answered)
4. Waits for the specified duration while streaming
5. Prints delay measurement reports
6. Verifies P2P connection was established
7. Cleans up all processes on exit

Prerequisites: server deps installed, client addon built, port 8080 free.

## License

MIT License — see [LICENSE](LICENSE).
