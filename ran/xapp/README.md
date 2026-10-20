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
`KPM_LOGGING=0` stops that and leaves `ran-kpm/` as it is; the node's
`logging` param says `on`, `off` or `no-url`. It is separate from the JSON
tap's `RAN_LOGGING`: either source can feed the logging dashboard's radio
panel alone (`ric.sh xapp` and `ran.yml` pass it through).

## Run parameters

| Param | Default | Meaning |
|---|---|---|
| `kpm_style` | 2 | E2SM-KPM report style (1, 2 and 5 wired in `osc`). Style 5 needs at least two UE ids |
| `kpm_metrics` | the five non-dummy DRB metrics OCUDU 26.10 serves | KPM measurement names. One the agent does not serve refuses the whole subscription |
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
- **`ue`** is an E2 UE id, or — under the admin — a client node_id such as
  `"client-ue-a1-0"`. The admin resolves a node through its `ue: {e2_ue_id}`
  hint in `topology.yml`, keeps the name as `ue_node`, and refuses (409) a
  node it cannot resolve. A standalone xApp takes ids only.
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

**Slice constraint (from oran-sc-ric @621ade2).** Its PRB-quota encoder
hard-codes PLMN `00101`, SST 1, SD 1. OCUDU matches that S-NSSAI **exactly**
against a slice in the gNB's `cell_cfg.slicing`, SD included, and the UE's
traffic must be in it. So SD 1 goes on the gNB, the core and the subscriber
([deploy manual, "The slice"](../../docs/operations/deploy-manual.md#the-slice-ran-control-acts-on)).
Otherwise every control is refused (`RIC_CONTROL_FAILURE` in `control.csv`,
`controls_failed` rises, `WF_POLICY_NOT_APPLIED`). The acknowledged control
used `dedicated_prb_ratio: 0`. The default of 100 has not been tried since
the slice was fixed.

**Open question: which direction the quota limits.** oran-sc-ric describes
its example as limiting the *downlink* PRBs available to a UE. The LiDAR
traffic is uplink. `gnb-sim` caps both directions, so the local tests cannot
tell. Calibration step 0.1 of the
[research protocol](../../docs/research/protocol.md) settles it on the lab
gNB before any campaign relies on it.

## Running it

| Where | How |
|---|---|
| Lab | `INFRA_HOST=<infra> bash deploy/lab/ric/ric.sh xapp` (`-d` to detach), after `ric.sh up`. See [the deploy manual](../../docs/operations/deploy-manual.md#the-gnb--metrics-tap-e2-and-the-ric) |
| Local | `make up-ran` / `make up-ran-admin`: the `xapp` service in `deploy/compose/ran.yml`, `E2_ADAPTER=sim` |
| Tests | `make test-ran`, or `pytest` here. The sim test runs the xApp against gnb-sim's E2 feed in-process |
| End to end | `pytest tests/e2e/test_ran_control.py`: the whole system under the admin, a 25 % cap seen in both RAN sources |

**What is verified, and what is not yet.**
- Everything above the adapter runs in tests and in the end-to-end test.
- **Against a real E2 agent (OCUDU 26.10, October 2026)**, on the single-host
  testbed ([guides/local-5g-ocudu.md](../../docs/guides/local-5g-ocudu.md),
  Part 3): KPM subscription and indications with style 2 and the five default
  metrics. `kpi.csv` joined to 3147 of 3154 frames. A PRB-quota control,
  stepped at 30 s and reverted at stop, was ACKed by the gNB.
- **Rejected subscriptions are detected.** The REST subscribe succeeds before
  the E2 node answers, and a refusal comes back as E2EventInstanceId 0. The
  adapter raises `SubscriptionRejected`, the node's `kpm_error` says so, and
  it retries after 60 s instead of claiming to be subscribed.
- Not yet verified: which direction the PRB quota limits (calibration step
  0.1), and the lab gNB itself. Run `ric.sh kpm` there first.
