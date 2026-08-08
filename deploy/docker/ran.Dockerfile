# syntax=docker/dockerfile:1
# srsRAN MAC metrics collector. Static-ish Rust binary on a slim runtime.
#
# Build from the repo root:
#   docker build -f deploy/docker/ran.Dockerfile -t mec-cast-ran .

FROM rust:1-bookworm AS build
WORKDIR /build
COPY Cargo.toml Cargo.lock rust-toolchain.toml ./
COPY telemetry ./telemetry
COPY ran ./ran
RUN cargo build --release -p ran-collector

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /build/target/release/ran-collector /usr/local/bin/ran-collector
WORKDIR /
ENTRYPOINT ["/usr/local/bin/ran-collector"]
