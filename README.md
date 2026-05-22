# WebRTC P2P Demo (Linux)

A simple WebRTC peer-to-peer **bidirectional audio + video** call demo with a Node.js
signaling server and console clients using native C++ WebRTC bindings. Each client
can independently send or receive audio and video, controlled via configuration.
Video is displayed in an X11 window.

## Architecture

```
┌──────────┐     WebSocket      ┌──────────┐     WebSocket      ┌──────────┐
│ Client A ├───────────────────►│  Server  │◄───────────────────┤ Client B │
│ (Node.js │   JSON signaling   │ (Node.js)│   JSON signaling   │ (Node.js │
│ + C++)   ├────────────────────┤          ├────────────────────┤  + C++)  │
└────┬─────┘                    └──────────┘                    └────┬─────┘
     │                                                               │
     │            P2P Bidirectional Audio + Video (after ICE)         │
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

### 4. Build WebRTC

```bash
cd src
gn gen out/release_x64 --args='is_debug=false rtc_include_tests=false proprietary_codecs=true ffmpeg_branding="Chrome"'
ninja -C out/release_x64 webrtc
```

This produces `out/release_x64/obj/libwebrtc.a`.

### 5. Apply the send-timestamp-ns patch

The delay measurement system requires a custom RTP header extension in WebRTC.
Apply the provided patch before building:

```bash
cd webrtc/src
git apply ../../webrtc-send-timestamp-ns.patch
```

Then rebuild WebRTC:

```bash
ninja -C out/release_x64 webrtc
```

The patch adds a `SendTimestampNsExtension` class that carries an 8-byte
nanosecond timestamp in every RTP packet, enabling one-way delay measurement
with PTP-synchronized clocks.

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

| Metric | Description |
|--------|-------------|
| **Network (one-way)** | RTP packet transit time (requires PTP sync) |
| **Glass-to-glass** | Camera capture → display render (end-to-end) |
| **Encoding** | Time spent in the video encoder |
| **Decoding** | Time spent in the video decoder |
| **Jitter buffer** | Time waiting in the receiver jitter buffer |
| **Sender pipeline** | Capture → RTP send |
| **Receiver pipeline** | RTP receive → render |

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

**Fallback behavior:** If no PTP hardware is detected (`/dev/ptp0` not available),
the system falls back to `CLOCK_REALTIME`. A signaling-based NTP estimation is
also available for development without PTP hardware (accuracy ~1-5ms).

### Custom RTP Header Extension

A custom 8-byte RTP header extension (`send-timestamp-ns`) carries the sender's
nanosecond timestamp in every RTP packet. This allows the receiver to compute
true one-way network delay when clocks are PTP-synchronized.

Extension URI: `http://www.mec-cast.org/experiments/rtp-hdrext/send-timestamp-ns`

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

## License

MIT License — see [LICENSE](LICENSE).
