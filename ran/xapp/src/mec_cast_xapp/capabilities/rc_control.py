"""E2SM-RC control: a PRB quota for one UE, scoped to one run (ADR-0011).

``ran_policy`` in the run parameters (or a mid-run ``ran.policy`` command)::

    {"type": "prb_quota",
     "ue": 0,                       # E2 UE id (int, or "e2:0")
     "min_prb_ratio": 0,            # percent of the cell's PRBs
     "max_prb_ratio": 30,
     "dedicated_prb_ratio": 100,    # optional, srsRAN's default
     "schedule": [{"t_s": 30, "max_prb_ratio": 100}, ...]}   # optional

The rule this capability exists to keep: **a policy never outlives its run.**

* It is reverted (min 0, max 100) when the run stops, when the policy is
  replaced, and when the admin has been unreachable for ADMIN_LOSS_REVERT_S —
  an xApp nobody can tell to stop must stop on its own.
* A crash cannot revert anything. So the policy in force is written to
  ``<RUNS_DIR>/.xapp-policy.json`` while it is in force, and the next start
  reverts it before doing anything else. Between a crash and that restart the
  cap stays — said here rather than promised away.
* Every action and its outcome goes to ``ran-kpm/control.csv`` and the
  logging service: a control the data cannot see is a confound, not an
  experiment.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core import Capability, XappContext

log = logging.getLogger("mec_cast_xapp.rc")

DEFAULT = {"min_prb_ratio": 0, "max_prb_ratio": 100, "dedicated_prb_ratio": 100}
ADMIN_LOSS_REVERT_S = 30.0
#: How long run stop waits for outstanding control outcomes. Above the osc
#: adapter's 5 s ack timeout, below docker stop's 10 s grace.
STOP_ACK_WAIT_S = 6.0
MARKER = ".xapp-policy.json"


class PolicyError(ValueError):
    pass


def parse_policy(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a ran_policy; returns it normalised. Raises PolicyError."""
    if not isinstance(raw, dict):
        raise PolicyError("ran_policy must be an object")
    if raw.get("type", "prb_quota") != "prb_quota":
        raise PolicyError(f"unsupported ran_policy type {raw.get('type')!r} (prb_quota)")
    ue = raw.get("ue", 0)
    if isinstance(ue, str):
        ue = ue[3:] if ue.startswith("e2:") else ue
    try:
        ue = int(ue)
    except (TypeError, ValueError) as e:
        raise PolicyError(f"ran_policy.ue must be an E2 UE id, got {raw.get('ue')!r}") from e

    def ratios(d: Dict[str, Any], base: Dict[str, int]) -> Dict[str, int]:
        out = dict(base)
        for k in DEFAULT:
            if k in d:
                v = int(d[k])
                if not 0 <= v <= 100:
                    raise PolicyError(f"{k}={v} outside 0-100")
                out[k] = v
        if out["min_prb_ratio"] > out["max_prb_ratio"]:
            raise PolicyError(
                f"min_prb_ratio {out['min_prb_ratio']} > max_prb_ratio {out['max_prb_ratio']}"
            )
        return out

    first = ratios(raw, DEFAULT)
    schedule = []
    prev_t = -1.0
    current = first
    for step in raw.get("schedule") or []:
        t = float(step.get("t_s", -1))
        if t <= prev_t:
            raise PolicyError("schedule t_s must be increasing and >= 0")
        current = ratios(step, current)
        schedule.append(dict(current, t_s=t))
        prev_t = t
    return {"type": "prb_quota", "ue": ue, **first, "schedule": schedule}


