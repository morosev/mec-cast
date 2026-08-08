# ADR-0004: Exact windowed percentiles, not a streaming estimator

- **Status:** Accepted
- **Date:** 2026-08-06

## Context

The C++ delay-measurement layer reported a `p99_ns` that was not a
percentile at all. It was an asymmetric exponential moving average —
rising by 1/10 of the gap on a new high, decaying by 1/1000 otherwise:

```cpp
if (value_ns > p99_ns) p99_ns = p99_ns + (value_ns - p99_ns) / 10;
else                   p99_ns = p99_ns - (p99_ns - value_ns) / 1000;
```

That converges to a slowly-decaying high-water mark, not to the 99th
percentile of anything. The same struct also initialised `max_ns` to `0`
(so an all-negative series reported a maximum never observed) and
accumulated a Welford variance that was never normalised or emitted.

Tail latency is the headline number for an industrial 5G study, so this
had to be correct before anything was published.

## Decision

Keep a bounded ring buffer of the last N samples per metric (default
N = 8192). Compute p50/p90/p99 **exactly** from that window at snapshot
time by sorting a copy. Emit `stddev_ns` alongside, and initialise `max`
to `i64::MIN` with an empty-window snapshot returning `None`.

## Rationale

- **P² was rejected.** Its error is unbounded on multimodal distributions,
  and 5G latency under HARQ retransmission and scheduler grants is
  exactly multimodal — the very regime where the estimate would mislead.
- The cost is negligible where it matters. `record()` stays O(1) (a ring
  push, no allocation) on the hot path. Sorting 8192 × `i64` costs
  microseconds and happens **on the writer thread**, once per 1–2 s
  snapshot, never in the measurement path.
- 64 KiB per metric is nothing on an edge server.
- An exact windowed percentile is defensible in a paper. "An asymmetric
  EWMA that we call p99" is not.

## Consequences

- Percentiles describe the **last N samples**, not the whole run. The
  window size is recorded in every snapshot so the statistic is
  self-describing.
- Full-run percentiles are still available offline from the per-frame CSV,
  which remains the source of truth for analysis.
- If sample rates ever rise enough that the window covers too short an
  interval, the window grows before the algorithm changes.
