"""Where a run's E2 data lands: ``runs/<run_id>/ran-kpm/`` and the logging service.

* ``kpi.csv`` — normalised rows, the same schema as the JSON tap's
  ``ran/kpi.csv`` (ran/schema/metrics.md), so analysis joins either.
* ``indications.jsonl`` — every decoded indication verbatim: the raw record,
  re-normalisable after the fact, and the fixture source for tests.
* ``control.csv`` — every control action and its outcome, on the same clock
  as the measurements it changes. A control nobody can see in the data is a
  confound, not an experiment.

Logging entries go to ``service=mec-cast-ran-kpm``, ``trace_id=run_id``, with
the raw indication as ``context.kpi`` and the rows as ``context.norm`` — the
shape the collector uses for ``mec-cast-ran``. Posting is batched on a thread
and drops rather than blocks: the control plane of a measurement must never
stall it.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from mec_cast_ran import KPI_CSV_HEADER, normalise_kpm

log = logging.getLogger("mec_cast_xapp.recording")

LEAF = "ran-kpm"
SERVICE = "mec-cast-ran-kpm"
CONTROL_HEADER = (
    "ts_ns",
    "node",
    "ue_id",
    "action",
    "min_prb_ratio",
    "max_prb_ratio",
    "dedicated_prb_ratio",
    "outcome",
    "detail",
)


def now_ns() -> int:
    """CLOCK_REALTIME in ns: the clock every recorder in mec-cast stamps with."""
    return time.time_ns()


class _Batcher:
    def __init__(self, url: Optional[str], interval_s: float = 1.0, capacity: int = 2000):
        self.url = url.rstrip("/") + "/api/v1/logs" if url else None
        self.interval_s = interval_s
        self.capacity = capacity
        self._buf: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.posted = 0
        self.failed = 0
        self.dropped = 0
        self._thread = None
        if self.url:
            self._thread = threading.Thread(target=self._run, name="kpm-logging", daemon=True)
            self._thread.start()

    def push(self, entry: Dict[str, Any]) -> None:
        if not self.url:
            return
        with self._lock:
            if len(self._buf) >= self.capacity:
                self.dropped += 1
                return
            self._buf.append(entry)

    def _flush(self) -> None:
        with self._lock:
            batch, self._buf = self._buf, []
        for i in range(0, len(batch), 100):  # the service's batch ceiling
            chunk = batch[i : i + 100]
            req = urllib.request.Request(
                self.url,
                data=json.dumps(chunk).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=3):
                    self.posted += 1
            except Exception as e:  # noqa: BLE001 - counted, reported in status
                self.failed += 1
                log.debug("logging post failed: %s", e)

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            self._flush()
        self._flush()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)


class RunRecorder:
    """Everything one run writes. Opened on run start, closed on stop."""

    def __init__(self, runs_dir: str, run_id: str, logging_url: Optional[str] = None):
        self.run_id = run_id
        self.out_dir = Path(runs_dir) / run_id / LEAF
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._kpi = self._open_csv("kpi.csv", KPI_CSV_HEADER)
        self._control = self._open_csv("control.csv", CONTROL_HEADER)
        # Append, as every recorder here does: a node rejoining a run
        # continues the same record.
        self._raw = (self.out_dir / "indications.jsonl").open("a")
        self._lock = threading.Lock()
        self._logging = _Batcher(logging_url)
        self.indications = 0
        self.rows = 0
        self.controls = 0
        #: Latest per-UE throughput sum (dl, ul) in bit/s, for parity.
        self.last_throughput: Optional[tuple] = None

    def _open_csv(self, name: str, header) -> Any:
        path = self.out_dir / name
        new = not path.exists() or path.stat().st_size == 0
        f = path.open("a", newline="")
        w = csv.writer(f)
        if new:
            w.writerow(header)
            f.flush()
        return (f, w)

    def _entry(self, message: str, context: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "level": "INFO",
            "service": SERVICE,
            "logger": "ran.xapp",
            "message": message,
            "trace_id": self.run_id,
            "context": dict(context, run_id=self.run_id),
        }

    def record_indication(self, indication: Dict[str, Any], recv_ns: Optional[int] = None) -> int:
        """Normalise, write and log one decoded indication. Returns its row count."""
        recv_ns = recv_ns or now_ns()
        rows = normalise_kpm(indication)
        with self._lock:
            self.indications += 1
            self._raw.write(json.dumps(dict(indication, recv_ns=recv_ns), default=str) + "\n")
            _, w = self._kpi
            for r in rows:
                w.writerow(
                    [
                        r.source,
                        r.gnb_ts_ns,
                        recv_ns,
                        r.cell,
                        r.ue,
                        r.metric,
                        int(r.value) if float(r.value).is_integer() else r.value,
                        r.unit,
                    ]
                )
            self.rows += len(rows)
            dl = [r.value for r in rows if r.metric == "ue.dl_throughput_bps"]
            ul = [r.value for r in rows if r.metric == "ue.ul_throughput_bps"]
            if dl or ul:
                # Per-UE values of the newest granularity period only.
                newest = max(r.gnb_ts_ns for r in rows)
                self.last_throughput = (
                    sum(
                        r.value
                        for r in rows
                        if r.metric == "ue.dl_throughput_bps" and r.gnb_ts_ns == newest
                    ),
                    sum(
                        r.value
                        for r in rows
                        if r.metric == "ue.ul_throughput_bps" and r.gnb_ts_ns == newest
                    ),
                )
        self._logging.push(
            self._entry(
                "kpm indication",
                {
                    "kpi": indication,
                    "recv_ns": recv_ns,
                    "gnb_ts_ns": int(indication.get("collect_start_ns") or 0),
                    "norm": [
                        {k: v for k, v in r.as_dict().items() if k not in ("source", "recv_ns")}
                        for r in rows
                    ],
                },
            )
        )
        return len(rows)

    def record_control(
        self,
        *,
        node: str,
        ue_id: int,
        action: str,
        min_prb_ratio: int,
        max_prb_ratio: int,
        dedicated_prb_ratio: int,
        outcome: str,
        detail: str = "",
    ) -> None:
        ts = now_ns()
        row = [
            ts,
            node,
            ue_id,
            action,
            min_prb_ratio,
            max_prb_ratio,
            dedicated_prb_ratio,
            outcome,
            detail,
        ]
        with self._lock:
            self.controls += 1
            f, w = self._control
            w.writerow(row)
            f.flush()
        self._logging.push(
            self._entry(
                f"ran control {action} {outcome}",
                {"control": dict(zip(CONTROL_HEADER, row))},
            )
        )

    def flush(self) -> None:
        with self._lock:
            self._kpi[0].flush()
            self._raw.flush()

    def close(self) -> Dict[str, Any]:
        self.flush()
        self._logging.close()
        with self._lock:
            self._kpi[0].close()
            self._control[0].close()
            self._raw.close()
        return {
            "indications": self.indications,
            "rows": self.rows,
            "controls": self.controls,
            "batches_posted": self._logging.posted,
            "post_failures": self._logging.failed,
            "dropped": self._logging.dropped,
        }

    def logging_counters(self) -> Dict[str, int]:
        return {
            "batches_posted": self._logging.posted,
            "post_failures": self._logging.failed,
        }


def env_runs_dir() -> str:
    return os.environ.get("RUNS_DIR") or "runs"
