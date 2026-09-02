# PTP setup for the lab

Cross-host one-way latency is only meaningful if the endpoints share a clock.
These units discipline `CLOCK_REALTIME` on each host from a PTP grandmaster.

**The sync runs on the management/backhaul LAN, never the 5G user plane.**
srsRAN + Open5GS do not implement 5G-TSN (DS-TT/NW-TT), so the 5G path
cannot carry time sync — see [ADR-0003](../../../docs/architecture/adr/0003-ptp-on-management-lan.md).

## Install (on every measuring host: UE, edge, gNB)

```bash
sudo apt install linuxptp
sudo cp deploy/lab/ptp/ptp4l.service deploy/lab/ptp/phc2sys.service /etc/systemd/system/
sudo cp deploy/lab/ptp/ptp4l.conf /etc/linuxptp/
# Set PTP_IFACE to the management NIC (must support hardware timestamping)
sudo systemctl edit ptp4l   # add: Environment=PTP_IFACE=eth1
sudo systemctl daemon-reload
sudo systemctl enable --now ptp4l phc2sys
```

## Verify before every measurement campaign

```bash
bash deploy/lab/ptp/verify-ptp.sh
```

Run it on **both** endpoints. It exits non-zero when the offset exceeds
1 µs. The telemetry layer independently records `ptp.reliable` in every
snapshot, so a run made without sync is identifiable after the fact — but
catching it beforehand saves the campaign.

### If your host is set up differently

The units above are one way to get a disciplined clock, not the only way, and
the check accepts any of them because what matters is the outcome:

- **`ptp4l@<iface>.service`** — a templated unit, one instance per interface,
  which is how a host with more than one candidate NIC will normally run it.
- **chrony with a PHC refclock** instead of `phc2sys`. A host already running
  chrony for its other time sources will usually take this route. The check
  requires the PHC to be the *selected* source, which is the distinction that
  matters: chrony happily lists a refclock it has rejected.

  ```bash
  chronyc sources -v | grep PHC     # want a leading `#*`, not `#?` or `#-`
  ```

  `#*` means selected. A `^x` line against your NTP server alongside it is not
  a fault — it is chrony marking that server a falseticker, which is what you
  want when it disagrees with PTP by seconds. It is worth chasing anyway,
  because the *other* hosts taking their time from it are then seconds out.

## Which /dev/ptpN — this is not a formality

`/dev/ptp0` is whichever NIC the driver registered first. On a multi-NIC host
it is routinely **not** the clock `ptp4l` disciplines, and nothing warns you.
Find the right one:

```bash
ethtool -T <iface> | grep 'PTP Hardware Clock'    # the index for that NIC
for d in /sys/class/ptp/ptp*; do echo "$d -> $(cat $d/clock_name)"; done
```

Then set it per host in that host's `.run-env`, which the containers read:

```bash
PTP_DEVICE=/dev/ptp2
```

The compose files map it to `/dev/ptp0` *inside* the container, so only this
one line is host-specific. `verify-ptp.sh` honours `PTP_DEVICE`, and also
resolves it from `PTP_IFACE` via `ethtool` when only that is set.

Three things must name the **same** device, and each one is set separately:

| What | Where |
|---|---|
| `ptp4l` disciplines it | `PTP_IFACE` in the ptp4l unit |
| chrony takes system time from it | `refclock PHC /dev/ptpN` in `chrony.conf` |
| the recorder judges clock health by it | `PTP_DEVICE` in `.run-env` |

A real example of getting this wrong, which cost a day: `ptp4l` on `ens3f0`
(`/dev/ptp2`, locked to −11 ns), chrony pointed at `/dev/ptp3` (`ens3f1`,
disciplined by nothing), containers handed `/dev/ptp0` (a third NIC). Result:
**11.15 s** of skew, and every indicator green — chrony reported nanoseconds
because it was measuring the system clock against the clock it was slaving it
to. Note also that chrony's `refid` is a free text label: that config read
`refid PHC0` while naming `/dev/ptp3`.

`verify-ptp.sh` now compares the disciplined PHC against `CLOCK_REALTIME` and
fails on a gap over 1 ms, which catches exactly this.

## The check that actually matters

Every check above is **local**: does this host track its own PHC? Two hosts can
each pass perfectly and still be seconds apart, because passing says nothing
about *which clock* they are tracking. Compare the two ends directly:

Run it **on one endpoint, naming the other**. It needs key-based ssh from the
host you are typing on to the peer:

```bash
# on the UE host, about the edge
bash deploy/lab/ptp/verify-ptp.sh --peer streaming-server
```

Naming the host you are standing on is rejected — a host cannot disagree with
itself, so that comparison passes vacuously or fails on a missing loopback key
and looks like a sync fault either way.

It reads the peer's clock over ssh and reports the gap with its own error bar.
The round trip is milliseconds, so this cannot validate PTP-grade sync — it is
aimed at the failure that actually occurs, where two endpoints are seconds
apart and every local indicator is green.

## Measuring hosts that are VMs

A guest with `ptp_kvm` has a real `/dev/ptp0` fed by its hypervisor, and chrony
can discipline from it to tens of nanoseconds. There is no `ptp4l`, correctly:
nothing here speaks PTP on the wire, the guest simply reads the host's clock.

**That clock is only as good as the hypervisor's.** The guest inherits the
hypervisor's error in full and cannot detect it — `chronyc tracking` will
report stratum 1, single-digit-nanosecond offsets and an RMS in the tens of ns
no matter how wrong the hypervisor is, because the guest is measuring itself
against the thing that is wrong.

So a `ptp4l` host locked to the lab grandmaster and a Proxmox guest whose host
runs plain NTP are two islands of internally perfect time, offset by whatever
their roots disagree by, with `ptp.reliable: true` on both. One-way delays
between them measure that offset rather than the network, and produce negative
values when the receiver's root is behind the sender's.

**Fix it at the hypervisor.** Sync the Proxmox host to the same grandmaster as
the bare-metal measuring hosts; the guest then inherits correct time through
`ptp_kvm` with no NIC passthrough. Passing a PTP-capable NIC into the guest and
running `ptp4l` there works too, and is more effort for the same result. Either
way, confirm with `--peer` afterwards rather than trusting the local numbers,
which looked flawless the whole time.

## When the check disagrees with reality

Trust `ptp4l`'s own output over any wrapper. This is a healthy host:

```
ptp4l[...]: master offset        -73 s2 freq  -21947 path delay       368
```

`s2` is the locked servo state and the offset is in nanoseconds; tens of ns
with a stable path delay is a correctly synchronised client. If the script
disagrees with that, the script is wrong — report it rather than
reconfiguring a working host.

## Checking NIC capability

```bash
ethtool -T eth1 | grep -A3 'Hardware Transmit'
```

You need `hardware-transmit`, `hardware-receive`, and `hardware-raw-clock`.
Without them `ptp4l` falls back to software timestamping (1–10 µs instead
of 10–100 ns), which is still usable but should be recorded in the run notes.
