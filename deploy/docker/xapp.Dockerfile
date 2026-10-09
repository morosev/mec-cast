# syntax=docker/dockerfile:1
# The mec-cast E2 xApp, for LOCAL testing against gnb-sim (E2_ADAPTER=sim).
#
# In the lab the xApp does not run in this image: the O-RAN SC RIC's routing
# table sends indications to its own xApp runner container, so ric.sh runs
# the same package there (E2_ADAPTER=osc). This image exists so everything
# above the adapter is exercised locally on the SAME Python as that runner —
# 3.8 — and a 3.9+ construct fails here, not in the lab.
#
#   docker build -f deploy/docker/xapp.Dockerfile -t mec-cast-xapp .

FROM python:3.8-slim

ARG VCS_REF=unknown
ARG VERSION=unknown
LABEL org.opencontainers.image.title="mec-cast-xapp" \
      org.opencontainers.image.description="mec-cast E2 xApp (KPM + RC), sim adapter image" \
      org.opencontainers.image.source="https://github.com/morosev/mec-cast" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.version="${VERSION}"
ENV VCS_REF=${VCS_REF} VERSION=${VERSION} PYTHONUNBUFFERED=1

WORKDIR /opt/mec-cast
COPY ran/py ./ran/py
COPY ran/xapp ./ran/xapp
COPY ros2/src/mec_cast_admin_client/mec_cast_admin_client ./admin_client/mec_cast_admin_client
RUN pip install --no-cache-dir ./ran/py ./ran/xapp
# The admin client is a ROS (ament_python) package; only its ROS-free modules
# are used, straight off the path.
ENV PYTHONPATH=/opt/mec-cast/admin_client

ENTRYPOINT ["python", "-m", "mec_cast_xapp"]
