#ifndef DELAY_MEASUREMENT_H_
#define DELAY_MEASUREMENT_H_

// Delay measurement engine: collects per-frame timing data,
// computes delay components, and maintains running statistics.

#include <cstdint>
#include <string>
#include <mutex>
#include <vector>

// Per-frame delay report with all timestamp components
struct DelayReport {
  uint64_t frame_id;           // RTP timestamp or sequence number
  uint64_t capture_ns;         // T_capture: camera/mic grabs frame
  uint64_t encode_start_ns;    // T_enc_in: frame enters encoder
  uint64_t encode_end_ns;      // T_enc_out: encoded frame exits encoder
  uint64_t rtp_send_ns;        // T_rtp_send: packet leaves sender (PTP clock)
  uint64_t rtp_recv_ns;        // T_rtp_recv: packet arrives at receiver (PTP clock)
  uint64_t decode_start_ns;    // T_dec_in: frame enters decoder
  uint64_t decode_end_ns;      // T_dec_out: decoded frame exits decoder
  uint64_t render_ns;          // T_render: frame displayed on screen

  // Derived delays (nanoseconds, can be negative if clocks drift)
  int64_t network_delay_ns;    // rtp_recv - rtp_send (requires PTP sync)
  int64_t encoding_delay_ns;   // encode_end - encode_start
  int64_t decoding_delay_ns;   // decode_end - decode_start
  int64_t jitter_buffer_ns;    // decode_start - rtp_recv
  int64_t glass_to_glass_ns;   // render - capture (requires PTP sync)
  int64_t sender_pipeline_ns;  // rtp_send - capture
  int64_t receiver_pipeline_ns; // render - rtp_recv
};

// Running statistics for a single delay metric
struct DelayStats {
  int64_t last_ns = 0;
  int64_t min_ns = INT64_MAX;
  int64_t max_ns = 0;
  double avg_ns = 0.0;
  double variance_ns = 0.0;   // For jitter computation
  int64_t p99_ns = 0;         // 99th percentile (approximate)
  uint64_t count = 0;

  void Update(int64_t value_ns);
  void Reset();
};

// Configuration for the delay measurement system
struct DelayMeasurementConfig {
  bool enabled = true;
  int ring_buffer_size = 1000;     // Keep last N reports
  bool measure_network = true;     // Requires PTP sync
  bool measure_glass_to_glass = true;
  bool measure_encoding = true;
  bool measure_decoding = true;
  bool measure_jitter_buffer = true;
  double ptp_reliability_threshold_ns = 1000.0;  // Max PTP offset for valid measurements
};

class DelayMeasurement {
 public:
  explicit DelayMeasurement(const DelayMeasurementConfig& config = DelayMeasurementConfig());
  ~DelayMeasurement();

  // Record timestamps at various pipeline stages (called from WebRTC callbacks)
  void RecordCapture(uint64_t frame_id, uint64_t timestamp_ns);
  void RecordEncodeStart(uint64_t frame_id, uint64_t timestamp_ns);
  void RecordEncodeEnd(uint64_t frame_id, uint64_t timestamp_ns);
  void RecordRtpSend(uint64_t frame_id, uint64_t timestamp_ns);
  void RecordRtpRecv(uint64_t frame_id, uint64_t send_timestamp_ns, uint64_t recv_timestamp_ns);
  void RecordDecodeStart(uint64_t frame_id, uint64_t timestamp_ns);
  void RecordDecodeEnd(uint64_t frame_id, uint64_t timestamp_ns);
  void RecordRender(uint64_t frame_id, uint64_t timestamp_ns);

  // Get the latest completed report
  bool GetLatestReport(DelayReport* out) const;

  // Get aggregated statistics
  struct AggregatedStats {
    DelayStats network;
    DelayStats encoding;
    DelayStats decoding;
    DelayStats jitter_buffer;
    DelayStats glass_to_glass;
    DelayStats sender_pipeline;
    DelayStats receiver_pipeline;
    uint64_t total_frames;
    bool ptp_reliable;
  };
  AggregatedStats GetStats() const;

  // Reset all statistics
  void Reset();

  // Get full report as JSON string
  std::string ToJson() const;

  // Configuration
  void Configure(const DelayMeasurementConfig& config);
  bool IsEnabled() const { return config_.enabled; }

 private:
  void FinalizeReport(uint64_t frame_id);
  DelayReport* FindOrCreateReport(uint64_t frame_id);

  mutable std::mutex mutex_;
  DelayMeasurementConfig config_;

  // Ring buffer of reports
  std::vector<DelayReport> reports_;
  int write_index_ = 0;
  uint64_t total_frames_ = 0;

  // Pending reports (frames still being processed)
  static constexpr int kMaxPending = 64;
  DelayReport pending_[64];
  bool pending_active_[64] = {};

  // Running statistics
  DelayStats stats_network_;
  DelayStats stats_encoding_;
  DelayStats stats_decoding_;
  DelayStats stats_jitter_buffer_;
  DelayStats stats_glass_to_glass_;
  DelayStats stats_sender_pipeline_;
  DelayStats stats_receiver_pipeline_;
};

// Global singleton
DelayMeasurement& GetDelayMeasurement();
void InitDelayMeasurement(const DelayMeasurementConfig& config);

#endif  // DELAY_MEASUREMENT_H_
