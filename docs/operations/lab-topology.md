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
              (ptp4l + phc2sys on every measuring host)
```

## Why the Zenoh router lives on the edge

The UE is behind the UPF's NAT. It dials **out** to the router, which is why
this works at all without port forwarding or a public UE address — see
[ADR-0001](../architecture/adr/0001-zenoh-over-dds.md). Set `EDGE_HOST` in
the UE role's environment to the router's reachable address.

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
