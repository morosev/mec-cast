"""launch_testing: G-PCC both ways, lidar -> edge -> renderer (ADR-0014).

With codec=gpcc everywhere: the lidar client codes its clouds at 1 mm onto
`mec_cast/cloud_gpcc`, the edge decodes and processes them and codes its
voxel result onto `mec_cast/result_gpcc`, and the renderer decodes and draws
it (sink null). Asserted per frame:

- **uplink:** the edge saw as many points, and found as many voxels, as the
  uncoded cloud gives — computed here from the same generator, which is
  deterministic per frame for `lidar_scan`;
- **downlink:** the renderer drew exactly as many points as the edge found
  voxels, and the round trip is positive.

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
import numpy as np
import pytest

from mec_cast_edge.edge_node import process_cloud
from mec_cast_lidar_client.publisher_node import PointCloudPublisher

FRAMES = 10
RATE_HZ = 5.0
TIMEOUT_S = 30.0

NUM_POINTS = 3000
EDGE_RE = re.compile(r"processed seq=(\d+) n=(\d+) voxels=(\d+)")
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
        parameters=[{"seed": 42, "num_points": NUM_POINTS, "rate_hz": RATE_HZ,
                     "pattern": "lidar_scan", "codec": "gpcc", "codec_precision_m": 0.001}],
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
    # Matched against each process's output so far, not one chunk: launch
    # delivers output in chunks that can split a line anywhere, the run id
    # included, and a missed readiness line means no publisher, ever.
    ready: set[str] = set()
    started = []
    seen = {"edge": "", "render": ""}

    def on_output(name):
        def handler(event):
            seen[name] += event.text.decode(errors="replace")
            if f"recording run {RUN_ID}" in seen[name]:
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


def uncoded(seq: int) -> tuple[int, int]:
    """(points, voxels) of the lidar client's frame `seq`, never coded."""
    gen = PointCloudPublisher.__new__(PointCloudPublisher)
    gen.pattern, gen.num_points, gen.seed, gen.seq = "lidar_scan", NUM_POINTS, 42, seq
    gen.rng = np.random.default_rng(42)
    pts = gen.generate_points()
    return len(pts), len(process_cloud(pts)[1])


def wait_for_renders(proc_output, render, count: int) -> list[tuple[int, ...]]:
    deadline = time.monotonic() + TIMEOUT_S
    while True:
        frames = logged(proc_output, render, RENDER_RE)
        if len(frames) >= count or time.monotonic() >= deadline:
            return frames
        time.sleep(0.2)


class TestGpcc(unittest.TestCase):
    def test_every_node_runs_the_codec(self, proc_output, edge, render):
        proc_output.assertWaitFor(
            "from mec_cast/cloud_gpcc (result -> mec_cast/result_gpcc)",
            process=edge, timeout=TIMEOUT_S,
        )
        proc_output.assertWaitFor("codec=gpcc", process=render, timeout=TIMEOUT_S)

    def test_the_edge_gets_the_uncoded_clouds_voxels(self, proc_output, edge, render):
        wait_for_renders(proc_output, render, FRAMES)
        frames = logged(proc_output, edge, EDGE_RE)
        self.assertGreaterEqual(len(frames), FRAMES, f"edge processed: {frames}")
        for seq, n, voxels in frames:
            self.assertEqual((n, voxels), uncoded(seq), f"seq {seq}: (points, voxels)")

    def test_the_renderer_draws_every_voxel_the_edge_found(self, proc_output, edge, render):
        renders = wait_for_renders(proc_output, render, FRAMES)
        self.assertGreaterEqual(len(renders), FRAMES, f"rendered: {renders}")
        voxels = {seq: v for seq, _, v in logged(proc_output, edge, EDGE_RE)}
        for seq, n, round_trip_ns in renders:
            self.assertIn(seq, voxels, f"seq {seq} rendered but not logged by the edge")
            self.assertGreater(n, 0, f"seq {seq} drew nothing")
            self.assertEqual(n, voxels[seq], f"seq {seq}: drew {n} of {voxels[seq]} voxels")
            self.assertGreater(round_trip_ns, 0, f"seq {seq}: frame not drawn")
