//! Delay statistics: running aggregates plus exact windowed percentiles.
//!
//! This is the Rust port of the C++ `DelayStats` from
//! `client/src/delay_measurement.cc`, with its known defects fixed:
//!
//! - percentiles are **exact** over a sliding window (the C++ version used an
//!   asymmetric EWMA that was not a percentile of anything),
//! - `max` starts at `i64::MIN` so all-negative series report a real maximum
//!   (the C++ version initialized to 0),
//! - the variance accumulated by Welford's algorithm is **emitted** as
//!   `stddev_ns` (jitter) instead of being computed and discarded,
//! - there are no global singletons; callers own their instances.
//!
//! `record()` is O(1) and allocation-free, safe for the writer thread's per
//! sample path. `snapshot()` copies and sorts the window (≤ `window` × 8
//! bytes) and is intended to be called at snapshot cadence (1–2 s), never on
//! the hot path.
//!
//! Exact-window percentiles were chosen over streaming estimators (P²,
//! t-digest): 5G latency under HARQ retransmission and scheduling is
//! multimodal, where P²'s error is unbounded, and at our sample rates the
//! sort cost is microseconds.

/// Default sliding-window length for percentile computation.
pub const DEFAULT_WINDOW: usize = 8192;

/// Running statistics for one delay metric. Values are nanoseconds and may
/// legitimately be negative (unsynchronized clocks).
#[derive(Clone, Debug)]
pub struct DelayStats {
    count: u64,
    last_ns: i64,
    min_ns: i64,
    max_ns: i64,
    mean: f64,
    m2: f64,
    ring: Box<[i64]>,
    ring_pos: usize,
    ring_filled: usize,
}

/// Point-in-time summary of a `DelayStats`.
///
/// `count`, `min`, `max`, `mean`, `stddev` cover **all** samples since the
/// last reset; the percentiles cover the last `window` samples.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct StatsSnapshot {
    pub count: u64,
    pub last_ns: i64,
    pub min_ns: i64,
    pub max_ns: i64,
    pub mean_ns: f64,
    /// Sample standard deviation (jitter). 0.0 when count < 2.
    pub stddev_ns: f64,
    pub p50_ns: i64,
    pub p90_ns: i64,
    pub p99_ns: i64,
}

impl Default for DelayStats {
    fn default() -> Self {
        Self::with_window(DEFAULT_WINDOW)
    }
}

impl DelayStats {
    pub fn new() -> Self {
        Self::default()
    }

    /// `window` is the number of most-recent samples percentiles are computed
    /// over. Clamped to at least 1.
    pub fn with_window(window: usize) -> Self {
        let window = window.max(1);
        Self {
            count: 0,
            last_ns: 0,
            min_ns: i64::MAX,
            max_ns: i64::MIN,
            mean: 0.0,
            m2: 0.0,
            ring: vec![0i64; window].into_boxed_slice(),
            ring_pos: 0,
            ring_filled: 0,
        }
    }

    /// Record one value. O(1), never allocates.
    pub fn record(&mut self, value_ns: i64) {
        self.count += 1;
        self.last_ns = value_ns;
        self.min_ns = self.min_ns.min(value_ns);
        self.max_ns = self.max_ns.max(value_ns);

        // Welford's online mean/variance.
        let v = value_ns as f64;
        let delta = v - self.mean;
        self.mean += delta / self.count as f64;
        self.m2 += delta * (v - self.mean);

        self.ring[self.ring_pos] = value_ns;
        self.ring_pos = (self.ring_pos + 1) % self.ring.len();
        self.ring_filled = self.ring_filled.saturating_add(1).min(self.ring.len());
    }

    pub fn count(&self) -> u64 {
        self.count
    }

    /// Summarize. Returns `None` when nothing has been recorded — callers
    /// can never mistake "no data" for a measured zero.
    pub fn snapshot(&self) -> Option<StatsSnapshot> {
        if self.count == 0 {
            return None;
        }
        let mut window: Vec<i64> = self.ring[..self.ring_filled].to_vec();
        window.sort_unstable();
        let stddev = if self.count > 1 {
            (self.m2 / (self.count - 1) as f64).sqrt()
        } else {
            0.0
        };
        Some(StatsSnapshot {
            count: self.count,
            last_ns: self.last_ns,
            min_ns: self.min_ns,
            max_ns: self.max_ns,
            mean_ns: self.mean,
            stddev_ns: stddev,
            p50_ns: percentile_sorted(&window, 0.50),
            p90_ns: percentile_sorted(&window, 0.90),
            p99_ns: percentile_sorted(&window, 0.99),
        })
    }

