"""launch_testing: publisher -> edge over the active RMW inside one container.

Validates node logic and telemetry stamping (message flow, seq continuity,
positive network delta on one host). The Zenoh-router topology and network
impairment are exercised separately by the host-side compose e2e tests.

Run inside the ros image:
    colcon test --packages-select mec_cast_edge && colcon test-result --verbose
"""

import os
import re
import unittest

import launch
import launch_ros.actions
import launch_testing
import launch_testing.actions
import pytest


@pytest.mark.launch_test
def generate_test_description():
    env = dict(os.environ, RUN_ID="launch-test-run", RUNS_DIR="/tmp/mec-cast-launch-test")
    publisher = launch_ros.actions.Node(
        package="mec_cast_lidar_client",
        executable="lidar_client",
        parameters=[{"seed": 42, "num_points": 1000, "rate_hz": 5.0}],
        env=env,
        output="screen",
    )
    edge = launch_ros.actions.Node(
        package="mec_cast_edge",
        executable="edge",
        env=env,
        output="screen",
    )
    return (
        launch.LaunchDescription(
            [
                edge,
                # Give the edge a moment to create its subscription first.
                launch.actions.TimerAction(period=2.0, actions=[publisher]),
                launch_testing.actions.ReadyToTest(),
            ]
        ),
        {"edge": edge, "publisher": publisher},
    )


class TestPipeline(unittest.TestCase):
    def test_edge_processes_frames(self, proc_output, edge):
        # Wait until the edge has processed at least frames 0..9.
        for seq in range(10):
            proc_output.assertWaitFor(
                f"processed seq={seq} ", process=edge, timeout=30
            )

    def test_network_delta_positive(self, proc_output, edge):
        # Same-host: recv_ns - send_ns must be non-negative (shared clock).
        # A digit (not '-') right after '=' means the delta is positive.
        proc_output.assertWaitFor(
            re.compile(r"network_ns=[0-9]"), process=edge, timeout=30
        )
