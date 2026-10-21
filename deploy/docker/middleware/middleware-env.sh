# shellcheck shell=bash
# Sourced by /ros-entrypoint.sh before every command in the mec-cast-ros image.
# Turns the deployment's middleware choice into what the RMW needs
# (ADR-0013, docs/architecture/middleware.md):
#
#   MIDDLEWARE        zenoh (default) | dds
#   MIDDLEWARE_LINK   zenoh: tcp (default) | quic      dds: udp (default) | tcp
#   MIDDLEWARE_HOST   where the edge's router / Discovery Server is. Set on the
#                     ROS nodes; unset on the router and discovery services
#                     themselves, which this file then leaves alone.
#   DDS_TCP_LISTEN_PORT   dds/tcp only: the port this participant listens on.
#                     Set on the edge; empty on UE nodes, which dial out.
#   DDS_INTERFACES    dds only: the interfaces this participant may use and
#                     announce, comma-separated addresses or interface names
#                     (e.g. tun_srsue, 10.53.1.1). Fast DDS otherwise announces
#                     EVERY interface, and a peer sends to whichever it can
#                     reach: on a host that is also on the management LAN, the
#                     point clouds would bypass the 5G link. Zenoh has no such
#                     choice to make -- a client dials one address.
#
# It exports RMW_IMPLEMENTATION plus the middleware's own config, and
# MIDDLEWARE_EFFECTIVE / MIDDLEWARE_LINK_EFFECTIVE, which the nodes report to
# the admin (mec_cast_admin_client.node_base). An explicit ZENOH_CONFIG_OVERRIDE
# or FASTDDS_DEFAULT_PROFILES_FILE in the environment wins, for experiments;
# the effective link is then read from it.
#
# An invalid pair stops the container with the allowed values: a node never
# starts on a transport nobody chose.

mec_cast_middleware_fail() {
  echo "mec-cast middleware: $*" >&2
  echo "  MIDDLEWARE=zenoh with MIDDLEWARE_LINK=tcp|quic, or MIDDLEWARE=dds with MIDDLEWARE_LINK=udp|tcp" >&2
  exit 64
}

# An address the RMW can use: DDS locators want an IP, compose gives a name.
mec_cast_resolve() {
  local ip
  ip=$(getent ahostsv4 "$1" 2>/dev/null | awk 'NR==1 {print $1}')
  echo "${ip:-$1}"
}

# Neither set: not a deployed node (a one-off `docker run`, the in-container
# launch tests). Leave the image's own defaults alone.
if [ -z "${MIDDLEWARE:-}" ] && [ -z "${MIDDLEWARE_HOST:-}" ]; then
  return 0
fi

MIDDLEWARE=${MIDDLEWARE:-zenoh}
case "$MIDDLEWARE" in
  zenoh) MIDDLEWARE_LINK=${MIDDLEWARE_LINK:-tcp} ;;
  dds) MIDDLEWARE_LINK=${MIDDLEWARE_LINK:-udp} ;;
  *) mec_cast_middleware_fail "unknown MIDDLEWARE=$MIDDLEWARE" ;;
esac
case "$MIDDLEWARE/$MIDDLEWARE_LINK" in
  zenoh/tcp | zenoh/quic | dds/udp | dds/tcp) ;;
  *) mec_cast_middleware_fail "MIDDLEWARE_LINK=$MIDDLEWARE_LINK is not a link of $MIDDLEWARE" ;;
esac
export MIDDLEWARE MIDDLEWARE_LINK

if [ "$MIDDLEWARE" = zenoh ]; then
  export RMW_IMPLEMENTATION=rmw_zenoh_cpp
  export ZENOH_ROUTER_CHECK_ATTEMPTS=${ZENOH_ROUTER_CHECK_ATTEMPTS:--1}
  if [ -n "${ZENOH_CONFIG_OVERRIDE:-}" ]; then
    case "$ZENOH_CONFIG_OVERRIDE" in
      *'"quic/'*) MIDDLEWARE_LINK_EFFECTIVE=quic ;;
      *'"tcp/'*) MIDDLEWARE_LINK_EFFECTIVE=tcp ;;
      *) MIDDLEWARE_LINK_EFFECTIVE=override ;;
    esac
  elif [ -n "${MIDDLEWARE_HOST:-}" ]; then
    # Client mode: a pure star, every sample relayed by the edge's router.
    if [ "$MIDDLEWARE_LINK" = quic ]; then
      export ZENOH_CONFIG_OVERRIDE="mode=\"client\";connect/endpoints=[\"quic/${MIDDLEWARE_HOST}:7449\"];transport/link/tls/root_ca_certificate=\"/zenoh/tls/ca.crt\""
    else
      export ZENOH_CONFIG_OVERRIDE="mode=\"client\";connect/endpoints=[\"tcp/${MIDDLEWARE_HOST}:7448\"]"
    fi
    MIDDLEWARE_LINK_EFFECTIVE=$MIDDLEWARE_LINK
  fi
