# ADR-0002: One shared telemetry core, written in Rust

- **Status:** Accepted
- **Date:** 2026-08-06

## Context

mec-cast carries two transport profiles — ROS2/Zenoh point clouds and
WebRTC media — and a RAN metrics tap. All three must produce latency
numbers that are directly comparable, because comparing them *is* the
platform's purpose.

The original delay-measurement code was C++ inside the libwebrtc addon.
It could not be reused by a Python edge node or a Rust WebRTC stack, and
it carried real defects (see ADR-0004).

The alternatives were: extract the existing C++ into a library; write the
core in Python to match the edge and the logging service; or write it in
Rust.

## Decision

A single Rust crate, **`mec-cast-telemetry`** at `telemetry/`, owns the
timing envelope, the statistics engine, the clock abstraction, the PTP
monitor, and the async recording pipeline. It is consumed by str0m
natively and by the Python edge through PyO3 (abi3 wheel).

## Rationale

- **One implementation, one set of tests.** Two implementations of the
  same percentile logic would eventually disagree, and the disagreement
  would look like a research finding.
- **Ecosystem alignment.** str0m and Zenoh are both Rust; the media
  profile links the crate with zero FFI on the hot path.
- **The hot path has hard constraints** — no allocation, no blocking, no
  locks while a frame is in flight. Rust expresses that safely; Python
  cannot meet it at all and C++ meets it without the guardrails.
- Python-first was rejected because str0m would have had to reimplement
  the schema, recreating the divergence problem.
- Reusing the C++ was rejected because it keeps the libwebrtc-era build
  complexity (Chromium clang, libc++ ABI matching) in a component that
  should build anywhere.

## Consequences

- The dependency direction is strictly one-way: profiles depend on
  telemetry; telemetry knows nothing about ROS, Zenoh, WebRTC, or srsRAN.
  This is enforced by review, not by tooling.
- Rust is now a prerequisite for the platform (not for the legacy client).
- The Python edge pays a PyO3 boundary crossing per sample. Measured at
  ~10–20 Hz this is irrelevant; if a per-packet Python path ever appears,
  it must be revisited.
- PTP support is Linux-only (`cfg(target_os = "linux")`), so the crate is
  portable but its PHC clock is not.
