"""launch_testing: the G-PCC downlink, lidar -> edge -> renderer (ADR-0014).

The edge codes its voxel result with codec=gpcc on `mec_cast/result_gpcc`;
the renderer decodes it and draws (sink null). Asserted per frame: the
renderer drew exactly as many points as the edge found voxels — the coding is
lossless end to end — and the round trip is positive.

Isolated as test_pipeline_launch.py is (a domain and a namespace of its own),
and for the same reasons; see there. The downlink is best-effort, so the
renderer may miss frames: assertions are on the frames it did draw.

Run inside the ros image:
    colcon test --packages-select mec_cast_render && colcon test-result --verbose
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

FRAMES = 10
RATE_HZ = 5.0
TIMEOUT_S = 30.0

EDGE_RE = re.compile(r"processed seq=(\d+) n=\d+ voxels=(\d+)")
RENDER_RE = re.compile(r"rendered seq=(\d+) n=(\d+) round_trip_ns=(-?\d+)")

RUN_ID = str(uuid.uuid4())
NAMESPACE = f"launch_test_{uuid.uuid4().hex[:12]}"


def isolated_env() -> dict:
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
        parameters=[{"seed": 42, "num_points": 3000, "rate_hz": RATE_HZ,
                     "pattern": "lidar_scan"}],
        env=env,
        output="screen",
    )
    edge = launch_ros.actions.Node(
        package="mec_cast_edge",
        executable="edge",
        namespace=NAMESPACE,
        parameters=[{"publish_result": True, "codec": "gpcc"}],
        env=env,
        output="screen",
    )
    render = launch_ros.actions.Node(
        package="mec_cast_render",
        executable="render",
        namespace=NAMESPACE,
        parameters=[{"sink": "null", "codec": "gpcc"}],
        env=env,
        output="screen",
    )

    # The publisher starts once both receivers have said they subscribed.
    ready: set[str] = set()
    started = []

    def on_output(name):
        def handler(event):
            text = event.text.decode(errors="replace")
            if f"recording run {RUN_ID}" in text:
                ready.add(name)
            if started or ready != {"edge", "render"}:
                return None
            started.append(True)
            return publisher
        return handler

    handlers = [
        launch.actions.RegisterEventHandler(
            launch.event_handlers.OnProcessIO(
                target_action=proc, on_stdout=on_output(name), on_stderr=on_output(name)
            )
        )
        for name, proc in (("edge", edge), ("render", render))
    ]
    return (
        launch.LaunchDescription([edge, render, *handlers, launch_testing.actions.ReadyToTest()]),
        {"edge": edge, "render": render},
    )


def logged(proc_output, proc, pattern) -> list[tuple[int, ...]]:
    try:
        chunks = proc_output[proc]
    except KeyError:
        return []
    text = "".join(chunk.text.decode(errors="replace") for chunk in chunks)
    return [tuple(int(g) for g in m.groups()) for m in pattern.finditer(text)]


def wait_for_renders(proc_output, render, count: int) -> list[tuple[int, ...]]:
    deadline = time.monotonic() + TIMEOUT_S
    while True:
        frames = logged(proc_output, render, RENDER_RE)
        if len(frames) >= count or time.monotonic() >= deadline:
            return frames
        time.sleep(0.2)


class TestGpccDownlink(unittest.TestCase):
    def test_both_ends_run_the_codec(self, proc_output, edge, render):
        proc_output.assertWaitFor(
            "(result -> mec_cast/result_gpcc)", process=edge, timeout=TIMEOUT_S
        )
        proc_output.assertWaitFor("codec=gpcc", process=render, timeout=TIMEOUT_S)

    def test_the_renderer_draws_every_voxel_the_edge_found(self, proc_output, edge, render):
        renders = wait_for_renders(proc_output, render, FRAMES)
        self.assertGreaterEqual(len(renders), FRAMES, f"rendered: {renders}")
        voxels = dict(logged(proc_output, edge, EDGE_RE))
        for seq, n, round_trip_ns in renders:
            self.assertIn(seq, voxels, f"seq {seq} rendered but not logged by the edge")
            self.assertGreater(n, 0, f"seq {seq} drew nothing")
            self.assertEqual(n, voxels[seq], f"seq {seq}: drew {n} of {voxels[seq]} voxels")
            self.assertGreater(round_trip_ns, 0, f"seq {seq}: frame not drawn")
