# Lab topology and deployment

## Hosts and roles

| Role | Runs | Deploy |
|---|---|---|
| **infra** | Admin service (:8099) + logging service + PostgreSQL; optionally a near-RT RIC — oran-sc-ric or FlexRIC, one at a time — and the E2 xApp (ADR-0010, ADR-0012) | `bash deploy/lab/deploy.sh infra user@host`; the RIC by hand: `bash deploy/lab/ric/oran-sc-ric.sh up` then `oran-sc-ric.sh xapp`, or the same with `flexric.sh` |
| **edge** | Zenoh router + ROS2 ingest node | `bash deploy/lab/deploy.sh edge user@host` |
| **ue** | LiDAR + ROS2 client node, behind the 5G modem | `bash deploy/lab/deploy.sh ue user@host` |
| **gnb** | srsRAN metrics collector (beside the O-DU) | `bash deploy/lab/deploy.sh gnb user@host` |

Deploy **infra first** — every other role POSTs snapshots to it and will
buffer-then-drop while it is unreachable.

```
   UE host                5G lab                    Edge host        Infra host
┌────────────┐                                   ┌────────────┐   ┌────────────┐
│ LiDAR      │   Uu    ┌────────┐  ┌─────────┐   │ zenoh      │   │ logging    │
│ ros2 client├─modem───┤ srsRAN ├──┤ Open5GS ├───┤ router     │   │ service    │
│            │  USRP   │ gNB    │  │ core    │UPF│ edge node  ├──►│ postgres   │
│            │         │        │  │         │   │            │   │ admin :8099│
└─────┬──────┘         └───┬────┘  └─────────┘   └─────┬──────┘   └─────▲──────┘
      │                    │ metrics UDP               │                │
      │                    ▼                           │                │
      │              gNB host: ran-collector ──────────┼────────────────┘
      │                                                │
      └────── PTP grandmaster, management LAN ─────────┘
              (every measuring host on the SAME grandmaster)
```

## The middleware: Zenoh or Fast DDS

The data plane runs on one of two ROS middlewares, chosen at deployment and the
same on every host ([ADR-0013](../architecture/adr/0013-two-middlewares.md),
[middleware.md](../architecture/middleware.md)):

| `MIDDLEWARE` | `MIDDLEWARE_LINK` | Centre on the edge host |
|---|---|---|
| `zenoh` (default) | `tcp` (default), `quic` | Zenoh router: 7448/tcp, 7449/udp for QUIC |
| `dds` | `udp` (default), `tcp` | Fast DDS Discovery Server: 11811/udp or /tcp |

Either way the topology is a **star centred on the edge**, and the UE dials
the centre at `EDGE_HOST`. The image's entrypoint builds the middleware's
config from these variables; no compose file names an RMW or an endpoint.

## The star, with Zenoh

Every node is a Zenoh **client** of the router; the router relays between
them. There is no peer mesh, and none is needed: nothing ever dials the UE,
so a NAT at the UPF does not matter.

```text
UE host                                  Edge host
  lidar  ──┐                              ┌──► edge node
  render ──┼──tcp/EDGE_HOST:7448─────────►│
           │                        zenoh-router
```

The entrypoint sets, for `zenoh/tcp`:

```
ZENOH_CONFIG_OVERRIDE: 'mode="client";connect/endpoints=["tcp/${EDGE_HOST}:7448"]'
```

Two properties of that line are load-bearing.

**`mode="client"`.** Clients neither listen nor gossip, so nothing attempts a
direct link to a node that cannot accept one. In `peer` mode each session
also opens a localhost-only listener and gossips it, and every other host
then tries and fails to reach it — harmless noise, but it masks real faults.

**Merged, not replaced.** `ZENOH_CONFIG_OVERRIDE` merges; pointing
`ZENOH_SESSION_CONFIG_URI` at a file calls `Config::from_file()`, which
**replaces** rmw_zenoh's packaged config outright. Everything not restated is
silently lost — including the `listen`, `gossip` and interest settings that
relaying depends on. Only the endpoint and the mode differ from upstream, so
only those are set.

