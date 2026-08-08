#!/bin/bash
# Run the in-container ROS2 test tier (launch_testing) in the mec-cast-ros
# image. Usage: make test-ros2   (or: ./deploy/docker/run-ros-tests.sh)
set -e
docker run --rm -w /ws mec-cast-ros bash -exc '
  colcon test --packages-select mec_cast_edge --event-handlers console_cohesion+ || true
  colcon test-result --verbose
'
