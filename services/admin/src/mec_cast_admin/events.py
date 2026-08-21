"""Posting admin lifecycle events to the logging service.

An operator pressing Start is part of a run's story, and the logging service
already holds the timeline every other component writes to. Posting there puts
"the run was started", "a node went offline" and the measurements themselves on
one axis, joinable by ``trace_id``.

Best effort by design: the admin must keep working when the logging service is
down, which is exactly when an operator is most likely to be looking at it.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

#: The `service` identifier this component writes under. Declared in
#: docs/_facts.yml services.admin.
SERVICE = "mec-cast-admin"

_TIMEOUT_S = 2.0


class EventLog:
    """Fire-and-forget writer for admin lifecycle events."""

    def __init__(self, logging_url: str = "") -> None:
        self.url = logging_url.rstrip("/") if logging_url else ""
        self.posted = 0
        self.failures = 0

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def emit(
        self,
        message: str,
        *,
        run_id: str | None = None,
        level: str = "INFO",
        context: dict[str, Any] | None = None,
    ) -> None:
        """Post one entry. Returns immediately; the write happens on a thread.

        A daemon thread rather than an async task so that a slow logging
        service cannot add latency to a websocket handler or a REST route.
        """
        if not self.enabled:
            return
        entry = {
            "level": level,
            "service": SERVICE,
            "logger": "admin.runs",
            "message": message,
            "trace_id": run_id,
            "context": {"run_id": run_id, **(context or {})},
        }
        threading.Thread(target=self._post, args=(entry,), daemon=True).start()

    def _post(self, entry: dict[str, Any]) -> None:
        request = urllib.request.Request(
            f"{self.url}/api/v1/logs",
            data=json.dumps([entry]).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT_S):
                self.posted += 1
        except (urllib.error.URLError, OSError, ValueError) as exc:
            self.failures += 1
            logger.debug("event post failed: %s", exc)
