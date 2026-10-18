# Local 5G testbed with OCUDU (Ubuntu)

A whole 5G network on one Ubuntu machine, with mec-cast running across it:
an **OCUDU** gNB, an **srsUE** software UE and an **Open5GS** core, with
**ZeroMQ** standing in for the radio. The LiDAR client sends from the UE side
of the link and the edge receives on the N6 side, so every frame crosses the
UE stack, the gNB scheduler and the GTP-U tunnel through the UPF.

Use it to develop and integration-test the pipeline against a real RAN stack
without SDR hardware: the Zenoh/LiDAR path, telemetry, the admin, and the RAN
metrics tap. **Do not use its latency or throughput numbers as results** (see
[What this setup is not](#what-this-setup-is-not)).

The netem-only path on one box is [local-development.md](local-development.md);
the real testbed is [operations/lab-topology.md](../operations/lab-topology.md).

- [Topology](#topology)
- [Prerequisites](#prerequisites)
- [Part 1 — the 5G network](#part-1--the-5g-network)
- [Part 2 — mec-cast over the 5G link](#part-2--mec-cast-over-the-5g-link)
- [Troubleshooting](#troubleshooting)
- [What this setup is not](#what-this-setup-is-not)
- [References](#references)

## Topology

```text
 UE namespace (mec5g-lidar-client)      host (root) namespace                         Open5GS container
┌───────────────────────────────┐  ┌──────────────────────────────────────────┐  ┌─────────────────────────┐
│ ue_agent (lidar)              │  │                                          │  │                         │
│   │  10.45.1.2                │  │  srsUE ══ZMQ IQ══ OCUDU gNB              │  │  AMF  10.53.1.2 :38412  │
│   ▼                           │  │   ▲  tcp 2001 UL      │  NGAP / SCTP  ───┼──┼─►                       │
│ tun_srsue ────────────────────┼──┼───┘  tcp 2000 DL      │  GTP-U udp 2152 ─┼──┼─► UPF ── ogstun         │
│ eth0 (compose bridge) ─┐      │  │                                          │  │         10.45.0.0/16    │
└────────────────────────┼──────┘  │  zenoh-router + edge  10.53.1.1 (N6) ◄───┼──┼── eth0 10.53.1.2        │
                         │         │  logging :8000   admin :8099             │  └─────────────────────────┘
                         └─────────┼─► management plane (compose bridge)      │
                                   └──────────────────────────────────────────┘
```

Three Linux network namespaces carry the whole design:

| Namespace | Holds | Why it is separate |
|---|---|---|
| UE (`ue1`) | the LiDAR client and `tun_srsue` | The host owns both 10.45.1.2 and 10.53.1.1. If they shared a namespace, the kernel would deliver UE→edge traffic locally and the RAN would never see it. With `tun_srsue` as the route to the edge, a frame has exactly one way out: into srsUE. |
| host | srsUE, gNB, Zenoh router, edge, collector | srsUE straddles the two: the process stays here (ZMQ on 127.0.0.1), only its TUN interface lives in `ue1`. |
| Open5GS container | AMF, SMF, UPF | Comes from OCUDU's own `docker/`, already configured for OCUDU and srsUE. |

**Uplink:** ue_agent → `tun_srsue` → srsUE (SDAP/PDCP/RLC/MAC/PHY → IQ) →
ZMQ → gNB → GTP-U → UPF → `ogstun` → 10.53.1.1 → Zenoh router → edge.
**Downlink** is the reverse, and needs one host route
(`10.45.0.0/16 via 10.53.1.2`) so replies to the UE go back into the core
instead of out of the default gateway.

The LiDAR client keeps its compose-bridge interface for logging and admin,
like the lab's management LAN. Only the edge address is routed over 5G, and
the client refuses to start until that route goes through `tun_srsue`.

## Prerequisites

| Item | Version used | Notes |
|---|---|---|
| OS | Ubuntu 24.04 LTS, x86_64 or arm64 | Bare metal or a VM. Needs kernel SCTP, network namespaces and TUN. |
| CPU / RAM | 8 cores, 16 GB | ZMQ runs the radio in real time on the CPU. |
| Disk | ~40 GB free | OCUDU build, srsRAN 4G, Docker images. |
| OCUDU | release 26.10 | Formerly srsRAN Project. Pin the release, not `dev`. |
| srsRAN 4G (srsUE) | 23.11 or later | Only `srsue` is built. 5G SA is maintenance-only: 15 kHz SCS, FDD, 5–20 MHz, one TAC. |
| Open5GS | built by OCUDU's `docker/` | Pre-provisioned with srsUE's default subscriber. |
| Docker Engine | with the compose plugin | Docker Engine on the host, not Docker Desktop: the gNB must reach the core's bridge. |
| Cell | band 3 FDD, 10 MHz, 15 kHz SCS, 11.52 Msps | The configuration OCUDU's srsUE tutorial uses. |

Paths used below: `~/src/ocudu`, `~/src/srsRAN_4G`, `~/ran-configs`, `~/mec-cast`.

## Part 1 — the 5G network

### 1.1 Prepare the host

Kernel features (SCTP carries NGAP between gNB and AMF):

```bash
sudo modprobe sctp || { sudo apt install -y linux-modules-extra-$(uname -r) && sudo modprobe sctp; }
echo sctp | sudo tee /etc/modules-load.d/sctp.conf      # load at every boot
sudo ip netns add test && sudo ip netns del test         # namespaces work
ls -l /dev/net/tun                                       # TUN exists
```

Build dependencies for OCUDU and srsRAN 4G:

```bash
sudo apt update
sudo apt install -y git cmake make gcc g++ clang pkg-config ccache \
  libfftw3-dev libmbedtls-dev libsctp-dev libyaml-cpp-dev libgtest-dev \
  libzmq3-dev libboost-program-options-dev libconfig++-dev \
  iperf3 net-tools tcpdump tmux
```

Docker Engine, and use it without `sudo`:

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
newgrp docker                          # or log out and back in
docker run --rm hello-world && docker compose version
```

### 1.2 Build the OCUDU gNB

```bash
mkdir -p ~/src && cd ~/src
git clone --branch release_26_10 https://gitlab.com/ocudu/ocudu.git
cd ocudu && git describe --tags          # confirm the release tag, not dev
mkdir -p build && cd build
CC=clang CXX=clang++ cmake .. -DCMAKE_BUILD_TYPE=Release \
  -DENABLE_EXPORT=ON -DENABLE_ZEROMQ=ON
make -j$(nproc) gnb
./apps/gnb/gnb --version                 # 26.10.x
```

- Clang, because OCUDU needs GCC ≥ 15.2 or Clang ≥ 14, and Ubuntu 24.04's GCC is 13.
- In the cmake output, `Found libZEROMQ` must appear. Without it the gNB has no
  ZMQ radio: install `libzmq3-dev`, delete `build/`, re-run cmake.
- No UHD and no real-time kernel are needed for ZMQ.

### 1.3 Build srsUE

```bash
cd ~/src
git clone https://github.com/srsran/srsRAN_4G.git
cd srsRAN_4G && git checkout release_23_11
mkdir build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release -DENABLE_WERROR=OFF
make -j$(nproc) srsue
```

`-DENABLE_WERROR=OFF` because GCC 13 turns a harmless array-bounds warning in
srsUE into an error. Check cmake reports ZeroMQ as found here too.

### 1.4 Start the Open5GS core

```bash
cd ~/src/ocudu/docker
docker compose up --build -d 5gc
docker compose logs -f 5gc              # Ctrl-C stops following, not the core
```

This creates the Docker bridge `10.53.1.0/24`: core `10.53.1.2`, host
`10.53.1.1`. Subscribers come from `SUBSCRIBER_DB` in
`docker/open5gs/open5gs.env`; the default entry (IMSI 001010123456780,
K `00112233445566778899aabbccddeeff`, OPc `63bfa50ee6523365ff14c1f45f88737d`)
matches the srsUE config below. WebUI: http://localhost:9999 (admin / 1423).

**Check the AMF advertises exactly one TAC.** srsUE cannot decode a tracking
area list with more than one entry (see [Troubleshooting](#troubleshooting)).
OCUDU's `dev` branch adds TACs 8 and 9 for an NTN example:

```bash
sed -n '/^amf:/,/^[a-z]/p' open5gs/open5gs-5gc.yml | grep 'tac:'
```

If more than one `tac:` is printed for the AMF, delete the extra `plmn_id`
entries so only `tac: 7` remains, then `docker compose up --build --force-recreate -d 5gc`.
The `tai:` near the top of the file belongs to the 4G MME; leave it.

### 1.5 Configuration files

Start from the official tutorial files and compare them with the listings:

```bash
mkdir -p ~/ran-configs && cd ~/ran-configs
BASE=https://gitlab.com/ocudu/ocudu_docs/-/raw/038a05c4d2600cd7a8bd7ffb46c561b2ca21e5aa/docs/tutorials/srsue/assets
wget $BASE/gnb_zmq.yaml $BASE/ue_zmq.conf
```

The ZMQ ports are crossed (gNB transmits on 2000 and receives on 2001, the UE
the opposite) and the sample rate, band and PLMN must match on both sides.

`~/ran-configs/gnb_zmq.yaml`:

```yaml
cu_cp:
  amf:
    addr: 10.53.1.2              # Open5GS AMF in the container
    port: 38412
    bind_addr: 10.53.1.1         # this host's address on the Docker bridge
    supported_tracking_areas:
      - tac: 7
        plmn_list:
          - plmn: "00101"
            tai_slice_support_list:
              - sst: 1
  inactivity_timer: 7200         # keep idle UEs connected

ru_sdr:
  device_driver: zmq
  device_args: tx_port=tcp://127.0.0.1:2000,rx_port=tcp://127.0.0.1:2001,base_srate=11.52e6
  srate: 11.52
  tx_gain: 0                     # 26.10 rejects positive gains for ZMQ
  rx_gain: 0

cell_cfg:
  dl_arfcn: 368500
  band: 3
  channel_bandwidth_MHz: 10
  common_scs: 15
  plmn: "00101"
  tac: 7
  pdcch:
    common:
      ss0_index: 0               # srsUE capability
      coreset0_index: 6          # 6 for 10 MHz, 12 for 20 MHz
    dedicated:
      ss2_type: common
      dci_format_0_1_and_1_1: false
  prach:
    prach_config_index: 1        # what srsUE expects

log:
  filename: /tmp/gnb.log
  all_level: warning
```

`~/ran-configs/ue_zmq.conf` (sections that differ from the defaults):

```ini
[rf]
freq_offset = 0
tx_gain = 50
rx_gain = 40
srate = 11.52e6
nof_antennas = 1
device_name = zmq
device_args = tx_port=tcp://127.0.0.1:2001,rx_port=tcp://127.0.0.1:2000,base_srate=11.52e6

[rat.eutra]
dl_earfcn = 2850
nof_carriers = 0                 # LTE off: 5G SA only

[rat.nr]
bands = 3
nof_carriers = 1
max_nof_prb = 52                 # 10 MHz (106 for 20 MHz)
nof_prb = 52

[rrc]
release = 15
ue_category = 4

[usim]
mode = soft
algo = milenage
opc  = 63BFA50EE6523365FF14C1F45F88737D
k    = 00112233445566778899aabbccddeeff
imsi = 001010123456780
imei = 353490069873319

[nas]
apn = internet
apn_protocol = ipv4

[gw]
netns = ue1
ip_devname = tun_srsue
ip_netmask = 255.255.255.0

[log]
filename = /tmp/ue.log
all_level = warning
```

For 20 MHz: sample rate 23.04 / 23.04e6 in all four places, `coreset0_index: 12`,
106 PRBs. The cell carries roughly 13 Mbit/s uplink at 10 MHz; see
[capacity](#what-this-setup-is-not).

### 1.6 Per-boot script

Namespaces, routes and the SCTP module do not survive a reboot.
`~/ran-configs/up.sh`:

```bash
#!/bin/bash
# Per-boot setup for the local 5G testbed. Run before starting the gNB.
set -e
sudo modprobe sctp                                            # NGAP transport
sudo ip netns list | grep -q '^ue1' || sudo ip netns add ue1  # UE namespace
sudo ip netns exec ue1 sysctl -qw net.ipv6.conf.all.disable_ipv6=1   # no IPv6 RS spam at the UPF
cd ~/src/ocudu/docker && docker compose up -d 5gc             # core + bridge 10.53.1.0/24
for _ in $(seq 1 30); do ip addr | grep -q 10.53.1.1 && break; sleep 1; done
ip route | grep -q 10.45.0.0/16 || sudo ip route add 10.45.0.0/16 via 10.53.1.2   # downlink return path
```

```bash
chmod +x ~/ran-configs/up.sh && ~/ran-configs/up.sh
```

### 1.7 Bring up and verify

Order: **core → gNB → UE**. Restart the gNB whenever you restart srsUE — the
ZMQ sample stream has no resynchronisation. Use three terminals (or tmux).

```bash
# 1. gNB — wait for "Connecting to AMF on 10.53.1.2:38412" and "==== gNB started ==="
cd ~/src/ocudu/build/apps/gnb && sudo ./gnb -c ~/ran-configs/gnb_zmq.yaml

# 2. srsUE — wait for "PDU Session Establishment successful. IP: 10.45.1.2"
cd ~/src/srsRAN_4G/build/srsue/src && sudo ./srsue ~/ran-configs/ue_zmq.conf
```

The core log should show `gNB-N2 accepted`, `Registration request`, and the UPF
line `UE … APN[internet] … IPv4[10.45.1.2]`.

```bash
# 3. routes and traffic
sudo ip netns exec ue1 ip route add default via 10.45.1.1 dev tun_srsue
sudo ip netns exec ue1 ping -c 4 10.45.1.1           # UE -> UPF
sudo ip netns exec ue1 ping -c 4 10.53.1.1           # UE -> host (N6)
ping -c 4 10.45.1.2                                  # host -> UE

iperf3 -s -D                                         # server on the host
sudo ip netns exec ue1 iperf3 -c 10.53.1.1 -t 30 -u -b 10M
```

Type `t` in the srsUE console for per-second MCS, BLER and bitrate. To confirm
traffic really crosses the RAN, `sudo tcpdump -ni any udp port 2152` shows
every ping as GTP-U between 10.53.1.1 and 10.53.1.2.

Stop with Ctrl-C on srsUE, then the gNB; `docker compose down` in
`~/src/ocudu/docker` stops the core.

## Part 2 — mec-cast over the 5G link

[`deploy/compose/local-5g.yml`](../../deploy/compose/local-5g.yml) is the
lab topology collapsed onto this host. It runs **with** `logging.yml` and
`admin.yml`:

| Service | Network | Plays the lab's |
|---|---|---|
| `zenoh-router`, `edge` | host (N6 side, `10.53.1.1`) | edge role |
| `lidar-client` | own namespace, `tun_srsue` moved in by srsUE | UE role |
| `ran-collector` (`--profile ran`) | host, beside the gNB | gNB role |
| `logging`, `postgres`, `admin` | compose bridge | infra role |

Differences from the lab: no PTP (`PTP_DEVICE` empty, everything shares one
clock anyway), and no RIC.

### 2.1 Build the image

```bash
cd ~/mec-cast
git submodule update --init services/logging     # the webrtc submodule is not needed
make build-ros2
```

### 2.2 Start the stack

With the core and gNB running and **srsUE stopped**:

```bash
cd ~/mec-cast
make up-local-5g
docker logs -f mec5g-lidar-client-1
# waiting for the 5G link: 10.53.1.1 does not route via tun_srsue yet
```

Start it through `make`, not a bare `docker compose ... up --build`. The
Makefile passes the commit as a build argument. A plain rebuild stamps every
node's version `unknown`, and the admin's version-skew check goes quiet.
For logs and one-off commands, this shell function is the same project:

```bash
mec5g() { docker compose -p mec5g -f deploy/compose/logging.yml \
  -f deploy/compose/admin.yml -f deploy/compose/local-5g.yml "$@"; }
```

That message is expected. The admin page (http://localhost:8099/admin) shows
only the edge node until the UE has its route.

### 2.3 Give srsUE the UE container's namespace

srsUE puts `tun_srsue` into whatever `ue1` is when it starts. Point `ue1` at
the LiDAR client's namespace:

```bash
PID=$(docker inspect -f '{{.State.Pid}}' mec5g-lidar-client-1)
sudo ip netns del ue1 2>/dev/null; sudo rm -f /run/netns/ue1
sudo mkdir -p /run/netns && sudo ln -sfT /proc/$PID/ns/net /run/netns/ue1
sudo ip netns identify $PID                       # must print: ue1
sudo ip netns exec ue1 sysctl -qw net.ipv6.conf.all.disable_ipv6=1
```

Then restart the radio in order — gNB, then srsUE — and wait for
`PDU Session Establishment successful`.

Redo this step whenever the `lidar-client` container is recreated: its PID,
and with it the namespace, changes.

### 2.4 Route the edge over 5G

```bash
sudo ip netns exec ue1 ip link set tun_srsue mtu 1400          # room for GTP-U
sudo ip netns exec ue1 ip route add 10.53.1.1/32 via 10.45.1.1 dev tun_srsue
```

Only the edge address goes over 5G. **Do not** add a default route via
`tun_srsue` here: the container's default route must stay on the compose bridge
so `logging` and `admin` resolve.

Within two seconds the client log prints
`5G link up: 10.53.1.1 via 10.45.1.1 dev tun_srsue src 10.45.1.2` and the
UE node `client-ue-local-0` registers in the admin.

### 2.5 Run an experiment

Start a run from the admin page; the admin names it. While it streams:

```bash
sudo tcpdump -ni any udp port 2152 -c 20      # LiDAR frames as GTP-U
```

and the srsUE trace (`t`) shows UL bitrate rising. Results are the usual
([running-an-experiment.md](running-an-experiment.md)):

```bash
ls runs/<run_id>/                              # run.json, pub-0/, edge-0/
curl -sG http://localhost:8000/api/v1/logs --data-urlencode 'trace_id=<run_id>' | jq
```

Workload knobs are compose variables, e.g.
`NUM_POINTS=3000 RATE_HZ=10 make up-local-5g`. This recreates the
`lidar-client` container, so repeat 2.3–2.4.

### 2.6 Optional: the RAN metrics tap

Merge the `metrics` and `remote_control` sections of
[`deploy/lab/srsran/gnb.mec-cast.yml`](../../deploy/lab/srsran/gnb.mec-cast.yml)
into `gnb_zmq.yaml` and restart the gNB. Those key names were written for
srsRAN 25.04+; confirm them against `./gnb --help` on the OCUDU build before
relying on them. Then:

```bash
make up-local-5g RAN=1
```

KPIs land in `runs/<run_id>/ran/`, joined to the application data by run id.

### 2.7 Tear down

```bash
make down-local-5g                # keeps the database — never add -v
# then Ctrl-C srsUE and the gNB; core: cd ~/src/ocudu/docker && docker compose down
sudo rm -f /run/netns/ue1 && sudo ip netns add ue1   # back to a plain UE namespace
```

## Troubleshooting

Most failures are a start-order problem or a mismatch between two files.
Read the symptom column first.

| Symptom | Cause | Fix |
|---|---|---|
| `permission denied … /var/run/docker.sock` | The shell predates your `docker` group membership | `newgrp docker`, or log out and in |
| gNB exits: `Channel gain must be <= 0.0 dB for ZMQ-device` | Positive `tx_gain`/`rx_gain` in `ru_sdr` | Set both to 0 |
| gNB never connects to the AMF | Core not up, or 10.53.1.1 not on the host yet | `docker compose ps`; `ip addr \| grep 10.53.1.1`; start the core first |
| srsUE stops at `Attaching UE...`, no `Random Access` line | No cell: gNB not running, not started, or left over from an earlier UE session | Ctrl-C both; start gNB, wait for `gNB started`, then srsUE. `sudo ss -tnp \| grep ':200[01]'` must show both processes |
| UE released after ~3 s; gNB log: `UE did not request a PDU session after 3000ms` | srsUE could not decode the Registration Accept | `grep -i "Decoding Failed" /tmp/ue.log` — see the next row |
| `Decoding Failed (5GS tracking area identity list): Length (19) does not equal expected length 7` | The AMF advertises several TACs; srsUE handles one | Reduce the AMF `tai:` list to `tac: 7` ([1.4](#14-start-the-open5gs-core)) |
| Core: `RRC Release` right after attach, `Cannot find IMSI in DB` | Subscriber missing or credentials differ | Fix `SUBSCRIBER_DB` or add the IMSI in the WebUI |
| UPF: `Invalid packet [IP version:6 …]` | IPv6 router solicitations from `tun_srsue`; the session is IPv4 | Harmless; disable IPv6 in `ue1` (in `up.sh`) |
| UE pings UPF but iperf3 / the edge times out | Host has no route back to the UE pool | `sudo ip route add 10.45.0.0/16 via 10.53.1.2` |
| iperf3: `Address already in use` | A server is already running | Use it, or `pkill iperf3` |
| Late / underflow messages, attach unstable | CPU cannot keep up with real time | Stay at 10 MHz, close other load, run OCUDU's `scripts/ocudu_performance` |
| Admin shows only the edge; client log `waiting for the 5G link` | srsUE is not in the container's namespace, or the /32 route is missing | `sudo ip netns identify $PID` must print `ue1` ([2.3](#23-give-srsue-the-ue-containers-namespace)); `docker exec mec5g-lidar-client-1 ip addr show tun_srsue`; add the route ([2.4](#24-route-the-edge-over-5g)) |
| `5G link up` printed but no UE node or no frames | Router unreachable through the tunnel | `sudo ip netns exec ue1 ping 10.53.1.1`; check the return route |
| Large clouds stall while pings work | GTP-U overhead exceeds the MTU | `ip link set tun_srsue mtu 1400` in `ue1` |
| Heavy frame loss, latency climbing during a run | Offered load above cell capacity | Lower `NUM_POINTS`/`RATE_HZ`, or use a 20 MHz cell |

Logs: gNB `/tmp/gnb.log`, srsUE `/tmp/ue.log`, core
`docker logs -f open5gs_5gc`, mec-cast `mec5g logs -f`. Raise `all_level` to
`info` in either radio config for one run when a log is silent.

## What this setup is not

- **Not a timing reference.** ZMQ is not a real-time radio: slot timing, HARQ,
  scheduler delay and throughput are not representative, and a VM adds
  scheduling jitter on top. Latency and throughput results come from the lab.
- **Not a capacity reference.** A 10 MHz cell carried about 13 Mbit/s uplink in
  OCUDU's own test. The default workload (5000 points at 10 Hz ≈ 4.8 Mbit/s of
  payload) fits; above roughly 10–12 k points at 10 Hz the link saturates and
  you measure queueing.
- **Not PTP-disciplined.** Everything shares one clock, so cross-host timing
  questions do not arise. The clock findings are not switched off: they
  cannot fire, because one clock cannot disagree with itself (`WF_CLOCK_SKEW`
  needs a negative delay). Every entry still records `ptp.reliable: false`,
  so these runs fail validity rule 1 of the
  [research protocol](../research/protocol.md) by construction.
- **Not multi-UE as written.** One ZMQ link carries one UE; more need OCUDU's
  GNU Radio broker flowgraph and one namespace per UE.

Run the same OCUDU release here and in the lab, so the metrics schema and E2
behaviour you develop against match what you measure.

## References

- [OCUDU — Building a 5G network with srsUE](https://docs.ocudu.org/tutorials/srsue/)
- [OCUDU — Installation](https://docs.ocudu.org/user_manual/installation/)
- [OCUDU — Release notes](https://docs.ocudu.org/releases/release_notes/)
- [srsRAN Project → OCUDU transition notice](https://github.com/srsran/srsRAN_Project)
- [srsUE multi-TAC decoding failure](https://github.com/srsran/srsRAN_Project/discussions/120)
- [ADR-0001 — Zenoh over DDS](../architecture/adr/0001-zenoh-over-dds.md) (why the UE dials out to the router)
