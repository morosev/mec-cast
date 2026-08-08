//! Property tests: DelayStats must agree with naive reference
//! implementations on arbitrary inputs.

use mec_cast_telemetry::{DelayStats, Modality, TimingEnvelope};
use proptest::prelude::*;

/// Naive nearest-rank percentile over the last `window` values.
fn naive_percentile(values: &[i64], window: usize, p: f64) -> i64 {
    let start = values.len().saturating_sub(window);
    let mut tail: Vec<i64> = values[start..].to_vec();
    tail.sort_unstable();
    let n = tail.len();
    let rank = (p * n as f64).ceil() as usize;
    tail[rank.clamp(1, n) - 1]
}

proptest! {
    #[test]
    fn percentiles_match_naive_sort(
        values in prop::collection::vec(-1_000_000_000i64..1_000_000_000, 1..500),
        window in 1usize..64,
    ) {
        let mut stats = DelayStats::with_window(window);
        for &v in &values {
            stats.record(v);
        }
        let snap = stats.snapshot().unwrap();
        prop_assert_eq!(snap.p50_ns, naive_percentile(&values, window, 0.50));
        prop_assert_eq!(snap.p90_ns, naive_percentile(&values, window, 0.90));
        prop_assert_eq!(snap.p99_ns, naive_percentile(&values, window, 0.99));
    }

    #[test]
    fn welford_matches_two_pass(
        values in prop::collection::vec(-1_000_000i64..1_000_000, 2..500),
    ) {
        let mut stats = DelayStats::new();
        for &v in &values {
            stats.record(v);
        }
        let snap = stats.snapshot().unwrap();

        let n = values.len() as f64;
        let mean = values.iter().map(|&v| v as f64).sum::<f64>() / n;
        let var = values.iter().map(|&v| (v as f64 - mean).powi(2)).sum::<f64>() / (n - 1.0);

        prop_assert!((snap.mean_ns - mean).abs() <= mean.abs().max(1.0) * 1e-9);
        prop_assert!((snap.stddev_ns - var.sqrt()).abs() <= var.sqrt().max(1.0) * 1e-9);
        prop_assert_eq!(snap.min_ns, *values.iter().min().unwrap());
        prop_assert_eq!(snap.max_ns, *values.iter().max().unwrap());
    }

    #[test]
    fn envelope_round_trips(
        capture in any::<i64>(),
        send in any::<i64>(),
        recv in any::<i64>(),
        done in any::<i64>(),
        seq in any::<u64>(),
        modality_raw in 0u8..4,
        trace in any::<[u8; 16]>(),
    ) {
        let e = TimingEnvelope {
            capture_ns: capture,
            send_ns: send,
            recv_ns: recv,
            process_done_ns: done,
            seq,
            modality: Modality::try_from(modality_raw).unwrap(),
            trace_id: trace,
        };
        prop_assert_eq!(TimingEnvelope::from_bytes(&e.to_bytes()).unwrap(), e);
    }

    #[test]
    fn envelope_never_panics_on_garbage(bytes in prop::collection::vec(any::<u8>(), 0..200)) {
        let _ = TimingEnvelope::from_bytes(&bytes);
    }
}
