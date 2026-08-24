"""The workflow diagnostics: what is wrong, and what to do about it."""

from __future__ import annotations

from mec_cast_admin.protocol import HelloPayload, NodeState, NodeType, Peer, StatusPayload
from mec_cast_admin.registry import Registry
from mec_cast_admin.state import RunState
from mec_cast_admin.store import Run
from mec_cast_admin.workflow import diagnose


def make_run(state: RunState = RunState.RUNNING) -> Run:
    run = Run(run_id="0190d1f2-0000-7000-8000-000000000000", seq=1)
    run.state = state
    return run


def join(registry: Registry, node_type: NodeType, host: str, **status) -> None:
    """Connect a node and give it a status in one step."""
    node = f"{node_type}-{host}-0"
    registry.on_hello(HelloPayload(node_type=node_type, node_id=node, host=host))
    registry.on_status(
        node,
        StatusPayload(
            node_type=node_type,
            state=status.pop("state", NodeState.RUNNING),
            run_id=status.pop("run_id", "0190d1f2-0000-7000-8000-000000000000"),
            **status,
        ),
    )


def codes(registry: Registry, run: Run | None, **kwargs) -> set[str]:
    return {f.code for f in diagnose(registry, run, **kwargs)}


class TestMissingRoles:
    def test_a_run_with_no_edge_says_so(self):
        registry = Registry()
        join(registry, NodeType.CLIENT, "ue01", streaming=True)
        assert "WF_EDGE_ABSENT" in codes(registry, make_run())

    def test_a_run_with_no_client_says_so(self):
        registry = Registry()
        join(registry, NodeType.EDGE, "mec01", subscribed=True)
        assert "WF_CLIENT_ABSENT" in codes(registry, make_run())

    def test_a_missing_gnb_is_a_warning_not_an_error(self):
        registry = Registry()
        join(registry, NodeType.CLIENT, "ue01", streaming=True)
        join(registry, NodeType.EDGE, "mec01", subscribed=True)
        gnb = [f for f in diagnose(registry, make_run()) if f.code == "WF_GNB_ABSENT"]
        assert gnb and gnb[0].severity == "warn"

    def test_nothing_is_reported_when_no_run_is_active(self):
        # An idle platform with no nodes is not a fault.
        assert codes(Registry(), make_run(RunState.DRAFT)) == set()


class TestSilentFailures:
    def test_a_qos_mismatch_is_detected(self):
        # The failure the node docstrings call out: publisher best_effort and
        # subscriber reliable deliver nothing, with no error anywhere.
        registry = Registry()
        join(
            registry, NodeType.CLIENT, "ue01", streaming=True, params={"reliability": "best_effort"}
        )
        join(
            registry,
            NodeType.EDGE,
            "mec01",
            subscribed=True,
            params={"reliability": "reliable"},
            peers=[Peer(peer_id="/mec_cast_lidar_client")],
        )
        found = [f for f in diagnose(registry, make_run()) if f.code == "WF_QOS_MISMATCH"]
        assert found
        assert "best_effort" in found[0].message and "reliable" in found[0].message

    def test_matching_qos_is_not_reported(self):
        registry = Registry()
        join(registry, NodeType.CLIENT, "ue01", streaming=True, params={"reliability": "reliable"})
        join(
            registry,
            NodeType.EDGE,
            "mec01",
            subscribed=True,
            params={"reliability": "reliable"},
            peers=[Peer(peer_id="/mec_cast_lidar_client")],
        )
        assert "WF_QOS_MISMATCH" not in codes(registry, make_run())

    def test_a_publisher_with_no_subscriber_is_only_reported_after_the_grace(self):
        registry = Registry()
        join(registry, NodeType.CLIENT, "ue01", streaming=True)
        join(registry, NodeType.EDGE, "mec01", subscribed=True, peers=[])
        # It has only just started streaming: innocent.
        assert "WF_NO_PEER" not in codes(registry, make_run())

        client = registry.get("client-ue01-0")
        client.streaming_since -= 30.0  # now it has been streaming a while
        assert "WF_NO_PEER" in codes(registry, make_run())

    def test_frames_leaving_but_not_arriving_is_detected(self):
        registry = Registry()
        join(registry, NodeType.CLIENT, "ue01", streaming=True, counters={"frames_published": 100})
        join(
            registry,
            NodeType.EDGE,
            "mec01",
            subscribed=True,
            peers=[Peer(peer_id="/mec_cast_lidar_client")],
            counters={"frames": 50},
        )
        registry.snapshot_counters()
        # Next pass: the client advances, the edge does not.
        registry.on_status(
            "client-ue01-0",
            StatusPayload(
                node_type=NodeType.CLIENT,
                state=NodeState.RUNNING,
                streaming=True,
                run_id=make_run().run_id,
                counters={"frames_published": 200},
            ),
        )
        assert "WF_NO_FRAMES" in codes(registry, make_run())

    def test_a_first_pass_is_never_read_as_flat(self):
        # With no previous sample there is nothing to compare against, and
        # guessing produces exactly the false positive this guards.
        registry = Registry()
        join(registry, NodeType.CLIENT, "ue01", streaming=True, counters={"frames_published": 100})
        join(
            registry,
            NodeType.EDGE,
            "mec01",
            subscribed=True,
            peers=[Peer(peer_id="/x")],
            counters={"frames": 100},
        )
        assert "WF_NO_FRAMES" not in codes(registry, make_run())


