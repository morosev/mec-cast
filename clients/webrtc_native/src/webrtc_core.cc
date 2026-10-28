// WebRTC core implementation — Linux and macOS.
// Built against the prebuilt libwebrtc (third_party/webrtc.lock), which links
// the platform's own C++ standard library, with the defines it was built with.
// Only exposes a C interface (webrtc_core.h).

#include "webrtc_core.h"

#include "delay_clock.h"
#include "delay_measurement.h"
#include "mec_cast_telemetry.h"
#include "ptp_monitor.h"

#include <algorithm>
#include <atomic>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <chrono>
#include <condition_variable>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <queue>
#include <sstream>
#include <string>
#include <functional>
#include <thread>
#include <vector>

#include <sys/stat.h>
#include <time.h>

#include "api/create_peerconnection_factory.h"
#include "api/peer_connection_interface.h"
#include "api/jsep.h"
#include "api/audio_codecs/builtin_audio_decoder_factory.h"
#include "api/audio_codecs/builtin_audio_encoder_factory.h"
#include "api/audio_options.h"
#include "api/scoped_refptr.h"
#include "api/make_ref_counted.h"
#include "api/rtp_packet_info.h"
#include "api/rtp_packet_infos.h"
#include "api/video/video_frame.h"
#include "api/video/video_sink_interface.h"
#include "api/video/i420_buffer.h"
#include "api/video_codecs/video_encoder_factory_template.h"
#include "api/video_codecs/video_encoder_factory_template_libvpx_vp8_adapter.h"
#include "api/video_codecs/video_encoder_factory_template_libvpx_vp9_adapter.h"
#include "api/video_codecs/video_encoder_factory_template_open_h264_adapter.h"
#include "api/video_codecs/video_encoder_factory_template_libaom_av1_adapter.h"
#include "api/video_codecs/video_decoder_factory_template.h"
#include "api/video_codecs/video_decoder_factory_template_libvpx_vp8_adapter.h"
#include "api/video_codecs/video_decoder_factory_template_libvpx_vp9_adapter.h"
#include "api/video_codecs/video_decoder_factory_template_open_h264_adapter.h"
#include "api/video_codecs/video_decoder_factory_template_dav1d_adapter.h"
#include "pc/video_track_source.h"
#include "test/test_video_capturer.h"
// libwebrtc's camera module has no macOS implementation: a camera there needs
// an AVFoundation capturer of our own. Until then macOS sends synthetic video.
#if !defined(WEBRTC_MAC)
#include "modules/video_capture/video_capture.h"
#include "modules/video_capture/video_capture_factory.h"
#include "test/vcm_capturer.h"
#endif
#include "rtc_base/thread.h"
#include "rtc_base/time_utils.h"
#include "rtc_base/logging.h"
#include "third_party/libyuv/include/libyuv.h"
#include "api/stats/rtc_stats_collector_callback.h"
#include "api/stats/rtcstats_objects.h"
#include "api/stats/rtc_stats_report.h"

#include <SDL.h>

#define DEMO_LOG(fmt, ...) fprintf(stderr, "[webrtc_core] " fmt "\n", ##__VA_ARGS__)

// --- File-based log sink for WebRTC internal logs ---
class FileLogSink : public webrtc::LogSink {
 public:
  FileLogSink(const std::string& path) {
    file_ = fopen(path.c_str(), "w");
    if (file_) {
      DEMO_LOG("Log file opened: %s", path.c_str());
    } else {
      DEMO_LOG("ERROR: Could not open log file: %s", path.c_str());
    }
  }
  ~FileLogSink() override {
    if (file_) fclose(file_);
  }
  void OnLogMessage(const std::string& message) override {
    if (file_) {
      fwrite(message.data(), 1, message.size(), file_);
      fflush(file_);
    }
  }
 private:
  FILE* file_ = nullptr;
};

// Internal event
struct InternalEvent {
  std::string type;
  std::string data;
};

// Helper: ref-counted CreateSessionDescriptionObserver
class CreateSdpObserver : public webrtc::CreateSessionDescriptionObserver {
 public:
  using SuccessCb = std::function<void(webrtc::SessionDescriptionInterface*)>;
  using FailureCb = std::function<void(webrtc::RTCError)>;

  static webrtc::scoped_refptr<CreateSdpObserver> Create(SuccessCb s, FailureCb f) {
    return webrtc::make_ref_counted<CreateSdpObserver>(std::move(s), std::move(f));
  }
  CreateSdpObserver(SuccessCb s, FailureCb f)
      : on_success_(std::move(s)), on_failure_(std::move(f)) {}
  void OnSuccess(webrtc::SessionDescriptionInterface* desc) override {
    if (on_success_) on_success_(desc);
  }
  void OnFailure(webrtc::RTCError error) override {
    if (on_failure_) on_failure_(std::move(error));
  }
 private:
  SuccessCb on_success_;
  FailureCb on_failure_;
};

// Helper: ref-counted SetSessionDescriptionObserver
class SetSdpObserver : public webrtc::SetSessionDescriptionObserver {
 public:
  using SuccessCb = std::function<void()>;
  using FailureCb = std::function<void(webrtc::RTCError)>;

  static webrtc::scoped_refptr<SetSdpObserver> Create(SuccessCb s, FailureCb f) {
    return webrtc::make_ref_counted<SetSdpObserver>(std::move(s), std::move(f));
  }
  SetSdpObserver(SuccessCb s, FailureCb f)
      : on_success_(std::move(s)), on_failure_(std::move(f)) {}
  void OnSuccess() override { if (on_success_) on_success_(); }
  void OnFailure(webrtc::RTCError error) override {
    if (on_failure_) on_failure_(std::move(error));
  }
 private:
  SuccessCb on_success_;
  FailureCb on_failure_;
};

// Escape a string for JSON
static std::string JsonEscape(const std::string& s) {
  std::ostringstream oss;
  for (char c : s) {
    switch (c) {
      case '"':  oss << "\\\""; break;
      case '\\': oss << "\\\\"; break;
      case '\n': oss << "\\n"; break;
      case '\r': oss << "\\r"; break;
      case '\t': oss << "\\t"; break;
      default:   oss << c;
    }
  }
  return oss.str();
}

// --- Video source settings, from the client's configuration ---
struct VideoConfig {
  bool synthetic = false;  // synthetic frames instead of a camera
  int width = 640;
  int height = 480;
  int fps = 30;
  uint32_t seed = 42;
  bool render = true;  // show received video in a window
};

// --- Synthetic video: deterministic frames, no camera needed ---
//
// The media counterpart of the lidar client's seeded patterns: frame n of a
// given (seed, size) is always the same picture, so runs are reproducible and
// a machine without a camera -- CI, a lab host -- still sends real video.
// Each frame is a moving gradient with seeded noise blocks, so the encoder has
// work to do, and a row of black/white blocks along the top carries the frame
// number in binary for anyone checking frames by eye.
class SyntheticCapturer : public webrtc::test::TestVideoCapturer {
 public:
  SyntheticCapturer(int width, int height, int fps, uint32_t seed)
      : width_(width), height_(height), fps_(fps > 0 ? fps : 30), seed_(seed) {
    Start();
  }
  ~SyntheticCapturer() override { Stop(); }

  void Start() override {
    if (running_.exchange(true)) return;
    thread_ = std::thread([this] { Run(); });
  }
  void Stop() override {
    if (!running_.exchange(false)) return;
    if (thread_.joinable()) thread_.join();
  }
  int GetFrameWidth() const override { return width_; }
  int GetFrameHeight() const override { return height_; }

