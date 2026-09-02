"""Runtime configuration, read from the environment or a local .env file."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Service settings. Every field maps to a ``MECADM_``-prefixed env var."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="MECADM_",
        extra="ignore",
    )

    # Where run manifests live. One directory per run, matching what
    # scripts/run-experiment.sh already writes.
    runs_dir: str = "runs"

    # Optional: post admin lifecycle events here so operator actions land on
    # the same timeline as the measurements. Empty disables it.
    logging_url: str = ""

    # Where the *operator's browser* reaches the logging service, which is not
    # the same address this service uses. `logging_url` is a container-network
    # name like http://logging:8000 and resolves nowhere outside the compose
    # network. Empty means "same host as this page, port 8000", which is right
    # locally and wrong in the lab, where logging lives on the infra host.
    #
    # A base URL with no path: the page appends /docs for the header link and
    # /api/v1/logs?trace_id=... for the per-run link.
    logging_public_url: str = ""

    # Keep-alive. These numbers are also compiled into both node clients, so
    # they are published in the welcome frame rather than assumed.
    keepalive_s: float = Field(default=10.0, gt=0)
    offline_timeout_s: float = Field(default=30.0, gt=0)

    # How long a run may sit in `starting` before it is declared failed.
    start_timeout_s: float = Field(default=30.0, gt=0)

    # How long a run may sit in `stopping` before the admin gives up waiting
    # for reports and calls it stopped. Generous next to start_timeout_s
    # because a node flushing a large recorder is doing real work and must not
    # be cut off; the run is already over either way, so the only cost of
    # waiting is the cell's slot. Without this a run whose participants are
    # online but will never report waits forever.
    stop_timeout_s: float = Field(default=120.0, gt=0)

    # How long a run may RECORD before the admin stops it. A forgotten run
    # writes until the disk is full: measured at the 5,000-point default a
    # renderer's session.rrd alone grows 4.6 GB/day, 24x faster than the CSVs
    # and the database put together. Four hours is comfortably longer than any
    # experiment run so far and far short of a weekend.
    #
    # This is a guard, not a policy: a 24-hour soak is a legitimate
    # experiment, so raise it for one, and 0 disables the stop entirely.
    # WF_RUN_TOO_LONG warns at 80% either way, which is the point at which a
    # deliberate long run can still be extended rather than cut off.
    max_run_duration_s: float = Field(default=14400.0, ge=0)

    # Free space on the runs volume, in GB. Below `start` a new run is
    # refused; below `abort` a recording run is stopped. The danger is not the
    # run: it is that on the infra host a full disk stops PostgreSQL writing,
    # which takes the measurement database and every role's telemetry sink
    # with it, and presents as "the logging service broke".
    #
    # abort < start on purpose. Refusing to start leaves room to investigate;
    # by the time the lower mark is hit, stopping is the last thing that can
    # still be done cleanly. 0 disables either check.
    # Sized against what a run actually costs -- ~5 GB for 24 h with one
    # renderer -- not against a big disk. A floor larger than the volume
    # blocks every run forever, which is worse than no guard at all.
    min_free_gb_start: float = Field(default=10.0, ge=0)
    min_free_gb_abort: float = Field(default=2.0, ge=0)

    # Cadence of the derived-diagnostics pass and of UI snapshot broadcasts.
    diagnostics_interval_s: float = Field(default=5.0, gt=0)
    ui_broadcast_min_interval_s: float = Field(default=1.0, gt=0)

    # Declared topology. A MISSING file is fine and yields the built-in role
    # rules — declaring the fleet is opt-in. The default is the container
    # path: both compose files mount deploy/lab read-only at /etc/mec-cast,
    # so dropping deploy/lab/topology.yml into the repo and restarting the
    # admin is all it takes. Running the service on the host instead? Point
    # MECADM_TOPOLOGY_PATH at the file directly.
    topology_path: str = "/etc/mec-cast/topology.yml"

    api_prefix: str = "/api/v1"
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings, loaded once."""
    return Settings()
