#!/bin/bash
# MEC-Cast Release Script
#
# Creates a tagged release with two binary packages:
#   - mec-cast-vX.Y.Z-linux-x64.zip       (runtime: prebuilt, ready to run)
#   - mec-cast-vX.Y.Z-linux-x64-dev.zip   (dev: adds libwebrtc.a + C++ source)
#
# Usage:
#   ./scripts/release.sh <version> [--dry-run]
#
# Examples:
#   ./scripts/release.sh 1.0.4 "Audio delay measurement, CSV export"
#   ./scripts/release.sh 1.0.4 "Bug fixes" --dry-run
#
# Prerequisites:
#   - Client addon built:   cd client && ./build.sh
#   - WebRTC built:         ninja -C webrtc/src/out/release_x64 webrtc
#   - gh CLI authenticated: gh auth status
#   - Clean git working tree (no uncommitted changes)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# --- Parse arguments ---
if [ $# -lt 2 ]; then
  echo "Usage: $0 <version> <description> [--dry-run]"
  echo "  version:     semantic version without 'v' prefix (e.g., 1.0.4)"
  echo "  description: short release highlights (e.g., \"Audio delay, CSV export\")"
  echo "  --dry-run:   build packages without pushing"
  exit 1
fi

VERSION="$1"
DESCRIPTION="$2"
TAG="v${VERSION}"
DRY_RUN=false
if [ "${3:-}" = "--dry-run" ]; then
  DRY_RUN=true
fi

DATE=$(date +%Y-%m-%d)
PLATFORM="linux-x64"
RUNTIME_PKG="mec-cast-${TAG}-${PLATFORM}"
DEV_PKG="mec-cast-${TAG}-${PLATFORM}-dev"

echo "=== MEC-Cast Release ${TAG} ==="
echo "Date: ${DATE}"
echo "Platform: ${PLATFORM}"
echo "Dry run: ${DRY_RUN}"
echo ""

# --- Verify prerequisites ---
ADDON="${ROOT_DIR}/client/build/Release/webrtc_addon.node"
LIBWEBRTC="${ROOT_DIR}/webrtc/src/out/release_x64/obj/libwebrtc.a"

if [ ! -f "$ADDON" ]; then
  echo "ERROR: webrtc_addon.node not found. Run: cd client && ./build.sh"
  exit 1
fi

if [ ! -f "$LIBWEBRTC" ]; then
  echo "ERROR: libwebrtc.a not found. Run: ninja -C webrtc/src/out/release_x64 webrtc"
  exit 1
fi

if ! gh auth status &>/dev/null; then
  echo "ERROR: gh CLI not authenticated. Run: gh auth login"
  exit 1
fi

cd "$ROOT_DIR"

if [ "$DRY_RUN" = false ] && ! git diff --quiet; then
  echo "ERROR: Uncommitted changes in working tree. Commit or stash first."
  exit 1
fi

echo "[OK] Prerequisites verified"
echo "  Addon: $(du -h "$ADDON" | cut -f1)"
echo "  libwebrtc.a: $(du -h "$LIBWEBRTC" | cut -f1)"
echo ""

# --- Build packages ---
BUILD_DIR=$(mktemp -d)
trap "rm -rf $BUILD_DIR" EXIT

RUNTIME_DIR="${BUILD_DIR}/${RUNTIME_PKG}"
DEV_DIR="${BUILD_DIR}/${DEV_PKG}"

# Runtime package
mkdir -p "$RUNTIME_DIR/client/build/Release" "$RUNTIME_DIR/server" "$RUNTIME_DIR/tests"
cp "$ADDON" "$RUNTIME_DIR/client/build/Release/"
cp client/client.js client/client-config.json client/package.json "$RUNTIME_DIR/client/"
cp server/server.js server/package.json "$RUNTIME_DIR/server/"
cp tests/e2e_local.sh "$RUNTIME_DIR/tests/"
cp LICENSE "$RUNTIME_DIR/"

# Generate INSTALL.md with version and date
cat > "$RUNTIME_DIR/INSTALL.md" << INSTALLEOF
# MEC-Cast ${TAG} — Prebuilt Runtime (Linux x64)

**Version:** ${VERSION}
**Date:** ${DATE}
**Platform:** Linux x86_64

This package contains prebuilt binaries ready to run without compiling WebRTC.

## Requirements

- Linux x86_64 (Ubuntu 22.04+ or equivalent)
- Node.js 18+
- X11 (for video display)
- PulseAudio or ALSA (for audio)

## Quick Start

\`\`\`bash
# Install server dependencies
cd server
npm install

# Install client dependencies
cd ../client
npm install

# Start the server (terminal 1)
cd ../server
node server.js

# Start client A (terminal 2)
cd ../client
node client.js

# Start client B (terminal 3)
cd ../client
node client.js
\`\`\`

## Usage

1. In each client: \`connect <name>\` (e.g., \`connect alice\`, \`connect bob\`)
2. One client: \`call\` to initiate
3. Other client: \`answer\` to accept
4. Either client: \`delay on\` for delay measurements, \`stats on\` for WebRTC stats
5. \`end\` to hang up, \`quit\` to exit

## E2E Test

\`\`\`bash
./tests/e2e_local.sh
\`\`\`

Runs an automated local test (server + 2 clients + call + delay report).

## Prebuilt Binary

\`client/build/Release/webrtc_addon.node\` is compiled for:
- Linux x86_64
- Node.js 18.x (N-API version 8)
- Linked against libc++ (bundled in the .node file)

If you need a different Node.js version or architecture, rebuild from source
(see the dev package or the main repository).

## Source Repository

https://github.com/morosev/mec-cast
INSTALLEOF

# Dev package (runtime + extras)
cp -r "$RUNTIME_DIR" "$DEV_DIR"
cp "$LIBWEBRTC" "$DEV_DIR/libwebrtc.a"
mkdir -p "$DEV_DIR/client/src"
cp client/build.sh "$DEV_DIR/client/"
cp client/src/*.cc client/src/*.h "$DEV_DIR/client/src/"

# Generate INSTALL-DEV.md with version and date
cat > "$DEV_DIR/INSTALL-DEV.md" << DEVEOF
# MEC-Cast ${TAG} — Developer Package (Linux x64)

**Version:** ${VERSION}
**Date:** ${DATE}
**Platform:** Linux x86_64

This package includes \`libwebrtc.a\` so you can rebuild the native addon
without compiling the full WebRTC source tree (~20 GB, 30+ min build).

## Rebuilding the addon

You need Chromium's clang++ toolchain. Get it via depot_tools:

\`\`\`bash
git clone https://chromium.googlesource.com/chromium/tools/depot_tools.git
export PATH="\$PWD/depot_tools:\$PATH"
\`\`\`

Then place \`libwebrtc.a\` where build.sh expects it:

\`\`\`bash
mkdir -p webrtc/src/out/release_x64/obj
cp libwebrtc.a webrtc/src/out/release_x64/obj/
\`\`\`

Ensure the WebRTC source headers are available at \`webrtc/src/\` (clone the
submodule from the main repository). Then rebuild:

\`\`\`bash
cd client
chmod +x build.sh
./build.sh
\`\`\`

## Source files included

\`client/src/\` contains all C++ source and headers for the native addon:
- \`webrtc_core.cc/h\` — PeerConnection, video capture, X11 renderer, delay hooks
- \`delay_measurement.cc/h\` — Per-frame delay tracking and statistics
- \`delay_clock.cc/h\` — Clock abstraction (PHC / CLOCK_REALTIME)
- \`ptp_monitor.cc/h\` — PTP synchronization quality monitor
- \`peer_connection_wrapper.cc/h\` — N-API wrapper
- \`addon.cc\` — Node.js addon entry point

## Source Repository

https://github.com/morosev/mec-cast (tag: ${TAG})
DEVEOF

echo "[OK] Packages built"

# --- Create zip files ---
cd "$BUILD_DIR"
zip -qr "${RUNTIME_PKG}.zip" "${RUNTIME_PKG}/"
zip -qr "${DEV_PKG}.zip" "${DEV_PKG}/"

echo "  ${RUNTIME_PKG}.zip: $(du -h "${RUNTIME_PKG}.zip" | cut -f1)"
echo "  ${DEV_PKG}.zip: $(du -h "${DEV_PKG}.zip" | cut -f1)"
echo ""

if [ "$DRY_RUN" = true ]; then
  echo "[DRY RUN] Skipping tag, release, and upload."
  echo "Packages are at: ${BUILD_DIR}/"
  # Don't clean up in dry-run mode
  trap - EXIT
  exit 0
fi

# --- Tag and release ---
cd "$ROOT_DIR"

# Update version history in RELEASING.md
echo "Updating RELEASING.md..."
# Insert new version row after the table separator line (|---|---|---|)
sed -i "/^|---------|------|------------|$/a | ${TAG} | ${DATE} | ${DESCRIPTION} |" RELEASING.md

git add RELEASING.md
git commit -m "Release ${TAG}: ${DESCRIPTION}"
git push origin main

echo "Creating tag ${TAG}..."
git tag -a "$TAG" -m "${TAG}: ${DESCRIPTION}"
git push origin "$TAG"

echo "Creating GitHub release..."
gh release create "$TAG" \
  --title "${TAG}" \
  --notes "## ${TAG} (${DATE})

${DESCRIPTION}"

echo "Uploading packages..."
gh release upload "$TAG" \
  "${BUILD_DIR}/${RUNTIME_PKG}.zip" \
  "${BUILD_DIR}/${DEV_PKG}.zip"

echo ""
echo "=== Release ${TAG} complete ==="
echo "https://github.com/morosev/mec-cast/releases/tag/${TAG}"
