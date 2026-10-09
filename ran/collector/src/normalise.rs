//! srsRAN JSON report -> `RanSample` rows: the collector's half of the RAN
//! data model (ADR-0010).
//!
//! The Python package `ran/py` (`mec_cast_ran`) implements the same rules for
//! analysis and for the E2 xApp. `ran/schema/vectors.json` is the contract
//! between them, and both are tested against it; the catalogue of canonical
//! names and units is `ran/schema/metrics.md`. Change all three together.
//!
//! Rules, in short: every numeric UE field becomes `ue.<canonical>` (or
//! `ue.raw.<field>` when the field is unknown), an `{avg, max}` object one row
//! per key, a numeric array its `_mean`; cells likewise under `cell.`; each
//! `event_list` entry `event.<type>` = 1. Identity fields, booleans, strings
//! and nulls are not measurements. A unit srsRAN does not document is "".

use serde_json::{json, Value};

use crate::report_timestamp_ns;

/// One metric for one cell or UE at one gNB timestamp.
#[derive(Clone, Debug, PartialEq)]
pub struct RanSample {
    pub gnb_ts_ns: i64,
    pub cell: String,
    pub ue: String,
    pub metric: String,
    pub value: f64,
    pub unit: &'static str,
}

/// Column order of `kpi.csv`, shared with `mec_cast_ran.KPI_CSV_HEADER`.
pub const KPI_CSV_HEADER: &str = "source,gnb_ts_ns,recv_ns,cell,ue,metric,value,unit";

impl RanSample {
    /// One `kpi.csv` line (no newline). Cells and UEs are numeric strings and
    /// metric names are dotted identifiers, so nothing needs quoting.
    pub fn csv_line(&self, recv_ns: i64) -> String {
        let value = if self.value.fract() == 0.0 && self.value.abs() < 1e15 {
            format!("{}", self.value as i64)
        } else {
            format!("{}", self.value)
        };
        format!(
            "json,{},{},{},{},{},{},{}",
            self.gnb_ts_ns, recv_ns, self.cell, self.ue, self.metric, value, self.unit
        )
    }

    /// The compact form logged as `context.norm`.
    pub fn to_json(&self) -> Value {
        json!({
            "cell": self.cell, "ue": self.ue, "metric": self.metric,
            "value": self.value, "unit": self.unit, "gnb_ts_ns": self.gnb_ts_ns,
        })
    }
}

/// srsRAN per-UE field -> (canonical metric, unit).
const JSON_UE: &[(&str, &str, &str)] = &[
    ("dl_brate", "ue.dl_throughput_bps", "bit/s"),
    ("ul_brate", "ue.ul_throughput_bps", "bit/s"),
    ("dl_mcs", "ue.dl_mcs", "index"),
    ("ul_mcs", "ue.ul_mcs", "index"),
    ("cqi", "ue.cqi", "index"),
    ("ri", "ue.dl_ri", "layers"),
    ("dl_ri", "ue.dl_ri", "layers"),
    ("ul_ri", "ue.ul_ri", "layers"),
    ("dl_nof_ok", "ue.dl_harq_ok", "count"),
    ("dl_nof_nok", "ue.dl_harq_nok", "count"),
    ("ul_nof_ok", "ue.ul_harq_ok", "count"),
    ("ul_nof_nok", "ue.ul_harq_nok", "count"),
    ("dl_bs", "ue.dl_buffer_bytes", "byte"),
    ("bsr", "ue.ul_bsr_bytes", "byte"),
    ("last_phr", "ue.phr_db", "dB"),
    ("pusch_snr_db", "ue.pusch_snr_db", "dB"),
    ("pucch_snr_db", "ue.pucch_snr_db", "dB"),
    ("pusch_rsrp_db", "ue.pusch_rsrp_db", "dB"),
    ("ta_ns", "ue.ta_ns", "ns"),
    ("pusch_ta_ns", "ue.pusch_ta_ns", "ns"),
    ("pucch_ta_ns", "ue.pucch_ta_ns", "ns"),
    ("srs_ta_ns", "ue.srs_ta_ns", "ns"),
    // Units undocumented by srsRAN: verify in the lab before giving one.
    ("sr_to_pusch_delay", "ue.sr_to_pusch_delay", ""),
    ("pusch_harq_delay", "ue.pusch_harq_delay", ""),
    ("pucch_harq_delay", "ue.pucch_harq_delay", ""),
    ("crc_delay", "ue.crc_delay", ""),
    ("ce_delay", "ue.ce_delay", ""),
    ("max_pusch_distance", "ue.max_pusch_distance", ""),
    ("max_pdsch_distance", "ue.max_pdsch_distance", ""),
];

