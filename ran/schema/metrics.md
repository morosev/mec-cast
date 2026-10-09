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
is kept forever (`ran/reports.jsonl`), so the normaliser can be re-run and
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
| `ue.sr_to_pusch_delay_{avg,max}` | "" | `sr_to_pusch_delay` | — | **unit** |
| `ue.pusch_harq_delay_{avg,max}` | "" | `pusch_harq_delay` | — | **unit** |
| `ue.pucch_harq_delay_{avg,max}` | "" | `pucch_harq_delay` | — | **unit** |
| `ue.crc_delay_{avg,max}`, `ue.ce_delay_{avg,max}` | "" | `crc_delay`, `ce_delay` | — | **unit** |
| `ue.max_pusch_distance` / `ue.max_pdsch_distance` | "" | same | — | **unit** |
| `ue.dl_rlc_sdu_volume_bits` / `ue.ul_rlc_sdu_volume_bits` | bit | — | `DRB.RlcSduTransmittedVolumeDL/UL` × 1000 (kbit) | KPM scale |
| `ue.dl_rlc_drop_rate` | "" | — | `DRB.RlcPacketDropRateDl` | **unit** |
| `ue.ul_packet_success_rate` | "" | — | `DRB.PacketSuccessRateUlgNBUu` | **unit** |
| `ue.kpm_dummy.{cqi,rsrp,rsrq}` | "" | — | `CQI`, `RSRP`, `RSRQ` | srsRAN documents these as dummies |
| `event.ue_create` / `event.ue_rem` / `event.ue_reconf` | count | `event_list` | — | |

## Per cell

| Metric | Unit | srsRAN JSON | E2SM-KPM | Verify |
|---|---|---|---|---|
| `cell.dl_throughput_bps` / `cell.ul_throughput_bps` | bit/s | `cell_metrics.dl_brate` / `ul_brate` (≤ 24.x) | `DRB.UEThpDl/Ul` × 1000, report styles 1–2 (node level) | KPM scale |
| `cell.error_indications` | count | `error_indication_count` | — | |
| `cell.failed_pdcch_allocs` / `cell.failed_uci_allocs` | count | `nof_failed_pdcch_allocs` / `nof_failed_uci_allocs` | — | |
| `cell.late_dl_harqs` / `cell.late_ul_harqs` | count | `late_dl_harqs` / `late_ul_harqs` | — | |
| `cell.msg3_ok` / `cell.msg3_nok` | count | `msg3_nof_ok` / `msg3_nof_nok` | — | |
| `cell.pusch_prbs_used_mean` / `cell.pdsch_prbs_used_mean` | prb | `p{u,d}sch_prbs_used_per_tdd_slot_idx` (mean over slots) | — | |
| `cell.avg_latency`, `cell.max_latency` | "" | `average_latency`, `max_latency` | — | **unit** |
| `cell.avg_prach_delay`, `cell.pucch_rb_usage_avg` | "" | same | — | **unit** |

## Identity: which UE is the LiDAR?

The two sources name UEs differently: JSON uses the RNTI, KPM uses the E2 UE
id. Neither is the client node.

- **In a single-UE cell** (the lab today), the mapping is trivial.
  `tools/ran_join.py` takes the only UE and says so.
- **With several UEs**, `ran_join.py --ue <rnti>` picks one explicitly.
  Without `--ue`, it picks the UE with the most uplink throughput (the LiDAR
  is the uplink-heavy one) and prints its choice.
- **JSON `event.ue_create` rows** carry the RNTI at attach. Correlating them
  with a UE's attach time is the route to an automatic mapping once more than
  one UE is in a cell.

## Cross-source parity

`ue.dl_throughput_bps` and `ue.ul_throughput_bps` are the one quantity both
sources measure. The collector and the xApp each report their latest UE sum to
the admin. When both are live and they differ beyond tolerance, the admin
raises `WF_RAN_SOURCES_DISAGREE`. That finding is also how a wrong KPM scale
(the *verify* column) shows itself in the lab.
