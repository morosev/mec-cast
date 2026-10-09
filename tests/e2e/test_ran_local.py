"""Host-side e2e: the RAN collector against a simulated gNB, per transport.

    gnb-sim --(UDP and WebSocket at once)--> ran-collector -> logging service
                                                  \\-> runs/<id>/ran/samples.csv

srsRAN has exported its JSON metrics over UDP (<= 24.x) and over the
remote_control WebSocket (25.04+). gnb-sim speaks both simultaneously, which
is the hard case for the collector: whichever source it is told to use, every
report must be recorded exactly once.

Only the RAN services and the logging backend start — no ROS image, no
pipeline — so this is the cheapest of the e2e suites.

    make test-e2e            # or: pytest tests/e2e/test_ran_local.py -v
"""

import csv
import json
import os
import pathlib
import subprocess
import time
import urllib.parse
import urllib.request
import uuid

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
COMPOSE = [
    "docker",
    "compose",
    "-f",
    "deploy/compose/logging.yml",
    "-f",
    "deploy/compose/local.yml",
    "-f",
    "deploy/compose/ran.yml",
]
SERVICES = ["postgres", "logging", "gnb-sim", "ran-collector"]
PERIOD_MS = 250
DURATION_S = 8


def compose(args, env, check=True):
    return subprocess.run(
        COMPOSE + args,
        cwd=REPO,
        env=env,
        check=check,
        capture_output=True,
        text=True,
        timeout=900,
    )


def wait_http_ok(url: str, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return
        except Exception:  # noqa: BLE001, S110 - retry loop
            pass
        time.sleep(1)
    raise TimeoutError(f"{url} not ready after {timeout_s}s")


def logged_entries(run_id: str) -> list[dict]:
    query = urllib.parse.urlencode(
        {"trace_id": run_id, "service": "mec-cast-ran", "limit": 1000}
    )
    with urllib.request.urlopen(
        f"http://localhost:8000/api/v1/logs?{query}", timeout=10
    ) as r:
        body = json.loads(r.read())
    return body["items"] if isinstance(body, dict) else body


@pytest.fixture(scope="module", params=["udp", "ws", "auto"])
def ran_run(request):
    source = request.param
    run_id = str(uuid.uuid4())
    env = dict(
        os.environ, RUN_ID=run_id, RAN_SOURCE=source, SIM_PERIOD_MS=str(PERIOD_MS)
    )

    up = compose(["up", "-d", "--build", *SERVICES], env, check=False)
    if up.returncode != 0:
        pytest.fail("compose up failed:\n" + (up.stderr or "")[-3000:])
    try:
        wait_http_ok("http://localhost:8000/health/ready", 90)
        time.sleep(DURATION_S)
        # Stop the collector first so it flushes its last batch and CSV rows.
        compose(["stop", "-t", "10", "ran-collector"], env)
        logs = compose(["logs", "ran-collector", "gnb-sim"], env, check=False).stdout
        yield source, run_id, logs
    finally:
        compose(["down", "--remove-orphans"], env, check=False)


def test_reports_land_in_this_runs_directory(ran_run):
    source, run_id, logs = ran_run
    path = REPO / "runs" / run_id / "ran" / "samples.csv"
    assert path.exists(), f"[{source}] no CSV at {path}\n{logs[-3000:]}"
    with path.open() as f:
        rows = list(csv.DictReader(f))
    assert len(rows) >= 5, f"[{source}] only {len(rows)} rows\n{logs[-3000:]}"


def test_each_report_is_recorded_once(ran_run):
    """gnb-sim sends every report on both transports. One row per report."""
    source, run_id, logs = ran_run
    with (REPO / "runs" / run_id / "ran" / "samples.csv").open() as f:
        rows = list(csv.DictReader(f))
    # Upper bound: the reports the sim could have produced while recording,
    # with slack for startup. Doubling would be ~2x this.
    ceiling = (DURATION_S + 4) * 1000 / PERIOD_MS
    assert len(rows) <= ceiling, (
        f"[{source}] {len(rows)} rows for at most {ceiling:.0f} reports: "
        f"recorded on both transports?\n{logs[-3000:]}"
    )


def test_the_gnb_timestamp_gives_a_small_positive_lag(ran_run):
    """send_ns is the gNB's own stamp, recv_ns the arrival: same host, so the
    difference is the metrics pipeline's lag and must be small and positive."""
    source, run_id, _ = ran_run
    with (REPO / "runs" / run_id / "ran" / "samples.csv").open() as f:
        lags = [int(r["network_ns"]) for r in csv.DictReader(f) if r["network_ns"]]
    assert lags, f"[{source}] no network_ns: the gNB timestamp was not parsed"
    # srsRAN stamps to the millisecond, so a report can look up to 1 ms early.
    assert all(-1_000_000 <= lag < 1_000_000_000 for lag in lags), (
        f"[{source}] implausible lag(s): {sorted(lags)[:3]} .. {sorted(lags)[-3:]}"
    )


def test_kpis_reach_the_logging_service_with_clock_evidence(ran_run):
    source, run_id, _ = ran_run
    entries = logged_entries(run_id)
    assert len(entries) >= 5, f"[{source}] only {len(entries)} KPI entries logged"
    ctx = entries[0]["context"]
    assert ctx["kpi"]["cells"][0]["ue_list"][0]["rnti"] == 17921
    assert ctx["gnb_ts_ns"] > 0
    assert ctx["ptp"]["reliable"] is False  # same host, no PHC: honest


def test_the_configured_transport_is_the_one_used(ran_run):
    source, _, logs = ran_run
    collector = "\n".join(l for l in logs.splitlines() if "ran-collector" in l)
    if source == "ws":
        assert "subscribed" in collector, collector[-2000:]
    if source == "udp":
        assert "subscribed" not in collector, collector[-2000:]
    if source == "auto":
        assert "auto: srsRAN is sending over" in collector, collector[-2000:]
