# ADR-0006: Reliable UDP for the Zenoh transport

- **Status:** Accepted
- **Date:** 2026-08-18
- **Amended:** 2026-08-20 — see below

> **Amendment, 2026-08-20.** This record originally described the chosen link
> as QUIC carried over UDP without TLS. That was wrong, and the error is
> corrected in place rather than superseded because the *decision* did not
> change — only the mechanism attributed to it.
>
> `udp/<host>:7447?rel=1` is Zenoh's own UDP link (`zenoh_link_udp`) with
> Zenoh's transport-level reliability above it. It is not QUIC, which in
> Zenoh is the separate `quic/` scheme and mandates TLS. Verified on the wire:
> the handshake datagrams are 35/78/56 bytes where QUIC requires client
> Initial packets to be padded to ≥1200; keepalives are a single byte, below
> QUIC's 16-byte AEAD tag alone; there is no QUIC version field; and an
> identical 25-byte cookie appears in cleartext in both directions, which
> QUIC's per-direction encryption makes impossible.
>
> **All measurements below stand** — they were measured, not inferred, and do
> not depend on what the link is called. What is withdrawn is the forward-
> looking rationale that assumed QUIC mechanics: connection-ID mobility,
> stream multiplexing, and user-space congestion control. Those benefits are
> **not** being obtained. If UE mobility across handover is required, it needs
> the real `quic/` scheme with TLS, which the sweep below measured as worse.

## Context

Frame loss under `netem` impairment was traced to TCP's congestion
response: a lost packet is retransmitted, congestion control collapses the
sending rate, and the publisher's `KEEP_LAST(10)` queue then sheds frames
that were never sent. TCP is designed for elastic bulk traffic that
tolerates delay in exchange for completeness. A teleoperation and
industrial-sensing testbed on a private 5G network is not that workload.

Five link configurations were built and measured against an identical
sweep before deciding. All run on the deployed
`ros-jazzy-rmw-zenoh-cpp 0.2.9` image with no rebuild — only the
`/zenoh/{router,session}-config.json5` endpoints change.

Two findings reframed the problem and are recorded because they are easy
to rediscover the hard way:

**The Mathis-bound figure is not a link capacity.** Nothing in
[`deploy/compose/local.yml`](../../../deploy/compose/local.yml) caps
bandwidth — the qdisc sets `delay` and `loss`, never `rate`. The ~0.44 MB/s
that TCP achieves under 25 ms / 0.4% is the rate *TCP selects for itself*.
The same 30,000-point workload runs at **0.00% loss unimpaired**.

**There is a second ceiling, and it is not the network.** With impairment
removed, delivered throughput tracks offered exactly to **~17.2 MB/s**,
then decouples — 200,000 and 1,000,000-point workloads both land in the
same ~17–19 MB/s band despite 5× different offered load. Loopback between
containers does orders of magnitude more. The limit is the software
pipeline: `numpy.tobytes()`, `PointCloud2` construction, Zenoh
serialisation and the edge's per-frame centroid/voxel pass, in Python
under the GIL. No transport choice moves it.

## Decision

Use **Reliable UDP** — `udp/<host>:7447?rel=1` — as the Zenoh link for
Profile A: Zenoh's UDP link, with Zenoh's transport-level reliability
(sequence numbers and retransmission) layered above it.

What that does and does not give:

| Property | Present? |
|---|---|
| Retransmission of lost fragments | **Yes**, at the Zenoh transport layer |
| Network congestion control | **No** — no cwnd, no pacing, no rate estimate |
| Queue congestion policy | Yes, but only `drop`/`block` on a full tx queue |
| Encryption | **No** |
| Connection survives an IP change | **No** — a 4-tuple UDP socket |

The absence of network congestion control is the point: TCP's collapse under
random loss is the failure this platform kept hitting, and a link that does
not interpret loss as congestion does not collapse that way. It also means
nothing throttles a publisher that outruns the path — backpressure comes from
the tx queue and `KEEP_LAST`, not from the network.

Point-cloud QoS stays `RELIABLE` + `KEEP_LAST(10)`, now expressed as the
`reliability` parameter on both nodes (default `reliable`) rather than an
unstated default. A partial LiDAR frame is not useful, so completeness is
the right contract for this modality; `best_effort` is one parameter away
for experiments that want drop-stale semantics.

## Rationale

**The bench measurements do not penalise this choice, but neither do they
compel it.** An earlier comparison appeared to show TCP with half the tail
latency — but it varied transport *and* QoS together (TCP+`RELIABLE`
against the datagram arm + `BEST_EFFORT`) and was therefore confounded. Held at
`RELIABLE` on both arms, 120 s runs of ~1,425 frames, Reliable UDP
measured equal or better in both repeats: p99 141.5 / 120.4 ms against
TCP's 261.9 / 124.5, jitter 21.2 / 18.7 against 36.4 / 21.6, with loss
identical to within one frame. TCP's own two runs differ by 2× at p99, so
run-to-run variance exceeds the between-transport gap in the second
repeat. Read this as *no measured penalty*, not as a demonstrated win.

