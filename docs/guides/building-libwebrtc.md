# Building the forked libwebrtc

**You do not need this to build the client.** The client uses the library
prebuilt: `make fetch-libwebrtc`. This guide is for changing the fork itself
-- a new patch, a new upstream base -- and publishing a new prebuilt release.

## What the fork adds

`third_party/webrtc/src` tracks
[morosev/mec-cast-webrtc](https://github.com/morosev/mec-cast-webrtc)
(branch `mec-cast`), which patches upstream WebRTC to:

- add `SendTimestampNsExtension` — a 16-byte RTP header extension carrying
  `capture_ns` + `send_ns`;
- force **every** frame to be a timing frame, so timing metadata is always
  present rather than periodically sampled;
- expose encode duration on decoded frames;
- convert the extension's `capture_ns` with an offset taken against
  WebRTC's own clock, which is not `CLOCK_MONOTONIC` on macOS;
- carry `.github/workflows/libwebrtc.yml`, which builds the release archives.

Why the fork is kept: [ADR-0015](../architecture/adr/0015-media-profile-relay.md).

## Publishing a prebuilt release

The usual path. Commit to the fork's `mec-cast` branch, then tag it:

```bash
cd third_party/webrtc/src
git push origin mec-cast
git tag webrtc-<upstream-short-sha>-<n> && git push origin webrtc-<upstream-short-sha>-<n>
```

The tag starts the release workflow on GitHub's runners (`ubuntu-22.04` for
linux-x64, `macos-14` for macos-arm64; about an hour). It syncs the fork with
gclient, builds `webrtc` with the args below and Chromium's own clang and
libc++, and attaches one archive per platform, with its `.sha256`, to the
tag's release. Each archive holds:

| Path | |
|---|---|
| `lib/libwebrtc.a`, `lib/libc++.a` | the library and Chromium's libc++ |
| `include/` | WebRTC, abseil, libyuv and libc++ headers, at their tree paths |
| `toolchain/` | the clang, lld and llvm-ar it was built with |
| `src/test/` | the test capturer sources the client compiles |
| `defines.txt`, `cflags_cc.txt`, `args.gn`, `BUILD_INFO` | how it was built |

Then point mec-cast at it: in [`third_party/webrtc.lock`](../../third_party/webrtc.lock)
set `tag`, `commit` and each `sha256.<platform>` (from the `.sha256`
assets), commit the lock with the moved submodule pointer, and
`make fetch-libwebrtc && make build-client`.

Why Chromium's libc++ and not the system's: `use_custom_libcxx=false` does
not build this WebRTC -- Chromium's Linux sysroot lacks C++20 library
features it uses, and protobuf does not compile against Apple's libc++. So
the archive ships the toolchain, and the client compiles its WebRTC-facing
half with it ([`build.sh`](../../clients/webrtc_native/build.sh)). There is
no linux-arm64 archive: Chromium ships no clang for arm64 Linux hosts.

## Building from source locally

To iterate on a patch before tagging. ~20 GB and hours.

```bash
cd third_party/webrtc
git clone https://chromium.googlesource.com/chromium/tools/depot_tools.git
export PATH="$PWD/depot_tools:$PATH"
cp .gclient.template .gclient
gclient sync

cd src
gn gen out/release --args='is_debug=false symbol_level=0 treat_warnings_as_errors=false rtc_include_tests=false rtc_build_examples=false rtc_build_tools=false rtc_enable_protobuf=false proprietary_codecs=true ffmpeg_branding="Chrome" rtc_use_h264=true'
ninja -C out/release webrtc
```

On Linux add `rtc_use_x11=false rtc_use_pipewire=false`. The client builds
against an archive's layout, not the build tree: the packaging step of
[the workflow](https://github.com/morosev/mec-cast-webrtc/blob/mec-cast/.github/workflows/libwebrtc.yml)
makes one, and `LIBWEBRTC_DIR=<unpacked> ./build.sh` uses it.

## Notes

- `.gclient` and `depot_tools/` are gitignored; only `.gclient.template` is
  tracked.
- `.dockerignore` excludes `third_party/webrtc/` entirely. A stray 20 GB
  build context will stall every docker build in the repo.
