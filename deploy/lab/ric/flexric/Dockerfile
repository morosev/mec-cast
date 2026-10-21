# syntax=docker/dockerfile:1
# FlexRIC (OpenAirInterface) as a near-RT RIC for mec-cast, beside oran-sc-ric.
#
#   docker build -f deploy/lab/ric/flexric/Dockerfile -t mec-cast-flexric .
#   (deploy/lab/ric/flexric.sh does this, and runs it)
#
# One image, three uses, all with host networking:
#   nearRT-RIC                 the RIC: E2 (SCTP 36421) from the gNB, E42 (36422) for xApps
#   python3 -m mec_cast_xapp   the mec-cast xApp, E2_ADAPTER=flexric, which runs
#                              mec-cast-e2-bridge (ran/xapp/flexric) as its child
#   emu_agent_gnb, xApp tools  FlexRIC's emulated gNB and stock xApps, for tests
#
# FlexRIC is cloned at a pinned tag and built here, never vendored: it is under
# the OAI CSSL v1.0 (copyright Apache-style; patents royalty-free for study,
# testing and research). The bridge only links to it.
#
# E2AP v3 and KPM v3.00: srsRAN Project / OCUDU implement E2AP v03.00, and
# KPM_V3_00 is what OCUDU's own FlexRIC tutorial builds. Both must match the
# gNB; override with --build-arg.

ARG BASE_IMAGE=ubuntu:jammy

FROM ${BASE_IMAGE} AS build
ARG FLEXRIC_REPO=https://gitlab.eurecom.fr/mosaic5g/flexric.git
ARG FLEXRIC_REF=v3.0.0
ARG FLEXRIC_SHA=18c3325091ad6890a706ebb2bba38d3c0fb741e5
ARG E2AP_VERSION=E2AP_V3
ARG KPM_VERSION=KPM_V3_00
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential gcc-12 g++-12 cmake ninja-build git ca-certificates \
      libsctp-dev libpcre2-dev bison flex python3 autoconf automake libtool m4 \
    && rm -rf /var/lib/apt/lists/*
# FlexRIC generates part of its ASN.1 code (the F1AP messages its xApp
# examples use) at build time, with this asn1c fork at this commit -- the one
# FlexRIC's own docker/Dockerfile.flexric.ubuntu uses.
ARG ASN1C_SHA=940dd5fa9f3917913fd487b13dfddfacd0ded06e
RUN git clone https://github.com/mouse07410/asn1c /tmp/asn1c \
    && git -C /tmp/asn1c checkout --quiet ${ASN1C_SHA} \
    && cd /tmp/asn1c && autoreconf -i >/dev/null && ./configure --prefix=/opt/asn1c >/dev/null \
    && make -j"$(nproc)" >/dev/null && make install >/dev/null && rm -rf /tmp/asn1c
RUN git clone --depth 1 --branch "${FLEXRIC_REF}" "${FLEXRIC_REPO}" /flexric \
    && test "$(git -C /flexric rev-parse HEAD)" = "${FLEXRIC_SHA}" \
       || { echo "FlexRIC ${FLEXRIC_REF} is not ${FLEXRIC_SHA}: the tag moved" >&2; exit 1; }
# The bridge, as one more target of FlexRIC's build: same flags, same E2AP/KPM
# selection, and the static e42_xapp with its ASN.1 dependencies. The one
# added line is in the build container's copy only.
COPY ran/xapp/flexric /src/bridge
RUN echo 'add_subdirectory(/src/bridge ${CMAKE_BINARY_DIR}/mec_cast_bridge)' >> /flexric/CMakeLists.txt \
    && cmake -S /flexric -B /flexric/build -GNinja -DCMAKE_BUILD_TYPE=Release \
      -DE2AP_VERSION=${E2AP_VERSION} -DKPM_VERSION=${KPM_VERSION} \
      -DCMAKE_C_COMPILER=gcc-12 -DCMAKE_CXX_COMPILER=g++-12 \
      -DNEAR_RIC_INSTALL=TRUE -DEMU_AGENT_INSTALL=TRUE -DASN1C_EXEC_PATH=/opt/asn1c/bin/asn1c \
    && ninja -C /flexric/build \
    && ninja -C /flexric/build install \
    && install -m 755 /flexric/build/mec_cast_bridge/mec-cast-e2-bridge /usr/local/bin/

FROM ${BASE_IMAGE}
ARG FLEXRIC_REF=v3.0.0
ARG FLEXRIC_SHA=18c3325091ad6890a706ebb2bba38d3c0fb741e5
ARG E2AP_VERSION=E2AP_V3
ARG KPM_VERSION=KPM_V3_00
LABEL org.opencontainers.image.title="mec-cast-flexric" \
      org.opencontainers.image.description="FlexRIC near-RT RIC + mec-cast-e2-bridge for the mec-cast xApp" \
      org.opencontainers.image.source="https://github.com/morosev/mec-cast" \
      mec-cast.flexric.ref="${FLEXRIC_REF}" mec-cast.flexric.sha="${FLEXRIC_SHA}" \
      mec-cast.flexric.e2ap="${E2AP_VERSION}" mec-cast.flexric.kpm="${KPM_VERSION}"
ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends \
      libsctp1 python3 python3-pip procps psmisc \
    && rm -rf /var/lib/apt/lists/* \
    && pip3 install --no-cache-dir "websockets>=12,<14"
COPY --from=build /usr/local/lib/flexric /usr/local/lib/flexric
COPY --from=build /usr/local/etc/flexric /usr/local/etc/flexric
COPY --from=build /usr/local/bin/nearRT-RIC /usr/local/bin/emu_agent_gnb /usr/local/bin/
COPY --from=build /flexric/build/examples/xApp/c /usr/local/flexric/xApp
COPY --from=build /flexric/build/28_552_kpm_meas.txt /flexric/build/28_552_kpm_meas.txt
COPY --from=build /usr/local/bin/mec-cast-e2-bridge /usr/local/bin/

# The xApp's code is mounted at run time (flexric.sh), as oran-sc-ric.sh mounts
# it into that RIC's runner: a code change needs no image rebuild.
ENV PYTHONPATH=/opt/mec-cast/ran/py/src:/opt/mec-cast/ran/xapp/src:/opt/mec-cast/admin_client \
    E2_ADAPTER=flexric
CMD ["stdbuf", "-oL", "nearRT-RIC"]