 private:
  void Run() {
    const auto period = std::chrono::nanoseconds(1000000000LL / fps_);
    auto next = std::chrono::steady_clock::now();
    while (running_) {
      auto buffer = webrtc::I420Buffer::Create(width_, height_);
      Draw(buffer.get(), frame_);
      // The capture time is now, on WebRTC's clock: the fork's extension
      // converts it to CLOCK_REALTIME at packet egress as capture_ns.
      OnFrame(webrtc::VideoFrame::Builder()
                  .set_video_frame_buffer(buffer)
                  .set_timestamp_us(webrtc::TimeMicros())
                  .set_rotation(webrtc::kVideoRotation_0)
                  .build());
      ++frame_;
      next += period;
      std::this_thread::sleep_until(next);
    }
  }

  // A small, stable hash, so a block's noise depends only on (seed, frame,
  // block) and never on the platform's random number generator.
  static uint32_t Mix(uint32_t a, uint32_t b, uint32_t c) {
    uint32_t h = a * 0x9E3779B1u ^ (b + 0x7F4A7C15u) * 0x85EBCA77u ^ c * 0xC2B2AE3Du;
    h ^= h >> 15;
    h *= 0x2C1B3C6Du;
    h ^= h >> 12;
    return h;
  }

  void Draw(webrtc::I420Buffer* b, uint64_t n) const {
    const int w = b->width(), h = b->height();
    uint8_t* y = b->MutableDataY();
    const int sy = b->StrideY();
    const int shift = static_cast<int>(n * 4 % std::max(w, 1));
    const int block = 16;
    for (int row = 0; row < h; ++row) {
      for (int col = 0; col < w; ++col) {
        int v = ((col + shift) % std::max(w, 1)) * 255 / std::max(w - 1, 1);
        uint32_t noise = Mix(seed_, static_cast<uint32_t>(n), (row / block) * 4099 + col / block);
        v = (v + static_cast<int>(noise & 63) - 32);
        y[row * sy + col] = static_cast<uint8_t>(std::clamp(v, 16, 235));
      }
    }
    // Frame number, 32 bits, one block per bit, most significant first.
    for (int bit = 0; bit < 32 && (bit + 1) * block <= w; ++bit) {
      const uint8_t v = (n >> (31 - bit)) & 1 ? 235 : 16;
      for (int row = 0; row < std::min(block, h); ++row) {
        std::memset(y + row * sy + bit * block, v, block);
      }
    }
    // Chroma: a slow colour drift, uniform per frame.
    const int cw = (w + 1) / 2, ch = (h + 1) / 2;
    const uint8_t u = static_cast<uint8_t>(128 + 40 * ((n / 30) % 3) - 40);
    const uint8_t v = static_cast<uint8_t>(128 - 40 * ((n / 45) % 3) + 40);
    for (int row = 0; row < ch; ++row) {
      std::memset(b->MutableDataU() + row * b->StrideU(), u, cw);
      std::memset(b->MutableDataV() + row * b->StrideV(), v, cw);
    }
  }

  const int width_, height_, fps_;
  const uint32_t seed_;
  uint64_t frame_ = 0;
  std::atomic<bool> running_{false};
  std::thread thread_;
};

// --- Video track source: the synthetic generator or a camera ---
class CapturerTrackSource : public webrtc::VideoTrackSource {
 public:
  static webrtc::scoped_refptr<CapturerTrackSource> Create(const VideoConfig& cfg) {
    if (cfg.synthetic) {
      DEMO_LOG("Video source: synthetic %dx%d @ %d fps, seed %u",
               cfg.width, cfg.height, cfg.fps, cfg.seed);
      return webrtc::make_ref_counted<CapturerTrackSource>(
          std::make_unique<SyntheticCapturer>(cfg.width, cfg.height, cfg.fps, cfg.seed));
    }
#if defined(WEBRTC_MAC)
    DEMO_LOG("ERROR: no camera support on macOS yet: set video_source to synthetic");
    return nullptr;
#else
    std::unique_ptr<webrtc::VideoCaptureModule::DeviceInfo> info(
        webrtc::VideoCaptureFactory::CreateDeviceInfo());
    if (!info) {
      DEMO_LOG("ERROR: VideoCaptureFactory::CreateDeviceInfo returned null"
               " (no camera support on this platform: use the synthetic source)");
      return nullptr;
    }
    int num_devices = info->NumberOfDevices();
    DEMO_LOG("Video capture devices: %d", num_devices);
    for (int i = 0; i < num_devices; ++i) {
      char name[256], uid[256];
      info->GetDeviceName(i, name, sizeof(name), uid, sizeof(uid));
      DEMO_LOG("  Device %d: %s", i, name);
      auto* capturer = webrtc::test::VcmCapturer::Create(cfg.width, cfg.height, cfg.fps, i);
      if (capturer) {
        DEMO_LOG("Video capturer created OK (device %d)", i);
        return webrtc::make_ref_counted<CapturerTrackSource>(
            std::unique_ptr<webrtc::test::TestVideoCapturer>(capturer));
      }
    }
    DEMO_LOG("ERROR: No video capture device available");
    return nullptr;
#endif
  }

  void StopCapture() {
    capturer_.reset();
    DEMO_LOG("CapturerTrackSource: capture stopped");
  }

 protected:
  explicit CapturerTrackSource(
      std::unique_ptr<webrtc::test::TestVideoCapturer> capturer)
      : VideoTrackSource(/*remote=*/false), capturer_(std::move(capturer)) {}
  ~CapturerTrackSource() override = default;

 private:
  webrtc::VideoSourceInterface<webrtc::VideoFrame>* source() override {
    return capturer_.get();
  }
  std::unique_ptr<webrtc::test::TestVideoCapturer> capturer_;
};

// --- Shared telemetry spine (Profile B → mec-cast-telemetry) ---
//
// Runs ALONGSIDE the in-process DelayMeasurement above: that one still backs
// the interactive `delay report` output, while this feeds the same CSV schema
// and logging service as the ROS2/Zenoh profile, so media and point-cloud
// runs are directly comparable.
//
// Configured entirely by environment, matching the ROS2 nodes:
//   RUN_ID       experiment run id (joins profiles; default "dev-run")
//   RUNS_DIR     base output directory (default "runs")
//   LOGGING_URL  logging service base URL (optional; CSV always written)
//   MEC_CAST_TELEMETRY=0 disables it entirely
//
// Threading: the queue is single-producer and this is fed only from the video
// render callback, which WebRTC drives on one thread per remote track.
namespace {

// The receiver's site code (docs/_facts.yml outputs.sites): 3 is the relay,
// 0-2 the ROS profile's. A frame's seq is its RTP timestamp at every site, so
// the relay's samples and these join per frame.
constexpr uint8_t kSiteReceiver = 4;

MctRecorder* g_telemetry = nullptr;
std::once_flag g_telemetry_once;

const char* EnvOr(const char* name, const char* fallback) {
  const char* v = std::getenv(name);
  return (v && *v) ? v : fallback;
}

void ShutdownTelemetry() {
  if (!g_telemetry) return;
  MctReport r{};
  mct_recorder_stop(g_telemetry, &r);
  g_telemetry = nullptr;
  DEMO_LOG("telemetry: %llu samples written, %llu dropped, %llu snapshots posted",
           (unsigned long long)r.samples_written,
           (unsigned long long)r.samples_dropped,
           (unsigned long long)r.snapshots_posted);
}

MctRecorder* Telemetry() {
  std::call_once(g_telemetry_once, [] {
    if (std::strcmp(EnvOr("MEC_CAST_TELEMETRY", "1"), "0") == 0) {
      DEMO_LOG("telemetry: disabled via MEC_CAST_TELEMETRY=0");
      return;
    }
    const char* run_id = EnvOr("RUN_ID", "dev-run");
    std::string out_dir =
        std::string(EnvOr("RUNS_DIR", "runs")) + "/" + run_id + "/receiver";
    const char* logging_url = std::getenv("LOGGING_URL");

    g_telemetry = mct_recorder_start(run_id, "mec-cast-media", out_dir.c_str(),
                                     logging_url, 0.0);
    if (g_telemetry) {
      DEMO_LOG("telemetry: recording to %s (run_id=%s)", out_dir.c_str(), run_id);
      std::atexit(ShutdownTelemetry);
    } else {
      DEMO_LOG("telemetry: WARNING failed to start (is %s writable?)",
               out_dir.c_str());
    }
  });
  return g_telemetry;
}

}  // namespace

