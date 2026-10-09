#!/bin/bash
# Turn a run's raw srsRAN reports into a ran-collector test fixture.
#
#   bash scripts/ran-fixture.sh <run_id> <srsran-version> <udp|ws> [lines]
#
# The collector keeps every report verbatim in runs/<run_id>/ran/reports.jsonl
# (RAN_RAW_REPORTS, on by default). This copies the first [lines] (default
# 120) well-formed ones to ran/collector/testdata/srsran_<version>.lab.jsonl
# and writes the provenance sidecar beside it (.lab.json). tests/fixtures.rs
# then checks the capture on every `cargo test`, and refuses a capture whose
# sidecar does not name its srsRAN version — the schema varies by release.
#
# <srsran-version> is what `gnb --version` prints on the lab gNB, e.g. 25.04.
# <udp|ws> is the transport the collector used: the admin's gNB node shows it
# as `transport`, and the collector logs "auto: srsRAN is sending over ...".
#
# Run it from the repo on whichever machine holds the run: the gNB host
# itself, or wherever scripts/collect-runs.sh gathered runs to.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

usage() { sed -n '2,19p' "$0"; exit 2; }
[ $# -ge 3 ] || usage
RUN_ID=$1
VERSION=$2
TRANSPORT=$3
LINES=${4:-120}

case "$TRANSPORT" in udp|ws) ;; *) echo "transport must be udp or ws" >&2; exit 2 ;; esac

SRC="runs/$RUN_ID/ran/reports.jsonl"
[ -s "$SRC" ] || { echo "ERROR: $SRC missing or empty — was the collector recording this run?" >&2; exit 1; }

SAFE_VERSION=$(printf '%s' "$VERSION" | tr -c 'A-Za-z0-9.' '_' | sed 's/_*$//')
OUT="ran/collector/testdata/srsran_${SAFE_VERSION}.lab.jsonl"
META="${OUT%.jsonl}.json"

python3 - "$SRC" "$OUT" "$META" "$LINES" "$VERSION" "$TRANSPORT" "$RUN_ID" <<'PY'
import datetime, json, socket, sys
src, out, meta, limit, version, transport, run_id = sys.argv[1:8]
kept, skipped = [], 0
with open(src) as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            if not isinstance(json.loads(line), dict):
                raise ValueError
        except ValueError:
            skipped += 1
            continue
        kept.append(line)
        if len(kept) >= int(limit):
            break
if not kept:
    sys.exit(f"ERROR: no well-formed reports in {src}")
with open(out, "w") as f:
    f.write("\n".join(kept) + "\n")
with open(meta, "w") as f:
    json.dump({
        "srsran_version": version,
        "transport": transport,
        "run_id": run_id,
        "captured_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "host": socket.gethostname(),
        "reports": len(kept),
        "skipped_malformed": skipped,
        "source": src,
    }, f, indent=2)
    f.write("\n")
print(f"{out}: {len(kept)} reports ({skipped} malformed skipped)")
print(f"{meta}: provenance")
PY

if command -v cargo >/dev/null 2>&1; then
  echo "==> cargo test -p ran-collector --test fixtures"
  cargo test -q -p ran-collector --test fixtures
else
  echo "cargo not on PATH here; run 'cargo test -p ran-collector --test fixtures' where it is."
fi
echo
echo "Commit both files, naming the srsRAN version in the message."
