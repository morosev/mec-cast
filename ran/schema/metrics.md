# The RAN data model

One schema for both RAN sources ([ADR-0010](../../docs/architecture/adr/0010-two-ran-sources.md)):

- **json**: srsRAN's JSON metrics, read by `ran/collector` (Rust).
- **kpm**: E2SM-KPM indications, read by `ran/xapp` (Python).

Both normalise into the same row, so analysis never needs to know which source
produced it:

```
source,gnb_ts_ns,recv_ns,cell,ue,metric,value,unit
```

| Column | json | kpm |
|---|---|---|
| `gnb_ts_ns` | the report's `timestamp` (epoch seconds or zoneless ISO, read as UTC) | the indication header's `colletStartTime`, plus one granularity period per extra value |
| `recv_ns` | collector arrival, on the shared telemetry clock | xApp arrival, same clock |
| `cell` | PCI | E2 node id |
| `ue` | RNTI; "" for cell rows | `e2:<gNB-CU-UE-F1AP-ID>`; "" for node-level rows |

**Files.** The collector writes `runs/<run_id>/ran/kpi.csv` and the xApp
writes `runs/<run_id>/ran-kpm/kpi.csv`. Both also log the rows as
`context.norm`, beside the untouched raw report in `context.kpi`. The raw form
is kept forever (`runs/<run_id>/ran/reports.jsonl`), so the normaliser can be re-run and
improved after the fact.