// --- Video renderer: an SDL2 window, driven from the app's main thread ---
//
// OnFrame runs on a WebRTC thread: it measures the frame and keeps it. The
// window belongs to Pump(), which the app calls from its main thread
// (webrtc_render_pump): macOS allows windows only there, and Linux does not
// mind, so one model serves both. Without a display, or with rendering off,
// every frame is still measured; nothing is drawn.
class VideoRenderer : public webrtc::VideoSinkInterface<webrtc::VideoFrame> {
 public:
  explicit VideoRenderer(bool show) : show_(show) {}

  // On the main thread, like Pump().
  ~VideoRenderer() override {
    if (texture_) SDL_DestroyTexture(texture_);
    if (renderer_) SDL_DestroyRenderer(renderer_);
    if (window_) SDL_DestroyWindow(window_);
  }

  // Draw the newest frame, if any. Main thread only.
  void Pump() {
    if (!show_) return;
    webrtc::scoped_refptr<webrtc::I420BufferInterface> frame;
    {
      std::lock_guard<std::mutex> lock(buffer_mutex_);
      if (!latest_) return;
      frame = latest_;
      latest_ = nullptr;
    }
    if (!window_ && !Open(frame->width(), frame->height())) return;
    SDL_Event event;
    while (SDL_PollEvent(&event)) {
      if (event.type == SDL_QUIT) {
        // Closing the window stops drawing; measuring goes on.
        DEMO_LOG("VideoRenderer: window closed, rendering stopped");
        show_ = false;
        return;
      }
    }
    if (frame->width() != tex_width_ || frame->height() != tex_height_) {
      if (texture_) SDL_DestroyTexture(texture_);
      texture_ = SDL_CreateTexture(renderer_, SDL_PIXELFORMAT_IYUV,
                                   SDL_TEXTUREACCESS_STREAMING,
                                   frame->width(), frame->height());
      tex_width_ = frame->width();
      tex_height_ = frame->height();
    }
    if (!texture_) return;
    SDL_UpdateYUVTexture(texture_, nullptr, frame->DataY(), frame->StrideY(),
                         frame->DataU(), frame->StrideU(),
                         frame->DataV(), frame->StrideV());
    SDL_RenderClear(renderer_);
    SDL_RenderCopy(renderer_, texture_, nullptr, nullptr);
    SDL_RenderPresent(renderer_);
  }

  void OnFrame(const webrtc::VideoFrame& frame) override {
    // Record render timestamp for delay measurement
    uint64_t render_ts = GetDelayClock().NowNs();
    uint32_t rtp_ts = frame.rtp_timestamp();

    // WebRTC's own clock -> CLOCK_REALTIME. Receive and decode times are on
    // WebRTC's clock, which is CLOCK_MONOTONIC on Linux but not on macOS,
    // where it stops during sleep: the offset must be taken against it.
    struct timespec ts_rt;
    clock_gettime(CLOCK_REALTIME, &ts_rt);
    const int64_t rt_offset_ns =
        static_cast<int64_t>(ts_rt.tv_sec) * 1000000000LL + ts_rt.tv_nsec -
        webrtc::TimeMicros() * 1000;
    auto to_realtime = [rt_offset_ns](webrtc::Timestamp t) {
      return static_cast<uint64_t>(t.us() * 1000 + rt_offset_ns);
    };

    // The frame's stamps, from its packets' infos (the sender's via the
    // mec-cast header extension). Each packet carries its own send time: the
    // frame's are its LAST packet's -- send and arrival both -- as the relay
    // records them, so the two sites agree on a frame's send_ns.
    const auto& pkt_infos = frame.packet_infos();
    uint64_t send_ts_ns = 0;
    uint64_t capture_ts_ns = 0;
    uint64_t receive_ts_ns = 0;
    for (const auto& info : pkt_infos) {
      if (info.send_timestamp_ns().has_value()) {
        send_ts_ns = std::max<uint64_t>(send_ts_ns, *info.send_timestamp_ns());
      }
      if (info.capture_timestamp_ns().has_value()) {
        capture_ts_ns = *info.capture_timestamp_ns();
      }
      if (info.receive_time().IsFinite() && info.receive_time().us() > 0) {
        receive_ts_ns = std::max(receive_ts_ns, to_realtime(info.receive_time()));
      }
    }

    uint64_t frame_id = static_cast<uint64_t>(rtp_ts);

    // Record capture (from sender's extension data)
    if (capture_ts_ns > 0) {
      GetDelayMeasurement().RecordCapture(frame_id, capture_ts_ns);
    }

    // Record RTP send (from sender's extension data)
    if (send_ts_ns > 0) {
      GetDelayMeasurement().RecordRtpSend(frame_id, send_ts_ns);
    }

    // Record RTP receive
    if (send_ts_ns > 0 && receive_ts_ns > 0) {
      GetDelayMeasurement().RecordRtpRecv(frame_id, send_ts_ns, receive_ts_ns);
    }

    // Extract decode time from processing_time (set by generic_decoder)
    if (frame.processing_time().has_value()) {
      auto pt = frame.processing_time().value();
      GetDelayMeasurement().RecordDecodeStart(frame_id, to_realtime(pt.start));
      GetDelayMeasurement().RecordDecodeEnd(frame_id, to_realtime(pt.finish));
    }

    // Extract encode duration from VideoTimingExtension (set by generic_decoder)
    if (frame.encode_duration_ms().has_value()) {
      int64_t enc_ms = *frame.encode_duration_ms();
      if (enc_ms > 0 && capture_ts_ns > 0) {
        // Approximate encode start/end relative to capture_ns
        // encode_start ≈ capture_ns (frame immediately enters encoder)
        // encode_end ≈ capture_ns + encode_duration
        uint64_t enc_start = capture_ts_ns;
        uint64_t enc_end = capture_ts_ns + static_cast<uint64_t>(enc_ms) * 1000000ULL;
        GetDelayMeasurement().RecordEncodeStart(frame_id, enc_start);
        GetDelayMeasurement().RecordEncodeEnd(frame_id, enc_end);
      }
    }

    // Record render
    GetDelayMeasurement().RecordRender(frame_id, render_ts);

    // Same frame into the shared telemetry spine. The four envelope stamps
    // map directly onto what this callback already has; process_done_ns is
    // render time, which makes e2e here the glass-to-glass metric.
    if (MctRecorder* t = Telemetry()) {
      int64_t decode_ns = 0;
      if (frame.processing_time().has_value()) {
        decode_ns = frame.processing_time()->Elapsed().ns();
      }
      mct_record(t, MCT_MODALITY_VIDEO, frame_id,
                 static_cast<int64_t>(capture_ts_ns),
                 static_cast<int64_t>(send_ts_ns),
                 static_cast<int64_t>(receive_ts_ns),
                 static_cast<int64_t>(render_ts),
                 static_cast<uint32_t>(frame.width() * frame.height() * 3 / 2),
                 decode_ns, kSiteReceiver);
    }

    if (!show_) return;
    auto buffer = frame.video_frame_buffer()->ToI420();
    std::lock_guard<std::mutex> lock(buffer_mutex_);
    latest_ = buffer;
  }

