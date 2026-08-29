"""The declared topology: parsing, defaults, and what it tells the rest."""

from __future__ import annotations

import pathlib

import pytest
from mec_cast_admin import topology as topo
from mec_cast_admin.protocol import HelloPayload, NodeType, StatusPayload, NodeState
from mec_cast_admin.registry import Registry
from mec_cast_admin.state import RunState
from mec_cast_admin.store import Run
from mec_cast_admin.workflow import diagnose

REPO = pathlib.Path(__file__).resolve().parents[3]


def make_run(state: RunState = RunState.RUNNING) -> Run:
    run = Run(run_id="0190d1f2-0000-7000-8000-000000000000", seq=1)
    run.state = state
    return run


def join(
    registry: Registry,
    node_type: NodeType,
    host: str,
    instance: int = 0,
    cell: str = "",
) -> str:
    node = f"{node_type}-{host}-{instance}"
    registry.on_hello(
        HelloPayload(node_type=node_type, node_id=node, host=host, cell=cell)
    )
    registry.on_status(
        node,
        StatusPayload(
            node_type=node_type,
            state=NodeState.RUNNING,
            run_id="0190d1f2-0000-7000-8000-000000000000",
        ),
    )
    return node


def write(tmp_path: pathlib.Path, text: str) -> pathlib.Path:
    path = tmp_path / "topology.yml"
    path.write_text(text, encoding="utf-8")
    return path


class TestDefaults:
    def test_no_path_yields_the_built_in_rules(self):
        spec = topo.load(None)
        assert not spec.declared
        assert spec.required_roles == (NodeType.CLIENT, NodeType.EDGE)

    def test_a_missing_file_is_not_an_error(self, tmp_path):
        # Declaring is opt-in: an absent file means "the platform's own
        # rules", which is how every deployment behaved before this existed.
        spec = topo.load(tmp_path / "nope.yml")
        assert not spec.declared
        assert spec.required_roles == (NodeType.CLIENT, NodeType.EDGE)

    def test_the_gnb_is_optional_but_still_reported(self):
        # The combination a single required/optional flag cannot express.
        gnb = topo.TopologySpec().role(NodeType.GNB)
        assert gnb.required is False
        assert gnb.absence == "warn"

    def test_the_renderer_is_optional_and_silent(self):
        render = topo.TopologySpec().role(NodeType.RENDER)
        assert render.required is False
        assert render.absence is None


class TestParsing:
    def test_nodes_get_derived_ids_matching_the_wire(self, tmp_path):
        spec = topo.load(write(tmp_path, """
nodes:
  - {role: client, host: ue01, instance: 1, cell: cell-a}
"""))
        assert spec.declared
        # Exactly what protocol.node_id() builds, or comparison is pointless.
        assert spec.nodes[0].node_id == "client-ue01-1"
        assert spec.nodes[0].cell == "cell-a"

    def test_cells_default_to_one(self, tmp_path):
        spec = topo.load(write(tmp_path, """
nodes:
  - {role: edge, host: mec01}
"""))
        assert spec.cells == (topo.DEFAULT_CELL,)

    def test_role_overrides_merge_rather_than_replace(self, tmp_path):
        # A file naming only the gNB must not drop the client/edge rules and
        # leave a fleet with no quorum requirement at all.
        spec = topo.load(write(tmp_path, """
roles:
  gnb: {required: true}
"""))
        assert spec.required_roles == (NodeType.CLIENT, NodeType.EDGE, NodeType.GNB)
        assert spec.role(NodeType.EDGE).required is True

    def test_a_duplicate_node_id_is_refused(self, tmp_path):
        with pytest.raises(topo.TopologyError, match="duplicate"):
            topo.load(write(tmp_path, """
nodes:
  - {role: client, host: ue01}
  - {role: client, host: ue01}
"""))

    def test_an_unknown_role_is_refused(self, tmp_path):
        with pytest.raises(topo.TopologyError, match="not one of"):
            topo.load(write(tmp_path, "nodes: [{role: sensor, host: ue01}]"))

    def test_a_node_without_a_host_is_refused(self, tmp_path):
        with pytest.raises(topo.TopologyError, match="host"):
            topo.load(write(tmp_path, "nodes: [{role: client}]"))

    def test_malformed_yaml_fails_loudly(self, tmp_path):
        # It must NOT degrade to "nothing declared": that would silently turn
        # every validation finding off, the opposite of what the operator
        # who wrote the file was asking for.
        with pytest.raises(topo.TopologyError):
            topo.load(write(tmp_path, "nodes: [oops\n"))

    def test_the_shipped_example_parses(self):
        # The example is documentation that can rot. Parsing it here means a
        # rename of a role or field breaks the build rather than an operator.
        spec = topo.load(REPO / "deploy/lab/topology.example.yml")
        assert spec.declared
        assert set(spec.cells) == {"cell-a", "cell-b"}
        assert "client-ue-a1-1" in {n.node_id for n in spec.nodes}


