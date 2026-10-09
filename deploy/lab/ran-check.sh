#!/bin/bash
# Is the RAN tap working on this gNB host? One command, every answer.
#
#   INFRA_HOST=10.0.0.10 bash deploy/lab/ran-check.sh
#
# Run on the gNB host, from the repo. Answers, in order:
#   1. which srsRAN this is (and so which metrics transport it should use)
#   2. whether that transport is configured and reachable
#   3. what the collector container says it is doing
#   4. what the admin sees: source, transport, WebSocket errors, PTP, counts
#   5. what the newest run on disk holds (reports.jsonl, layout)
#
# Read-only: it starts, stops and changes nothing.
#
# Variables: INFRA_HOST (admin + logging; omit to skip 4), GNB_BIN (path to
# the gnb binary if not on PATH), GNB_CONF (gnb.yml to inspect),
# WS (remote_control host:port, default 127.0.0.1:8001), RUNS (default runs).
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT_DIR"

WS=${WS:-127.0.0.1:8001}
RUNS=${RUNS:-runs}
ok()   { printf '  \033[32mOK\033[0m   %s\n' "$*"; }
warn() { printf '  \033[33mWARN\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$*"; }
say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }

# --- 1. srsRAN version ---------------------------------------------------
say "1. srsRAN"
GNB=${GNB_BIN:-$(command -v gnb 2>/dev/null || true)}
EXPECT=""
if [ -n "$GNB" ] && [ -x "$GNB" ]; then
  VERSION_LINE=$("$GNB" --version 2>&1 | grep -m1 -iE 'version|srsRAN' || true)
  ok "gnb: $GNB"
  echo "       $VERSION_LINE"
  # 25.04 removed metrics.addr/port; from there the JSON is on the WebSocket.
  if printf '%s' "$VERSION_LINE" | grep -qE '(^|[^0-9])2[5-9]\.[0-9]+'; then
    EXPECT=ws
    echo "       >= 25.x: metrics come over the remote_control WebSocket"
  elif printf '%s' "$VERSION_LINE" | grep -qE '(^|[^0-9])2[0-4]\.[0-9]+'; then
    EXPECT=udp
    echo "       <= 24.x: metrics come as UDP datagrams (metrics.addr/port)"
  else
    warn "could not read a release number; record the line above in lab-topology.md"
  fi
else
  warn "gnb binary not found (set GNB_BIN=/path/to/gnb); version unknown"
fi

# --- 2. configuration and reachability ------------------------------------
say "2. srsRAN metrics configuration"
CONF=${GNB_CONF:-}
if [ -z "$CONF" ]; then
  CONF=$(ps -eo args 2>/dev/null | grep -E '[g]nb .*-c' | sed -E 's/.*-c[ =]?([^ ]+).*/\1/' | head -1)
fi
if [ -n "$CONF" ] && [ -r "$CONF" ]; then
  ok "config: $CONF"
  grep -nE '^\s*(enable_json|enable_json_metrics|addr|port|bind_addr|enabled|du_report_period|enable_sched)\s*:' "$CONF" \
    | sed 's/^/       /' | head -20
  grep -qE '^\s*remote_control\s*:' "$CONF" && echo "       remote_control block present" \
    || echo "       no remote_control block (needed for ws, srsRAN 25.04+)"
else
  warn "no readable gnb config found (set GNB_CONF=/path/to/gnb.yml)"
fi

if (exec 3<>"/dev/tcp/${WS%:*}/${WS##*:}") 2>/dev/null; then
  ok "remote_control WebSocket port open at $WS"
  WS_OPEN=1
else
  [ "$EXPECT" = ws ] && bad "nothing listening at $WS — srsRAN 25.x needs remote_control.enabled: true" \
                     || warn "nothing listening at $WS (fine for a UDP-exporting gNB)"
  WS_OPEN=0
fi

