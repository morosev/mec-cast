//! PTP Hardware Clock (`/dev/ptpN`) as a `Clock`.
//!
//! Compiled only on Linux with the `linux-ptp` feature. Opening the device
//! requires read permission on `/dev/ptpN` (typically root or a udev rule).

use std::fs::{File, OpenOptions};
use std::io;
use std::os::fd::AsRawFd;

use super::{clock_gettime_ns, Clock, ClockId};

/// Kernel convention for turning a PHC file descriptor into a dynamic
/// `clockid_t` (see `clock_gettime(2)`, "Dynamic clocks").
fn fd_to_clockid(fd: i32) -> libc::clockid_t {
    !((fd as libc::clockid_t) << 3)
}

/// Direct reader of a PTP Hardware Clock device.
#[derive(Debug)]
pub struct PhcClock {
    // Held only to keep the fd (and thus the clockid) alive.
    _file: File,
    clockid: libc::clockid_t,
    device: String,
}

impl PhcClock {
    /// Open a PHC device (e.g. `/dev/ptp0`) and verify it is readable as a
    /// clock.
    pub fn open(device: &str) -> io::Result<Self> {
        let file = OpenOptions::new().read(true).write(true).open(device)?;
        let clockid = fd_to_clockid(file.as_raw_fd());
        // Probe once so a bad device fails at open, not on the hot path.
        let mut ts = libc::timespec {
            tv_sec: 0,
            tv_nsec: 0,
        };
        // SAFETY: valid writable timespec; clockid derived from an open fd.
        let rc = unsafe { libc::clock_gettime(clockid, &mut ts) };
        if rc != 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(Self {
            _file: file,
            clockid,
            device: device.to_string(),
        })
    }

    pub fn device(&self) -> &str {
        &self.device
    }
}

impl Clock for PhcClock {
    fn now_ns(&self) -> i64 {
        clock_gettime_ns(self.clockid)
    }
    fn id(&self) -> ClockId {
        ClockId::Phc
    }
}
