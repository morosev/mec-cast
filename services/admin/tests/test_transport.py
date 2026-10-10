"""The nodes of one cell must share a Zenoh transport, and runs record it.

The transport is fixed when a node process starts -- one Zenoh session, one
link, from ZENOH_CONFIG_OVERRIDE -- and each node reports the scheme it
really dials. Nothing is declared per run any more: the admin compares what
the nodes report, raises WF_TRANSPORT_MISMATCH when a cell's nodes disagree
(its data would cross two links), and writes what the participants reported
into run.json as `zenoh_link`.
"""

from __future__ import annotations

import contextlib
import json
import pathlib
import time

from mec_cast_admin import protocol as p
from mec_cast_admin.registry import Registry
from mec_cast_admin.store import Run
from mec_cast_admin.workflow import diagnose
from test_multicell import connect, report, start_run


def _node(reg: Registry, node_type: str, host: str, transport: str, cell: str = "") -> str:
    node = p.node_id(node_type, host, 0)
    reg.on_hello(p.HelloPayload(node_type=node_type, node_id=node, host=host, cell=cell))
    reg.on_status(
        node,
        p.StatusPayload(
            node_type=node_type,
            state=p.NodeState.RUNNING,
            run_id="r1",
            params={"transport": transport} if transport else {},
        ),
    )
    return node


def mismatches(reg: Registry) -> list:
    return [f for f in diagnose(reg, None) if f.code == "WF_TRANSPORT_MISMATCH"]


class TestTransportMismatch:
    def test_nodes_on_one_link_are_silent(self):
        reg = Registry()
        _node(reg, "client", "ue01", "tcp")
        _node(reg, "edge", "mec01", "tcp")
        _node(reg, "render", "ue01", "tcp")
        assert mismatches(reg) == []

    def test_the_odd_node_is_named_against_the_majority(self):
        reg = Registry()
        _node(reg, "client", "ue01", "udp-rel1")
        _node(reg, "edge", "mec01", "tcp")
        _node(reg, "render", "ue01", "tcp")
        found = mismatches(reg)
        assert [f.subject for f in found] == ["client-ue01-0"]
        assert found[0].severity == "error"
        assert "'udp-rel1'" in found[0].message and "'tcp'" in found[0].message
        assert "ZENOH_CONFIG_OVERRIDE" in found[0].remedy

    def test_it_is_reported_before_any_run_exists(self):
        """A deployment fault, so it shows on an idle platform too."""
        reg = Registry()
        _node(reg, "client", "ue01", "quic")
        _node(reg, "edge", "mec01", "tcp")
        assert len(mismatches(reg)) == 1

    def test_nodes_without_zenoh_are_not_compared(self):
        """The gNB collector and the xApp have no Zenoh session. A transport
        they report -- an older collector sent its udp/ws metrics feed as
        `transport` -- is not a link and must not be compared."""
        reg = Registry()
        _node(reg, "client", "ue01", "tcp")
        _node(reg, "edge", "mec01", "tcp")
        _node(reg, "gnb", "gnb01", "ws")
        _node(reg, "xapp", "ric01", "udp")
        assert mismatches(reg) == []

    def test_udp_reliability_is_part_of_the_identity(self):
        """udp ?rel=1 and ?rel=0 are different links -- ADR-0006's sweep
        turns on exactly that distinction."""
        reg = Registry()
        _node(reg, "client", "ue01", "udp-rel0")
        _node(reg, "edge", "mec01", "udp-rel1")
        assert len(mismatches(reg)) == 1

    def test_cells_are_judged_separately(self):
        """Two cells may legitimately run different links."""
        reg = Registry()
        _node(reg, "client", "ue-a", "tcp", cell="a")
        _node(reg, "edge", "mec-a", "tcp", cell="a")
        _node(reg, "client", "ue-b", "quic", cell="b")
        _node(reg, "edge", "mec-b", "quic", cell="b")
        assert mismatches(reg) == []


class TestTheRunRecordsItsLink:
    def _run(self, *transports):
        run = Run(run_id="r1", seq=1)
        for i, t in enumerate(transports):
            run.participants[f"n{i}"] = {"role": "client", **({"transport": t} if t else {})}
        return run

    def test_one_link_is_recorded_as_that_link(self):
        assert self._run("tcp", "tcp", None).to_manifest()["zenoh_link"] == "tcp"

    def test_disagreement_is_recorded_as_mixed(self):
        assert self._run("tcp", "quic").to_manifest()["zenoh_link"] == "mixed"

    def test_nothing_reported_is_recorded_as_unknown(self):
        assert self._run(None).to_manifest()["zenoh_link"] is None

    def test_the_middleware_field_is_not_the_link(self):
        manifest = self._run("quic").to_manifest()
        assert manifest["transport"] == "rmw_zenoh_cpp"
        assert manifest["zenoh_link"] == "quic"


def test_a_runs_participants_record_the_link_they_reported(client, settings):
    """End to end through the service: nodes join a run, report their link,
    and run.json says which link the run's data crossed."""
    with contextlib.ExitStack() as stack:
        kinds = (p.NodeType.CLIENT, p.NodeType.EDGE)
        nodes = [
            connect(client, stack, kinds[0], "ue01", "default"),
            connect(client, stack, kinds[1], "mec01", "default"),
        ]
        run_id = start_run(client)
        client.post(f"/api/v1/runs/{run_id}/start")
        for (node, socket), kind in zip(nodes, kinds, strict=True):
            report(socket, node, kind, run_id, params={"transport": "tcp"})
        manifest = pathlib.Path(settings.runs_dir) / run_id / "run.json"
        body: dict = {}
        for _ in range(50):
            body = json.loads(manifest.read_text()) if manifest.exists() else {}
            if body.get("zenoh_link"):
                break
            time.sleep(0.05)
        assert body["zenoh_link"] == "tcp"
        assert {v.get("transport") for v in body["participants"].values()} == {"tcp"}


def test_a_gnb_participant_does_not_make_the_link_mixed(client, settings):
    """The gNB collector's metrics feed (ws) is not the run's Zenoh link."""
    with contextlib.ExitStack() as stack:
        kinds = (p.NodeType.CLIENT, p.NodeType.EDGE, p.NodeType.GNB)
        hosts = ("ue01", "mec01", "gnb01")
        links = ("tcp", "tcp", "ws")
        nodes = [connect(client, stack, k, h, "default") for k, h in zip(kinds, hosts, strict=True)]
        run_id = start_run(client)
        client.post(f"/api/v1/runs/{run_id}/start")
        for (node, socket), kind, link in zip(nodes, kinds, links, strict=True):
            report(socket, node, kind, run_id, params={"transport": link})
        manifest = pathlib.Path(settings.runs_dir) / run_id / "run.json"
        body: dict = {}
        for _ in range(50):
            body = json.loads(manifest.read_text()) if manifest.exists() else {}
            if len(body.get("participants", {})) == 3 and body.get("zenoh_link"):
                break
            time.sleep(0.05)
        assert body["zenoh_link"] == "tcp"
        gnb = next(v for v in body["participants"].values() if v["role"] == "gnb")
        assert "transport" not in gnb
