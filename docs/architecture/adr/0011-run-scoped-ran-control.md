# ADR-0011: RAN control is run-scoped, recorded, and always reverted

- **Status:** Accepted
- **Date:** 2026-10-10

## Context

ADR-0010 added an E2 xApp, and with E2SM-RC the platform can change the
scheduler under a measurement: today, a slice-level PRB quota for one UE.
That is the point — PRB reservation for the LiDAR UE is the first control
experiment — but it changes the nature of the platform. Until now nothing it
did could alter the radio it measures. Now a forgotten setting can.

Three things can go wrong, and none of them looks wrong in the data:

- **A policy outlives its run.** The next run, perhaps someone else's, is
  measured under a cap nobody asked for. Its CSVs look normal.
- **A run is labelled with a policy that is not in force.** The RIC refused
  it, the PLMN or slice did not match, or the ack never came. The run is
  compared as "capped" when it was not.
- **A control happens and the data cannot see when.** The effect is in the
  latency, and the cause is nowhere.

## Decision

1. **A policy belongs to a run.** It travels as `ran_policy` in the run's
   parameters, or as the `ran.policy` command mid-run. There is no standing
   policy and no out-of-band "set it and leave it".
2. **It is always reverted** to the gNB default (min 0 %, max 100 %):
   - when the run stops;
   - when the supervisor fails the run — the admin sends `run.stop` to the
     run's members for that too;
   - when the policy is replaced;
   - when the admin has been unreachable for 30 s (an xApp nobody can stop
     stops itself).
3. **A crash is handled honestly.** The policy in force is written to
   `<RUNS_DIR>/.xapp-policy.json`, and the next xApp process reverts it before
   anything else, retrying until the RIC answers. Between the crash and that
   restart the cap stays in force. This is stated here, not promised away.
4. **Every action is data.**
   - `runs/<id>/ran-kpm/control.csv` records sent, ack and failed, with the
     ratios and the RIC's answer, on the same clock as every measurement.
   - The same records go to the logging service.
   - `run.json` names the policy that was asked for.
5. **"Asked for" and "in force" are kept apart.**
   - The admin validates a policy at the door: a malformed one is a 422, not
     a failed ack.
   - The xApp's `policy_state` reports what actually happened.
   - `WF_POLICY_NOT_APPLIED` is an **error**, because the run's data is not
     under the policy it names.

## Rationale

- **Not a standing policy on the RIC, configured separately:** that is
  exactly the first failure above. It has no owner and no end.
- **Not "revert on the next start" alone:** a lab session ends without a next
  start. Reverting on stop is the normal path. The marker is only the backstop
  for a crash.
- **Not an assumption that a sent control took effect:** the RIC can refuse
  it. oran-sc-ric's PRB-quota encoder hard-codes PLMN 00101 / SST 1 / SD 1, so
  a lab slice that differs is rejected. Only an acknowledgement counts.
- **Why the effect is checked in the other source:** the JSON tap sees the
  capped throughput independently of the xApp that set it. In the local
  end-to-end test, a 25 % cap reads 0.252 in KPM and 0.250 in the JSON tap.
  A control that only its own source can see is not established.

## Consequences

- `ran-kpm/control.csv` is part of a run's provenance and is analysed with it.
- The admin can now stop nodes on its own initiative (a failed run). That was
  harmless before RAN control; after it, it was required.
- A crash between apply and revert leaves the cap until the xApp restarts.
  That is the residual risk. `deploy/lab/ric/ric.sh xapp` restarting the xApp
  is the mitigation, and an operator should know it.
- Further controls (handover, other RC actions, closed loops) are new
  capabilities under the same rules, not exceptions to them.
