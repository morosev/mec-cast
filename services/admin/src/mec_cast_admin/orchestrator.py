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
from . import topology as topo
from .config import Settings
from .events import EventLog
from .registry import Registry
from .state import Action, Event, RunState, TransitionError, advance, allowed_actions, occupies_slot
from .store import Run, RunStore, utc_now, uuid7
from .topology import DEFAULT_CELL
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

        #: run_id -> when it entered `starting`. Per-run since runs became
        #: per-cell: one scalar would let cell A's start reset cell B's
        #: timeout clock, so a slow cell could hold a fast one open forever.
        self._starting_since: dict[str, float] = {}
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
    def active_runs(self) -> list[Run]:
        """Every run holding a slot, at most one per cell.

        Derived rather than stored, as `active_run` always was — so lifting
        the single-run limit corrupts no schema and needs no migration.
        """
        return [r for r in self._runs.values() if occupies_slot(r.state)]

    def active_run_in(self, cell: str) -> Run | None:
        for run in self._runs.values():
            if occupies_slot(run.state) and run.cell == cell:
                return run
        return None

    @property
    def active_run(self) -> Run | None:
        """The first active run, for the single-cell callers and the page's
        `active_run_id`. Kept because a deployment with one cell has exactly
        one active run, and every one-cell caller means precisely that."""
        runs = self.active_runs
        return runs[0] if runs else None

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

    def create_run(
        self,
        label: str = "",
        params: dict[str, Any] | None = None,
        cell: str = DEFAULT_CELL,
    ) -> Run:
        # An unknown cell is refused rather than silently accepted: a typo
        # would create a run that no node can ever join, which looks exactly
        # like a broken deployment. Only checked against a DECLARED topology,
        # since without one any name is as good as another.
        if self.topology.declared and cell not in self.topology.cells:
            raise OrchestratorError(
                f"No cell {cell!r} in {self.topology.source}. "
                f"Declared cells: {', '.join(self.topology.cells)}."
            )
        run = Run(
            run_id=uuid7(),
            seq=max((r.seq for r in self._runs.values()), default=0) + 1,
            label=label,
            params=params or {},
            cell=cell or DEFAULT_CELL,
        )
        self._runs[run.run_id] = run
        self._persist(run, "created", {"to": str(run.state)})
        self.events.emit(
            "run created",
            run_id=run.run_id,
            context={
                "label": run.label,
                "params": run.params,
                "seq": run.seq,
                "cell": run.cell,
            },
        )
        self.mark_dirty()
        return run

    async def act(self, run_id: str, action: Action) -> Run:
        """Apply an operator action, or explain why it is not allowed."""
        run = self.get_run(run_id)

        if action is Action.START:
            # One active run per CELL. Two cells are independent deployments
            # of the same platform, so a run in one must not block the other;
            # within a cell the limit still holds, because it falls out of
            # one active Recorder per node process (ADR-0007).
            blocking = self.active_run_in(run.cell)
            if blocking is not None and blocking.run_id != run_id:
                raise OrchestratorError(
                    f"Run {blocking.seq} ({blocking.run_id}) is still {blocking.state} "
                    f"in cell {run.cell}. Only one run can be active per cell; "
                    "stop it first."
                )

        try:
            run.state = advance(run.state, Event(str(action)))
        except TransitionError as exc:
            raise OrchestratorError(str(exc)) from exc

        if action is Action.START:
            run.started_utc = utc_now()
            run.participants = {}
            self._starting_since[run.run_id] = time.monotonic()
            # Start recruits, so it goes to the cell rather than to members.
            await self._broadcast_command(
                p.CommandType.RUN_START, run_id=run.run_id, args=run.params, run=run
            )
        elif action is Action.STOP:
            # Stop goes only to the nodes actually recording this run.
            await self._broadcast_command(
                p.CommandType.RUN_STOP, run_id=run.run_id, run=run, by_membership=True
            )
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

    def _command_targets(
        self,
        *,
        run: Run | None,
        by_membership: bool,
        node_type: p.NodeType | None,
    ) -> list[str]:
        """Who a command is actually for.

        The two lifecycle commands need different answers, and conflating
        them is the bug this replaces — the old code shouted at every online
        node, so with two concurrent runs a `run.stop` for one cell stopped
        the other cell's nodes mid-measurement.

        * **stop** targets run MEMBERSHIP: exactly the nodes recording this
          run. Nothing else should hear it.
        * **start** cannot target membership — no node is in the run yet;
          recruiting them is what start is for. It targets the run's CELL
          instead, which is the set of nodes eligible to join.

        A node that reports no cell is treated as being in the default cell,
        so an undeclared single-cell deployment keeps receiving everything
        exactly as before.
        """
        online = self.registry.online(node_type)
        if node_type is not None:
            online = [r for r in online if r.node_type == node_type]
        if run is None:
            return [r.node_id for r in online]

        in_cell = [r for r in online if (r.cell or DEFAULT_CELL) == run.cell]
        if not by_membership:
            return [r.node_id for r in in_cell]

        # Membership, plus the nodes that have not said yet. A node that took
        # `run.start` a moment ago has no run_id on its record until its first
        # status lands, and missing the stop would leave it streaming into a
        # run everyone else has left. A node recording a DIFFERENT run has a
        # different non-empty run_id and is still correctly excluded, which is
        # the property that matters — being scoped to the cell already, this
        # can only over-reach within the run's own cell.
        return [r.node_id for r in in_cell if r.run_id == run.run_id or not r.run_id]

    async def _broadcast_command(
        self,
        command: p.CommandType,
        *,
        run_id: str | None = None,
        node_type: p.NodeType | None = None,
        args: dict[str, Any] | None = None,
        run: Run | None = None,
        by_membership: bool = False,
    ) -> int:
        payload = p.CommandPayload(
            command=command, target_node_type=node_type, run_id=run_id, args=args or {}
        )
        frame = p.build(p.MessageType.COMMAND, payload)
        targets = self._command_targets(run=run, by_membership=by_membership, node_type=node_type)
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

        # The run for THIS node's cell, not simply the first active one — a
        # node joining cell B must not be told to start cell A's run.
        run = self.active_run_in(record.cell or DEFAULT_CELL)
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

        run = self._runs.get(record.run_id or "") or self.active_run_in(record.cell or DEFAULT_CELL)
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
        # One pass per active run, because diagnostics are cell-scoped: with
        # two runs, diagnosing only the first left the other cell with no
        # diagnosis at all while reporting its nodes as being on the wrong
        # run. With no active run, one pass with None — an idle platform
        # still has version skew and reachability to report.
        runs = self.active_runs or [None]
        seen: set[tuple[str, str, str]] = set()
        merged: list[dict[str, Any]] = []
        for run in runs:
            for finding in diagnose(
                self.registry,
                run,
                admin_sha=self._admin_sha,
                topology=self.topology,
            ):
                # Fleet-wide findings (version skew, logging reachability)
                # are produced by every pass; keep one.
                key = (finding.code, finding.subject, finding.cell)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(finding.to_dict())
        self._findings = merged
        if self._findings != before:
            self.mark_dirty()

    async def _supervise_once(self) -> None:
        # Every active run, not just the first: with runs per cell there can
        # be several, and supervising only one would leave the others stuck
        # in `starting` forever.
        for run in self.active_runs:
            await self._supervise_run(run)

    async def _supervise_run(self, run: Run) -> None:
        participants = self.registry.participants_of(run.run_id)
        online = [r for r in participants if r.is_online(self._settings.offline_timeout_s)]
        running = [r for r in online if r.state is p.NodeState.RUNNING]
        # Which roles quorum needs comes from the topology spec, so this rule
        # and the page's role chips and the WF_*_ABSENT findings cannot drift
        # apart. Default spec = one client and one edge, exactly as before.
        # Judged among THIS run's participants, which are already scoped to
        # the run — so a client in cell B cannot satisfy cell A's quorum.
        has_quorum = all(
            any(r.node_type == role for r in running) for role in self.topology.required_roles
        )

        before = run.state
        if run.state is RunState.STARTING:
            started_at = self._starting_since.get(run.run_id)
            if has_quorum:
                run.state = advance(run.state, Event.QUORUM_MET)
                self._starting_since.pop(run.run_id, None)
            elif (
                started_at is not None
                and time.monotonic() - started_at > self._settings.start_timeout_s
            ):
                run.state = advance(run.state, Event.START_TIMEOUT)
                self._starting_since.pop(run.run_id, None)
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
                # badly is part of its provenance. Only this cell's findings —
                # pinning another cell's problems onto this run's record would
                # misattribute them forever, and the manifest is the artefact
                # someone reads months later.
                run.findings = [f for f in self._findings if f.get("cell") in (None, "", run.cell)]
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
            # Per-cell, since runs are per-cell. `active_run_id` above stays
            # for the single-cell case and for readers that predate this.
            "active_runs": {r.cell: r.run_id for r in self.active_runs},
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
