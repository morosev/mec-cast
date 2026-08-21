# ADR-0008: Run identity is a UUIDv7 minted by the admin, stored in `run.json`

- **Status:** Accepted
- **Date:** 2026-08-19

## Context

ADR-0007 gives the admin service ownership of run lifecycle, which forces two
questions the platform has never had to answer: who decides what a run id *is*,
and where does a run's existence outlive the process that created it.

Today neither has an owner. `scripts/run-experiment.sh` calls `uuidgen` and
exports the result; `deploy/compose/local.yml` defaults to the literal string
`dev-run`; the lab composes demand `RUN_ID: ${RUN_ID:?set RUN_ID}` and leave the
value to the operator. Persistence is incidental: `runs/<id>/run.json` is written
by the experiment script and nothing else, the `runs/<id>/{pub,edge,ran}/` tree
has no index, and the logging service can only *derive* a session list by
`GROUP BY trace_id` over rows that a component already managed to post. A run
that failed before its first snapshot leaves no trace anywhere.

The lab makes this worse in a way local development hides: each node writes its
CSV to its own host, and nothing records which host holds which directory.

## Decision

**Identity.** The admin mints a **UUIDv7** in canonical hyphenated form when a
run is created. Alongside it a free-text `label` and a monotonic integer `seq`
for human reference. No semantics are ever derived from the id itself.

**Store.** The admin writes the *same* `runs/<run_id>/run.json` that
`scripts/run-experiment.sh` already writes, with additive fields — `seq`,
`label`, `source`, `state`, timestamps, `participants`, `sites`, `reports`,
`findings`, `removed` — and appends one JSON line per state transition to
`runs/admin-journal.jsonl`. On startup it rebuilds its table by globbing
`runs/*/run.json` and replaying the journal tail.

`store.py` defines a `RunStore` Protocol; `JsonRunStore` is the only
implementation.

## Rationale

UUIDv7 over UUIDv4: still a UUID, so `run_trace_id()` and every downstream
consumer work unchanged, but time-ordered — runs sort chronologically in the
table, in `ls runs/`, and in Postgres. Python 3.12 has no `uuid.uuid7`, but
RFC 9562 §5.7 is ten lines of `os.urandom` and `time.time_ns()`, so it costs no
dependency. Over a monotonic counter: a counter needs a durable allocator and
collides across hosts.

| Store alternative | Why it lost |
|---|---|
| A table in the logging service's Postgres | It is a submodule with its own migration runner — the schema change lands in a different repo. In the lab, Postgres is on the `infra` host and the admin on `edge`: a cross-host dependency for the one page you need when infra is unhappy. |
| The admin's own database | Same cross-host problem, plus a second Postgres to operate. |
| SQLite | Warranted at thousands of runs or with concurrent admins. There is one admin and runs number in the tens. Kept one class away behind `RunStore`. |

Reusing `run.json` rather than inventing a store is the point:
`docs/guides/running-an-experiment.md` already calls it "the reproducibility
artifact — a number without a `run.json` beside it cannot be defended". Making
the admin its author strengthens that rather than adding a competing source of
truth, and it survives Postgres being down, because it is a file next to the
measurements it describes.

`sites` is new capability rather than bookkeeping: the admin learns each node's
hostname from `hello`, so for the first time a lab run records which machine
holds which CSV.

## Consequences

- **Two writers of `run.json`** — the admin and `run-experiment.sh`. Mitigated
  by one documented schema in `docs/_facts.yml`, a `"source"` field naming the
  writer, and a test asserting both shapes parse. Revisit if the script is
  retired.
- **The store is the filesystem**, so it inherits the filesystem's semantics: a
  half-written journal line is possible and is skipped with a warning rather
  than being fatal.
- **In the lab the manifest lives on the edge host** while CSVs live on each
  node's own host. Already true; now recorded explicitly in `sites` instead of
  being folklore.
- Run ids stay UUIDs, so nothing downstream — `run_trace_id()`, the 16-byte
  envelope `trace_id`, the logging service's session grouping — needs to change.
