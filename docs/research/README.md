# Research notes

Experiment protocol, results, and paper material. Kept in the repo so that
a figure can always be traced back to the code and configuration that
produced it.

## Suggested contents

- `protocol.md` — what is being measured, which variables are swept, how
  many repetitions, what counts as a valid run.
- `results/` — one note per campaign, each citing the `run_id`s it draws on.
- `figures/` — generated plots, regenerable from `tools/`.

## Rules that make results defensible

1. **Every reported number cites a `run_id`.** `runs/<run_id>/run.json`
   records the workload, impairment, transport, and the git SHA of the repo
   and every submodule — including whether the tree was dirty.
2. **Cross-host latency requires verified PTP.** Check
   `context.ptp.reliable` in the snapshots, not just that the run completed.
   See [ADR-0003](../architecture/adr/0003-ptp-on-management-lan.md) and
   [timing-model.md](../architecture/timing-model.md).
3. **Percentiles in live snapshots are windowed** (last N samples). For
   whole-run statistics compute from the per-frame CSV — see
   [ADR-0004](../architecture/adr/0004-exact-percentiles.md).
4. **Report dropped samples.** Every snapshot carries drop counters; a run
   with nonzero drops under-represents the tail it was measuring.
5. **State the transport.** Zenoh and DDS results are not interchangeable,
   and comparing them is itself a result
   ([ADR-0001](../architecture/adr/0001-zenoh-over-dds.md)).
