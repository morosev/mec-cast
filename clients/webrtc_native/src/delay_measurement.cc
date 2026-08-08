#include "delay_measurement.h"
#include "ptp_monitor.h"

#include <cstdio>
#include <cstring>
#include <cmath>
#include <algorithm>
#include <sstream>

#define DM_LOG(fmt, ...) fprintf(stderr, "[delay_meas] " fmt "\n", ##__VA_ARGS__)

// --- DelayStats ---

void DelayStats::Update(int64_t value_ns) {
  last_ns = value_ns;
  if (value_ns < min_ns) min_ns = value_ns;
  if (value_ns > max_ns) max_ns = value_ns;
  count++;

  // Welford's online algorithm for mean and variance
  double delta = static_cast<double>(value_ns) - avg_ns;
  avg_ns += delta / static_cast<double>(count);
  double delta2 = static_cast<double>(value_ns) - avg_ns;
  variance_ns += delta * delta2;

  // Approximate P99 using exponential moving average toward high values
  if (count == 1) {
    p99_ns = value_ns;
  } else {
    if (value_ns > p99_ns) {
      p99_ns = p99_ns + (value_ns - p99_ns) / 10;
    } else {
      p99_ns = p99_ns - (p99_ns - value_ns) / 1000;
    }
  }
}

void DelayStats::Reset() {
  last_ns = 0;
  min_ns = INT64_MAX;
  max_ns = 0;
  avg_ns = 0.0;
  variance_ns = 0.0;
  p99_ns = 0;
  count = 0;
}

// --- DelayMeasurement ---

DelayMeasurement::DelayMeasurement(const DelayMeasurementConfig& config)
    : config_(config) {
  reports_.resize(config.ring_buffer_size);
  memset(pending_, 0, sizeof(pending_));
  memset(pending_active_, 0, sizeof(pending_active_));
  DM_LOG("Initialized (buffer_size=%d)", config.ring_buffer_size);
}

DelayMeasurement::~DelayMeasurement() = default;

DelayReport* DelayMeasurement::FindOrCreateReport(uint64_t frame_id) {
  for (int i = 0; i < kMaxPending; i++) {
    if (pending_active_[i] && pending_[i].frame_id == frame_id) {
      return &pending_[i];
    }
  }
  for (int i = 0; i < kMaxPending; i++) {
    if (!pending_active_[i]) {
      memset(&pending_[i], 0, sizeof(DelayReport));
      pending_[i].frame_id = frame_id;
      pending_active_[i] = true;
      return &pending_[i];
    }
  }
  // Evict oldest if all full
  memmove(&pending_[0], &pending_[1], sizeof(DelayReport) * (kMaxPending - 1));
  memmove(&pending_active_[0], &pending_active_[1], sizeof(bool) * (kMaxPending - 1));
  memset(&pending_[kMaxPending - 1], 0, sizeof(DelayReport));
  pending_[kMaxPending - 1].frame_id = frame_id;
  pending_active_[kMaxPending - 1] = true;
  return &pending_[kMaxPending - 1];
}

void DelayMeasurement::RecordCapture(uint64_t frame_id, uint64_t timestamp_ns) {
  std::lock_guard<std::mutex> lock(mutex_);
  if (!config_.enabled) return;
  auto* r = FindOrCreateReport(frame_id);
  r->capture_ns = timestamp_ns;
}

void DelayMeasurement::RecordEncodeStart(uint64_t frame_id, uint64_t timestamp_ns) {
  std::lock_guard<std::mutex> lock(mutex_);
  if (!config_.enabled) return;
  auto* r = FindOrCreateReport(frame_id);
  r->encode_start_ns = timestamp_ns;
}

void DelayMeasurement::RecordEncodeEnd(uint64_t frame_id, uint64_t timestamp_ns) {
  std::lock_guard<std::mutex> lock(mutex_);
  if (!config_.enabled) return;
  auto* r = FindOrCreateReport(frame_id);
  r->encode_end_ns = timestamp_ns;
}

void DelayMeasurement::RecordRtpSend(uint64_t frame_id, uint64_t timestamp_ns) {
  std::lock_guard<std::mutex> lock(mutex_);
  if (!config_.enabled) return;
  auto* r = FindOrCreateReport(frame_id);
  r->rtp_send_ns = timestamp_ns;
}

