# ADR-0013: Two middlewares: Zenoh for TCP and QUIC, Fast DDS for UDP and TCP

- **Status:** Accepted
- **Date:** 2026-10-21

## Context

ADR-0001 chose `rmw_zenoh` for the data plane, because DDS's multicast
discovery (SPDP) does not cross the UPF, and named "Fast DDS with a Discovery
Server" as the fallback. ADR-0006 then tried to carry Zenoh over UDP. Zenoh's
UDP link never relayed on this build: declarations are lost
(ros2/rmw_zenoh#765), so the platform runs Zenoh over TCP, with QUIC
available.

The research wants UDP as a data-plane transport. It also wants the
middleware itself as a factor: the same LiDAR stream through a brokered star
(Zenoh) and through RTPS (DDS). Both should run the same nodes, over the same
topology, chosen at deployment.

## Decision

1. **Two middlewares, one per deployment, the same on every host:**
   - `MIDDLEWARE=zenoh` runs `rmw_zenoh_cpp`. `MIDDLEWARE_LINK` is `tcp`
     (default) or `quic`.
   - `MIDDLEWARE=dds` runs Fast DDS, `rmw_fastrtps_cpp`. `MIDDLEWARE_LINK` is
     `udp` (default) or `tcp`.

   Zenoh's UDP option is removed. UDP belongs to DDS.
2. **The same interfaces.** The nodes are `rclpy` only and never name a
   middleware. The image's entrypoint
   (`deploy/docker/middleware/middleware-env.sh`) turns the two variables
   into `RMW_IMPLEMENTATION` and the middleware's config. It is the only
   code that knows both.
3. **The same topology: a star centred on the edge.**
   - **Zenoh:** clients of the router, which relays every sample.
   - **DDS:** clients of the Fast DDS **Discovery Server** (server id 0,
     port 11811). Discovery goes through the server; data then flows
     directly between participants.
   - The edge runs whichever centre is chosen (compose profiles `zenoh` and
     `dds`), and the UE dials it as before.
4. **DDS is pinned to the measured path.**
   - **One transport, no built-ins.** Each participant uses exactly one
     transport, UDPv4 or TCPv4. Fast DDS's built-in shared memory is off:
     two containers sharing a hostname negotiate it and then lose samples.
   - **One set of interfaces (`DDS_INTERFACES`).** Fast DDS announces every
     interface, and a peer sends to any address it can reach, so a host on
     the management LAN would carry the point clouds around the 5G link.
     Zenoh never had this choice to make: a client dials one address.
   - **TCP dials out from the UE.** Over TCP, the edge listens
     (`DDS_TCP_LISTEN_PORT`) and the UE's participants dial it, the way a
     Zenoh client dials the router.
5. **The UE must be routable from the edge for DDS over UDP.** RTPS sends
   data peer to peer, so the edge's participants address the UE directly,
   for the renderer's downlink and for reliable ACKs. The lab's UPF does not
   NAT the UE pool. A deployment where it does uses `dds/tcp` or Zenoh.
6. **Nodes report what they really run, and the admin shows it.**
   - Each ROS node reports `params.middleware` and `params.transport` (the
     link), as configured by the entrypoint.
   - The admin shows a zone's middleware on its page.
   - `WF_TRANSPORT_MISMATCH` fires when a zone's nodes disagree on either
     the middleware or the link.
   - `run.json` records `middleware: {name, rmw, link}`, which replaces the
     constant `transport: rmw_zenoh_cpp` and `zenoh_link`.

## Rationale

- **Why Fast DDS, not Cyclone DDS:** it is ROS 2 Jazzy's default RMW and
  already in the image. Its Discovery Server gives the star without per-host
  peer lists. It also carries both UDP and TCP.
- **Why one knob per deployment, not per run:** the middleware is fixed when a
  node process starts (one session, one link), as the link always was. A run
  cannot switch it; it can only record it.
- **Why keep both instead of moving to DDS:** Zenoh over TCP and QUIC is the
  measured, NAT-proof path, and DDS over UDP is what the research adds. A
  comparison needs both, over the same nodes and the same star.

## Consequences

- **The middleware is a factor of every run.** Runs on different middlewares or
  links are different conditions. `run.json` says which. A run whose nodes
  disagreed is recorded as `mixed`, and is not a valid run.
- **Supersedes ADR-0006** (UDP for Zenoh). **Extends ADR-0001**: its DDS
  fallback becomes a supported option. The reasons ADR-0001 gives against
  multicast DDS still hold, which is why DDS here always uses the Discovery
  Server.
- **Ports.**
  - Zenoh: 7448/tcp, plus 7449/udp for QUIC.
  - DDS: the Discovery Server on 11811 (udp or tcp), then RTPS data on UDP
    7400 and up for `dds/udp`, or the edge's TCP 7410 for `dds/tcp`.
  - The lab's firewalls must pass the set the deployment uses.
- **ROS CLI introspection** (`ros2 topic list`) under the Discovery Server needs
  a SUPER_CLIENT profile (`DDS_DISCOVERY_PROTOCOL=SUPER_CLIENT`).
- **An explicit `ZENOH_CONFIG_OVERRIDE` or `FASTDDS_DEFAULT_PROFILES_FILE`** still
  wins, for experiments. The node then reports the link as `override` where
  it cannot tell.