 private:
  bool Open(int width, int height) {
    if (SDL_WasInit(SDL_INIT_VIDEO) == 0 && SDL_InitSubSystem(SDL_INIT_VIDEO) != 0) {
      DEMO_LOG("WARNING: no display (%s): measuring without a window", SDL_GetError());
      show_ = false;
      return false;
    }
    window_ = SDL_CreateWindow("mec-cast WebRTC", SDL_WINDOWPOS_UNDEFINED,
                               SDL_WINDOWPOS_UNDEFINED, width, height,
                               SDL_WINDOW_RESIZABLE);
    if (window_) renderer_ = SDL_CreateRenderer(window_, -1, 0);
    if (!window_ || !renderer_) {
      DEMO_LOG("WARNING: cannot open a window (%s): measuring without one",
               SDL_GetError());
      show_ = false;
      return false;
    }
    DEMO_LOG("VideoRenderer: window %dx%d", width, height);
    return true;
  }

  std::atomic<bool> show_;
  std::mutex buffer_mutex_;
  webrtc::scoped_refptr<webrtc::I420BufferInterface> latest_;
  SDL_Window* window_ = nullptr;
  SDL_Renderer* renderer_ = nullptr;
  SDL_Texture* texture_ = nullptr;
  int tex_width_ = 0, tex_height_ = 0;
};

static std::string SdpToJson(const webrtc::SessionDescriptionInterface* desc) {
  std::string sdp_str;
  desc->ToString(&sdp_str);
  std::string type_str = webrtc::SdpTypeToString(desc->GetType());
  return "{\"type\":\"" + type_str + "\",\"sdp\":\"" + JsonEscape(sdp_str) + "\"}";
}

// The actual peer implementation
struct WebrtcPeer : public webrtc::PeerConnectionObserver {
  webrtc::scoped_refptr<webrtc::PeerConnectionFactoryInterface> factory;
  webrtc::scoped_refptr<webrtc::PeerConnectionInterface> pc;
  std::unique_ptr<webrtc::Thread> signaling_thread;
  std::mutex events_mutex;
  std::queue<InternalEvent> events;
  int send_audio = 1;
  int send_video = 1;
  VideoConfig video;

  // Video state. The renderer is set on the signaling thread (OnTrack) and
  // pumped from the app's main thread, hence the mutex.
  webrtc::scoped_refptr<CapturerTrackSource> video_source;
  std::mutex renderer_mutex;
  std::unique_ptr<VideoRenderer> video_renderer;

  // Logging
  std::unique_ptr<FileLogSink> log_sink;

  void PushEvent(const std::string& type, const std::string& data) {
    std::lock_guard<std::mutex> lock(events_mutex);
    events.push({type, data});
  }

  // PeerConnectionObserver
  void OnSignalingChange(webrtc::PeerConnectionInterface::SignalingState s) override {
    DEMO_LOG("OnSignalingChange: %d", static_cast<int>(s));
  }
  void OnIceGatheringChange(webrtc::PeerConnectionInterface::IceGatheringState s) override {
    DEMO_LOG("OnIceGatheringChange: %d", static_cast<int>(s));
    if (s == webrtc::PeerConnectionInterface::kIceGatheringComplete)
      PushEvent("ice_gathering_complete", "{}");
  }
  void OnIceCandidate(const webrtc::IceCandidateInterface* candidate) override {
    std::string sdp;
    candidate->ToString(&sdp);
    DEMO_LOG("OnIceCandidate: mid=%s idx=%d", candidate->sdp_mid().c_str(),
             candidate->sdp_mline_index());
    std::ostringstream oss;
    oss << "{\"sdpMid\":\"" << candidate->sdp_mid()
        << "\",\"sdpMLineIndex\":" << candidate->sdp_mline_index()
        << ",\"candidate\":\"" << JsonEscape(sdp) << "\"}";
    PushEvent("ice_candidate", oss.str());
  }
  void OnIceConnectionChange(webrtc::PeerConnectionInterface::IceConnectionState s) override {
    const char* names[] = {"new","checking","connected","completed","failed","disconnected","closed","max"};
    int idx = static_cast<int>(s);
    if (idx < 0 || idx > 7) idx = 7;
    DEMO_LOG("OnIceConnectionChange: %s", names[idx]);
    PushEvent("ice_connection_state", std::string("{\"state\":\"") + names[idx] + "\"}");
  }
  void OnConnectionChange(webrtc::PeerConnectionInterface::PeerConnectionState s) override {
    const char* names[] = {"new","connecting","connected","disconnected","failed","closed"};
    int idx = static_cast<int>(s);
    if (idx < 0 || idx > 5) idx = 0;
    DEMO_LOG("OnConnectionChange: %s", names[idx]);
    PushEvent("connection_state", std::string("{\"state\":\"") + names[idx] + "\"}");
  }
  void OnDataChannel(webrtc::scoped_refptr<webrtc::DataChannelInterface>) override {
    DEMO_LOG("OnDataChannel");
  }
  void OnAddStream(webrtc::scoped_refptr<webrtc::MediaStreamInterface> stream) override {
    DEMO_LOG("OnAddStream: id=%s audio=%zu video=%zu", stream->id().c_str(),
             stream->GetAudioTracks().size(), stream->GetVideoTracks().size());
  }
  void OnRemoveStream(webrtc::scoped_refptr<webrtc::MediaStreamInterface>) override {
    DEMO_LOG("OnRemoveStream");
  }
  void OnRenegotiationNeeded() override {
    DEMO_LOG("OnRenegotiationNeeded");
  }
  void OnTrack(webrtc::scoped_refptr<webrtc::RtpTransceiverInterface> transceiver) override {
    auto track = transceiver->receiver()->track();
    DEMO_LOG("OnTrack: kind=%s enabled=%d state=%d",
             track ? track->kind().c_str() : "null",
             track ? track->enabled() : -1,
             track ? static_cast<int>(track->state()) : -1);
    if (track && track->kind() == webrtc::MediaStreamTrackInterface::kAudioKind) {
      track->set_enabled(true);
      DEMO_LOG("OnTrack: enabled remote audio track");
      PushEvent("remote_audio_track", "{\"state\":\"added\"}");
    }
    if (track && track->kind() == webrtc::MediaStreamTrackInterface::kVideoKind) {
      track->set_enabled(true);
      DEMO_LOG("OnTrack: enabled remote video track");
      auto* video_track = static_cast<webrtc::VideoTrackInterface*>(track.get());
      {
        std::lock_guard<std::mutex> lock(renderer_mutex);
        video_renderer = std::make_unique<VideoRenderer>(video.render);
        video_track->AddOrUpdateSink(video_renderer.get(), webrtc::VideoSinkWants());
      }
      DEMO_LOG("VideoRenderer attached to remote video track");
      PushEvent("remote_video_track", "{\"state\":\"added\"}");
    }
  }
  void OnAddTrack(
      webrtc::scoped_refptr<webrtc::RtpReceiverInterface> receiver,
      const std::vector<webrtc::scoped_refptr<webrtc::MediaStreamInterface>>& streams) override {
    auto track = receiver->track();
    DEMO_LOG("OnAddTrack: kind=%s streams=%zu",
             track ? track->kind().c_str() : "null", streams.size());
    if (track && track->kind() == webrtc::MediaStreamTrackInterface::kAudioKind) {
      track->set_enabled(true);
    }
  }
};

// --- C API implementation ---

