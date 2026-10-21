"""A stand-in for mec-cast-e2-bridge: the JSON E2 feed protocol on stdin/stdout.

Behaves as the real bridge does in the cases the adapter must handle:
style 4 per-UE indications, a refused subscription, an ack and a failure, and
(FAKE_BRIDGE_ABORT=1) FlexRIC's abort on a RIC Subscription Failure.
"""

import json
import os
import sys
import threading
import time

NODE = "gnbd_001_01_19b"
subs = {}
lock = threading.Lock()


def send(obj):
    with lock:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


def ticker():
    t = 0
    while True:
        time.sleep(0.05)
        t += 1
        for sid in list(subs):
            send(
                {
                    "op": "indication",
                    "id": sid,
                    "indication": {
                        "e2_node_id": NODE,
                        "collect_start_ns": 1_700_000_000_000_000_000 + t,
                        "meas": {
                            "ueMeasData": {
                                "0": {"measData": {"DRB.UEThpUl": [5000 + t]}, "granulPeriod": 1000}
                            }
                        },
                    },
                }
            )


threading.Thread(target=ticker, daemon=True).start()
print("FlexRIC chatter goes to stderr in the real bridge", file=sys.stderr, flush=True)
for line in sys.stdin:
    msg = json.loads(line)
    op = msg.get("op")
    if op == "nodes":
        send({"op": "nodes", "nodes": [NODE]})
    elif op == "subscribe":
        if os.environ.get("FAKE_BRIDGE_ABORT"):
            os.abort()
        if "DRB.Unsupported" in msg.get("metrics", []):
            send(
                {
                    "op": "subscribe_failed",
                    "id": msg["id"],
                    "detail": "the E2 node does not offer DRB.Unsupported in report style 4",
                }
            )
        else:
            subs[msg["id"]] = msg
            send({"op": "subscribed", "id": msg["id"]})
    elif op == "unsubscribe":
        subs.pop(msg.get("id"), None)
    elif op == "control":
        ok = msg.get("max_prb_ratio") != 13  # 13: the fake gNB refuses
        sliced = (msg.get("plmn"), msg.get("sst"), msg.get("sd"))
        send(
            {
                "op": "control_ack",
                "id": msg["id"],
                "ok": ok,
                "detail": "" if ok else f"RIC_CONTROL_FAILURE slice={sliced}",
            }
        )
