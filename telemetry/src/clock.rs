//! Clock abstraction: one trait, explicit implementations, no globals.
//!
//! All clocks return **nanoseconds as `i64`**. `RealtimeClock` (and
//! `PhcClock`) return nanoseconds since the Unix epoch and are comparable
//! across PTP-synchronized machines; `MonotonicClock` is local-only.
//! `MockClock` makes every time-dependent unit test deterministic.

use std::sync::atomic::{AtomicI64, Ordering};
use std::sync::Arc;

#[cfg(all(target_os = "linux", feature = "linux-ptp"))]
mod phc;
#[cfg(all(target_os = "linux", feature = "linux-ptp"))]
pub use phc::PhcClock;

/// Identifies which time base a timestamp came from. Recorded alongside
/// snapshots so cross-machine comparisons can be validated after the fact.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ClockId {
    Monotonic,
    Realtime,
    Phc,
    Mock,
}

impl ClockId {
    pub fn as_str(&self) -> &'static str {
        match self {
            ClockId::Monotonic => "monotonic",
            ClockId::Realtime => "realtime",
            ClockId::Phc => "phc",
            ClockId::Mock => "mock",
        }
    }
}

/// A nanosecond clock.
pub trait Clock: Send + Sync {
    fn now_ns(&self) -> i64;
    fn id(&self) -> ClockId;
}

#[cfg(unix)]
// tv_sec/tv_nsec are i64 on 64-bit Linux (making the conversion "useless"
// there) but i32 on some 32-bit targets, where it is required.
#[allow(clippy::useless_conversion)]
fn clock_gettime_ns(clk: libc::clockid_t) -> i64 {
    let mut ts = libc::timespec {
        tv_sec: 0,
        tv_nsec: 0,
    };
    // SAFETY: ts is a valid, writable timespec; clk is a valid clock id for
    // the calling platform.
    let rc = unsafe { libc::clock_gettime(clk, &mut ts) };
    debug_assert_eq!(rc, 0, "clock_gettime failed");
    i64::from(ts.tv_sec) * 1_000_000_000 + i64::from(ts.tv_nsec)
}

/// `CLOCK_MONOTONIC`: jitter-free local intervals, not comparable across
/// machines.
#[cfg(unix)]
#[derive(Clone, Copy, Debug, Default)]
pub struct MonotonicClock;

#[cfg(unix)]
impl Clock for MonotonicClock {
    fn now_ns(&self) -> i64 {
        clock_gettime_ns(libc::CLOCK_MONOTONIC)
    }
    fn id(&self) -> ClockId {
        ClockId::Monotonic
    }
}

/// `CLOCK_REALTIME`: wall-clock ns since the Unix epoch. Comparable across
/// machines only when both are disciplined from the SAME PTP grandmaster
/// (by `phc2sys`, or by chrony from a PHC refclock -- the daemon does not
/// matter, the shared root does) or, degraded, NTP. Two hosts each locked
/// perfectly to a different root compare as confidently as they do wrongly.
#[cfg(unix)]
#[derive(Clone, Copy, Debug, Default)]
pub struct RealtimeClock;

#[cfg(unix)]
impl Clock for RealtimeClock {
    fn now_ns(&self) -> i64 {
        clock_gettime_ns(libc::CLOCK_REALTIME)
    }
    fn id(&self) -> ClockId {
        ClockId::Realtime
    }
}

/// Deterministic clock for tests. Cloning shares the underlying time, so a
/// test can hold one handle and hand clones to the code under test.
#[derive(Clone, Debug, Default)]
pub struct MockClock {
    t: Arc<AtomicI64>,
}

impl MockClock {
    pub fn new(start_ns: i64) -> Self {
        Self {
            t: Arc::new(AtomicI64::new(start_ns)),
        }
    }

    pub fn set(&self, ns: i64) {
        self.t.store(ns, Ordering::SeqCst);
    }

    pub fn advance(&self, delta_ns: i64) {
        self.t.fetch_add(delta_ns, Ordering::SeqCst);
    }
}

impl Clock for MockClock {
    fn now_ns(&self) -> i64 {
        self.t.load(Ordering::SeqCst)
    }
    fn id(&self) -> ClockId {
        ClockId::Mock
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn mock_clock_is_deterministic_and_shared() {
        let clock = MockClock::new(100);
        let handle = clock.clone();
        assert_eq!(clock.now_ns(), 100);
        handle.advance(50);
        assert_eq!(clock.now_ns(), 150);
        clock.set(-7);
        assert_eq!(handle.now_ns(), -7);
        assert_eq!(clock.id(), ClockId::Mock);
    }

    #[cfg(unix)]
    #[test]
    fn real_clocks_advance() {
        let mono = MonotonicClock;
        let a = mono.now_ns();
        let b = mono.now_ns();
        assert!(b >= a, "monotonic clock went backwards");
        assert!(RealtimeClock.now_ns() > 1_500_000_000_000_000_000); // after 2017
    }
}
