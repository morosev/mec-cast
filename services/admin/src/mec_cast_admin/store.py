"""Run identity and persistence.

Runs are stored as ``runs/<run_id>/run.json`` — the same manifest
``scripts/run-experiment.sh`` already writes — plus an append-only journal at
``runs/admin-journal.jsonl`` recording every state transition. On startup the
admin globs the manifests and replays the journal tail, so the table survives a
restart without a database. See ADR-0008.

Two writers therefore share ``run.json``. The ``source`` field names which one,
and the fields the admin adds are strictly additive: a manifest written by the
script still loads here, and a manifest written here still satisfies anything
that reads the script's shape.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from .state import RunState

logger = logging.getLogger(__name__)

JOURNAL_NAME = "admin-journal.jsonl"
MANIFEST_NAME = "run.json"


_uuid7_lock = threading.Lock()
_uuid7_last_ms = 0
_uuid7_counter = 0


def uuid7() -> str:
    """A UUIDv7 in canonical hyphenated form (RFC 9562 §5.7).

    Time-ordered, so runs sort chronologically in the table, in ``ls runs/``
    and in Postgres — which UUIDv4 does not. Still a UUID, so
    ``run_trace_id()`` and the 16-byte envelope ``trace_id`` are unaffected.
    Python 3.12 has no ``uuid.uuid7``; this is the dozen lines that would be.

    The 12-bit ``rand_a`` field carries a counter rather than randomness
    (RFC 9562 §6.2, "fixed-length dedicated counter"). Without it two ids
    minted in the same millisecond sort arbitrarily, and the ordering promise
    would hold only at millisecond granularity — true for a human pressing a
    button, false for anything programmatic, and a guarantee that is only
    usually true is worse than none.
    """
    global _uuid7_last_ms, _uuid7_counter

    with _uuid7_lock:
        ts_ms = time.time_ns() // 1_000_000
        if ts_ms == _uuid7_last_ms:
            _uuid7_counter += 1
            if _uuid7_counter > 0x0FFF:
                # 4096 ids in one millisecond. Borrow from the next.
                ts_ms += 1
                _uuid7_last_ms = ts_ms
                _uuid7_counter = 0
        else:
            if ts_ms < _uuid7_last_ms:
                # The wall clock went backwards. Keep issuing ordered ids.
                ts_ms = _uuid7_last_ms
            _uuid7_last_ms = ts_ms
            _uuid7_counter = 0
        counter = _uuid7_counter

    raw = bytearray(ts_ms.to_bytes(6, "big") + os.urandom(10))
    raw[6] = 0x70 | (counter >> 8)  # version 7 | counter high nibble
    raw[7] = counter & 0xFF  # counter low byte
    raw[8] = (raw[8] & 0x3F) | 0x80  # variant 10xx
    return str(uuid.UUID(bytes=bytes(raw)))


def utc_now() -> str:
    """Second-resolution UTC, matching the format run-experiment.sh writes."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


#: Kept as a module-private alias for readability at call sites inside store.
_utc_now = utc_now