void DelayMeasurement::RecordRtpRecv(uint64_t frame_id, uint64_t send_timestamp_ns,
                                      uint64_t recv_timestamp_ns) {
  std::lock_guard<std::mutex> lock(mutex_);
  if (!config_.enabled) return;
  auto* r = FindOrCreateReport(frame_id);
  r->rtp_send_ns = send_timestamp_ns;
  r->rtp_recv_ns = recv_timestamp_ns;

  if (send_timestamp_ns > 0 && recv_timestamp_ns > 0) {
    r->network_delay_ns = static_cast<int64_t>(recv_timestamp_ns - send_timestamp_ns);
  }
}

void DelayMeasurement::RecordDecodeStart(uint64_t frame_id, uint64_t timestamp_ns) {
  std::lock_guard<std::mutex> lock(mutex_);
  if (!config_.enabled) return;
  auto* r = FindOrCreateReport(frame_id);
  r->decode_start_ns = timestamp_ns;
}

void DelayMeasurement::RecordDecodeEnd(uint64_t frame_id, uint64_t timestamp_ns) {
  std::lock_guard<std::mutex> lock(mutex_);
  if (!config_.enabled) return;
  auto* r = FindOrCreateReport(frame_id);
  r->decode_end_ns = timestamp_ns;
}

void DelayMeasurement::RecordRender(uint64_t frame_id, uint64_t timestamp_ns) {
  std::lock_guard<std::mutex> lock(mutex_);
  if (!config_.enabled) return;
  auto* r = FindOrCreateReport(frame_id);
  r->render_ns = timestamp_ns;
  FinalizeReport(frame_id);
}

void DelayMeasurement::FinalizeReport(uint64_t frame_id) {
  int idx = -1;
  for (int i = 0; i < kMaxPending; i++) {
    if (pending_active_[i] && pending_[i].frame_id == frame_id) {
      idx = i;
      break;
    }
  }
  if (idx < 0) return;

  DelayReport& r = pending_[idx];

  if (r.encode_start_ns > 0 && r.encode_end_ns > 0) {
    r.encoding_delay_ns = static_cast<int64_t>(r.encode_end_ns - r.encode_start_ns);
    stats_encoding_.Update(r.encoding_delay_ns);
  }

  if (r.decode_start_ns > 0 && r.decode_end_ns > 0) {
    r.decoding_delay_ns = static_cast<int64_t>(r.decode_end_ns - r.decode_start_ns);
    stats_decoding_.Update(r.decoding_delay_ns);
  }

  if (r.rtp_recv_ns > 0 && r.decode_start_ns > 0) {
    r.jitter_buffer_ns = static_cast<int64_t>(r.decode_start_ns - r.rtp_recv_ns);
    stats_jitter_buffer_.Update(r.jitter_buffer_ns);
  }

  if (r.rtp_send_ns > 0 && r.rtp_recv_ns > 0) {
    r.network_delay_ns = static_cast<int64_t>(r.rtp_recv_ns - r.rtp_send_ns);
    stats_network_.Update(r.network_delay_ns);
  }

  if (r.capture_ns > 0 && r.render_ns > 0) {
    r.glass_to_glass_ns = static_cast<int64_t>(r.render_ns - r.capture_ns);
    stats_glass_to_glass_.Update(r.glass_to_glass_ns);
  }

  if (r.capture_ns > 0 && r.rtp_send_ns > 0) {
    r.sender_pipeline_ns = static_cast<int64_t>(r.rtp_send_ns - r.capture_ns);
    stats_sender_pipeline_.Update(r.sender_pipeline_ns);
  }

  if (r.rtp_recv_ns > 0 && r.render_ns > 0) {
    r.receiver_pipeline_ns = static_cast<int64_t>(r.render_ns - r.rtp_recv_ns);
    stats_receiver_pipeline_.Update(r.receiver_pipeline_ns);
  }

  reports_[write_index_] = r;
  write_index_ = (write_index_ + 1) % static_cast<int>(reports_.size());
  total_frames_++;

  pending_active_[idx] = false;
}

bool DelayMeasurement::GetLatestReport(DelayReport* out) const {
  std::lock_guard<std::mutex> lock(mutex_);
  if (total_frames_ == 0) return false;
  int idx = (write_index_ - 1 + static_cast<int>(reports_.size())) %
            static_cast<int>(reports_.size());
  *out = reports_[idx];
  return true;
}

