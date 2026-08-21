"""Shared FastAPI dependencies."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from .config import Settings
from .orchestrator import Orchestrator


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_orchestrator(request: Request) -> Orchestrator:
    return request.app.state.orchestrator


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
OrchestratorDep = Annotated[Orchestrator, Depends(get_orchestrator)]