/// srsRAN per-cell field -> (canonical metric, unit).
const JSON_CELL: &[(&str, &str, &str)] = &[
    ("dl_brate", "cell.dl_throughput_bps", "bit/s"),
    ("ul_brate", "cell.ul_throughput_bps", "bit/s"),
    ("error_indication_count", "cell.error_indications", "count"),
    (
        "nof_failed_pdcch_allocs",
        "cell.failed_pdcch_allocs",
        "count",
    ),
    ("nof_failed_uci_allocs", "cell.failed_uci_allocs", "count"),
    ("late_dl_harqs", "cell.late_dl_harqs", "count"),
    ("late_ul_harqs", "cell.late_ul_harqs", "count"),
    ("msg3_nof_ok", "cell.msg3_ok", "count"),
    ("msg3_nof_nok", "cell.msg3_nok", "count"),
    (
        "pusch_prbs_used_per_tdd_slot_idx",
        "cell.pusch_prbs_used",
        "prb",
    ),
    (
        "pdsch_prbs_used_per_tdd_slot_idx",
        "cell.pdsch_prbs_used",
        "prb",
    ),
    ("average_latency", "cell.avg_latency", ""),
    ("max_latency", "cell.max_latency", ""),
    ("avg_prach_delay", "cell.avg_prach_delay", ""),
    ("pucch_tot_rb_usage_avg", "cell.pucch_rb_usage_avg", ""),
];

const IDENTITY: &[&str] = &["rnti", "pci", "ue", "timestamp"];

/// Row sink: (cell, ue, metric, value, unit, gnb_ts_ns).
type Push<'a> = dyn FnMut(&str, &str, String, f64, &'static str, i64) + 'a;

fn number(v: &Value) -> Option<f64> {
    match v {
        Value::Number(n) => n.as_f64().filter(|f| f.is_finite()),
        _ => None,
    }
}

fn int_string(v: Option<&Value>) -> String {
    v.and_then(number)
        .map(|f| format!("{}", f as i64))
        .unwrap_or_default()
}