class TestValidation:
    def _spec(self, tmp_path) -> topo.TopologySpec:
        return topo.load(write(tmp_path, """
nodes:
  - {role: client, host: ue01, cell: cell-a}
  - {role: edge,   host: mec01, cell: cell-a}
"""))

    def test_an_undeclared_node_is_reported(self, tmp_path):
        registry = Registry()
        join(registry, NodeType.CLIENT, "ue01")
        join(registry, NodeType.EDGE, "mec01")
        join(registry, NodeType.CLIENT, "stray")
        codes = {
            f.subject
            for f in diagnose(registry, make_run(), topology=self._spec(tmp_path))
            if f.code == "WF_TOPOLOGY_UNDECLARED"
        }
        assert codes == {"client-stray-0"}

    def test_a_declared_node_that_never_arrives_is_reported(self, tmp_path):
        registry = Registry()
        join(registry, NodeType.CLIENT, "ue01")
        found = [
            f
            for f in diagnose(registry, make_run(), topology=self._spec(tmp_path))
            if f.code == "WF_TOPOLOGY_MISSING"
        ]
        assert [f.subject for f in found] == ["edge-mec01-0"]

    def test_a_matching_fleet_is_silent(self, tmp_path):
        registry = Registry()
        join(registry, NodeType.CLIENT, "ue01")
        join(registry, NodeType.EDGE, "mec01")
        codes = {
            f.code for f in diagnose(registry, make_run(), topology=self._spec(tmp_path))
        }
        assert not {c for c in codes if c.startswith("WF_TOPOLOGY")}

    def test_nothing_is_validated_without_a_declaration(self):
        # The default spec declares no nodes, so a fleet of anything is fine.
        registry = Registry()
        join(registry, NodeType.CLIENT, "whatever")
        join(registry, NodeType.EDGE, "elsewhere")
        codes = {f.code for f in diagnose(registry, make_run())}
        assert not {c for c in codes if c.startswith("WF_TOPOLOGY")}


