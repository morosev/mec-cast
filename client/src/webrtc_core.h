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
// role: 0 = caller (sends audio+video), 1 = callee (receives audio+video)
// username: used for log file name (log/<username>_webrtc.log)
WebrtcPeer* webrtc_create(int role, const char* username);
void webrtc_destroy(WebrtcPeer* peer);

// Diagnostics — returns a JSON string (caller must free with free())
char* webrtc_get_audio_info(WebrtcPeer* peer);
char* webrtc_get_video_info(WebrtcPeer* peer);

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
