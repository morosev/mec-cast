# Releasing

Three artifact streams, deliberately different in cadence and effort.

| Stream | Artifact | Cadence | Effort |
|---|---|---|---|
| Container images | `ghcr.io/morosev/mec-cast-{ros,ran}` | every green push to `main` | none — CI |
| Platform version marker | an annotated git tag | per campaign or milestone | one command |
| Legacy WebRTC client | zips on a GitHub Release | rare | `scripts/release.sh` |

## Container images — automatic

Published by the `publish-images` job, gated on `rust`, `ros`, `ran-image`
and `e2e`, so an image whose pipeline test failed never reaches the registry
four lab hosts pull from. Tagged `:main` and `:sha-<short>`. The repository
is public, so hosts pull without credentials. Details and the pinning
recipe: [deploy/README.md](deploy/README.md#published-images-ghcr).

Nothing to maintain per push. In a few months, set a package retention rule
so `sha-` tags do not accumulate indefinitely.

## Platform version marker — a tag, not a Release

Measurement campaigns need a citable handle. `runs/<id>/run.json` already
records the repo and submodule SHAs, so reproducibility is covered at the
run level; a tag simply makes it human-readable.

```bash
git tag -a platform-v0.2.0 -m "<what changed>" && git push origin platform-v0.2.0
```

**Use a namespace that does not collide with `v1.0.x`.** Those tags mean the
legacy client, while `telemetry` and `ran/collector` sit at `0.1.0`; a
`v1.0.4` would imply a client release that never happened.

**Do not create a GitHub Release for these tags.** A Release carries
downloadable assets, and for the platform there are none — the artifact is
the image in GHCR and the source at that SHA. A bare tag is the marker; a
Release with no assets is ceremony.

Do not publish the crate to crates.io or the wheel to PyPI while neither has
a consumer outside this repository.

## Legacy WebRTC client — the rest of this document

Everything below concerns Profile B only (`clients/webrtc_native/` +
`edge/signaling/`, built against the libwebrtc fork). It predates the
platform restructure and packages a two-binary zip for end users who do not
want to compile WebRTC. It retires when str0m reaches parity.

## Prerequisites

1. All changes committed and pushed.
2. libwebrtc built — see
   [docs/guides/building-libwebrtc.md](docs/guides/building-libwebrtc.md):
   ```bash
   ninja -C third_party/webrtc/src/out/release_x64 webrtc
   ```
3. Client addon built:
   ```bash
   make build-client
   ```
4. Legacy e2e passes:
   ```bash
   make test-legacy
   ```
5. `gh` CLI authenticated (`gh auth status`).

## Creating a release

```bash
./scripts/release.sh 1.0.4 "Audio delay measurement, CSV export"
```

This verifies prerequisites, packages both zips, appends a row to the
version history below, commits and pushes it, creates and pushes an
annotated tag `vX.Y.Z`, and creates a GitHub release with both zips
attached.

Dry run (packages only, no tag or push):

```bash
./scripts/release.sh 1.0.4 "Test release" --dry-run
```

## Package contents

Paths below are **inside the zip**, which keeps the historical flat layout
(`client/`, `server/`, `tests/`) rather than the repository layout.

### Runtime — `mec-cast-vX.Y.Z-linux-x64.zip`

- `client/build/Release/webrtc_addon.node` — prebuilt native addon
- `client/client.js`, `client-config.json`, `package.json`
- `server/server.js`, `package.json`
- `tests/e2e_local.sh`
- `LICENSE`, `INSTALL.md` (version and date injected)

### Dev — `mec-cast-vX.Y.Z-linux-x64-dev.zip`

Runtime contents plus:

- `libwebrtc.a` — prebuilt static library
- `client/src/*` — C++ sources and headers
- `client/build.sh`
- `INSTALL-DEV.md`

## Gaps

Known and deliberate, recorded so they are not rediscovered:

- **The telemetry crate and the PyO3 wheel are not published.** Deliberate
  while neither has a consumer outside this repository; publishing would
  create a maintenance obligation for no reader. The container images and
  the git SHAs in `runs/<run_id>/run.json` cover reproducibility today.
- **Zip layout diverges from repo layout**, kept for continuity with
  released v1.0.x packages.
- **This process retires with Profile B.** When the str0m SFU reaches
  parity ([str0m-profile.md](docs/architecture/str0m-profile.md)), the
  libwebrtc fork and this document go with it.

## Version history

| Version | Date | Highlights |
|---------|------|------------|
| v1.0.3 | 2025-05-25 | Full pipeline delay measurement, WebRTC submodule |
| v1.0.2 | — | Initial Linux port with basic delay measurement |