class TestMermaid:
    def test_nothing_declared_draws_nothing(self):
        assert topo.to_mermaid(topo.TopologySpec()) == ""

    def test_cells_become_subgraphs_with_the_data_path(self, tmp_path):
        spec = topo.load(write(tmp_path, """
nodes:
  - {role: client, host: ue01,  cell: cell-a}
  - {role: gnb,    host: gnb01, cell: cell-a}
  - {role: edge,   host: mec01, cell: cell-a}
"""))
        out = topo.to_mermaid(spec)
        assert 'subgraph CELL0["cell-a"]' in out
        # client -> gnb -> edge, the architecture's own order.
        assert "N_client_ue01_0 --> N_gnb_gnb01_0" in out
        assert "N_gnb_gnb01_0 --> N_edge_mec01_0" in out

    def test_several_clients_fan_in_rather_than_chaining(self, tmp_path):
        # The bug a live run caught: walking a flat role-ordered list and
        # joining consecutive pairs drew client-0 --> client-1, a link that
        # does not exist. Each client reaches the gNB independently.
        spec = topo.load(write(tmp_path, """
nodes:
  - {role: client, host: ue01, instance: 0, cell: cell-a}
  - {role: client, host: ue01, instance: 1, cell: cell-a}
  - {role: gnb,    host: gnb01, cell: cell-a}
  - {role: edge,   host: mec01, cell: cell-a}
"""))
        out = topo.to_mermaid(spec)
        assert "N_client_ue01_0 --> N_client_ue01_1" not in out
        assert "N_client_ue01_0 --> N_gnb_gnb01_0" in out
        assert "N_client_ue01_1 --> N_gnb_gnb01_0" in out

    def test_a_cell_with_no_gnb_sends_clients_straight_to_the_edge(self, tmp_path):
        # The local topology: no radio, so the hop must not be dropped.
        spec = topo.load(write(tmp_path, """
nodes:
  - {role: client, host: ue01, cell: cell-a}
  - {role: edge,   host: mec01, cell: cell-a}
"""))
        assert "N_client_ue01_0 --> N_edge_mec01_0" in topo.to_mermaid(spec)

    def test_the_renderer_hangs_off_the_edge_downlink(self, tmp_path):
        spec = topo.load(write(tmp_path, """
nodes:
  - {role: client, host: ue01, cell: cell-a}
  - {role: edge,   host: mec01, cell: cell-a}
  - {role: render, host: ue01, cell: cell-a}
"""))
        # Dotted: the downlink only exists when publish_result is on.
        assert "N_edge_mec01_0 -.-> N_render_ue01_0" in topo.to_mermaid(spec)

    def test_ids_are_mermaid_safe(self, tmp_path):
        # Hyphens in a node_id break mermaid identifiers.
        spec = topo.load(write(tmp_path, "nodes: [{role: edge, host: mec-01}]"))
        out = topo.to_mermaid(spec)
        assert "N_edge_mec_01_0" in out
        assert "edge-mec-01-0[" not in out


class TestCellOnTheWire:
    """`cell` is additive under the extra="ignore" rule — no version bump."""

    def _spec(self, tmp_path) -> topo.TopologySpec:
        return topo.load(write(tmp_path, """
nodes:
  - {role: client, host: ue01, cell: cell-a}
  - {role: edge,   host: mec01, cell: cell-a}
"""))

    def test_a_node_in_the_wrong_cell_is_reported(self, tmp_path):
        # CELL set wrong on the host, or the wrong compose file deployed. The
        # node works perfectly; its samples just land in the wrong bucket.
        registry = Registry()
        join(registry, NodeType.CLIENT, "ue01", cell="cell-b")
        join(registry, NodeType.EDGE, "mec01", cell="cell-a")
        found = [
            f
            for f in diagnose(registry, make_run(), topology=self._spec(tmp_path))
            if f.code == "WF_TOPOLOGY_CELL_MISMATCH"
        ]
        assert [f.subject for f in found] == ["client-ue01-0"]
        assert "CELL=cell-a" in found[0].remedy

    def test_a_node_that_reports_no_cell_is_not_reported(self, tmp_path):
        # Every node predating this field, and every deployment that never
        # sets CELL. Silence is required or the finding fires fleet-wide the
        # moment anyone writes a topology file.
        registry = Registry()
        join(registry, NodeType.CLIENT, "ue01")
        join(registry, NodeType.EDGE, "mec01")
        codes = {
            f.code for f in diagnose(registry, make_run(), topology=self._spec(tmp_path))
        }
        assert "WF_TOPOLOGY_CELL_MISMATCH" not in codes

    def test_a_matching_cell_is_silent(self, tmp_path):
        registry = Registry()
        join(registry, NodeType.CLIENT, "ue01", cell="cell-a")
        join(registry, NodeType.EDGE, "mec01", cell="cell-a")
        codes = {
            f.code for f in diagnose(registry, make_run(), topology=self._spec(tmp_path))
        }
        assert not {c for c in codes if c.startswith("WF_TOPOLOGY")}

    def test_the_registry_keeps_what_the_node_reported(self):
        registry = Registry()
        node = join(registry, NodeType.EDGE, "mec01", cell="cell-b")
        assert registry.online()[0].cell == "cell-b"
        assert node in {r.node_id for r in registry.online()}
