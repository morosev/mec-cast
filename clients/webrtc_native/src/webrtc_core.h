#ifndef WEBRTC_CORE_H_
#define WEBRTC_CORE_H_

// Pure C interface for WebRTC operations.
// This header is safe to include from any C/C++ compiler.

#ifdef __cplusplus
extern "C" {
#endif

typedef struct WebrtcPeer WebrtcPeer;

// Event structure passed back to JS layer
typedef struct {
  const char* type;  // event type string
  const char* data;  // JSON string
} WebrtcEvent;

// Lifecycle
// send_audio: 1 = capture and send microphone audio, 0 = receive only
// send_video: 1 = capture and send camera video, 0 = receive only
// username: used for log file name (log/<username>_webrtc.log)
WebrtcPeer* webrtc_create(int send_audio, int send_video, const char* username);
// As webrtc_create, with the video source and the window chosen:
// synthetic: 1 = deterministic synthetic frames (no camera), 0 = a camera
// width, height, fps: the source's format; seed: the synthetic frames' seed
// render: 1 = show received video in a window, 0 = measure only
// relay: 1 = through the relay (WHIP/WHEP): with send_audio 0, audio is
//        receive-only rather than a muted track, which still sends silence
WebrtcPeer* webrtc_create_ex(int send_audio, int send_video, const char* username,
                             int synthetic, int width, int height, int fps,
                             unsigned int seed, int render, int relay);
void webrtc_destroy(WebrtcPeer* peer);

// Draw the newest received frame. Call it from the app's main thread, about
// every 16 ms: on macOS windows exist only there.
void webrtc_render_pump(WebrtcPeer* peer);

// Diagnostics — returns a JSON string (caller must free with free())
char* webrtc_get_audio_info(WebrtcPeer* peer);
char* webrtc_get_video_info(WebrtcPeer* peer);
char* webrtc_get_stats(WebrtcPeer* peer);
// webrtc.csv rows for every RTP stream, JSON: {"ts_ns":N,"rows":[...]}.
char* webrtc_get_stats_rows(WebrtcPeer* peer);

// Delay measurement — returns JSON string (caller must free with free())
char* webrtc_get_delay_report(WebrtcPeer* peer);
char* webrtc_get_ptp_status(WebrtcPeer* peer);
int webrtc_configure_delay(WebrtcPeer* peer, const char* config_json);
void webrtc_reset_delay_stats(WebrtcPeer* peer);

// Operations
int webrtc_create_offer(WebrtcPeer* peer);
int webrtc_create_answer(WebrtcPeer* peer);
int webrtc_set_local_description(WebrtcPeer* peer, const char* type,
                                 const char* sdp);
int webrtc_set_remote_description(WebrtcPeer* peer, const char* type,
                                  const char* sdp);
int webrtc_add_ice_candidate(WebrtcPeer* peer, const char* sdp_mid,
                             int sdp_mline_index, const char* candidate);
void webrtc_close(WebrtcPeer* peer);

// Event polling - caller must free returned events with webrtc_free_events
int webrtc_poll_events(WebrtcPeer* peer, WebrtcEvent** out_events,
                       int* out_count);
void webrtc_free_events(WebrtcEvent* events, int count);

#ifdef __cplusplus
}
#endif

#endif  // WEBRTC_CORE_H_
