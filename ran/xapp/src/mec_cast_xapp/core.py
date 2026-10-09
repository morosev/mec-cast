"""The RIC-agnostic heart of the xApp: ports, capabilities, the registry.

Two seams, and they are the whole design:

* :class:`E2Port` is everything a capability may ask of the RIC. An adapter
  implements it once per RIC (``adapters/osc.py``, ``adapters/sim.py``);
  capabilities never import a RIC SDK.
* :class:`Capability` is one behaviour (``kpm_monitor``, ``rc_control``, …).
  New behaviour is a new capability registered by name, never a fork of the
  main loop. Third parties register through the ``mec_cast_xapp.capabilities``
  entry-point group.

Python 3.8: the O-RAN SC xApp runner is python:3.8-slim.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Type

log = logging.getLogger("mec_cast_xapp")

#: Callback for decoded KPM indications. The dict is normalise_kpm's input:
#: ``{"e2_node_id", "collect_start_ns", "meas", ["ue_id"]}``.
IndicationCallback = Callable[[Dict[str, Any]], None]
#: Callback for a control outcome: (ok, detail).
ControlCallback = Callable[[bool, str], None]


class E2Port:
    """What a capability may ask of the RIC. Adapters implement all of it."""

    name = "abstract"

    def connected_nodes(self) -> List[str]:
        """E2 node ids the RIC reports as CONNECTED, best first."""
        raise NotImplementedError

    def subscribe_kpm(
        self,
        node: str,
        style: int,
        metrics: List[str],
        ue_ids: List[int],
        period_ms: int,
        callback: IndicationCallback,
    ) -> str:
        """Subscribe to E2SM-KPM; returns a subscription handle."""
        raise NotImplementedError

    def unsubscribe(self, handle: str) -> None:
        raise NotImplementedError

    def control_prb_quota(
        self,
        node: str,
        ue_id: int,
        min_ratio: int,
        max_ratio: int,
        dedicated_ratio: int,
        callback: Optional[ControlCallback] = None,
    ) -> None:
        """E2SM-RC Control Style 2, Action 6: slice-level PRB quota, percent."""
        raise NotImplementedError

    def close(self) -> None:
        pass


class Capability:
    """One behaviour of the xApp, bound to the run lifecycle.

    Every hook is optional. ``ctx`` is the :class:`XappContext`: the port,
    the run's recorder, the clock and the run parameters.
    """

    name = "abstract"

    def on_boot(self, port: E2Port) -> bool:
        """Once per process, before any run. Return False to be called again
        (e.g. the RIC is not reachable yet); True when done."""
        return True

    def on_start(self, ctx: XappContext) -> None:
        pass

    def on_policy(self, ctx: XappContext, policy: Dict[str, Any]) -> None:
        """A ``ran_policy`` arrived, at run start or mid-run. Raise to refuse
        it: the error goes back to the admin as a failed ack."""

    def on_stop(self, ctx: XappContext) -> None:
        pass

    def tick(self, ctx: XappContext) -> None:
        """Called about every 200 ms while a run is active."""

    def status(self) -> Dict[str, Any]:
        """Counters to merge into the node's status (integers only)."""
        return {}

    def params(self) -> Dict[str, Any]:
        """Params to merge into the node's status."""
        return {}


class XappContext:
    """What a capability sees of the running xApp."""

    def __init__(self, port: E2Port, run_id: str, params: Dict[str, Any], recorder: Any, now_ns):
        self.port = port
        self.run_id = run_id
        self.params = params
        self.recorder = recorder
        self.now_ns = now_ns
        #: Seconds since the admin was last reachable; 0 when connected or
        #: when there is no admin (standalone). Set by the app.
        self.admin_lost_s = lambda: 0.0

    def node(self) -> Optional[str]:
        """The E2 node to act on: ``params["e2_node_id"]`` or the first connected."""
        wanted = self.params.get("e2_node_id")
        if wanted:
            return str(wanted)
        nodes = self.port.connected_nodes()
        return nodes[0] if nodes else None


_BUILTIN: Dict[str, str] = {
    "kpm_monitor": "mec_cast_xapp.capabilities.kpm_monitor:KpmMonitor",
    "rc_control": "mec_cast_xapp.capabilities.rc_control:RcControl",
}


def _import(path: str) -> Type[Capability]:
    module, _, attr = path.partition(":")
    mod = __import__(module, fromlist=[attr])
    return getattr(mod, attr)


def available_capabilities() -> Dict[str, str]:
    """Built-ins plus anything registered under the entry-point group."""
    found = dict(_BUILTIN)
    try:
        from importlib.metadata import entry_points

        eps = entry_points()
        group = (
            eps.select(group="mec_cast_xapp.capabilities")
            if hasattr(eps, "select")
            else eps.get("mec_cast_xapp.capabilities", [])
        )
        for ep in group:
            found[ep.name] = ep.value
    except Exception:  # noqa: BLE001 - metadata is best-effort; built-ins suffice
        pass
    return found


def load_capabilities(names: List[str]) -> List[Capability]:
    """Instantiate capabilities by name, in order. Unknown names are an error:
    a typo must not silently run an xApp that does less than asked."""
    known = available_capabilities()
    out = []
    for name in names:
        name = name.strip()
        if not name:
            continue
        if name not in known:
            raise ValueError(f"unknown capability {name!r}; known: {sorted(known)}")
        out.append(_import(known[name])())
    return out
