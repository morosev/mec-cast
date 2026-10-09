//! PTP synchronization quality: is the wall clock trustworthy right now?
//!
//! Every snapshot the recorder emits carries a `PtpQuality`, so analysis can
//! filter measurement windows by clock health after the fact instead of
//! trusting a boolean set once at startup.
//!
//! The offset measured here is PHC-vs-`CLOCK_REALTIME` via two adjacent
//! reads, which includes a few hundred ns of syscall latency; that is
//! adequate for gating against a microsecond-scale threshold. (A future
//! refinement is the `PTP_SYS_OFFSET` ioctl, which brackets the reads in the
//! kernel.) Without the `linux-ptp` feature or without PTP hardware, a
//! monitor is `disabled()` and reports `reliable: false` honestly — exactly
//! what same-host container runs should record.
//!
//! # What `reliable` does not mean
//!
//! It is **local**: this host's `CLOCK_REALTIME` against this host's PHC. It
//! cannot see whether that PHC agrees with the PHC at the other end of the
//! measurement, and so it cannot detect the one failure that invalidates
//! every cross-host figure — two endpoints each disciplined perfectly to a
//! *different* root. A `ptp4l` host locked to a grandmaster and a VM taking
//! `ptp_kvm` time from a hypervisor on NTP will both report `reliable: true`
//! with tens of ns of local offset while being seconds apart.
//!
//! Negative derived delays are the symptom that does catch it (the recorder
//! counts them, and the admin raises `WF_CLOCK_SKEW`), but only once a run is
//! already contaminated. Beforehand, the check is
//! `deploy/lab/ptp/verify-ptp.sh --peer <host>`, which compares the two ends
//! rather than each end against itself.

#[cfg(all(target_os = "linux", feature = "linux-ptp"))]
use crate::clock::PhcClock;
use crate::clock::{Clock, RealtimeClock};

/// Default maximum |PHC − system| offset for measurements to count as
/// PTP-reliable: 1 µs.
pub const DEFAULT_THRESHOLD_NS: i64 = 1_000;

/// Point-in-time clock-sync quality.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct PtpQuality {
    /// PHC minus system clock, nanoseconds. 0 when disabled.
    pub offset_ns: i64,
    /// True only when a PHC is present and |offset| < threshold.
    pub reliable: bool,
    /// `CLOCK_REALTIME` when this quality was sampled.
    pub sampled_at_ns: i64,
}

enum Source {
    Disabled,
    #[cfg(all(target_os = "linux", feature = "linux-ptp"))]
    Phc(PhcClock),
}

/// Polls PTP sync quality on demand. Cheap enough to call at snapshot
/// cadence; owns no threads.
pub struct PtpMonitor {
    source: Source,
    threshold_ns: i64,
}

impl PtpMonitor {
    /// Monitor that always reports `reliable: false` — for hosts without PTP
    /// hardware and for same-host testing.
    pub fn disabled() -> Self {
        Self {
            source: Source::Disabled,
            threshold_ns: DEFAULT_THRESHOLD_NS,
        }
    }

    /// Monitor backed by a PTP Hardware Clock.
    #[cfg(all(target_os = "linux", feature = "linux-ptp"))]
    pub fn with_phc(phc: PhcClock, threshold_ns: i64) -> Self {
        Self {
            source: Source::Phc(phc),
            threshold_ns: threshold_ns.max(1),
        }
    }

    pub fn threshold_ns(&self) -> i64 {
        self.threshold_ns
    }

    /// Sample sync quality now.
    pub fn poll(&self) -> PtpQuality {
        let now = RealtimeClock.now_ns();
        match &self.source {
            Source::Disabled => PtpQuality {
                offset_ns: 0,
                reliable: false,
                sampled_at_ns: now,
            },
            #[cfg(all(target_os = "linux", feature = "linux-ptp"))]
            Source::Phc(phc) => {
                let offset = phc.now_ns() - RealtimeClock.now_ns();
                PtpQuality {
                    offset_ns: offset,
                    reliable: offset.abs() < self.threshold_ns,
                    sampled_at_ns: now,
                }
            }
        }
    }
}

/// Build the clock-quality monitor for a configured PHC device.
///
/// Returns `(monitor, enabled, error)`. A device that is set but cannot be
/// opened degrades to `disabled()` rather than failing the caller:
/// `ptp.reliable` annotates the measurement, and a health flag must never take
/// down the thing it annotates. The degrade is reported through `enabled` and
/// `error` so it cannot pass unnoticed — silence is how this stayed broken,
/// with every node reporting `reliable: false` and nothing saying the monitor
/// had never been built at all.
pub fn monitor_from_device(device: Option<&str>) -> (PtpMonitor, bool, String) {
    match device {
        Some(dev) if !dev.is_empty() => open_phc(dev),
        _ => (
            PtpMonitor::disabled(),
            false,
            "no device configured".to_string(),
        ),
    }
}

#[cfg(all(target_os = "linux", feature = "linux-ptp"))]
fn open_phc(device: &str) -> (PtpMonitor, bool, String) {
    match PhcClock::open(device) {
        Ok(clock) => (
            PtpMonitor::with_phc(clock, DEFAULT_THRESHOLD_NS),
            true,
            String::new(),
        ),
        // The OS error, verbatim. A bare "could not be opened" covers a
        // missing device, a permission failure, a non-PHC device and a build
        // with no PHC support at all -- four causes with four different
        // fixes, and distinguishing them cost five rounds of remote probing
        // once. ENOENT, EACCES and EINVAL each name themselves here.
        Err(e) => (PtpMonitor::disabled(), false, format!("{device}: {e}")),
    }
}

/// Without the `linux-ptp` feature there is no PHC to open. Configuring a
/// device on such a build is a no-op, and this says so rather than blaming
/// the device -- a distinction no amount of checking the host can reveal.
#[cfg(not(all(target_os = "linux", feature = "linux-ptp")))]
fn open_phc(_device: &str) -> (PtpMonitor, bool, String) {
    (
        PtpMonitor::disabled(),
        false,
        "this build has no PHC support (linux-ptp feature off)".to_string(),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn disabled_monitor_is_honest() {
        let m = PtpMonitor::disabled();
        let q = m.poll();
        assert!(!q.reliable);
        assert_eq!(q.offset_ns, 0);
        assert!(q.sampled_at_ns > 0);
    }

    #[test]
    fn no_device_is_disabled_and_says_why() {
        for device in [None, Some("")] {
            let (m, enabled, why) = monitor_from_device(device);
            assert!(!enabled);
            assert!(!m.poll().reliable);
            assert_eq!(why, "no device configured");
        }
    }

    #[test]
    fn an_unopenable_device_degrades_and_names_itself() {
        let (m, enabled, why) = monitor_from_device(Some("/dev/does-not-exist-ptp9"));
        assert!(!enabled);
        assert!(!m.poll().reliable);
        assert!(!why.is_empty());
    }
}