    pub fn reset(&mut self) {
        let window = self.ring.len();
        *self = Self::with_window(window);
    }
}

/// Nearest-rank percentile of an already-sorted, non-empty slice.
fn percentile_sorted(sorted: &[i64], p: f64) -> i64 {
    debug_assert!(!sorted.is_empty());
    debug_assert!((0.0..=1.0).contains(&p));
    let n = sorted.len();
    let rank = (p * n as f64).ceil() as usize;
    sorted[rank.clamp(1, n) - 1]
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn empty_returns_none() {
        assert!(DelayStats::new().snapshot().is_none());
    }

    #[test]
    fn single_value() {
        let mut s = DelayStats::new();
        s.record(1000);
        let snap = s.snapshot().unwrap();
        assert_eq!(snap.count, 1);
        assert_eq!(snap.last_ns, 1000);
        assert_eq!(snap.min_ns, 1000);
        assert_eq!(snap.max_ns, 1000);
        assert_eq!(snap.mean_ns, 1000.0);
        assert_eq!(snap.stddev_ns, 0.0);
        assert_eq!(snap.p50_ns, 1000);
        assert_eq!(snap.p99_ns, 1000);
    }

    #[test]
    fn all_negative_series_reports_true_max() {
        // The C++ version initialized max to 0 and would report max=0 for a
        // series that never contained 0. This is the regression test for it.
        let mut s = DelayStats::new();
        for v in [-50, -20, -90] {
            s.record(v);
        }
        let snap = s.snapshot().unwrap();
        assert_eq!(snap.max_ns, -20);
        assert_eq!(snap.min_ns, -90);
    }

    #[test]
    fn constant_series() {
        let mut s = DelayStats::new();
        for _ in 0..100 {
            s.record(7);
        }
        let snap = s.snapshot().unwrap();
        assert_eq!(snap.mean_ns, 7.0);
        assert_eq!(snap.stddev_ns, 0.0);
        assert_eq!(snap.p50_ns, 7);
        assert_eq!(snap.p90_ns, 7);
        assert_eq!(snap.p99_ns, 7);
    }

    #[test]
    fn known_percentiles_1_to_100() {
        let mut s = DelayStats::new();
        for v in 1..=100 {
            s.record(v);
        }
        let snap = s.snapshot().unwrap();
        // nearest-rank on 1..=100: p50 -> 50th value, p90 -> 90th, p99 -> 99th
        assert_eq!(snap.p50_ns, 50);
        assert_eq!(snap.p90_ns, 90);
        assert_eq!(snap.p99_ns, 99);
        assert_eq!(snap.min_ns, 1);
        assert_eq!(snap.max_ns, 100);
        assert_eq!(snap.mean_ns, 50.5);
    }

    #[test]
    fn percentiles_use_only_the_window() {
        let mut s = DelayStats::with_window(4);
        // Old values (1_000_000) must age out of the percentile window,
        // but min/max/count are all-time.
        for _ in 0..10 {
            s.record(1_000_000);
        }
        for v in [1, 2, 3, 4] {
            s.record(v);
        }
        let snap = s.snapshot().unwrap();
        assert_eq!(snap.count, 14);
        assert_eq!(snap.max_ns, 1_000_000);
        assert_eq!(snap.p50_ns, 2);
        assert_eq!(snap.p99_ns, 4);
    }

    #[test]
    fn stddev_matches_two_pass() {
        let values = [3i64, 7, 7, 19, 24, 1, 8, 42, 5, 5];
        let mut s = DelayStats::new();
        for &v in &values {
            s.record(v);
        }
        let mean = values.iter().sum::<i64>() as f64 / values.len() as f64;
        let var = values
            .iter()
            .map(|&v| (v as f64 - mean).powi(2))
            .sum::<f64>()
            / (values.len() - 1) as f64;
        let snap = s.snapshot().unwrap();
        assert!((snap.stddev_ns - var.sqrt()).abs() < 1e-9);
        assert!((snap.mean_ns - mean).abs() < 1e-9);
    }

    #[test]
    fn reset_clears_everything() {
        let mut s = DelayStats::with_window(16);
        s.record(5);
        s.reset();
        assert!(s.snapshot().is_none());
        s.record(-3);
        let snap = s.snapshot().unwrap();
        assert_eq!(snap.count, 1);
        assert_eq!(snap.max_ns, -3);
    }
}
