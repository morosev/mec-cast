"""The xApp end to end against gnb-sim's E2 feed: the real sim adapter, the
real capabilities, normaliser and recorder — everything the lab runs except
the RIC itself."""

import csv
import json
import threading
import time
from pathlib import Path

import gnb_sim

from mec_cast_xapp.adapters.sim import SimPort
from mec_cast_xapp.app import Xapp
from mec_cast_xapp.core import load_capabilities

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "collector"
    / "testdata"
    / "srsran_ws_metrics.synthetic.jsonl"
)


def start_sim():
    e2 = gnb_sim.E2Sim("gnbd_test_0")
    server = e2.serve("127.0.0.1:0")
    port = server.socket.getsockname()[1]
    report = json.loads(FIXTURE.read_text().splitlines()[0])
    stop = threading.Event()

    def loop():  # what gnb_sim.main does each period: cap, then publish
        while not stop.wait(0.1):
            r = json.loads(json.dumps(report))
            e2.apply_caps(r)
            e2.update(r)

    threading.Thread(target=loop, daemon=True).start()
    return e2, server, port, stop


def wait_for(pred, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.1)
    return False


def test_kpm_and_a_prb_quota_through_the_sim(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path))
    e2, server, port, stop = start_sim()
    sim = SimPort(f"127.0.0.1:{port}")
    app = Xapp(
        sim,
        load_capabilities(["kpm_monitor", "rc_control"]),
        runs_dir=str(tmp_path),
        logging_url=None,
    )
    try:
        assert sim.connected_nodes() == ["gnbd_test_0"]
        app.start_run("run-sim", {"ran_policy": {"ue": 0, "max_prb_ratio": 25}})
        assert wait_for(lambda: e2.caps.get(0) == 25), "the cap never reached the gNB"

        out = tmp_path / "run-sim" / "ran-kpm"
        assert wait_for(lambda: app.ctx.recorder.indications >= 2), "no KPM indications"
        status = app.status()
        assert status["params"]["e2_connected"] is True
        assert status["params"]["policy_state"] == "applied"
        # KPM sees the capped throughput: 118234567 bit/s x 25 %.
        assert abs(status["counters"]["ue_dl_throughput_bps"] - 118234567 * 0.25) < 2000
    finally:
        app.stop_run()
    assert wait_for(lambda: 0 not in e2.caps), "the policy outlived its run"

    rows = list(csv.DictReader((out / "kpi.csv").open()))
    assert {r["metric"] for r in rows} >= {"ue.dl_throughput_bps", "ue.ul_throughput_bps"}
    assert all(r["source"] == "kpm" and r["ue"] == "e2:0" for r in rows)
    actions = [
        r["action"] + ":" + r["outcome"] for r in csv.DictReader((out / "control.csv").open())
    ]
    assert actions == ["apply:sent", "apply:ack", "revert-run-stop:sent", "revert-run-stop:ack"]
    assert (out / "indications.jsonl").read_text().count("\n") >= 2

    sim.close()
    stop.set()
    server.shutdown()
