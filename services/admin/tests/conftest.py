"""Shared fixtures.

Unlike the logging service, nothing here touches a database and nothing is
destructive: the store writes under ``tmp_path`` and the service holds its
state in memory. There is no equivalent of that service's TRUNCATE fixture.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mec_cast_admin.app import create_app
from mec_cast_admin.config import Settings


@pytest.fixture
def settings(tmp_path) -> Settings:
    # Timers are wound right down: the tests drive the passes themselves
    # rather than sleeping through production cadences.
    return Settings(
        runs_dir=str(tmp_path),
        keepalive_s=0.05,
        offline_timeout_s=0.5,
        start_timeout_s=0.5,
        diagnostics_interval_s=0.05,
        ui_broadcast_min_interval_s=0.01,
    )


@pytest.fixture
def client(settings: Settings):
    with TestClient(create_app(settings)) as test_client:
        yield test_client
