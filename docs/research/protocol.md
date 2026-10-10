# Experiment protocol

What a campaign measures, what it varies, how often, and what makes a run
count. The rules in [README.md](README.md) apply to every campaign. Results go
in `results/`, one note per campaign, citing every `run_id`.

## Campaign 1 — PRB reservation for the LiDAR UE

### Question

Does reserving uplink radio resources for the LiDAR UE shorten the tail of
its application latency when another UE competes for the cell?

A tail is the target, not the median. Teleoperation and perception offload
fail on the worst frames, and the scheduler is where a contended uplink
queues them.

**Hypotheses**
- **H1:** under competing uplink load, a PRB reservation for the LiDAR UE
  lowers the p99 of `network_ns` compared with no reservation.
- **H0:** it does not, beyond run-to-run variation.

### Stage 0 — calibration. All four must pass before any campaign run.

Each step settles something the campaign would otherwise assume.

| # | Check | How | Pass |
|---|---|---|---|
| 0.1 | **Which direction the control acts on.** oran-sc-ric describes its PRB-quota example as limiting *downlink* PRBs. The LiDAR traffic is *uplink*. `gnb-sim` caps both directions, so the local tests cannot answer this. | One UE with saturating iperf3 traffic in both directions. Apply `max_prb_ratio: 20`, hold 30 s, lift it. Read `ue.dl_throughput_bps` and `ue.ul_throughput_bps` from the JSON tap. | Recorded which of DL / UL / both drops. **If UL does not drop, stop:** this control cannot test H1, and the campaign needs a different lever (an uplink slice in `gnb.yml`, or a different RC action). |
| 0.2 | **Which E2 UE id is the LiDAR UE** | Attach the LiDAR UE alone; note its E2 UE id in `ran-kpm/kpi.csv` and its RNTI in `ran/kpi.csv`. Attach the competitor; note both again | A written mapping, re-checked at the start of every session (ids follow attach order) |
| 0.3 | **The two sources agree** | Steady uplink traffic, no policy, 2 min | No `WF_RAN_SOURCES_DISAGREE`; the KPM throughput scale confirmed (the *verify* column of `ran/schema/metrics.md`) |
| 0.4 | **Clocks** | `verify-ptp.sh --peer` between UE and edge hosts | Passes; `context.ptp.reliable` true on every measuring host |

### Factors

| Factor | Levels |
|---|---|
| Policy | **none**; **reserve**: LiDAR UE `min_prb_ratio: 50`; **cap**: competitor `max_prb_ratio: 50` |
| Competing uplink load | **none**; **saturating**: iperf3 UDP uplink from a second UE at a rate above the cell's capacity |
| Payload | `NUM_POINTS` 5000 and 30000, at `RATE_HZ` 10, pattern `lidar_scan` |

This gives 12 conditions. The *none / none* condition is the floor every
other one is compared against.

The **reserve** and **cap** levels answer different questions: protecting
one flow versus throttling the other. If Stage 0.1 shows the control acts
per direction, both levels are kept. If the srsRAN agent applies only one of
them meaningfully, record that and drop the other.

### Run design

- **The run is the unit of analysis.** Frames within a run are
  autocorrelated through the scheduler and the channel, so a run gives one
  observation of each statistic.
- **Count:** at least **5 runs per condition** (60 runs).
- **Length:** **120 s** each. The first **10 s** are excluded from analysis:
  scheduler and HARQ warm-up, policy settling.
- **Order:** randomised **within blocks**. Each block runs all 12 conditions
  once, so slow drift in the radio environment spreads across conditions
  instead of aliasing with one. Record the block number in the run label.
- **The policy arrives with the run** (`params.ran_policy`, ADR-0011), never
  set by hand between runs. Its `apply:ack` time in `ran-kpm/control.csv`
  starts the measured window; it must fall within the warm-up.
- **Netem off** (`NETEM=0`): the radio is the impairment under test.
- **Same everything else:** srsRAN version, `gnb.yml` (record its hash in
  the run label or the results note), UE positions, antenna placement. A
  change of any of these starts a new campaign, not a new block.

### A run is valid only if

1. `context.ptp.reliable` is true on UE and edge for the whole window, and
   Stage 0.4 passed that session.
2. No error-severity finding was raised during the run. In particular:
   `WF_POLICY_NOT_APPLIED`, `WF_CLOCK_SKEW`, `WF_RAN_SOURCES_DISAGREE`.
3. For policy conditions, `ran-kpm/control.csv` shows `apply:ack` and, after
   the stop, `revert-run-stop:ack`. A run whose revert is missing also
   invalidates **the next run**, until a revert is confirmed.
4. The recorder dropped nothing (drop counters zero), and at least 95 % of
   published frames reached the edge. Below that, the tail statistic
   describes survivors, not traffic.
5. The competitor's load was actually present when the condition said so:
   its `ue.ul_throughput_bps` in the JSON tap is near cell capacity.

Invalid runs are **kept and listed** in the results note with the reason.
They are re-run at the end of the block in which they failed, never silently
replaced.

### Responses

| Response | From | Statistic |
|---|---|---|
| One-way uplink delay | `edge-0/samples.csv`, `network_ns` | p50, p99, p99.9 over the window, whole-run (ADR-0004) |
| Sensor to processed | `edge-0/samples.csv`, `e2e_ns` | p50, p99 |
| Delivery | `pub-0` vs `edge-0` sequence numbers | fraction delivered |
| Mechanism | `ran/kpi.csv` joined per frame (`tools/ran_join.py`) | LiDAR UE's `ul_mcs`, `ul_harq_nok`, `ul_bsr_bytes`, `sr_to_pusch_delay`, PRB use |

### Analysis

- **Main comparison:** p99 `network_ns` per run, by condition. Report the
  difference between *reserve* and *none* under saturating load, with a
  bootstrap 95 % interval over runs (resample runs, not frames).
- **Supporting evidence, not proof:** the RAN responses explain *why* a
  difference exists, e.g. lower BSR and scheduling-request delay under
  reservation. Correlations from `ran_join.py` are descriptive; across 60
  runs they are worth reporting, within one run they are not.
- **Every figure cites its `run_id`s and the `ran_policy` each run carried.**

### Known limits

- One cell, one USRP, one srsRAN build. The result describes this
  scheduler, not 5G in general.
- The LiDAR source is synthetic (`lidar_scan`): realistic in size and rate,
  not in content. Compression is not under test.
- KPM reports at 1 s. Per-frame mechanism comes from the JSON tap only.
- E2 UE ids follow attach order. A UE that re-attaches mid-session invalidates
  the mapping from Stage 0.2 until it is re-checked.
