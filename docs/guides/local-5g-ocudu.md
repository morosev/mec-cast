# Local 5G testbed with OCUDU (Ubuntu)

A whole 5G network on one Ubuntu machine, with mec-cast running across it:
an **OCUDU** gNB, an **srsUE** software UE and an **Open5GS** core, with
**ZeroMQ** standing in for the radio. The LiDAR client sends from the UE side
of the link and the edge receives on the N6 side, so every frame crosses the
UE stack, the gNB scheduler and the GTP-U tunnel through the UPF.

Use it to develop and integration-test the pipeline against a real RAN stack
without SDR hardware: the Zenoh/LiDAR path, telemetry, the admin, both RAN
sources (the JSON metrics tap and E2SM-KPM through the near-RT RIC), and RAN
control (an E2SM-RC PRB quota applied by the mec-cast xApp). **Do not use its
latency or throughput numbers as results** (see
[What this setup is not](#what-this-setup-is-not)).

Every step here was run end to end in October 2026, including the E2 path:
KPM indications recorded by the xApp, and a PRB-quota control the gNB
acknowledged. The build was OCUDU from the 26.10 release line (a `dev`
checkout, commit `52af368`), srsRAN 4G `master` (`bef8680`) for srsUE, and the
Open5GS 2.6.6 container from OCUDU's `docker/`. Pin the release tags below for
anything you need to reproduce.

The netem-only path on one box is [local-development.md](local-development.md);
the real testbed is [operations/lab-topology.md](../operations/lab-topology.md).

- [Topology](#topology)
- [Prerequisites](#prerequisites)
- [Part 1 — the 5G network](#part-1--the-5g-network)
- [Part 2 — mec-cast over the 5G link](#part-2--mec-cast-over-the-5g-link)
- [Part 3 — E2: the RIC, the xApp and RAN control](#part-3--e2-the-ric-the-xapp-and-ran-control)
- [Cold start: the whole order](#cold-start-the-whole-order)
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

With Part 3 a fourth network joins: the RIC's own Docker network
`10.0.2.0/24` (oran-sc-ric). e2term is `10.0.2.10`, the xApp runner
`10.0.2.20`, and the host is `10.0.2.1` on it. The gNB reaches e2term
directly over SCTP, and the xApp reaches the admin and the logging service
through the host at `10.0.2.1`.

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
| Slice | SST 1, SD 1 (`000001`) | RAN control needs exactly this S-NSSAI on the gNB, the AMF and the subscriber ([why](#36-ran-control-a-prb-quota)). Configured from the start in Part 1. |
| near-RT RIC | oran-sc-ric at the pin in `deploy/lab/ric/ric.sh` | Part 3 only. Its images are amd64: an arm64 host needs Rosetta or QEMU 8.1.x ([3.1](#31-arm64-hosts-only-run-amd64-correctly)). |

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
sudo apt install -y git cmake gcc g++ clang pkg-config ccache make \
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

**Give the slice SD 1.** RAN control in Part 3 names the slice SST 1 / SD 1,
and the UE's traffic must live in that slice. The core ships SST 1 with no SD.
Add `sd: 000001` under **every** `sst: 1` in the 5GC file (the AMF's
`plmn_support`, and any NSSF/SMF slice entries):

```bash
grep -n -B1 -A2 "sst:" open5gs/open5gs-5gc.yml      # find them
```

```yaml
      s_nssai:
        - sst: 1
          sd: 000001
```

Then rebuild, which also resets the subscriber database:

```bash
docker compose up --build --force-recreate -d 5gc
```

and set the subscriber's slice: WebUI → IMSI 001010123456780 → slice SST 1,
SD `000001` → save. **Redo the WebUI step after every `--force-recreate`**: the
container re-provisions subscribers from `SUBSCRIBER_DB`, which carries no SD.
If the UE stops registering after a recreate, this is the first thing to check.

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
                sd: 1            # the slice RAN control names (Part 3)
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
  slicing:                       # the scheduler slice a PRB quota rewrites
    - sst: 1
      sd: 1
      sched_cfg:
        min_prb_policy_ratio: 0
        max_prb_policy_ratio: 100

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

The `sd: 1` lines and the `slicing:` block are there for RAN control
(Part 3). They must match the core (1.4); with a mismatch the UE fails to
register. The metrics tap (2.6) and E2 (3.3) add further sections later.

For 20 MHz: sample rate 23.04 / 23.04e6 in all four places, `coreset0_index: 12`,
106 PRBs. The cell carries roughly 13 Mbit/s uplink at 10 MHz; see
[capacity](#what-this-setup-is-not).

### 1.6 Per-boot script

Namespaces, routes and the SCTP module do not survive a reboot, and `ue1`
also goes stale whenever the LiDAR client container is removed
(`make down-local-5g`). This script is for Part 1, the radio on its own.
From Part 2 on, `make attach-local-5g` does the same setup itself, every time
it runs. It is safe to run any number of times. `~/ran-configs/up.sh`:

```bash
#!/bin/bash
# Per-boot setup for the local 5G testbed. Run before starting the gNB.
set -e
sudo modprobe sctp                                            # NGAP transport
# UE namespace. `make attach-local-5g` turns ue1 into a link to the client
# container's namespace; once that container is gone the link dangles, still
# shows in `ip netns list`, and every `ip netns exec ue1` fails. Test it, not the name.
sudo ip netns exec ue1 true 2>/dev/null || { sudo rm -f /run/netns/ue1; sudo ip netns add ue1; }
sudo ip netns exec ue1 sysctl -qw net.ipv6.conf.all.disable_ipv6=1   # no IPv6 RS spam at the UPF
cd ~/src/ocudu/docker && docker compose up -d 5gc             # core + bridge 10.53.1.0/24
for _ in $(seq 1 30); do ip addr | grep -q 10.53.1.1 && break; sleep 1; done
ip route | grep -q 10.45.0.0/16 || sudo ip route add 10.45.0.0/16 via 10.53.1.2   # downlink return path
```

```bash
chmod +x ~/ran-configs/up.sh && ~/ran-configs/up.sh
```

### 1.7 Bring up and verify

A one-time check that the radio works on its own, before mec-cast is involved.
From Part 2 on, `make attach-local-5g` starts and stops the radio for you.

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

Stop with Ctrl-C on srsUE, then the gNB, before Part 2: the attach script
starts its own. Leave the core running; `docker compose down` in
`~/src/ocudu/docker` stops it.

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
clock anyway), and the RIC runs on this host too, on its own Docker network
(Part 3), where the lab runs it on the infra host.

### 2.1 Build the image

```bash
cd ~/mec-cast
git submodule update --init services/logging     # the webrtc submodule is not needed
make build-ros2
```

### 2.2 Start the stack

Nothing else needs to be running; the next step brings up the core and the
radio.

```bash
cd ~/mec-cast
make up-local-5g
docker logs mec5g-lidar-client-1
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
the edge (and the gNB, with the tap on) but not the UE until step 2.3 is done.

### 2.3 Bring the radio up behind the UE

```bash
make attach-local-5g          # = bash scripts/ocudu-attach-ue.sh; asks for sudo
```

It does four things, and is safe to run at any time:

1. **Host setup**, as `up.sh` does: SCTP, the Open5GS core if it is not up,
   and the return route to the UE pool.
2. **Points the `ue1` namespace name at the LiDAR client container's
   namespace.** srsUE's `[gw] netns = ue1` opens that name when srsUE starts,
   which is how `tun_srsue` ends up inside the container.
3. **Starts the radio**, if the link is not already up: stops any running
   srsUE and gNB, starts the gNB and waits for `gNB started`, then starts
   srsUE and waits for the PDU session. They run in two root tmux sessions,
   so the consoles stay usable:
   ```bash
   sudo tmux attach -t srsue     # type t for the per-second trace; Ctrl-b d to leave
   sudo tmux attach -t gnb
   ```
   Copies of both consoles go to `/tmp/gnb.console` and `/tmp/srsue.console`.
4. **Routes the edge over 5G:** MTU 1400 (room for GTP-U), IPv6 off,
   `10.53.1.1/32` via `tun_srsue`, then a ping across the link.

The binaries and configs are found at the paths of Part 1. If yours differ,
set `GNB_BIN`, `GNB_CONF`, `SRSUE_BIN`, `UE_CONF` or `OCUDU_DOCKER`.

Only the edge address goes over 5G. The container's default route stays on the
compose bridge, so `logging` and `admin` still resolve. **Never** add a
default route via `tun_srsue` here.

Within two seconds the client log prints
`5G link up: 10.53.1.1 dev tun_srsue src 10.45.1.2` and the UE node
`client-ue-local-0` registers in the admin.

### 2.4 When to run it again

Neither the route nor the namespace link persists. In every case below the
fix is one command:

| What happened | What it broke | Run |
|---|---|---|
| The client container was recreated: `make up-local-5g` after a new commit, `RAN=1`, a changed knob, `down`/`up` | The container has a new namespace; `ue1` names the dead one | `make attach-local-5g`: it sees the move and restarts the radio |
| You changed `gnb_zmq.yaml` or `ue_zmq.conf` | Nothing yet: the radio still runs the old config | `make attach-local-5g RESTART=1` |
| The gNB or srsUE stopped, or was restarted by hand | The new `tun_srsue` has no route and the default MTU | `make attach-local-5g` |
| Host or VM reboot | Everything | `make up-local-5g`, then `make attach-local-5g` |

With the link already up, `make attach-local-5g` leaves the radio alone and
only re-checks the route.

`make up-local-5g` rebuilds the image, and a new commit changes it, so any
re-run of that target can recreate the client. To change only one service,
use the `mec5g` function with `--no-deps`, e.g.
`mec5g --profile ran up -d --no-deps ran-collector`.

<details>
<summary>What the script runs, by hand</summary>

To drive the radio yourself instead, `make attach-local-5g MANUAL=1` does
everything except start it, and waits while you do. Or, entirely by hand:

```bash
PID=$(docker inspect -f '{{.State.Pid}}' mec5g-lidar-client-1)
sudo ip netns del ue1 2>/dev/null; sudo rm -f /run/netns/ue1
sudo mkdir -p /run/netns && sudo ln -sfT /proc/$PID/ns/net /run/netns/ue1
sudo ip netns identify $PID                       # must print: ue1
sudo ip netns exec ue1 sysctl -qw net.ipv6.conf.all.disable_ipv6=1
# restart gNB, then srsUE; after "PDU Session Establishment successful":
sudo ip netns exec ue1 ip link set tun_srsue mtu 1400
sudo ip netns exec ue1 ip route replace 10.53.1.1/32 dev tun_srsue
```

</details>

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
`NUM_POINTS=3000 RATE_HZ=10 make up-local-5g`. That recreates the
`lidar-client` container, so run `make attach-local-5g` again.

### 2.6 Optional: the RAN metrics tap

Merge the `metrics` and `remote_control` sections of
[`deploy/lab/srsran/gnb.mec-cast.yml`](../../deploy/lab/srsran/gnb.mec-cast.yml)
into `gnb_zmq.yaml` (`metrics.enable_json`, the `layers`, and `remote_control`
on 127.0.0.1:8001). Those keys work unchanged on OCUDU 26.10. Then:

```bash
make up-local-5g RAN=1                # adds ran-collector; may recreate the client
make attach-local-5g RESTART=1        # the gNB loads the new config; srsUE follows
```

`RESTART=1` because the gNB config changed: the radio must restart even if
the link was up. The admin then shows three nodes: the gNB, the edge and
`client-ue-local-0`.

KPIs land in `runs/<run_id>/ran/` (`kpi.csv`, `samples.csv`, `reports.jsonl`),
joined to the application data by run id. Check the join:

```bash
python3 tools/ran_join.py runs/<run_id>      # expect every frame "with RAN state"
```

The tap names the UE by its RNTI (e.g. `17921` = `0x4601`); KPM in Part 3 calls
the same UE `e2:0`.

The OCUDU 26.10 metrics schema has no committed fixture yet. After the first
good run, capture one ([deploy manual, step 5](../operations/deploy-manual.md#the-gnb--metrics-tap-e2-and-the-ric)):

```bash
bash scripts/ran-fixture.sh <run_id> 26.10.0 ws
```

### 2.7 Tear down

```bash
make down-local-5g                # stack, then srsUE and the gNB; keeps the database (never add -v)
cd ~/src/ocudu/docker && docker compose down        # the core, if you want it gone too
```

`make down-local-5g` also turns `ue1` back into a plain namespace, so Part 1's
manual check works again afterwards.

## Part 3 — E2: the RIC, the xApp and RAN control

The near-RT RIC is srsRAN's
[oran-sc-ric](https://github.com/srsran/oran-sc-ric), cloned beside the repo
at the pin in [`deploy/lab/ric/ric.sh`](../../deploy/lab/ric/ric.sh) and never
modified (ADR-0010). `ric.sh` starts it, runs the stock KPM monitor, and runs
the mec-cast xApp inside the RIC's xApp runner. This part is the lab's E2
procedure
([deploy manual, steps 6–8](../operations/deploy-manual.md#the-gnb--metrics-tap-e2-and-the-ric))
on one host, plus what OCUDU 26.10 needs that srsRAN did not
([3.7](#37-what-ocudu-2610-needs-from-the-e2-path)).

Do the steps in order and do not skip the stock monitor. A failure there is
the E2 path; a failure after it is the xApp.

### 3.1 arm64 hosts only: run amd64 correctly

The RIC images are amd64 only. `ric.sh` builds and runs them as amd64 on an
arm64 host, so the host needs a way to execute amd64 binaries, and **which way
matters**. QEMU user emulation 9.0.1 and later crashes Go programs in the
garbage collector (`fatal error: lfstack.push`,
[QEMU #2560](https://gitlab.com/qemu-project/qemu/-/issues/2560)). The build
of `rtmgr_sim` dies first, and the RIC's Go services would follow at runtime.

- **Ubuntu VM on Apple Silicon (Lima, VZ):** use Rosetta. On the Mac:
  ```bash
  limactl stop <vm> && limactl edit <vm> --rosetta && limactl start <vm>
  ```
  (or `rosetta: {enabled: true, binfmt: true}` in the VM's YAML). In the VM,
  drop QEMU's handler so Rosetta takes amd64:
  ```bash
  docker run --privileged --rm tonistiigi/binfmt --uninstall qemu-x86_64
  ls /proc/sys/fs/binfmt_misc/                           # rosetta, no qemu-x86_64
  docker run --rm --platform linux/amd64 alpine uname -m # x86_64
  ```
  A VM restart drops namespaces and routes: `make attach-local-5g` redoes them.
- **arm64 Ubuntu on bare metal:** QEMU 8.1.x, from before the regression:
  ```bash
  docker run --privileged --rm tonistiigi/binfmt --uninstall qemu-x86_64
  docker run --privileged --rm tonistiigi/binfmt:qemu-v8.1.5 --install amd64
  ```

`ric.sh up` checks which handler is registered and says so.

### 3.2 Start the RIC — before the gNB

`runs/` is usually created by the mec-cast containers, as root, and `ric.sh`
writes `runs/ric/` as you. Hand the directory back once:

```bash
cd ~/mec-cast
sudo chown $USER: runs                 # top level only; run folders stay the containers'
```

Then:

```bash
bash deploy/lab/ric/ric.sh up          # clones ~/oran-sc-ric at the pin, builds, starts
docker logs -f ric_submgr              # wait for "RMR is ready now", then Ctrl-C
ip addr | grep 10.0.2.1                # the RIC network exists on the host
```

Start the RIC before the gNB: the gNB sets up E2 when it starts.

### 3.3 Point the gNB at the RIC

Add to `~/ran-configs/gnb_zmq.yaml` (keep the tap sections from 2.6):

```yaml
e2:
  enable_du_e2: true
  e2sm_kpm_enabled: true
  e2sm_rc_enabled: true
  addr: 10.0.2.10        # e2term on the RIC network
  bind_addr: 10.0.2.1    # this host on that network
  port: 36421

pcap:
  e2ap_enable: true      # E2AP capture for fixtures; written when the gNB exits
  e2ap_du_filename: /tmp/gnb_du_e2ap.pcap
```

Load it:

```bash
make attach-local-5g RESTART=1
```

Then:

```bash
bash deploy/lab/ric/ric.sh status     # "gnbd_001_001_00019b_0 CONNECTED"
```

The RIC refuses an E2 reconnect within 60 s of a disconnect. If the node is
missing after a quick gNB restart, wait a minute and restart the radio:
`sleep 60 && make attach-local-5g RESTART=1`.

OCUDU logs E2 at debug level as `[E2-DU]`, `[E2SM-KPM]`, `[E2SM-RC]`,
`[E2-SUBSCRIBER]`. Search for `\[E2`: a plain `grep ric` matches "metric" on
every PHY line.

### 3.4 Prove E2 with the stock monitor

Put load on the UE (a mec-cast run from the admin, or
`sudo ip netns exec ue1 iperf3 -c 10.53.1.1 -t 300 -u -b 5M`), then:

```bash
KPM_SECONDS=30 bash deploy/lab/ric/ric.sh kpm
```

It subscribes with the defaults, which are style 2 (one UE, E2 UE id 0) and
the five metrics OCUDU serves ([3.7](#37-what-ocudu-2610-needs-from-the-e2-path)).
It prints one indication per second and finishes with submgr's verdict:

```text
RIC Indication Received from gnbd_001_001_00019b_0 for Subscription ID: 4, KPM Report Style: 2, UE ID: 0
--Metric: DRB.UEThpUl, Value: [5714.15185546875]
...
--- submgr verdict for this monitor:
    no subscription failure
```

With the LiDAR stream at its defaults, `DRB.UEThpUl` reads about 5,700
(kbit/s), and `DRB.RlcSduTransmittedVolumeUL` about 5,700 per 1 s period
(kbit). Keep the evidence as E2 fixtures. The PCAP is written when the gNB
exits cleanly, so copy it after the next Ctrl-C of the gNB:

```bash
mkdir -p runs/ric/e2-fixtures && cp runs/ric/kpm-*.log runs/ric/e2-fixtures/
cp /tmp/gnb_du_e2ap.pcap runs/ric/e2-fixtures/      # after a clean gNB stop
```

### 3.5 The mec-cast xApp

```bash
INFRA_HOST=10.0.2.1 bash deploy/lab/ric/ric.sh xapp -d
```

`INFRA_HOST` is the host as the RIC network sees it. `ric.sh` hands the xApp
the same style, metrics and UE ids the monitor used, as defaults; run params
override them per run. In the foreground (no `-d`) it logs:

```text
mec_cast_xapp: xapp 0.1.0 up: adapter=osc capabilities=['kpm_monitor', 'rc_control'] ...
admin: connected to ws://10.0.2.1:8099/ws/node
```

The admin page shows a fourth node, `xapp-<host>-0`, with `e2_connected=true`.
It subscribes when a run starts. Start a run of about two minutes from the
admin page, with the LiDAR streaming:

```bash
ls runs/<run_id>/ran-kpm/                          # kpi.csv, indications.jsonl
head -5 runs/<run_id>/ran-kpm/kpi.csv              # source=kpm, ue=e2:0, canonical names + units
python3 tools/ran_join.py runs/<run_id>                 # JSON tap
python3 tools/ran_join.py runs/<run_id> --source kpm    # KPM
```

On the verification run both sources joined to the frames: the tap to
3154/3154, KPM to 3147/3154 (the gap is the seconds before the first
indication). On the admin page:

| Finding | Meaning |
|---|---|
| `WF_XAPP_NO_E2` | The xApp found no CONNECTED E2 node: the gNB's `e2:` block, or the 60 s rule |
| `WF_KPM_SILENT` + the node's `kpm_error` says *rejected* | The gNB refused the subscription: an unsupported metric or style ([3.7](#37-what-ocudu-2610-needs-from-the-e2-path)) |
| `WF_KPM_SILENT`, no `kpm_error` | Subscribed, nothing arriving: the UE is idle |
| `WF_RAN_SOURCES_DISAGREE` in steady traffic | The two sources disagree by a constant factor: a unit is wrong (`ran/schema/metrics.md`) |

### 3.6 RAN control: a PRB quota

A run can carry a `ran_policy` (ADR-0011): a PRB cap on a UE, optionally
stepped over time, reverted when the run stops. Create one with a 30 % cap
released at 30 s, start it, and stop it after 60–90 s:

```bash
RUN=$(curl -s -X POST http://localhost:8099/api/v1/runs -H 'Content-Type: application/json' -d '{
  "label":"rc-smoke",
  "params":{"ran_policy":{"ue":0,"min_prb_ratio":0,"max_prb_ratio":30,"dedicated_prb_ratio":0,
                          "schedule":[{"t_s":30,"max_prb_ratio":100}]}}}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["run_id"])')
curl -s -X POST http://localhost:8099/api/v1/runs/$RUN/start; echo
sleep 75
curl -s -X POST http://localhost:8099/api/v1/runs/$RUN/stop; echo
cat runs/$RUN/ran-kpm/control.csv
```

Every action must read `sent` then `ack`:

```text
...,apply,0,30,0,sent,
...,apply,0,30,0,ack,
...,step@30s,0,100,0,sent,
...,step@30s,0,100,0,ack,
...,revert-run-stop,...,ack,
```

`failed,RIC_CONTROL_FAILURE` with the admin raising `WF_POLICY_NOT_APPLIED`
means the gNB has no slice matching the control. oran-sc-ric's encoder
hard-codes PLMN 00101, **SST 1, SD 1**, and OCUDU matches the full S-NSSAI
against a slice in `cell_cfg.slicing`. The gNB log shows the request, then the
refusal:

```text
[E2SM-RC ] [I] Slice-level PRB quota Control Request for UE gNB-DU-UE-F1AP-ID=0:
  RRM Policy Member List:
  - PLMN:00101, SST:1, SD:1
[E2-DU   ] [I] Sending E2 RIC Control Failure
```

The fix is SD 1 in all three places, as in Part 1: the gNB's
`tai_slice_support_list` and `slicing:`, the core's `s_nssai` entries, and the
subscriber. Adding `slicing:` without the SD was tried and is not enough.

**What the cap does to the traffic is still open.** oran-sc-ric describes the
quota as a downlink limit; the LiDAR traffic is uplink. Compare both
directions under the cap and after it:

```bash
T0=$(awk -F, 'NR==2{print $1}' runs/$RUN/ran-kpm/control.csv)
awk -F, -v t0=$T0 '$6 ~ /ue\.(ul|dl)_throughput_bps/ { p = ($2 < t0+30e9) ? "capped" : "free";
  s[$6" "p]+=$7; n[$6" "p]++ } END { for (k in s) printf "%-28s %.2f Mbit/s\n", k, s[k]/n[k]/1e6 }' \
  runs/$RUN/ran/kpi.csv | sort
```

Record what you see: it is calibration step 0.1 of the
[research protocol](../research/protocol.md), and it decides whether this
control is a lever on the LiDAR stream at all.

### 3.7 What OCUDU 26.10 needs from the E2 path

oran-sc-ric and mec-cast's E2 defaults were written against srsRAN Project.
OCUDU 26.10 differs in the following, and the repo now carries each fix:

| Area | srsRAN-era default | What OCUDU 26.10 does | Now |
|---|---|---|---|
| KPM report style | 5, one UE id | Style 5 needs at least two UE ids; a request for a UE it does not have is refused | Style 2 (one UE, E2 UE id 0) in `ric.sh` and the xApp |
| KPM metrics | Six, incl. `DRB.PacketSuccessRateUlgNBUu` | Does not serve `PacketSuccessRateUlgNBUu`. **One unsupported metric refuses the whole subscription**: `Action not supported` in the gNB log, `RICSubscriptionFailure … Cause:6, Value 0` at submgr | Five metrics: `DRB.UEThpDl`, `DRB.UEThpUl`, `DRB.RlcSduTransmittedVolumeDL`, `…UL`, `DRB.RlcPacketDropRateDl` |
| Rejected subscription | Reported as subscribed | The refusal arrives after the REST call succeeds, as an E2EventInstanceId of 0 | The xApp checks for it: `kpm_error` says *rejected*, retry after 60 s |
| PRB-quota slice | Matched on SST | Matches PLMN + SST + SD exactly, against `cell_cfg.slicing` | SD 1 on the gNB, the core and the subscriber |
| `dedicated_prb_ratio` | 100 (mec-cast default) | The ACKed control used 0. 100 was only tried before the slice fix, so untested since | Pass 0 explicitly until 100 is tried |
| `ric.sh kpm` | Ctrl-C ended it | Under `\| tee` Ctrl-C never reached the container. The monitor kept its ports, and the next one died with `Address already in use` before subscribing | `ric.sh` stops leftovers before and after; `KPM_SECONDS` |

The same table belongs to the lab: these are properties of the gNB software,
not of ZMQ.

### 3.8 Stop the RIC

```bash
bash deploy/lab/ric/ric.sh down
```

The xApp reverts any policy in force when its run stops, when the admin is
gone for 30 s, and at its next boot after a crash, so stopping the RIC never
leaves a cap behind in the gNB (ADR-0011).

## Cold start: the whole order

After a host or VM boot, with everything built:

1. `bash deploy/lab/ric/ric.sh up`, then wait for `RMR is ready now`. Only if
   you use E2; it must come before the gNB.
2. `make up-local-5g RAN=1`: mec-cast, with the metrics tap.
3. `make attach-local-5g`: host setup and the core, `ue1`, the gNB, srsUE,
   the route.
4. `bash deploy/lab/ric/ric.sh status`: the node is CONNECTED. If not:
   `sleep 60 && make attach-local-5g RESTART=1`.
5. `INFRA_HOST=10.0.2.1 bash deploy/lab/ric/ric.sh xapp -d`.
6. The admin page shows four nodes: gNB, edge, `client-ue-local-0` and
   `xapp-<host>-0`. Start runs.

Every later restart is `make attach-local-5g`: plain after the client
container was recreated or the radio stopped, `RESTART=1` after a radio
config change. It restarts the gNB and srsUE together, gNB first, because
ZMQ cannot resynchronise, and always after `ue1` points at the client
container, because srsUE opens the namespace only when it starts.

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
| `Cannot open network namespace "ue1": No such file or directory` | `ue1` still links to a client container that was removed without `make down-local-5g` | `make attach-local-5g` relinks it; for Part 1 on its own, `up.sh` resets it |
| UE pings UPF but iperf3 / the edge times out | Host has no route back to the UE pool | `sudo ip route add 10.45.0.0/16 via 10.53.1.2` |
| iperf3: `Address already in use` | A server is already running | Use it, or `pkill iperf3` |
| Late / underflow messages, attach unstable | CPU cannot keep up with real time | Stay at 10 MHz, close other load, run OCUDU's `scripts/ocudu_performance` |
| Admin shows the edge (and gNB) but no UE; client log `waiting for the 5G link` | srsUE or the gNB restarted, or the client container was recreated; the route or the namespace link is gone | `make attach-local-5g` ([2.4](#24-when-to-run-it-again)) |
| `make attach-local-5g`: `gNB did not print 'gNB started'` or no PDU session | The gNB or srsUE failed to start: core down, a config error, a stale process holding the ZMQ ports | The script prints the console tail; more in `sudo tmux attach -t gnb` / `-t srsue` and `/tmp/*.console` |
| `5G link up` printed but no UE node or no frames | Router unreachable through the tunnel | `sudo ip netns exec ue1 ping 10.53.1.1`; check the return route |
| Large clouds stall while pings work | GTP-U overhead exceeds the MTU | `make attach-local-5g` (sets MTU 1400) |
| Heavy frame loss, latency climbing during a run | Offered load above cell capacity | Lower `NUM_POINTS`/`RATE_HZ`, or use a 20 MHz cell |
| srsUE attached (`PUCCH … cqi=15` in its log) but `Device "tun_srsue" does not exist` in the client container | srsUE was started by hand before `ue1` pointed at the container | `make attach-local-5g RESTART=1` |
| UE stops registering after a core rebuild | `--force-recreate` re-provisioned the subscriber without SD 1 | Set SST 1 / SD `000001` again in the WebUI ([1.4](#14-start-the-open5gs-core)) |
| `ric.sh up`: `fatal error: lfstack.push` while building `rtmgr_sim` | QEMU ≥ 9.0.1 runs the amd64 Go build and crashes | Rosetta or QEMU 8.1.x ([3.1](#31-arm64-hosts-only-run-amd64-correctly)) |
| `ric.sh`: `cannot write …/runs` | The containers created `runs/` as root | `sudo chown $USER: runs` |
| `ric.sh status` lists no node | gNB started before the RIC, the 60 s reconnect rule, or a wrong `e2:` address | Start the RIC first; wait 60 s and restart the gNB; `addr: 10.0.2.10`, `bind_addr: 10.0.2.1` |
| Monitor or xApp: `OSError: [Errno 98] Address already in use` | A leftover monitor or xApp holds its ports in the runner | Current `ric.sh` stops leftovers; otherwise `docker restart python_xapp_runner` |
| `WF_KPM_SILENT`; submgr `RICSubscriptionFailure … Cause:6, Value 0`; gNB `Action not supported` | A metric OCUDU does not serve, or style 5 with one UE | The defaults in [3.7](#37-what-ocudu-2610-needs-from-the-e2-path); test a list with `METRICS=… KPM_SECONDS=20 ric.sh kpm` |
| `WF_POLICY_NOT_APPLIED`; `control.csv` `failed,RIC_CONTROL_FAILURE` | No gNB slice matches PLMN 00101 / SST 1 / SD 1 | SD 1 on gNB, core and subscriber ([3.6](#36-ran-control-a-prb-quota)) |

Logs: gNB `/tmp/gnb.log`, srsUE `/tmp/ue.log`, core
`docker logs -f open5gs_5gc`, mec-cast `mec5g logs -f`, RIC
`docker logs ric_submgr` (subscriptions) and `ric.sh status`. Raise `all_level` to
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
- [OCUDU — Integrating a Near-RT RIC](https://docs.ocudu.org/tutorials/near-rt-ric/)
- [srsran/oran-sc-ric](https://github.com/srsran/oran-sc-ric) and [issue #31](https://github.com/srsran/oran-sc-ric/issues/31) (KPM style 5 needs two UE ids)
- [QEMU #2560 — Go GC crash under qemu-x86_64 on aarch64](https://gitlab.com/qemu-project/qemu/-/issues/2560)
- [Lima — Intel-on-ARM with Rosetta](https://lima-vm.io/docs/config/multi-arch)
- [ADR-0001 — Zenoh over DDS](../architecture/adr/0001-zenoh-over-dds.md) (why the UE dials out to the router)