@dataclass
class Run:
    """One measurement run, as the admin sees it."""

    run_id: str
    seq: int
    label: str = ""
    state: RunState = RunState.DRAFT
    source: str = "admin"

    created_utc: str = field(default_factory=_utc_now)
    started_utc: str | None = None
    stopped_utc: str | None = None

    #: Workload knobs carried into `run.start`. Same names the nodes' ROS
    #: parameters and the script's `workload` block use.
    params: dict[str, Any] = field(default_factory=dict)

    #: node_id -> {role, host, version_sha}, snapshotted when the run starts.
    participants: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: site -> {host, path}. Which machine holds which CSV directory — in the
    #: lab that is not knowable from the manifest alone today.
    sites: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: node_id -> the recorder report from that node's goodbye.
    reports: dict[str, dict[str, Any]] = field(default_factory=dict)
    findings: list[dict[str, Any]] = field(default_factory=list)
    removed: bool = False

    def to_manifest(self) -> dict[str, Any]:
        """The on-disk shape, compatible with the experiment script's."""
        return {
            "run_id": self.run_id,
            "tag": self.label,
            "started_utc": self.started_utc or self.created_utc,
            "host": os.uname().nodename,
            "workload": {
                "num_points": self.params.get("num_points"),
                "rate_hz": self.params.get("rate_hz"),
                "seed": self.params.get("seed"),
                "modality": "pointcloud",
            },
            "transport": "rmw_zenoh_cpp",
            # --- admin additions, all optional to older readers ---
            "seq": self.seq,
            "label": self.label,
            "source": self.source,
            "state": str(self.state),
            "created_utc": self.created_utc,
            "stopped_utc": self.stopped_utc,
            "params": self.params,
            "participants": self.participants,
            "sites": self.sites,
            "reports": self.reports,
            "findings": self.findings,
            "removed": self.removed,
        }

    @classmethod
    def from_manifest(cls, data: dict[str, Any]) -> Run:
        """Load a manifest written by either writer.

        A manifest from ``run-experiment.sh`` has no ``state`` or ``seq``; it
        describes a run that already happened, so it loads as ``stopped``.
        """
        run_id = data["run_id"]
        source = data.get("source", "script")
        raw_state = data.get("state")
        state = RunState(raw_state) if raw_state else RunState.STOPPED
        params = data.get("params")
        if not params:
            workload = data.get("workload") or {}
            params = {k: v for k, v in workload.items() if v is not None}
        return cls(
            run_id=run_id,
            seq=int(data.get("seq", 0)),
            label=data.get("label") or data.get("tag") or "",
            state=state,
            source=source,
            created_utc=data.get("created_utc") or data.get("started_utc") or _utc_now(),
            started_utc=data.get("started_utc"),
            stopped_utc=data.get("stopped_utc"),
            params=params,
            participants=data.get("participants") or {},
            sites=data.get("sites") or {},
            reports=data.get("reports") or {},
            findings=data.get("findings") or [],
            removed=bool(data.get("removed", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["state"] = str(self.state)
        return d


class RunStore(Protocol):
    """The seam a database would slot into. ``JsonRunStore`` is the only
    implementation; SQLite is one class away if run counts ever justify it."""

    def load(self) -> dict[str, Run]: ...
    def save(self, run: Run) -> None: ...
    def journal(self, run_id: str, event: str, detail: dict[str, Any]) -> None: ...


class JsonRunStore:
    """Manifests on disk, plus an append-only transition journal."""

    def __init__(self, runs_dir: str | Path) -> None:
        self.runs_dir = Path(runs_dir)

    @property
    def journal_path(self) -> Path:
        return self.runs_dir / JOURNAL_NAME

    def load(self) -> dict[str, Run]:
        """Every manifest under ``runs/``, newest first by id (UUIDv7 sorts).

        A directory without a readable manifest is skipped with a warning: a
        run whose CSVs exist but whose manifest is truncated should not stop
        the service from starting.
        """
        runs: dict[str, Run] = {}
        if not self.runs_dir.is_dir():
            return runs

        for manifest in sorted(self.runs_dir.glob(f"*/{MANIFEST_NAME}")):
            try:
                runs[manifest.parent.name] = Run.from_manifest(
                    json.loads(manifest.read_text(encoding="utf-8"))
                )
            except (OSError, ValueError, KeyError) as exc:
                logger.warning("skipping unreadable manifest %s: %s", manifest, exc)

        self._replay_journal(runs)
        return runs

    def _replay_journal(self, runs: dict[str, Run]) -> None:
        """Apply transitions the journal recorded after the last manifest write.

        The manifest is rewritten on every transition, so the journal is
        normally redundant. It matters when the process died between the two.
        """
        if not self.journal_path.exists():
            return
        try:
            lines = self.journal_path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            logger.warning("cannot read journal: %s", exc)
            return

        for number, line in enumerate(lines, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                # A half-written final line is expected after a hard kill.
                logger.warning("skipping malformed journal line %d", number)
                continue
            run = runs.get(entry.get("run_id", ""))
            state = entry.get("to")
            if run is None or not state:
                continue
            try:
                run.state = RunState(state)
            except ValueError:
                logger.warning("skipping unknown state %r in journal line %d", state, number)

    def save(self, run: Run) -> None:
        """Write the manifest atomically: a reader never sees a half-file."""
        directory = self.runs_dir / run.run_id
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / MANIFEST_NAME
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(run.to_manifest(), indent=2) + "\n", encoding="utf-8")
        tmp.replace(target)

    def journal(self, run_id: str, event: str, detail: dict[str, Any]) -> None:
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        entry = {"ts": _utc_now(), "run_id": run_id, "event": event, **detail}
        with self.journal_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
