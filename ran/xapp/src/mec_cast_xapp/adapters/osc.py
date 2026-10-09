"""E2Port on the O-RAN SC near-RT RIC, via srsRAN's oran-sc-ric (ADR-0010).

**Runs inside oran-sc-ric's ``python_xapp_runner`` container.** That is not a
convenience: the RIC's static routing table (``ric/configs/routes.rtg``)
delivers RIC_INDICATION to 10.0.2.20:4560-4562 and RIC_CONTROL_ACK/FAILURE to
10.0.2.20:4560 — the runner's address. ``deploy/lab/ric/ric.sh xapp`` puts
this package there.

What it uses, and how:

* ``ricxappframe`` (Apache-2.0) for RMR, the subscription REST client and E2AP
  indication decoding — directly, like any SDK.
* oran-sc-ric's ``lib`` (AGPL-3.0) for the E2SM-KPM and E2SM-RC ASN.1
  encoders/decoders, **imported at runtime from the RIC's own checkout**
  (``/opt/xApps/lib``) — never copied into this repository. Those encoders
  are the ones srsRAN's E2 agent is tested against; rewriting them is not a
  better use of a lab week.
* Its own receive loop (not ``xAppBase._run``), because that loop only prints
  RIC_CONTROL_ACK/FAILURE, and a control whose outcome cannot be recorded is
  a confound (ADR-0011).

Written blind against oran-sc-ric @621ade2 (ric.sh RIC_PIN); first verified in
the lab. Known constraint from that code: its RC PRB-quota encoder hardcodes
PLMN 00101, SST 1, SD 1 — the lab slice must match, or RC controls are
rejected (``controls_failed`` rises, WF_POLICY_NOT_APPLIED says so).
"""

from __future__ import annotations

import collections
import datetime as dt
import json
import logging
import os
import sys
import threading
import time
import urllib.request
from typing import Any, Dict, List, Optional

from ..core import E2Port

log = logging.getLogger("mec_cast_xapp.osc")

RIC_INDICATION = 12050
RIC_CONTROL_ACK = 12041
RIC_CONTROL_FAILURE = 12042
CONTROL_TIMEOUT_S = 5.0


def _to_ns(value: Any) -> int:
    """colletStartTime as decoded by extract_hdr_info: a datetime."""
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return int(value.timestamp() * 1e6) * 1000
    if isinstance(value, (int, float)):
        return int(value)
    return 0


