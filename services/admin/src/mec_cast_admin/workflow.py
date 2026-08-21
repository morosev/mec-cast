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

from .protocol import NodeType
from .registry import NodeRecord, Registry
from .state import RunState
from .store import Run

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

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "subject": self.subject,
            "message": self.message,
            "remedy": self.remedy,
        }


def _rising(record: NodeRecord, key: str) -> bool | None:
    """Whether a counter grew since the previous pass.

    None when there is no previous sample yet — the first pass after a node
    connects must not be read as "flat".
    """
    if key not in record.counters or key not in record.previous_counters:
        return None
    return record.counters[key] > record.previous_counters[key]


def diagnose(
    registry: Registry,
    run: Run | None,
    *,
    admin_sha: str = "",
) -> list[Finding]:
    """Everything currently wrong, worst first."""
    findings: list[Finding] = []
    online = registry.online()
    clients = [r for r in online if r.node_type == NodeType.CLIENT]
    edges = [r for r in online if r.node_type == NodeType.EDGE]
    gnbs = [r for r in online if r.node_type == NodeType.GNB]

    run_is_active = run is not None and run.state in {
        RunState.STARTING,
        RunState.RUNNING,
        RunState.DEGRADED,
    }

    # --- roles missing entirely ------------------------------------------
    if run_is_active and not edges:
        findings.append(
            Finding(
                "WF_EDGE_ABSENT",
                "error",
                "edge",
                "No edge node is connected, so nothing is receiving the stream.",
                "Local: `make up-admin`. Lab: `bash deploy/lab/deploy.sh edge <user@host>`. "
                "If it is running, check that its ADMIN_URL points at this service.",
            )
        )
    if run_is_active and not clients:
        findings.append(
            Finding(
                "WF_CLIENT_ABSENT",
                "error",
                "client",
                "No client node is connected, so no frames are being produced.",
                "Local: `make up-admin`. Lab: `bash deploy/lab/deploy.sh ue <user@host>`.",
            )
        )
    if run_is_active and not gnbs:
        findings.append(
            Finding(
                "WF_GNB_ABSENT",
                "warn",
                "gnb",
                "No gNB collector is connected; this run will have no RAN KPIs.",
                "Deploy the gnb role with `bash deploy/lab/deploy.sh gnb <user@host>`, "
                "or accept the run without RAN metrics — it is still a valid latency run.",
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
        for edge in edges:
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
            if record.run_id and record.run_id != run.run_id:
                findings.append(
                    Finding(
                        "WF_RUN_MISMATCH",
                        "error",
                        record.node_id,
                        f"{record.node_id} is recording run {record.run_id} while the active "
                        f"run is {run.run_id}.",
                        "Stop that node's run from this page, then start the active run again.",
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
