"""The moving part: connections, runs, commands, and the periodic passes.

Everything stateful lives here so that :mod:`state`, :mod:`workflow` and
:mod:`protocol` can stay pure and testable. One instance per process, held on
``app.state`` — which is also why the service runs a single worker: a second
worker would hold a second, divergent view of the fleet.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from typing import Any

from . import __version__
from . import protocol as p
from .config import Settings
from .events import EventLog
from .registry import Registry
from .state import Action, Event, RunState, TransitionError, advance, allowed_actions, occupies_slot
from .store import Run, RunStore, utc_now, uuid7
from . import topology as topo
from .workflow import diagnose

logger = logging.getLogger(__name__)


class OrchestratorError(RuntimeError):
    """An operator action that cannot be carried out. The message is shown."""


class Orchestrator:
    def __init__(self, settings: Settings, store: RunStore) -> None:
        self._settings = settings
        self._store = store
        self.registry = Registry(offline_timeout_s=settings.offline_timeout_s)
        # Operator actions belong on the same timeline as the measurements.
        self.events = EventLog(settings.logging_url)

        self._runs: dict[str, Run] = {}
        self._node_sockets: dict[str, Any] = {}
        self._ui_sockets: set[Any] = set()

        self._starting_since: float | None = None
        #: Findings are computed once per supervise pass, not per snapshot.
        #: The "is this counter rising?" checks compare against the previous
        #: pass, so evaluating them at an arbitrary moment would read a counter
        #: as flat merely because no status had arrived since the last compare.
        self._findings: list[dict[str, Any]] = []
        self._dirty = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._admin_sha = os.environ.get("VCS_REF", "")
        # The declared topology, or the built-in role rules when no file
        # exists. Loaded once: a topology change is a deployment change, and
        # re-reading it mid-run would let the rules shift under a run that is
        # already being judged against them.
        self.topology = topo.load(settings.topology_path)
        if self.topology.declared:
            logger.info(
                "topology: %d node(s) across %d cell(s) declared in %s",
                len(self.topology.nodes),
                len(self.topology.cells),
                self.topology.source,
            )

    # --- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        self._runs = self._store.load()
        logger.info("loaded %d run(s) from %s", len(self._runs), self._settings.runs_dir)
        # A run left mid-flight by a restart has no participants any more.
        for run in self._runs.values():
            if occupies_slot(run.state):
                logger.warning("run %s was %s at shutdown; marking failed", run.run_id, run.state)
                run.state = RunState.FAILED
                self._store.save(run)
        self._tasks = [
            asyncio.create_task(self._keepalive_loop(), name="keepalive"),
            asyncio.create_task(self._supervise_loop(), name="supervise"),
            asyncio.create_task(self._broadcast_loop(), name="broadcast"),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()

    # --- runs -------------------------------------------------------------

    @property
    def active_run(self) -> Run | None:
        for run in self._runs.values():
            if occupies_slot(run.state):
                return run
        return None

    def visible_runs(self) -> list[Run]:
        """Newest first. UUIDv7 sorts chronologically, which is the point."""
        return sorted(
            (r for r in self._runs.values() if not r.removed),
            key=lambda r: r.run_id,
            reverse=True,
        )

    def get_run(self, run_id: str) -> Run:
        run = self._runs.get(run_id)
        if run is None or run.removed:
            raise OrchestratorError(f"No run {run_id}.")
        return run

    def create_run(self, label: str = "", params: dict[str, Any] | None = None) -> Run:
        run = Run(
            run_id=uuid7(),
            seq=max((r.seq for r in self._runs.values()), default=0) + 1,
            label=label,
            params=params or {},
        )
        self._runs[run.run_id] = run
        self._persist(run, "created", {"to": str(run.state)})
        self.events.emit(
            "run created",
            run_id=run.run_id,
            context={"label": run.label, "params": run.params, "seq": run.seq},
        )
        self.mark_dirty()
        return run

    async def act(self, run_id: str, action: Action) -> Run:
        """Apply an operator action, or explain why it is not allowed."""
        run = self.get_run(run_id)

        if action is Action.START:
            blocking = self.active_run
            if blocking is not None and blocking.run_id != run_id:
                raise OrchestratorError(
                    f"Run {blocking.seq} ({blocking.run_id}) is still {blocking.state}. "
                    "Only one run can be active at a time; stop it first."
                )

        try:
            run.state = advance(run.state, Event(str(action)))
        except TransitionError as exc:
            raise OrchestratorError(str(exc)) from exc

        if action is Action.START:
            run.started_utc = utc_now()
            run.participants = {}
            self._starting_since = time.monotonic()
            await self._broadcast_command(
                p.CommandType.RUN_START, run_id=run.run_id, args=run.params
            )
        elif action is Action.STOP:
            await self._broadcast_command(p.CommandType.RUN_STOP, run_id=run.run_id)
        elif action is Action.REMOVE:
            run.removed = True

        self._persist(run, str(action), {"to": str(run.state)})
        self.events.emit(
            f"run {action} -> {run.state}",
            run_id=run.run_id,
            context={"action": str(action), "state": str(run.state), "label": run.label},
        )
        self.mark_dirty()
        return run

    def _persist(self, run: Run, event: str, detail: dict[str, Any]) -> None:
        self._store.save(run)
        self._store.journal(run.run_id, event, detail)

    # --- node connections -------------------------------------------------

    async def attach_node(self, node_id: str, socket: Any) -> None:
        previous = self._node_sockets.get(node_id)
        if previous is not None and previous is not socket:
            # A reconnect before the old socket was reaped. The new one wins.
            with contextlib.suppress(Exception):
                await previous.close()
        self._node_sockets[node_id] = socket

    async def detach_node(self, node_id: str, socket: Any) -> None:
        if self._node_sockets.get(node_id) is socket:
            del self._node_sockets[node_id]
        self.registry.on_disconnect(node_id)
        self.mark_dirty()

    async def send(self, node_id: str, frame: dict[str, Any]) -> bool:
        socket = self._node_sockets.get(node_id)
        if socket is None:
            return False
        try:
            await socket.send_text(json.dumps(frame))
            return True
        except Exception as exc:  # a closing socket is normal, not exceptional
            logger.debug("send to %s failed: %s", node_id, exc)
            return False

    async def _broadcast_command(
        self,
        command: p.CommandType,
        *,
        run_id: str | None = None,
        node_type: p.NodeType | None = None,
        args: dict[str, Any] | None = None,
    ) -> int:
        payload = p.CommandPayload(
            command=command, target_node_type=node_type, run_id=run_id, args=args or {}
        )
        frame = p.build(p.MessageType.COMMAND, payload)
        targets = [
            r.node_id
            for r in self.registry.online(node_type)
            if node_type is None or r.node_type == node_type
        ]
        sent = 0
        for node_id in targets:
            # One frame, one msg_id: acks are per-command, not per-node.
            if await self.send(node_id, frame):
                sent += 1
        return sent

    # --- inbound frames ---------------------------------------------------

    async def on_hello(self, hello: p.HelloPayload, socket: Any) -> dict[str, Any]:
        record = self.registry.on_hello(hello)
        await self.attach_node(record.node_id, socket)

        run = self.active_run
        active = None
        if run is not None:
            active = p.ActiveRun(run_id=run.run_id, label=run.label, params=run.params)
            if record.autostart or record.node_type is p.NodeType.EDGE:
                run.participants.setdefault(
                    record.node_id,
                    {
                        "role": str(record.node_type),
                        "host": record.host,
                        "version_sha": record.version_sha,
                    },
                )

        self.mark_dirty()
        return p.build(
            p.MessageType.WELCOME,
            p.WelcomePayload(
                server_version=__version__,
                keepalive_s=self._settings.keepalive_s,
                offline_timeout_s=self._settings.offline_timeout_s,
                active_run=active,
            ),
        )

    async def on_status(self, node_id: str, status: p.StatusPayload) -> None:
        record = self.registry.on_status(node_id, status)
        if record is None:
            return

        # A node sends its recorder report once, in the status that follows a
        # stop. Keyed by the run it belongs to, not the active one: the report
        # for a run arrives while that run is still `stopping`.
        if status.report:
            reported = self._runs.get(status.run_id or "") or self.active_run
            if reported is not None:
                reported.reports[node_id] = dict(status.report)
                self._store.save(reported)

        run = self.active_run
        if run is not None and record.run_id == run.run_id:
            run.participants.setdefault(
                node_id,
                {
                    "role": str(record.node_type),
                    "host": record.host,
                    "version_sha": record.version_sha,
                },
            )
            # Keyed by node_id, and the node REPORTS its own directory leaf
            # (params.out_leaf, e.g. "pub-0") rather than the admin deriving
            # it from node_type. The old shape keyed a constant site string
            # per type with setdefault, which silently omitted every instance
            # after the first from the manifest. The derived leaf remains as
            # the fallback for nodes that predate out_leaf — today that is
            # the gNB collector, which stays single-instance.
            role = str(record.node_type)
            if record.state is p.NodeState.RUNNING:
                leaf = (record.params or {}).get("out_leaf") or {
                    "client": "pub",
                    "edge": "edge",
                    "gnb": "ran",
                    "render": "render",
                }.get(role)
                if leaf:
                    run.sites[node_id] = {
                        "role": role,
                        "host": record.host,
                        "path": f"runs/{run.run_id}/{leaf}",
                    }
        self.mark_dirty()

    async def on_goodbye(self, node_id: str, goodbye: p.GoodbyePayload) -> None:
        record = self.registry.on_goodbye(node_id)
        if record is None:
            return
        run = self._runs.get(goodbye.run_id or "")
        if run is not None and goodbye.final_report:
            run.reports[node_id] = goodbye.final_report
            self._store.save(run)
        self.mark_dirty()

    # --- periodic passes --------------------------------------------------

    async def _keepalive_loop(self) -> None:
        frame = None
        while True:
            await asyncio.sleep(self._settings.keepalive_s)
            # A fresh msg_id per round so a stuck node is visible in logs.
            frame = p.build(p.MessageType.PING)
            for node_id in list(self._node_sockets):
                await self.send(node_id, frame)

    async def _supervise_loop(self) -> None:
        while True:
            await asyncio.sleep(self._settings.diagnostics_interval_s)
            try:
                await self._supervise_once()
                self._refresh_findings()
            except Exception:
                logger.exception("supervisor pass failed")
            finally:
                # Roll counters forward only after the compare, or every pass
                # would compare a sample against itself.
                self.registry.snapshot_counters()

    def _refresh_findings(self) -> None:
        before = self._findings
        self._findings = [
            f.to_dict()
            for f in diagnose(
                self.registry,
                self.active_run,
                admin_sha=self._admin_sha,
                topology=self.topology,
            )
        ]
        if self._findings != before:
            self.mark_dirty()

    async def _supervise_once(self) -> None:
        run = self.active_run
        if run is None:
            return

        participants = self.registry.participants_of(run.run_id)
        online = [r for r in participants if r.is_online(self._settings.offline_timeout_s)]
        running = [r for r in online if r.state is p.NodeState.RUNNING]
        # Which roles quorum needs comes from the topology spec, so this rule
        # and the page's role chips and the WF_*_ABSENT findings cannot drift
        # apart. Default spec = one client and one edge, exactly as before.
        has_quorum = all(
            any(r.node_type == role for r in running)
            for role in self.topology.required_roles
        )

        before = run.state
        if run.state is RunState.STARTING:
            if has_quorum:
                run.state = advance(run.state, Event.QUORUM_MET)
                self._starting_since = None
            elif (
                self._starting_since is not None
                and time.monotonic() - self._starting_since > self._settings.start_timeout_s
            ):
                run.state = advance(run.state, Event.START_TIMEOUT)
                self._starting_since = None
        elif run.state is RunState.RUNNING and not has_quorum:
            run.state = advance(run.state, Event.PARTICIPANT_LOST)
        elif run.state is RunState.DEGRADED and has_quorum:
            run.state = advance(run.state, Event.PARTICIPANT_RECOVERED)
        elif run.state is RunState.STOPPING and not online:
            run.state = advance(run.state, Event.REPORTS_COMPLETE)

        if run.state in {RunState.STARTING, RunState.RUNNING, RunState.DEGRADED} and not online:
            run.state = advance(run.state, Event.ALL_OFFLINE)

        if run.state is RunState.STOPPING and all(r.node_id in run.reports for r in participants):
            run.state = advance(run.state, Event.REPORTS_COMPLETE)

        if run.state is not before:
            if run.state in {RunState.STOPPED, RunState.FAILED}:
                run.stopped_utc = utc_now()
                # Freeze the diagnostics into the manifest: why a run ended
                # badly is part of its provenance.
                run.findings = list(self._findings)
            self._persist(run, "auto", {"from": str(before), "to": str(run.state)})
            self.events.emit(
                f"run {before} -> {run.state}",
                run_id=run.run_id,
                level="WARNING" if run.state in {RunState.DEGRADED, RunState.FAILED} else "INFO",
                context={
                    "from": str(before),
                    "to": str(run.state),
                    "findings": [f["code"] for f in self._findings],
                },
            )
            logger.info("run %s: %s -> %s", run.run_id, before, run.state)
            self.mark_dirty()

    # --- UI ---------------------------------------------------------------

    def mark_dirty(self) -> None:
        self._dirty.set()

    async def attach_ui(self, socket: Any) -> None:
        self._ui_sockets.add(socket)

    async def detach_ui(self, socket: Any) -> None:
        self._ui_sockets.discard(socket)

    async def _broadcast_loop(self) -> None:
        while True:
            await self._dirty.wait()
            self._dirty.clear()
            snapshot = json.dumps(self.snapshot())
            for socket in list(self._ui_sockets):
                try:
                    await socket.send_text(snapshot)
                except Exception:
                    self._ui_sockets.discard(socket)
            # Coalesce: a burst of status frames becomes one repaint.
            await asyncio.sleep(self._settings.ui_broadcast_min_interval_s)

    def snapshot(self) -> dict[str, Any]:
        """The whole view. Full replacement beats diffing for tens of rows."""
        run = self.active_run
        return {
            "server_version": __version__,
            "protocol": p.PROTOCOL_VERSION,
            "active_run_id": run.run_id if run else None,
            "runs": [
                {**r.to_dict(), "allowed": allowed_actions(r.state)} for r in self.visible_runs()
            ],
            "nodes": self.registry.to_dict(),
            # The page reads `topology.roles` for its role chips (one source
            # with the quorum rule and the findings) and `topology.nodes` for
            # the declared-topology view. `mermaid` is empty when nothing is
            # declared, which is how the page knows to hide that card.
            "topology": {**self.topology.to_dict(), "mermaid": topo.to_mermaid(self.topology)},
            "findings": list(self._findings),
        }
