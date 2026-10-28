#!/bin/bash
# Build the WebRTC native addon (build/Release/webrtc_addon.node) on Linux or
# macOS, against the prebuilt libwebrtc pinned in third_party/webrtc.lock.
#
#   bash build.sh                       # fetches libwebrtc if it is missing
#   LIBWEBRTC_DIR=/path bash build.sh   # another tree with the same layout
#
# libwebrtc is built as Chromium builds it, with Chromium's own libc++
# (namespace std::__Cr), and the archive carries that clang and libc++. So the
# addon compiles in two halves that meet only at the C interface of
# webrtc_core.h, as it always has:
#   - the WebRTC-facing code (webrtc_core.cc and its helpers) with the
#     archive's clang, against Chromium's libc++ and the library's own defines
#     (defines.txt), so its headers see the configuration it was built with;
#   - the Node-facing code (addon.cc, peer_connection_wrapper.cc) with the
#     system compiler, against Node's headers and the system C++ library.
#
# Needs: Node.js 18+ (npm install has run here), SDL2 (apt install libsdl2-dev,
# or brew install sdl2), pkg-config, and cargo for the telemetry staticlib.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$SCRIPT_DIR"

case "$(uname -s)/$(uname -m)" in
  Linux/x86_64)  PLATFORM=linux-x64 ;;
  Linux/aarch64) PLATFORM=linux-arm64 ;;
  Darwin/arm64)  PLATFORM=macos-arm64 ;;
  *) echo "ERROR: unsupported platform $(uname -s)/$(uname -m)" >&2; exit 2 ;;
esac

LIBWEBRTC_DIR=${LIBWEBRTC_DIR:-$REPO_ROOT/third_party/libwebrtc/libwebrtc-$PLATFORM}
if [ ! -f "$LIBWEBRTC_DIR/lib/libwebrtc.a" ]; then
  bash "$REPO_ROOT/scripts/fetch-libwebrtc.sh" "$PLATFORM"
fi
for f in lib/libwebrtc.a lib/libc++.a defines.txt toolchain/bin/clang++ \
         include/api/peer_connection_interface.h; do
  [ -e "$LIBWEBRTC_DIR/$f" ] || { echo "ERROR: $LIBWEBRTC_DIR/$f missing" >&2; exit 1; }
done

# The Node-facing half: any C++ compiler.
CXX=${CXX:-$(command -v clang++ || command -v g++)}
# The WebRTC-facing half, and the link: the archive's clang.
CRCXX="$LIBWEBRTC_DIR/toolchain/bin/clang++"

# Node and node-addon-api headers.
NODE_VER=$(node -e "console.log(process.version.slice(1))")
NODE_INC=""
for d in "$HOME/.cache/node-gyp/$NODE_VER/include/node" \
         "$HOME/Library/Caches/node-gyp/$NODE_VER/include/node" \
         "$HOME/.node-gyp/$NODE_VER/include/node" \
         "$(dirname "$(dirname "$(command -v node)")")/include/node"; do
  if [ -f "$d/node_api.h" ]; then NODE_INC=$d; break; fi
done
[ -n "$NODE_INC" ] || { echo "ERROR: Node.js headers not found; run: npx node-gyp install" >&2; exit 1; }
NAPI_DIR=$(node -p "require('node-addon-api').include" 2>/dev/null | tr -d '"')
[ -f "$NAPI_DIR/napi.h" ] || NAPI_DIR="$SCRIPT_DIR/node_modules/node-addon-api"
[ -f "$NAPI_DIR/napi.h" ] || { echo "ERROR: napi.h not found; run: npm install" >&2; exit 1; }

SDL_CFLAGS=$(pkg-config --cflags sdl2) || { echo "ERROR: SDL2 not found (libsdl2-dev / brew install sdl2)" >&2; exit 1; }
SDL_LIBS=$(pkg-config --libs sdl2)

# Shared Rust telemetry spine, linked over its C ABI (telemetry/src/ffi.rs).
TELEMETRY_INC="$REPO_ROOT/telemetry/include"
TELEMETRY_LIB="$REPO_ROOT/target/release/libmec_cast_telemetry.a"
if command -v cargo >/dev/null 2>&1; then
  echo "Building mec-cast-telemetry staticlib ..."
  (cd "$REPO_ROOT" && cargo build --release -p mec-cast-telemetry)
fi
[ -f "$TELEMETRY_LIB" ] || { echo "ERROR: $TELEMETRY_LIB missing (install Rust)" >&2; exit 1; }