class RcControl(Capability):
    name = "rc_control"

    def __init__(self) -> None:
        self.policy: Optional[Dict[str, Any]] = None
        self.node: Optional[str] = None
        self.applied_at = 0.0
        self.next_step = 0
        self.state = "none"  # none | pending | applied | failed | reverted
        self.last_error: Optional[str] = None
        self.sent = 0
        self.acked = 0
        self.failed = 0
        self._ctx: Optional[XappContext] = None
        self._stale: Optional[Dict[str, Any]] = None
        self._stale_read = False
        self._wrote_marker = False

    # --- the marker: what to undo after a crash ------------------------------

    @staticmethod
    def _marker(ctx: Optional[XappContext]) -> Path:
        return Path(os.environ.get("RUNS_DIR") or "runs") / MARKER

    def _write_marker(self, ctx: XappContext) -> None:
        try:
            path = self._marker(ctx)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._wrote_marker = True
            path.write_text(
                json.dumps(
                    {
                        "node": self.node,
                        "ue": self.policy["ue"],
                        "run_id": ctx.run_id,
                        "policy": self.policy,
                    }
                )
            )
        except OSError as e:
            log.warning("rc: cannot write %s: %s", MARKER, e)

    def _clear_marker(self, ctx: XappContext) -> None:
        try:
            self._marker(ctx).unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning("rc: cannot remove %s: %s", MARKER, e)

    def on_boot(self, port) -> bool:
        """A marker found at boot means the previous process died with a
        policy in force. Undo it before relying on anything else.

        The marker is read ONCE, the first time this runs — before any run of
        this process can have written its own — and that content is what gets
        reverted, however many retries it takes for the RIC to answer. The
        file is only removed while no policy of this process is in force: by
        then a marker on disk is this process's own, and must stay.
        """
        if not self._stale_read:
            self._stale_read = True
            path = self._marker(None)
            # A marker this process wrote is its own policy, not a leftover —
            # whatever order boot and the first run happened in.
            if path.exists() and not self._wrote_marker:
                try:
                    self._stale = json.loads(path.read_text())
                except (OSError, ValueError):
                    log.warning("rc: unreadable %s; removing it", path)
                    path.unlink(missing_ok=True)
        stale = self._stale
        if not stale or "ue" not in stale:
            self._stale = None
            return True
        nodes = port.connected_nodes()
        node = stale.get("node") if stale.get("node") in nodes else (nodes[0] if nodes else None)
        if node is None:
            return False  # nothing to send it to yet; try again next tick
        log.warning(
            "rc: the previous xApp left a policy in force (run %s, ue %s); reverting",
            stale.get("run_id"),
            stale["ue"],
        )
        try:
            port.control_prb_quota(
                node,
                int(stale["ue"]),
                DEFAULT["min_prb_ratio"],
                DEFAULT["max_prb_ratio"],
                DEFAULT["dedicated_prb_ratio"],
                lambda ok, d: log.warning(
                    "rc: revert-after-crash %s %s", "ack" if ok else "FAILED", d
                ),
            )
        except Exception as e:  # noqa: BLE001
            log.warning("rc: revert-after-crash not sent: %s", e)
            return False
        self._stale = None
        if self.policy is None:
            self._marker(None).unlink(missing_ok=True)
        return True

    # --- control ------------------------------------------------------------

    def _send(self, ctx: XappContext, node: str, ue: int, r: Dict[str, int], action: str) -> None:
        rec = ctx.recorder

        def done(ok: bool, detail: str) -> None:
            if ok:
                self.acked += 1
            else:
                self.failed += 1
                self.last_error = detail
            if action.startswith("apply") or action.startswith("step"):
                self.state = "applied" if ok else "failed"
            if rec is not None:
                rec.record_control(
                    node=node,
                    ue_id=ue,
                    action=action,
                    outcome="ack" if ok else "failed",
                    detail=detail,
                    **r,
                )

        self.sent += 1
        if rec is not None:
            rec.record_control(node=node, ue_id=ue, action=action, outcome="sent", **r)
        try:
            ctx.port.control_prb_quota(
                node, ue, r["min_prb_ratio"], r["max_prb_ratio"], r["dedicated_prb_ratio"], done
            )
        except Exception as e:  # noqa: BLE001 - the outcome is data
            done(False, f"send failed: {e}")

    def on_start(self, ctx: XappContext) -> None:
        self._ctx = ctx

    def on_policy(self, ctx: XappContext, policy: Dict[str, Any]) -> None:
        parsed = parse_policy(policy)  # raises: the admin acks the command not ok
        node = ctx.node()
        if node is None:
            self.state, self.last_error = "failed", "no E2 node connected"
            raise PolicyError(self.last_error)
        if self.policy is not None and self.policy["ue"] != parsed["ue"]:
            self._send(ctx, self.node, self.policy["ue"], DEFAULT, "revert-replaced")
        self.policy, self.node = parsed, node
        self.applied_at = time.monotonic()
        self.next_step = 0
        self.state = "pending"
        self._write_marker(ctx)
        self._send(ctx, node, parsed["ue"], {k: parsed[k] for k in DEFAULT}, "apply")

    def tick(self, ctx: XappContext) -> None:
        if self.policy is None:
            return
        sched: List[Dict[str, Any]] = self.policy["schedule"]
        elapsed = time.monotonic() - self.applied_at
        while self.next_step < len(sched) and elapsed >= sched[self.next_step]["t_s"]:
            step = sched[self.next_step]
            self.next_step += 1
            self._send(
                ctx,
                self.node,
                self.policy["ue"],
                {k: step[k] for k in DEFAULT},
                f"step@{step['t_s']:g}s",
            )
        if ctx.admin_lost_s() > ADMIN_LOSS_REVERT_S:
            log.warning(
                "rc: admin unreachable for %.0f s; reverting the policy", ctx.admin_lost_s()
            )
            self._revert(ctx, "revert-admin-lost")

    def _revert(self, ctx: XappContext, why: str) -> None:
        if self.policy is None:
            return
        self._send(ctx, self.node, self.policy["ue"], DEFAULT, why)
        self.policy = None
        self.state = "reverted"
        self._clear_marker(ctx)

    def on_stop(self, ctx: XappContext) -> None:
        self._revert(ctx, "revert-run-stop")
        # The recorder closes right after this returns. Wait (bounded) for the
        # revert's outcome first, or it arrives on the adapter's thread after
        # the file is shut and the run's record never shows the revert landed
        # — found by the sim test, where the ack lost that race on 3.8.
        deadline = time.monotonic() + STOP_ACK_WAIT_S
        while self.acked + self.failed < self.sent and time.monotonic() < deadline:
            time.sleep(0.02)
        if self.acked + self.failed < self.sent:
            log.warning(
                "rc: %d control outcome(s) still pending at run stop",
                self.sent - self.acked - self.failed,
            )
        self._ctx = None

    def status(self) -> Dict[str, Any]:
        return {
            "controls_sent": self.sent,
            "controls_acked": self.acked,
            "controls_failed": self.failed,
        }

    def params(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"policy_state": self.state, "policy_error": self.last_error}
        if self.policy is not None:
            out["policy"] = {k: self.policy[k] for k in ("ue", *DEFAULT)}
            out["policy_steps_left"] = len(self.policy["schedule"]) - self.next_step
        return out
