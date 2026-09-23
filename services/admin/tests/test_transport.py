"""A run's declared transport must match what the nodes are actually on.

The transport is fixed when a node process starts -- one Zenoh session, one
link, opened at rclpy.init(). The admin cannot switch it for a run, so the
run RECORDS which transport produced its data and this check makes a wrong
record loud. A campaign labelled "quic" that ran on tcp is worse than one
with no label, because nothing downstream can tell the difference.
"""

from __future__ import annotations

from mec_cast_admin import protocol as p
from mec_cast_admin.registry import Registry
from mec_cast_admin.workflow import diagnose


def _node(reg: Registry, node_type: str, host: str, transport: str) -> str:
    node = p.node_id(node_type, host, 0)
    reg.on_hello(p.HelloPayload(node_type=node_type, node_id=node, host=host, cell=""))
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


class _Run:
    """The parts of a Run that diagnose() reads."""

    def __init__(self, params):
        self.run_id = "r1"
        self.cell = ""
        self.params = params
        self.state = None
        self.reports = {}
        self.participants = {}


def codes(findings):
    return [f.code for f in findings]


class TestTransportMismatch:
    def test_a_node_on_the_wrong_transport_is_reported(self):
        reg = Registry()
        _node(reg, "client", "ue", "tcp")
        found = diagnose(reg, _Run({"transport": "quic"}))
        assert "WF_TRANSPORT_MISMATCH" in codes(found)
        hit = next(f for f in found if f.code == "WF_TRANSPORT_MISMATCH")
        assert hit.severity == "error"
        assert "quic" in hit.message and "tcp" in hit.message

    def test_a_matching_transport_is_silent(self):
        """The common case must not produce noise, or the check gets ignored."""
        reg = Registry()
        _node(reg, "client", "ue", "quic")
        assert "WF_TRANSPORT_MISMATCH" not in codes(diagnose(reg, _Run({"transport": "quic"})))

    def test_a_run_that_declares_nothing_is_silent(self):
        """Transport is optional: an unrecorded run must not be nagged."""
        reg = Registry()
        _node(reg, "client", "ue", "tcp")
        assert "WF_TRANSPORT_MISMATCH" not in codes(diagnose(reg, _Run({})))

    def test_a_node_that_reports_nothing_is_silent(self):
        """An older node predating the field must not look like a mismatch."""
        reg = Registry()
        _node(reg, "client", "ue", "")
        assert "WF_TRANSPORT_MISMATCH" not in codes(diagnose(reg, _Run({"transport": "quic"})))

    def test_udp_reliability_is_part_of_the_identity(self):
        """udp?rel=1 and udp?rel=0 are different experiments, not one
        transport -- ADR-0006's whole sweep turns on that distinction."""
        reg = Registry()
        _node(reg, "client", "ue", "udp-rel0")
        assert "WF_TRANSPORT_MISMATCH" in codes(diagnose(reg, _Run({"transport": "udp-rel1"})))