class TestGnb:
    def test_a_silent_collector_is_reported(self):
        registry = Registry()
        join(registry, NodeType.GNB, "gnb01", counters={"datagrams": 10})
        registry.snapshot_counters()
        registry.on_status(
            "gnb-gnb01-0",
            StatusPayload(
                node_type=NodeType.GNB,
                state=NodeState.RUNNING,
                run_id=make_run().run_id,
                counters={"datagrams": 10},
            ),
        )
        assert "WF_GNB_SILENT" in codes(registry, make_run())

    def test_a_busy_collector_is_not_reported(self):
        registry = Registry()
        join(registry, NodeType.GNB, "gnb01", counters={"datagrams": 10})
        registry.snapshot_counters()
        registry.on_status(
            "gnb-gnb01-0",
            StatusPayload(
                node_type=NodeType.GNB,
                state=NodeState.RUNNING,
                run_id=make_run().run_id,
                counters={"datagrams": 99},
            ),
        )
        assert "WF_GNB_SILENT" not in codes(registry, make_run())


class TestConsistency:
    def test_a_node_on_the_wrong_run_is_reported(self):
        registry = Registry()
        join(registry, NodeType.EDGE, "mec01", subscribed=True, run_id="some-other-run")
        assert "WF_RUN_MISMATCH" in codes(registry, make_run())

    def test_version_skew_is_reported(self):
        registry = Registry()
        registry.on_hello(
            HelloPayload(
                node_type=NodeType.EDGE,
                node_id="edge-mec01-0",
                host="mec01",
                version={"sha": "aaaaaaa", "tag": ""},
            )
        )
        assert "WF_VERSION_SKEW" in codes(registry, make_run(), admin_sha="bbbbbbb")

    def test_matching_versions_are_not_reported(self):
        registry = Registry()
        registry.on_hello(
            HelloPayload(
                node_type=NodeType.EDGE,
                node_id="edge-mec01-0",
                host="mec01",
                version={"sha": "aaaaaaa", "tag": ""},
            )
        )
        assert "WF_VERSION_SKEW" not in codes(registry, make_run(), admin_sha="aaaaaaa")

    def test_a_lost_participant_is_reported(self):
        registry = Registry()
        join(registry, NodeType.CLIENT, "ue01", streaming=True)
        join(registry, NodeType.EDGE, "mec01", subscribed=True, peers=[Peer(peer_id="/x")])
        lost = registry.get("edge-mec01-0")
        lost.last_seen -= 120.0  # silent for two minutes
        assert "WF_PARTICIPANT_LOST" in codes(registry, make_run())

    def test_a_node_that_said_goodbye_is_not_reported_as_lost(self):
        # A clean exit is not a crash and must not read as one.
        registry = Registry()
        join(registry, NodeType.CLIENT, "ue01", streaming=True)
        join(registry, NodeType.EDGE, "mec01", subscribed=True)
        registry.on_goodbye("edge-mec01-0")
        registry.get("edge-mec01-0").last_seen -= 120.0
        assert "WF_PARTICIPANT_LOST" not in codes(registry, make_run())


