#!/bin/bash
# Run the in-container ROS2 test tier (launch_testing) in the mec-cast-ros
# image, once per middleware (ADR-0013). Usage: make test-ros2
# (or: ./deploy/docker/run-ros-tests.sh [dds|zenoh] ...)
#
# One container, no star centre to dial: Fast DDS discovers its peers on
# localhost; Zenoh gets a router started beside the tests, as the edge runs
# one. Either way the tests reach the nodes through rclpy alone.
set -e
for mw in "${@:-dds zenoh}"; do
  for one in $mw; do
    echo "==> launch tests on $one"
    docker run --rm -w /ws -e MIDDLEWARE_UNDER_TEST="$one" mec-cast-ros bash -exc '
      if [ "$MIDDLEWARE_UNDER_TEST" = zenoh ]; then
        export RMW_IMPLEMENTATION=rmw_zenoh_cpp
        ros2 run rmw_zenoh_cpp rmw_zenohd >/tmp/zenohd.log 2>&1 &
        sleep 2
      else
        export RMW_IMPLEMENTATION=rmw_fastrtps_cpp
      fi
      colcon test --packages-select mec_cast_edge mec_cast_render --event-handlers console_cohesion+ || true
      colcon test-result --verbose
    '
  done
done
