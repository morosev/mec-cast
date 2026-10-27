# ADR-0014: G-PCC as the point-cloud codec, opt-in and coded frame by frame

- **Status:** Proposed
- **Date:** 2026-10-27

## Context

Every frame crosses the 5G uplink as a raw `PointCloud2`: 12 bytes per point,
60 KB at the 5,000-point default, 4.8 Mbit/s at 10 Hz. The edge's result
returns the same way. Compression has so far been outside the research
([protocol](../../research/protocol.md): "Compression is not under test"),
and Draco was the codec named for later.

The research asks how large data crosses an industrial 5G link. A codec
changes that directly: the bytes on the air, and the compute on both ends.
MPEG's G-PCC is the standardised codec for exactly this data, with lidar
modes, and our fork of its reference software
([mpeg-pcc-tmc13](https://github.com/morosev/mpeg-pcc-tmc13), in
`third_party/`) adds parallel slice coding and a C interface for embedding
it (`libtmc3codec`).

Measured on our synthetic clouds through that interface (5,000 points,
geometry only, every frame coded on its own, Apple M-series, 20 frames):

| Pattern | Leg | Precision | Raw | Coded | Encode | Decode |
|---|---|---|---|---|---|---|
| `uniform_cube` | uplink | 1 mm | 60 KB | 18.7 KB | 6.5 ms | 5.4 ms |
| `lidar_scan` | uplink | 1 mm | 60 KB | 5.2 KB | 4.4 ms | 3.5 ms |
| `lidar_scan` | uplink | 1 cm | 60 KB | 3.1 KB | 2.8 ms | 2.1 ms |
| `uniform_cube` | downlink, voxel indices | lossless | 45 KB | 1.1 KB | 0.8 ms | 0.4 ms |
| `lidar_scan` | downlink, voxel indices | lossless | 6.6 KB | 135 B | 0.2 ms | 0.1 ms |

The edge's voxel result was identical with and without the codec in every
case. Inside the ROS image (Ubuntu 24.04, aarch64, in a container on the same
Mac) the coded sizes are byte-identical and the times within 10 % for the
uplink, and within 0.5 ms for the downlink.

## Decision

1. **G-PCC, through `libtmc3codec`, is the point-cloud codec.** The ROS image
   builds the library from the submodule and loads it with `ctypes`; no
   language binding of our own is compiled.
2. **Off by default.** A `codec` parameter (`none` | `gpcc`) on the lidar
   client, the edge and the renderer, set as a run parameter and recorded in
   `run.json`. With `none` every node behaves exactly as before, so runs stay
   comparable with every campaign recorded without a codec.
3. **Coded frames travel as their own message, on their own topics.**
   `CompressedCloudWithTelemetry` (the same `TimingEnvelope`, the codec, the
   point count, the voxel size and the coded bytes) on `mec_cast/cloud_gpcc`
   and `mec_cast/result_gpcc`. Reusing the raw topics with another type would
   match nothing and fail silently.
4. **Every frame is coded on its own.** No inter prediction. The uplink may
   run best-effort and the downlink keeps the newest frame only, so frames are
   dropped by design; with inter prediction one lost frame would corrupt every
   frame after it. Each coded frame carries its parameter sets, so a decoder
   that joins late, or restarts, decodes the next frame it receives.
5. **Positions are quantised to a precision that divides the voxel size**
   (1 mm or 1 cm against 0.5 m). The codec truncates, so no point can cross a
   voxel boundary and the edge's result does not depend on the codec. The
   precision is a run parameter; the bitstream declares metres, so the
   decoder returns metres.
6. **The downlink is coded losslessly, as voxel indices.** The edge's result
   is already an integer grid, which is what G-PCC codes best.
7. **The timing envelope is unchanged, and so is the meaning of each stamp.**
   - Encoding happens between `capture_ns` and `send_ns`, so it is part of
     the sender delay.
   - Decoding happens after `recv_ns` and before `process_done_ns`, so it is
     part of the processing delay, at the edge and at the renderer.
   - `payload_bytes` is the coded size.

## Rationale

- **G-PCC over Draco.** Draco is a general mesh and point codec. G-PCC is the
  ISO/IEC standard (23090-9) for point clouds, with octree and predictive
  geometry coding designed for spinning lidar. A result about G-PCC is also
  a result about the codec a 5G system is expected to carry. The fork's C
  interface takes numpy arrays without copying and releases the GIL, so the
  nodes stay in Python.
- **The reference software over a production encoder.** No production G-PCC
  encoder is openly available. The reference software is slower than one
  would be; the measurements above say it is fast enough at this point count,
  and the encode and decode times are recorded, not hidden.
- **Opt-in over always on.** A codec changes the bytes, the sender delay and
  the processing delay at once. Off by default keeps every existing
  comparison valid, the way `publish_result` did for the downlink
  ([ADR-0009](0009-render-return-path.md)).
- **No new stamps.** Codec time could be stamped separately, but that changes
  the 64-byte envelope shared with Profile B. Folding it into the sender and
  processing delays keeps the envelope and every derived metric as they are.
  Per-frame encode and decode times can be added to the recorded sample later
  if the analysis needs them apart.
- **Frame by frame over inter prediction.** Inter prediction would cut the
  uplink further on slowly changing scenes, but it needs every frame
  delivered, which only reliable QoS gives, and a single loss then costs
  many frames. It can be revisited for reliable runs only.

## Consequences

- **The codec becomes a factor of a run**, as the middleware is
  ([ADR-0013](0013-two-middlewares.md)): `codec` and the precision are
  recorded per run, and the admin flags a zone whose nodes disagree.
- **The research protocol's known limit changes.** With `codec=none` it still
  holds; with `gpcc` compression is under test, and a run must say which.
- **The image builds C++ from source.** The ROS image gains a build stage,
  the build context includes `third_party/mpeg-pcc-tmc13` (`.dockerignore`),
  the lab deploy syncs it, and CI checks the submodule out. The image build
  loads the library once, so a library that builds but cannot load fails the
  build.
- **The reference decoder is not hardened** against malformed streams. The
  only producer is our own encoder, on a closed lab network.
- **One decoder per publisher** is assumed until shown otherwise: whether one
  decoder can take frames interleaved from several lidar instances is not yet
  verified.
- **Measured on one machine.** Encode and decode times on the lab's hosts
  (x86-64 or otherwise) must be measured before any timing comparison.
