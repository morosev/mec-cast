# The ROS middleware: Zenoh or Fast DDS

The LiDAR client, the edge and the renderer talk through ROS 2, over one of
two middlewares, chosen at deployment
([ADR-0013](adr/0013-two-middlewares.md)):

| `MIDDLEWARE` | RMW | `MIDDLEWARE_LINK` | Star centre on the edge |
|---|---|---|---|
| `zenoh` (default) | `rmw_zenoh_cpp` | `tcp` (default), `quic` | Zenoh router, `zenoh-router` |
| `dds` | `rmw_fastrtps_cpp` (Fast DDS) | `udp` (default), `tcp` | Fast DDS Discovery Server, `dds-discovery` |

Set both variables the same on **every host** of a deployment. The nodes never
name a middleware: they are `rclpy` only, so the same code, topics and QoS run
on either. The image's entrypoint (`deploy/docker/middleware/middleware-env.sh`)
turns the two variables into the RMW and its config.

## One topology, two middlewares

```
            Zenoh                                   Fast DDS
UE  ──client──►  router (edge)  ──►  edge      UE ──discovery──► Discovery Server (edge)
                 relays every sample              ◄──────── data, peer to peer ────────►  edge
```

- **Zenoh.** Every node is a client of the router and dials it. The router
  relays every sample. Nothing dials the UE, which is why Zenoh crosses a NAT.
- **Fast DDS.** Every node is a CLIENT of the Discovery Server and learns its
  peers there. Data then flows directly between participants, RTPS over UDP or
  TCP. The edge sends to the UE's own address for the renderer's downlink and
  for reliable ACKs, so **the UE pool must be routable from the edge**. The
  lab's UPF does not NAT it.

Multicast DDS discovery is never used: it does not cross the UPF, which is
ADR-0001's reason for Zenoh. The Discovery Server makes DDS a star like
Zenoh.

## What each link means

| Link | Wire | Use |
|---|---|---|
| `zenoh/tcp` | TCP to the router on 7448 | the default; measured, NAT-proof |
| `zenoh/quic` | QUIC (UDP, TLS 1.3) to 7449 | needs TLS material: `bash scripts/gen-dev-tls.sh` for dev, real certificates in the lab |
| `dds/udp` | RTPS over UDPv4 | Discovery Server on 11811/udp; data on the RTPS ports (7400 and up) |
| `dds/tcp` | RTPS over TCPv4 | Discovery Server on 11811/tcp; the edge listens on 7410 (`DDS_TCP_LISTEN_PORT`) and the UE dials out |

Zenoh no longer offers UDP. Its UDP link lost declarations on this build
(ros2/rmw_zenoh#765), so UDP is DDS's.

`RELIABILITY` (reliable / best_effort) and `QOS_DEPTH` mean the same on both
middlewares: they are ROS QoS, and each RMW maps them onto its own protocol.
A best-effort publisher and a reliable subscriber still deliver nothing,
whichever middleware carries them.

## Keeping DDS on the measured path

Fast DDS needs two settings Zenoh does not. The entrypoint writes them into
the participant's XML profile.

- **One transport, no built-ins.** Fast DDS adds shared memory by default.
  Two containers that share a hostname, as `lidar-client` and `render` do
  (`ue-local`), negotiate it and then cannot use it, so samples vanish. Each
  participant uses exactly the link's transport.
- **`DDS_INTERFACES`, on multi-homed hosts.** Fast DDS announces every
  interface, and a peer sends to any address it can reach. On a host that is
  also on the management LAN, the point clouds could take that LAN instead
  of the 5G link, and the run would measure the wrong network.
  - **The value:** comma-separated addresses or interface names.
  - **Lab UE:** its 5G interface. **Lab edge:** its N6 address.
  - **Local-5G:** the compose file sets `tun_srsue` and `N6_ADDR`.
  - **Not needed** in the local single-bridge stack.

## Choosing it at deployment

| Where | How |
|---|---|
| Local | `MIDDLEWARE=dds make up-admin`, `MIDDLEWARE=dds MIDDLEWARE_LINK=tcp make up-admin`. The Makefile sets `COMPOSE_PROFILES`, which starts the matching centre |
| Lab | `MIDDLEWARE=dds deploy.sh <role> …` on every role; `deploy.sh` forwards both variables and the profile. Set `DDS_INTERFACES` per host |
| Local-5G | `MIDDLEWARE=dds make up-local-5g` |
| Tests | `MIDDLEWARE=dds MIDDLEWARE_LINK=udp make test-e2e`; CI runs `zenoh/tcp`, `dds/udp` and `dds/tcp` |

The middleware is fixed when a node process starts: changing it means
**recreating** the containers, not restarting a run.

## What the admin shows

- **Each ROS node reports `params.middleware` and `params.transport`**: what
  the entrypoint configured, not what the operator believes.
- **The admin page** shows a badge per zone, e.g. "Fast DDS · UDP", and a
  Middleware column in the Nodes table.
- **`WF_TRANSPORT_MISMATCH`** (error) fires when a zone's nodes disagree on
  the middleware or the link. Across middlewares nothing flows; across links
  a run measures a mixture.
- **`run.json`** records
  `"middleware": {"name": "dds", "rmw": "rmw_fastrtps_cpp", "link": "udp"}`,
  or `"name": "mixed"` for a run whose nodes disagreed. A mixed run is not a
  valid run.

## Experiments and introspection

- **An explicit `ZENOH_CONFIG_OVERRIDE` or `FASTDDS_DEFAULT_PROFILES_FILE`**
  in a node's environment wins over the generated config. The node then
  reports the link it can read from it, or `override`.
- **ROS CLI on DDS:** a CLIENT does not see the whole graph. From a container
  of the deployment, use a SUPER_CLIENT:
  ```bash
  docker exec -e DDS_DISCOVERY_PROTOCOL=SUPER_CLIENT <container> bash -lc \
    'source /middleware/middleware-env.sh && ros2 topic list'
  ```
- **ROS CLI on Zenoh:** `ros2 topic list` works from any node container.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| A node exits at start: `mec-cast middleware: MIDDLEWARE_LINK=udp is not a link of zenoh` | an invalid pair | Zenoh takes tcp or quic; DDS takes udp or tcp |
| `WF_TRANSPORT_MISMATCH` | one host deployed with other variables | the same `MIDDLEWARE` and `MIDDLEWARE_LINK` everywhere; recreate the odd container |
| DDS: the edge receives nothing, discovery looks fine | firewall on the RTPS data ports, or the UE not routable from the edge | open UDP 7400+ (or TCP 7410); check `ping <UE address>` from the edge |
| DDS: data flows, but over the management LAN | `DDS_INTERFACES` unset on a multi-homed host | set it to the 5G interface (UE) and the N6 address (edge) |
| Zenoh QUIC: the router dies at start | TLS material missing | `bash scripts/gen-dev-tls.sh`, or lab certificates in `deploy/docker/zenoh/tls/` |
