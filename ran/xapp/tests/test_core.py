"""The core, the policy rules and crash recovery, against a fake RIC."""

import json

import pytest

from mec_cast_xapp.app import Xapp
from mec_cast_xapp.capabilities.rc_control import MARKER, PolicyError, RcControl, parse_policy
from mec_cast_xapp.core import E2Port, load_capabilities


class FakePort(E2Port):
    name = "fake"

    def __init__(self, nodes=("gnb-1",)):
        self.nodes = list(nodes)
        self.controls = []
        self.subs = {}

    def connected_nodes(self):
        return list(self.nodes)

    def subscribe_kpm(self, node, style, metrics, ue_ids, period_ms, callback):
        self.subs["s1"] = callback
        return "s1"

    def unsubscribe(self, handle):
        self.subs.pop(handle, None)

    def control_prb_quota(self, node, ue_id, min_ratio, max_ratio, dedicated_ratio, callback=None):
        self.controls.append((node, ue_id, min_ratio, max_ratio))
        if callback:
            callback(True, "")


def test_unknown_capabilities_are_refused_not_ignored():
    with pytest.raises(ValueError, match="unknown capability"):
        load_capabilities(["kpm_monitor", "kpm_monitr"])


@pytest.mark.parametrize(
    "bad",
    [
        {"type": "slicing"},
        {"ue": "abc"},
        {"max_prb_ratio": 130},
        {"min_prb_ratio": 50, "max_prb_ratio": 20},
        {"schedule": [{"t_s": 10}, {"t_s": 5}]},
    ],
)
def test_bad_policies_are_refused(bad):
    with pytest.raises(PolicyError):
        parse_policy(bad)


def test_a_policy_is_normalised_and_its_schedule_inherits():
    p = parse_policy(
        {
            "ue": "e2:3",
            "max_prb_ratio": 30,
            "schedule": [{"t_s": 10, "max_prb_ratio": 60}, {"t_s": 20, "min_prb_ratio": 10}],
        }
    )
    assert p["ue"] == 3 and p["min_prb_ratio"] == 0 and p["max_prb_ratio"] == 30
    assert p["schedule"][1] == {
        "min_prb_ratio": 10,
        "max_prb_ratio": 60,
        "dedicated_prb_ratio": 100,
        "t_s": 20.0,
    }


def make_app(tmp_path, port):
    return Xapp(
        port,
        load_capabilities(["kpm_monitor", "rc_control"]),
        runs_dir=str(tmp_path),
        logging_url=None,
    )


def test_a_run_applies_its_policy_and_reverts_it_on_stop(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path))
    port = FakePort()
    app = make_app(tmp_path, port)
    app.start_run("run-a", {"ran_policy": {"ue": 0, "max_prb_ratio": 30}})
    assert port.controls == [("gnb-1", 0, 0, 30)]
    assert (tmp_path / MARKER).exists(), "the policy in force must be on disk"
    app.stop_run()
    assert port.controls[-1] == ("gnb-1", 0, 0, 100)
    assert not (tmp_path / MARKER).exists()
    lines = (tmp_path / "run-a" / "ran-kpm" / "control.csv").read_text().splitlines()
    actions = [line.split(",")[3] + ":" + line.split(",")[7] for line in lines[1:]]
    assert actions == ["apply:sent", "apply:ack", "revert-run-stop:sent", "revert-run-stop:ack"]


def test_a_refused_policy_fails_the_command(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path))
    app = make_app(tmp_path, FakePort())
    app.start_run("run-a")
    with pytest.raises(ValueError, match="rc_control"):
        app.apply_policy({"max_prb_ratio": 500})
    app.stop_run()


def test_a_crashed_xapps_policy_is_reverted_at_the_next_boot(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path))
    (tmp_path / MARKER).write_text(json.dumps({"node": "gnb-1", "ue": 4, "run_id": "old"}))
    port = FakePort(nodes=())
    rc = RcControl()
    assert rc.on_boot(port) is False, "no E2 node yet: keep the marker, try again"
    assert (tmp_path / MARKER).exists() and port.controls == []
    port.nodes = ["gnb-1"]
    assert rc.on_boot(port) is True
    assert port.controls == [("gnb-1", 4, 0, 100)]
    assert not (tmp_path / MARKER).exists()


def test_losing_the_admin_reverts_the_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path))
    port = FakePort()
    app = make_app(tmp_path, port)
    app.start_run("run-a", {"ran_policy": {"ue": 1, "max_prb_ratio": 20}})
    app.ctx.admin_lost_s = lambda: 31.0
    app.tick()
    assert port.controls[-1] == ("gnb-1", 1, 0, 100)
    rc = next(c for c in app.capabilities if c.name == "rc_control")
    assert rc.params()["policy_state"] == "reverted"
    app.stop_run()
    assert port.controls.count(("gnb-1", 1, 0, 100)) == 1, "reverted once, not twice"


def test_boot_never_reverts_this_processes_own_policy(tmp_path, monkeypatch):
    """The marker of a policy THIS process applied is not a crash leftover —
    the bug the first version of on_boot had: reverting the live policy on
    the first tick after a run started."""
    monkeypatch.setenv("RUNS_DIR", str(tmp_path))
    port = FakePort()
    app = make_app(tmp_path, port)
    app.boot()
    app.start_run("run-a", {"ran_policy": {"ue": 0, "max_prb_ratio": 30}})
    app.tick()
    app.tick()
    assert port.controls == [("gnb-1", 0, 0, 30)], "only the apply; nothing reverted it"
    assert (tmp_path / MARKER).exists()
    app.stop_run()
