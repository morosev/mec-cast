"""Clock skew caught from the frames the admin already receives.

The lab ran two hosts 11.15 s apart with every local indicator green: each
was perfectly disciplined against its own reference, and neither reference
was the other's. No per-host check can see that, and the cross-host one
(`verify-ptp.sh --peer`) only runs when a human remembers.

Every envelope has always carried the node's CLOCK_REALTIME in `ts_ns` --
the same clock its recorder stamps samples with. The admin was discarding it.
"""

from __future__ import annotations

import contextlib
import json
import time

from fastapi.testclient import TestClient

from mec_cast_admin import protocol as p
from mec_cast_admin.app import create_app
from mec_cast_admin.config import Settings

SKEW_NS = 11_151_080_964  # the real one, from ran-4


def settings_with(tmp_path, **overrides):
    base = dict(
        runs_dir=str(tmp_path),
        keepalive_s=0.05,
        offline_timeout_s=30.0,
        start_timeout_s=0.5,
        diagnostics_interval_s=0.05,
        ui_broadcast_min_interval_s=0.01,
        max_run_duration_s=0,
        min_free_gb_start=0,
        min_free_gb_abort=0,
    )
    base.update(overrides)
    return Settings(**base)


def connect_skewed(client, stack, node_type, host, skew_ns):
    """A node whose clock is `skew_ns` behind the admin's."""
    node = p.node_id(node_type, host, 0)
    socket = stack.enter_context(client.websocket_connect("/ws/node"))
    frame = p.build(
        p.MessageType.HELLO,
        p.HelloPayload(node_type=node_type, node_id=node, host=host, cell=""),
        node_id=node,
    )
    frame["ts_ns"] = p.now_ns() - skew_ns
    socket.send_text(json.dumps(frame))
    socket.receive_text()
    return node, socket


def findings_for(client, code):
    body = client.get("/api/v1/state").json()
    return [f for f in body.get("findings", []) if f["code"] == code]


def wait_for_finding(client, code, timeout=4.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        hits = findings_for(client, code)
        if hits:
            return hits
        time.sleep(0.05)
    return findings_for(client, code)


class TestClockOffset:
    def test_a_skewed_node_is_reported_before_any_run_starts(self, tmp_path):
        s = settings_with(tmp_path)
        with TestClient(create_app(s)) as client, contextlib.ExitStack() as stack:
            connect_skewed(client, stack, "client", "ran-4", SKEW_NS)
            hits = wait_for_finding(client, "WF_CLOCK_OFFSET")
            assert hits, "an 11 s skew must be reported"
            assert hits[0]["severity"] == "error"
            assert "11.15" in hits[0]["message"]

    def test_a_healthy_node_is_not_reported(self, tmp_path):
        """The check must be silent on a good fleet, or it is noise."""
        s = settings_with(tmp_path)
        with TestClient(create_app(s)) as client, contextlib.ExitStack() as stack:
            connect_skewed(client, stack, "client", "ok-host", 0)
            time.sleep(0.5)
            assert not findings_for(client, "WF_CLOCK_OFFSET")

    def test_a_node_ahead_is_reported_too(self, tmp_path):
        """Sign is not assumed: the receiver may be either side."""
        s = settings_with(tmp_path)
        with TestClient(create_app(s)) as client, contextlib.ExitStack() as stack:
            connect_skewed(client, stack, "client", "fast-host", -SKEW_NS)
            hits = wait_for_finding(client, "WF_CLOCK_OFFSET")
            assert hits, "a node AHEAD of the admin must also be reported"
            assert "ahead of" in hits[0]["message"]

    def test_when_every_node_is_adrift_the_admin_is_blamed(self, tmp_path):
        """Otherwise an operator 'fixes' a healthy fleet. The odd one out is
        whichever clock they all disagree with."""
        s = settings_with(tmp_path)
        with TestClient(create_app(s)) as client, contextlib.ExitStack() as stack:
            connect_skewed(client, stack, "client", "h1", SKEW_NS)
            connect_skewed(client, stack, "edge", "h2", SKEW_NS)
            hits = wait_for_finding(client, "WF_CLOCK_OFFSET")
            assert hits
            assert any("ADMIN" in f["message"] for f in hits), (
                "with every node adrift, the finding must point at the admin"
            )

    def test_zero_disables_the_check(self, tmp_path):
        s = settings_with(tmp_path, clock_offset_warn_ns=0)
        with TestClient(create_app(s)) as client, contextlib.ExitStack() as stack:
            connect_skewed(client, stack, "client", "ran-4", SKEW_NS)
            time.sleep(0.5)
            assert not findings_for(client, "WF_CLOCK_OFFSET")

    def test_the_offset_is_visible_on_the_node_row(self, tmp_path):
        """The finding says something is wrong; the number says how much."""
        s = settings_with(tmp_path)
        with TestClient(create_app(s)) as client, contextlib.ExitStack() as stack:
            node, _ = connect_skewed(client, stack, "client", "ran-4", SKEW_NS)
            time.sleep(0.3)
            rows = client.get("/api/v1/state").json()["nodes"]
            row = next(r for r in rows if r["node_id"] == node)
            assert row["clock_offset_ns"] is not None
            assert abs(row["clock_offset_ns"] - SKEW_NS) < 1_000_000_000
