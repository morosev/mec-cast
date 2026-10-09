"""Request and response models for the operator API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .ranpolicy import validate_ran_policy


class HealthResponse(BaseModel):
    status: str
    version: str
    protocol: int


class RunCreate(BaseModel):
    """What an operator fills in behind the Add button."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(default="", max_length=200, description="Free-text tag for the run.")
    cell: str = Field(
        default="default",
        max_length=64,
        description="Which cell this run covers. Each cell runs at most one "
        "run at a time; a deployment that has not declared a topology has "
        "exactly one cell and can leave this alone.",
    )
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="Workload knobs carried to the nodes with run.start: "
        "num_points, rate_hz, seed, pattern, reliability, qos_depth; and for the "
        "E2 xApp, kpm_style, kpm_metrics, kpm_ue_ids, e2_node_id and ran_policy "
        "(ADR-0011).",
    )

    @field_validator("params")
    @classmethod
    def _ran_policy(cls, params: dict[str, Any]) -> dict[str, Any]:
        # Refused here, at create, rather than as a failed ack mid-run.
        if params.get("ran_policy") is not None:
            params = dict(params, ran_policy=validate_ran_policy(params["ran_policy"]))
        return params


class RanPolicyUpdate(BaseModel):
    """A mid-run RAN control policy for the run's xApp (ADR-0011)."""

    model_config = ConfigDict(extra="forbid")

    ran_policy: dict[str, Any]

    @field_validator("ran_policy")
    @classmethod
    def _valid(cls, v: dict[str, Any]) -> dict[str, Any]:
        return validate_ran_policy(v)


class RunView(BaseModel):
    """One row of the run table. ``allowed`` drives the buttons."""

    model_config = ConfigDict(extra="allow")

    run_id: str
    seq: int
    label: str
    state: str
    allowed: list[str]


class StateResponse(BaseModel):
    """The whole view, as pushed over /ws/ui and served for polling fallback."""

    model_config = ConfigDict(extra="allow")

    server_version: str
    protocol: int
    active_run_id: str | None
    runs: list[dict[str, Any]]
    nodes: list[dict[str, Any]]
    findings: list[dict[str, Any]]