class TestContract:
    def test_every_finding_carries_an_actionable_remedy(self):
        # A diagnostic that says something is wrong without saying what to do
        # costs attention and returns nothing.
        registry = Registry()
        join(
            registry, NodeType.CLIENT, "ue01", streaming=True, params={"reliability": "best_effort"}
        )
        join(registry, NodeType.EDGE, "mec01", subscribed=False, params={"reliability": "reliable"})
        findings = diagnose(registry, make_run(), admin_sha="zzz")
        assert findings
        for finding in findings:
            assert finding.remedy.strip(), finding.code
            assert finding.message.strip(), finding.code
            assert finding.severity in {"error", "warn", "info"}

    def test_errors_sort_before_warnings(self):
        registry = Registry()
        join(registry, NodeType.CLIENT, "ue01", streaming=True)
        findings = diagnose(registry, make_run())
        severities = [f.severity for f in findings]
        assert severities == sorted(severities, key=lambda s: {"error": 0, "warn": 1}[s])

    def test_findings_are_serialisable(self):
        registry = Registry()
        join(registry, NodeType.CLIENT, "ue01", streaming=True)
        for finding in diagnose(registry, make_run()):
            assert set(finding.to_dict()) == {"code", "severity", "subject", "message", "remedy"}


class TestRenderer:
    """The renderer is optional, so only a *starved* one is a fault."""

    def _registry_with(self, render_frames_second: int) -> Registry:
        registry = Registry()
        join(registry, NodeType.CLIENT, "ue01", streaming=True, counters={"frames_published": 100})
        join(
            registry,
            NodeType.EDGE,
            "mec01",
            subscribed=True,
            peers=[Peer(peer_id="/mec_cast_lidar_client")],
            counters={"frames": 100},
        )
        join(registry, NodeType.RENDER, "ue01", subscribed=True, counters={"frames": 10})
        registry.snapshot_counters()
        # Second pass: the edge keeps processing; the renderer may or may not
        # be receiving.
        registry.on_status(
            "edge-mec01-0",
            StatusPayload(
                node_type=NodeType.EDGE,
                state=NodeState.RUNNING,
                subscribed=True,
                run_id=make_run().run_id,
                peers=[Peer(peer_id="/mec_cast_lidar_client")],
                counters={"frames": 200},
            ),
        )
        registry.on_status(
            "render-ue01-0",
            StatusPayload(
                node_type=NodeType.RENDER,
                state=NodeState.RUNNING,
                subscribed=True,
                run_id=make_run().run_id,
                counters={"frames": render_frames_second},
            ),
        )
        return registry

    def test_a_starved_renderer_is_detected(self):
        # The edge is processing but sending nothing down — publish_result is
        # off by default, which is exactly the trap this catches.
        found = [
            f
            for f in diagnose(self._registry_with(10), make_run())
            if f.code == "WF_RENDER_STARVED"
        ]
        assert found
        assert "publish_result" in found[0].remedy

    def test_a_fed_renderer_is_not_reported(self):
        assert "WF_RENDER_STARVED" not in codes(self._registry_with(60), make_run())

    def test_a_missing_renderer_is_not_reported_at_all(self):
        # Unlike the gNB there is not even a warning: a run with no viewer is
        # the normal case, not a degraded one.
        registry = Registry()
        join(registry, NodeType.CLIENT, "ue01", streaming=True)
        join(registry, NodeType.EDGE, "mec01", subscribed=True)
        assert not any("RENDER" in c for c in codes(registry, make_run()))
