# ADR-0009: A return path and a UE-side renderer

- **Status:** Accepted
- **Date:** 2026-08-24

## Context

Profile A measured one direction. `lidar_client` stamps `capture_ns` on the
UE, the cloud crosses the 5G uplink, `mec_cast_edge` stamps `recv_ns` and
`process_done_ns`, records a sample, and stops. `process_cloud()` computed a
centroid and a voxel count and discarded both. Nothing came back, and nothing
displayed anything to anyone.

Two things followed from that. There was no picture — an operator watching a
campaign had log lines and a CSV. And, less obviously, **every latency number
the platform produced was PTP-dependent**. `network_ns`, `e2e_ns` and
`sender_ns` all subtract a stamp taken on one host from a stamp taken on
another, so each is only as trustworthy as `ptp4l` was that afternoon.
`context.ptp.reliable` records whether to believe them, which is honest, but
when it comes back `false` there is no second opinion — the campaign is
simply suspect.

## Decision

The edge gains an opt-in downlink, and the UE gains a renderer.

`mec_cast_edge` republishes its processing result on **`mec_cast/result`** as
a `CloudWithTelemetry` — the same message the uplink uses, unchanged. The
payload is the voxel-downsampled cloud that `process_cloud()` already
computed and threw away, so the return costs one multiply-add rather than a
second pass. `mec_cast_render` subscribes on the UE, stamps arrival, draws,
stamps completion, and records at **site 2**, `runs/<RUN_ID>/render/`.

The returned envelope carries the **original** `capture_ns`, `seq` and
`trace_id` forward and takes a fresh `send_ns`. That is the whole mechanism,
and it yields this at the render site with no change to the telemetry crate,
the CSV schema or the message definitions:

| Column | Meaning at site 2 | Depends on PTP? |
|---|---|---|
| `e2e_ns` | **round-trip glass-to-glass** | **No** |
| `network_ns` | downlink leg, edge → UE | Yes |
| `processing_ns` | draw time | No |
| `sender_ns` | capture → edge send (uplink + edge work) | Yes |

`capture_ns` is stamped by the publisher on the UE and `process_done_ns` by
the renderer on the same host, off the same `CLOCK_REALTIME`. **This is the
only end-to-end latency figure in the platform that does not depend on clock
discipline**, and comparing it against the sum of the PTP-derived one-way legs
gives an independent read on the offset. The renderer is a measuring
instrument that happens to draw.

**Both halves are off by default.** The edge's `publish_result` defaults to
false, so `local.yml` alone still produces measurements comparable with every
run recorded before this existed. The renderer's `sink` defaults to `null`,
which measures the full round trip and draws nothing — that is what CI and
any host without a GPU need, and it keeps the measurement path from ever
being gated on a graphics dependency.

## The renderer is not the decision

`sinks.py` holds the only code that knows a renderer exists:

| Sink | What it is for |
|---|---|
| `null` | CI, headless hosts, and measuring the return path without a renderer's cost folded in |
| `rerun` | The lab default. Apache-2.0, renders large clouds well, and plots scalars on the same timeline as the 3D view — so a frame's glass-to-glass delay is visible beside the frame |
| `ros` | Republishes a plain `sensor_msgs/PointCloud2` on `mec_cast/render/cloud`, so RViz2 or Foxglove can attach without this node knowing they exist |

Rerun was chosen because the UE is a headless SSH box running
`ros:jazzy-ros-base`, which rules out RViz2 as a default (Qt and X11 into a
container on a machine with no display), and because it is the only candidate
that puts the number and the picture on one timeline — which is what this
repo is for. Foxglove's bridge is open but Studio v2+ is not, an awkward
dependency for a platform that is otherwise first-party, and it stamps no
display time, so the measurement would be lost.

The `ros` sink is what makes that choice non-binding. If Rerun's Python API
churns — it has, across its 0.x line, which is why `RerunSink` probes for the
call it needs instead of importing one — the cost of moving is one class.

## What serving the viewer actually required

Written down because two of the three were only discovered by running it, and
the first two fail in ways that look like the renderer is broken.

