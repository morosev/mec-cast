"""ran_policy: refused at the door, delivered to the xApp, reported if not applied.

The refusal cases are the same list ran/xapp/tests/test_core.py holds for the
xApp's own check — the admin validates first, the xApp is the authority, and
the two must agree on what is malformed.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mec_cast_admin import protocol as p
from mec_cast_admin.ranpolicy import validate_ran_policy
from mec_cast_admin.registry import Registry
from mec_cast_admin.schemas import RunCreate
from mec_cast_admin.workflow import diagnose
from test_workflow import join, make_run
from test_ws import hello, recv, recv_type, send

BAD = [
    {"type": "slicing"},
    {"ue": "abc"},
    {"max_prb_ratio": 130},
    {"min_prb_ratio": 50, "max_prb_ratio": 20},
    {"schedule": [{"t_s": 10}, {"t_s": 5}]},
]


@pytest.mark.parametrize("bad", BAD)
def test_the_admin_refuses_what_the_xapp_refuses(bad):
    with pytest.raises(ValueError):
        validate_ran_policy(bad)


def test_a_policy_is_normalised_on_create():
    body = RunCreate(params={"ran_policy": {"ue": "e2:3", "max_prb_ratio": 30}})
    assert body.params["ran_policy"]["ue"] == 3
    assert body.params["ran_policy"]["dedicated_prb_ratio"] == 100


def test_a_malformed_policy_is_a_422_not_a_failed_ack(client):
    with pytest.raises(ValidationError):
        RunCreate(params={"ran_policy": {"max_prb_ratio": 500}})
    r = client.post("/api/v1/runs", json={"params": {"ran_policy": {"max_prb_ratio": 500}}})
    assert r.status_code == 422


def test_a_policy_on_a_run_that_is_not_active_is_refused(client):
    run = client.post("/api/v1/runs", json={"label": "x"}).json()
    r = client.post(
        f"/api/v1/runs/{run['run_id']}/ran-policy", json={"ran_policy": {"max_prb_ratio": 30}}
    )
    assert r.status_code == 409


def test_a_mid_run_policy_reaches_the_xapp_and_only_the_xapp(client):
    run = client.post("/api/v1/runs", json={"label": "rc"}).json()
    xnode, xhello = hello(p.NodeType.XAPP, "ric01", autostart=True)
    enode, ehello = hello(p.NodeType.EDGE, "mec01")
    with client.websocket_connect("/ws/node") as xs, client.websocket_connect("/ws/node") as es:
        send(xs, p.MessageType.HELLO, xhello, node_id=xnode)
        recv(xs)
        send(es, p.MessageType.HELLO, ehello, node_id=enode)
        recv(es)
        client.post(f"/api/v1/runs/{run['run_id']}/start")
        recv_type(xs, p.MessageType.COMMAND)  # run.start
        recv_type(es, p.MessageType.COMMAND)

        r = client.post(
            f"/api/v1/runs/{run['run_id']}/ran-policy",
            json={"ran_policy": {"ue": 0, "max_prb_ratio": 30}},
        )
        assert r.status_code == 200, r.text
        assert r.json()["delivered_to"] == 1
        env, cmd = recv_type(xs, p.MessageType.COMMAND)
        assert cmd.command is p.CommandType.RAN_POLICY
        assert cmd.args["ran_policy"]["max_prb_ratio"] == 30

    saved = client.get("/api/v1/state").json()["runs"]
    mine = next(x for x in saved if x["run_id"] == run["run_id"])
    assert mine["params"]["ran_policy"]["max_prb_ratio"] == 30, "run.json names the policy"


def test_a_policy_the_xapp_could_not_apply_is_an_error():
    registry = Registry()
    join(
        registry,
        p.NodeType.XAPP,
        "ric01",
        params={
            "e2_connected": True,
            "policy_state": "failed",
            "policy_error": "no RIC_CONTROL_ACK within 5 s",
        },
    )
    found = [f for f in diagnose(registry, make_run()) if f.code == "WF_POLICY_NOT_APPLIED"]
    assert found and found[0].severity == "error"
    assert "RIC_CONTROL_ACK" in found[0].message