# The library's own defines, one per line in defines.txt.
WEBRTC_DEFINES=$(grep -v '^[[:space:]]*$' "$LIBWEBRTC_DIR/defines.txt" \
  | sed -E 's/^[[:space:]]*/-D/' | tr '\n' ' ')

INC="$LIBWEBRTC_DIR/include"
# Chromium's libc++ in place of the system's: its headers, the library's
# defines, no exceptions or RTTI, as the library was compiled.
WEBRTC_CFLAGS="-c -O2 -std=c++20 -fPIC -fno-exceptions -fno-rtti -w \
  $WEBRTC_DEFINES \
  -nostdinc++ \
  -isystem $INC/buildtools/third_party/libc++ \
  -isystem $INC/third_party/libc++/src/include \
  -isystem $INC/third_party/libc++abi/src/include \
  -I$INC -I$INC/third_party/abseil-cpp -I$INC/third_party/libyuv/include \
  -I$TELEMETRY_INC -Isrc $SDL_CFLAGS"
ADDON_CFLAGS="-c -O2 -std=c++17 -fPIC -fno-exceptions -w \
  -DNAPI_VERSION=8 -DNAPI_DISABLE_CPP_EXCEPTIONS -DNODE_GYP_MODULE_NAME=webrtc_addon \
  -I$NODE_INC -I$NAPI_DIR -Isrc"

WEBRTC_SOURCES="src/webrtc_core.cc src/delay_clock.cc src/ptp_monitor.cc \
  src/delay_measurement.cc $LIBWEBRTC_DIR/src/test/test_video_capturer.cc"
ADDON_SOURCES="src/addon.cc src/peer_connection_wrapper.cc"

case "$PLATFORM" in
  macos-*)
    SDK=$(xcrun --show-sdk-path)
    TARGET="-target arm64-apple-macos12.0 -isysroot $SDK"
    WEBRTC_CFLAGS="$TARGET $WEBRTC_CFLAGS"
    # Node resolves its own symbols when it loads the bundle.
    LINK="$TARGET -bundle -undefined dynamic_lookup"
    PLATFORM_LIBS="-framework Foundation -framework AVFoundation -framework CoreMedia \
      -framework CoreVideo -framework CoreAudio -framework AudioToolbox \
      -framework VideoToolbox -framework AppKit -framework Metal -framework IOSurface \
      -framework QuartzCore -framework CoreGraphics -framework ApplicationServices \
      -framework Security -framework SystemConfiguration -framework IOKit"
    WEBRTC_LIBS="$LIBWEBRTC_DIR/lib/libwebrtc.a $LIBWEBRTC_DIR/lib/libc++.a"
    ;;
  linux-*)
    # libwebrtc needs only V4L2 for the camera on Linux.
    WEBRTC_SOURCES="$WEBRTC_SOURCES $LIBWEBRTC_DIR/src/test/vcm_capturer.cc"
    # Node's own symbols resolve when it loads the addon.
    LINK="-shared -fuse-ld=lld"
    PLATFORM_LIBS="-lpthread -ldl -lrt"
    WEBRTC_LIBS="-Wl,--whole-archive $LIBWEBRTC_DIR/lib/libwebrtc.a -Wl,--no-whole-archive \
      $LIBWEBRTC_DIR/lib/libc++.a"
    ;;
esac

echo "=== WebRTC native addon ($PLATFORM) ==="
echo "WebRTC half: $CRCXX"
echo "Node half:   $CXX"
echo "libwebrtc:   $LIBWEBRTC_DIR ($(sed -n 's/^commit: //p' "$LIBWEBRTC_DIR/BUILD_INFO" 2>/dev/null | cut -c1-12))"

OBJ="$SCRIPT_DIR/build/obj"
mkdir -p "$OBJ" build/Release
OBJECTS=""
compile() { # compiler flags source
  local obj="$OBJ/$(basename "${3%.cc}").o"
  echo "Compiling $(basename "$3") ..."
  # shellcheck disable=SC2086
  "$1" $2 -o "$obj" "$3"
  OBJECTS="$OBJECTS $obj"
}
for src in $WEBRTC_SOURCES; do compile "$CRCXX" "$WEBRTC_CFLAGS" "$src"; done
for src in $ADDON_SOURCES; do compile "$CXX" "$ADDON_CFLAGS" "$src"; done

echo "Linking webrtc_addon.node ..."
# shellcheck disable=SC2086
"$CRCXX" $LINK -o build/Release/webrtc_addon.node $OBJECTS \
  $WEBRTC_LIBS "$TELEMETRY_LIB" $SDL_LIBS $PLATFORM_LIBS

echo "=== Build successful: build/Release/webrtc_addon.node ==="