class OscPort(E2Port):
    name = "osc"

    def __init__(
        self,
        *,
        xapps_dir: Optional[str] = None,
        http_port: Optional[int] = None,
        rmr_port: Optional[int] = None,
        e2mgr: Optional[str] = None,
    ):
        xapps_dir = xapps_dir or os.environ.get("OSC_XAPPS_DIR") or "/opt/xApps"
        if xapps_dir not in sys.path:
            sys.path.insert(0, xapps_dir)
        try:
            from lib.xAppBase import xAppBase  # oran-sc-ric, at runtime only
            from ricxappframe.e2ap.asn1 import IndicationMsg
            from ricxappframe.xapp_frame import rmr
        except ImportError as e:
            raise SystemExit(
                f"E2_ADAPTER=osc needs oran-sc-ric's xApp runner ({e}). Run it there: "
                "bash deploy/lab/ric/ric.sh xapp"
            ) from e
        self._rmr = rmr
        self._IndicationMsg = IndicationMsg
        self.e2mgr = e2mgr or f"http://{os.environ.get('E2MGR_IP', '10.0.2.11')}:3800"
        # 4560 is the only port the routing table sends control acks to.
        self.base = xAppBase(
            None,
            http_port or int(os.environ.get("XAPP_HTTP_PORT") or 8093),
            rmr_port or int(os.environ.get("XAPP_RMR_PORT") or 4560),
        )
        self.base.running = True
        self._pending: collections.deque = collections.deque()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._loop, name="osc-rmr", daemon=True)
        self._thread.start()

    # --- receive loop ---------------------------------------------------------

    def _loop(self) -> None:
        rmr = self._rmr
        while self.base.running:
            try:
                sbuf = rmr.rmr_torcv_msg(self.base.rmr_client, None, 100)
                summary = rmr.message_summary(sbuf)
            except Exception:  # noqa: BLE001 - a receive timeout is normal
                self._expire_controls()
                continue
            try:
                if summary[rmr.RMR_MS_MSG_STATE] == 0:
                    mtype = summary["message type"]
                    if mtype == RIC_INDICATION:
                        self._on_indication(summary, rmr.get_payload(sbuf))
                    elif mtype in (RIC_CONTROL_ACK, RIC_CONTROL_FAILURE):
                        self._on_control(mtype == RIC_CONTROL_ACK)
            except Exception:
                log.exception("rmr message handling failed")
            finally:
                rmr.rmr_free_msg(sbuf)
            self._expire_controls()

    def _on_indication(self, summary: Dict[str, Any], data: bytes) -> None:
        sub = self.base.my_subscriptions.get(summary["subscription id"])
        if sub is None or sub.callback_func is None:
            return
        ind = self._IndicationMsg()
        ind.decode(data)
        sub.callback_func(
            str(summary["meid"].decode("utf-8")), summary["subscription id"], ind, None
        )

    def _on_control(self, ok: bool) -> None:
        with self._lock:
            entry = self._pending.popleft() if self._pending else None
        if entry is not None and entry[1] is not None:
            entry[1](ok, "" if ok else "RIC_CONTROL_FAILURE")

    def _expire_controls(self) -> None:
        now = time.monotonic()
        while True:
            with self._lock:
                if not self._pending or now - self._pending[0][0] < CONTROL_TIMEOUT_S:
                    return
                _, cb = self._pending.popleft()
            if cb is not None:
                cb(False, f"no RIC_CONTROL_ACK within {CONTROL_TIMEOUT_S:.0f} s")

    # --- E2Port -----------------------------------------------------------------

    def connected_nodes(self) -> List[str]:
        try:
            with urllib.request.urlopen(f"{self.e2mgr}/v1/nodeb/states", timeout=3) as r:
                nodes = json.load(r)
        except Exception as e:  # noqa: BLE001 - "no answer" means none connected
            log.debug("e2mgr: %s", e)
            return []
        return [n["inventoryName"] for n in nodes if n.get("connectionStatus") == "CONNECTED"]

    def subscribe_kpm(self, node, style, metrics, ue_ids, period_ms, callback) -> str:
        kpm = self.base.e2sm_kpm

        def on_raw(agent: str, sub_id: Any, ind: Any, _unused: Any) -> None:
            hdr, msg = kpm.unpack_ric_indication(ind)
            hdr = kpm.extract_hdr_info(hdr)
            meas = kpm.extract_meas_data(msg)
            indication = {
                "e2_node_id": agent,
                "collect_start_ns": _to_ns(hdr.get("colletStartTime")),
                "meas": meas,
            }
            if style == 2 and ue_ids:
                indication["ue_id"] = ue_ids[0]
            callback(indication)

        before = {s.subscription_id for s in self.base.my_subscriptions.values()}
        granul = period_ms
        if style == 1:
            kpm.subscribe_report_service_style_1(node, period_ms, metrics, granul, on_raw)
        elif style == 2:
            kpm.subscribe_report_service_style_2(
                node, period_ms, ue_ids[0], metrics, granul, on_raw
            )
        elif style == 5:
            ids = list(ue_ids)
            if len(ids) < 2:
                # E2SM-KPM style 5 needs at least two UE ids; oran-sc-ric's own
                # monitor pads with a dummy the same way.
                ids.append(ids[0] + 1)
            kpm.subscribe_report_service_style_5(node, period_ms, ids, metrics, granul, on_raw)
        else:
            raise ValueError(f"KPM report style {style} is not wired in the osc adapter (1, 2, 5)")
        after = {s.subscription_id for s in self.base.my_subscriptions.values()}
        new = after - before
        return str(next(iter(new))) if new else ""

    def unsubscribe(self, handle: str) -> None:
        if not handle:
            return
        self.base.unsubscribe(handle)
        for key, sub in list(self.base.my_subscriptions.items()):
            if str(sub.subscription_id) == handle:
                del self.base.my_subscriptions[key]

    def control_prb_quota(self, node, ue_id, min_ratio, max_ratio, dedicated_ratio, callback=None):
        with self._lock:
            self._pending.append((time.monotonic(), callback))
        self.base.e2sm_rc.control_slice_level_prb_quota(
            node,
            int(ue_id),
            int(min_ratio),
            int(max_ratio),
            dedicated_prb_ratio=int(dedicated_ratio),
            ack_request=1,
        )

    def close(self) -> None:
        # Not xAppBase.stop(): it ends in sys.exit().
        try:
            self.base.unsubscribe_all()
        except Exception:  # noqa: BLE001
            pass
        self.base.running = False
        self._thread.join(timeout=2)
        try:
            self.base.httpServer.stop()
            self._rmr.rmr_close(self.base.rmr_client)
        except Exception:  # noqa: BLE001
            pass
