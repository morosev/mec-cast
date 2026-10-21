"""E2Port on FlexRIC (OpenAirInterface), through mec-cast-e2-bridge.

FlexRIC's Python SDK covers only FlexRIC's own service models (MAC, RLC, PDCP,
GTP, its slicing), which only the OAI gNB implements; E2SM-KPM and E2SM-RC are
in its C SDK alone. So the E2 work is done by mec-cast-e2-bridge
(ran/xapp/flexric/), a small FlexRIC xApp in C that speaks the JSON E2 feed
protocol (adapters/jsonfeed.py) on its stdin and stdout, and this adapter runs
it as a child process. Everything above it -- capabilities, normaliser,
recorder, admin -- is the code the oran-sc-ric path runs.

Environment:

  FLEXRIC_BRIDGE   the bridge binary          (mec-cast-e2-bridge, on PATH)
  FLEXRIC_CONF     FlexRIC xApp config file   (/usr/local/etc/flexric/flexric.conf):
                   NEAR_RIC_IP, where the nearRT-RIC's E42 port (36422) is
  FLEXRIC_SLICE    the slice a PRB quota names, PLMN:SST:SD (00101:1:1). Not
                   hard-coded as oran-sc-ric's encoder is: the bridge builds
                   the RC message itself.

A bridge that exits is started again on the next request, and the KPM
subscriptions it held are re-sent: a run survives a bridge crash with a gap.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import threading
from typing import Any, Dict, List, Optional

from .jsonfeed import JsonFeedPort

log = logging.getLogger("mec_cast_xapp.flexric")

DEFAULT_CONF = "/usr/local/etc/flexric/flexric.conf"


def parse_slice(text: str) -> Dict[str, Any]:
    """``PLMN:SST:SD`` -> {"plmn", "sst", "sd"}; SD in hex or decimal, as written."""
    plmn, sst, sd = (text or "00101:1:1").split(":")
    if not (plmn.isdigit() and len(plmn) in (5, 6)):
        raise ValueError(f"FLEXRIC_SLICE PLMN {plmn!r}: MCC+MNC, 5 or 6 digits")
    return {
        "plmn": plmn,
        "sst": int(sst, 0),
        "sd": int(sd, 16) if sd.lower().startswith("0x") else int(sd),
    }


class FlexricPort(JsonFeedPort):
    name = "flexric"

    def __init__(
        self,
        command: Optional[List[str]] = None,
        timeout_s: float = 10.0,
        slice_spec: Optional[str] = None,
    ):
        # FlexRIC answers a subscription only after the E2 node has: longer
        # than gnb-sim's instant reply.
        super().__init__(timeout_s)
        if command is None:
            binary = os.environ.get("FLEXRIC_BRIDGE") or "mec-cast-e2-bridge"
            conf = os.environ.get("FLEXRIC_CONF") or DEFAULT_CONF
            command = [*shlex.split(binary), "-c", conf]
        self.command = command
        self.slice = parse_slice(slice_spec or os.environ.get("FLEXRIC_SLICE") or "00101:1:1")
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()

    def _where(self) -> str:
        return "mec-cast-e2-bridge"

    # --- the child process --------------------------------------------------------

    def _ensure(self) -> subprocess.Popen:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return self._proc
            restarted = self._proc is not None
            log.info("starting %s", " ".join(self.command))
            self._proc = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            proc = self._proc
            threading.Thread(target=self._read, args=(proc,), daemon=True).start()
            threading.Thread(target=self._drain, args=(proc,), daemon=True).start()
        if restarted and self._sub_requests:
            log.warning("bridge restarted; re-sending %d subscription(s)", len(self._sub_requests))
            for sid, req in list(self._sub_requests.items()):
                self._write(proc, dict(req, op="subscribe", id=sid))
        return proc

    @staticmethod
    def _write(proc: subprocess.Popen, obj: Dict[str, Any]) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def _send(self, obj: Dict[str, Any]) -> None:
        if obj.get("op") == "control":
            obj = dict(obj, **self.slice)
        self._write(self._ensure(), obj)

    def _read(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                log.info("bridge: %s", line)  # FlexRIC's own prints, if any reach stdout
                continue
            self._dispatch(msg)
        code = proc.wait()
        log.warning("bridge exited (code %s)", code)
        # FlexRIC v3.0.0 aborts on a RIC Subscription Failure (its handler is
        # an unimplemented assert). A subscription still waiting for its answer
        # when the bridge dies was refused: say so, so the xApp backs off
        # instead of retrying straight into the same abort.
        for key in [k for k in self._replies if k.startswith("subscribed:")]:
            self._dispatch(
                {
                    "op": "subscribe_failed",
                    "id": key.split(":", 1)[1],
                    "detail": f"mec-cast-e2-bridge exited (code {code}) before the answer: "
                    "FlexRIC aborts on a RIC Subscription Failure. Check the metric list "
                    "and report style against the gNB's E2 log",
                }
            )

    @staticmethod
    def _drain(proc: subprocess.Popen) -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            if line.strip():
                log.info("bridge: %s", line.rstrip())

    def close(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            try:
                assert proc.stdin is not None
                proc.stdin.close()  # EOF: the bridge unsubscribes and exits
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                proc.kill()


# --- a stand-alone KPM monitor: `flexric.sh kpm` --------------------------------


def monitor(argv: Optional[List[str]] = None) -> int:
    """Subscribe, print each indication, report the verdict. Proves E2 before the xApp.

    FlexRIC's own xapp_kpm_moni subscribes to every report style the E2 node
    offers and has no builder for styles 2, 3 and 5, so it cannot be the
    first test against a gNB that offers them. This goes through the same
    bridge the xApp uses, with the same style and metrics.
    """
    import argparse
    import time

    ap = argparse.ArgumentParser(prog="python3 -m mec_cast_xapp.adapters.flexric")
    ap.add_argument("--style", type=int, default=4)
    ap.add_argument("--metrics", default="DRB.UEThpDl,DRB.UEThpUl")
    ap.add_argument("--ue-ids", default="0")
    ap.add_argument("--period-ms", type=int, default=1000)
    ap.add_argument("--node", default="", help="E2 node; default the first connected")
    ap.add_argument(
        "--seconds", type=float, default=0, help="stop after this long; 0 = until Ctrl-C"
    )
    ap.add_argument("--nodes", action="store_true", help="list connected E2 nodes and exit")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    port = FlexricPort()
    try:
        nodes = port.connected_nodes()
        if a.nodes:
            for n in nodes:
                print(n, "CONNECTED")
            return 0
        node = a.node or (nodes[0] if nodes else "")
        if not node:
            print("ERROR: no E2 node is connected to FlexRIC. Check the gNB's e2: block.")
            return 1
        metrics = [m for m in a.metrics.split(",") if m]
        ue_ids = [int(u) for u in a.ue_ids.split(",") if u]
        count = [0]

        def show(ind: Dict[str, Any]) -> None:
            count[0] += 1
            meas = ind.get("meas") or {}
            per_ue = meas.get("ueMeasData") or {"node": {"measData": meas.get("measData") or {}}}
            for ue, block in per_ue.items():
                values = ", ".join(f"{k}={v}" for k, v in (block.get("measData") or {}).items())
                print(
                    f"RIC Indication from {ind.get('e2_node_id')} style {a.style}, "
                    f"UE {ue}: {values}"
                )

        print(f"==> node={node} style={a.style} metrics={metrics} period={a.period_ms} ms")
        try:
            port.subscribe_kpm(node, a.style, metrics, ue_ids, a.period_ms, show)
        except Exception as e:  # noqa: BLE001 - the verdict is the point
            print(f"--- verdict: subscription refused: {e}")
            return 1
        deadline = time.monotonic() + a.seconds if a.seconds > 0 else None
        try:
            while deadline is None or time.monotonic() < deadline:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        print(f"--- verdict: subscribed, {count[0]} indication(s) received")
        return 0 if count[0] else 2
    finally:
        port.close()


if __name__ == "__main__":
    raise SystemExit(monitor())
