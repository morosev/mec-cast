"""Host-side e2e: a run created and driven entirely by the admin service.

    admin(:8099) <-ws-> lidar-client, edge          zenoh-router in between

Asserts that a run started from the control plane reaches `running`, produces
per-frame CSV under the admin-minted run id, and reaches `stopped` with the
recorders' final reports collected. Also that a missing role is reported with
an actionable finding rather than a silent stall.

**No RUN_ID is set anywhere.** That is the point: the admin names the run.
`test_e2e_latency.py` covers the opposite path and must keep passing without
an admin at all.

Run (docker + built images required):

    pytest tests/e2e/test_admin_run.py -v

Standard library only, like its sibling.
"""

import json
import pathlib
import subprocess
import time
import urllib.error
import urllib.request

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
COMPOSE = [
    "docker", "compose",
    "-f", "deploy/compose/logging.yml",
    "-f", "deploy/compose/local.yml",
    "-f", "deploy/compose/admin.yml",
]
ADMIN = "http://localhost:8099"

NUM_POINTS = 3000
RATE_HZ = 10.0
STREAM_S = 12


def compose(args, check=True):
    return subprocess.run(
        COMPOSE + args, cwd=REPO, check=check, capture_output=True, text=True
    )


def api(path, method="GET", body=None, timeout=10):
    request = urllib.request.Request(
        f"{ADMIN}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"} if body is not None else {},
        method=method,
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def wait_until(predicate, timeout, what):
    """Poll until `predicate(state)` holds, or fail naming what was awaited."""
    deadline = time.monotonic() + timeout
    state = None
    while time.monotonic() < deadline:
        try:
            state = api("/api/v1/state")
        except (urllib.error.URLError, OSError):
            time.sleep(1.0)
            continue
        if predicate(state):
            return state
        time.sleep(1.0)
    pytest.fail(f"timed out waiting for {what}; last state: {json.dumps(state)[:600]}")


def run_row(state, run_id):
    return next((r for r in state["runs"] if r["run_id"] == run_id), None)


@pytest.fixture(scope="module")
def topology():
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        pytest.skip("docker is not available")

    compose(["up", "-d", "--build"])
    try:
        # The admin answers before the nodes have dialled in; wait for both.
        wait_until(
            lambda s: {n["node_type"] for n in s["nodes"] if n["online"]}
            >= {"client", "edge"},
            timeout=180,
            what="the client and edge nodes to subscribe",
        )
        yield
    finally:
        compose(["down", "-v", "--remove-orphans"], check=False)


def test_a_run_created_from_the_admin_records_and_stops(topology):
    created = api(
        "/api/v1/runs",
        method="POST",
        body={
            "label": "e2e-admin",
            "params": {"num_points": NUM_POINTS, "rate_hz": RATE_HZ, "seed": 3},
        },
    )
    run_id = created["run_id"]
    assert created["state"] == "draft"
    assert created["allowed"] == ["start", "remove"]

    api(f"/api/v1/runs/{run_id}/start", method="POST")
    state = wait_until(
        lambda s: (row := run_row(s, run_id)) and row["state"] == "running",
        timeout=90,
        what="the run to reach `running`",
    )
    row = run_row(state, run_id)
    # Both required roles must have joined, or quorum was declared wrongly.
    roles = {p["role"] for p in row["participants"].values()}
    assert {"client", "edge"} <= roles, row["participants"]

    time.sleep(STREAM_S)

    # The CSVs land under the id the admin minted, not any RUN_ID from the env.
    run_dir = REPO / "runs" / run_id
    for site in ("pub", "edge"):
        csv_path = run_dir / site / "samples.csv"
        assert csv_path.exists(), f"no {site} CSV at {csv_path}"
        assert len(csv_path.read_text().splitlines()) > 1, f"{site} CSV has no rows"

    api(f"/api/v1/runs/{run_id}/stop", method="POST")
    state = wait_until(
        lambda s: (row := run_row(s, run_id)) and row["state"] == "stopped",
        timeout=90,
        what="the run to reach `stopped`",
    )
    row = run_row(state, run_id)
    assert row["allowed"] == ["remove"]
    # Every participant returns its recorder accounting on stop.
    assert len(row["reports"]) >= 2, row["reports"]
    for report in row["reports"].values():
        assert report["samples_written"] > 0

    # And which host holds which directory is recorded — new information in
    # the lab, where each node writes to its own machine.
    assert {"pub", "edge"} <= set(row["sites"])

    sizes = [(run_dir / s / "samples.csv").stat().st_size for s in ("pub", "edge")]
    time.sleep(3)
    assert [
        (run_dir / s / "samples.csv").stat().st_size for s in ("pub", "edge")
    ] == sizes, "CSVs still growing after stop"


def test_a_missing_role_is_reported_with_a_remedy(topology):
    """The requirement: say what to do when the workflow is not established."""
    compose(["stop", "-t", "10", "edge"])
    try:
        run = api("/api/v1/runs", method="POST", body={"label": "e2e-no-edge"})
        api(f"/api/v1/runs/{run['run_id']}/start", method="POST")

        state = wait_until(
            lambda s: any(f["code"] == "WF_EDGE_ABSENT" for f in s["findings"]),
            timeout=90,
            what="WF_EDGE_ABSENT to be reported",
        )
        finding = next(f for f in state["findings"] if f["code"] == "WF_EDGE_ABSENT")
        assert finding["severity"] == "error"
        # A diagnostic without a remedy is a worse version of silence.
        assert finding["remedy"].strip()

        api(f"/api/v1/runs/{run['run_id']}/stop", method="POST")
    finally:
        compose(["start", "edge"], check=False)