The decision rests on the measured result above plus one property TCP
cannot offer:

- **No congestion collapse under random loss.** TCP treats every lost packet
  as congestion and halves its window. On a link whose loss is radio error
  rather than queue overflow that is the wrong inference, and it is the
  failure this platform kept hitting: a 30,000-point workload that runs at
  0.00% loss unimpaired collapses to a fraction of its offered rate under
  0.4% netem loss. Reliable UDP retransmits the lost fragment without
  throttling the sender. The cost is the mirror image — nothing throttles a
  publisher that outruns the path either.
- **UPF/NAT traversal**, the same reason
  [ADR-0001](0001-zenoh-over-dds.md) chose Zenoh over raw DDS. UDP-based
  transports traverse carrier NAT more predictably than long-lived TCP.

**Withdrawn by the 2026-08-20 amendment.** Three arguments in the original
record assumed QUIC and do not hold:

- *UE mobility via connection ID* — originally called "the strongest
  argument". `zenoh_link_udp` is keyed by the 4-tuple, so a handover or NAT
  rebinding breaks the link exactly as TCP would. **This benefit is not
  being obtained**, and it is the one that would justify paying for `quic/`
  and TLS if mobility turns out to matter on the real radio.
- *Stream multiplexing via `multistream=1`* — there are no QUIC streams, so
  head-of-line blocking is not addressed by this choice.
- *Positioning for BBR in user space* — there is no quinn congestion
  controller to select. Selecting BBR would mean the kernel's, which reaches
  only TCP, or the `quic/` scheme.

