//! Every fixture under `testdata/` is held to what the collector relies on.
//!
//! Dropping a lab capture into `testdata/` is all it takes to have it
//! checked: no test has to name it. A capture is `*.lab.jsonl`, and it must
//! carry a provenance sidecar (`*.lab.json`) naming the srsRAN version that
//! produced it — the schema varies between releases, so a fixture that does
//! not say which release it came from cannot answer the only question it
//! exists for. `scripts/ran-fixture.sh` writes both.

use std::path::{Path, PathBuf};

use ran_collector::{kpi_entry, report_timestamp_ns};
use serde_json::Value;

fn testdata() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("testdata")
}

fn fixtures() -> Vec<PathBuf> {
    let mut found: Vec<PathBuf> = std::fs::read_dir(testdata())
        .expect("testdata/ missing")
        .filter_map(|e| e.ok().map(|e| e.path()))
        .filter(|p| p.extension().is_some_and(|x| x == "jsonl"))
        .collect();
    found.sort();
    found
}

/// Per-UE entries in either srsRAN layout: `ue_list[].ue_container` (<= 24.x)
/// or `cells[].ue_list[]` (25.x).
fn ues(report: &Value) -> Vec<&Value> {
    let mut out = Vec::new();
    if let Some(list) = report.get("ue_list").and_then(Value::as_array) {
        out.extend(list.iter().map(|u| u.get("ue_container").unwrap_or(u)));
    }
    for cell in report
        .get("cells")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        if let Some(list) = cell.get("ue_list").and_then(Value::as_array) {
            out.extend(list.iter());
        }
    }
    out
}

#[test]
fn there_is_at_least_one_fixture_per_layout() {
    let names: Vec<String> = fixtures()
        .iter()
        .map(|p| p.file_name().unwrap().to_string_lossy().into_owned())
        .collect();
    assert!(names.len() >= 2, "expected both layouts, found {names:?}");
}

#[test]
fn every_fixture_is_reports_the_collector_can_read() {
    for path in fixtures() {
        let name = path.file_name().unwrap().to_string_lossy().into_owned();
        let text = std::fs::read_to_string(&path).unwrap();
        let lines: Vec<&str> = text.lines().filter(|l| !l.trim().is_empty()).collect();
        assert!(!lines.is_empty(), "{name}: empty");

        let mut stamped = 0;
        let mut with_ue = 0;
        for (i, line) in lines.iter().enumerate() {
            let entry = kpi_entry(line.as_bytes(), "fixture", 1)
                .unwrap_or_else(|| panic!("{name}:{}: not a JSON object", i + 1));
            let report = &entry["context"]["kpi"];
            if report_timestamp_ns(report).is_some() {
                stamped += 1;
            }
            if ues(report).iter().any(|u| u.get("rnti").is_some()) {
                with_ue += 1;
            }
        }
        // A capture can include layers other than the scheduler (MAC, RLC,
        // app usage...), each its own report, so not every line has UEs. But
        // a fixture with none at all is not a scheduler capture.
        assert!(with_ue > 0, "{name}: no report carries a UE with an rnti");
        assert_eq!(
            stamped,
            lines.len(),
            "{name}: {} report(s) without a timestamp the collector can parse",
            lines.len() - stamped
        );
    }
}

#[test]
fn every_lab_capture_says_where_it_came_from() {
    for path in fixtures() {
        let name = path.file_name().unwrap().to_string_lossy().into_owned();
        if !name.ends_with(".lab.jsonl") {
            continue;
        }
        let sidecar = path.with_extension("json");
        let meta: Value = serde_json::from_str(
            &std::fs::read_to_string(&sidecar)
                .unwrap_or_else(|e| panic!("{name}: no provenance sidecar {sidecar:?}: {e}")),
        )
        .unwrap_or_else(|e| panic!("{name}: sidecar is not JSON: {e}"));
        for key in ["srsran_version", "captured_utc", "transport", "run_id"] {
            assert!(
                meta.get(key)
                    .and_then(Value::as_str)
                    .is_some_and(|v| !v.is_empty()),
                "{name}: sidecar lacks {key}"
            );
        }
    }
}
