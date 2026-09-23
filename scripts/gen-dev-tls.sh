#!/bin/bash
# Development TLS material for the quic/ link. QUIC mandates TLS 1.3, so there
# is no unencrypted quic/ endpoint -- see ADR-0006.
#
# NOT for the lab: this is a local CA with a long-lived key, generated into a
# gitignored directory so no private key is ever committed.
set -euo pipefail
OUT="$(cd "$(dirname "$0")/.." && pwd)/deploy/docker/zenoh/tls"
mkdir -p "$OUT"
cd "$OUT"

if [ -f router.crt ] && [ -f ca.crt ]; then
  echo "  certs already present in $OUT"
  exit 0
fi

openssl req -x509 -newkey rsa:2048 -nodes -keyout ca.key -out ca.crt -days 3650 \
  -subj "/CN=mec-cast-dev-ca" \
  -addext "basicConstraints=critical,CA:TRUE" 2>/dev/null

openssl req -newkey rsa:2048 -nodes -keyout router.key -out router.csr \
  -subj "/CN=zenoh-router" 2>/dev/null

# basicConstraints CA:FALSE is mandatory. A self-signed cert that is also a CA
# is rejected by zenoh's TLS stack as CaUsedAsEndEntity -- recorded in ADR-0006
# and the single most expensive trap here.
#
# The SAN must cover every name a client dials: the compose service name
# locally, and loopback for a node sharing a host with its router.
openssl x509 -req -in router.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
  -out router.crt -days 825 \
  -extfile <(printf 'basicConstraints=critical,CA:FALSE\nsubjectAltName=DNS:zenoh-router,DNS:localhost,IP:127.0.0.1\n') 2>/dev/null

rm -f router.csr ca.srl
chmod 644 ca.crt router.crt
chmod 600 ca.key router.key
echo "  wrote ca.crt router.crt router.key in $OUT"
openssl x509 -in router.crt -noout -ext basicConstraints,subjectAltName | sed 's/^/    /'
