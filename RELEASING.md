# Releasing

**Scope: this releases the legacy WebRTC client only** (Profile B —
`clients/webrtc_native/` + `edge/signaling/`, built against the libwebrtc
fork). It predates the platform restructure and packages a two-binary zip
for end users who do not want to compile WebRTC.

The platform components — `telemetry/`, `ros2/`, `ran/collector` — have **no
release process yet**; they are consumed from source via `make` and the
container images. See [Gaps](#gaps) below.

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

- **Platform components are unreleasable.** No versioning or artifact for
  the telemetry crate (not published to crates.io), the PyO3 wheel, the
  ROS2 packages, or the container images. Today reproducibility rests on
  the git SHAs recorded in `runs/<run_id>/run.json`, which is adequate for
  measurement campaigns but not for external consumers.
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
