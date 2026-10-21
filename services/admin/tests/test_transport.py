"""The nodes of one zone must share a middleware and a link, and runs record it.

Both are fixed when a node process starts -- one Zenoh or Fast DDS session,
one link, from MIDDLEWARE / MIDDLEWARE_LINK (ADR-0013) -- and each node
reports what it really runs (`params.middleware`, `params.transport`). Nothing
is declared per run: the admin compares what the nodes report, raises
WF_TRANSPORT_MISMATCH when a zone's nodes disagree, shows each zone's
middleware in its snapshot, and writes what the participants reported into
run.json as `middleware`.
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
from test_multizone import connect, report, start_run


def _node(reg: Registry, node_type: str, host: str, mw: str, zone: str = "") -> str:
    """mw: "zenoh/tcp", "dds/udp", ... or "" for a node that reports none."""
    node = p.node_id(node_type, host, 0)
    reg.on_hello(p.HelloPayload(node_type=node_type, node_id=node, host=host, zone=zone))
    name, _, link = mw.partition("/")
    params = {"middleware": name, "transport": link} if mw else {}
    reg.on_status(
        node,
        p.StatusPayload(node_type=node_type, state=p.NodeState.RUNNING, run_id="r1", params=params),
    )
    return node


def mismatches(reg: Registry) -> list:
    return [f for f in diagnose(reg, None) if f.code == "WF_TRANSPORT_MISMATCH"]


class TestTransportMismatch:
    def test_nodes_on_one_middleware_and_link_are_silent(self):
        reg = Registry()
        _node(reg, "client", "ue01", "dds/udp")
        _node(reg, "edge", "mec01", "dds/udp")
        _node(reg, "render", "ue01", "dds/udp")
        assert mismatches(reg) == []

    def test_another_middleware_is_named_against_the_majority(self):
        reg = Registry()
        _node(reg, "client", "ue01", "dds/udp")
        _node(reg, "edge", "mec01", "zenoh/tcp")
        _node(reg, "render", "ue01", "zenoh/tcp")
        found = mismatches(reg)
        assert [f.subject for f in found] == ["client-ue01-0"]
        assert found[0].severity == "error"
        assert "'dds/udp'" in found[0].message and "'zenoh/tcp'" in found[0].message
        assert "MIDDLEWARE" in found[0].remedy

    def test_another_link_of_the_same_middleware_is_a_mismatch_too(self):
        reg = Registry()
        _node(reg, "client", "ue01", "zenoh/quic")
        _node(reg, "edge", "mec01", "zenoh/tcp")
        assert len(mismatches(reg)) == 1

    def test_it_is_reported_before_any_run_exists(self):
        """A deployment fault, so it shows on an idle platform too."""
        reg = Registry()
        _node(reg, "client", "ue01", "dds/tcp")
        _node(reg, "edge", "mec01", "dds/udp")
        assert len(mismatches(reg)) == 1

    def test_nodes_without_a_ros_session_are_not_compared(self):
        """The gNB collector and the xApp have no middleware session. A
        transport they report -- the collector's udp/ws metrics feed -- is not
        a link and must not be compared."""
        reg = Registry()
        _node(reg, "client", "ue01", "zenoh/tcp")
        _node(reg, "edge", "mec01", "zenoh/tcp")
        _node(reg, "gnb", "gnb01", "dds/ws")
        _node(reg, "xapp", "ric01", "dds/udp")
        assert mismatches(reg) == []

    def test_zones_are_judged_separately(self):
        """Two zones may legitimately run different middlewares."""
        reg = Registry()
        _node(reg, "client", "ue-a", "zenoh/tcp", zone="a")
        _node(reg, "edge", "mec-a", "zenoh/tcp", zone="a")
        _node(reg, "client", "ue-b", "dds/udp", zone="b")
        _node(reg, "edge", "mec-b", "dds/udp", zone="b")
        assert mismatches(reg) == []


class TestMiddlewareOf:
    def test_name_and_link(self):
        assert p.middleware_of({"middleware": "dds", "transport": "udp"}) == "dds/udp"
        assert p.middleware_of({"middleware": "zenoh", "transport": ""}) == "zenoh"
        assert p.middleware_of({"transport": "ws"}) is None, "a link alone is no middleware"
        assert p.middleware_of(None) is None


class TestTheRunRecordsItsMiddleware:
    def _run(self, *kinds):
        run = Run(run_id="r1", seq=1)
        for i, kind in enumerate(kinds):
            entry: dict = {"role": "client"}
            if kind:
                name, _, link = kind.partition("/")
                entry.update(middleware=name, transport=link)
            run.participants[f"n{i}"] = entry
        return run

    def test_one_middleware_is_recorded_with_its_rmw_and_link(self):
        assert self._run("dds/udp", "dds/udp", None).to_manifest()["middleware"] == {
            "name": "dds",
            "rmw": "rmw_fastrtps_cpp",
            "link": "udp",
        }
        assert self._run("zenoh/quic").to_manifest()["middleware"]["rmw"] == "rmw_zenoh_cpp"

    def test_disagreement_is_recorded_as_mixed(self):
        assert self._run("zenoh/tcp", "dds/udp").to_manifest()["middleware"]["name"] == "mixed"
        assert self._run("dds/udp", "dds/tcp").to_manifest()["middleware"]["name"] == "mixed"

    def test_nothing_reported_is_recorded_as_none(self):
        assert self._run(None).to_manifest()["middleware"] is None

    def test_the_old_fields_are_gone(self):
        manifest = self._run("zenoh/tcp").to_manifest()
        assert "zenoh_link" not in manifest and "transport" not in manifest


def _wait_manifest(settings, run_id, pred):
    manifest = pathlib.Path(settings.runs_dir) / run_id / "run.json"
    body: dict = {}
    for _ in range(50):
        body = json.loads(manifest.read_text()) if manifest.exists() else {}
        if pred(body):
            break
        time.sleep(0.05)
    return body


def test_a_runs_participants_record_the_middleware_they_reported(client, settings):
    """End to end through the service: nodes join a run, report their
    middleware, and run.json and the snapshot say which one the run used."""
    with contextlib.ExitStack() as stack:
        kinds = (p.NodeType.CLIENT, p.NodeType.EDGE)
        nodes = [
            connect(client, stack, kinds[0], "ue01", "default"),
            connect(client, stack, kinds[1], "mec01", "default"),
        ]
        run_id = start_run(client)
        client.post(f"/api/v1/runs/{run_id}/start")
        for (node, socket), kind in zip(nodes, kinds, strict=True):
            report(socket, node, kind, run_id, params={"middleware": "dds", "transport": "udp"})
        body = _wait_manifest(settings, run_id, lambda b: b.get("middleware"))
        assert body["middleware"] == {"name": "dds", "rmw": "rmw_fastrtps_cpp", "link": "udp"}
        assert {v.get("middleware") for v in body["participants"].values()} == {"dds"}
        state = client.get("/api/v1/state").json()
        assert state["middleware"] == {"default": "dds/udp"}


def test_a_gnb_participant_does_not_make_the_middleware_mixed(client, settings):
    """The gNB collector's metrics feed is not the run's middleware."""
    with contextlib.ExitStack() as stack:
        kinds = (p.NodeType.CLIENT, p.NodeType.EDGE, p.NodeType.GNB)
        hosts = ("ue01", "mec01", "gnb01")
        params = (
            {"middleware": "zenoh", "transport": "tcp"},
            {"middleware": "zenoh", "transport": "tcp"},
            {"middleware": "dds", "transport": "ws"},
        )
        nodes = [connect(client, stack, k, h, "default") for k, h in zip(kinds, hosts, strict=True)]
        run_id = start_run(client)
        client.post(f"/api/v1/runs/{run_id}/start")
        for (node, socket), kind, prm in zip(nodes, kinds, params, strict=True):
            report(socket, node, kind, run_id, params=prm)
        body = _wait_manifest(
            settings, run_id, lambda b: len(b.get("participants", {})) == 3 and b.get("middleware")
        )
        assert body["middleware"]["name"] == "zenoh"
        gnb = next(v for v in body["participants"].values() if v["role"] == "gnb")
        assert "middleware" not in gnb
