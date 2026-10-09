# ran/xapp — the mec-cast E2 xApp

The standards-aligned RAN source of
[ADR-0010](../../docs/architecture/adr/0010-two-ran-sources.md). It runs
E2SM-KPM monitoring and E2SM-RC control on the O-RAN SC near-RT RIC, beside
the JSON tap in [`ran/collector`](../collector/README.md). Both sources write
the same [RAN data model](../schema/metrics.md).

```
admin (ws) ──► mec_cast_xapp ──► E2Port ──► osc: oran-sc-ric (lab)
                 │  capabilities              sim: gnb-sim's E2 feed (local)
                 ├─ kpm_monitor   → ran-kpm/kpi.csv, indications.jsonl
                 └─ rc_control    → ran-kpm/control.csv  (ADR-0011)
```

## Design

- **`core.py`.** Two seams:
  - `E2Port` is everything a capability may ask of a RIC.
  - `Capability` is one behaviour, bound to the run lifecycle: `on_boot`,
    `on_start`, `on_policy`, `tick`, `on_stop`, `status`.

  New behaviour is a new capability, registered by name. A third party
  registers under the `mec_cast_xapp.capabilities` entry-point group. An
  unknown name is an error, not a no-op.
- **Adapters.**
  - `osc` runs inside oran-sc-ric's `python_xapp_runner`. The RIC's routing
    table sends indications and control acks to that container's address.
    It uses `ricxappframe` (Apache-2.0) and imports oran-sc-ric's
    E2SM-KPM/RC encoders **at runtime from the RIC's checkout**; nothing of
    theirs (AGPL) is copied here. Its own receive loop records
    RIC_CONTROL_ACK/FAILURE.
  - `sim` talks to `gnb-sim`'s E2 feed. Its indications already have the
    decoded shape, so everything above the adapter runs the lab's code.
- **`app.py`.** The admin client, which is the same ROS-free client the ROS
  nodes use, plus the run lifecycle and status.
  - Standalone (no `ADMIN_URL`), it records under `RUN_ID`.
  - Under the admin, it records the runs it is given, and `run.start` args
    override the environment defaults.
- **Python 3.8:** the RIC's xApp runner is `python:3.8-slim`. CI tests on 3.8,
  and the local image is built on 3.8.

## Output

Each run writes `runs/<run_id>/ran-kpm/`. The admin's manifest names this leaf
for an `xapp` node.

| File | What |
|---|---|
| `kpi.csv` | Normalised rows: `source=kpm`, `ue=e2:<id>` (ran/schema/metrics.md) |
| `indications.jsonl` | Every decoded indication verbatim, plus `recv_ns` |
| `control.csv` | Every control: `sent`, then `ack` or `failed`, with ratios and the RIC's answer |

Logging service: `service=mec-cast-ran-kpm`, `trace_id=run_id`, raw as
`context.kpi`, rows as `context.norm`, controls as `context.control`.

## Run parameters

| Param | Default | Meaning |
|---|---|---|
| `kpm_style` | 5 | E2SM-KPM report style (1, 2 and 5 wired in `osc`) |
| `kpm_metrics` | the six non-dummy DRB metrics srsRAN exposes | KPM measurement names |
| `kpm_ue_ids` | `[0]` | E2 UE ids for styles 2 and 5 |
| `kpm_period_ms` | 1000 | srsRAN's minimum |
| `e2_node_id` | first connected | which E2 node |
| `ran_policy` | — | see below |

The environment sets defaults: `KPM_STYLE`, `KPM_METRICS`, `KPM_UE_IDS`,
`E2_NODE_ID`.

## RAN control: `ran_policy`

```json
{"type": "prb_quota", "ue": 0, "min_prb_ratio": 0, "max_prb_ratio": 30,
 "schedule": [{"t_s": 30, "max_prb_ratio": 100}]}
```

- Percent of the cell's PRBs, applied through E2SM-RC Control Style 2,
  Action 6.
- Give it when creating the run:
  `POST /api/v1/runs {"params": {"ran_policy": ...}}`.
- Or send it mid-run: `POST /api/v1/runs/<id>/ran-policy {"ran_policy": ...}`.
- The admin validates it on the way in (`ranpolicy.py`), and the xApp
  validates it again.
- **The rules (ADR-0011).** It is reverted:
  - on run stop;
  - when the supervisor fails the run;
  - when the policy is replaced;
  - after 30 s without the admin;
  - and, after a crash, by the next process at boot (`.xapp-policy.json`).
- The node's `policy_state` says whether it is actually in force.
  `WF_POLICY_NOT_APPLIED` is an error.

**Lab constraint (from oran-sc-ric @621ade2).** Its PRB-quota encoder
hard-codes PLMN `00101`, SST 1, SD 1. If the lab slice differs, controls are
refused: `controls_failed` rises and the finding fires.

## Running it

| Where | How |
|---|---|
| Lab | `INFRA_HOST=<infra> bash deploy/lab/ric/ric.sh xapp` (`-d` to detach), after `ric.sh up`. See [the deploy manual](../../docs/operations/deploy-manual.md#the-gnb--metrics-tap-e2-and-the-ric) |
| Local | `make up-ran` / `make up-ran-admin`: the `xapp` service in `deploy/compose/ran.yml`, `E2_ADAPTER=sim` |
| Tests | `make test-ran`, or `pytest` here. The sim test runs the xApp against gnb-sim's E2 feed in-process |
| End to end | `pytest tests/e2e/test_ran_control.py`: the whole system under the admin, a 25 % cap seen in both RAN sources |

**What is verified, and what is not yet.**
- Everything above the adapter runs in tests and in the end-to-end test.
- The `osc` adapter has been brought up inside the real oran-sc-ric runner,
  locally under emulation. It imports the RIC's library, initialises RMR,
  queries e2mgr and reports to the admin.
- Not yet verified: subscription, indications and control against a real
  srsRAN E2 agent. That is the lab's first job, and the reason it starts with
  `ric.sh kpm`, the stock monitor.
