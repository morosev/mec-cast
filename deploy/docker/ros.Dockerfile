# syntax=docker/dockerfile:1
# mec-cast ROS2 image: telemetry wheel (stage 1) + ROS2 Jazzy + rmw_zenoh +
# colcon-built mec_cast packages (stage 2). One image serves the zenoh
# router, the point-cloud publisher, and the edge node.
#
# Build from the repo root:
#   docker build -f deploy/docker/ros.Dockerfile -t mec-cast-ros .

FROM rust:1-bookworm AS wheel
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/maturin \
    && /opt/maturin/bin/pip install --no-cache-dir maturin
WORKDIR /build
COPY Cargo.toml rust-toolchain.toml ./
COPY telemetry ./telemetry
COPY ran ./ran
# abi3-py310 wheel: one artifact serves the container's 3.12 and any >=3.10.
RUN cd telemetry/python && /opt/maturin/bin/maturin build --release -o /wheels

FROM ros:jazzy-ros-base
RUN apt-get update && apt-get install -y --no-install-recommends \
        ros-jazzy-rmw-zenoh-cpp \
        python3-colcon-common-extensions \
        python3-pip \
        python3-numpy \
        python3-pytest \
        iproute2 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=wheel /wheels /wheels
RUN pip install --no-cache-dir --break-system-packages /wheels/*.whl

WORKDIR /ws
COPY ros2/src ./src
RUN . /opt/ros/jazzy/setup.sh && colcon build

COPY deploy/docker/ros-entrypoint.sh /ros-entrypoint.sh
COPY deploy/docker/zenoh /zenoh
RUN chmod +x /ros-entrypoint.sh
ENTRYPOINT ["/ros-entrypoint.sh"]
CMD ["bash"]