extern "C" {

WebrtcPeer* webrtc_create(int send_audio, int send_video, const char* username) {
  // A camera at 640x480 @ 30 fps and a window: the behaviour before
  // webrtc_create_ex existed.
  return webrtc_create_ex(send_audio, send_video, username, /*synthetic=*/0,
                          640, 480, 30, 42, /*render=*/1, /*relay=*/0);
}

WebrtcPeer* webrtc_create_ex(int send_audio, int send_video, const char* username,
                             int synthetic, int width, int height, int fps,
                             unsigned int seed, int render, int relay) {
  DEMO_LOG("webrtc_create(send_audio=%d, send_video=%d, user=%s, video=%s %dx%d@%d, render=%d, relay=%d) starting...",
           send_audio, send_video, username ? username : "(null)",
           synthetic ? "synthetic" : "camera", width, height, fps, render, relay);

  // Initialize delay measurement infrastructure (once)
  static bool delay_initialized = false;
  if (!delay_initialized) {
    ClockConfig clk_cfg;
    clk_cfg.source = ClockSource::kAuto;
    InitDelayClock(clk_cfg);
    InitPtpMonitor(clk_cfg.phc_device);
    GetPtpMonitor().Start(1000);
    InitDelayMeasurement(DelayMeasurementConfig());
    delay_initialized = true;
    DEMO_LOG("Delay measurement initialized (clock=%d, PTP=%s)",
             static_cast<int>(GetDelayClock().ActiveSource()),
             GetDelayClock().HasPTP() ? "yes" : "no");
  }

  auto* peer = new WebrtcPeer();
  peer->send_audio = send_audio;
  peer->send_video = send_video;
  peer->video.synthetic = synthetic != 0;
  peer->video.width = width > 0 ? width : 640;
  peer->video.height = height > 0 ? height : 480;
  peer->video.fps = fps > 0 ? fps : 30;
  peer->video.seed = seed;
  peer->video.render = render != 0;

  // Set up logging
  mkdir("log", 0755);
  std::string log_path = "log/";
  log_path += (username ? username : "unknown");
  log_path += "_webrtc.log";
  peer->log_sink = std::make_unique<FileLogSink>(log_path);
  webrtc::LogMessage::LogToDebug(webrtc::LS_INFO);
  webrtc::LogMessage::SetLogToStderr(false);
  webrtc::LogMessage::AddLogToStream(peer->log_sink.get(), webrtc::LS_INFO);

  peer->signaling_thread = webrtc::Thread::CreateWithSocketServer();
  peer->signaling_thread->SetName("signaling_thread", nullptr);

  if (!peer->signaling_thread->Start()) {
    DEMO_LOG("ERROR: Failed to start signaling thread");
    delete peer;
    return nullptr;
  }
  DEMO_LOG("Signaling thread started OK");

  peer->factory = webrtc::CreatePeerConnectionFactory(
      nullptr,  // network thread (auto-created)
      nullptr,  // worker thread (auto-created)
      peer->signaling_thread.get(),
      nullptr,  // ADM (auto-created by voice engine)
      webrtc::CreateBuiltinAudioEncoderFactory(),
      webrtc::CreateBuiltinAudioDecoderFactory(),
      std::make_unique<webrtc::VideoEncoderFactoryTemplate<
          webrtc::LibvpxVp8EncoderTemplateAdapter,
          webrtc::LibvpxVp9EncoderTemplateAdapter,
          webrtc::OpenH264EncoderTemplateAdapter,
          webrtc::LibaomAv1EncoderTemplateAdapter>>(),
      std::make_unique<webrtc::VideoDecoderFactoryTemplate<
          webrtc::LibvpxVp8DecoderTemplateAdapter,
          webrtc::LibvpxVp9DecoderTemplateAdapter,
          webrtc::OpenH264DecoderTemplateAdapter,
          webrtc::Dav1dDecoderTemplateAdapter>>(),
      nullptr, nullptr);
  if (!peer->factory) {
    DEMO_LOG("ERROR: CreatePeerConnectionFactory returned null");
    delete peer;
    return nullptr;
  }
  DEMO_LOG("PeerConnectionFactory created OK");

  webrtc::PeerConnectionInterface::RTCConfiguration config;
  config.sdp_semantics = webrtc::SdpSemantics::kUnifiedPlan;
  webrtc::PeerConnectionInterface::IceServer stun;
  stun.uri = "stun:stun.l.google.com:19302";
  config.servers.push_back(stun);

  webrtc::PeerConnectionDependencies deps(peer);
  auto result = peer->factory->CreatePeerConnectionOrError(config, std::move(deps));
  if (!result.ok()) {
    DEMO_LOG("ERROR: CreatePeerConnectionOrError: %s", result.error().message());
    delete peer;
    return nullptr;
  }
  peer->pc = result.MoveValue();
  DEMO_LOG("PeerConnection created OK");

  if (relay && !send_audio) {
    // Through the relay, a client that sends no audio offers receive-only
    // audio: a muted track would still send silent Opus frames, ~50 packets
    // a second on the link being measured.
    webrtc::RtpTransceiverInit init;
    init.direction = webrtc::RtpTransceiverDirection::kRecvOnly;
    auto t = peer->pc->AddTransceiver(webrtc::MediaType::AUDIO, init);
    if (!t.ok()) {
      DEMO_LOG("ERROR: AddTransceiver audio recvonly: %s", t.error().message());
    } else {
      DEMO_LOG("Audio receive only: recvonly transceiver added");
    }
  } else {
    // Peer to peer, both sides add an audio track for proper SDP negotiation.
    webrtc::AudioOptions audio_opts;
    if (!peer->send_audio) {
      audio_opts.echo_cancellation = false;
    }
    auto audio_source = peer->factory->CreateAudioSource(audio_opts);
    if (!audio_source) {
      DEMO_LOG("ERROR: CreateAudioSource returned null");
    } else {
      DEMO_LOG("AudioSource created OK");
    }
    auto audio_track = peer->factory->CreateAudioTrack("audio_label", audio_source.get());
    if (!audio_track) {
      DEMO_LOG("ERROR: CreateAudioTrack returned null");
    } else {
      DEMO_LOG("AudioTrack created OK, enabled=%d", audio_track->enabled());
      if (!peer->send_audio) {
        audio_track->set_enabled(false);
        DEMO_LOG("Audio track muted (receive only)");
      }
    }
    auto add_result = peer->pc->AddTrack(audio_track, {"stream_id"});
    if (!add_result.ok()) {
      DEMO_LOG("ERROR: AddTrack audio: %s", add_result.error().message());
    } else {
      DEMO_LOG("AddTrack audio OK");
    }
  }

  // Video track — only added if send_video is enabled
  if (peer->send_video) {
    peer->video_source = CapturerTrackSource::Create(peer->video);
    if (peer->video_source) {
      auto video_track = peer->factory->CreateVideoTrack(
          peer->video_source, "video_label");
      if (video_track) {
        DEMO_LOG("VideoTrack created OK");
        auto vr = peer->pc->AddTrack(video_track, {"stream_id"});
        if (!vr.ok()) {
          DEMO_LOG("ERROR: AddTrack video: %s", vr.error().message());
        } else {
          DEMO_LOG("AddTrack video OK");
        }
      }
    } else {
      DEMO_LOG("WARNING: No video capture device, skipping video");
    }
  } else {
    // Receive only. A receive-only video m-line is offered anyway, so a
    // receiver that makes the offer -- a WHEP player of the relay, or a P2P
    // caller -- receives video. As the answerer it is simply left unused.
    webrtc::RtpTransceiverInit init;
    init.direction = webrtc::RtpTransceiverDirection::kRecvOnly;
    auto t = peer->pc->AddTransceiver(webrtc::MediaType::VIDEO, init);
    if (!t.ok()) {
      DEMO_LOG("ERROR: AddTransceiver video recvonly: %s", t.error().message());
    } else {
      DEMO_LOG("Video receive only: recvonly transceiver added");
    }
  }

  DEMO_LOG("webrtc_create done");
  return peer;
}

void webrtc_render_pump(WebrtcPeer* peer) {
  if (!peer) return;
  std::lock_guard<std::mutex> lock(peer->renderer_mutex);
  if (peer->video_renderer) peer->video_renderer->Pump();
}

void webrtc_destroy(WebrtcPeer* peer) {
  if (!peer) return;
  DEMO_LOG("webrtc_destroy");
  // The connection first: until it is closed, a frame may still arrive at the
  // renderer.
  if (peer->pc) { peer->pc->Close(); peer->pc = nullptr; }
  {
    std::lock_guard<std::mutex> lock(peer->renderer_mutex);
    peer->video_renderer.reset();
  }
  peer->video_source = nullptr;
  peer->factory = nullptr;
  if (peer->log_sink) {
    webrtc::LogMessage::RemoveLogToStream(peer->log_sink.get());
    peer->log_sink.reset();
  }
  delete peer;
}

char* webrtc_get_audio_info(WebrtcPeer* peer) {
  std::ostringstream oss;
  oss << "{";
  if (peer && peer->pc) {
    auto senders = peer->pc->GetSenders();
    auto receivers = peer->pc->GetReceivers();
    oss << "\"senders\":" << senders.size()
        << ",\"receivers\":" << receivers.size();
    for (size_t i = 0; i < receivers.size(); i++) {
      auto track = receivers[i]->track();
      if (track) {
        oss << ",\"receiver" << i << "_kind\":\"" << track->kind() << "\""
            << ",\"receiver" << i << "_enabled\":" << (track->enabled() ? "true" : "false")
            << ",\"receiver" << i << "_state\":" << static_cast<int>(track->state());
      }
    }
    for (size_t i = 0; i < senders.size(); i++) {
      auto track = senders[i]->track();
      if (track) {
        oss << ",\"sender" << i << "_kind\":\"" << track->kind() << "\""
            << ",\"sender" << i << "_enabled\":" << (track->enabled() ? "true" : "false")
            << ",\"sender" << i << "_state\":" << static_cast<int>(track->state());
      }
    }
  }
  oss << "}";
  return strdup(oss.str().c_str());
}

char* webrtc_get_video_info(WebrtcPeer* peer) {
  std::ostringstream oss;
  oss << "{";
  if (peer) {
    oss << "\"has_video_source\":" << (peer->video_source ? "true" : "false");
    oss << ",\"has_video_renderer\":" << (peer->video_renderer ? "true" : "false");
    if (peer->pc) {
      int video_senders = 0, video_receivers = 0;
      for (auto& s : peer->pc->GetSenders()) {
        auto t = s->track();
        if (t && t->kind() == webrtc::MediaStreamTrackInterface::kVideoKind) {
          oss << ",\"sender_video_enabled\":" << (t->enabled() ? "true" : "false")
              << ",\"sender_video_state\":" << static_cast<int>(t->state());
          video_senders++;
        }
      }
      for (auto& r : peer->pc->GetReceivers()) {
        auto t = r->track();
        if (t && t->kind() == webrtc::MediaStreamTrackInterface::kVideoKind) {
          oss << ",\"receiver_video_enabled\":" << (t->enabled() ? "true" : "false")
              << ",\"receiver_video_state\":" << static_cast<int>(t->state());
          video_receivers++;
        }
      }
      oss << ",\"video_senders\":" << video_senders
          << ",\"video_receivers\":" << video_receivers;
    }
  }
  oss << "}";
  return strdup(oss.str().c_str());
}

int webrtc_create_offer(WebrtcPeer* peer) {
  if (!peer || !peer->pc) return -1;
  DEMO_LOG("webrtc_create_offer");
  auto obs = CreateSdpObserver::Create(
      [peer](webrtc::SessionDescriptionInterface* desc) {
        DEMO_LOG("Offer created successfully");
        peer->PushEvent("offer_created", SdpToJson(desc));
      },
      [peer](webrtc::RTCError err) {
        DEMO_LOG("ERROR: CreateOffer failed: %s", err.message());
        peer->PushEvent("error", std::string("{\"message\":\"CreateOffer: ") + err.message() + "\"}");
      });
  webrtc::PeerConnectionInterface::RTCOfferAnswerOptions opts;
  opts.offer_to_receive_audio = 1;
  opts.offer_to_receive_video = 1;
  peer->pc->CreateOffer(obs.get(), opts);
  return 0;
}

int webrtc_create_answer(WebrtcPeer* peer) {
  if (!peer || !peer->pc) return -1;
  DEMO_LOG("webrtc_create_answer");
  auto obs = CreateSdpObserver::Create(
      [peer](webrtc::SessionDescriptionInterface* desc) {
        DEMO_LOG("Answer created successfully");
        peer->PushEvent("answer_created", SdpToJson(desc));
      },
      [peer](webrtc::RTCError err) {
        DEMO_LOG("ERROR: CreateAnswer failed: %s", err.message());
        peer->PushEvent("error", std::string("{\"message\":\"CreateAnswer: ") + err.message() + "\"}");
      });
  webrtc::PeerConnectionInterface::RTCOfferAnswerOptions opts;
  peer->pc->CreateAnswer(obs.get(), opts);
  return 0;
}

int webrtc_set_local_description(WebrtcPeer* peer, const char* type,
                                 const char* sdp) {
  if (!peer || !peer->pc) return -1;
  DEMO_LOG("webrtc_set_local_description(type=%s)", type);
  std::string type_s(type);
  std::string sdp_s(sdp);
  auto sdp_type = webrtc::SdpTypeFromString(type_s);
  if (!sdp_type) {
    DEMO_LOG("ERROR: invalid SDP type: %s", type);
    return -1;
  }
  webrtc::SdpParseError err;
  auto desc = webrtc::CreateSessionDescription(*sdp_type, sdp_s, &err);
  if (!desc) {
    DEMO_LOG("ERROR: CreateSessionDescription failed: %s", err.description.c_str());
    return -1;
  }
  auto obs = SetSdpObserver::Create(
      [peer]() {
        DEMO_LOG("SetLocalDescription OK");
        peer->PushEvent("local_description_set", "{}");
      },
      [peer](webrtc::RTCError e) {
        DEMO_LOG("ERROR: SetLocalDescription: %s", e.message());
        peer->PushEvent("error", std::string("{\"message\":\"SetLocal: ") + e.message() + "\"}");
      });
  peer->pc->SetLocalDescription(obs.get(), desc.release());
  return 0;
}

int webrtc_set_remote_description(WebrtcPeer* peer, const char* type,
                                  const char* sdp) {
  if (!peer || !peer->pc) return -1;
  DEMO_LOG("webrtc_set_remote_description(type=%s)", type);
  std::string type_s(type);
  std::string sdp_s(sdp);
  auto sdp_type = webrtc::SdpTypeFromString(type_s);
  if (!sdp_type) {
    DEMO_LOG("ERROR: invalid SDP type: %s", type);
    return -1;
  }
  webrtc::SdpParseError err;
  auto desc = webrtc::CreateSessionDescription(*sdp_type, sdp_s, &err);
  if (!desc) {
    DEMO_LOG("ERROR: CreateSessionDescription failed: %s", err.description.c_str());
    return -1;
  }
  auto obs = SetSdpObserver::Create(
      [peer]() {
        DEMO_LOG("SetRemoteDescription OK");
        peer->PushEvent("remote_description_set", "{}");
      },
      [peer](webrtc::RTCError e) {
        DEMO_LOG("ERROR: SetRemoteDescription: %s", e.message());
        peer->PushEvent("error", std::string("{\"message\":\"SetRemote: ") + e.message() + "\"}");
      });
  peer->pc->SetRemoteDescription(obs.get(), desc.release());
  return 0;
}

int webrtc_add_ice_candidate(WebrtcPeer* peer, const char* sdp_mid,
                             int sdp_mline_index, const char* candidate) {
  if (!peer || !peer->pc) return -1;
  webrtc::SdpParseError err;
  std::unique_ptr<webrtc::IceCandidateInterface> c(
      webrtc::CreateIceCandidate(sdp_mid, sdp_mline_index,
                                 std::string(candidate), &err));
  if (!c) {
    DEMO_LOG("ERROR: CreateIceCandidate failed: %s", err.description.c_str());
    return -1;
  }
  peer->pc->AddIceCandidate(c.get());
  return 0;
}

// --- Stats collector callback (blocks until stats are delivered) ---
class BlockingStatsCollector : public webrtc::RTCStatsCollectorCallback {
 public:
  static webrtc::scoped_refptr<BlockingStatsCollector> Create() {
    return webrtc::make_ref_counted<BlockingStatsCollector>();
  }
  void OnStatsDelivered(
      const webrtc::scoped_refptr<const webrtc::RTCStatsReport>& report) override {
    std::lock_guard<std::mutex> lock(mutex_);
    report_ = report;
    ready_ = true;
    cv_.notify_one();
  }
  webrtc::scoped_refptr<const webrtc::RTCStatsReport> WaitForReport(
      int timeout_ms = 2000) {
    std::unique_lock<std::mutex> lock(mutex_);
    cv_.wait_for(lock, std::chrono::milliseconds(timeout_ms),
                 [this] { return ready_; });
    return report_;
  }
 private:
  std::mutex mutex_;
  std::condition_variable cv_;
  bool ready_ = false;
  webrtc::scoped_refptr<const webrtc::RTCStatsReport> report_;
};

char* webrtc_get_stats(WebrtcPeer* peer) {
  if (!peer || !peer->pc) return strdup("{}");

  auto collector = BlockingStatsCollector::Create();
  peer->pc->GetStats(collector.get());
  auto report = collector->WaitForReport();
  if (!report) return strdup("{}");

  // Collect codec map: id -> mime_type
  std::map<std::string, std::string> codecs;
  for (const auto& stat : *report) {
    if (stat.type() == webrtc::RTCCodecStats::kType) {
      const auto& cs = stat.cast_to<webrtc::RTCCodecStats>();
      if (cs.mime_type.has_value()) {
        codecs[stat.id()] = *cs.mime_type;
      }
    }
  }

  // Candidate pair (RTT)
  double rtt_ms = -1;
  for (const auto& stat : *report) {
    if (stat.type() == webrtc::RTCIceCandidatePairStats::kType) {
      const auto& cp = stat.cast_to<webrtc::RTCIceCandidatePairStats>();
      if (cp.nominated.has_value() && *cp.nominated &&
          cp.current_round_trip_time.has_value()) {
        rtt_ms = *cp.current_round_trip_time * 1000.0;
      }
    }
  }

  // Inbound RTP (receive side)
  std::ostringstream oss;
  oss << "{\"rtt_ms\":" << rtt_ms;

  for (const auto& stat : *report) {
    if (stat.type() == webrtc::RTCInboundRtpStreamStats::kType) {
      const auto& in = stat.cast_to<webrtc::RTCInboundRtpStreamStats>();
      std::string kind = in.kind.has_value() ? *in.kind : "";
      std::string prefix = (kind == "video") ? "in_video" : "in_audio";

      if (in.bytes_received.has_value())
        oss << ",\"" << prefix << "_bytes_received\":" << *in.bytes_received;
      if (in.packets_lost.has_value())
        oss << ",\"" << prefix << "_packets_lost\":" << *in.packets_lost;
      if (in.jitter.has_value())
        oss << ",\"" << prefix << "_jitter\":" << *in.jitter;

      if (kind == "video") {
        if (in.frame_width.has_value())
          oss << ",\"in_video_width\":" << *in.frame_width;
        if (in.frame_height.has_value())
          oss << ",\"in_video_height\":" << *in.frame_height;
        if (in.frames_per_second.has_value())
          oss << ",\"in_video_fps\":" << *in.frames_per_second;
      }

      if (in.codec_id.has_value()) {
        auto it = codecs.find(*in.codec_id);
        if (it != codecs.end())
          oss << ",\"" << prefix << "_codec\":\"" << it->second << "\"";
      }
    }
  }

  // Outbound RTP (send side)
  for (const auto& stat : *report) {
    if (stat.type() == webrtc::RTCOutboundRtpStreamStats::kType) {
      const auto& out = stat.cast_to<webrtc::RTCOutboundRtpStreamStats>();
      std::string kind = out.kind.has_value() ? *out.kind : "";
      std::string prefix = (kind == "video") ? "out_video" : "out_audio";

      if (out.bytes_sent.has_value())
        oss << ",\"" << prefix << "_bytes_sent\":" << *out.bytes_sent;

      if (kind == "video") {
        if (out.frame_width.has_value())
          oss << ",\"out_video_width\":" << *out.frame_width;
        if (out.frame_height.has_value())
          oss << ",\"out_video_height\":" << *out.frame_height;
        if (out.frames_per_second.has_value())
          oss << ",\"out_video_fps\":" << *out.frames_per_second;
      }

      if (out.codec_id.has_value()) {
        auto it = codecs.find(*out.codec_id);
        if (it != codecs.end())
          oss << ",\"" << prefix << "_codec\":\"" << it->second << "\"";
      }
    }
  }

  oss << "}";
  return strdup(oss.str().c_str());
}

// --- Stats rows: webrtc.csv (docs/architecture/str0m-profile.md) ---
//
// One row per RTP stream, in the column set the relay writes too. "out" is
// what this peer sends, with what the far end reported about it (RTCP
// receiver reports); "in" is what it receives. Counters are cumulative since
// the stream began: rates are the analysis's to derive.

}  // extern "C"

