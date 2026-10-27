"""The nodes of one zone must use one point-cloud codec, and runs record it.

Each of the lidar client, the edge and the renderer listens or publishes only
on its codec's topics (ADR-0014), so a zone whose nodes disagree carries
nothing between them, and nothing on the wire says why. A run's `codec`
parameter sets every node alike; the admin flags what the nodes report, and
writes what each participant ran into run.json.
"""

from __future__ import annotations

import contextlib

import pytest

from mec_cast_admin import protocol as p
from mec_cast_admin.registry import Registry
from mec_cast_admin.workflow import diagnose
from test_multizone import connect, report, start_run
from test_transport import _wait_manifest


def _node(
    reg: Registry,
    node_type: str,
    host: str,
    codec: str | None,
    state: p.NodeState = p.NodeState.IDLE,
    zone: str = "",
) -> str:
    node = p.node_id(node_type, host, 0)
    reg.on_hello(p.HelloPayload(node_type=node_type, node_id=node, host=host, zone=zone))
    params = {"codec": codec} if codec else {}
    reg.on_status(
        node, p.StatusPayload(node_type=node_type, state=state, run_id="r1", params=params)
    )
    return node


def mismatches(reg: Registry) -> list:
    return [f for f in diagnose(reg, None) if f.code == "WF_CODEC_MISMATCH"]


class TestCodecMismatch:
    def test_one_codec_is_silent(self):
        reg = Registry()
        for kind, host in (("client", "ue01"), ("edge", "mec01"), ("render", "ue01")):
            _node(reg, kind, host, "gpcc")
        assert mismatches(reg) == []

    def test_idle_nodes_that_disagree_are_a_warning(self):
        """Only the configured defaults differ: a run naming its codec would
        set every node alike."""
        reg = Registry()
        _node(reg, "client", "ue01", "gpcc")
        _node(reg, "edge", "mec01", "none")
        _node(reg, "render", "ue01", "none")
        found = mismatches(reg)
        assert [f.subject for f in found] == ["client-ue01-0"]
        assert found[0].severity == "warning"
        assert "params.codec" in found[0].remedy and "CODEC" in found[0].remedy

    def test_running_nodes_that_disagree_are_an_error(self):
        reg = Registry()
        _node(reg, "client", "ue01", "gpcc", p.NodeState.RUNNING)
        _node(reg, "edge", "mec01", "none", p.NodeState.RUNNING)
        found = mismatches(reg)
        assert len(found) == 1 and found[0].severity == "error"
        assert "nothing is flowing" in found[0].message

    def test_a_tie_names_the_coded_node(self):
        """With one node each way, `none` -- the default -- is taken as the
        intended codec, so the finding names the node that departs from it."""
        reg = Registry()
        _node(reg, "client", "ue01", "gpcc")
        _node(reg, "edge", "mec01", "none")
        assert [f.subject for f in mismatches(reg)] == ["client-ue01-0"]

    def test_nodes_that_report_no_codec_are_not_compared(self):
        """The gNB collector and the xApp carry no point clouds; neither do
        nodes from before ADR-0014."""
        reg = Registry()
        _node(reg, "client", "ue01", "gpcc")
        _node(reg, "edge", "mec01", "gpcc")
        _node(reg, "gnb", "gnb01", "none")
        _node(reg, "render", "ue01", None)
        assert mismatches(reg) == []

    def test_zones_are_judged_separately(self):
        reg = Registry()
        _node(reg, "client", "ue-a", "gpcc", zone="a")
        _node(reg, "edge", "mec-a", "gpcc", zone="a")
        _node(reg, "client", "ue-b", "none", zone="b")
        _node(reg, "edge", "mec-b", "none", zone="b")
        assert mismatches(reg) == []


class TestRunParams:
    @pytest.mark.parametrize(
        "params",
        [
            {"codec": "draco"},
            {"codec": "gpcc", "codec_precision_m": 0},
            {"codec": "gpcc", "codec_precision_m": -0.01},
            {"codec": "gpcc", "codec_precision_m": "1mm"},
            {"codec": "gpcc", "codec_precision_m": True},
        ],
    )
    def test_a_bad_codec_is_refused_when_the_run_is_created(self, client, params):
        response = client.post("/api/v1/runs", json={"label": "", "params": params})
        assert response.status_code == 422, response.text

    def test_a_good_codec_is_kept(self, client):
        params = {"codec": "gpcc", "codec_precision_m": 0.01}
        response = client.post("/api/v1/runs", json={"label": "", "params": params})
        assert response.status_code == 201, response.text
        assert response.json()["params"] == params


def test_each_participant_records_the_codec_it_ran(client, settings):
    """From the RUNNING report: before it a node reports its default, which
    the run's codec overrides."""
    with contextlib.ExitStack() as stack:
        kinds = (p.NodeType.CLIENT, p.NodeType.EDGE, p.NodeType.GNB)
        hosts = ("ue01", "mec01", "gnb01")
        params = (
            {"codec": "gpcc", "codec_precision_m": 0.001},
            {"codec": "gpcc"},
            {"codec": "gpcc"},
        )
        nodes = [connect(client, stack, k, h, "default") for k, h in zip(kinds, hosts, strict=True)]
        run_id = start_run(client)
        client.post(f"/api/v1/runs/{run_id}/start")
        for (node, socket), kind, prm in zip(nodes, kinds, params, strict=True):
            report(socket, node, kind, run_id, params=prm)
        body = _wait_manifest(
            settings,
            run_id,
            lambda b: (
                len(b.get("participants", {})) == 3
                and all("codec" in v for v in b["participants"].values() if v["role"] != "gnb")
            ),
        )
        by_role = {v["role"]: v for v in body["participants"].values()}
        assert by_role["client"]["codec"] == "gpcc"
        assert by_role["client"]["codec_precision_m"] == 0.001
        assert by_role["edge"]["codec"] == "gpcc"
        assert "codec_precision_m" not in by_role["edge"]
        assert "codec" not in by_role["gnb"], "the gNB collector carries no clouds"
