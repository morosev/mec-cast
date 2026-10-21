# ran/xapp/flexric — mec-cast-e2-bridge

The mec-cast xApp's E2 work on FlexRIC
([ADR-0012](../../../docs/architecture/adr/0012-two-near-rt-rics.md)).
FlexRIC's Python SDK has no E2SM-KPM or E2SM-RC, so this small FlexRIC xApp in
C does them, and the Python xApp's `flexric` adapter runs it as a child
process (`ran/xapp/src/mec_cast_xapp/adapters/flexric.py`).

**Protocol.** One JSON object per line, the JSON E2 feed protocol that
`gnb-sim` defines (`adapters/jsonfeed.py`): `nodes`, `subscribe` /
`subscribed` / `subscribe_failed`, `indication`, `unsubscribe`, `control` /
`control_ack`. Requests come in on stdin, answers go out on the original stdout.
FlexRIC's own prints go to stderr. End of stdin removes every subscription and
exits.

**What it builds.**
- **KPM report style 1** (node level), or **style 4**: every UE whose S-NSSAI
  has the requested SST, reported per UE. Each indication is decoded into the
  shape the normaliser reads (`measData`, or `ueMeasData` keyed by the UE's
  gNB-CU-UE F1AP id).
- **The slice-level PRB quota**: E2SM-RC Control Style 2, Action 6. It carries
  the RAN parameter tree srsRAN/OCUDU parse (ids 1–13), with the UE as a
  gNB-DU UE id and the slice (PLMN, SST, SD) from the request.

**FlexRIC v3.0.0 behaviours it works around.**
- **Refused controls look successful.** `control_sm_xapp_api()` returns
  success whether the gNB acknowledged or refused. The bridge reads FlexRIC's
  stdout through a pipe and takes `CONTROL ACK rx` or `CONTROL FAILURE rx`
  from it.
- **A refused subscription aborts the process.** The bridge checks the style
  and every metric against the RAN function definition the gNB advertised,
  and answers `subscribe_failed` with the reason instead of sending. If FlexRIC
  aborts anyway, the adapter reports it as a refusal.
- **Report callbacks carry no context.** Each subscription takes one of 16 slot
  callbacks.

**Build.** It is one more target of FlexRIC's CMake build, because it links
FlexRIC's static `e42_xapp` with its ASN.1 dependencies, under the same
`E2AP_VERSION` and `KPM_VERSION`. `deploy/lab/ric/flexric/Dockerfile` adds
this directory to the FlexRIC checkout's build, and `flexric.sh up` builds the
image. `FLEXRIC_REBUILD=1 flexric.sh up` rebuilds it after a change here.

**Test.** `tests/test_flexric.py` runs the adapter against a fake bridge
(`tests/fake_bridge.py`). The real bridge runs against FlexRIC's emulated gNB
with `flexric.sh up`, then `flexric.sh emu`, then `flexric.sh kpm`.

It links to FlexRIC (OAI CSSL v1.0) and contains none of its code.
