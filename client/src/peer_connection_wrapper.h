#ifndef PEER_CONNECTION_WRAPPER_H_
#define PEER_CONNECTION_WRAPPER_H_

#include <napi.h>
#include "webrtc_core.h"

class PeerConnectionWrapper
    : public Napi::ObjectWrap<PeerConnectionWrapper> {
 public:
  static Napi::Object Init(Napi::Env env, Napi::Object exports);
  PeerConnectionWrapper(const Napi::CallbackInfo& info);
  ~PeerConnectionWrapper() override;

  Napi::Value CreateOffer(const Napi::CallbackInfo& info);
  Napi::Value CreateAnswer(const Napi::CallbackInfo& info);
  Napi::Value SetLocalDescription(const Napi::CallbackInfo& info);
  Napi::Value SetRemoteDescription(const Napi::CallbackInfo& info);
  Napi::Value AddIceCandidate(const Napi::CallbackInfo& info);
  Napi::Value Close(const Napi::CallbackInfo& info);
  Napi::Value PollEvents(const Napi::CallbackInfo& info);
  Napi::Value GetAudioInfo(const Napi::CallbackInfo& info);
  Napi::Value GetVideoInfo(const Napi::CallbackInfo& info);
  Napi::Value GetStats(const Napi::CallbackInfo& info);

 private:
  WebrtcPeer* peer_ = nullptr;
};

#endif  // PEER_CONNECTION_WRAPPER_H_
