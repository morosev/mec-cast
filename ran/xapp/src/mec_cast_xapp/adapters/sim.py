"""E2Port against gnb-sim's E2 feed (ran/sim/gnb_sim.py, SIM_E2_BIND).

For testing without a radio or a RIC. The feed speaks the JSON E2 feed
protocol (adapters/jsonfeed.py) and its indications are already in the decoded
shape the normaliser reads, so everything above the adapter — the
capabilities, the normaliser, the recorder, the admin — runs exactly the code
it runs in the lab.

One WebSocket, one reader thread. The connection is re-established on demand:
gnb-sim may start after the xApp, and may restart under it.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Dict, Optional

from .jsonfeed import JsonFeedPort

log = logging.getLogger("mec_cast_xapp.sim")


class SimPort(JsonFeedPort):
    name = "sim"

    def __init__(self, target: str, timeout_s: float = 3.0):
        super().__init__(timeout_s)
        self.url = target if "://" in target else f"ws://{target}"
        self._ws = None
        self._lock = threading.Lock()
        self._reader: Optional[threading.Thread] = None

    def _where(self) -> str:
        return self.url

    def _connect(self):
        with self._lock:
            if self._ws is not None:
                return self._ws
            from websockets.sync.client import connect

            # Entered explicitly: websockets >= 14 wants connect() used as a
            # context manager, and this connection outlives any one block.
            self._cm = connect(self.url, open_timeout=self.timeout_s)
            self._ws = self._cm.__enter__()
            self._reader = threading.Thread(target=self._read, args=(self._ws,), daemon=True)
            self._reader.start()
            # Resubscribe after a reconnect: a restarted gnb-sim forgets.
            for sid, req in self._sub_requests.items():
                self._ws.send(json.dumps(dict(req, op="subscribe", id=sid)))
            return self._ws

    def _send(self, obj: Dict[str, Any]) -> None:
        ws = self._connect()
        try:
            ws.send(json.dumps(obj))
        except Exception:
            with self._lock:
                self._ws = None
            raise

    def _read(self, ws) -> None:
        try:
            for raw in ws:
                self._dispatch(json.loads(raw))
        except Exception as e:  # noqa: BLE001 - the connection dropped
            log.warning("sim e2 feed closed: %s", e)
        finally:
            with self._lock:
                if self._ws is ws:
                    self._ws = None

    def close(self) -> None:
        with self._lock:
            ws, self._ws = self._ws, None
            cm, self._cm = getattr(self, "_cm", None), None
        if ws is not None and cm is not None:
            try:
                cm.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
