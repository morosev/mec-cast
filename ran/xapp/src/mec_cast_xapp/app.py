"""The xApp process: admin client, E2 adapter, capabilities, run lifecycle.

    python -m mec_cast_xapp            # or: mec-cast-xapp

Environment:

  E2_ADAPTER         osc | sim                       (osc)
  SIM_E2             gnb-sim's E2 feed, host:port    (gnb-sim:8002; sim adapter)
  XAPP_CAPABILITIES  comma-separated                 (kpm_monitor,rc_control)
  ADMIN_URL          ws://<infra>:8099/ws/node; empty = standalone under RUN_ID
  RUN_ID             standalone run id               (dev-run)
  RUNS_DIR           output base                     (runs)
  LOGGING_URL        logging service                 (none: files only)
  CELL               radio cell, as the other nodes report it
  XAPP_HOST          node identity host              (hostname)
  KPM_STYLE, KPM_METRICS, KPM_UE_IDS, E2_NODE_ID     defaults for run params

Like every node here: with no ADMIN_URL it records immediately under RUN_ID;
with one, the admin names and scopes the runs (ADR-0007). Run parameters from
run.start override the environment defaults; ``ran_policy`` among them goes to
every capability's ``on_policy`` (ADR-0011).
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
from typing import Any, Dict, List, Optional

from . import __version__
from .core import Capability, E2Port, XappContext, load_capabilities
from .recording import RunRecorder, env_runs_dir

log = logging.getLogger("mec_cast_xapp")

STATUS_EVERY_S = 2.0
TICK_S = 0.2


def make_port(name: str) -> E2Port:
    if name == "sim":
        from .adapters.sim import SimPort

        return SimPort(os.environ.get("SIM_E2") or "gnb-sim:8002")
    if name == "osc":
        from .adapters.osc import OscPort

        return OscPort()
    raise SystemExit(f"E2_ADAPTER={name!r}: expected osc or sim")


def env_params() -> Dict[str, Any]:
    """Run-parameter defaults from the environment; run.start args win."""
    out: Dict[str, Any] = {}
    if os.environ.get("KPM_STYLE"):
        out["kpm_style"] = int(os.environ["KPM_STYLE"])
    if os.environ.get("KPM_METRICS"):
        out["kpm_metrics"] = [m for m in os.environ["KPM_METRICS"].split(",") if m]
    if os.environ.get("KPM_UE_IDS"):
        out["kpm_ue_ids"] = [int(u) for u in os.environ["KPM_UE_IDS"].split(",") if u]
    if os.environ.get("E2_NODE_ID"):
        out["e2_node_id"] = os.environ["E2_NODE_ID"]
    return out


class Xapp:
    def __init__(
        self,
        port: E2Port,
        capabilities: List[Capability],
        *,
        runs_dir: str,
        logging_url: Optional[str],
        defaults: Optional[Dict[str, Any]] = None,
        admin: Any = None,
    ):
        self.port = port
        self.capabilities = capabilities
        self.runs_dir = runs_dir
        self.logging_url = logging_url
        self.defaults = defaults or {}
        self.admin = admin
        self.ctx: Optional[XappContext] = None
        self._lock = threading.RLock()
        self._booted = {c.name: False for c in capabilities}
        self._admin_seen = time.monotonic()

    # --- run lifecycle ------------------------------------------------------

    @property
    def run_id(self) -> Optional[str]:
        return self.ctx.run_id if self.ctx else None

    def start_run(self, run_id: str, args: Optional[Dict[str, Any]] = None) -> None:
        with self._lock:
            if self.ctx is not None and self.ctx.run_id == run_id:
                return  # idempotent, as for every node
            if self.ctx is not None:
                self.stop_run()
            params = dict(self.defaults, **(args or {}))
            recorder = RunRecorder(self.runs_dir, run_id, self.logging_url)
            self.ctx = XappContext(self.port, run_id, params, recorder, time.time_ns)
            self.ctx.admin_lost_s = self.admin_lost_s
            log.info(
                "xapp recording run %s (capabilities: %s)",
                run_id,
                ", ".join(c.name for c in self.capabilities),
            )
            for cap in self.capabilities:
                self._guard(cap, "on_start", cap.on_start, self.ctx)
            if self.admin is not None:
                self.admin.update_identity(state="running", run_id=run_id)
            policy = params.get("ran_policy")
            if policy:
                self.apply_policy(policy)  # raises into the run.start ack

    def apply_policy(self, policy: Dict[str, Any]) -> None:
        """Every capability sees the policy; the first refusal is raised, so
        the admin's ack carries it. Unlike the other hooks this one is not
        swallowed: an invalid policy acknowledged as applied would be the
        worst outcome available."""
        with self._lock:
            if self.ctx is None:
                raise ValueError("ran.policy with no run active")
            self.ctx.params["ran_policy"] = policy
            errors = []
            for cap in self.capabilities:
                try:
                    cap.on_policy(self.ctx, policy)
                except Exception as e:  # noqa: BLE001 - collected and raised below
                    log.error("capability %s refused the policy: %s", cap.name, e)
                    errors.append(f"{cap.name}: {e}")
            if errors:
                raise ValueError("; ".join(errors))

    def admin_lost_s(self) -> float:
        """Seconds the admin has been unreachable; 0 if connected or absent."""
        if self.admin is None or self.admin.connected:
            self._admin_seen = time.monotonic()
            return 0.0
        return time.monotonic() - self._admin_seen

    def boot(self) -> None:
        """Run pending on_boot hooks; cheap once they have all succeeded."""
        for cap in self.capabilities:
            if not self._booted[cap.name]:
                try:
                    self._booted[cap.name] = bool(cap.on_boot(self.port))
                except Exception:
                    log.exception("capability %s.on_boot failed", cap.name)

    def stop_run(self) -> Dict[str, Any]:
        with self._lock:
            if self.ctx is None:
                return {}
            # Reverse order: control reverts before monitoring stops, so the
            # revert itself is observed and recorded (ADR-0011).
            for cap in reversed(self.capabilities):
                self._guard(cap, "on_stop", cap.on_stop, self.ctx)
            report = self.ctx.recorder.close()
            log.info("xapp stopped run %s: %s", self.ctx.run_id, report)
            self.ctx = None
            if self.admin is not None:
                self.admin.update_identity(state="idle", run_id=None)
            return report

    def tick(self) -> None:
        with self._lock:
            self.admin_lost_s()  # keeps the last-seen time current
            self.boot()
            if self.ctx is None:
                return
            for cap in self.capabilities:
                self._guard(cap, "tick", cap.tick, self.ctx)
            self.ctx.recorder.flush()

    @staticmethod
    def _guard(cap: Capability, hook: str, fn, *args) -> None:
        # One capability's failure must not take down the others or the run.
        try:
            fn(*args)
        except Exception:
            log.exception("capability %s.%s failed", cap.name, hook)

    # --- status ---------------------------------------------------------------

    def status(self, report: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        from mec_cast_admin_client import protocol as ap

        with self._lock:
            params: Dict[str, Any] = {
                "adapter": self.port.name,
                "capabilities": [c.name for c in self.capabilities],
                "version": __version__,
            }
            try:
                nodes = self.port.connected_nodes()
                params["e2_nodes"] = nodes
                params["e2_connected"] = bool(nodes)
            except Exception as e:  # noqa: BLE001 - the RIC may be restarting
                params["e2_connected"] = False
                params["e2_error"] = str(e)
            counters: Dict[str, int] = {}
            for cap in self.capabilities:
                params.update(cap.params())
                counters.update({k: int(v) for k, v in cap.status().items()})
            if self.ctx is not None:
                counters.update(self.ctx.recorder.logging_counters())
                params["out_leaf"] = "ran-kpm"
            return ap.status_payload(
                node_type=ap.NodeType.XAPP,
                state=ap.NodeState.RUNNING if self.ctx else ap.NodeState.IDLE,
                run_id=self.run_id,
                subscribed=bool(params.get("kpm_subscribed")),
                params=params,
                counters=counters,
                autostart=True,
                report=report or {},
            )

    # --- admin ----------------------------------------------------------------

    def drain_admin(self) -> None:
        from mec_cast_admin_client import protocol as ap

        for frame in self.admin.poll():
            payload = frame.get("payload") or {}
            kind = frame.get("type")
            if kind == ap.MessageType.WELCOME:
                active = payload.get("active_run")
                if active:
                    self.start_run(active["run_id"], active.get("params"))
                self.admin.publish_status(self.status())
            elif kind == ap.MessageType.COMMAND:
                self.apply_command(frame, payload)

    def apply_command(self, frame: Dict[str, Any], payload: Dict[str, Any]) -> None:
        from mec_cast_admin_client import protocol as ap

        command = payload.get("command")
        ok, error, report = True, None, None
        try:
            if command in (ap.CommandType.RUN_START, ap.CommandType.STREAM_START):
                if not payload.get("run_id"):
                    raise ValueError("run.start without a run_id")
                self.start_run(payload["run_id"], payload.get("args"))
            elif command in (ap.CommandType.RUN_STOP, ap.CommandType.STREAM_STOP):
                report = self.stop_run()
            elif command == "ran.policy":
                self.apply_policy((payload.get("args") or {}).get("ran_policy") or {})
            elif command != ap.CommandType.STATUS_REPORT:
                raise ValueError(f"unknown command {command!r}")
        except Exception as exc:  # noqa: BLE001 - a bad command must not kill the node
            ok, error = False, str(exc)
            log.error("admin command %s failed: %s", command, exc)
        self.admin.publish_ack(frame["msg_id"], ok=ok, error=error)
        self.admin.publish_status(self.status(report))


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="[xapp] %(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    port = make_port(os.environ.get("E2_ADAPTER") or "osc")
    caps = load_capabilities(
        (os.environ.get("XAPP_CAPABILITIES") or "kpm_monitor,rc_control").split(",")
    )
    admin_url = os.environ.get("ADMIN_URL") or ""
    admin = None
    if admin_url:
        from mec_cast_admin_client import AdminClient

        admin = AdminClient(
            node_type="xapp",
            host=os.environ.get("XAPP_HOST") or socket.gethostname(),
            url=admin_url,
            cell=os.environ.get("CELL", ""),
            version_sha=os.environ.get("VCS_REF", ""),
            version_tag=os.environ.get("VERSION", ""),
            pid=os.getpid(),
        )
        admin.update_identity(autostart=True)
        admin.start()

    app = Xapp(
        port,
        caps,
        runs_dir=env_runs_dir(),
        logging_url=os.environ.get("LOGGING_URL") or None,
        defaults=env_params(),
        admin=admin,
    )
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    log.info(
        "xapp %s up: adapter=%s capabilities=%s admin=%s",
        __version__,
        port.name,
        [c.name for c in caps],
        admin_url or "(standalone)",
    )
    # Before any run: a previous process's leftovers are undone first.
    app.boot()
    if admin is None:
        app.start_run(os.environ.get("RUN_ID") or "dev-run")

    next_status = 0.0
    try:
        while not stop.is_set():
            if admin is not None:
                app.drain_admin()
            app.tick()
            if admin is not None and time.monotonic() >= next_status:
                admin.publish_status(app.status())
                next_status = time.monotonic() + STATUS_EVERY_S
            stop.wait(TICK_S)
    finally:
        report = app.stop_run()
        if admin is not None:
            admin.goodbye(final_report=report)
        port.close()
    return 0
