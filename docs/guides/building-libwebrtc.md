# Building the forked libwebrtc

**This is a ~20 GB checkout and a multi-hour build. It is opt-in and never
runs in CI.** You only need it to rebuild the legacy WebRTC native client
(`clients/webrtc_native/`). Nothing else in the platform depends on it.

## What the fork adds

`third_party/webrtc/src` tracks
[morosev/mec-cast-webrtc](https://github.com/morosev/mec-cast-webrtc)
(branch `mec-cast`), which patches upstream WebRTC to:

- add `SendTimestampNsExtension` — a 16-byte RTP header extension carrying
  `capture_ns` + `send_ns`;
- force **every** frame to be a timing frame, so timing metadata is always
  present rather than periodically sampled;
- expose encode duration on decoded frames.

The str0m profile exists partly to make this fork unnecessary: with a
sans-IO stack you own the sockets and can stamp egress/ingress without
patching anything. See [str0m-profile.md](../architecture/str0m-profile.md).

## Build

```bash
cd third_party/webrtc
git clone https://chromium.googlesource.com/chromium/tools/depot_tools.git
export PATH="$PWD/depot_tools:$PATH"
cp .gclient.template .gclient
gclient sync                      # ~20 GB, slow on first run

cd src
sudo ./build/install-build-deps.sh --no-prompt
gn gen out/release_x64 --args='is_debug=false rtc_include_tests=false proprietary_codecs=true ffmpeg_branding="Chrome"'
ninja -C out/release_x64 webrtc   # hours
```

Produces `third_party/webrtc/src/out/release_x64/obj/libwebrtc.a`.

Then build the addon:

```bash
make build-client
```

## Notes

- `.gclient` and `depot_tools/` are gitignored; only `.gclient.template` is
  tracked. The template's solution root is relative, which is why the whole
  `third_party/webrtc/` directory can be moved as a unit.
- The addon links against Chromium's clang and libc++ objects from the
  WebRTC build output — this is the ABI-matching reason for the
  split-compilation approach in `clients/webrtc_native/build.sh`.
- `.dockerignore` excludes `third_party/` entirely. A stray 20 GB build
  context will stall every docker build in the repo.
