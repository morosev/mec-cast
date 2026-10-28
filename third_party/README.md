# Third-party components

Forks of external libraries, vendored as submodules because mec-cast
extends or builds them. All are excluded from the root Cargo workspace
(`exclude = ["third_party"]`), and all but `mpeg-pcc-tmc13` and `str0m` from
every docker build context (`.dockerignore`) — `webrtc/` alone is ~20 GB.

| Directory | Upstream | Our fork | Why forked |
|---|---|---|---|
| `webrtc/src` | [WebRTC](https://webrtc.googlesource.com/src) | [mec-cast-webrtc](https://github.com/morosev/mec-cast-webrtc) (`mec-cast`) | `SendTimestampNsExtension`; force every frame to be a timing frame; expose encode duration; a release workflow that prebuilds it |
| `str0m` | [algesten/str0m](https://github.com/algesten/str0m) | [mec-cast-str0m](https://github.com/morosev/mec-cast-str0m) (`main`) | none in use: the [relay](../edge/README.md#media-relay) needs only upstream features |
| `mpeg-pcc-tmc13` | [MPEGGroup/mpeg-pcc-tmc13](https://github.com/MPEGGroup/mpeg-pcc-tmc13) (G-PCC reference, v23.0-rc2) | [mpeg-pcc-tmc13](https://github.com/morosev/mpeg-pcc-tmc13) (`master`) | slices of a frame encoded and decoded in parallel; a C interface for embedding the codec |

## Getting them

```bash
git submodule update --init third_party/str0m          # small
git submodule update --init third_party/mpeg-pcc-tmc13 # small
make fetch-libwebrtc                                    # prebuilt libwebrtc, 60-90 MB
git submodule update --init --recursive third_party/webrtc/src   # the source, ~20 GB
```

`str0m` is an ordinary Cargo crate — `cargo build` inside it just works.
`mpeg-pcc-tmc13` is plain CMake (`mkdir build && cd build && cmake .. && make`),
see its own README. The ROS image builds its `libtmc3codec` from this
submodule ([ADR-0014](../docs/architecture/adr/0014-gpcc-point-cloud-codec.md)),
so the `ue` and `edge` lab roles and CI need it checked out.
The relay image builds against `str0m`, so the `relay` lab role and CI need
it checked out.

`webrtc` is used prebuilt: the fork's release workflow builds linux-x64 and
macos-arm64 archives on a `webrtc-*` tag, [`webrtc.lock`](webrtc.lock) pins
the tag and their SHA-256, and `make fetch-libwebrtc` unpacks the one for
this host into `third_party/libwebrtc/` (ignored by git). The source
checkout is needed only to change the fork; see
[building libwebrtc](../docs/guides/building-libwebrtc.md).

## Keep the str0m delta small

str0m was chosen because it is sans-IO and has what the relay needs upstream:
an RTP mode that forwards packets without depacketizing, and header
extensions registered by URI with user serializers. The relay uses the
fork's `main` unmodified. Keep it that way, so that a plain crates.io
dependency stays a live option; logic belongs in
[`edge/media-relay`](../edge/media-relay/), not in the library.

See [docs/architecture/str0m-profile.md](../docs/architecture/str0m-profile.md).
