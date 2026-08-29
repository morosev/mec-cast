"""Measurement-site codes: which point in the pipeline produced a sample.

These were three unrelated module constants in three node files, with nothing
asserting they stay distinct. One home, imported by every node.

The recorder treats ``site`` as an opaque ``u8`` tag (telemetry
``recorder.rs``); the *meaning* is fixed here and in ``docs/_facts.yml``'s
``outputs.sites`` table — change either only with the other.

The gNB collector (``ran/collector``) is deliberately absent: it records KPIs,
not per-frame samples, and does not participate in this numbering.
"""

#: LiDAR client, on the UE. First stamp of a frame's life (`capture_ns`).
SITE_PUBLISHER = 0
#: MEC edge. Receives the uplink, processes, optionally republishes.
SITE_EDGE = 1
#: Renderer, on the UE. Its ``e2e_ns`` is a ROUND TRIP — PTP-free only while
#: the paired lidar runs on the same host (ADR-0009).
SITE_RENDER = 2

#: Directory leaf per site, before the instance suffix. The recorder writes
#: ``runs/<RUN_ID>/<leaf>-<instance>/samples.csv``.
SITE_LEAVES = {SITE_PUBLISHER: "pub", SITE_EDGE: "edge", SITE_RENDER: "render"}