else
  export RMW_IMPLEMENTATION=rmw_fastrtps_cpp
  if [ -n "${FASTDDS_DEFAULT_PROFILES_FILE:-}" ]; then
    MIDDLEWARE_LINK_EFFECTIVE=override
  elif [ -n "${MIDDLEWARE_HOST:-}" ]; then
    server=$(mec_cast_resolve "$MIDDLEWARE_HOST")
    profile=/tmp/mec-cast-fastdds.xml
    allow=""
    if [ -n "${DDS_INTERFACES:-}" ]; then
      for itf in ${DDS_INTERFACES//,/ }; do
        if [[ "$itf" =~ ^[0-9.]+$ ]]; then
          allow="$allow<address>$itf</address>"
        else
          allow="$allow<interface>$itf</interface>"
        fi
      done
      allow="<interfaceWhiteList>$allow</interfaceWhiteList>"
    fi
    # Built-in transports OFF, one user transport ON. Fast DDS's default adds
    # shared memory, which two containers sharing a hostname (lidar-client and
    # render are both ue-local) negotiate and then cannot use: samples vanish.
    if [ "$MIDDLEWARE_LINK" = tcp ]; then
      ports=""
      [ -n "${DDS_TCP_LISTEN_PORT:-}" ] && ports="<port>${DDS_TCP_LISTEN_PORT}</port>"
      transport="<transport_descriptor><transport_id>mec_cast</transport_id><type>TCPv4</type>${allow}
        <listening_ports>${ports}</listening_ports>
        <sendBufferSize>4194304</sendBufferSize><receiveBufferSize>4194304</receiveBufferSize>
      </transport_descriptor>"
      server_locator="<tcpv4><address>${server}</address><physical_port>11811</physical_port><port>11811</port></tcpv4>"
    else
      transport="<transport_descriptor><transport_id>mec_cast</transport_id><type>UDPv4</type>${allow}
        <sendBufferSize>4194304</sendBufferSize><receiveBufferSize>4194304</receiveBufferSize>
      </transport_descriptor>"
      server_locator="<udpv4><address>${server}</address><port>11811</port></udpv4>"
    fi
    # Discovery Server 0's GUID prefix (Fast DDS's fixed prefix for server id 0),
    # as ROS_DISCOVERY_SERVER would set it. CLIENT: discovers through the
    # server only, the DDS counterpart of a Zenoh client of the router.
    cat > "$profile" <<XML
<?xml version="1.0" encoding="UTF-8"?>
<dds xmlns="http://www.eprosima.com/XMLSchemas/fastRTPS_Profiles">
  <profiles>
    <transport_descriptors>${transport}</transport_descriptors>
    <participant profile_name="mec_cast" is_default_profile="true">
      <rtps>
        <userTransports><transport_id>mec_cast</transport_id></userTransports>
        <useBuiltinTransports>false</useBuiltinTransports>
        <builtin>
          <discovery_config>
            <discoveryProtocol>${DDS_DISCOVERY_PROTOCOL:-CLIENT}</discoveryProtocol>
            <discoveryServersList>
              <RemoteServer prefix="44.53.00.5f.45.50.52.4f.53.49.4d.41">
                <metatrafficUnicastLocatorList><locator>${server_locator}</locator></metatrafficUnicastLocatorList>
              </RemoteServer>
            </discoveryServersList>
          </discovery_config>
        </builtin>
      </rtps>
    </participant>
  </profiles>
</dds>
XML
    export FASTDDS_DEFAULT_PROFILES_FILE=$profile
    MIDDLEWARE_LINK_EFFECTIVE=$MIDDLEWARE_LINK
    unset server profile ports transport server_locator allow itf
  fi
fi

if [ -n "${MIDDLEWARE_LINK_EFFECTIVE:-}" ]; then
  export MIDDLEWARE_EFFECTIVE=$MIDDLEWARE MIDDLEWARE_LINK_EFFECTIVE
fi