DelayMeasurement::AggregatedStats DelayMeasurement::GetStats() const {
  std::lock_guard<std::mutex> lock(mutex_);
  AggregatedStats s;
  s.network = stats_network_;
  s.encoding = stats_encoding_;
  s.decoding = stats_decoding_;
  s.jitter_buffer = stats_jitter_buffer_;
  s.glass_to_glass = stats_glass_to_glass_;
  s.sender_pipeline = stats_sender_pipeline_;
  s.receiver_pipeline = stats_receiver_pipeline_;
  s.total_frames = total_frames_;
  s.ptp_reliable = GetPtpMonitor().IsSyncReliable();
  return s;
}

void DelayMeasurement::Reset() {
  std::lock_guard<std::mutex> lock(mutex_);
  stats_network_.Reset();
  stats_encoding_.Reset();
  stats_decoding_.Reset();
  stats_jitter_buffer_.Reset();
  stats_glass_to_glass_.Reset();
  stats_sender_pipeline_.Reset();
  stats_receiver_pipeline_.Reset();
  total_frames_ = 0;
  write_index_ = 0;
  memset(pending_active_, 0, sizeof(pending_active_));
  DM_LOG("Statistics reset");
}

void DelayMeasurement::Configure(const DelayMeasurementConfig& config) {
  std::lock_guard<std::mutex> lock(mutex_);
  config_ = config;
  if (static_cast<int>(reports_.size()) != config.ring_buffer_size) {
    reports_.resize(config.ring_buffer_size);
    write_index_ = 0;
  }
  DM_LOG("Reconfigured (enabled=%d, buffer=%d)", config.enabled, config.ring_buffer_size);
}

static void AppendStatJson(std::ostringstream& oss, const char* name,
                           const DelayStats& s) {
  oss << ",\"" << name << "\":{";
  oss << "\"last_ns\":" << s.last_ns;
  oss << ",\"min_ns\":" << (s.count > 0 ? s.min_ns : 0);
  oss << ",\"max_ns\":" << s.max_ns;
  oss << ",\"avg_ns\":" << s.avg_ns;
  oss << ",\"p99_ns\":" << s.p99_ns;
  oss << ",\"count\":" << s.count;
  oss << "}";
}

std::string DelayMeasurement::ToJson() const {
  std::lock_guard<std::mutex> lock(mutex_);
  std::ostringstream oss;
  oss << "{\"total_frames\":" << total_frames_;
  oss << ",\"enabled\":" << (config_.enabled ? "true" : "false");
  oss << ",\"ptp_reliable\":" << (GetPtpMonitor().IsSyncReliable() ? "true" : "false");

  AppendStatJson(oss, "network", stats_network_);
  AppendStatJson(oss, "encoding", stats_encoding_);
  AppendStatJson(oss, "decoding", stats_decoding_);
  AppendStatJson(oss, "jitter_buffer", stats_jitter_buffer_);
  AppendStatJson(oss, "glass_to_glass", stats_glass_to_glass_);
  AppendStatJson(oss, "sender_pipeline", stats_sender_pipeline_);
  AppendStatJson(oss, "receiver_pipeline", stats_receiver_pipeline_);

  if (total_frames_ > 0) {
    int idx = (write_index_ - 1 + static_cast<int>(reports_.size())) %
              static_cast<int>(reports_.size());
    const auto& r = reports_[idx];
    oss << ",\"latest\":{";
    oss << "\"frame_id\":" << r.frame_id;
    oss << ",\"network_ns\":" << r.network_delay_ns;
    oss << ",\"encoding_ns\":" << r.encoding_delay_ns;
    oss << ",\"decoding_ns\":" << r.decoding_delay_ns;
    oss << ",\"jitter_buffer_ns\":" << r.jitter_buffer_ns;
    oss << ",\"glass_to_glass_ns\":" << r.glass_to_glass_ns;
    oss << ",\"sender_pipeline_ns\":" << r.sender_pipeline_ns;
    oss << ",\"receiver_pipeline_ns\":" << r.receiver_pipeline_ns;
    oss << "}";
  }

  oss << "}";
  return oss.str();
}

// --- Global singleton ---
static DelayMeasurement* g_delay_measurement = nullptr;

DelayMeasurement& GetDelayMeasurement() {
  if (!g_delay_measurement) {
    g_delay_measurement = new DelayMeasurement();
  }
  return *g_delay_measurement;
}

void InitDelayMeasurement(const DelayMeasurementConfig& config) {
  if (g_delay_measurement) {
    delete g_delay_measurement;
  }
  g_delay_measurement = new DelayMeasurement(config);
}
