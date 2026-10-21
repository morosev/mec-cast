#!/bin/bash
# The edge's star centre, for whichever middleware the deployment chose
# (ADR-0013): the Zenoh router, or the Fast DDS Discovery Server.
#
#   middleware-server.sh zenoh   # rmw_zenohd: tcp/7448, plus quic/7449 when MIDDLEWARE_LINK=quic
#   middleware-server.sh dds     # fast-discovery-server 0 on 11811, udp or tcp per MIDDLEWARE_LINK
#
# Both read MIDDLEWARE_LINK, so the centre listens on the link its clients dial.
set -euo pipefail

case "${1:-}" in
  zenoh)
    if [ "${MIDDLEWARE_LINK:-tcp}" = quic ]; then
      # Needs the TLS material in /zenoh/tls (scripts/gen-dev-tls.sh): a
      # listener whose certificates are missing is fatal to the whole router.
      export ZENOH_CONFIG_OVERRIDE='listen/endpoints=["tcp/[::]:7448","quic/[::]:7449"]'
    fi
    exec ros2 run rmw_zenoh_cpp rmw_zenohd
    ;;
  dds)
    if [ "${MIDDLEWARE_LINK:-udp}" = tcp ]; then
      exec fast-discovery-server -i 0 -t 0.0.0.0 -q 11811
    fi
    exec fast-discovery-server -i 0 -l 0.0.0.0 -p 11811
    ;;
  *)
    echo "usage: middleware-server.sh zenoh|dds" >&2
    exit 64
    ;;
esac
