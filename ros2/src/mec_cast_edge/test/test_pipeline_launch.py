"""launch_testing: publisher -> edge over the active RMW inside one container.

Validates node logic and telemetry stamping (message flow, seq continuity,
positive network delta on one host). The Zenoh-router topology and network
impairment are exercised separately by the host-side compose e2e tests.

This test must pass on a host that is already running a full compose
topology — a router, a lidar client, an edge and a renderer, all on the
default domain. Two things make that true, and both are structural rather
than a longer timeout:

  * **Isolation** (`isolated_env`, `NAMESPACE`). A per-invocation
    ROS_DOMAIN_ID, discovery confined to this container, and a per-invocation
    node/topic namespace. A foreign publisher cannot reach our edge, and a
    foreign subscriber cannot see our publisher.
  * **No start-up race**. The publisher starts when the edge *says* it has
    subscribed, not after a fixed sleep, and the assertions are relative to
    the first frame observed rather than pinned to seq 0. That matters
    because the QoS here is RELIABLE with VOLATILE durability: a sample
    published before the subscription is matched is gone for good, so a
    publisher that wins the race makes `seq=0` unobservable *forever* — no
    timeout, however generous, recovers it.

Run inside the ros image:
    colcon test --packages-select mec_cast_edge && colcon test-result --verbose
"""

import os
import random
import re
import time
import unittest
import uuid

import launch
import launch.event_handlers
import launch_ros.actions
import launch_testing
import launch_testing.actions
import pytest

#: Frames the edge must process before the assertions are satisfied. At
#: RATE_HZ this is a couple of seconds of streaming.
FRAMES = 10
RATE_HZ = 5.0
#: Generous, and never approached on a healthy run (~5 s end to end). It
#: bounds a hang; it is not how the race above is avoided.
TIMEOUT_S = 30.0

#: The edge's per-frame progress line. Both numbers are asserted on.
FRAME_RE = re.compile(r"processed seq=(\d+) .*?network_ns=(-?\d+)")

#: One run id per invocation, so the edge's own log tells us it is recording
#: *this* run and not a leftover from something else in the image.
RUN_ID = str(uuid.uuid4())
#: Nodes and topics live under here. `mec_cast/cloud` is a relative name, so
#: it resolves to /<NAMESPACE>/mec_cast/cloud for both processes — a name
#: nothing outside this test is publishing to.
NAMESPACE = f"launch_test_{uuid.uuid4().hex[:12]}"


def isolated_env() -> dict:
    """The launched processes' environment, walled off from the host's graph.

    ROS_DOMAIN_ID is honoured by every RMW — DDS puts it in the port
    arithmetic, rmw_zenoh puts it in the key expressions — so a domain of our
    own is the one wall that holds whatever RMW is active. It is drawn at
    random from the range that is safe on Linux with the default ephemeral
    port range (0-101), skipping 0 because that is what everything else on
    the host is using. Two concurrent invocations can draw the same number;
    NAMESPACE is what keeps that harmless.

    ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST keeps DDS discovery inside this
    container even on a shared domain. It is a no-op under rmw_zenoh, which
    is why it is the second wall and not the only one.
    """
    return dict(
        os.environ,
        RUN_ID=RUN_ID,
        RUNS_DIR="/tmp/mec-cast-launch-test",
        ROS_DOMAIN_ID=str(random.SystemRandom().randint(1, 101)),
        ROS_AUTOMATIC_DISCOVERY_RANGE="LOCALHOST",
        ROS_STATIC_PEERS="",
    )


@pytest.mark.launch_test
def generate_test_description():
    env = isolated_env()
    publisher = launch_ros.actions.Node(
        package="mec_cast_lidar_client",
        executable="lidar_client",
        namespace=NAMESPACE,
        parameters=[{"seed": 42, "num_points": 1000, "rate_hz": RATE_HZ}],
        env=env,
        output="screen",
    )
    edge = launch_ros.actions.Node(
        package="mec_cast_edge",
        executable="edge",
        namespace=NAMESPACE,
        env=env,
        output="screen",
    )

    # Start the publisher on the edge's own readiness line rather than on a
    # timer. The edge logs it immediately after create_subscription() returns,
    # so the subscription exists before the publishing process is even forked
    # — which is as early as the launch system can tell us anything.
    started = []

    def on_edge_output(event):
        if started or f"edge recording run {RUN_ID}" not in event.text.decode(
            errors="replace"
        ):
            return None
        started.append(True)
        return publisher

    return (
        launch.LaunchDescription(
            [
                edge,
                launch.actions.RegisterEventHandler(
                    launch.event_handlers.OnProcessIO(
                        target_action=edge,
                        on_stdout=on_edge_output,
                        on_stderr=on_edge_output,
                    )
                ),
                launch_testing.actions.ReadyToTest(),
            ]
        ),
        {"edge": edge, "publisher": publisher},
    )


def edge_frames(proc_output, edge) -> list[tuple[int, int]]:
    """Every frame the edge has logged so far, as (seq, network_ns).

    Scoped to the edge process launched by *this* test, which is the last of
    the three walls: even a foreign frame that somehow reached our edge would
    have to have been logged by our own process to be counted here.

    The IoHandler only knows a process once it has written something, so a
    poll that lands before the edge's first line raises rather than returning
    empty. That is "no frames yet", not a failure.
    """
    try:
        chunks = proc_output[edge]
    except KeyError:
        return []
    text = "".join(chunk.text.decode(errors="replace") for chunk in chunks)
    return [(int(m.group(1)), int(m.group(2))) for m in FRAME_RE.finditer(text)]


def wait_for_frames(proc_output, edge, count: int) -> list[tuple[int, int]]:
    """Poll until the edge has logged `count` frames, or TIMEOUT_S elapses.

    Returns whatever was collected either way; the caller asserts on it, so a
    timeout fails with the frames actually seen instead of a bare timeout.
    """
    deadline = time.monotonic() + TIMEOUT_S
    while True:
        frames = edge_frames(proc_output, edge)
        if len(frames) >= count or time.monotonic() >= deadline:
            return frames
        time.sleep(0.2)


class TestPipeline(unittest.TestCase):
    def test_edge_records_this_run(self, proc_output, edge):
        # Fails fast and specifically when the edge never came up — otherwise
        # that shows up as an empty frame list further down.
        proc_output.assertWaitFor(
            f"edge recording run {RUN_ID}", process=edge, timeout=TIMEOUT_S
        )

    def test_edge_processes_frames(self, proc_output, edge):
        frames = wait_for_frames(proc_output, edge, FRAMES)
        self.assertGreaterEqual(
            len(frames), FRAMES, f"edge processed {len(frames)} frames: {frames}"
        )
        # Seq continuity, relative to the first frame observed. Which seq that
        # is depends on when discovery matched; that it then advances by one
        # per frame does not, and is what this asserts.
        seqs = [seq for seq, _ in frames[:FRAMES]]
        self.assertEqual(
            seqs,
            list(range(seqs[0], seqs[0] + FRAMES)),
            f"seq gap in the edge's frames: {seqs}",
        )

    def test_network_delta_positive(self, proc_output, edge):
        # Same-host: recv_ns - send_ns must be non-negative (shared clock).
        # Every frame, not just the first one that happened to be logged.
        frames = wait_for_frames(proc_output, edge, FRAMES)
        self.assertTrue(frames, "the edge processed no frames at all")
        negative = [(seq, delta) for seq, delta in frames if delta < 0]
        self.assertFalse(negative, f"negative network_ns on (seq, delta): {negative}")
