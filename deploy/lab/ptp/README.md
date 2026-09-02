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
