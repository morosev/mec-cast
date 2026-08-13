"""Host-side e2e: full compose topology with netem impairment.

    zenoh-router <- publisher(+netem 20ms) -> edge -> logging service

Asserts that (1) per-frame CSV lands with the injected delay visible,
(2) aggregated snapshots reach the logging service joined by trace_id.

Run (docker + built images required; first run builds them):

    make test-e2e            # or: pytest tests/e2e -v

Uses only the standard library so it runs on the bare WSL host python.
"""

import csv
import json
import os
import pathlib
import statistics
import subprocess
import time
import urllib.parse
import urllib.request
import uuid

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
COMPOSE = [
    "docker", "compose",
    "-f", "deploy/compose/logging.yml",
    "-f", "deploy/compose/local.yml",
]


def logging_build_context() -> str | None:
    """Resolve the logging-service build context, relative to the compose file.

    The services/logging submodule is the normal source and what the compose
    files already point at, so the usual answer is None. The sibling-checkout
    fallback only covers a clone made without --recurse-submodules; it keeps
    the suite runnable instead of failing on a missing pyproject.toml.
    """
    if (REPO / "services" / "logging" / "pyproject.toml").exists():
        return None  # compose default already points here
    sibling = REPO.parent / "mec-cast-logging-service"
    if (sibling / "pyproject.toml").exists():
        return f"../../../{sibling.name}"
    raise RuntimeError(
        "No logging service source found. Populate the services/logging "
        "submodule or place mec-cast-logging-service beside the repo."
    )

RATE_HZ = 10.0
DURATION_S = 30
NETEM_DELAY_MS = 20


def compose(args, env, check=True):
    return subprocess.run(
        COMPOSE + args, cwd=REPO, env=env, check=check,
        capture_output=True, text=True, timeout=900,
    )


def wait_http_ok(url: str, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    last_err = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return
        except Exception as e:  # noqa: BLE001 - retry loop
            last_err = e
        time.sleep(1)
    raise TimeoutError(f"{url} not ready after {timeout_s}s: {last_err}")


def get_json(url: str):
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read())


@pytest.fixture(scope="module")
def e2e_run():
    run_id = str(uuid.uuid4())
    env = dict(
        os.environ,
        RUN_ID=run_id,
        NETEM_DELAY=f"{NETEM_DELAY_MS}ms",
        NETEM_JITTER="2ms",
        NETEM_LOSS="0%",
        RATE_HZ=str(RATE_HZ),
        NUM_POINTS="10000",
        SEED="42",
    )
    ctx = logging_build_context()
    if ctx is not None:
        env["MECLOG_BUILD_CONTEXT"] = ctx
    compose(["up", "-d", "--build"], env)
    try:
        wait_http_ok("http://localhost:8000/health/ready", 90)
        time.sleep(DURATION_S)
        # Graceful stop so recorders drain, flush, and post final snapshots.
        compose(["stop", "-t", "15", "lidar-client", "edge"], env)
        yield run_id
    finally:
        compose(["down", "-v", "--remove-orphans"], env, check=False)


def test_edge_csv_has_expected_rows_and_delay(e2e_run):
    run_id = e2e_run
    csv_path = REPO / "runs" / run_id / "edge" / "samples.csv"
    assert csv_path.exists(), f"edge CSV missing: {csv_path}"

    with csv_path.open() as f:
        rows = list(csv.DictReader(f))

    expected = RATE_HZ * DURATION_S
    assert len(rows) >= 0.7 * expected, (
        f"only {len(rows)} rows for ~{expected:.0f} published frames"
    )

    network_ns = [int(r["network_ns"]) for r in rows if r["network_ns"]]
    assert network_ns, "no network delay values recorded"
    p50_ms = statistics.median(network_ns) / 1e6
    # Same-host clock: p50 must reflect the injected one-way delay.
    assert NETEM_DELAY_MS <= p50_ms <= NETEM_DELAY_MS + 150, (
        f"median network delay {p50_ms:.1f} ms, expected ≈{NETEM_DELAY_MS} ms"
    )

    # e2e (capture -> processed) must be >= network by construction.
    e2e_ns = [int(r["e2e_ns"]) for r in rows if r["e2e_ns"]]
    assert statistics.median(e2e_ns) >= statistics.median(network_ns)


def test_snapshots_reached_logging_service(e2e_run):
    run_id = e2e_run
    q = urllib.parse.urlencode({"service": "mec-cast-edge", "trace_id": run_id})
    page = get_json(f"http://localhost:8000/api/v1/logs?{q}")
    items = page["items"]
    # 2s cadence over the run duration, generous lower bound.
    assert len(items) >= DURATION_S // 2 - 2, f"only {len(items)} snapshots"

    ctx = items[0]["context"]
    network = ctx["metrics"]["network"]
    assert network["count"] > 0
    p50_ms = network["p50_ns"] / 1e6
    assert NETEM_DELAY_MS <= p50_ms <= NETEM_DELAY_MS + 150
    assert "stddev_ns" in network, "jitter must be emitted"
    assert ctx["ptp"]["reliable"] is False, "same-host runs must not claim PTP"

    # The publisher side logs under its own service, same trace_id.
    q = urllib.parse.urlencode({"service": "mec-cast-pub", "trace_id": run_id})
    pub_page = get_json(f"http://localhost:8000/api/v1/logs?{q}")
    assert pub_page["items"], "no publisher snapshots joined by trace_id"
