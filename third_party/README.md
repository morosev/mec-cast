# Third-party components

Forks of external libraries, vendored as submodules because mec-cast
extends them. Both are excluded from the Cargo workspace
(`exclude = ["third_party"]`) and from every docker build context
(`.dockerignore`) — `webrtc/` alone is ~20 GB.

| Directory | Upstream | Our fork | Why forked |
|---|---|---|---|
| `webrtc/src` | [WebRTC](https://webrtc.googlesource.com/src) | [mec-cast-webrtc](https://github.com/morosev/mec-cast-webrtc) (`mec-cast`) | `SendTimestampNsExtension`; force every frame to be a timing frame; expose encode duration |
| `str0m` | [algesten/str0m](https://github.com/algesten/str0m) | [mec-cast-str0m](https://github.com/morosev/mec-cast-str0m) (`main`) | RTP header-extension registration for the timing envelope |

## Getting them

```bash
git submodule update --init third_party/str0m          # small
git submodule update --init --recursive third_party/webrtc/src   # ~20 GB
```

`str0m` is an ordinary Cargo crate — `cargo build` inside it just works.
`webrtc` needs depot_tools and hours; see
[building libwebrtc](../docs/guides/building-libwebrtc.md).

## Keep the str0m delta small

str0m was chosen precisely because it is sans-IO: owning the socket means
egress and ingress can be timestamped **without** patching the stack, which
is the entire reason the libwebrtc fork exists. Vendoring a fork of str0m
partly works against that rationale.

Keep the diff minimal — ideally only what upstream will not accept — so
that reverting to a plain crates.io dependency stays a live option. If the
fork starts accumulating logic, that logic probably belongs in the
mec-cast SFU under `edge/`, not in the library.

See [docs/architecture/str0m-profile.md](../docs/architecture/str0m-profile.md).
