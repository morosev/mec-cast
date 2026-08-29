"""The UE agent: N lidar + M render instances in one ROS2 process.

One robot, one process, however many sensors and displays it carries. Each
instance is a full node — its own name, its own AdminClient connection with a
distinct node_id (`client-<host>-<i>`), its own Recorder writing
`runs/<RUN_ID>/pub-<i>/` — hosted on one SingleThreadedExecutor.

Why SingleThreadedExecutor and not MultiThreaded: every node in this repo is
written to the contract in mec_cast_admin_client/client.py — the admin socket
thread never touches rclpy, and all callbacks run on the executor thread, so
`start_run`/`stop_run` mutate recorder/sub/timer with no reentrancy to reason
about. A MultiThreadedExecutor would silently break that.

Fully command-line drivable, no admin and no environment required:

    ros2 run mec_cast_ue ue_agent --ros-args \
      -p run_id:=dev-001 -p lidar_count:=2 -p render_count:=1 \
      -p num_points:=3000 -p rate_hz:=10.0 -p pattern:=sphere

Global `-p` arguments reach every instance (each node declares its own
parameters and picks the value up), while per-instance identity is pinned via
parameter_overrides, which rclpy applies with higher precedence than global
arguments — so `-p admin_instance:=…` cannot accidentally collapse the
instances into one identity.

Topics are shared, not namespaced: all lidar instances publish
`mec_cast/cloud` and all render instances subscribe `mec_cast/result`,
because the edge knows nothing about instances. Consequence: with several
lidars the edge sees an interleaved seq stream (its seq_gaps counter
overcounts); per-instance truth lives in each instance's own CSV.

Renderers with `sink:=rerun` get staggered ports: instance j serves
web_port+2j / grpc_port+2j, so two viewers on one UE do not collide.
"""

from __future__ import annotations

import os
import signal

import rclpy
from rclpy.executors import ExternalShutdownException, SingleThreadedExecutor
from rclpy.node import Node
from rclpy.parameter import Parameter

from mec_cast_lidar_client.publisher_node import PointCloudPublisher
from mec_cast_render.render_node import RenderNode


class UeAgentConfig(Node):
    """A tiny node whose only job is to receive the agent's own parameters.

    Instance counts cannot live on the instances themselves (chicken and
    egg), so they land here. Environment variables are defaults, as
    everywhere else: LIDAR_INSTANCES / RENDER_INSTANCES.
    """

    def __init__(self) -> None:
        super().__init__("mec_cast_ue_agent")
        self.declare_parameter(
            "lidar_count", int(os.environ.get("LIDAR_INSTANCES", "1"))
        )
        self.declare_parameter(
            "render_count", int(os.environ.get("RENDER_INSTANCES", "0"))
        )
        # Base viewer ports; render instance j serves base+2j so several
        # viewers on one UE do not collide. Declared here rather than pinned
        # in the agent so RENDER_PORT / `-p web_port` keep working.
        self.declare_parameter("web_port", int(os.environ.get("RENDER_PORT", "9876")))
        self.declare_parameter(
            "grpc_port", int(os.environ.get("RENDER_GRPC_PORT", "9877"))
        )

    @property
    def lidar_count(self) -> int:
        return max(0, int(self.get_parameter("lidar_count").value))

    @property
    def render_count(self) -> int:
        return max(0, int(self.get_parameter("render_count").value))

    @property
    def web_port(self) -> int:
        return int(self.get_parameter("web_port").value)

    @property
    def grpc_port(self) -> int:
        return int(self.get_parameter("grpc_port").value)


def main(args=None) -> None:
    rclpy.init(args=args)
    # docker stop sends SIGTERM: leave spin cleanly so every recorder flushes.
    signal.signal(signal.SIGTERM, lambda *_: rclpy.shutdown())

    config = UeAgentConfig()
    nodes: list = []
    try:
        for i in range(config.lidar_count):
            nodes.append(
                PointCloudPublisher(
                    node_name=f"mec_cast_lidar_client_{i}",
                    parameter_overrides=[
                        Parameter("admin_instance", value=i),
                    ],
                )
            )
        for j in range(config.render_count):
            nodes.append(
                RenderNode(
                    node_name=f"mec_cast_render_{j}",
                    parameter_overrides=[
                        Parameter("admin_instance", value=j),
                        Parameter("web_port", value=config.web_port + 2 * j),
                        Parameter("grpc_port", value=config.grpc_port + 2 * j),
                    ],
                )
            )
        if not nodes:
            config.get_logger().error(
                "nothing to run: lidar_count and render_count are both 0"
            )
            return

        config.get_logger().info(
            f"ue agent up: {config.lidar_count} lidar + "
            f"{config.render_count} render instance(s) in one process"
        )

        executor = SingleThreadedExecutor()
        executor.add_node(config)
        for node in nodes:
            executor.add_node(node)
        try:
            executor.spin()
        except (KeyboardInterrupt, ExternalShutdownException):
            pass
    finally:
        # Drain every instance; a failure in one must not skip the rest.
        for node in nodes:
            try:
                node.finish()
            except Exception as exc:  # noqa: BLE001 — shutdown must complete
                config.get_logger().error(f"{node.get_name()} finish failed: {exc}")
            node.destroy_node()
        config.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
