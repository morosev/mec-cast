# ADR-0012: Two near-RT RICs behind one E2Port: oran-sc-ric and FlexRIC

- **Status:** Accepted
- **Date:** 2026-10-21

## Context

ADR-0010 put the E2 xApp on the O-RAN SC near-RT RIC (srsRAN's `oran-sc-ric`),
and it now runs end to end against OCUDU: KPM recorded, a PRB quota
acknowledged. The lab also wants FlexRIC (OpenAirInterface, EURECOM): it is
the other near-RT RIC OCUDU documents, its deployment is simpler (one native
process, no RMR routing table), and it runs natively on arm64.

Two facts about FlexRIC v3.0.0 decide how it can be used:

- **Its Python SDK has no E2SM-KPM or E2SM-RC.** The SWIG wrapper exposes only
  FlexRIC's own service models (MAC, RLC, PDCP, GTP, its slicing), which only
  the OAI gNB implements. OCUDU speaks the standard KPM and RC. Both are in
  FlexRIC's C SDK alone.
- **Its C SDK hides two answers.** `control_sm_xapp_api()` returns success
  whether the E2 node acknowledged the control or refused it; the only trace
  is a printf. And a RIC Subscription Failure aborts the xApp process: the
  handler is an unimplemented assert.

## Decision

1. **Both RICs, side by side, behind the existing `E2Port` seam.** The xApp's
   capabilities, recording, normaliser, admin client and revert rules are
   unchanged. Only the adapter differs: `E2_ADAPTER=osc` for oran-sc-ric,
   `E2_ADAPTER=flexric` for FlexRIC.
2. **A C bridge does FlexRIC's E2 work.** `mec-cast-e2-bridge`
   (`ran/xapp/flexric/`) is a small FlexRIC xApp that speaks, on its stdin and
   stdout, the JSON E2 feed protocol `gnb-sim` already defines. The Python
   adapter runs it as a child process. The protocol is shared, so the `sim`
   and `flexric` adapters share their protocol code (`adapters/jsonfeed.py`).
3. **The bridge works around FlexRIC rather than patching it.**
   - FlexRIC's stdout is a pipe the bridge reads: it forwards FlexRIC's prints
     to stderr and takes the control acknowledge or failure from them. The
     JSON protocol runs on a duplicate of the original stdout.
   - A subscription is checked first against the report styles and metric
     names the E2 node advertised in its RAN function definition, so the usual
     refusal (an unsupported metric) is answered without asking. An abort
     right after a subscribe is reported to the xApp as a refusal, so it backs
     off instead of resubscribing into the same crash.
4. **KPM report style 4 on FlexRIC.** Per UE, every UE whose S-NSSAI has the
   slice's SST; style 2 (one named UE), oran-sc-ric's default, is not built by
   the bridge. Rows land as `ue=e2:<id>` exactly as on oran-sc-ric.
5. **The PRB quota is built from the RC parameter tree** srsRAN/OCUDU parses
   (E2SM-RC Control Style 2, Action 6, RAN parameters 1–13), so the slice it
   names is a parameter (`FLEXRIC_SLICE`, default PLMN 00101 / SST 1 / SD 1)
   instead of oran-sc-ric's hard-coded one.
6. **One RIC per host at a time.** Both listen on E2 SCTP 36421. Each script
   (`deploy/lab/ric/oran-sc-ric.sh`, `deploy/lab/ric/flexric.sh`) refuses to
   start while the other runs.
7. **FlexRIC is pinned and built, never vendored**: tag `v3.0.0`
   (`18c3325`), built inside the `mec-cast-flexric` image with `E2AP_V3`
   (srsRAN/OCUDU implement E2AP v03.00) and `KPM_V3_00` (what OCUDU's FlexRIC
   tutorial builds). It is under the OAI CSSL v1.0: copyright Apache-style,
   patents royalty-free for study, testing and research, FRAND otherwise. The
   bridge links to FlexRIC and contains none of its code, which the licence
   excludes from Derivative Works.

## Rationale

- **Why not FlexRIC's Python SDK:** it cannot talk to OCUDU at all, so it is
  not a shorter path but no path.
- **Why a child process and not a Python extension:** the bridge's protocol
  already exists (gnb-sim's E2 feed), so it is testable with a fake bridge in
  CI and with FlexRIC's emulated gNB locally; a crash in FlexRIC (its abort on
  a refused subscription) takes down the bridge, not the xApp, which restarts
  it and resubscribes.
- **Why keep oran-sc-ric:** it is the path verified against OCUDU, and
  FlexRIC is not yet. Two RICs behind one seam also keep the xApp honest about
  what is RIC-specific.

## Consequences

- **What is verified, and where.** Locally, against FlexRIC's emulated gNB
  (`flexric.sh emu`): E2 setup, KPM style 4 indications normalised into
  `ran-kpm/kpi.csv` and the logging service, an unsupported metric and style 2
  refused cleanly, and a PRB quota applied, stepped and reverted, each
  acknowledged. Not verified until the lab gNB: what OCUDU accepts of the
  bridge's encoding — style 4 with an S-NSSAI condition, the UE id form, the
  RC parameter tree — and whether its E2 setup advertises the five metrics
  under style 4.
- **Style 4 reports every UE in the slice.** A run's KPM rows can carry UEs
  other than the LiDAR UE; analysis picks the UE (`ran_join.py --ue`), as it
  does for the JSON tap.
- **A bridge change rebuilds FlexRIC.** The bridge is a target of FlexRIC's own
  build (same flags, the static `e42_xapp` with its ASN.1 dependencies), so
  the image is rebuilt with `FLEXRIC_REBUILD=1 flexric.sh up`.
- **FlexRIC's quirks are pinned to v3.0.0.** A newer FlexRIC that reports
  control failures or handles subscription failures would let the bridge drop
  its workarounds; bumping the pin is a deliberate change, tested with
  `flexric.sh emu` first.
