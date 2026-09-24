# Lab topology and deployment

## Hosts and roles

| Role | Runs | Deploy |
|---|---|---|
| **infra** | Admin service (:8099) + logging service + PostgreSQL | `bash deploy/lab/deploy.sh infra user@host` |
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

## Why the Zenoh router lives on the edge

The UE is behind the UPF's NAT. It dials **out** to the router, which is why
this works at all without port forwarding or a public UE address — see
[ADR-0001](../architecture/adr/0001-zenoh-over-dds.md). Set `EDGE_HOST` in
the UE role's environment to the router's reachable address.

## The Zenoh topology is a star

Every node is a Zenoh **client** of the router; the router relays between
them. There is no peer mesh, and there cannot be one: the UE sits behind the
UPF's NAT and the edge's node reaches its router over loopback, so neither
can be dialled by the other.

```text
UE host                                  Edge host
  lidar  ──┐                              ┌──► edge node
  render ──┼──tcp/EDGE_HOST:7448─────────►│
           │                        zenoh-router
```

Set per compose file, merged into `rmw_zenoh`'s packaged config:

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

**TCP, not `udp/…?rel=1`.** Relayed key-expression declarations are lost over
the UDP link on this build, and the router then drops every sample naming a
scope it never registered
([ros2/rmw_zenoh#765](https://github.com/ros2/rmw_zenoh/issues/765)). Measured
on the e2e suite: TCP relay 8/8 with zero errors; the same star over UDP
failed 4–8 of 8 with dozens of `unknown scope`. See the 2026-09-23 amendment
in [ADR-0006](../architecture/adr/0006-reliable-udp-transport.md). The router
still listens on `udp/[::]:7447?rel=1` alongside `tcp/[::]:7448` so that
transport remains available to experiments.

## Choosing a transport

The router listens on three endpoints at once, so a role's transport is
whichever one it dials:

| Endpoint | State |
|---|---|
| `tcp/[::]:7448` | **what the platform runs on** — correct and fast |
| `udp/[::]:7447?rel=1` | listening, but **cannot relay on this build** — declarations are lost |
| `quic/[::]:7449` | **not listening by default** — opt-in, see below |

Change a role by changing one line, then **recreate the container** — the
link is opened once at process start:

```
ZENOH_CONFIG_OVERRIDE: 'mode="client";connect/endpoints=["quic/${EDGE_HOST}:7449"];transport/link/tls/root_ca_certificate="/zenoh/tls/ca.crt"'
```

### Enabling QUIC

The router does **not** listen on `quic/` by default. It needs TLS material
that is gitignored, and **zenoh treats a failed listener as fatal** -- listing
an endpoint whose certificates are absent kills the whole router, taking the
working TCP and UDP listeners with it. Every node then fails to resolve a
container that has just exited, which looks like a DNS fault rather than a
missing file.

So generate the material first:

```bash
bash scripts/gen-dev-tls.sh
```

then add the listener to that deployment's router, which merges rather than
replacing:

```
ZENOH_CONFIG_OVERRIDE: 'listen/endpoints=[udp/[::]:7447?rel=1,tcp/[::]:7448,quic/[::]:7449]'
```

That writes a development CA into `deploy/docker/zenoh/tls/`, which is
gitignored and mounted read-only at runtime rather than baked into an image —
a private key must never enter a layer. For the lab, generate certificates
whose SAN covers the address each role dials, not the dev ones.

Measurements and the reasoning behind the default are in
[ADR-0006](../architecture/adr/0006-reliable-udp-transport.md), including a
warning worth reading before any transport comparison: `netem delay X jitter Y`
reorders packets, which penalises QUIC far more than TCP and is not how a real
link behaves.

## Recording which transport a run used

A run can declare its transport in **New Run**, and the admin verifies it.

**It is recorded, not applied.** A Zenoh session opens one link at
`rclpy.init()`, so nothing can move a running node from TCP to QUIC — the
field labels the run's data for provenance. Each node reports the transport it
is really on, and a disagreement raises `WF_TRANSPORT_MISMATCH` as an error:

```text
Run declares transport 'quic' but client-ran-4-0 is connected over 'tcp'.
```

Leave it as *(not recorded)* if you are not comparing transports. `udp-rel1`
and `udp-rel0` are kept distinct because they are different experiments.

## How publish/subscribe reaches the edge

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

## Required environment per role

```bash
export INFRA_HOST=10.0.0.10       # infra host: logging :8000 AND admin :8099
export EDGE_HOST=10.0.0.20        # edge host (UE and gNB roles)
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
authority for the fleet, because runs are per cell and an admin per edge would
be an authority per cell. The admin mints the run id, and the nodes
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
3. Confirm the gNB's `gnb.yml` `metrics.addr/port` points at the gNB host's
   collector (default port 55555).
4. One short smoke run; confirm `runs/<id>/{pub-0,edge-0,ran}/samples.csv` all
   appear and `context.ptp.reliable` is `true` in the snapshots.