**No encryption**, because `rel=1` offers none and the alternative that does
— the `quic/` scheme — costs more than it buys here: the 5G user plane is
already ciphered, the lab is a trusted network, and certificate management is
real operational weight (a self-signed cert must be a leaf, `CA:FALSE`, or
Zenoh's TLS stack rejects it as `CaUsedAsEndEntity`). `quic/` also measured
worse than `rel=1` at every size in the sweep below.

Note this is a *consequence* of the link choice, not an independent decision:
there is no encrypted `rel=1` and no unencrypted `quic/`. QUIC mandates
TLS 1.3.

**Alternatives rejected.** Plain `udp/` has no congestion control at all —
the Eclipse Zenoh wiki is explicit that "no retransmission mechanism, nor
congestion control, is implemented in Zenoh yet" for it. It won the
bandwidth race and lost the delivery race: at 10,000 points it delivered
the most frames (2.10% loss) and the stalest (p50 276 ms, p99 1458 ms),
because without backpressure frames queue rather than being shed.
`mixed_rel=1` with `BEST_EFFORT` QoS — the "true media" configuration —
was measured and gave an identical median with a worse tail, and its
datagram path could not be confirmed to engage at all: at 0.4% loss it
delivered **0.00%** frame loss across ~1400 multi-packet frames, where
genuinely unreliable delivery must shed some. `BEST_EFFORT` is retained as
a parameter, not adopted as the default.

## Consequences

- **No measured latency penalty on the bench** once QoS is held constant,
  and possibly a jitter improvement that two repeats cannot establish. The
  decision still rests on properties only a real radio exercises: if the
  lab link does not show the mobility/NAT benefit, this ADR is wrong and
  should be superseded.
- **There is no congestion control to tune, and no bandwidth estimate is
  exposed.** `transport.link.*` accepts only `protocols, tx, rx, tls, tcp,
  unixpipe` — the absence of a `quic` section was originally read as "QUIC's
  CC is not tunable"; it is in fact one of the signs that this link is not
  QUIC at all. Either way the platform must derive throughput itself from
  `payload_bytes` and `recv_ns`, which every per-frame CSV already carries.
  For a measurement testbed that is arguably the better source: it is
  end-to-end goodput, not a transport's internal guess.
- **`capacity_check()` in
  [`scripts/run-experiment.sh`](../../../scripts/run-experiment.sh) now
  models the wrong transport.** Its Mathis formula describes TCP's
  congestion response and does not govern this link, which has no congestion
  control at all. It is left in place as a rough over-subscription warning,
  but its output is not a prediction for the configured transport and must be
  reworked.
- **The pipeline ceiling (~17 MB/s) is unchanged by this decision** and
  remains the binding constraint on large-payload experiments. It is
  CPU/serialisation-bound; a lab host will sit elsewhere on that curve, so
  it must be measured per host before any impairment result can be
  attributed to the network.
- **Tunable knobs, verified accepted by this build:** `tx.queue.size`
  (per-priority depth, above `KEEP_LAST`), `tx.batch_size` (≤ 65535),
  `tx.queue.batching.{enabled,time_limit}`, `tx.threads`,
  `unicast.qos.enabled`, and per-endpoint `#so_sndbuf`/`#so_rcvbuf`,
  `#initial_mtu`, `#dscp`. `#dscp` is the one to reach for when the 5G
  bearer should classify the point-cloud flow.

  Corrected 2026-08-20: `transport.link.tx.queue.congestion_control` **is**
  present in this build, contrary to the original text — a live config dump
  shows `drop.wait_before_drop: 1000`,
  `drop.max_wait_before_drop_fragments: 50000` and
  `block.wait_before_close: 5000000`. It governs what happens when the local
  tx queue fills (drop the sample or block the publisher); it is not network
  congestion control and does not pace the sender.
- **Revisit if:** UE mobility across handover turns out to matter on the real
  radio — that requires the `quic/` scheme with TLS, and this link cannot
  provide it; a Zenoh release exposes link stats or a congestion controller
  for the UDP link; or mixed traffic classes make head-of-line blocking worth
  addressing, which again means `quic/`.

## Measurements

Sweep: 10 Hz, `netem delay=25ms jitter=5ms loss=0.4%`. Frame loss, 30 s runs:

| Points | TCP | UDP | `quic/`+TLS | RelUDP | RelUDP+MS |
|---:|---:|---:|---:|---:|---:|
| 500 | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% |
| 1,000 | 0.00% | 0.00% | 0.00% | 0.21% | 0.21% |
| 3,000 | 0.42% | 0.00% | 0.21% | 0.21% | 0.00% |
| 10,000 | 9.94% | 2.10% | 22.01% | 18.45% | 14.05% |

**Head-to-head, QoS held at `RELIABLE` on both arms** — the adopted
configuration against the transport it replaces. n=3,000, 120 s,
~1,425 frames each:

| Transport | rep | loss | p50 ms | p90 ms | p99 ms | jitter ms |
|---|---:|---:|---:|---:|---:|---:|
| TCP | 1 | 0.07% | 50.45 | 75.96 | 261.88 | 36.38 |
| **RelUDP** | 1 | 0.00% | 43.77 | 60.25 | **141.47** | **21.20** |
| TCP | 2 | 0.07% | 48.31 | 69.78 | 124.45 | 21.60 |
| **RelUDP** | 2 | 0.07% | 49.39 | 72.59 | **120.35** | **18.68** |

A **confounded** comparison, kept only as a caution: it varies transport
and QoS together, and the apparent TCP advantage here does not survive
holding QoS constant above. The "QUIC-dgram" label predates the 2026-08-20
amendment and was not re-verified; it denotes the unreliable-datagram arm
(`mixed_rel=1`), not the `quic/` scheme. Treat the label, not the numbers,
as uncertain.

| Config | n | loss | p50 ms | p99 ms | jitter ms |
|---|---:|---:|---:|---:|---:|
| TCP reliable | 3,000 | 0.00% | 48.94 | 126.57 | 20.70 |
| QUIC-dgram best_effort | 3,000 | 0.00% | 48.61 | 235.29 | 39.06 |
| TCP reliable | 10,000 | 22.42% | 120.53 | 1166.10 | 269.04 |
| QUIC-dgram best_effort | 10,000 | 15.35% | 208.49 | 1247.95 | 259.02 |

30 s runs are **underpowered for p99**: three repeats at n=500 gave TCP
p99 52.69 / 49.62 / 43.72 ms against RelUDP 98.02 / 85.19 / **32.09** ms.
A single RelUDP run appearing to beat TCP by 2.6× was sampling noise —
at that size only ~2% of frames are touched by loss, and p99 over ~424
frames *is* that 2%. Tail claims need ≥ ~1,000 frames per run.

Unimpaired, TCP, to locate the pipeline ceiling:

| Points | Offered | Achieved | Loss |
|---:|---:|---:|---:|
| 30,000 | 3.43 MB/s | 3.43 MB/s | 0.00% |
| 150,000 | 17.22 MB/s | **17.22 MB/s** | 0.30% |
| 200,000 | 22.89 MB/s | ~18.9 MB/s | 21.34% |
| 1,000,000 | 114.44 MB/s | ~16.4 MB/s | 86.32% |

## Reproducing

```bash
# Reliable UDP is the committed default; compare against TCP by swapping
# the endpoint in both configs and nothing else. To confirm what is actually
# on the wire, run the router with RUST_LOG=zenoh=debug and read the module
# name in the "Accepted ... connection" line: zenoh_link_udp, not
# zenoh_link_quic.
#   router-config.json5:  listen.endpoints  ["udp/[::]:7447?rel=1"]
#   session-config.json5: connect.endpoints ["udp/zenoh-router:7447?rel=1"]
bash scripts/run-experiment.sh -n 3000 -r 10.0 -d 120 -l 25ms -j 5ms -L 0.4%
```

Set `RELIABILITY=best_effort` to switch the data plane's QoS contract.
Compare `runs/<id>/pub/samples.csv` against `runs/<id>/edge/samples.csv`
for loss, and the `network_ns` column for latency.
