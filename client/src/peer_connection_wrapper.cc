// NAPI wrapper — only uses the C interface from webrtc_core.h.
// No WebRTC C++ headers are included here.

#include "peer_connection_wrapper.h"

Napi::Object PeerConnectionWrapper::Init(Napi::Env env,
                                         Napi::Object exports) {
  Napi::Function func = DefineClass(
      env, "PeerConnection",
      {
          InstanceMethod("createOffer", &PeerConnectionWrapper::CreateOffer),
          InstanceMethod("createAnswer", &PeerConnectionWrapper::CreateAnswer),
          InstanceMethod("setLocalDescription",
                         &PeerConnectionWrapper::SetLocalDescription),
          InstanceMethod("setRemoteDescription",
                         &PeerConnectionWrapper::SetRemoteDescription),
          InstanceMethod("addIceCandidate",
                         &PeerConnectionWrapper::AddIceCandidate),
          InstanceMethod("close", &PeerConnectionWrapper::Close),
          InstanceMethod("pollEvents", &PeerConnectionWrapper::PollEvents),
          InstanceMethod("getAudioInfo", &PeerConnectionWrapper::GetAudioInfo),
          InstanceMethod("getVideoInfo", &PeerConnectionWrapper::GetVideoInfo),
          InstanceMethod("getStats", &PeerConnectionWrapper::GetStats),
      });
  exports.Set("PeerConnection", func);
  return exports;
}

PeerConnectionWrapper::PeerConnectionWrapper(const Napi::CallbackInfo& info)
    : Napi::ObjectWrap<PeerConnectionWrapper>(info) {
  int role = 0;  // default: caller (sends audio+video)
  std::string username = "unknown";
  if (info.Length() > 0 && info[0].IsNumber()) {
    role = info[0].As<Napi::Number>().Int32Value();
  }
  if (info.Length() > 1 && info[1].IsString()) {
    username = info[1].As<Napi::String>().Utf8Value();
  }
  peer_ = webrtc_create(role, username.c_str());
  if (!peer_) {
    Napi::Error::New(info.Env(), "Failed to initialize PeerConnection")
        .ThrowAsJavaScriptException();
  }
}

PeerConnectionWrapper::~PeerConnectionWrapper() {
  if (peer_) {
    webrtc_destroy(peer_);
    peer_ = nullptr;
  }
}

Napi::Value PeerConnectionWrapper::CreateOffer(const Napi::CallbackInfo& info) {
  if (peer_) webrtc_create_offer(peer_);
  return info.Env().Undefined();
}

Napi::Value PeerConnectionWrapper::CreateAnswer(const Napi::CallbackInfo& info) {
  if (peer_) webrtc_create_answer(peer_);
  return info.Env().Undefined();
}

Napi::Value PeerConnectionWrapper::SetLocalDescription(
    const Napi::CallbackInfo& info) {
  Napi::Env env = info.Env();
  if (info.Length() < 2 || !info[0].IsString() || !info[1].IsString()) {
    Napi::TypeError::New(env, "Expected (type, sdp)")
        .ThrowAsJavaScriptException();
    return env.Undefined();
  }
  std::string type = info[0].As<Napi::String>().Utf8Value();
  std::string sdp = info[1].As<Napi::String>().Utf8Value();
  if (peer_) webrtc_set_local_description(peer_, type.c_str(), sdp.c_str());
  return env.Undefined();
}

Napi::Value PeerConnectionWrapper::SetRemoteDescription(
    const Napi::CallbackInfo& info) {
  Napi::Env env = info.Env();
  if (info.Length() < 2 || !info[0].IsString() || !info[1].IsString()) {
    Napi::TypeError::New(env, "Expected (type, sdp)")
        .ThrowAsJavaScriptException();
    return env.Undefined();
  }
  std::string type = info[0].As<Napi::String>().Utf8Value();
  std::string sdp = info[1].As<Napi::String>().Utf8Value();
  if (peer_) webrtc_set_remote_description(peer_, type.c_str(), sdp.c_str());
  return env.Undefined();
}

Napi::Value PeerConnectionWrapper::AddIceCandidate(
    const Napi::CallbackInfo& info) {
  Napi::Env env = info.Env();
  if (info.Length() < 3 || !info[0].IsString() || !info[1].IsNumber() ||
      !info[2].IsString()) {
    Napi::TypeError::New(env, "Expected (sdpMid, sdpMLineIndex, candidate)")
        .ThrowAsJavaScriptException();
    return env.Undefined();
  }
  std::string mid = info[0].As<Napi::String>().Utf8Value();
  int idx = info[1].As<Napi::Number>().Int32Value();
  std::string cand = info[2].As<Napi::String>().Utf8Value();
  if (peer_) webrtc_add_ice_candidate(peer_, mid.c_str(), idx, cand.c_str());
  return env.Undefined();
}

Napi::Value PeerConnectionWrapper::Close(const Napi::CallbackInfo& info) {
  if (peer_) webrtc_close(peer_);
  return info.Env().Undefined();
}

Napi::Value PeerConnectionWrapper::PollEvents(const Napi::CallbackInfo& info) {
  Napi::Env env = info.Env();
  WebrtcEvent* events = nullptr;
  int count = 0;
  if (peer_) webrtc_poll_events(peer_, &events, &count);

  Napi::Array result = Napi::Array::New(env, count);
  for (int i = 0; i < count; i++) {
    Napi::Object obj = Napi::Object::New(env);
    obj.Set("type", Napi::String::New(env, events[i].type));
    obj.Set("data", Napi::String::New(env, events[i].data));
    result.Set(static_cast<uint32_t>(i), obj);
  }
  if (events) webrtc_free_events(events, count);
  return result;
}

Napi::Value PeerConnectionWrapper::GetAudioInfo(const Napi::CallbackInfo& info) {
  Napi::Env env = info.Env();
  if (!peer_) return Napi::String::New(env, "{}");
  char* json = webrtc_get_audio_info(peer_);
  Napi::String result = Napi::String::New(env, json ? json : "{}");
  if (json) free(json);
  return result;
}

Napi::Value PeerConnectionWrapper::GetVideoInfo(const Napi::CallbackInfo& info) {
  Napi::Env env = info.Env();
  if (!peer_) return Napi::String::New(env, "{}");
  char* json = webrtc_get_video_info(peer_);
  Napi::String result = Napi::String::New(env, json ? json : "{}");
  if (json) free(json);
  return result;
}

Napi::Value PeerConnectionWrapper::GetStats(const Napi::CallbackInfo& info) {
  Napi::Env env = info.Env();
  if (!peer_) return Napi::String::New(env, "{}");
  char* json = webrtc_get_stats(peer_);
  Napi::String result = Napi::String::New(env, json ? json : "{}");
  if (json) free(json);
  return result;
}
