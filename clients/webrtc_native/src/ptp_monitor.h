#ifndef PTP_MONITOR_H_
#define PTP_MONITOR_H_

// PTP synchronization quality monitor.
// Tracks PHC offset, sync state, and reports quality metrics.

#include <cstdint>
#include <string>
#include <atomic>
#include <thread>
#include <mutex>
#include <memory>

enum class PtpSyncState {
  kUnknown,      // Not initialized or no PTP hardware
  kFreerun,      // PHC not locked to PTP master
  kTracking,     // Acquiring lock
  kLocked,       // Good sync achieved (<100ns offset)
  kHoldover      // Lost reference, coasting on local oscillator
};

struct PtpStatus {
  PtpSyncState state = PtpSyncState::kUnknown;
  double offset_ns = 0.0;       // Current PHC-to-system offset in nanoseconds
  double max_offset_ns = 0.0;   // Maximum observed offset since last reset
  uint64_t last_update_ns = 0;  // Monotonic timestamp of last status update
  std::string device;            // PHC device path
  bool reliable = false;         // True if measurements can be trusted
};

class PtpMonitor {
 public:
  explicit PtpMonitor(const std::string& phc_device = "/dev/ptp0");
  ~PtpMonitor();

  // Start background monitoring thread (polls every interval_ms)
  void Start(int interval_ms = 1000);
  void Stop();

  // Get current status (thread-safe)
  PtpStatus GetStatus() const;

  // Returns true if PTP is locked and offset < threshold_ns
  bool IsSyncReliable(double threshold_ns = 1000.0) const;

  // Returns JSON representation of current status
  std::string ToJson() const;

 private:
  void MonitorLoop();
  void UpdateStatus();

  std::string phc_device_;
  mutable std::mutex mutex_;
  PtpStatus status_;
  std::atomic<bool> running_{false};
  std::unique_ptr<std::thread> thread_;
  int interval_ms_ = 1000;
};

// Global singleton
PtpMonitor& GetPtpMonitor();
void InitPtpMonitor(const std::string& phc_device);

#endif  // PTP_MONITOR_H_
