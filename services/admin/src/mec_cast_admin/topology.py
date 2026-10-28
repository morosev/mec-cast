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
every existing single-zone deployment keep working untouched.

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

from .protocol import NODE_PROFILE, NodeType

#: Where a declared topology is looked for when none is configured. Relative
#: to the repo root, matching the compose files it sits beside.
DEFAULT_TOPOLOGY_PATH = "deploy/lab/topology.yml"

#: The zone every node belongs to until zones are declared. One zone is the
#: single-zone deployment the platform has always had, so nothing changes for
#: an operator who never writes a topology file.
DEFAULT_ZONE = "default"


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
    #: Instances of this role expected per zone. `max_per_zone=None` is
    #: unbounded — several LiDARs on one UE is the normal case since M1.
    min_per_zone: int = 0
    max_per_zone: int | None = None

    @property
    def profile(self) -> str | None:
        """The run profile this role serves; None for the RAN sources, which
        serve both. Architectural, so derived rather than declarable."""
        return NODE_PROFILE.get(self.role)

    def serves(self, profile: str) -> bool:
        return self.profile is None or self.profile == profile

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": str(self.role),
            "profile": self.profile,
            "required": self.required,
            "absence": self.absence,
            "min_per_zone": self.min_per_zone,
            "max_per_zone": self.max_per_zone,
        }


#: The architecture's own rules. A required role is required by runs of its
#: profile: a robotics run's quorum is one client and one edge -- the rule
#: ADR-0007 states -- and a media run's is the relay, a sender and a receiver
#: (ADR-0015).
DEFAULT_ROLES: tuple[RoleSpec, ...] = (
    RoleSpec(NodeType.CLIENT, required=True, absence="error", min_per_zone=1),
    RoleSpec(NodeType.EDGE, required=True, absence="error", min_per_zone=1, max_per_zone=1),
    RoleSpec(NodeType.GNB, required=False, absence="warn", max_per_zone=1),
    RoleSpec(NodeType.RENDER, required=False, absence=None),
    # The E2 xApp is a second RAN source, not a requirement: silent when
    # absent by default, since most runs are JSON-tap only. A study that needs
    # KPM declares `roles: {xapp: {required: true}}` in topology.yml.
    RoleSpec(NodeType.XAPP, required=False, absence=None, max_per_zone=1),
    RoleSpec(NodeType.RELAY, required=True, absence="error", min_per_zone=1, max_per_zone=1),
    RoleSpec(NodeType.SENDER, required=True, absence="error", min_per_zone=1),
    RoleSpec(NodeType.RECEIVER, required=True, absence="error", min_per_zone=1),
)


@dataclass(frozen=True)
class NodeSpec:
    """One declared node: which role runs on which host, in which zone."""

    node_id: str
    role: NodeType
    host: str
    zone: str = DEFAULT_ZONE
    instance: int = 0
    #: For a client: which RAN UE it is, as observed — ``e2_ue_id`` (the
    #: E2 xApp's id) and/or ``rnti`` (the JSON tap's). Both follow attach
    #: order, so this records what an operator saw at session start (research
    #: protocol step 0.2), and must be re-checked after a re-attach. It lets a
    #: ran_policy and tools/ran_join.py name the client node, not an id.
    ue: dict[str, int] | None = field(default=None, compare=False, hash=False)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "node_id": self.node_id,
            "role": str(self.role),
            "host": self.host,
            "zone": self.zone,
            "instance": self.instance,
        }
        if self.ue:
            out["ue"] = dict(self.ue)
        return out


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

    def roles_for(self, profile: str) -> tuple[RoleSpec, ...]:
        """The roles a run of this profile involves, its own and the RAN's."""
        return tuple(s for s in self.roles if s.serves(profile))

    def required_roles_for(self, profile: str) -> tuple[NodeType, ...]:
        """The roles a run of this profile needs for quorum. Read by the
        orchestrator instead of the hand-written `any(CLIENT) and any(EDGE)`."""
        return tuple(s.role for s in self.roles_for(profile) if s.required)

    @property
    def declared(self) -> bool:
        """True when an operator has said what the fleet should be."""
        return bool(self.nodes)

    # --- zones ------------------------------------------------------------

    @property
    def zones(self) -> tuple[str, ...]:
        """Declared zones, in declaration order, without duplicates."""
        seen: list[str] = []
        for node in self.nodes:
            if node.zone not in seen:
                seen.append(node.zone)
        return tuple(seen) or (DEFAULT_ZONE,)

    def nodes_in(self, zone: str) -> tuple[NodeSpec, ...]:
        return tuple(n for n in self.nodes if n.zone == zone)

    def find(self, node_id: str) -> NodeSpec | None:
        for node in self.nodes:
            if node.node_id == node_id:
                return node
        return None

    def to_dict(self) -> dict[str, Any]:
        """What the UI receives. `roleChips` reads `roles`; the topology view
        reads `nodes` and `zones`."""
        return {
            "source": self.source,
            "declared": self.declared,
            "roles": [r.to_dict() for r in self.roles],
            "nodes": [n.to_dict() for n in self.nodes],
            "zones": list(self.zones),
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
            raise TopologyError(f"{where}: role {role!r} is not one of {sorted(valid_roles)}")
        if not host:
            raise TopologyError(f"{where}: `host` is required")
        instance = int(entry.get("instance", 0))
        zone = str(entry.get("zone", DEFAULT_ZONE)).strip() or DEFAULT_ZONE
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
                zone=zone,
                instance=instance,
                ue=_parse_ue(entry.get("ue"), role, where),
            )
        )
    return tuple(nodes)


