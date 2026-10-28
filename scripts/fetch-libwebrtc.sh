#!/bin/bash
# Download the prebuilt libwebrtc for this machine, as pinned in
# third_party/webrtc.lock, verify it, and unpack it into
# third_party/libwebrtc/libwebrtc-<platform>/.
#
#   bash scripts/fetch-libwebrtc.sh              # this machine's platform
#   bash scripts/fetch-libwebrtc.sh linux-arm64  # another one
#
# Nothing is fetched when the unpacked tree already matches the lock.
set -euo pipefail

cd "$(dirname "$0")/.."
LOCK=third_party/webrtc.lock
DEST=third_party/libwebrtc

value() { sed -n "s/^$1=//p" "$LOCK"; }

platform=${1:-}
if [ -z "$platform" ]; then
  case "$(uname -s)/$(uname -m)" in
    Linux/x86_64)          platform=linux-x64 ;;
    Linux/aarch64)         platform=linux-arm64 ;;
    Darwin/arm64)          platform=macos-arm64 ;;
    *) echo "ERROR: no prebuilt libwebrtc for $(uname -s)/$(uname -m)." >&2
       echo "  Platforms: linux-x64, linux-arm64, macos-arm64 (see $LOCK)." >&2
       exit 2 ;;
  esac
fi

repo=$(value repository)
tag=$(value tag)
sum=$(value "sha256.$platform")
name="libwebrtc-$platform"
stamp="$DEST/$name/.fetched"

if [ -f "$stamp" ] && [ "$(cat "$stamp")" = "$tag $sum" ]; then
  echo "libwebrtc $tag ($platform) already in $DEST/$name"
  exit 0
fi
if [ -z "$sum" ]; then
  echo "ERROR: $LOCK has no sha256.$platform: the release $tag is not recorded yet." >&2
  exit 1
fi

url="https://github.com/$repo/releases/download/$tag/$name.tar.xz"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
echo "==> $url"
curl -fL --retry 3 -o "$tmp/$name.tar.xz" "$url"

got=$(shasum -a 256 "$tmp/$name.tar.xz" | awk '{print $1}')
if [ "$got" != "$sum" ]; then
  echo "ERROR: checksum mismatch for $name.tar.xz" >&2
  echo "  expected $sum" >&2
  echo "  got      $got" >&2
  exit 1
fi

rm -rf "${DEST:?}/$name"
mkdir -p "$DEST"
tar -xJf "$tmp/$name.tar.xz" -C "$DEST"
echo "$tag $sum" > "$stamp"
echo "libwebrtc $tag ($platform) unpacked into $DEST/$name"
