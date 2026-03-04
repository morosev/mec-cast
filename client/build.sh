#!/bin/bash
set -e

# Build script for the WebRTC native addon on Linux using Chromium's clang.
# Must be run from the client/ directory.
# The WebRTC source tree is expected at ../../webrtc/src relative to this script.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WEBRTC_SRC="$SCRIPT_DIR/../webrtc/src"

if [ ! -d "$WEBRTC_SRC" ]; then
  echo "ERROR: WebRTC source not found at $WEBRTC_SRC"
  echo "Expected directory layout:"
  echo "  mec-cast/"
  echo "    webrtc/src/   (WebRTC source tree)"
  echo "    client/       (this project)"
  exit 1
fi

CLANG="$WEBRTC_SRC/third_party/llvm-build/Release+Asserts/bin/clang++"

if [ ! -f "$CLANG" ]; then
  echo "ERROR: clang++ not found at $CLANG"
  exit 1
fi

# Get Node.js include and lib paths
NODE_VER=$(node -e "console.log(process.version.slice(1))")
NODE_GYP_DIR="$HOME/.cache/node-gyp/$NODE_VER"
if [ ! -d "$NODE_GYP_DIR" ]; then
  NODE_GYP_DIR="$HOME/.node-gyp/$NODE_VER"
fi
NODE_INC="$NODE_GYP_DIR/include/node"

# Find node-addon-api include
NAPI_DIR=$(node -e "console.log(require('node-addon-api').include)" 2>/dev/null | tr -d '"')
if [ -z "$NAPI_DIR" ] || [ ! -f "$NAPI_DIR/napi.h" ]; then
  NAPI_DIR="$SCRIPT_DIR/node_modules/node-addon-api"
fi

if [ ! -f "$NODE_INC/node_api.h" ]; then
  echo "ERROR: Node.js headers not found at $NODE_INC"
  echo "Run: node-gyp install"
  exit 1
fi

if [ ! -f "$NAPI_DIR/napi.h" ]; then
  echo "ERROR: napi.h not found at $NAPI_DIR"
  echo "Run: npm install"
  exit 1
fi

echo "=== WebRTC Native Addon Build (Linux) ==="
echo "Clang++:      $CLANG"
echo "WebRTC src:   $WEBRTC_SRC"
echo "Node include: $NODE_INC"
echo "NAPI include: $NAPI_DIR"
echo ""

mkdir -p build/Release

# Common flags for clang++ (libc++ ABI matching libwebrtc.a)
WEBRTC_CFLAGS="-c -O2 -std=c++20 -fPIC -fno-exceptions -fno-rtti -Wno-everything \
  -DNDEBUG \
  -D__STDC_CONSTANT_MACROS \
  -D__STDC_FORMAT_MACROS \
  -D_FILE_OFFSET_BITS=64 \
  -D_LARGEFILE_SOURCE \
  -D_LARGEFILE64_SOURCE \
  -D_GNU_SOURCE \
  -DWEBRTC_POSIX \
  -DWEBRTC_LINUX \
  -DWEBRTC_USE_H264 \
  -DWEBRTC_ENABLE_PROTOBUF=1 \
  -DWEBRTC_INCLUDE_INTERNAL_AUDIO_DEVICE \
  -DRTC_ENABLE_VP9 \
  -DRTC_DAV1D_IN_INTERNAL_DECODER_FACTORY \
  -DWEBRTC_HAVE_SCTP \
  -DWEBRTC_LIBRARY_IMPL \
  -DWEBRTC_NON_STATIC_TRACE_EVENT_HANDLERS=0 \
  -DABSL_ALLOCATOR_NOTHROW=1 \
  -DUSE_UDEV \
  -DUSE_AURA=1 \
  -DUSE_GLIB=1 \
  -DUSE_OZONE=1 \
  -DWEBRTC_USE_PIPEWIRE \
  -DWEBRTC_USE_GIO \
  -DHAVE_PTHREAD \
  -D_LIBCPP_HARDENING_MODE=_LIBCPP_HARDENING_MODE_EXTENSIVE \
  -D_LIBCPP_DISABLE_VISIBILITY_ANNOTATIONS \
  -D_LIBCXXABI_DISABLE_VISIBILITY_ANNOTATIONS \
  -D_LIBCPP_INSTRUMENTED_WITH_ASAN=0 \
  -DCR_LIBCXX_REVISION=7ab65651aed6802d2599dcb7a73b1f82d5179d05 \
  -nostdinc++ \
  -isystem $WEBRTC_SRC/buildtools/third_party/libc++ \
  -isystem $WEBRTC_SRC/third_party/libc++/src/include \
  -isystem $WEBRTC_SRC/third_party/libc++abi/src/include \
  -I$WEBRTC_SRC \
  -I$WEBRTC_SRC/third_party/abseil-cpp \
  -I$WEBRTC_SRC/third_party/libyuv/include"

# Flags for addon/wrapper files (Node.js N-API layer)
ADDON_CFLAGS="-c -O2 -std=c++20 -fPIC -fno-exceptions -fno-rtti \
  -DNAPI_VERSION=8 \
  -DNAPI_DISABLE_CPP_EXCEPTIONS \
  -DWEBRTC_POSIX \
  -DWEBRTC_LINUX \
  -DWEBRTC_USE_H264 \
  -DNODE_GYP_MODULE_NAME=webrtc_addon \
  -DUSING_UV_SHARED=1 \
  -DUSING_V8_SHARED=1 \
  -DV8_DEPRECATION_WARNINGS=1 \
  -I$NODE_INC \
  -I$NAPI_DIR \
  -Isrc"

echo "Compiling webrtc_core.cc ..."
"$CLANG" $WEBRTC_CFLAGS -o src/webrtc_core.o src/webrtc_core.cc

echo "Compiling test_video_capturer.cc ..."
"$CLANG" $WEBRTC_CFLAGS -o src/test_video_capturer.o \
  "$WEBRTC_SRC/test/test_video_capturer.cc"

echo "Compiling vcm_capturer.cc ..."
"$CLANG" $WEBRTC_CFLAGS -o src/vcm_capturer.o \
  "$WEBRTC_SRC/test/vcm_capturer.cc"

echo "Compiling addon.cc ..."
"$CLANG" $ADDON_CFLAGS -o src/addon.o src/addon.cc

echo "Compiling peer_connection_wrapper.cc ..."
"$CLANG" $ADDON_CFLAGS -o src/peer_connection_wrapper.o src/peer_connection_wrapper.cc

echo "Linking webrtc_addon.node ..."
"$CLANG" -shared -fuse-ld=lld -o build/Release/webrtc_addon.node \
  src/webrtc_core.o \
  src/test_video_capturer.o \
  src/vcm_capturer.o \
  src/addon.o \
  src/peer_connection_wrapper.o \
  -Wl,--whole-archive \
  "$WEBRTC_SRC/out/x64_release/obj/libwebrtc.a" \
  -Wl,--no-whole-archive \
  "$WEBRTC_SRC/out/x64_release/obj/buildtools/third_party/libc++/libc++/"*.o \
  -lX11 \
  -lpthread \
  -ldl \
  -lrt \
  -Wl,--no-as-needed

echo ""
echo "=== Build successful! ==="
echo "Output: build/Release/webrtc_addon.node"

# Clean up object files
rm -f src/*.o
