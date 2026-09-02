"""Detecting that the workflow is not established, and saying what to do.

The two failure modes this exists for are the ones already named in the node
docstrings as silent: a `reliability` QoS mismatch, and a publisher with no
subscriber. Both produce zero frames, zero errors, and a full-length run
discovered worthless at analysis time. Nothing in the platform could detect
either until now.

Findings are **derived on every pass, never stored**, so a condition that
clears simply stops being reported and there is no stale-alert bug class.

Every finding carries a `remedy`. A diagnostic that says something is wrong
without saying what to do is a worse version of silence, because it costs
attention as well.

NOTE for doc-sync: the remedies below embed commands, ports and file paths.
They are operator documentation that happens to live in a .py file, and they
rot when a script, port or make target is renamed. See
`.claude/skills/doc-sync/references/code-to-doc-map.md`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .protocol import NodeState, NodeType
from .registry import NodeRecord, Registry
from .state import RunState
from .store import Run
from .topology import DEFAULT_CELL, TopologySpec

#: A client may legitimately publish for a moment before the edge reports its
#: first peer. Below this, "no peer" is startup rather than a fault.
PEER_GRACE_S = 10.0


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str  # "error" | "warn" | "info"
    subject: str
    message: str
    remedy: str
    #: Which cell this is about. Empty for fleet-wide findings that belong to
    #: no single cell. Carried so a run's manifest can freeze its OWN
    #: problems and not another cell's.
    cell: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "subject": self.subject,
            "message": self.message,
            "remedy": self.remedy,
            "cell": self.cell,
        }


def _rising(record: NodeRecord, key: str) -> bool | None:
    """Whether a counter grew since the previous pass.

    None when there is no previous sample yet — the first pass after a node
    connects must not be read as "flat".
    """
    if key not in record.counters or key not in record.previous_counters:
        return None
    return record.counters[key] > record.previous_counters[key]


#: Message and remedy for each role whose absence is worth reporting. Kept
#: out of the spec because these are operator documentation — they name
#: commands and make targets, and doc-sync checks them (see the module note).
_ABSENCE_PROSE: dict[NodeType, tuple[str, str]] = {
    NodeType.EDGE: (
        "No edge node is connected, so nothing is receiving the stream.",
        "Local: `make up-admin`. Lab: `bash deploy/lab/deploy.sh edge <user@host>`. "
        "If it is running, check that its ADMIN_URL points at this service.",
    ),
    NodeType.CLIENT: (
        "No client node is connected, so no frames are being produced.",
        "Local: `make up-admin`. Lab: `bash deploy/lab/deploy.sh ue <user@host>`.",
    ),
    NodeType.GNB: (
        "No gNB collector is connected; this run will have no RAN KPIs.",
        "Deploy the gnb role with `bash deploy/lab/deploy.sh gnb <user@host>`, "
        "or accept the run without RAN metrics — it is still a valid latency run.",
    ),
}


def diagnose(
    registry: Registry,
    run: Run | None,
    *,
    admin_sha: str = "",
    topology: TopologySpec | None = None,
) -> list[Finding]:
    """Everything currently wrong, worst first."""
    findings: list[Finding] = []
    # No spec passed means the built-in role rules, which is what every
    # caller got before topology existed — the defaults ARE today's behaviour.
    spec = topology if topology is not None else TopologySpec()
    online = registry.online()

    # Everything role-shaped below is judged WITHIN a cell. A flat partition
    # would let a gNB in cell B satisfy cell A's WF_GNB_ABSENT, and would
    # pair every client against every edge across cells — N x M spurious
    # WF_QOS_MISMATCH and WF_NO_PEER findings the moment a second cell
    # exists. An undeclared deployment has exactly one cell, so this is the
    # same computation it always was.
    cell_of = lambda r: r.cell or DEFAULT_CELL  # noqa: E731
    cells = sorted({cell_of(r) for r in online}) or [DEFAULT_CELL]
    if run is not None and getattr(run, "cell", None):
        # A run is scoped to its cell: diagnosing the others while it is the
        # subject would attribute their faults to it.
        cells = [c for c in cells if c == run.cell] or [run.cell]

    clients = [r for r in online if r.node_type == NodeType.CLIENT]
    edges = [r for r in online if r.node_type == NodeType.EDGE]
    gnbs = [r for r in online if r.node_type == NodeType.GNB]
    renderers = [r for r in online if r.node_type == NodeType.RENDER]

    run_is_active = run is not None and run.state in {
        RunState.STARTING,
        RunState.RUNNING,
        RunState.DEGRADED,
    }

    # --- roles missing entirely ------------------------------------------
    # Which roles must be present, and how loudly to say so, is the topology
    # spec's business now — the same source the quorum rule and the page's
    # role chips read, so the three cannot disagree. Only the prose stays
    # here, because a remedy is operator documentation and not a rule.
    for cell in cells:
        in_cell = [r for r in online if cell_of(r) == cell]
        present = {role: [r for r in in_cell if r.node_type == role] for role in NodeType}
        for role_spec in spec.roles:
            if role_spec.absence is None or present.get(role_spec.role):
                continue
            if not run_is_active:
                continue
            detail = _ABSENCE_PROSE.get(role_spec.role)
            if detail is None:
                continue
            message, remedy = detail
            where = f" in cell {cell}" if len(cells) > 1 else ""
            findings.append(
                Finding(
                    f"WF_{str(role_spec.role).upper()}_ABSENT",
                    role_spec.absence,
                    str(role_spec.role) if len(cells) == 1 else f"{cell}/{role_spec.role}",
                    message.rstrip(".") + where + "." if where else message,
                    remedy,
                    cell=cell,
                )
            )

    # --- the declared fleet, when there is one ----------------------------
    # Only meaningful once an operator has written deploy/lab/topology.yml.
    # Without it these say nothing, which is the point: validation is opt-in,
    # and an undeclared fleet is not a wrong fleet.
    if spec.declared:
        for record in online:
            declared = spec.find(record.node_id)
            if declared is not None:
                # A node that reports a different cell from the one it is
                # declared in is a deployment mistake, not a topology one:
                # CELL was set wrong, or the wrong compose file reached the
                # host. Worth catching because the node still works perfectly
                # — it just gets grouped with the wrong cell's results.
                if record.cell and record.cell != declared.cell:
                    findings.append(
                        Finding(
                            "WF_TOPOLOGY_CELL_MISMATCH",
                            "warn",
                            record.node_id,
                            f"{record.node_id} reports cell {record.cell!r} but "
                            f"{spec.source} declares it in {declared.cell!r}.",
                            f"Set CELL={declared.cell} on that host (or fix the "
                            f"declaration in {spec.source}) and restart the node. "
                            "Until then its samples are attributed to the wrong "
                            "cell in any per-cell comparison.",
                        )
                    )
                continue
            findings.append(
                Finding(
                    "WF_TOPOLOGY_UNDECLARED",
                    "warn",
                    record.node_id,
                    f"{record.node_id} is connected but not declared in "
                    f"{spec.source}. It is participating in runs while the "
                    "declared topology says it should not exist.",
                    f"Add it to {spec.source} (role: {record.node_type}, "
                    f"host: {record.host}), or stop it if it is a leftover "
                    "container from another experiment — a stray node of a "
                    "required role can satisfy quorum and quietly join a run.",
                )
            )
        if run_is_active:
            online_ids = {r.node_id for r in online}
            for node in spec.nodes:
                if node.node_id in online_ids:
                    continue
                findings.append(
                    Finding(
                        "WF_TOPOLOGY_MISSING",
                        "warn",
                        node.node_id,
                        f"{node.node_id} is declared in {spec.source} "
                        f"(cell {node.cell}) but has never connected.",
                        f"Deploy it, or remove it from {spec.source} if the "
                        "fleet has genuinely shrunk. A declared node that never "
                        "arrives means the run covers less of the topology than "
                        "whoever reads the results will assume.",
                    )
                )

    # A renderer is optional like the gNB, so its absence is not reported at
    # all — only a renderer that is present and starved, which means the edge
    # is not sending the downlink it is being asked to draw.
    for render in renderers:
        if not run_is_active or not render.subscribed:
            continue
        edge_producing = any(_rising(e, "frames") for e in edges)
        if edge_producing and not _rising(render, "frames"):
            findings.append(
                Finding(
                    "WF_RENDER_STARVED",
                    "error",
                    render.node_id,
                    f"Renderer {render.node_id} is subscribed but receiving nothing "
                    "while the edge is processing frames.",
                    "The edge only sends the downlink when `publish_result` is true — "
                    "it is off by default. Set PUBLISH_RESULT=1 on the edge (or "
                    "`-p publish_result:=true`) and recreate the container. Also "
                    "confirm the renderer's `reliability` matches the edge's "
                    "`result_reliability`: a best_effort publisher with a reliable "
                    "subscriber is an incompatible pair and delivers nothing.",
                )
            )

    # Unsynchronised clocks. A one-way delay cannot be negative, so a node
    # reporting any is telling us the sending host's clock is AHEAD of its
    # own -- and the whole skew lands in every cross-host figure it records.
    #
    # This is an error rather than a warning because the run keeps looking
    # healthy: frames flow, CSVs grow, the page shows green. Only the numbers
    # are wrong, and they are the reason the run exists. `ptp.reliable` says
    # whether PTP THINKS it is disciplined; this says the arithmetic came out
    # impossible, which is the stronger statement.
    for record in online:
        skewed = int((record.counters or {}).get("negative_delays") or 0)
        if skewed:
            findings.append(
                Finding(
                    "WF_CLOCK_SKEW",
                    "error",
                    record.node_id,
                    f"{record.node_id} recorded {skewed} impossible (negative) "
                    "delay(s): the sending host's clock is ahead of this one's. "
                    "Every cross-host figure from this node is wrong by the skew.",
                    "Clocks are not synchronised. Run "
                    "`bash deploy/lab/ptp/verify-ptp.sh` on both hosts and check "
                    "ptp4l and phc2sys are running; seconds of skew mean PTP is "
                    "not running at all rather than drifting. Discard this run's "
                    "cross-host numbers. The renderer's own e2e_ns survives, "
                    "since both its stamps come off one host (ADR-0009).",
                    cell=cell_of(record),
                )
            )

    # A renderer on a host with no lidar client loses ADR-0009's property:
    # its e2e_ns subtracts a capture stamp taken on ANOTHER host's clock, so
    # the "PTP-free round trip" quietly becomes PTP-dependent like every
    # other cross-host figure. The number still computes; it just no longer
    # means what site 2 is documented to mean — which is exactly the kind of
    # silent redefinition worth interrupting someone for.
    client_hosts = {c.host for c in clients}
    for render in renderers:
        if not run_is_active or render.state is not NodeState.RUNNING:
            continue
        if client_hosts and render.host not in client_hosts:
            findings.append(
                Finding(
                    "WF_RENDER_CROSS_HOST",
                    "warn",
                    render.node_id,
                    f"Renderer {render.node_id} runs on {render.host!r} but no lidar "
                    "client does. Its e2e_ns is a cross-host difference — "
                    "PTP-dependent, not the PTP-free round trip of ADR-0009.",
                    "Co-locate a lidar client on that host to restore the PTP-free "
                    "round trip, or treat this renderer's e2e_ns like any other "
                    "cross-host metric: valid only while `context.ptp.reliable` "
                    "is true.",
                )
            )

    # --- connected but not doing their job --------------------------------
    for edge in edges:
        if run_is_active and not edge.subscribed:
            findings.append(
                Finding(
                    "WF_EDGE_IDLE",
                    "error",
                    edge.node_id,
                    f"Edge {edge.node_id} is connected but not subscribed to the cloud topic.",
                    "Press Start for this run, or check the node's `admin_autostart` parameter.",
                )
            )

    # --- the silent failures ----------------------------------------------
    streaming_clients = [c for c in clients if c.streaming]
    for client in streaming_clients:
        # Only edges in the SAME cell. Across cells this was a cross-product:
        # N clients x M edges, every pair generating findings about a link
        # that does not exist.
        for edge in edges:
            if cell_of(edge) != cell_of(client):
                continue
            if not edge.subscribed:
                continue

            client_reliability = client.params.get("reliability")
            edge_reliability = edge.params.get("reliability")
            if client_reliability and edge_reliability and client_reliability != edge_reliability:
                findings.append(
                    Finding(
                        "WF_QOS_MISMATCH",
                        "error",
                        f"{client.node_id} -> {edge.node_id}",
                        f"Publisher is {client_reliability!r} and subscriber is "
                        f"{edge_reliability!r}. That pair delivers nothing, silently.",
                        "Set the same `reliability` on both, e.g. RELIABILITY=reliable "
                        "for the whole topology, and restart the run.",
                        cell=cell_of(client),
                    )
                )
                continue

            if not edge.peers and client.streaming_for(PEER_GRACE_S):
                findings.append(
                    Finding(
                        "WF_NO_PEER",
                        "error",
                        f"{client.node_id} -> {edge.node_id}",
                        f"Client {client.node_id} is publishing but {edge.node_id} sees no "
                        "publisher on the cloud topic.",
                        "The client and edge are not meeting on the Zenoh router. Check the "
                        "router is reachable from the UE at udp/<edge>:7447?rel=1 and that "
                        "ZENOH_CONFIG_OVERRIDE names the right host.",
                        cell=cell_of(client),
                    )
                )
                continue

            client_up = _rising(client, "frames_published")
            edge_up = _rising(edge, "frames")
            if client_up is True and edge_up is False:
                findings.append(
                    Finding(
                        "WF_NO_FRAMES",
                        "error",
                        f"{client.node_id} -> {edge.node_id}",
                        f"{client.node_id} is publishing but {edge.node_id}'s frame count is "
                        "not moving.",
                        "Frames are leaving the client and not arriving. Check the Zenoh "
                        "router and, if netem is active, whether the offered load exceeds "
                        "what the impaired link can carry.",
                        cell=cell_of(client),
                    )
                )

    # --- the gNB ----------------------------------------------------------
    for gnb in gnbs:
        if _rising(gnb, "datagrams") is False:
            findings.append(
                Finding(
                    "WF_GNB_SILENT",
                    "warn",
                    gnb.node_id,
                    f"{gnb.node_id} is bound but srsRAN is sending it nothing.",
                    "Point srsRAN at this collector: set metrics.addr and metrics.port in "
                    "gnb.yml to this host and port 55555.",
                )
            )

    # --- consistency ------------------------------------------------------
    if run is not None:
        for record in online:
            # Only within this run's own cell. A node in another cell is
            # recording that cell's run, which is correct, not a mismatch —
            # comparing across cells reported every other cell's nodes as
            # being on the wrong run the moment two ran at once.
            if cell_of(record) != (getattr(run, "cell", None) or DEFAULT_CELL):
                continue
            if record.run_id and record.run_id != run.run_id:
                findings.append(
                    Finding(
                        "WF_RUN_MISMATCH",
                        "error",
                        record.node_id,
                        f"{record.node_id} is recording run {record.run_id} while the active "
                        f"run in cell {cell_of(record)} is {run.run_id}.",
                        "Stop that node's run from this page, then start the active run again.",
                        cell=cell_of(record),
                    )
                )

    if admin_sha:
        for record in online:
            if record.version_sha and record.version_sha != admin_sha:
                findings.append(
                    Finding(
                        "WF_VERSION_SKEW",
                        "warn",
                        record.node_id,
                        f"{record.node_id} is running commit {record.version_sha} and this "
                        f"service is {admin_sha}.",
                        "Redeploy that host so the whole topology is on one commit: "
                        "`bash deploy/lab/deploy.sh <role> <user@host>`.",
                    )
                )

    for record in online:
        if _rising(record, "post_failures") is True:
            findings.append(
                Finding(
                    "WF_LOGGING_UNREACHABLE",
                    "warn",
                    record.node_id,
                    f"Snapshots from {record.node_id} are not reaching the logging service. "
                    "CSV recording is unaffected.",
                    "Check LOGGING_URL on that node and that the logging service answers on "
                    "port 8000.",
                )
            )

    # --- participants that vanished mid-run -------------------------------
    if run_is_active:
        for record in registry.participants_of(run.run_id):
            if record.is_online(30.0) or record.departed:
                continue
            findings.append(
                Finding(
                    "WF_PARTICIPANT_LOST",
                    "error",
                    record.node_id,
                    f"{record.node_id} joined this run and has stopped answering.",
                    "The process died or the host is unreachable. Check its container; if it "
                    "comes back it will rejoin this run and append to the same CSV.",
                )
            )

    order = {"error": 0, "warn": 1, "info": 2}
    findings.sort(key=lambda f: (order.get(f.severity, 3), f.code, f.subject))
    return findings
