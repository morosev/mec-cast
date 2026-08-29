"""What the fleet is *supposed* to look like, as data.

Before this module the expected topology was asserted three times, in three
languages, and never as data:

* ``workflow.py`` — one ``if not X`` per role, with required-vs-optional
  encoded in whichever severity constant the branch happened to pass;
* ``admin.js`` — ``required = {client: true, edge: true, gnb: false, …}``;
* ``orchestrator.py`` — the quorum rule, "one running client and one running
  edge", spelled out inline.

Three places to edit, no test that they agree, and nothing an operator could
read to learn what the platform expects. They now all read a ``TopologySpec``.

Two layers, deliberately separate:

**Roles** (``RoleSpec``) — the cardinality rules, which are a property of the
architecture, not of a deployment. They ship as ``DEFAULT_ROLES`` and are what
makes a spec useful with no file present at all: local development, CI, and
every existing single-cell deployment keep working untouched.

**Nodes** (``NodeSpec``) — the declared fleet, which *is* deployment-specific
and lives in ``deploy/lab/topology.yml``. Optional. Declaring it buys
validation: a node that connects but was never declared, or a declared node
that never appears, both become findings rather than surprises at analysis
time.

The distinction matters because the failure modes differ. A missing *role* is
wrong everywhere. An undeclared *node* is only wrong where someone has said
what the fleet should be.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field
from typing import Any

from .protocol import NodeType

#: Where a declared topology is looked for when none is configured. Relative
#: to the repo root, matching the compose files it sits beside.
DEFAULT_TOPOLOGY_PATH = "deploy/lab/topology.yml"

#: The cell every node belongs to until cells are declared. One cell is the
#: single-cell deployment the platform has always had, so nothing changes for
#: an operator who never writes a topology file.
DEFAULT_CELL = "default"


@dataclass(frozen=True)
class RoleSpec:
    """Cardinality and reporting rules for one node type.

    ``required`` and ``absence`` are separate on purpose, because the gNB
    needs exactly the combination a single flag cannot express: it is **not**
    required (a run with no RAN KPIs is a legitimate run, so it must not
    block quorum) yet its absence **is** worth saying out loud, because more
    often than not it means the collector failed to start rather than that
    nobody wanted RAN data. The renderer is the other shape: optional, and
    silent when missing, since a run nobody is watching is entirely normal.
    """

    role: NodeType
    #: Counts toward quorum: a run cannot start recording without it.
    required: bool
    #: How absence is reported: "error", "warn", or None for not at all.
    absence: str | None
    #: Instances of this role expected per cell. `max_per_cell=None` is
    #: unbounded — several LiDARs on one UE is the normal case since M1.
    min_per_cell: int = 0
    max_per_cell: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": str(self.role),
            "required": self.required,
            "absence": self.absence,
            "min_per_cell": self.min_per_cell,
            "max_per_cell": self.max_per_cell,
        }


#: The architecture's own rules. Quorum is one client and one edge — the same
#: rule ADR-0007 states and `_supervise_once` used to spell out inline.
DEFAULT_ROLES: tuple[RoleSpec, ...] = (
    RoleSpec(NodeType.CLIENT, required=True, absence="error", min_per_cell=1),
    RoleSpec(NodeType.EDGE, required=True, absence="error", min_per_cell=1, max_per_cell=1),
    RoleSpec(NodeType.GNB, required=False, absence="warn", max_per_cell=1),
    RoleSpec(NodeType.RENDER, required=False, absence=None),
)


@dataclass(frozen=True)
class NodeSpec:
    """One declared node: which role runs on which host, in which cell."""

    node_id: str
    role: NodeType
    host: str
    cell: str = DEFAULT_CELL
    instance: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "role": str(self.role),
            "host": self.host,
            "cell": self.cell,
            "instance": self.instance,
        }


@dataclass(frozen=True)
class TopologySpec:
    """The whole expectation: role rules always, declared nodes when given."""

    roles: tuple[RoleSpec, ...] = DEFAULT_ROLES
    nodes: tuple[NodeSpec, ...] = ()
    #: Where this came from, for the UI to say so. Empty = built-in defaults.
    source: str = ""

    # --- role questions ---------------------------------------------------

    def role(self, node_type: NodeType | str) -> RoleSpec | None:
        for spec in self.roles:
            if spec.role == node_type:
                return spec
        return None

    @property
    def required_roles(self) -> tuple[NodeType, ...]:
        """The roles quorum needs. Read by the orchestrator instead of the
        hand-written `any(CLIENT) and any(EDGE)`."""
        return tuple(s.role for s in self.roles if s.required)

    @property
    def declared(self) -> bool:
        """True when an operator has said what the fleet should be."""
        return bool(self.nodes)

    # --- cells ------------------------------------------------------------

    @property
    def cells(self) -> tuple[str, ...]:
        """Declared cells, in declaration order, without duplicates."""
        seen: list[str] = []
        for node in self.nodes:
            if node.cell not in seen:
                seen.append(node.cell)
        return tuple(seen) or (DEFAULT_CELL,)

    def nodes_in(self, cell: str) -> tuple[NodeSpec, ...]:
        return tuple(n for n in self.nodes if n.cell == cell)

    def find(self, node_id: str) -> NodeSpec | None:
        for node in self.nodes:
            if node.node_id == node_id:
                return node
        return None

    def to_dict(self) -> dict[str, Any]:
        """What the UI receives. `roleChips` reads `roles`; the topology view
        reads `nodes` and `cells`."""
        return {
            "source": self.source,
            "declared": self.declared,
            "roles": [r.to_dict() for r in self.roles],
            "nodes": [n.to_dict() for n in self.nodes],
            "cells": list(self.cells),
        }


class TopologyError(ValueError):
    """A declared topology that cannot be honoured.

    Raised at load time only. A malformed file must fail loudly at startup
    rather than degrade into "no topology declared", which would silently
    turn every validation finding off — the opposite of what the operator
    who wrote the file was asking for.
    """


def _parse_nodes(raw: Any, source: str) -> tuple[NodeSpec, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise TopologyError(f"{source}: `nodes` must be a list, got {type(raw).__name__}")

    valid_roles = {str(t) for t in NodeType}
    nodes: list[NodeSpec] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw):
        where = f"{source}: nodes[{i}]"
        if not isinstance(entry, dict):
            raise TopologyError(f"{where} must be a mapping")
        role = str(entry.get("role", "")).strip()
        host = str(entry.get("host", "")).strip()
        if role not in valid_roles:
            raise TopologyError(
                f"{where}: role {role!r} is not one of {sorted(valid_roles)}"
            )
        if not host:
            raise TopologyError(f"{where}: `host` is required")
        instance = int(entry.get("instance", 0))
        cell = str(entry.get("cell", DEFAULT_CELL)).strip() or DEFAULT_CELL
        # node_id is derived exactly as protocol.node_id() derives it, so a
        # declared node and a connected one compare directly. Declaring it
        # explicitly is allowed for the rare host whose hostname differs from
        # what the operator wrote here.
        node_id = str(entry.get("node_id", "")).strip() or f"{role}-{host}-{instance}"
        if node_id in seen:
            raise TopologyError(
                f"{where}: duplicate node_id {node_id!r} — two nodes of one role on "
                "one host need distinct `instance` values, the same number passed "
                "to the node as admin_instance"
            )
        seen.add(node_id)
        nodes.append(
            NodeSpec(
                node_id=node_id,
                role=NodeType(role),
                host=host,
                cell=cell,
                instance=instance,
            )
        )
    return tuple(nodes)


def _parse_roles(raw: Any, source: str) -> tuple[RoleSpec, ...]:
    """Role overrides, merged onto DEFAULT_ROLES.

    Overrides rather than replacement: a file that mentions only `render`
    must not silently drop the client and edge rules and leave a fleet with
    no quorum requirement at all.
    """
    if raw is None:
        return DEFAULT_ROLES
    if not isinstance(raw, dict):
        raise TopologyError(f"{source}: `roles` must be a mapping, got {type(raw).__name__}")

    by_role = {s.role: s for s in DEFAULT_ROLES}
    for key, value in raw.items():
        role = str(key).strip()
        if role not in {str(t) for t in NodeType}:
            raise TopologyError(f"{source}: roles.{role} is not a known node type")
        if not isinstance(value, dict):
            raise TopologyError(f"{source}: roles.{role} must be a mapping")
        base = by_role[NodeType(role)]
        absence = value.get("absence", base.absence)
        if absence not in ("error", "warn", None):
            raise TopologyError(
                f"{source}: roles.{role}.absence must be 'error', 'warn' or null"
            )
        by_role[NodeType(role)] = RoleSpec(
            role=base.role,
            required=bool(value.get("required", base.required)),
            absence=absence,
            min_per_cell=int(value.get("min_per_cell", base.min_per_cell)),
            max_per_cell=(
                value["max_per_cell"] if "max_per_cell" in value else base.max_per_cell
            ),
        )
    # Keep DEFAULT_ROLES' order so the UI's chips do not reshuffle per file.
    return tuple(by_role[s.role] for s in DEFAULT_ROLES)


def load(path: str | pathlib.Path | None) -> TopologySpec:
    """Read a topology file, or return the built-in defaults.

    A missing file is not an error — it is the single-cell deployment the
    platform has always had. A *malformed* file is an error, because someone
    tried to say something and it did not take.
    """
    if not path:
        return TopologySpec()
    p = pathlib.Path(path)
    if not p.exists():
        return TopologySpec()

    import yaml  # imported here so a deployment with no topology file need not care

    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise TopologyError(f"{p}: not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise TopologyError(f"{p}: expected a mapping at the top level")

    return TopologySpec(
        roles=_parse_roles(raw.get("roles"), str(p)),
        nodes=_parse_nodes(raw.get("nodes"), str(p)),
        source=str(p),
    )


def to_mermaid(spec: TopologySpec) -> str:
    """The declared topology as a mermaid flowchart, for the admin page.

    Read-only and generated per snapshot. Grouped by cell, because that is
    the structure the extended model adds and the thing an operator most
    needs to see at a glance.
    """
    if not spec.declared:
        return ""
    lines = ["flowchart LR"]
    for i, cell in enumerate(spec.cells):
        lines.append(f'  subgraph CELL{i}["{cell}"]')
        lines.append("    direction LR")
        for node in spec.nodes_in(cell):
            ident = _mermaid_id(node.node_id)
            lines.append(f'    {ident}["{node.role}<br/>{node.host}"]')
        lines.append("  end")
    # The data path within each cell. Fan-in, not a chain: every client
    # reaches the gNB independently, so walking a flat list and joining
    # consecutive pairs would draw client-0 -> client-1, which is not a link
    # that exists. Renderers hang off the edge, in the downlink direction.
    for cell in spec.cells:
        in_cell = spec.nodes_in(cell)
        by_role: dict[NodeType, list[str]] = {}
        for node in in_cell:
            by_role.setdefault(node.role, []).append(_mermaid_id(node.node_id))

        clients = by_role.get(NodeType.CLIENT, [])
        gnbs = by_role.get(NodeType.GNB, [])
        edges = by_role.get(NodeType.EDGE, [])
        renders = by_role.get(NodeType.RENDER, [])

        # Uplink: client -> gNB -> edge, skipping a hop that is not declared.
        uplink_target = gnbs or edges
        for client in clients:
            for target in uplink_target:
                lines.append(f"  {client} --> {target}")
        for gnb in gnbs:
            for edge in edges:
                lines.append(f"  {gnb} --> {edge}")
        # Downlink: only exists when the edge republishes (ADR-0009), but a
        # declared renderer is there to receive it, so draw the intent.
        for edge in edges:
            for render in renders:
                lines.append(f"  {edge} -.-> {render}")
    return "\n".join(lines)


def _mermaid_id(node_id: str) -> str:
    """A mermaid-safe identifier. Hyphens and dots break node ids."""
    return "N_" + "".join(c if c.isalnum() else "_" for c in node_id)
