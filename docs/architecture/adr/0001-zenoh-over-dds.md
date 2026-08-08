# ADR-0001: Use rmw_zenoh rather than raw DDS for the point-cloud transport

- **Status:** Accepted
- **Date:** 2026-08-06

## Context

Profile A moves LiDAR `PointCloud2` from a 5G UE to a MEC edge server. The
path is UE → 5G modem → srsRAN → Open5GS → **UPF/NAT** → edge. ROS2's
default middleware is DDS (Fast DDS or Cyclone), and DDS is the more
popular, more mature, industry-standard choice — it is what most ROS2
deployments use and what industrial partners recognise.

That popularity is measured on **local networks**, which is not our path.
Our link is NAT'd, lossy, uplink-constrained, and carries samples far
larger than an MTU.

## Decision

Use **`rmw_zenoh`** (Tier-1 supported in ROS2 Jazzy) as the transport for
Profile A. Keep Fast DDS with a **Discovery Server** as a documented
fallback.

## Rationale

| Concern on a 5G link | DDS | Zenoh |
|---|---|---|
| Discovery across NAT/UPF | SPDP is **multicast**; the cellular user plane drops it. Needs a manually configured Discovery Server to work at all | Router-based unicast dial-out; traverses NAT natively |
| Data-path NAT traversal | Announces private-IP locators the peer cannot reach | UE dials *out* to the edge router; outbound connection just works |
| Large samples | RTPS fragments a ~2 MB cloud; one lost fragment loses the whole sample under BEST_EFFORT, while RELIABLE adds retransmit latency and head-of-line blocking | Built for large payloads over lossy links; runs over TCP/QUIC |

Critically, `rmw_zenoh` keeps the application in **vanilla ROS2** — nodes,
topics, and `PointCloud2` are unchanged. Only the middleware layer swaps,
so the "less popular" cost is not paid at the application layer.

## Consequences

- Weeks of multicast/NAT/fragmentation debugging avoided; that work would
  have been incidental to the actual research question.
- A Zenoh router must run somewhere reachable by the UE (the edge host).
  It is a new operational dependency.
- Zenoh's QoS model is simpler than DDS's. If deadline/liveliness/ownership
  semantics are ever required, this decision must be revisited.
- Because the edge subscriber is transport-agnostic (it depends only on
  `mec-cast-telemetry`), **"Zenoh vs DDS latency over 5G" is itself a
  measurable, publishable result** rather than a closed door.
- If an industrial partner mandates DDS on the wire, the fallback is
  Fast DDS + Discovery Server, and the NAT/fragmentation tuning cost
  returns.
