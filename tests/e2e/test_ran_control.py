"""Host-side e2e: a run-scoped RAN policy, end to end, under the admin.

    admin --run.start{ran_policy}--> xapp --E2 (sim)--> gnb-sim
                                      |                    | caps UE 0
                                      v                    v
                               ran-kpm/{kpi,control}.csv   ran/kpi.csv (JSON tap)

Everything the lab runs except the radio and the RIC: the LiDAR pipeline,
the collector, the xApp with the `sim` adapter, the admin. The run carries
``ran_policy``: cap E2 UE 0 at 25 % of PRBs, lift it at 8 s. The test asserts
the three things ADR-0011 promises:

1. every control action is recorded with its outcome (control.csv);
2. its effect is visible in BOTH RAN sources — the KPM rows and the JSON
   tap's — so the data shows the cause beside the effect;
3. the policy does not outlive the run, and no finding fires.

    pytest tests/e2e/test_ran_control.py -v
"""

import csv
import json
import os
import pathlib
import statistics
import subprocess
import time
import urllib.request

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
COMPOSE = [
    "docker", "compose",
    "-f", "deploy/compose/logging.yml",
    "-f", "deploy/compose/local.yml",
    "-f", "deploy/compose/admin.yml",
    "-f", "deploy/compose/ran.yml",
]
ADMIN = "http://localhost:8099"
CAP = 25
STEP_S = 8
RUN_S = 16


def compose(args, env, check=True):
    return subprocess.run(
        COMPOSE + args, cwd=REPO, env=env, check=check,
        capture_output=True, text=True, timeout=1200,
    )


def api(path, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        ADMIN + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read() or b"{}")


def wait_until(pred, timeout, what):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = pred()
            if last:
                return last
        except Exception as e:  # noqa: BLE001 - retry loop
            last = e
        time.sleep(1)
    raise TimeoutError(f"{what} not reached in {timeout}s (last: {last})")


def rows(path):
    with path.open() as f:
        return list(csv.DictReader(f))


@pytest.fixture(scope="module")
def controlled_run():
    env = dict(os.environ, ADMIN_URL="ws://admin:8099/ws/node", NETEM_LOSS="0%",
               SIM_PERIOD_MS="250", RUN_ID="")
    up = compose(["up", "-d", "--build", "--scale", "netem=0"], env, check=False)
    if up.returncode != 0:
        pytest.fail("compose up failed:\n" + (up.stderr or "")[-3000:])
    try:
        # Nodes started before the admin retry every 30 s: wait for all four.
        wait_until(
            lambda: {n["node_type"] for n in api("/api/v1/state")["nodes"] if n["online"]}
            >= {"client", "edge", "gnb", "xapp"},
            120, "client, edge, gnb and xapp online",
        )
        policy = {"ue": 0, "max_prb_ratio": CAP, "schedule": [{"t_s": STEP_S, "max_prb_ratio": 100}]}
        run = api("/api/v1/runs", "POST", {"label": "e2e-ran-control",
                                           "params": {"ran_policy": policy}})
        rid = run["run_id"]
        api(f"/api/v1/runs/{rid}/start", "POST")
        time.sleep(STEP_S / 2)
        mid_findings = {f["code"] for f in api("/api/v1/state")["findings"]}
        mid_xapp = next(n for n in api("/api/v1/state")["nodes"] if n["node_type"] == "xapp")
        time.sleep(RUN_S - STEP_S / 2)
        api(f"/api/v1/runs/{rid}/stop", "POST")
        time.sleep(8)  # the revert's ack, the recorders' flush
        logs = compose(["logs", "xapp", "gnb-sim"], env, check=False).stdout
        yield rid, mid_findings, mid_xapp, logs
    finally:
        compose(["down", "--remove-orphans"], env, check=False)


def test_the_run_names_its_policy(controlled_run):
    rid, *_ = controlled_run
    manifest = json.loads((REPO / "runs" / rid / "run.json").read_text())
    assert manifest["params"]["ran_policy"]["max_prb_ratio"] == CAP


def test_every_control_action_is_recorded_with_its_outcome(controlled_run):
    rid, _, _, logs = controlled_run
    actions = [r["action"] + ":" + r["outcome"]
               for r in rows(REPO / "runs" / rid / "ran-kpm" / "control.csv")]
    assert actions == [
        "apply:sent", "apply:ack",
        f"step@{STEP_S}s:sent", f"step@{STEP_S}s:ack",
        "revert-run-stop:sent", "revert-run-stop:ack",
    ], logs[-3000:]


def _capped_ratio(rid, source_csv, ue):
    """median throughput while capped / median after the step lifted it."""
    ctl = rows(REPO / "runs" / rid / "ran-kpm" / "control.csv")
    applied = int(next(r["ts_ns"] for r in ctl if r["action"] == "apply" and r["outcome"] == "ack"))
    lifted = int(next(r["ts_ns"] for r in ctl if r["action"].startswith("step") and r["outcome"] == "ack"))
    reverted = int(next(r["ts_ns"] for r in ctl if r["action"].startswith("revert") and r["outcome"] == "sent"))
    settle = 1_500_000_000  # one KPM period plus slack
    vals = [(int(r["recv_ns"]), float(r["value"])) for r in rows(source_csv)
            if r["metric"] == "ue.dl_throughput_bps" and r["ue"] == ue]
    capped = [v for t, v in vals if applied + settle < t < lifted]
    free = [v for t, v in vals if lifted + settle < t < reverted]
    assert capped and free, f"no samples in a window: {len(capped)} capped, {len(free)} free"
    return statistics.median(capped) / statistics.median(free)


def test_the_cap_shows_in_the_kpm_source(controlled_run):
    rid, *_ = controlled_run
    ratio = _capped_ratio(rid, REPO / "runs" / rid / "ran-kpm" / "kpi.csv", "e2:0")
    assert abs(ratio - CAP / 100) < 0.06, ratio


def test_the_cap_shows_in_the_json_tap_too(controlled_run):
    """The effect seen by the OTHER source: the cause in control.csv, the
    effect in ran/kpi.csv, on one clock."""
    rid, *_ = controlled_run
    ratio = _capped_ratio(rid, REPO / "runs" / rid / "ran" / "kpi.csv", "17921")
    assert abs(ratio - CAP / 100) < 0.06, ratio


def test_nothing_was_wrong_mid_run(controlled_run):
    _, findings, xapp, _ = controlled_run
    assert xapp["params"]["policy_state"] == "applied", xapp["params"]
    assert xapp["params"]["e2_connected"] is True
    bad = findings & {"WF_POLICY_NOT_APPLIED", "WF_RAN_SOURCES_DISAGREE",
                      "WF_KPM_SILENT", "WF_XAPP_NO_E2", "WF_GNB_SILENT"}
    assert not bad, bad


def test_the_policy_did_not_outlive_the_run(controlled_run):
    _, _, _, logs = controlled_run
    quotas = [line for line in logs.splitlines() if "PRB quota ue=0" in line]
    assert quotas and quotas[-1].rstrip().endswith("max=100"), quotas
