#include <napi.h>
#include "peer_connection_wrapper.h"

Napi::Object InitAll(Napi::Env env, Napi::Object exports) {
  return PeerConnectionWrapper::Init(env, exports);
}

NODE_API_MODULE(webrtc_addon, InitAll)
