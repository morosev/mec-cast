# Lab topology and deployment

## Hosts and roles

| Role | Runs | Deploy |
|---|---|---|
| **infra** | Logging service + PostgreSQL | `bash deploy/lab/deploy.sh infra user@host` |
| **edge** | Zenoh router + ROS2 ingest node | `bash deploy/lab/deploy.sh edge user@host` |
| **ue** | LiDAR + ROS2 client node, behind the 5G modem | `bash deploy/lab/deploy.sh ue user@host` |
| **gnb** | srsRAN metrics collector (beside the gNB) | `bash deploy/lab/deploy.sh gnb user@host` |

Deploy **infra first** — every other role POSTs snapshots to it and will
buffer-then-drop while it is unreachable.

```
   UE host                5G lab                    Edge host        Infra host
┌────────────┐                                   ┌────────────┐   ┌────────────┐
│ LiDAR      │   Uu    ┌────────┐  ┌─────────┐   │ zenoh      │   │ logging    │
│ ros2 client├─modem───┤ srsRAN ├──┤ Open5GS ├───┤ router     │   │ service    │
│            │  USRP   │ gNB    │  │ core    │UPF│ edge node  ├──►│ postgres   │
└─────┬──────┘         └───┬────┘  └─────────┘   └─────┬──────┘   └─────▲──────┘
      │                    │ metrics UDP              │                 │
      │                    ▼                          │                 │
      │              gNB host: ran-collector ─────────┼─────────────────┘
      │                                               │
      └────── PTP grandmaster, management LAN ────────┘
              (ptp4l + phc2sys on every measuring host)
```

## Why the Zenoh router lives on the edge

The UE is behind the UPF's NAT. It dials **out** to the router, which is why
this works at all without port forwarding or a public UE address — see
[ADR-0001](../architecture/adr/0001-zenoh-over-dds.md). Set `EDGE_HOST` in
the UE role's environment to the router's reachable address.

## Required environment per role

```bash
export RUN_ID=$(uuidgen)          # same value across ALL roles for one run
export LOGGING_HOST=10.0.0.10     # infra host
export EDGE_HOST=10.0.0.20        # edge host (UE role only)
```

`RUN_ID` **must match across roles** — it becomes `trace_id`, the join key
that correlates UE, edge, and RAN records for one experiment.

## Deployment mechanism

`deploy/lab/deploy.sh` rsyncs the repo (excluding `third_party/`, `runs/`,
`target/`), builds there, and runs the role's compose file. For four hosts
this beats a configuration-management system: it stays debuggable at 2am in
the lab, which is when it will be used. If the host count grows past a
handful, revisit.

It also runs `verify-ptp.sh` on the target and warns loudly on failure.

## Pre-campaign checklist

1. `bash deploy/lab/ptp/verify-ptp.sh` on UE, edge, and gNB — all must pass.
2. `curl -sf http://$LOGGING_HOST:8000/health/ready`
3. Confirm the gNB's `gnb.yml` `metrics.addr/port` points at the gNB host's
   collector (default port 55555).
4. One short smoke run; confirm `runs/<id>/{pub,edge,ran}/samples.csv` all
   appear and `context.ptp.reliable` is `true` in the snapshots.

Step 4 is what catches a mis-set `RUN_ID` before it costs you a campaign.
