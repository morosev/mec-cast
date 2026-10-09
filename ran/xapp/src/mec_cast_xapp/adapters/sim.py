"""E2Port against gnb-sim's E2 feed (ran/sim/gnb_sim.py, SIM_E2_BIND).

For testing without a radio or a RIC. The feed speaks a small JSON protocol
whose indications are already in the decoded shape oran-sc-ric's
``extract_meas_data`` produces, so everything above the adapter — the
capabilities, the normaliser, the recorder, the admin — runs exactly the code
it runs in the lab.

One WebSocket, one reader thread. The connection is re-established on demand:
gnb-sim may start after the xApp, and may restart under it.
"""

from __future__ import annotations

import itertools
import json
import logging
import threading
from typing import Any, Dict, List, Optional

from ..core import ControlCallback, E2Port, IndicationCallback

log = logging.getLogger("mec_cast_xapp.sim")


class SimPort(E2Port):
    name = "sim"

    def __init__(self, target: str, timeout_s: float = 3.0):
        self.url = target if "://" in target else f"ws://{target}"
        self.timeout_s = timeout_s
        self._ws = None
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._subs: Dict[str, IndicationCallback] = {}
        self._sub_requests: Dict[str, Dict[str, Any]] = {}
        self._controls: Dict[str, Optional[ControlCallback]] = {}
        self._replies: Dict[str, threading.Event] = {}
        self._reply_data: Dict[str, Any] = {}
        self._reader: Optional[threading.Thread] = None

    # --- connection ---------------------------------------------------------

    def _connect(self):
        with self._lock:
            if self._ws is not None:
                return self._ws
            from websockets.sync.client import connect

            # Entered explicitly: websockets >= 14 wants connect() used as a
            # context manager, and this connection outlives any one block.
            self._cm = connect(self.url, open_timeout=self.timeout_s)
            self._ws = self._cm.__enter__()
            self._reader = threading.Thread(target=self._read, args=(self._ws,), daemon=True)
            self._reader.start()
            # Resubscribe after a reconnect: a restarted gnb-sim forgets.
            for sid, req in self._sub_requests.items():
                self._ws.send(json.dumps(dict(req, op="subscribe", id=sid)))
            return self._ws

    def _send(self, obj: Dict[str, Any]) -> None:
        ws = self._connect()
        try:
            ws.send(json.dumps(obj))
        except Exception:
            with self._lock:
                self._ws = None
            raise

    def _read(self, ws) -> None:
        try:
            for raw in ws:
                msg = json.loads(raw)
                op = msg.get("op")
                if op == "indication":
                    cb = self._subs.get(str(msg.get("id")))
                    if cb is not None:
                        try:
                            cb(msg["indication"])
                        except Exception:
                            log.exception("indication callback failed")
                elif op == "control_ack":
                    cb = self._controls.pop(str(msg.get("id")), None)
                    if cb is not None:
                        cb(bool(msg.get("ok")), str(msg.get("detail") or ""))
                key = f"{op}:{msg.get('id', '')}"
                if key in self._replies:
                    self._reply_data[key] = msg
                    self._replies[key].set()
        except Exception as e:  # noqa: BLE001 - the connection dropped
            log.warning("sim e2 feed closed: %s", e)
        finally:
            with self._lock:
                if self._ws is ws:
                    self._ws = None

    def _request(self, obj: Dict[str, Any], reply_op: str) -> Dict[str, Any]:
        key = f"{reply_op}:{obj.get('id', '')}"
        ev = self._replies[key] = threading.Event()
        try:
            self._send(obj)
            if not ev.wait(self.timeout_s):
                raise TimeoutError(f"no {reply_op} from {self.url}")
            return self._reply_data.pop(key)
        finally:
            self._replies.pop(key, None)

    # --- E2Port ---------------------------------------------------------------

    def connected_nodes(self) -> List[str]:
        try:
            return list(self._request({"op": "nodes"}, "nodes").get("nodes") or [])
        except Exception as e:  # noqa: BLE001 - "no RIC" is an answer, not a crash
            log.debug("nodes: %s", e)
            return []

    def subscribe_kpm(self, node, style, metrics, ue_ids, period_ms, callback) -> str:
        sid = f"kpm-{next(self._ids)}"
        req = {
            "style": style,
            "metrics": metrics,
            "ue_ids": ue_ids,
            "period_ms": period_ms,
            "node": node,
        }
        self._subs[sid] = callback
        self._sub_requests[sid] = req
        self._request(dict(req, op="subscribe", id=sid), "subscribed")
        return sid

    def unsubscribe(self, handle: str) -> None:
        self._subs.pop(handle, None)
        self._sub_requests.pop(handle, None)
        try:
            self._send({"op": "unsubscribe", "id": handle})
        except Exception:  # noqa: BLE001 - nothing to unsubscribe from
            pass

    def control_prb_quota(self, node, ue_id, min_ratio, max_ratio, dedicated_ratio, callback=None):
        cid = f"ctl-{next(self._ids)}"
        self._controls[cid] = callback
        self._send(
            {
                "op": "control",
                "id": cid,
                "action": "prb_quota",
                "node": node,
                "ue_id": ue_id,
                "min_prb_ratio": min_ratio,
                "max_prb_ratio": max_ratio,
                "dedicated_prb_ratio": dedicated_ratio,
            }
        )

    def close(self) -> None:
        with self._lock:
            ws, self._ws = self._ws, None
            cm, self._cm = getattr(self, "_cm", None), None
        if ws is not None and cm is not None:
            try:
                cm.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
