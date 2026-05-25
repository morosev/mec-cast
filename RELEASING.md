# Releasing MEC-Cast

## Prerequisites

Before creating a release, ensure:

1. All changes are committed and pushed
2. WebRTC is built: `cd webrtc/src && ninja -C out/release_x64 webrtc`
3. Client addon is built: `cd client && ./build.sh`
4. E2E test passes: `./tests/e2e_local.sh`
5. `gh` CLI is authenticated: `gh auth status`

## Creating a Release

```bash
./scripts/release.sh <version>
```

Example:
```bash
./scripts/release.sh 1.0.4
```

This will:
1. Verify prerequisites (built binaries, clean tree, gh auth)
2. Package the runtime zip (prebuilt `webrtc_addon.node` + JS files)
3. Package the dev zip (adds `libwebrtc.a` + C++ source + build.sh)
4. Create an annotated git tag `vX.Y.Z`
5. Push the tag to GitHub
6. Create a GitHub release with release notes
7. Upload both zip packages as release assets

## Dry Run

To test packaging without tagging/pushing:

```bash
./scripts/release.sh 1.0.4 --dry-run
```

Packages are created in a temp directory for inspection.

## Package Contents

### Runtime (`mec-cast-vX.Y.Z-linux-x64.zip`)

Everything needed to run without building:
- `client/build/Release/webrtc_addon.node` — prebuilt native addon
- `client/client.js`, `client-config.json`, `package.json`
- `server/server.js`, `package.json`
- `tests/e2e_local.sh`
- `LICENSE`
- `INSTALL.md` (with version and date injected)

### Dev (`mec-cast-vX.Y.Z-linux-x64-dev.zip`)

For developers who want to modify and rebuild the addon without compiling WebRTC:
- All runtime contents
- `libwebrtc.a` — prebuilt WebRTC static library
- `client/src/*` — all C++ source and headers
- `client/build.sh` — build script
- `INSTALL-DEV.md` (with version and date injected)

## Version History

| Version | Date | Highlights |
|---------|------|------------|
| v1.0.3 | 2025-05-25 | Full pipeline delay measurement, WebRTC submodule |
| v1.0.2 | — | Initial Linux port with basic delay measurement |
