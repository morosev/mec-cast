# ran/xapp — the mec-cast E2 xApp

The standards-aligned RAN source of
[ADR-0010](../../docs/architecture/adr/0010-two-ran-sources.md). It runs
E2SM-KPM monitoring and E2SM-RC control on a near-RT RIC, beside the JSON
tap in [`ran/collector`](../collector/README.md). Both sources write the same
[RAN data model](../schema/metrics.md). Two RICs are supported, side by side
([ADR-0012](../../docs/architecture/adr/0012-two-near-rt-rics.md)): the O-RAN
SC RIC (srsRAN's oran-sc-ric) and FlexRIC (OpenAirInterface).

```
admin (ws) ──► mec_cast_xapp ──► E2Port ──► osc:     oran-sc-ric            (lab, local-5G)
                 │  capabilities              flexric: FlexRIC, through
                 │                                     mec-cast-e2-bridge (C)
                 │                            sim:     gnb-sim's E2 feed      (local, CI)
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
  - `flexric` runs `mec-cast-e2-bridge` ([`flexric/`](flexric/)), a small
    FlexRIC xApp in C, as a child process. FlexRIC's Python SDK has no KPM or
    RC, so the E2 work is done there, against FlexRIC's C SDK. The bridge
    speaks the JSON E2 feed protocol on its stdin and stdout, the protocol
    `gnb-sim` defines, so `flexric` and `sim` share their protocol code
    (`adapters/jsonfeed.py`). It also works around two FlexRIC v3.0.0
    behaviours. FlexRIC reports a refused control as a success, so the bridge
    reads the ack or failure from FlexRIC's own output. FlexRIC aborts on a
    refused subscription, so the bridge checks the metric list against what
    the gNB advertised first, and the adapter reports an abort as a refusal.
    Variables: `FLEXRIC_BRIDGE`, `FLEXRIC_CONF`, and `FLEXRIC_SLICE`, the
    PLMN:SST:SD a PRB quota names (default `00101:1:1`).
  - `sim` talks to `gnb-sim`'s E2 feed. Its indications already have the
    decoded shape, so everything above the adapter runs the lab's code.
- **`app.py`.** The admin client, which is the same ROS-free client the ROS
  nodes use, plus the run lifecycle and status.
  - Standalone (no `ADMIN_URL`), it records under `RUN_ID`.
  - Under the admin, it records the runs it is given, and `run.start` args
    override the environment defaults.
- **Python 3.8:** the oran-sc-ric xApp runner is `python:3.8-slim`. CI tests
  on 3.8, and the local image is built on 3.8. The FlexRIC image runs it on
  Ubuntu 22.04's Python 3.10.

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
panel alone (`oran-sc-ric.sh xapp` and `ran.yml` pass it through).

## Run parameters

| Param | Default | Meaning |
|---|---|---|
| `kpm_style` | 2 | E2SM-KPM report style: 1, 2 and 5 wired in `osc` (style 5 needs at least two UE ids); 1 and 4 in `flexric`, whose default is 4 (`flexric.sh` sets it) |
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

**Slice constraint (from oran-sc-ric @621ade2; on FlexRIC the slice is
`FLEXRIC_SLICE`, which can be set).** Its PRB-quota encoder
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
| Lab, oran-sc-ric | `INFRA_HOST=<infra> bash deploy/lab/ric/oran-sc-ric.sh xapp` (`-d` to detach), after `oran-sc-ric.sh up`. See [the deploy manual](../../docs/operations/deploy-manual.md#the-gnb--metrics-tap-e2-and-the-ric) |
| Lab, FlexRIC | `INFRA_HOST=<infra> bash deploy/lab/ric/flexric.sh xapp` (`-d` to detach), after `flexric.sh up`. `flexric.sh emu` adds FlexRIC's emulated gNB, for a test without a radio |
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
- **FlexRIC (`flexric`), against FlexRIC v3.0.0's emulated gNB**
  (`flexric.sh emu`, October 2026):
  - E2 setup; KPM style 4 indications, normalised into `ran-kpm/kpi.csv`
    (`ue=e2:<id>`) and the logging service;
  - an unsupported metric and style 2 refused with the reason;
  - a PRB quota applied, stepped and reverted, each acknowledged.

  `tests/test_flexric.py` covers the adapter against a fake bridge in CI:
  refusal, ack and failure, the slice, FlexRIC's abort, and a bridge restart.
- Not yet verified:
  - which direction the PRB quota limits (calibration step 0.1);
  - the lab gNB itself (run `oran-sc-ric.sh kpm` or `flexric.sh kpm` there
    first);
  - and, on FlexRIC, whether OCUDU accepts the bridge's encoding: style 4
    with an S-NSSAI condition, and the RC parameter tree.

  The deploy manual's step 6b lists which step proves which.