**Two implementations, one contract.** `ran/py` (`mec_cast_ran`, standard
library only, Python ≥ 3.8 for the RIC's xApp runner) and
`ran/collector/src/normalise.rs` both test against
[`vectors.json`](vectors.json). Change a mapping in both and in this table, and
add a vector for it.

## Rules

- **Known field:** a numeric field listed below becomes its canonical metric,
  scaled to the canonical unit.
- **Unknown numeric field:** becomes `ue.raw.<field>`, `cell.raw.<field>` or
  `<scope>.raw.<KPM name>`, with unit "". It is kept, but not blessed.
- **`{avg, max}` object:** one row per numeric key, `<metric>_<key>`.
- **Numeric array:** one row, `<metric>_mean`.
- **Not a measurement:** identity fields (`rnti`, `pci`, `ue`, `timestamp`),
  booleans, strings and nulls produce no row.
- **`event_list` entries:** each becomes `event.<event_type>` = 1 for its RNTI.
- **Undocumented units:** where srsRAN does not document the unit, the unit
  is **""**, not a guess. Each such row is marked *verify* below and gets a
  unit only after the lab confirms one.

## Per UE

| Metric | Unit | srsRAN JSON | E2SM-KPM | Verify |
|---|---|---|---|---|
| `ue.dl_throughput_bps` | bit/s | `dl_brate` | `DRB.UEThpDl` × 1000 (kbit/s) | KPM scale |
| `ue.ul_throughput_bps` | bit/s | `ul_brate` | `DRB.UEThpUl` × 1000 (kbit/s) | KPM scale |
| `ue.dl_mcs` / `ue.ul_mcs` | index | `dl_mcs` / `ul_mcs` | — | |
| `ue.cqi` | index | `cqi` | — (srsRAN's KPM CQI is a dummy: `ue.kpm_dummy.cqi`) | |
| `ue.dl_ri` / `ue.ul_ri` | layers | `dl_ri` (or `ri`) / `ul_ri` | — | |
| `ue.dl_harq_ok` / `ue.dl_harq_nok` | count | `dl_nof_ok` / `dl_nof_nok` | — | per report period |
| `ue.ul_harq_ok` / `ue.ul_harq_nok` | count | `ul_nof_ok` / `ul_nof_nok` | — | per report period |
| `ue.dl_buffer_bytes` | byte | `dl_bs` | — | |
| `ue.ul_bsr_bytes` | byte | `bsr` | — | |
| `ue.phr_db` | dB | `last_phr` | — | |
| `ue.pusch_snr_db` / `ue.pucch_snr_db` | dB | `pusch_snr_db` / `pucch_snr_db` | — | |
| `ue.pusch_rsrp_db` | dB | `pusch_rsrp_db` | — (KPM RSRP is a dummy) | |
| `ue.ta_ns`, `ue.{pusch,pucch,srs}_ta_ns` | ns | `ta_ns`, `pusch_ta_ns`, … | — | |
| `ue.sr_to_pusch_delay_{avg,max}` | "" | `sr_to_pusch_delay` `{avg, max}`; OCUDU 26.10: flat `avg_sr_to_pusch_delay` / `max_sr_to_pusch_delay` | — | **unit** |
| `ue.pusch_harq_delay_{avg,max}` | "" | `pusch_harq_delay`; OCUDU: `avg_`/`max_pusch_harq_delay` | — | **unit** |
| `ue.pucch_harq_delay_{avg,max}` | "" | `pucch_harq_delay`; OCUDU: `avg_`/`max_pucch_harq_delay` | — | **unit** |
| `ue.crc_delay_{avg,max}`, `ue.ce_delay_{avg,max}` | "" | `crc_delay`, `ce_delay`; OCUDU: `avg_`/`max_crc_delay`, `avg_`/`max_ce_delay` | — | **unit** |
| `ue.max_pusch_distance` / `ue.max_pdsch_distance` | "" | same | — | **unit** |
| `ue.dl_rlc_sdu_volume_bits` / `ue.ul_rlc_sdu_volume_bits` | bit | — | `DRB.RlcSduTransmittedVolumeDL/UL` × 1000 (kbit) | KPM scale |
| `ue.dl_rlc_drop_rate` | "" | — | `DRB.RlcPacketDropRateDl` | **unit** |
| `ue.ul_packet_success_rate` | "" | — | `DRB.PacketSuccessRateUlgNBUu` (not served by OCUDU 26.10: requesting it refuses the whole subscription) | **unit** |
| `ue.kpm_dummy.{cqi,rsrp,rsrq}` | "" | — | `CQI`, `RSRP`, `RSRQ` | srsRAN documents these as dummies |
| `event.ue_create` / `event.ue_rem` / `event.ue_reconf` | count | `event_list` | — | |

## Per cell

| Metric | Unit | srsRAN JSON | E2SM-KPM | Verify |
|---|---|---|---|---|
| `cell.dl_throughput_bps` / `cell.ul_throughput_bps` | bit/s | `cell_metrics.dl_brate` / `ul_brate` (≤ 24.x) | `DRB.UEThpDl/Ul` × 1000, report styles 1–2 (node level) | KPM scale |
| `cell.error_indications` | count | `error_indication_count` | — | |
| `cell.failed_pdcch_allocs` / `cell.failed_uci_allocs` | count | `nof_failed_pdcch_allocs` / `nof_failed_uci_allocs` | — | |
| `cell.failed_{dl,ul}_pdcch_allocs`, `cell.failed_common_{dl,ul}_pdcch_allocs`, `cell.failed_fallback_uci_allocs` | count | OCUDU 26.10, which splits `nof_failed_pdcch_allocs`: `failed_dl_pdcch`, `failed_ul_pdcch`, `failed_common_dl_pdcch`, `failed_common_ul_pdcch`, `failed_fallback_uci_allocs` | — | |
| `cell.late_dl_harqs` / `cell.late_ul_harqs` | count | `late_dl_harqs` / `late_ul_harqs` | — | |
| `cell.msg3_ok` / `cell.msg3_nok` / `cell.msg3_dtx` | count | `msg3_nof_ok` / `msg3_nof_nok` / `msg3_nof_dtx` (OCUDU) | — | |
| `cell.pusch_prbs_used_mean` / `cell.pdsch_prbs_used_mean` | prb | `p{u,d}sch_prbs_used_per_tdd_slot_idx` (mean over slots). **TDD only:** an FDD cell reports no PRB use at all on OCUDU 26.10, per cell or per UE, so these rows are absent there | — | |
| `cell.avg_latency`, `cell.max_latency` | "" | `average_latency`, `max_latency` | — | **unit** |
| `cell.avg_prach_delay`, `cell.pucch_rb_usage_avg` | "" | same | — | **unit** |

## OCUDU's other layers: RLC and DU-MAC

OCUDU 26.10 sends these as messages of their own beside the scheduler's
`cells[]` report. The JSON tap only; KPM has its own RLC volume and drop rate
(above).

**RLC, per UE and DRB.** One `rlc_metrics` message per UE and DRB. `rx` is
what the gNB received (the uplink, the LiDAR's direction), `tx` what it sent.
The message names its UE by the DU's UE index (`ue_id`), not the RNTI. The
scheduler message carries both (`ue`, `rnti`), so the normaliser keeps that
mapping across a gNB's stream (`UeIndex`) and labels the rows with the RNTI.
Until the first scheduler message the UE is `du-ue:<index>` and the cell "".
The mapping assumes `rlc_metrics.ue_id` is the same index as the scheduler's
`ue`, which holds in the single-UE capture: **verify** with two UEs.

| Metric (`ue.rlc.drb<id>.`…) | Unit | OCUDU JSON | Verify |
|---|---|---|---|
| `ul_sdus` / `ul_sdu_bytes` | count / byte | `rx.num_sdus` / `rx.num_sdu_bytes` | |
| `ul_pdus` / `ul_pdu_bytes` | count / byte | `rx.num_pdus` / `rx.num_pdu_bytes` | |
| `ul_lost_pdus` / `ul_malformed_pdus` | count | `rx.num_lost_pdus` / `rx.num_malformed_pdus` | |
| `dl_sdus` / `dl_sdu_bytes` | count / byte | `tx.num_sdus` / `tx.num_sdu_bytes` | |
| `dl_dropped_sdus` / `dl_discarded_sdus` / `dl_discard_failures` | count | `tx.num_dropped_sdus` / `tx.num_discarded_sdus` / `tx.num_discard_failures` | |
| `dl_sdu_latency_avg_ns` | ns | `tx.sum_sdu_latency_us` × 1000 / `tx.num_sdus` (absent when no SDUs) | |
| `dl_pdu_latency_avg_ns` | ns | `tx.sum_pdu_latency_ns` / `tx.num_pdus`, when OCUDU sends a PDU count (26.10 does not) | |
| `dl_pdu_latency_max_ns` | ns | `tx.max_pdu_latency_ns` | |
| `<ul\|dl>_raw.<field>` | "" | any other numeric `rx` / `tx` field; `pull_latency_histogram` is not normalised | |

**DU-high MAC processing, per cell.** `du.du_high.mac.<dl|ul>[]`, one entry
per cell (`pci`): the MAC's own processing time, not radio latency.

| Metric (`cell.mac.`…) | Unit | OCUDU JSON | Verify |
|---|---|---|---|
| `dl_latency_avg_ns` / `_max_ns` / `_min_ns` | ns | `average_latency_us` / `max_latency_us` / `min_latency_us` × 1000 | |
| `dl_cpu_usage` | "" | `cpu_usage_percent` (26.10 reports ~0.001 under load: percent or fraction?) | **unit** |
| `<dl\|ul>_raw.<field>` | "" | any other numeric field | |

## Identity: which UE is the LiDAR?

The two sources name UEs differently: JSON uses the RNTI, KPM uses the E2 UE
id. Neither is the client node.

- **Record the mapping in `topology.yml`.** A client node may declare
  `ue: {e2_ue_id: 0, rnti: 17921}` — what was observed at session start
  (research protocol step 0.2). Every run freezes the cell's hints into
  `run.json` as `ue_map`, so a run keeps the mapping it ran under.
- **Then name the node.** `ran_join.py --ue client-ue-a1-0` maps it through
  that run's `ue_map` (RNTI for json, E2 id for kpm), and a `ran_policy` may
  say `"ue": "client-ue-a1-0"`.
- **Without a declaration:** in a single-UE cell `ran_join.py` takes the only
  UE and says so; with several, `--ue <rnti>` or `--ue e2:<id>`, or by default
  the UE with the most uplink throughput (the LiDAR is the uplink-heavy one).
- **JSON `event.ue_create` rows** carry the RNTI at attach. Correlating them
  with a UE's attach time is the route to an automatic mapping once more than
  one UE is in a cell.

## Cross-source parity

`ue.dl_throughput_bps` and `ue.ul_throughput_bps` are the one quantity both
sources measure. The collector and the xApp each report their latest UE sum to
the admin. When both are live and they differ beyond tolerance, the admin
raises `WF_RAN_SOURCES_DISAGREE`. That finding is also how a wrong KPM scale
(the *verify* column) shows itself in the lab.
