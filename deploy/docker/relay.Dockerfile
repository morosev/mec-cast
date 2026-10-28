# syntax=docker/dockerfile:1
# mec-cast media relay (edge/media-relay): forwards a WebRTC sender's RTP to
# its receivers without decoding, and measures each frame. A Rust binary on a
# slim runtime, built against the vendored str0m (third_party/str0m).
#
# Build from the repo root:
#   docker build -f deploy/docker/relay.Dockerfile -t mec-cast-relay .
#
# VCS_REF/VERSION are stamped as OCI labels, as for every mec-cast image.

FROM rust:1-bookworm AS build
# aws-lc-rs (str0m's DTLS/SRTP) builds C with cmake.
RUN apt-get update && apt-get install -y --no-install-recommends cmake clang \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
# The relay is a workspace of its own; the root manifest is here only because
# the telemetry crate inherits its edition and licence from it.
COPY Cargo.toml rust-toolchain.toml ./
COPY telemetry ./telemetry
COPY edge/media-relay ./edge/media-relay
COPY third_party/str0m ./third_party/str0m
RUN cargo build --release --locked --manifest-path edge/media-relay/Cargo.toml

FROM debian:bookworm-slim

ARG VCS_REF=unknown
ARG VERSION=unknown
LABEL org.opencontainers.image.title="mec-cast-relay" \
      org.opencontainers.image.description="mec-cast media relay: WebRTC RTP forwarding and per-frame measurement (str0m)" \
      org.opencontainers.image.source="https://github.com/morosev/mec-cast" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.version="${VERSION}"
ENV VCS_REF=${VCS_REF} \
    VERSION=${VERSION}

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /build/edge/media-relay/target/release/mec-cast-relay /usr/local/bin/mec-cast-relay
ENTRYPOINT ["/usr/local/bin/mec-cast-relay"]
