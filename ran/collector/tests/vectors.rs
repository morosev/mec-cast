//! `ran/schema/vectors.json` is the contract between this normaliser and the
//! Python one in `ran/py`, which asserts against the same file.

use std::collections::BTreeMap;

use ran_collector::normalise::normalise_json;
use serde_json::Value;

fn vectors() -> Value {
    let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../schema/vectors.json");
    serde_json::from_str(&std::fs::read_to_string(path).expect("vectors.json")).unwrap()
}

type Key = (String, String, String, i64);

fn keyed(rows: impl Iterator<Item = (Key, (f64, String))>) -> BTreeMap<Key, (f64, String)> {
    let mut out = BTreeMap::new();
    for (k, v) in rows {
        assert!(out.insert(k.clone(), v).is_none(), "duplicate row {k:?}");
    }
    out
}

#[test]
fn every_json_vector_matches() {
    let v = vectors();
    let cases = v["json"].as_array().expect("json cases");
    assert!(!cases.is_empty());
    for case in cases {
        let name = case["name"].as_str().unwrap();
        let got = keyed(normalise_json(&case["input"]).into_iter().map(|r| {
            (
                (r.cell, r.ue, r.metric, r.gnb_ts_ns),
                (r.value, r.unit.to_string()),
            )
        }));
        let want = keyed(case["expected"].as_array().unwrap().iter().map(|r| {
            (
                (
                    r["cell"].as_str().unwrap().to_string(),
                    r["ue"].as_str().unwrap().to_string(),
                    r["metric"].as_str().unwrap().to_string(),
                    r["gnb_ts_ns"].as_i64().unwrap(),
                ),
                (
                    r["value"].as_f64().unwrap(),
                    r["unit"].as_str().unwrap().to_string(),
                ),
            )
        }));
        let got_keys: Vec<_> = got.keys().collect();
        let want_keys: Vec<_> = want.keys().collect();
        assert_eq!(got_keys, want_keys, "{name}: rows differ");
        for (k, (wv, wu)) in &want {
            let (gv, gu) = &got[k];
            assert!(
                (gv - wv).abs() <= 1e-9 * wv.abs().max(1.0),
                "{name} {k:?}: {gv} != {wv}"
            );
            assert_eq!(gu, wu, "{name} {k:?}: unit");
        }
    }
}
