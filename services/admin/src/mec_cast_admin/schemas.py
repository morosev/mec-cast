"""Request and response models for the operator API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


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
        "num_points, rate_hz, seed, pattern, reliability, qos_depth.",
    )


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
