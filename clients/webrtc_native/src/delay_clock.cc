#include "delay_clock.h"

#include <cerrno>
#include <cstdio>
#include <cstring>
#include <time.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/ioctl.h>
#include <linux/ptp_clock.h>

#define CLOCK_LOG(fmt, ...) fprintf(stderr, "[delay_clock] " fmt "\n", ##__VA_ARGS__)

// Convert PHC fd to clockid_t (Linux kernel convention)
static inline clockid_t fd_to_clockid(int fd) {
  return ~(static_cast<clockid_t>(fd) << 3);
}

DelayClock::DelayClock(const ClockConfig& config) : config_(config) {
  switch (config_.source) {
    case ClockSource::kPTP:
      if (!TryOpenPHC()) {
        CLOCK_LOG("ERROR: Failed to open PHC device %s",
                  config_.phc_device.c_str());
        CLOCK_LOG("Falling back to CLOCK_REALTIME");
        active_source_ = ClockSource::kRealtime;
      }
      break;

    case ClockSource::kAuto:
      if (TryOpenPHC()) {
        CLOCK_LOG("PTP Hardware Clock detected at %s", config_.phc_device.c_str());
      } else {
        CLOCK_LOG("No PHC available, using CLOCK_REALTIME (ensure phc2sys is running for PTP sync)");
        active_source_ = ClockSource::kRealtime;
      }
      break;

    case ClockSource::kRealtime:
      active_source_ = ClockSource::kRealtime;
      CLOCK_LOG("Using CLOCK_REALTIME");
      break;

    case ClockSource::kMonotonic:
      active_source_ = ClockSource::kMonotonic;
      CLOCK_LOG("Using CLOCK_MONOTONIC (local intervals only, no cross-node sync)");
      break;
  }
}

DelayClock::~DelayClock() {
  ClosePHC();
}

bool DelayClock::TryOpenPHC() {
  phc_fd_ = open(config_.phc_device.c_str(), O_RDWR);
  if (phc_fd_ < 0) {
    CLOCK_LOG("Cannot open %s: %s", config_.phc_device.c_str(), strerror(errno));
    return false;
  }

  // Verify it's a valid PTP clock by reading time
  struct timespec ts;
  clockid_t clkid = fd_to_clockid(phc_fd_);
  if (clock_gettime(clkid, &ts) != 0) {
    CLOCK_LOG("clock_gettime on PHC fd %d failed: %s", phc_fd_, strerror(errno));
    close(phc_fd_);
    phc_fd_ = -1;
    return false;
  }

  phc_clockid_ = static_cast<int>(clkid);
  active_source_ = ClockSource::kPTP;
  CLOCK_LOG("PHC opened: %s (clockid=%d, time=%ld.%09ld)",
            config_.phc_device.c_str(), phc_clockid_, ts.tv_sec, ts.tv_nsec);
  return true;
}

void DelayClock::ClosePHC() {
  if (phc_fd_ >= 0) {
    close(phc_fd_);
    phc_fd_ = -1;
    phc_clockid_ = -1;
  }
}

uint64_t DelayClock::NowNs() const {
  struct timespec ts;
  clockid_t clk;

  switch (active_source_) {
    case ClockSource::kPTP:
      clk = static_cast<clockid_t>(phc_clockid_);
      break;
    case ClockSource::kRealtime:
    case ClockSource::kAuto:
      clk = CLOCK_REALTIME;
      break;
    case ClockSource::kMonotonic:
      clk = CLOCK_MONOTONIC;
      break;
  }

  clock_gettime(clk, &ts);
  return static_cast<uint64_t>(ts.tv_sec) * 1000000000ULL +
         static_cast<uint64_t>(ts.tv_nsec);
}

uint64_t DelayClock::MonotonicNowNs() const {
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return static_cast<uint64_t>(ts.tv_sec) * 1000000000ULL +
         static_cast<uint64_t>(ts.tv_nsec);
}

// --- Global singleton ---
static DelayClock* g_delay_clock = nullptr;

DelayClock& GetDelayClock() {
  if (!g_delay_clock) {
    g_delay_clock = new DelayClock();
  }
  return *g_delay_clock;
}

void InitDelayClock(const ClockConfig& config) {
  if (g_delay_clock) {
    delete g_delay_clock;
  }
  g_delay_clock = new DelayClock(config);
}
