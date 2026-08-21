# Code → doc map

Which docs a code change implicates. Use it to turn a diff into candidates;
it is a starting point, not a substitute for reading the diff.

## By source path

| Changed path | Implicates | Watch for |
|---|---|---|
| `telemetry/src/envelope.rs` | `_facts.yml` (envelope), `telemetry/README.md`, `architecture/overview.md`, `timing-model.md`, both dataflow diagrams | Wire size or field changes break the cross-profile contract — check the ROS msg and the C header agree |
| `telemetry/src/stats.rs` | `timing-model.md`, ADR-0004, `telemetry/README.md`, PPT slide 6 | ADR-0004 asserts exact percentiles. A change here may contradict a decision |
| `telemetry/src/recorder.rs` | `telemetry/README.md`, `architecture/overview.md`, lifecycle diagram | Queue size, drop policy, thread split are all documented specifics |
| `telemetry/src/clock.rs`, `ptp.rs` | `timing-model.md`, ADR-0003, `operations/lab-topology.md`, `deploy/lab/ptp/README.md` | The clock model is the basis for every cross-host claim |
| `telemetry/src/ffi.rs`, `include/` | `clients/webrtc_native/README.md`, `telemetry/README.md`, PPT slide 8 | The C ABI is a published contract |
| `telemetry/src/py.rs` | `telemetry/README.md`, `ros2/README.md` | trace_id derivation must stay identical to the C binding |
| `ros2/src/mec_cast_msgs/` | `ros2/README.md`, `_facts.yml`, both dataflow diagrams | Message shape is the transport contract |
| `ros2/src/mec_cast_lidar_client/` | `ros2/README.md`, PPT slide 5, `guides/running-an-experiment.md` | Parameter names/defaults are the experiment's sweep variables |
| `ros2/src/mec_cast_edge/` | `ros2/README.md`, PPT slide 6, lifecycle diagram | Where the stamps happen |
| `ran/collector/` | `ran/collector/README.md`, ADR-0005, PPT slides 1 & 10 | KPI list and UDP contract |
| `deploy/compose/` | `deploy/README.md`, `guides/manual-operation.md`, PPT slide 2, topology diagram | Service names, ports, volumes appear in many places |
| `deploy/lab/` | `operations/lab-topology.md`, `guides/manual-operation.md`, PPT slide 3, `lab-deployment.mmd` | Roles, start order, env requirements |
| `deploy/docker/` | `deploy/README.md`, `ros2/README.md` | Image names and entrypoints |
| `Makefile` | `README.md`, `guides/manual-operation.md`, most component READMEs | Every doc that shows a `make` command |
| `scripts/` | `guides/running-an-experiment.md`, `guides/manual-operation.md`, `README.md` | Flags and output layout |
| `clients/webrtc_native/` | `clients/README.md` + its own README, PPT slide 8 | Note the camera limitation stays stated |
| `services/logging` (SHA bump) | `operations/logging-submodule.md`, `services/README.md`, PPT slide 4, `_facts.yml` | Schema changes are breaking — `extra="forbid"` |
| `services/admin/**` | `operations/admin-service.md`, `services/README.md`, root README, `_facts.yml`, PPT slide 4 | Port, protocol version, keepalive/timeout numbers, run states |
| `ros2/src/mec_cast_admin_client/` | `ros2/README.md`, `operations/admin-service.md`, ADR-0007 | The Python and Rust clients must agree on the envelope — check both, and `vectors.json` |
| `ran/collector/src/admin.rs` | `ran/collector/README.md`, `operations/admin-service.md`, ADR-0007 | Feature gate: `--no-default-features` must still build without tungstenite |
| `.github/workflows/` | `README.md` (status), `guides/manual-operation.md` | What CI actually covers |
| `Cargo.toml` (workspace) | `README.md`, `telemetry/README.md`, `third_party/README.md` | Member and exclude lists |

## Change shapes that usually mean an ADR

Not every one is architectural, but each deserves the question:

- a new dependency crossing a component boundary
- a wire format, schema, or serialization change
- swapping a protocol, transport, or middleware
- adding or removing a persistent store
- reversing something an existing ADR decided
- a new security-relevant default (auth, exposure, retention)

## Docs that live in code

A class this map did not previously cover. `services/admin/src/mec_cast_admin/workflow.py`
holds the operator-facing remedy strings the admin page shows — they embed
`make up-admin`, `bash deploy/lab/deploy.sh <role> <host>`, port 55555, the
Zenoh endpoint form, and config keys like `metrics.addr`. They are
Tier-1-checkable facts sitting in a `.py` file where no doc gate looks.

**Grep `workflow.py` whenever a script, make target, port or path is renamed.**
The tests assert every finding has a non-empty remedy, but nothing asserts the
remedy is still true.

## Claims that rot silently

Grep for these in any doc you touch — no linter catches them, and each has
already been wrong once in this repo:

- "not yet", "currently only", "does not yet", "planned", "for now"
- "needs an upstream push", "unpopulated", "empty"
- version numbers and pinned SHAs
- "X is not installed" / "X is unavailable"
- counts ("three components", "four roles", "10 slides")
- status tables in `README.md`
- remedy strings in `services/admin/src/mec_cast_admin/workflow.py`