/// (metric, value, unit) for every measurement in one UE or cell object.
fn fields(
    obj: &serde_json::Map<String, Value>,
    table: &[(&str, &str, &'static str)],
    scope: &str,
) -> Vec<(String, f64, &'static str)> {
    let mut out = Vec::new();
    for (key, raw) in obj {
        if IDENTITY.contains(&key.as_str()) {
            continue;
        }
        let (name, unit) = table
            .iter()
            .find(|(field, _, _)| field == key)
            .map(|(_, name, unit)| (name.to_string(), *unit))
            .unwrap_or_else(|| (format!("{scope}.raw.{key}"), ""));
        if let Some(n) = number(raw) {
            out.push((name, n, unit));
        } else if let Value::Object(sub) = raw {
            for (k, v) in sub {
                if let Some(n) = number(v) {
                    out.push((format!("{name}_{k}"), n, unit));
                }
            }
        } else if let Value::Array(items) = raw {
            let nums: Vec<f64> = items.iter().filter_map(number).collect();
            if !items.is_empty() && nums.len() == items.len() {
                out.push((
                    format!("{name}_mean"),
                    nums.iter().sum::<f64>() / nums.len() as f64,
                    unit,
                ));
            }
        }
    }
    out
}

fn cell_timestamp(cell: &Value) -> Option<i64> {
    // A cell carries its own timestamp in the 25.x layout; reuse the report
    // parser by presenting it as a report.
    cell.get("timestamp")
        .and_then(|ts| report_timestamp_ns(&json!({ "timestamp": ts })))
}

/// One srsRAN JSON metrics report -> rows, sorted by (cell, ue, metric).
pub fn normalise_json(report: &Value) -> Vec<RanSample> {
    let Some(obj) = report.as_object() else {
        return Vec::new();
    };
    let ts = report_timestamp_ns(report).unwrap_or(0);
    let mut rows = Vec::new();

    let mut push = |cell: &str, ue: &str, metric: String, value: f64, unit, t: i64| {
        rows.push(RanSample {
            gnb_ts_ns: t,
            cell: cell.to_string(),
            ue: ue.to_string(),
            metric,
            value,
            unit,
        })
    };

    fn ue_rows(entries: Option<&Value>, cell_default: &str, t: i64, push: &mut Push<'_>) {
        for entry in entries.and_then(Value::as_array).into_iter().flatten() {
            let ue = entry.get("ue_container").unwrap_or(entry);
            let Some(ue_obj) = ue.as_object() else {
                continue;
            };
            let cell = match int_string(ue.get("pci")) {
                s if s.is_empty() => cell_default.to_string(),
                s => s,
            };
            let rnti = int_string(ue.get("rnti"));
            for (metric, value, unit) in fields(ue_obj, JSON_UE, "ue") {
                push(&cell, &rnti, metric, value, unit, t);
            }
        }
    }

    // 25.x layout
    for c in obj
        .get("cells")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        if !c.is_object() {
            continue;
        }
        let t = cell_timestamp(c).unwrap_or(ts);
        let empty = serde_json::Map::new();
        let cm = c
            .get("cell_metrics")
            .and_then(Value::as_object)
            .unwrap_or(&empty);
        let cell = int_string(cm.get("pci"));
        for (metric, value, unit) in fields(cm, JSON_CELL, "cell") {
            push(&cell, "", metric, value, unit, t);
        }
        for ev in c
            .get("event_list")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
        {
            if let Some(kind) = ev.get("event_type").and_then(Value::as_str) {
                push(
                    &cell,
                    &int_string(ev.get("rnti")),
                    format!("event.{kind}"),
                    1.0,
                    "count",
                    t,
                );
            }
        }
        ue_rows(c.get("ue_list"), &cell, t, &mut push);
    }

    // <= 24.x layout
    let legacy_cell = obj.get("cell_metrics").and_then(Value::as_object);
    if let Some(cm) = legacy_cell {
        let cell = int_string(cm.get("pci"));
        for (metric, value, unit) in fields(cm, JSON_CELL, "cell") {
            push(&cell, "", metric, value, unit, ts);
        }
    }
    if obj.contains_key("ue_list") {
        let default = legacy_cell
            .map(|cm| int_string(cm.get("pci")))
            .unwrap_or_default();
        ue_rows(obj.get("ue_list"), &default, ts, &mut push);
    }

    rows.sort_by(|a, b| (&a.cell, &a.ue, &a.metric).cmp(&(&b.cell, &b.ue, &b.metric)));
    rows
}

/// Total UE throughput in one report, `(dl, ul)` bit/s — what the admin
/// compares against the xApp's KPM figure (`WF_RAN_SOURCES_DISAGREE`).
pub fn ue_throughput(rows: &[RanSample]) -> Option<(f64, f64)> {
    let sum = |m: &str| {
        rows.iter()
            .filter(|r| r.metric == m)
            .map(|r| r.value)
            .sum::<f64>()
    };
    rows.iter()
        .any(|r| r.metric == "ue.dl_throughput_bps" || r.metric == "ue.ul_throughput_bps")
        .then(|| (sum("ue.dl_throughput_bps"), sum("ue.ul_throughput_bps")))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn csv_lines_print_integers_as_integers() {
        let r = RanSample {
            gnb_ts_ns: 5,
            cell: "1".into(),
            ue: "17921".into(),
            metric: "ue.dl_mcs".into(),
            value: 27.0,
            unit: "index",
        };
        assert_eq!(r.csv_line(9), "json,5,9,1,17921,ue.dl_mcs,27,index");
        let r = RanSample { value: 31.2, ..r };
        assert_eq!(r.csv_line(9), "json,5,9,1,17921,ue.dl_mcs,31.2,index");
    }
}
