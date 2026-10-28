# Releasing

Three artifact streams, deliberately different in cadence and effort.

| Stream | Artifact | Tag namespace | Cadence | Effort |
|---|---|---|---|---|
| Container images | `ghcr.io/morosev/mec-cast-{ros,ran}` | — | every green push to `main` | none — CI |
| Platform | tag + Release notes | `platform-vX.Y.Z` | per campaign or milestone | `scripts/tag-release.sh` |
| Prebuilt libwebrtc | archives on a Release of the fork, [mec-cast-webrtc](https://github.com/morosev/mec-cast-webrtc) | `webrtc-<upstream>-<n>`, on the fork | when the fork changes | push a tag; CI builds |

## Tag namespaces

`platform-v0.7.0` means the **testbed**: the telemetry crate, the ROS 2
packages, the RAN collector and xApp, the media profile's client and relay
([ADR-0015](docs/architecture/adr/0015-media-profile-relay.md)), the admin,
the compose topologies and the images. Both profiles release together.

`webrtc-c7f637a-4` names one build of the patched libwebrtc, tagged on the
fork, not here: the upstream commit it is based on, then a counter. mec-cast
pins one in `third_party/webrtc.lock`, so a platform release names the
libwebrtc its media clients build against.

`v1.0.x` tags are the WebRTC client's former zip releases, retired; `v1.0.3`
was the last. The client now builds in minutes on Linux and macOS from the
prebuilt libwebrtc, so it ships as source in a platform release. No new `v`
tags are made, and `scripts/version.sh` and `tag-release.sh` match
`platform-v*` only.

For platform versions:

| Bump | Means |
|---|---|
| MAJOR | runs before and after **cannot be compared** — the `TimingEnvelope` wire format, the CSV schema (`samples.csv` or `webrtc.csv`), the media profile's RTP header extension, the logging-service `context` shape, or a metric definition changed |
| MINOR | new capability; existing runs stay comparable |
| PATCH | fixes and docs |

That MAJOR rule is the one worth being strict about. Every other kind of
breakage announces itself; a silently redefined metric does not, and it
invalidates comparisons made months later against archived data.

## Container images — automatic

Published by the `publish-images` job, gated on `rust`, `ros`, `ran-image`
and `e2e`, so an image whose pipeline test failed never reaches the registry
four lab hosts pull from. Tagged `:main` and `:sha-<short>`. The repository
is public, so hosts pull without credentials. Details and the pinning
recipe: [deploy/README.md](deploy/README.md#published-images-ghcr).

Nothing to maintain per push. In a few months, set a package retention rule
so `sha-` tags do not accumulate indefinitely.

Not every role has a published image. The media relay's (`mec-cast-relay`)
is built on its host by `deploy/lab/deploy.sh relay`, and the media clients
are not containerised: each UE builds them with
`make fetch-libwebrtc build-client`.

## Platform releases

```bash
bash scripts/tag-release.sh 0.2.0 "Version reporting and OCI image stamps"
bash scripts/tag-release.sh 0.2.0 "..." --dry-run   # print the notes, change nothing
```

Pass the bare `X.Y.Z`; the script adds `platform-v`. It refuses a dirty tree,
a branch other than `main`, a local `main` that differs from the remote, and
an existing tag; it warns on a non-green pipeline and asks before continuing.

It creates an annotated tag **and** a GitHub Release. No assets are attached —
the artifacts are the images already in GHCR and the source at that SHA — but
the Release is the changelog, which is the part worth keeping. The notes are
generated: commit log bounded by the previous `platform-v*` tag, the pinned
`sha-` image references, submodule commits, and the deploy commands.

The tag names a commit; it does not trigger a build. Images for that SHA were
published when it landed on `main`.

Do not publish the crate to crates.io or the wheel to PyPI while neither has
a consumer outside this repository.

## Prebuilt libwebrtc

The media clients link a patched libwebrtc that takes hours and ~20 GB to
build, so it is built once per change, by the fork's release workflow, and
downloaded everywhere else. To publish one:

1. Commit the change to the fork's `mec-cast` branch and push it.
2. Tag it `webrtc-<upstream-short-sha>-<n>`, the next `n`, and push the tag.
   The workflow builds linux-x64 and macos-arm64 (about an hour) and attaches
   one archive per platform, with its `.sha256`, to the tag's Release.
3. In mec-cast, set `tag`, `commit` and each `sha256.<platform>` in
   `third_party/webrtc.lock`, and commit the lock with the moved
   `third_party/webrtc/src` submodule pointer.
4. `make fetch-libwebrtc build-client`, and run a media run end to end.

The details, and why the archive carries its own clang and libc++:
[building libwebrtc](docs/guides/building-libwebrtc.md#publishing-a-prebuilt-release).

A new libwebrtc can change what a media run measures -- its encoder, pacer
and congestion control are the subject. Each media run records the release it
used, as `media.libwebrtc` in `run.json`; say in the platform release notes
when the lock moved.

## Knowing what a host is running

The scenario this exists for: a release lands, an admin pulls on each host and
redeploys per role, and then needs to confirm what that host actually ended up
with. On any host:

```bash
make version
```

It reports the role, the version and commit, the submodule pins, every running
container with the commit its image was built from, and PTP presence — and it
**warns when a running image was built from a different commit than the
checkout**, which is how a campaign silently detaches from its source. Nothing
in it is read from a hand-maintained file.

Two ways source reaches a host, and the report handles both:

- `git pull` — the checkout speaks for itself.
- `deploy/lab/deploy.sh` — rsyncs without `.git`, so it writes
  `.deployed-version` (version, SHA, role, timestamp, who deployed it) and
  `make version` falls back to that. `deploy.sh` also prints the report at the
  end of every deploy, so the admin sees it without a second login.

A host with neither says `UNKNOWN` rather than guessing. Deliberate: no answer
is safer than a confident wrong one when the question is which code produced a
measurement.

The images are stamped with OCI labels (`org.opencontainers.image.revision`
and `.version`) at build time, which is what makes the comparison possible:

```bash
docker inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
  ghcr.io/morosev/mec-cast-ros:main
```

## Gaps

Known and deliberate, recorded so they are not rediscovered:

- **The telemetry crate and the PyO3 wheel are not published.** Deliberate
  while neither has a consumer outside this repository; publishing would
  create a maintenance obligation for no reader. The container images and
  the git SHAs in `runs/<run_id>/run.json` cover reproducibility today.
- **The relay image is not in GHCR**, and the media clients are not packaged:
  both build on their hosts from the release's source. The relay builds in
  minutes; the clients' one heavy dependency, libwebrtc, is the prebuilt
  archive.
- **No arm64 Linux libwebrtc**: Chromium ships no clang for arm64 Linux hosts,
  so the fork's workflow cannot build one.

## Former client releases

The WebRTC client's zips, retired. Kept for the record of what `v1.0.x` tags
mean.

| Version | Date | Highlights |
|---------|------|------------|
| v1.0.3 | 2025-05-25 | Full pipeline delay measurement, WebRTC submodule |
| v1.0.2 | — | Initial Linux port with basic delay measurement |