_UE_KEYS = ("e2_ue_id", "rnti")


def _parse_ue(raw: Any, role: str, where: str) -> dict[str, int] | None:
    """A client's RAN identity hint: ``{e2_ue_id: 0, rnti: 17921}``."""
    if raw is None:
        return None
    if role != str(NodeType.CLIENT):
        raise TopologyError(f"{where}: `ue` belongs on a client node, not a {role}")
    if not isinstance(raw, dict) or not raw:
        raise TopologyError(f"{where}: `ue` must be a mapping with {' and/or '.join(_UE_KEYS)}")
    out: dict[str, int] = {}
    for key, value in raw.items():
        if key not in _UE_KEYS:
            raise TopologyError(f"{where}: `ue.{key}` is not one of {list(_UE_KEYS)}")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise TopologyError(f"{where}: `ue.{key}` must be a non-negative integer")
        out[key] = value
    return out


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
            raise TopologyError(f"{source}: roles.{role}.absence must be 'error', 'warn' or null")
        by_role[NodeType(role)] = RoleSpec(
            role=base.role,
            required=bool(value.get("required", base.required)),
            absence=absence,
            min_per_zone=int(value.get("min_per_zone", base.min_per_zone)),
            # An explicit `max_per_zone: null` means unbounded, and .get
            # returns that None unchanged — same result as the longhand.
            max_per_zone=value.get("max_per_zone", base.max_per_zone),
        )
    # Keep DEFAULT_ROLES' order so the UI's chips do not reshuffle per file.
    return tuple(by_role[s.role] for s in DEFAULT_ROLES)


def load(path: str | pathlib.Path | None) -> TopologySpec:
    """Read a topology file, or return the built-in defaults.

    A missing file is not an error — it is the single-zone deployment the
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

    Read-only and generated per snapshot. Grouped by zone, because that is
    the structure the extended model adds and the thing an operator most
    needs to see at a glance.
    """
    if not spec.declared:
        return ""
    lines = ["flowchart LR"]
    for i, zone in enumerate(spec.zones):
        lines.append(f'  subgraph ZONE{i}["{zone}"]')
        lines.append("    direction LR")
        for node in spec.nodes_in(zone):
            ident = _mermaid_id(node.node_id)
            lines.append(f'    {ident}["{node.role}<br/>{node.host}"]')
        lines.append("  end")
    # The data path within each zone. Fan-in, not a chain: every client
    # reaches the gNB independently, so walking a flat list and joining
    # consecutive pairs would draw client-0 -> client-1, which is not a link
    # that exists. Renderers hang off the edge, in the downlink direction.
    for zone in spec.zones:
        in_zone = spec.nodes_in(zone)
        by_role: dict[NodeType, list[str]] = {}
        for node in in_zone:
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
