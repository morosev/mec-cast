# The `services/logging` submodule

`services/logging` is a submodule of
[mec-cast-logging-service](https://github.com/morosev/mec-cast-logging-service):
a FastAPI + PostgreSQL service that ingests aggregated latency snapshots and
makes them queryable by `trace_id`, service, level, and JSONB `context`
containment.

## Getting it

```bash
git submodule update --init services/logging
test -f services/logging/pyproject.toml && echo OK
```

A fresh clone should use `--recurse-submodules`; `scripts/bootstrap-dev.sh`
initialises it otherwise.

## Why a submodule rather than a merge

- It is an independent deployable with its own database, migrations, and
  release cadence — merging would dissolve a real boundary and put two
  unrelated Python projects under one CI configuration.
- Telemetry's HTTP sink is coupled to its `LogEntryCreate` schema, which is
  `extra="forbid"`: **any** unexpected top-level field is a 422 at runtime.
  Pinning a submodule SHA makes "which schema version does this build
  target" an explicit, reviewable fact rather than whatever happens to be
  checked out next door.
- It removes the out-of-repo `../` build context the compose files used to
  need, so a fresh clone gives a working e2e suite and CI can run the job.

## The schema contract

Snapshots posted by `mec-cast-telemetry` must match `LogEntryCreate`
exactly — top level is only `timestamp, level, service, host, logger,
message, context, trace_id`. Everything mec-cast-specific goes inside
`context`. When bumping the submodule, re-run:

```bash
make test-e2e
```

`tests/e2e/test_e2e_latency.py` asserts snapshots actually land and are
queryable by `trace_id`, so a schema drift fails there rather than silently
in the lab.

## Pointing the build elsewhere

The compose files take the build context from `MECLOG_BUILD_CONTEXT`,
defaulting to `../../services/logging`. To build from a working tree
instead:

```bash
MECLOG_BUILD_CONTEXT=../../../mec-cast-logging-service make up-logging
```

`tests/e2e/` and `scripts/run-experiment.sh` set this automatically if the
submodule is ever empty, so the suite degrades gracefully rather than
failing with a confusing missing-`pyproject.toml` error.
