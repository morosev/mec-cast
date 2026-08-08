#!/bin/bash
set -e
# Sourcing ROS / colcon setup scripts can consume the shell's positional
# parameters — save the real command first and clear $@ before sourcing.
args=("$@")
set --
source /opt/ros/jazzy/setup.bash
if [ -f /ws/install/setup.bash ]; then
  source /ws/install/setup.bash
fi
exec "${args[@]}"
