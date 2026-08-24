# syntax=docker/dockerfile:1
# mec-cast ROS2 image: telemetry wheel (stage 1) + ROS2 Jazzy + rmw_zenoh +
# colcon-built mec_cast packages (stage 2). One image serves the zenoh
# router, the point-cloud publisher, and the edge node.
#
# Build from the repo root:
#   docker build -f deploy/docker/ros.Dockerfile -t mec-cast-ros .
#
# VCS_REF/VERSION are stamped as OCI labels so a running container can be
# traced back to a commit without a checkout beside it. scripts/version.sh
# compares the label against the local HEAD and warns when a host is running
# an image built from different source — the failure that silently detaches
# a measurement campaign from the code that produced it.
#   --build-arg VCS_REF=$(git rev-parse HEAD)

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

ARG VCS_REF=unknown
ARG VERSION=unknown
LABEL org.opencontainers.image.title="mec-cast-ros" \
      org.opencontainers.image.description="ROS2 + rmw_zenoh + mec_cast packages" \
      org.opencontainers.image.source="https://github.com/morosev/mec-cast" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.version="${VERSION}"

# ARG is build-time only, so it is gone by the time the node runs. The nodes
# report their commit to the admin control plane from $VCS_REF at runtime
# (mec_cast_admin_client), and the admin's WF_VERSION_SKEW finding is guarded
# on a non-empty value — without this promotion the check silently never fires,
# which is exactly the "one host is on a different commit" case it exists for.
ENV VCS_REF=${VCS_REF} \
    VERSION=${VERSION}

RUN apt-get update && apt-get install -y --no-install-recommends \
        ros-jazzy-rmw-zenoh-cpp \
        python3-colcon-common-extensions \
        python3-pip \
        python3-numpy \
        python3-pytest \
        iproute2 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=wheel /wheels /wheels
# websockets carries the admin control-plane client. The apt package is
# 10.4 and websockets.sync.client landed in 12, so it comes from pip.
# Build-time network only: nothing is fetched when the container runs.
#
# rerun-sdk is the render node's viewer (ADR-0009). Only mec_cast_render
# imports it, and only when sink=rerun — the default sink is `null`, so every
# other node in this image ignores it. `--ignore-installed psutil` is required
# because rerun depends on psutil and apt already placed one here without a
# RECORD file, which pip cannot uninstall.
ARG WITH_RERUN=1
RUN pip install --no-cache-dir --break-system-packages /wheels/*.whl "websockets>=12" \
    && if [ "$WITH_RERUN" = "1" ]; then \
         pip install --no-cache-dir --break-system-packages --ignore-installed psutil "rerun-sdk>=0.36,<0.37"; \
       fi

WORKDIR /ws
COPY ros2/src ./src
RUN . /opt/ros/jazzy/setup.sh && colcon build

COPY deploy/docker/ros-entrypoint.sh /ros-entrypoint.sh
COPY deploy/docker/zenoh /zenoh
RUN chmod +x /ros-entrypoint.sh
ENTRYPOINT ["/ros-entrypoint.sh"]
CMD ["bash"]
