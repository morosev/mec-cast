#ifndef DELAY_CLOCK_H_
#define DELAY_CLOCK_H_

// High-resolution clock abstraction for nanosecond delay measurement.
// Supports PTP Hardware Clock (PHC), CLOCK_REALTIME, and CLOCK_MONOTONIC.

#include <cstdint>
#include <string>

enum class ClockSource {
  kPTP,        // Direct read from PTP Hardware Clock (/dev/ptpN)
  kRealtime,   // CLOCK_REALTIME (disciplined by phc2sys from PTP)
  kMonotonic,  // CLOCK_MONOTONIC (local intervals only, not synced)
  kAuto        // Try PHC first, fall back to CLOCK_REALTIME
};

struct ClockConfig {
  ClockSource source = ClockSource::kAuto;
  std::string phc_device = "/dev/ptp0";  // PTP Hardware Clock device
};

class DelayClock {
 public:
  explicit DelayClock(const ClockConfig& config = ClockConfig());
  ~DelayClock();

  // Returns nanoseconds since Unix epoch from the synchronized clock (PTP or
  // CLOCK_REALTIME). This timestamp is comparable across PTP-synced nodes.
  uint64_t NowNs() const;

  // Returns nanoseconds from CLOCK_MONOTONIC for local interval measurement.
  // Not comparable across machines, but jitter-free and monotonic.
  uint64_t MonotonicNowNs() const;

  // Returns the active clock source after initialization.
  ClockSource ActiveSource() const { return active_source_; }

  // Returns true if PTP Hardware Clock was successfully opened.
  bool HasPTP() const { return phc_fd_ >= 0; }

  // Returns the PHC device path (e.g., "/dev/ptp0").
  const std::string& PhcDevice() const { return config_.phc_device; }

 private:
  ClockConfig config_;
  ClockSource active_source_ = ClockSource::kRealtime;
  int phc_fd_ = -1;        // File descriptor for /dev/ptpN
  int phc_clockid_ = -1;   // clockid_t derived from PHC fd

  bool TryOpenPHC();
  void ClosePHC();
};

// Global singleton for convenience (initialized once at startup)
DelayClock& GetDelayClock();
void InitDelayClock(const ClockConfig& config);

#endif  // DELAY_CLOCK_H_
