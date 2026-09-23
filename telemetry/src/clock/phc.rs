//! PTP Hardware Clock (`/dev/ptpN`) as a `Clock`.
//!
//! Compiled only on Linux with the `linux-ptp` feature. The device is opened
//! READ-ONLY: this type only ever reads the clock, and the kernel's
//! `posix_clock` layer checks `FMODE_WRITE` in `pc_clock_settime` and
//! `pc_clock_adjtime` but not in `pc_clock_gettime`, so a descriptor without
//! write access is sufficient. That keeps `/dev/ptpN` usable from a container
//! running as a non-root user, or through a udev rule granting a group read
//! access -- deployments the read-write open rejected for no reason it could
//! act on.

use std::fs::{File, OpenOptions};
use std::io;
use std::os::fd::AsRawFd;

use super::{clock_gettime_ns, Clock, ClockId};

/// Kernel tag for a dynamic (fd-backed) clock id, and the mask the kernel
/// tests it against. From `include/uapi/linux/posix-types.h`:
///
/// ```text
/// #define CLOCKFD             3
/// #define CLOCKFD_MASK        (CLOCK_TAI + 1)   /* == 7 */
/// #define FD_TO_CLOCKID(fd)   ((~(clockid_t) (fd) << 3) | CLOCKFD)
/// #define CLOCKID_TO_FD(clk)  ((unsigned int) ~((clk) >> 3))
/// ```
const CLOCKFD: libc::clockid_t = 3;
#[cfg(test)]
const CLOCKFD_MASK: libc::clockid_t = 7;

/// Kernel convention for turning a PHC file descriptor into a dynamic
/// `clockid_t` (see `clock_gettime(2)`, "Dynamic clocks").
///
/// The `| CLOCKFD` is load-bearing and was missing. `clockid_to_kclock()`
/// dispatches a negative id on `(id & CLOCKFD_MASK) == CLOCKFD`; complementing
/// `fd << 3` leaves 7 in those bits rather than 3, so every id was routed to
/// the POSIX CPU-clock handler instead of the dynamic one and rejected with
/// EINVAL. The device opened fine and the very next syscall failed, on every
/// host, for the life of this module -- which read as "no PHC here" because
/// `open()` returns `Err` either way.
fn fd_to_clockid(fd: i32) -> libc::clockid_t {
    ((!(fd as libc::clockid_t)) << 3) | CLOCKFD
}

/// Inverse of [`fd_to_clockid`], for the round-trip test.
#[cfg(test)]
mod clockid_tests {
    use super::*;

    // The kernel accepts a dynamic clock id only when its low three bits are
    // CLOCKFD. This needs no PTP hardware, which is the point: the bug it
    // catches made every real device fail while no test could see it.
    #[test]
    fn a_clock_id_carries_the_dynamic_clock_tag() {
        for fd in [0, 3, 5, 9, 17, 255, 1023] {
            let id = fd_to_clockid(fd);
            assert_eq!(
                id & CLOCKFD_MASK,
                CLOCKFD,
                "fd {fd} produced clockid {id}, whose low bits are {} not {CLOCKFD}; \
                 the kernel routes that to the CPU-clock handler and returns EINVAL",
                id & CLOCKFD_MASK
            );
        }
    }

    /// A descriptor without write access must still reach `clock_gettime`.
    ///
    /// Opening read-write turned every read-only deployment -- a container as
    /// a non-root user, a udev rule granting a group read -- into "no PHC
    /// here", indistinguishable from having no hardware. The device below is
    /// not a PHC, so the call still fails; what matters is WHICH error comes
    /// back. EACCES means `open` was refused and the flags are wrong again.
    /// EINVAL means the open succeeded and the kernel rejected the clock,
    /// which is as far as a regular file can get.
    #[test]
    fn a_read_only_descriptor_gets_past_open() {
        // SAFETY: getuid() is always safe.
        if unsafe { libc::getuid() } == 0 {
            // Root ignores the permission bits, so this proves nothing.
            return;
        }
        let path = std::env::temp_dir().join("mec_cast_phc_ro_probe");
        std::fs::write(&path, b"").expect("write probe file");
        let mut perms = std::fs::metadata(&path).unwrap().permissions();
        std::os::unix::fs::PermissionsExt::set_mode(&mut perms, 0o444);
        std::fs::set_permissions(&path, perms).expect("chmod 0444");

        let err = PhcClock::open(path.to_str().unwrap()).expect_err("a regular file is not a PHC");
        let _ = std::fs::remove_file(&path);

        assert_ne!(
            err.kind(),
            io::ErrorKind::PermissionDenied,
            "open was refused on a readable file: the device is being opened \
             for write again, which locks out every read-only deployment"
        );
    }

    #[test]
    fn a_clock_id_round_trips_to_its_descriptor() {
        for fd in [0, 3, 5, 9, 17, 255, 1023] {
            assert_eq!(clockid_to_fd(fd_to_clockid(fd)), fd);
        }
    }

    #[test]
    fn a_clock_id_is_negative_so_the_kernel_treats_it_as_dynamic() {
        // clockid_to_kclock() only consults CLOCKFD_MASK for id < 0.
        for fd in [0, 3, 255] {
            assert!(fd_to_clockid(fd) < 0, "fd {fd} must yield a negative id");
        }
    }
}

#[cfg(test)]
fn clockid_to_fd(clk: libc::clockid_t) -> i32 {
    !(clk >> 3)
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
    /// Open a PHC device (e.g. `/dev/ptp0`) read-only and verify it is
    /// readable as a clock.
    pub fn open(device: &str) -> io::Result<Self> {
        // Read-only on purpose; see the module docs. Opening read-write asked
        // for a permission this type never exercises, and turned every
        // read-only deployment into "no PHC here".
        let file = OpenOptions::new().read(true).open(device)?;
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
