"""Host-side e2e: the return path and the round trip that does not need PTP.

    zenoh-router <- publisher(+netem) -> edge -> result -> render

The property under test is the one ADR-0009 rests on: the edge publishes back
over the *same* Zenoh session the UE dialled out on, and the renderer's
`e2e_ns` is a full round trip stamped twice on one host.

That reverse direction is the load-bearing assumption of the whole design —
the UE sits behind the UPF's NAT in the lab and never accepts an inbound
connection — so it gets a test rather than an argument. The NAT case itself
can only be confirmed on real radio; this confirms the routing.

Runs with RUN_ID in the environment and no admin service, so it also shows the
downlink working on the standalone path.
"""

import csv
import os
import pathlib
import statistics
import subprocess
import time
import uuid

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
COMPOSE = [
    "docker", "compose",
    "-f", "deploy/compose/logging.yml",
    "-f", "deploy/compose/local.yml",
    "-f", "deploy/compose/render.yml",
]

RATE_HZ = 10.0
DURATION_S = 25
NETEM_DELAY_MS = 20
NUM_POINTS = 10000


def compose(args, env, check=True):
    return subprocess.run(
        COMPOSE + args, cwd=REPO, env=env, check=check,
        capture_output=True, text=True, timeout=900,
    )


def diagnostics(env) -> str:
    parts = ["", "=" * 70, "RENDER LOOP DIAGNOSTICS", "=" * 70]
    ps = compose(["ps", "-a"], env, check=False)
    parts += ["--- ps -a ---", ps.stdout or "(none)"]
    for svc in ("zenoh-router", "lidar-client", "edge", "render"):
        got = compose(["logs", "--tail", "30", svc], env, check=False)
        body = (got.stdout or "").strip() or (got.stderr or "").strip()
        parts += [f"--- logs: {svc} ---", body or "(no output — did it start?)"]
    return "\n".join(parts + ["=" * 70, ""])


DIAG = ""


@pytest.fixture(scope="module")
def render_run():
    global DIAG
    run_id = str(uuid.uuid4())
    env = dict(
        os.environ,
        RUN_ID=run_id,
        NETEM_DELAY=f"{NETEM_DELAY_MS}ms",
        NETEM_JITTER="2ms",
        NETEM_LOSS="0%",
        RATE_HZ=str(RATE_HZ),
        NUM_POINTS=str(NUM_POINTS),
        SEED="42",
        # `null` measures the whole round trip and draws nothing, which is
        # exactly what CI wants: no GPU, no viewer, same stamps.
        RENDER_SINK="null",
    )
    build = subprocess.run(
        ["docker", "build", "-f", "deploy/docker/ros.Dockerfile",
         "-t", "mec-cast-ros", "."],
        cwd=REPO, capture_output=True, text=True, timeout=1800,
    )
    if build.returncode != 0:
        pytest.fail("building mec-cast-ros failed:\n" + build.stderr[-3000:])

    up = compose(["up", "-d", "--build"], env, check=False)
    if up.returncode != 0:
        pytest.fail("compose up failed:\n" + (up.stderr or "")[-3000:])
    try:
        time.sleep(DURATION_S)
        compose(["stop", "-t", "15", "lidar-client", "edge", "render"], env)
        DIAG = diagnostics(env)
        yield run_id
    finally:
        compose(["down", "-v", "--remove-orphans"], env, check=False)


def rows(run_id: str, site: str) -> list[dict]:
    path = REPO / "runs" / run_id / site / "samples.csv"
    assert path.exists(), f"{site} CSV missing: {path}\n{DIAG}"
    with path.open() as f:
        return list(csv.DictReader(f))


def test_the_edge_result_reaches_a_renderer_on_the_other_side(render_run):
    """Reverse routing works: the edge publishes back and the UE receives."""
    edge, render = rows(render_run, "edge-0"), rows(render_run, "render-0")
    assert render, f"renderer recorded nothing — the downlink never arrived\n{DIAG}"

    # Not a trickle: the renderer must see essentially everything the edge
    # processed. A handful of frames in flight at shutdown is expected.
    assert len(render) >= 0.9 * len(edge), (
        f"renderer got {len(render)} of the edge's {len(edge)} frames\n{DIAG}"
    )
    # Site 2 throughout, so the CSVs stay joinable by (run_id, seq, site).
    assert {r["site"] for r in render} == {"2"}


def test_the_round_trip_is_measured_and_exceeds_the_one_way(render_run):
    edge = {int(r["seq"]): r for r in rows(render_run, "edge-0")}
    render = {int(r["seq"]): r for r in rows(render_run, "render-0")}
    common = sorted(set(edge) & set(render))
    assert common, f"no seq joins between edge and render\n{DIAG}"

    trips = [int(render[s]["e2e_ns"]) for s in common if render[s]["e2e_ns"]]
    assert trips, f"no round-trip values recorded\n{DIAG}"
    trip_p50 = statistics.median(trips) / 1e6

    one_way = [int(edge[s]["e2e_ns"]) for s in common if edge[s]["e2e_ns"]]
    one_way_p50 = statistics.median(one_way) / 1e6

    # A round trip is the one-way plus a downlink, so it cannot be shorter.
    assert trip_p50 >= one_way_p50, (
        f"round trip {trip_p50:.1f} ms < one-way {one_way_p50:.1f} ms — a stamp "
        f"is wrong, most likely capture_ns not carried through\n{DIAG}"
    )
    # And it must contain the injected uplink delay.
    assert trip_p50 >= NETEM_DELAY_MS, (
        f"round trip {trip_p50:.1f} ms does not contain the {NETEM_DELAY_MS} ms "
        f"uplink impairment\n{DIAG}"
    )


def test_every_stamp_is_consistent_frame_by_frame(render_run):
    """round_trip - (edge_e2e + downlink + draw) must be a small positive gap.

    That residual is the edge's record()-to-publish() interval and is the only
    segment no stamp covers. A negative value means the renderer's capture_ns
    is not the publisher's; a large one means work crept in between.
    """
    edge = {int(r["seq"]): r for r in rows(render_run, "edge-0")}
    render = {int(r["seq"]): r for r in rows(render_run, "render-0")}

    residuals = []
    for s in sorted(set(edge) & set(render)):
        e, r = edge[s], render[s]
        if not (e["e2e_ns"] and r["e2e_ns"] and r["network_ns"]):
            continue
        residuals.append(
            int(r["e2e_ns"])
            - (int(e["e2e_ns"]) + int(r["network_ns"]) + int(r["processing_ns"] or 0))
        )
    assert residuals, f"nothing to reconcile\n{DIAG}"
    p50_ms = statistics.median(residuals) / 1e6
    assert 0 <= p50_ms < 25, (
        f"unaccounted {p50_ms:.2f} ms between the edge's record() and its "
        f"publish() — the stamps do not reconcile\n{DIAG}"
    )


def test_the_downlink_is_smaller_than_the_uplink(render_run):
    """The point of sending a *result* back rather than echoing the cloud."""
    pub = {int(r["seq"]): r for r in rows(render_run, "pub-0")}
    render = {int(r["seq"]): r for r in rows(render_run, "render-0")}
    common = sorted(set(pub) & set(render))
    up = statistics.mean(int(pub[s]["payload_bytes"]) for s in common)
    down = statistics.mean(int(render[s]["payload_bytes"]) for s in common)
    assert down < up, (
        f"downlink {down:.0f} B is not smaller than uplink {up:.0f} B — the "
        f"edge is echoing rather than sending a downsampled result\n{DIAG}"
    )
