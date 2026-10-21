"""E2Port over the JSON E2 feed protocol, whatever carries it.

The protocol is the one gnb-sim's E2 feed defines (ran/sim/gnb_sim.py): a few
JSON messages whose indications are already in the decoded shape the
normaliser reads (``e2_node_id``, ``collect_start_ns``, ``meas.measData`` or
``meas.ueMeasData``). Two things speak it:

* gnb-sim, over a WebSocket (adapters/sim.py), for tests without a radio;
* mec-cast-e2-bridge, a small FlexRIC xApp in C, over its stdin and stdout
  (adapters/flexric.py), because FlexRIC's Python SDK has no KPM or RC.

  -> {"op": "nodes"}
       <- {"op": "nodes", "nodes": [...]}
  -> {"op": "subscribe", "id", "node", "style", "metrics", "ue_ids", "period_ms"}
       <- {"op": "subscribed", "id"}
       or {"op": "subscribe_failed", "id", "detail"}
       <- {"op": "indication", "id", "indication": {...}}
  -> {"op": "unsubscribe", "id"}
  -> {"op": "control", "id", "node", "action": "prb_quota", "ue_id",
      "min_prb_ratio", "max_prb_ratio", "dedicated_prb_ratio"}
       <- {"op": "control_ack", "id", "ok", "detail"}

Subclasses provide the transport: ``_send`` and a reader that hands every
received message to ``_dispatch``.
"""

from __future__ import annotations

import itertools
import logging
import threading
from typing import Any, Dict, List, Optional

from ..core import ControlCallback, E2Port, IndicationCallback, SubscriptionRejected

log = logging.getLogger("mec_cast_xapp.jsonfeed")


class JsonFeedPort(E2Port):
    name = "jsonfeed"

    def __init__(self, timeout_s: float = 3.0):
        self.timeout_s = timeout_s
        self._ids = itertools.count(1)
        self._subs: Dict[str, IndicationCallback] = {}
        self._sub_requests: Dict[str, Dict[str, Any]] = {}
        self._controls: Dict[str, Optional[ControlCallback]] = {}
        self._replies: Dict[str, threading.Event] = {}
        self._reply_data: Dict[str, Any] = {}

    # --- transport (subclasses) ------------------------------------------------

    def _send(self, obj: Dict[str, Any]) -> None:
        raise NotImplementedError

    def _where(self) -> str:
        return self.name

    # --- protocol --------------------------------------------------------------

    def _dispatch(self, msg: Dict[str, Any]) -> None:
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
        elif op == "log":
            log.info("%s: %s", self._where(), msg.get("detail"))
        # A request waits for its reply under "<op>:<id>"; a refused
        # subscription answers under its own op, so it wakes the same waiter.
        reply = "subscribed" if op == "subscribe_failed" else op
        key = f"{reply}:{msg.get('id', '')}"
        if key in self._replies:
            self._reply_data[key] = msg
            self._replies[key].set()

    def _request(self, obj: Dict[str, Any], reply_op: str) -> Dict[str, Any]:
        key = f"{reply_op}:{obj.get('id', '')}"
        ev = self._replies[key] = threading.Event()
        try:
            self._send(obj)
            if not ev.wait(self.timeout_s):
                raise TimeoutError(f"no {reply_op} from {self._where()}")
            return self._reply_data.pop(key)
        finally:
            self._replies.pop(key, None)

    def _resubscribe(self) -> None:
        """After a reconnect or a restart of the far end, which forgets."""
        for sid, req in self._sub_requests.items():
            self._send(dict(req, op="subscribe", id=sid))

    # --- E2Port -----------------------------------------------------------------

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
        reply = self._request(dict(req, op="subscribe", id=sid), "subscribed")
        if reply.get("op") == "subscribe_failed":
            self._subs.pop(sid, None)
            raise SubscriptionRejected(
                f"E2 node {node} rejected the KPM subscription (style {style}, "
                f"metrics {metrics}): {reply.get('detail') or 'no reason given'}"
            )
        self._sub_requests[sid] = req
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