# --- 3. the collector container -------------------------------------------
say "3. ran-collector container"
CID=$(docker ps --filter name=ran-collector --format '{{.ID}} {{.Names}} {{.Status}}' 2>/dev/null | head -1)
if [ -z "$CID" ]; then
  bad "no running ran-collector container (deploy/lab/compose.gnb.yml)"
else
  ok "$CID"
  docker logs --tail 200 "${CID%% *}" 2>&1 \
    | grep -E 'source=|subscribed|auto: srsRAN|reconnecting|PTP_DEVICE|recording run|admin at' \
    | tail -8 | sed 's/^/       /'
  docker inspect "${CID%% *}" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null \
    | grep -E '^(GNB_METRICS_|PTP_DEVICE|ADMIN_URL|RAN_RAW)' | sed 's/^/       /'
fi

# --- 4. what the admin sees ------------------------------------------------
say "4. admin view"
if [ -z "${INFRA_HOST:-}" ]; then
  warn "INFRA_HOST unset; skipped"
else
  STATE=$(curl -sf --max-time 5 "http://$INFRA_HOST:8099/api/v1/state" || true)
  if [ -z "$STATE" ]; then
    bad "admin not reachable at http://$INFRA_HOST:8099"
  else
    STATE="$STATE" python3 - <<'PY'
import json, os
s = json.loads(os.environ["STATE"])
gnbs = [n for n in s.get("nodes", []) if n.get("node_type") == "gnb"]
if not gnbs:
    print("  \033[31mFAIL\033[0m no gNB node connected to the admin")
for n in gnbs:
    p, c = n.get("params") or {}, n.get("counters") or {}
    print("  node %s: state=%s online=%s run=%s"
          % (n.get("node_id"), n.get("state"), n.get("online"), n.get("run_id")))
    print("       source=%s transport=%s bind=%s"
          % (p.get("source"), p.get("transport"), p.get("bind")))
    if p.get("ws_last_error"):
        print("       \033[33mws_last_error\033[0m: %s" % p["ws_last_error"])
    print("       ptp_enabled=%s %s" % (p.get("ptp_enabled"), p.get("ptp_error") or ""))
    print("       datagrams=%s malformed=%s posted=%s post_failures=%s"
          % (c.get("datagrams"), c.get("malformed"), c.get("batches_posted"), c.get("post_failures")))
for f in s.get("findings", []):
    if f.get("code", "").startswith("WF_GNB"):
        print("  \033[33m%s\033[0m %s" % (f["code"], f.get("message")))
        print("       %s" % (f.get("remedy") or ""))
PY
  fi
fi

# --- 5. the newest run on disk ---------------------------------------------
say "5. newest RAN run on disk ($RUNS)"
LATEST=$(ls -t "$RUNS"/*/ran/samples.csv 2>/dev/null | head -1)
if [ -z "$LATEST" ]; then
  warn "no runs/*/ran/ yet — start a run on the admin page"
else
  DIR=$(dirname "$LATEST")
  ROWS=$(( $(wc -l < "$LATEST") - 1 ))
  ok "$DIR: $ROWS report(s) in samples.csv"
  if [ -s "$DIR/reports.jsonl" ]; then
    python3 - "$DIR/reports.jsonl" <<'PY'
import json, sys
shapes, n = {}, 0
for line in open(sys.argv[1]):
    try:
        r = json.loads(line)
    except ValueError:
        continue
    n += 1
    key = "cells[] (25.x)" if "cells" in r else "ue_list[] (<=24.x)" if "ue_list" in r else ",".join(sorted(r)[:4])
    shapes[key] = shapes.get(key, 0) + 1
print(f"       reports.jsonl: {n} report(s); layouts: {shapes}")
PY
    echo "       capture it:  bash scripts/ran-fixture.sh $(basename "$(dirname "$DIR")") <srsran-version> <udp|ws>"
  else
    warn "no reports.jsonl beside it (collector older than RAN_RAW_REPORTS, or set to 0)"
  fi
fi
echo
