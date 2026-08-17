# syntax=docker/dockerfile:1
# srsRAN MAC metrics collector. Static-ish Rust binary on a slim runtime.
#
# Build from the repo root:
#   docker build -f deploy/docker/ran.Dockerfile -t mec-cast-ran .
#
# VCS_REF/VERSION are stamped as OCI labels so a running container can be
# traced back to a commit without a checkout beside it. scripts/version.sh
# compares the label against the local HEAD and warns when a host is running
# an image built from different source — the failure that silently detaches
# a measurement campaign from the code that produced it.
#   --build-arg VCS_REF=$(git rev-parse HEAD)

FROM rust:1-bookworm AS build
WORKDIR /build
COPY Cargo.toml Cargo.lock rust-toolchain.toml ./
COPY telemetry ./telemetry
COPY ran ./ran
RUN cargo build --release -p ran-collector

FROM debian:bookworm-slim

ARG VCS_REF=unknown
ARG VERSION=unknown
LABEL org.opencontainers.image.title="mec-cast-ran" \
      org.opencontainers.image.description="srsRAN O-DU MAC metrics collector" \
      org.opencontainers.image.source="https://github.com/morosev/mec-cast" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.version="${VERSION}"

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /build/target/release/ran-collector /usr/local/bin/ran-collector
WORKDIR /
ENTRYPOINT ["/usr/local/bin/ran-collector"]
