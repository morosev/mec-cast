"""E2SM-KPM monitoring: subscribe for the run, record every indication.

Run parameters (all optional, carried by run.start):

  kpm_style      report style 1-5               (5: per UE, the useful one)
  kpm_metrics    list of KPM measurement names  (every non-dummy one srsRAN exposes)
  kpm_ue_ids     E2 UE ids for styles 2 and 5   ([0])
  kpm_period_ms  report period                  (1000: srsRAN's minimum)
  e2_node_id     which E2 node                  (the first connected)

srsRAN's agent reports nothing below 1 s. That is the granularity of this
source, and why the JSON tap stays (ADR-0010).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from ..core import Capability, XappContext

log = logging.getLogger("mec_cast_xapp.kpm")

DEFAULT_METRICS: List[str] = [
    "DRB.UEThpDl",
    "DRB.UEThpUl",
    "DRB.RlcSduTransmittedVolumeDL",
    "DRB.RlcSduTransmittedVolumeUL",
    "DRB.RlcPacketDropRateDl",
    "DRB.PacketSuccessRateUlgNBUu",
]
RETRY_S = 5.0


class KpmMonitor(Capability):
    name = "kpm_monitor"

    def __init__(self) -> None:
        self.handle: Optional[str] = None
        self.node: Optional[str] = None
        self.style = 5
        self.last_error: Optional[str] = None
        self._next_try = 0.0
        self._ctx: Optional[XappContext] = None

    def on_start(self, ctx: XappContext) -> None:
        self._ctx = ctx
        self.handle = None
        self._next_try = 0.0
        self._try_subscribe(ctx)

    def _try_subscribe(self, ctx: XappContext) -> None:
        if self.handle is not None or time.monotonic() < self._next_try:
            return
        p = ctx.params
        self.style = int(p.get("kpm_style") or 5)
        metrics = list(p.get("kpm_metrics") or DEFAULT_METRICS)
        ue_ids = [int(u) for u in (p.get("kpm_ue_ids") or [0])]
        period = max(1000, int(p.get("kpm_period_ms") or 1000))
        node = ctx.node()
        if node is None:
            self.last_error = "no E2 node connected to the RIC"
            self._next_try = time.monotonic() + RETRY_S
            return
        try:
            self.handle = ctx.port.subscribe_kpm(
                node, self.style, metrics, ue_ids, period, self._on_indication
            )
            self.node = node
            self.last_error = None
            log.info("kpm: subscribed node=%s style=%d metrics=%s", node, self.style, metrics)
        except Exception as e:  # noqa: BLE001 - retried; the reason is in status
            self.last_error = f"subscribe failed: {e}"
            self._next_try = time.monotonic() + RETRY_S
            log.warning("kpm: %s", self.last_error)

    def _on_indication(self, indication: Dict[str, Any]) -> None:
        ctx = self._ctx
        if ctx is None or ctx.recorder is None:
            return
        if self.style == 2 and "ue_id" not in indication:
            # Style 2 reports one UE that the indication does not name.
            ids = ctx.params.get("kpm_ue_ids") or [0]
            indication = dict(indication, ue_id=ids[0])
        ctx.recorder.record_indication(indication)

    def tick(self, ctx: XappContext) -> None:
        self._try_subscribe(ctx)

    def on_stop(self, ctx: XappContext) -> None:
        if self.handle is not None:
            try:
                ctx.port.unsubscribe(self.handle)
            except Exception as e:  # noqa: BLE001 - the RIC may already be gone
                log.warning("kpm: unsubscribe failed: %s", e)
        self.handle = None
        self._ctx = None

    def status(self) -> Dict[str, Any]:
        rec = self._ctx.recorder if self._ctx else None
        out: Dict[str, Any] = {}
        if rec is not None:
            out["indications"] = rec.indications
            out["rows"] = rec.rows
            if rec.last_throughput is not None:
                dl, ul = rec.last_throughput
                # Integers: the admin's counters are dict[str, int].
                out["ue_dl_throughput_bps"] = round(dl)
                out["ue_ul_throughput_bps"] = round(ul)
        return out

    def params(self) -> Dict[str, Any]:
        return {
            "kpm_style": self.style,
            "e2_node_id": self.node,
            "kpm_subscribed": self.handle is not None,
            "kpm_error": self.last_error,
        }