**No Zenoh over UDP.** Relayed key-expression declarations are lost over
Zenoh's UDP link on this build, and the router then drops every sample naming
a scope it never registered
([ros2/rmw_zenoh#765](https://github.com/ros2/rmw_zenoh/issues/765)). The
router listens on TCP only, plus QUIC on request; UDP is Fast DDS's.

### Zenoh over QUIC

`MIDDLEWARE_LINK=quic` makes the router listen on `quic/[::]:7449` as well, and
the clients dial it with the CA in `/zenoh/tls/ca.crt`. QUIC mandates TLS 1.3,
so the TLS material must exist first, and **zenoh treats a failed listener as
fatal** -- a router whose certificates are absent dies, and every node then
fails to resolve it, which looks like a DNS fault rather than a missing file:

```bash
bash scripts/gen-dev-tls.sh        # development CA, into deploy/docker/zenoh/tls/ (gitignored)
```

For the lab, generate certificates whose SAN covers the address each role
dials. The material is mounted read-only at runtime, never baked into an
image. Read [ADR-0006](../architecture/adr/0006-reliable-udp-transport.md)'s
methodology warning before comparing transports: `netem delay X jitter Y`
reorders packets, which penalises QUIC far more than TCP.

## The star, with Fast DDS

Every node is a Fast DDS **CLIENT** of the Discovery Server on the edge. It
learns its peers there, and data then flows **directly** between participants,
RTPS over UDP or TCP. Multicast discovery is never used: it does not cross the
UPF, which is ADR-0001's reason for Zenoh.

```text
UE host                                    Edge host
  lidar  ──discovery: udp/EDGE_HOST:11811──► dds-discovery
  render ◄═══════════ RTPS data ══════════► edge node
```

Three things differ from Zenoh, and each needs attention in the lab:

- **The edge sends to the UE's own address**, for the renderer's downlink and
  for reliable ACKs. The UE pool must be routable from the edge, which it is
  here: the UPF does not NAT it. Behind a NAT, use `dds/tcp` or Zenoh.
- **`DDS_INTERFACES` on every multi-homed host.** Fast DDS announces every
  interface, and a peer sends to any address it can reach. A UE and an edge
  that are both on the management LAN could exchange point clouds over it,
  skipping 5G. Set `DDS_INTERFACES` to the UE's 5G interface and the edge's
  N6 address.
- **Ports.** Discovery on 11811. Data on UDP 7400 and up (RTPS port mapping),
  or TCP 7410 on the edge for `dds/tcp`, which the UE dials out to.

## Which middleware a run used

The middleware and its link are chosen at deployment, not per run. Both are
fixed at `rclpy.init()`, so nothing can move a running node from one to
another. To compare them, redeploy with other values and **recreate** the
containers.

- **Each node reports what it really runs.** The admin page shows a badge per
  zone (e.g. "Fast DDS · UDP") and a **Middleware** column in the Nodes table.
- **The run records it.** Each participant's middleware and link go into
  `run.json` under `participants`, with a summary
  `middleware: {name, rmw, link}`, or `name: "mixed"` when the nodes
  disagreed.
- **A zone's nodes must agree.** If one runs another middleware or link, the
  admin raises `WF_TRANSPORT_MISMATCH` as an error:

  ```text
  client-ran-4-0 runs on 'dds/udp', while edge-mec-0 runs on 'zenoh/tcp'.
  ```

The gNB collector and the xApp have no ROS session and are not compared.

## How publish/subscribe reaches the edge (Zenoh)

ROS 2 topics become Zenoh **key expressions**; `rmw_zenoh` rewrites every `/`
to `%`, so `/mec_cast/cloud` is one chunk carrying topic, type and type hash.
Publisher and subscriber match only when topic, type **and** QoS all agree.

The router is not a packet forwarder — it is declaration-driven:

1. A subscriber declares an **interest**: "I want `mec_cast/cloud`".
2. The router forwards a publication **only** where a matching subscription
   exists. No subscriber means nothing crosses, by design.
3. Key expressions are interned: a session declares one and gets a numeric
   **scope id**, and later samples carry that integer instead of the string.
4. The router confirms with **`DeclareFinal`**.

When steps 3–4 fail the signature is distinctive, and it is a handshake that
never finished rather than a network fault:

```text
Didn't receive DeclareFinal for interest ...: Timeout(10s)!
Route data with unknown scope 42!
```

The render path is the same mechanism in reverse — the edge publishes
`mec_cast/result` (needs `PUBLISH_RESULT=1`) and the renderer subscribes,
relayed by the same router.

## Diagnosing "no traffic"

Three signals mean nothing on their own, and all three read as healthy while
nothing moves:

- **A matching ROS graph.** `ros2 topic info -v` is built from liveliness
  tokens the router serves directly. It will show a matching publisher and
  subscription, identical QoS and type hash, while not one sample crosses.
- **A climbing `frames_published`.** A publisher whose session has no
  transport increments it identically, with `samples_dropped: 0`.
- **`running` in the admin.** That is the node's claim about its own state
  machine, not evidence a session exists.

What does answer it, in order:

```bash
docker exec lab-ue-agent-1 bash -c 'source /opt/ros/jazzy/setup.bash && source /ws/install/setup.bash && ros2 node list'
```

Seeing only local nodes means the session never reached the router. Then
watch the handshake, restarting the node so it retries —
`ZENOH_ROUTER_CHECK_ATTEMPTS=-1` retries forever in silence rather than
failing loudly:

```bash
sudo timeout 45 tcpdump -ni any port 7448 -c 10 & sleep 1 && docker restart lab-ue-agent-1 && wait
```

On **Fast DDS**, `ros2 node list` from a plain CLIENT shows only what that
participant matched; ask as a SUPER_CLIENT, which the Discovery Server tells
everything:

```bash
docker exec -e DDS_DISCOVERY_PROTOCOL=SUPER_CLIENT lab-ue-agent-1 bash -lc 'source /middleware/middleware-env.sh && ros2 node list'
```

The edge missing from that list is a discovery fault: port 11811 blocked, or
`EDGE_HOST` wrong. The edge listed but no samples arriving is a data-path
fault: the RTPS ports (UDP 7400 and up, or TCP 7410), or a UE address the
edge cannot route back to. Watch both:

```bash
sudo timeout 45 tcpdump -ni any 'port 11811 or portrange 7400-7500' -c 20
```

## Required environment per role

```bash
export INFRA_HOST=10.0.0.10       # infra host: logging :8000 AND admin :8099
export EDGE_HOST=10.0.0.20        # edge host (UE and gNB roles)
export MIDDLEWARE=zenoh            # or dds -- the same on every host (ADR-0013)
export MIDDLEWARE_LINK=tcp         # zenoh: tcp | quic; dds: udp | tcp
export DDS_INTERFACES=...          # dds only, per host: the UE's 5G interface, the edge's N6 address
export CODEC=none                  # or gpcc (ADR-0014): the default codec, the same on ue and edge; a run may name its own
export RUN_ID=$(uuidgen)          # only without the admin; see below
```

`INFRA_HOST` names the **host**, not one service on it. Every measuring role
posts telemetry to `:8000` there and connects its control plane to `:8099`.
It was called `LOGGING_HOST` until the admin moved off the edge, at which
point the name covered only half of what it addresses; `deploy.sh` still
accepts the old name and says so once.

`RUN_ID` **must match across roles** — it becomes `trace_id`, the join key
that correlates UE, edge, and RAN records for one experiment.

With the admin service, do not set it. Every role defaults `ADMIN_URL` to
`ws://${INFRA_HOST}:8099/ws/node` — the admin runs on **infra**, one
authority for the fleet, because runs are per zone and an admin per edge would
be an authority per zone. The admin mints the run id, and the nodes
ignore `RUN_ID` entirely — which removes the "same value across all roles"
requirement that is easy to get wrong by hand. See
[admin-service.md](admin-service.md).

To run **without** it, pass an empty `ADMIN_URL` — omitting it is not enough,
since the default is a live address. The procedure is in
[deploy-manual.md](deploy-manual.md#without-the-admin-service).

## Deployment mechanism

`deploy/lab/deploy.sh` rsyncs the repo (excluding `third_party/`, `runs/`,
`target/`), builds there, and runs the role's compose file. For four hosts
this beats a configuration-management system: it stays debuggable at 2am in
the lab, which is when it will be used. If the host count grows past a
handful, revisit.

It also runs `verify-ptp.sh` on the target and warns loudly on failure.

### One-time setup per host

Three things bite on a host's first deploy — docker group membership, not
running `deploy.sh` under `sudo`, and key authentication. Each fails in a way
that points somewhere other than the cause, and all three are written out with
their symptoms in
[deploy-manual.md](deploy-manual.md#one-time-setup-per-machine).

## Pre-campaign checklist

Also in [deploy-manual.md](deploy-manual.md#pre-campaign-checklist), which is
where you will be when you need it:

1. `bash deploy/lab/ptp/verify-ptp.sh` on UE, edge and gNB — all must pass.
2. `curl -sf http://$INFRA_HOST:8000/health/ready`
3. Confirm the gNB exports metrics where the collector reads them: up to
   srsRAN 24.x, `gnb.yml` `metrics.addr/port` → the gNB host, port 55555; from
   25.04, `metrics.enable_json` plus `remote_control` (port 8001). The admin's
   gNB node shows `source`, `metrics_transport` and `ws_last_error` in its status.
4. One short smoke run; confirm `runs/<id>/{pub-0,edge-0,ran}/samples.csv` all
   appear and `context.ptp.reliable` is `true` in the snapshots.
