#include "ptp_monitor.h"
#include "delay_clock.h"

#include <cstdio>
#include <cstring>
#include <cmath>
#include <sstream>
#include <fcntl.h>
#include <unistd.h>
#include <time.h>
#include <linux/ptp_clock.h>

#define PTP_LOG(fmt, ...) fprintf(stderr, "[ptp_monitor] " fmt "\n", ##__VA_ARGS__)

static inline clockid_t fd_to_clockid(int fd) {
  return ~(static_cast<clockid_t>(fd) << 3);
}

PtpMonitor::PtpMonitor(const std::string& phc_device)
    : phc_device_(phc_device) {
  status_.device = phc_device;
}

PtpMonitor::~PtpMonitor() {
  Stop();
}

void PtpMonitor::Start(int interval_ms) {
  if (running_) return;
  interval_ms_ = interval_ms;
  running_ = true;
  thread_ = std::make_unique<std::thread>(&PtpMonitor::MonitorLoop, this);
  PTP_LOG("Monitor started (interval=%dms, device=%s)",
          interval_ms_, phc_device_.c_str());
}

void PtpMonitor::Stop() {
  running_ = false;
  if (thread_ && thread_->joinable()) {
    thread_->join();
    thread_.reset();
  }
}

PtpStatus PtpMonitor::GetStatus() const {
  std::lock_guard<std::mutex> lock(mutex_);
  return status_;
}

bool PtpMonitor::IsSyncReliable(double threshold_ns) const {
  std::lock_guard<std::mutex> lock(mutex_);
  return status_.reliable && std::fabs(status_.offset_ns) < threshold_ns;
}

std::string PtpMonitor::ToJson() const {
  std::lock_guard<std::mutex> lock(mutex_);
  std::ostringstream oss;
  const char* state_str = "unknown";
  switch (status_.state) {
    case PtpSyncState::kFreerun:  state_str = "freerun"; break;
    case PtpSyncState::kTracking: state_str = "tracking"; break;
    case PtpSyncState::kLocked:   state_str = "locked"; break;
    case PtpSyncState::kHoldover: state_str = "holdover"; break;
    default: break;
  }
  oss << "{\"state\":\"" << state_str << "\""
      << ",\"offset_ns\":" << status_.offset_ns
      << ",\"max_offset_ns\":" << status_.max_offset_ns
      << ",\"device\":\"" << status_.device << "\""
      << ",\"reliable\":" << (status_.reliable ? "true" : "false")
      << "}";
  return oss.str();
}

void PtpMonitor::MonitorLoop() {
  while (running_) {
    UpdateStatus();
    std::this_thread::sleep_for(std::chrono::milliseconds(interval_ms_));
  }
}

void PtpMonitor::UpdateStatus() {
  // Measure offset between PHC and CLOCK_REALTIME.
  // This tells us how well phc2sys is disciplining the system clock.
  int fd = open(phc_device_.c_str(), O_RDONLY);
  if (fd < 0) {
    std::lock_guard<std::mutex> lock(mutex_);
    status_.state = PtpSyncState::kUnknown;
    status_.reliable = false;
    return;
  }

  clockid_t phc_clk = fd_to_clockid(fd);
  struct timespec ts_phc, ts_sys1, ts_sys2;

  // Bracket PHC read with system clock reads for offset estimation
  clock_gettime(CLOCK_REALTIME, &ts_sys1);
  int ret = clock_gettime(phc_clk, &ts_phc);
  clock_gettime(CLOCK_REALTIME, &ts_sys2);
  close(fd);

  if (ret != 0) {
    std::lock_guard<std::mutex> lock(mutex_);
    status_.state = PtpSyncState::kUnknown;
    status_.reliable = false;
    return;
  }

  // Compute offset: PHC - system_midpoint
  uint64_t sys1_ns = static_cast<uint64_t>(ts_sys1.tv_sec) * 1000000000ULL + ts_sys1.tv_nsec;
  uint64_t sys2_ns = static_cast<uint64_t>(ts_sys2.tv_sec) * 1000000000ULL + ts_sys2.tv_nsec;
  uint64_t phc_ns = static_cast<uint64_t>(ts_phc.tv_sec) * 1000000000ULL + ts_phc.tv_nsec;
  uint64_t sys_mid = (sys1_ns + sys2_ns) / 2;

  double offset = static_cast<double>(static_cast<int64_t>(phc_ns - sys_mid));

  std::lock_guard<std::mutex> lock(mutex_);
  status_.offset_ns = offset;
  status_.last_update_ns = GetDelayClock().MonotonicNowNs();

  if (std::fabs(offset) > status_.max_offset_ns) {
    status_.max_offset_ns = std::fabs(offset);
  }

  // Determine sync state based on offset magnitude
  double abs_offset = std::fabs(offset);
  if (abs_offset < 100.0) {
    status_.state = PtpSyncState::kLocked;
    status_.reliable = true;
  } else if (abs_offset < 10000.0) {
    status_.state = PtpSyncState::kTracking;
    status_.reliable = true;
  } else if (abs_offset < 1000000.0) {
    status_.state = PtpSyncState::kHoldover;
    status_.reliable = false;
  } else {
    status_.state = PtpSyncState::kFreerun;
    status_.reliable = false;
  }
}

// --- Global singleton ---
static PtpMonitor* g_ptp_monitor = nullptr;

PtpMonitor& GetPtpMonitor() {
  if (!g_ptp_monitor) {
    g_ptp_monitor = new PtpMonitor();
  }
  return *g_ptp_monitor;
}

void InitPtpMonitor(const std::string& phc_device) {
  if (g_ptp_monitor) {
    g_ptp_monitor->Stop();
    delete g_ptp_monitor;
  }
  g_ptp_monitor = new PtpMonitor(phc_device);
}
