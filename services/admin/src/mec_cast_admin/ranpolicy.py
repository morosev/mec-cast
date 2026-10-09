"""The ``ran_policy`` run parameter: validated here, before it reaches a RIC.

A run may carry a RAN control policy (ADR-0011) — today one kind, an
E2SM-RC slice-level PRB quota for one UE, optionally changing on a schedule.
The xApp validates it again (``ran/xapp``, ``rc_control.parse_policy``) and is
the authority on applying it; the admin checks it at the door so a malformed
policy is a 422 on create, not a failed ack mid-run.

The two checks must agree. ``tests/test_ranpolicy.py`` holds the cases both
sides must refuse; the xApp's ``tests/test_core.py`` holds the same list.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class PolicyStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    t_s: float = Field(ge=0, description="Seconds after the policy was applied.")
    min_prb_ratio: int | None = Field(default=None, ge=0, le=100)
    max_prb_ratio: int | None = Field(default=None, ge=0, le=100)
    dedicated_prb_ratio: int | None = Field(default=None, ge=0, le=100)


class RanPolicy(BaseModel):
    """E2SM-RC Control Style 2 Action 6 — percent of the cell's PRBs."""

    model_config = ConfigDict(extra="forbid")

    type: str = "prb_quota"
    ue: int = Field(default=0, ge=0, description="E2 UE id (gNB-CU-UE-F1AP-ID).")
    min_prb_ratio: int = Field(default=0, ge=0, le=100)
    max_prb_ratio: int = Field(default=100, ge=0, le=100)
    dedicated_prb_ratio: int = Field(default=100, ge=0, le=100)
    schedule: list[PolicyStep] = Field(default_factory=list)

    @field_validator("type")
    @classmethod
    def _known_type(cls, v: str) -> str:
        if v != "prb_quota":
            raise ValueError(f"unsupported ran_policy type {v!r} (prb_quota)")
        return v

    @field_validator("ue", mode="before")
    @classmethod
    def _e2_prefix(cls, v: Any) -> Any:
        if isinstance(v, str) and v.startswith("e2:"):
            return v[3:]
        return v

    @model_validator(mode="after")
    def _consistent(self) -> RanPolicy:
        lo, hi = self.min_prb_ratio, self.max_prb_ratio
        if lo > hi:
            raise ValueError(f"min_prb_ratio {lo} > max_prb_ratio {hi}")
        prev = -1.0
        for step in self.schedule:
            if step.t_s <= prev:
                raise ValueError("schedule t_s must be strictly increasing")
            prev = step.t_s
            lo = step.min_prb_ratio if step.min_prb_ratio is not None else lo
            hi = step.max_prb_ratio if step.max_prb_ratio is not None else hi
            if lo > hi:
                raise ValueError(f"schedule step at {step.t_s} s: min {lo} > max {hi}")
        return self


def validate_ran_policy(raw: Any) -> dict[str, Any]:
    """Return the policy as the xApp will read it, or raise ValueError."""
    if not isinstance(raw, dict):
        raise ValueError("ran_policy must be an object")
    return RanPolicy.model_validate(raw).model_dump(exclude_none=True)