namespace {

class StatsRow {
 public:
  void Str(const char* k, const std::string& v) {
    Sep();
    oss_ << '"' << k << "\":\"";
    for (char c : v) {
      if (c == '"' || c == '\\') oss_ << '\\';
      if (static_cast<unsigned char>(c) >= 0x20) oss_ << c;
    }
    oss_ << '"';
  }
  template <typename T>
  void Num(const char* k, const std::optional<T>& v, double scale = 1.0) {
    if (!v.has_value()) return;
    Sep();
    oss_ << '"' << k << "\":";
    if (scale == 1.0)
      oss_ << *v;
    else
      oss_ << static_cast<double>(*v) * scale;
  }
  template <typename T>
  void Num(const char* k, T v) {
    Sep();
    oss_ << '"' << k << "\":" << v;
  }
  std::string Json() const { return "{" + oss_.str() + "}"; }

 private:
  void Sep() {
    if (any_) oss_ << ',';
    any_ = true;
  }
  std::ostringstream oss_;
  bool any_ = false;
};

}  // namespace

extern "C" {

char* webrtc_get_stats_rows(WebrtcPeer* peer) {
  if (!peer || !peer->pc) return strdup("{\"rows\":[]}");

  auto collector = BlockingStatsCollector::Create();
  peer->pc->GetStats(collector.get());
  auto report = collector->WaitForReport();
  if (!report) return strdup("{\"rows\":[]}");

  struct timespec ts;
  clock_gettime(CLOCK_REALTIME, &ts);
  uint64_t now_ns =
      static_cast<uint64_t>(ts.tv_sec) * 1000000000ULL + ts.tv_nsec;

  std::map<std::string, std::string> codecs;
  std::map<std::string, const webrtc::RTCRemoteInboundRtpStreamStats*> remote_in;
  std::optional<double> rtt_s, bwe_bps;
  for (const auto& stat : *report) {
    if (stat.type() == webrtc::RTCCodecStats::kType) {
      const auto& cs = stat.cast_to<webrtc::RTCCodecStats>();
      if (cs.mime_type.has_value()) codecs[stat.id()] = *cs.mime_type;
    } else if (stat.type() == webrtc::RTCRemoteInboundRtpStreamStats::kType) {
      const auto& r = stat.cast_to<webrtc::RTCRemoteInboundRtpStreamStats>();
      if (r.local_id.has_value()) remote_in[*r.local_id] = &r;
    } else if (stat.type() == webrtc::RTCIceCandidatePairStats::kType) {
      const auto& cp = stat.cast_to<webrtc::RTCIceCandidatePairStats>();
      if (cp.nominated.value_or(false)) {
        rtt_s = cp.current_round_trip_time;
        bwe_bps = cp.available_outgoing_bitrate;
      }
    }
  }
  auto codec_of = [&](const std::optional<std::string>& id) {
    if (!id.has_value()) return std::string();
    auto it = codecs.find(*id);
    return it == codecs.end() ? std::string() : it->second;
  };

  std::ostringstream rows;
  bool first = true;
  auto emit = [&](const StatsRow& r) {
    rows << (first ? "" : ",") << r.Json();
    first = false;
  };

  for (const auto& stat : *report) {
    if (stat.type() == webrtc::RTCOutboundRtpStreamStats::kType) {
      const auto& o = stat.cast_to<webrtc::RTCOutboundRtpStreamStats>();
      StatsRow r;
      r.Str("direction", "out");
      r.Str("kind", o.kind.value_or(""));
      r.Str("mid", o.mid.value_or(""));
      r.Num("ssrc", o.ssrc);
      r.Str("codec", codec_of(o.codec_id));
      r.Num("packets", o.packets_sent);
      r.Num("bytes", o.bytes_sent);
      r.Num("retransmitted_packets", o.retransmitted_packets_sent);
      auto ri = remote_in.find(stat.id());
      if (ri != remote_in.end()) {
        r.Num("packets_lost", ri->second->packets_lost);
        r.Num("loss_fraction", ri->second->fraction_lost);
        r.Num("jitter_ms", ri->second->jitter, 1000.0);
        r.Num("rtt_ms", ri->second->round_trip_time, 1000.0);
      } else {
        r.Num("rtt_ms", rtt_s, 1000.0);
      }
      r.Num("nacks", o.nack_count);
      r.Num("plis", o.pli_count);
      r.Num("firs", o.fir_count);
      r.Num("frames", o.frames_encoded);
      r.Num("keyframes", o.key_frames_encoded);
      r.Num("fps", o.frames_per_second);
      r.Num("width", o.frame_width);
      r.Num("height", o.frame_height);
      r.Num("bwe_bps", bwe_bps);
      r.Num("target_bps", o.target_bitrate);
      r.Str("quality_limitation", o.quality_limitation_reason.value_or(""));
      emit(r);
    } else if (stat.type() == webrtc::RTCInboundRtpStreamStats::kType) {
      const auto& in = stat.cast_to<webrtc::RTCInboundRtpStreamStats>();
      StatsRow r;
      r.Str("direction", "in");
      r.Str("kind", in.kind.value_or(""));
      r.Str("mid", in.mid.value_or(""));
      r.Num("ssrc", in.ssrc);
      r.Str("codec", codec_of(in.codec_id));
      r.Num("packets", in.packets_received);
      r.Num("bytes", in.bytes_received);
      r.Num("retransmitted_packets", in.retransmitted_packets_received);
      r.Num("packets_lost", in.packets_lost);
      r.Num("jitter_ms", in.jitter, 1000.0);
      r.Num("rtt_ms", rtt_s, 1000.0);
      r.Num("nacks", in.nack_count);
      r.Num("plis", in.pli_count);
      r.Num("firs", in.fir_count);
      r.Num("frames", in.frames_decoded);
      r.Num("keyframes", in.key_frames_decoded);
      r.Num("frames_dropped", in.frames_dropped);
      r.Num("fps", in.frames_per_second);
      r.Num("width", in.frame_width);
      r.Num("height", in.frame_height);
      if (in.jitter_buffer_delay.has_value() &&
          in.jitter_buffer_emitted_count.value_or(0) > 0) {
        r.Num("jitter_buffer_ms", *in.jitter_buffer_delay * 1000.0 /
                                      *in.jitter_buffer_emitted_count);
      }
      r.Num("freezes", in.freeze_count);
      r.Num("freeze_ms", in.total_freezes_duration, 1000.0);
      emit(r);
    }
  }

  std::string out = "{\"ts_ns\":" + std::to_string(now_ns) + ",\"rows\":[" +
                    rows.str() + "]}";
  return strdup(out.c_str());
}

// --- Delay measurement API ---

char* webrtc_get_delay_report(WebrtcPeer* peer) {
  (void)peer;
  std::string json = GetDelayMeasurement().ToJson();
  return strdup(json.c_str());
}

char* webrtc_get_ptp_status(WebrtcPeer* peer) {
  (void)peer;
  std::string json = GetPtpMonitor().ToJson();
  return strdup(json.c_str());
}

int webrtc_configure_delay(WebrtcPeer* peer, const char* config_json) {
  (void)peer;
  DelayMeasurementConfig config;
  std::string js(config_json);

  auto pos = js.find("\"enabled\"");
  if (pos != std::string::npos) {
    config.enabled = (js.find("true", pos) < js.find("false", pos));
  }

  pos = js.find("\"ring_buffer_size\"");
  if (pos != std::string::npos) {
    pos = js.find(":", pos);
    if (pos != std::string::npos) {
      config.ring_buffer_size = atoi(js.c_str() + pos + 1);
    }
  }

  GetDelayMeasurement().Configure(config);
  return 0;
}

void webrtc_reset_delay_stats(WebrtcPeer* peer) {
  (void)peer;
  GetDelayMeasurement().Reset();
}

void webrtc_close(WebrtcPeer* peer) {
  if (peer && peer->pc) {
    DEMO_LOG("webrtc_close");
    peer->pc->Close();
    {
      std::lock_guard<std::mutex> lock(peer->renderer_mutex);
      peer->video_renderer.reset();
    }
    if (peer->video_source) {
      peer->video_source->StopCapture();
    }
  }
}

int webrtc_poll_events(WebrtcPeer* peer, WebrtcEvent** out_events,
                       int* out_count) {
  if (!peer) return -1;
  std::lock_guard<std::mutex> lock(peer->events_mutex);
  int n = static_cast<int>(peer->events.size());
  if (n == 0) {
    *out_events = nullptr;
    *out_count = 0;
    return 0;
  }
  auto* arr = new WebrtcEvent[n];
  for (int i = 0; i < n; i++) {
    auto& e = peer->events.front();
    arr[i].type = strdup(e.type.c_str());
    arr[i].data = strdup(e.data.c_str());
    peer->events.pop();
  }
  *out_events = arr;
  *out_count = n;
  return 0;
}

void webrtc_free_events(WebrtcEvent* events, int count) {
  if (!events) return;
  for (int i = 0; i < count; i++) {
    free((void*)events[i].type);
    free((void*)events[i].data);
  }
  delete[] events;
}

}  // extern "C"
