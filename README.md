# WebRTC P2P Demo (Linux)

A simple WebRTC peer-to-peer **one-way audio + video** call demo with a Node.js
signaling server and console clients using native C++ WebRTC bindings. The caller
captures audio from the microphone and video from the camera; the callee plays
audio through speakers and displays video in an X11 window.

## Architecture

```
┌──────────┐     WebSocket      ┌──────────┐     WebSocket      ┌──────────┐
│ Client A ├───────────────────►│  Server  │◄───────────────────┤ Client B │
│ (Node.js │   JSON signaling   │ (Node.js)│   JSON signaling   │ (Node.js │
│ + C++)   ├────────────────────┤          ├────────────────────┤  + C++)  │
└────┬─────┘                    └──────────┘                    └────┬─────┘
     │                                                               │
     │              P2P Audio + Video (after ICE)                    │
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

### 5. Install dependencies and build the demo

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
  "auto_answer": true
}
```

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `server_address` | string | `"ws://localhost:8080"` | WebSocket URL of the signaling server |
| `username` | string | `""` | Name to register with on the server |
| `auto_connect` | bool | `false` | Connect to the server automatically on startup |
| `auto_answer` | bool | `false` | Automatically answer incoming calls |

**Validation rules:**
- `auto_connect` requires both `server_address` and `username` to be set.

When the config file is absent, the client starts in fully manual mode.

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

## License

MIT License — see [LICENSE](LICENSE).