`rerun.serve_web()` does not exist in 0.36 — it was the pre-0.3x spelling. The
working sequence is two servers: `serve_grpc()` for the stream and
`serve_web_viewer()` for the page. A probe-and-fallback that tried
`serve_web`, then `serve_grpc`, then `serve` "succeeded" on the second and
served no page at all, which is worse than failing. The version is pinned and
`RerunSink._serve` now asserts both names up front.

**Both ports must be published**, 9876 and 9877. The page runs in the
operator's browser and opens the gRPC stream itself, so publishing only the
web port gives a page that loads and never fills in.

**`connect_to` does not put the source into the served page.** Verified
against 0.36.2: the bare page boots a viewer with no data source and never
attempts a connection. The viewer does honour a `?url=` query parameter, so
the node builds that address and logs it. The host in it is resolved by the
browser, not the container, which is why `viewer_host` exists — `localhost` is
wrong the moment the UE is a different machine, as it is in the lab.

`open_browser` defaults to true, and there is no browser in a container.

## Consequences

**Measured, local compose, 30,000 points at 10 Hz, 20 ms netem:** uplink
352 KB/frame becomes 92 KB on the downlink, a **3.84× reduction** — 3.60 MB/s
up against 0.94 MB/s down. The renderer received 100% of what the edge sent.
Round trip p50 68.59 ms against the edge's one-way 67.32 ms; downlink 0.94 ms;
draw 0.01 ms with the `null` sink. Frame-by-frame, `round_trip − (edge_e2e +
downlink + draw)` is 0.17 ms — the edge's `record()`-to-`publish()` gap, and
the only unaccounted segment. Every stamp is therefore consistent.

The compression is workload-dependent and is now an experimental knob:
`uniform_cube` at 3,000 points reduces only 1.20×, because 3,000 points across
8,000 voxels barely collide, while `rotating_plane` is a sheet through the same
cube and reduces far more. Downlink volume is a function of the `pattern`
parameter, not a constant.

**What this costs.** The return path shares the Zenoh session and the router
with the uplink. At the measured load the downlink is roughly a quarter of the
uplink and 5G's downlink is normally the roomier direction, but that is an
argument, not a measurement, and on the real radio it must be checked with
`publish_result` as the A/B. The edge does more per frame; publishing happens
after `record()` so its own sample is never delayed.

**Loss attribution changes shape.** The render subscription is `best_effort`
+ `KEEP_LAST(1)` — display semantics, newest frame wins, never show a stale
one. A frame superseded before the callback runs is dropped by the middleware
and never seen, so a slow renderer and a lossy downlink both surface as
`seq_gaps`. They are told apart afterwards by `processing_ns`: gaps alongside
a draw time near the frame interval mean the renderer, not the network.

**Reverse routing was the load-bearing assumption** and is now verified. The
UE dials out to the router and the edge publishes back over the established
session, which is exactly the property ADR-0001 chose Zenoh for. Confirmed on
the local topology; the UPF NAT case can only be confirmed in the lab.

**A fourth optional role.** `NodeType.RENDER` joins the control plane. It is
excluded from quorum like the gNB — a run with no viewer is normal, not
degraded — so its absence is not reported at all. Only a renderer that is
present and starved is a fault, `WF_RENDER_STARVED`, whose remedy names the
default that causes it: `publish_result` is off.

## Alternatives rejected

**Echo the raw cloud back.** Doubles the load to say nothing new, and models
no real MEC workload. Heavy sensor data up, a lighter result down, is the
shape of the thing being studied.

**Return only the centroid and voxel count.** Tiny, and enough for the
round-trip timing, but there would be nothing to draw and no downlink volume
worth measuring.

**Grow `TimingEnvelope` with a display stamp.** It is a pinned 64-byte
contract shared with the C ABI. Sites already join on `(run_id, seq)`; a
second row from a second site is the mechanism the schema was built for.

**A `render` role in `deploy.sh`.** The renderer runs on the UE host beside
the client. A compose profile on the existing `ue` role is enough, and leaves
`deploy.sh`'s `case` untouched.
