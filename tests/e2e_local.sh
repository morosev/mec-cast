#!/bin/bash
# End-to-end local test: starts server + 2 clients, establishes a call,
# enables delay measurement, prints the delay report, then tears down.
#
# Usage: ./tests/e2e_local.sh [duration_seconds]
#   duration_seconds: how long to keep the call active (default: 10)
#
# Prerequisites:
#   - Server deps installed (cd server && npm install)
#   - Client addon built    (cd client && ./build.sh)
#   - Port 8080 free

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DURATION=${1:-10}

SERVER_PID=""
C1_PID=""
C2_PID=""
C1_LOG=$(mktemp)
C2_LOG=$(mktemp)

cleanup() {
  local pids=($SERVER_PID $C1_PID $C2_PID)
  for pid in "${pids[@]}"; do
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
    fi
  done
  wait 2>/dev/null || true
  rm -f "$C1_LOG" "$C2_LOG"
}
trap cleanup EXIT

echo "=== MEC-Cast E2E Local Test ==="
echo "Duration: ${DURATION}s"
echo ""

# --- Start signaling server ---
cd "$ROOT_DIR/server"
node server.js &
SERVER_PID=$!
sleep 2

if ! kill -0 $SERVER_PID 2>/dev/null; then
  echo "FAIL: Server did not start (port 8080 in use?)"
  exit 1
fi
echo "[OK] Server started (PID=$SERVER_PID)"

# --- Start Client 1 (alice): connect, wait for peer, call ---
cd "$ROOT_DIR/client"
(
  sleep 0; echo "connect alice"
  sleep 4; echo "call"
  sleep "$DURATION"; echo "delay on"
  sleep 3; echo "quit"
) | node client.js > "$C1_LOG" 2>&1 &
C1_PID=$!

# --- Start Client 2 (bob): connect, auto-answers, enable delay ---
(
  sleep 1; echo "connect bob"
  # auto_answer is true in config, so the call is answered automatically
  sleep $((DURATION + 2)); echo "delay on"
  sleep 3; echo "quit"
) | node client.js > "$C2_LOG" 2>&1 &
C2_PID=$!

echo "[OK] Clients started (alice PID=$C1_PID, bob PID=$C2_PID)"
echo "     Waiting ${DURATION}s for call + measurement..."

# Wait for clients to finish
wait $C1_PID 2>/dev/null || true
wait $C2_PID 2>/dev/null || true

echo ""
echo "=== Results ==="
echo ""

# Check if call was established
if grep -q "P2P connection established" "$C1_LOG"; then
  echo "[OK] Alice: P2P connection established"
else
  echo "[FAIL] Alice: P2P connection NOT established"
  echo "--- Alice log ---"
  cat "$C1_LOG"
  exit 1
fi

if grep -q "P2P connection established" "$C2_LOG"; then
  echo "[OK] Bob: P2P connection established"
else
  echo "[FAIL] Bob: P2P connection NOT established"
  echo "--- Bob log ---"
  cat "$C2_LOG"
  exit 1
fi

# Check for delay report (bob receives video from alice)
if grep -q "DELAY REPORT" "$C2_LOG"; then
  echo "[OK] Bob: Delay report received"
  echo ""
  echo "--- Bob Delay Report ---"
  grep -A 20 "DELAY REPORT" "$C2_LOG" | grep -E "Network|Glass|Encoding|Decoding|Jitter|Sender|Receiver|Frames"
else
  echo "[WARN] Bob: No delay report (call may have been too short)"
fi

# Check alice delay report
if grep -q "DELAY REPORT" "$C1_LOG"; then
  echo ""
  echo "--- Alice Delay Report ---"
  grep -A 20 "DELAY REPORT" "$C1_LOG" | grep -E "Network|Glass|Encoding|Decoding|Jitter|Sender|Receiver|Frames"
fi

echo ""
echo "=== E2E Test PASSED ==="
