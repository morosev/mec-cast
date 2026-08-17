# Releasing

Three artifact streams, deliberately different in cadence and effort.

| Stream | Artifact | Tag namespace | Cadence | Effort |
|---|---|---|---|---|
| Container images | `ghcr.io/morosev/mec-cast-{ros,ran}` | — | every green push to `main` | none — CI |
| Platform | tag + Release notes | `platform-vX.Y.Z` | per campaign or milestone | `scripts/tag-release.sh` |
| Legacy WebRTC client | zips on a GitHub Release | `vX.Y.Z` | rare | `scripts/release.sh` |

## Two tag namespaces, and why

`v1.0.3` means the **legacy WebRTC client** — the two zips end users download.
`platform-v0.2.0` means the **testbed**: telemetry crate, ROS2 packages, RAN
collector, compose topologies, images. They version independently because they
change independently, and a `v1.0.4` would advertise a client release that
never happened.

They converge when Profile B retires: once the str0m SFU reaches parity the
`platform-` prefix is dropped and the line continues at `v2.0.0`.

For platform versions:

| Bump | Means |
|---|---|
| MAJOR | runs before and after **cannot be compared** — the `TimingEnvelope` wire format, the CSV schema, the logging-service `context` shape, or a metric definition changed |
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
