#!/usr/bin/env python3
"""gnb-sim: srsRAN's JSON metrics export, without srsRAN.

Local-testing stand-in for a gNB (level 1 in the RAN plan): no radio, no
srsRAN build, runs anywhere Docker does — macOS included. It speaks both of
the transports srsRAN has used, at the same time, so the collector's
``GNB_METRICS_SOURCE`` choice is exercised exactly as a real gNB would:

* **UDP push** (srsRAN <= 24.x ``metrics.addr/port``): one datagram per report
  to ``SIM_UDP_TARGET``.
* **WebSocket** (srsRAN 25.04+ ``remote_control``): serves ``SIM_WS_BIND``;
  a client sends ``{"cmd":"metrics_subscribe"}``, gets the reply srsRAN gives,
  then every report as a text frame.

Reports come from a fixture (default: the collector's current-format
fixture), one line per report, cycled. Each one is re-stamped with *now* in
srsRAN's ISO format, so the collector's ``network_ns`` (gNB stamp -> arrival)
is a real measurement. With ``SIM_MODE=model`` the per-UE figures also take a
seeded random walk (MCS, CQI, SNR, throughput, HARQ failures), so a long run
is not the same five reports on repeat.

Environment (all optional):

  SIM_FIXTURE     JSONL of reports            (ran/collector/testdata/srsran_ws_metrics.synthetic.jsonl)
  SIM_PERIOD_MS   report period               (1000 — srsRAN's du_report_period default)
  SIM_UDP_TARGET  host:port, empty = off      (ran-collector:55555)
  SIM_WS_BIND     host:port, empty = off      (0.0.0.0:8001)
  SIM_MODE        replay | model              (replay)
  SIM_SEED        model seed                  (42)
  SIM_E2_BIND     host:port, empty = off      (0.0.0.0:8002)
  SIM_E2_NODE     the E2 node id it reports   (gnbd_001_001_00019b_0)

**The E2 feed** (SIM_E2_BIND) stands in for a near-RT RIC with one srsRAN E2
node behind it, for the xApp's ``sim`` adapter. It is a small JSON protocol,
not E2AP — what it simulates is the *data*, decoded exactly as oran-sc-ric's
``extract_meas_data`` decodes it:

  -> {"op": "nodes"}                                  <- {"op": "nodes", "nodes": [...]}
  -> {"op": "subscribe", "id", "style", "metrics", "ue_ids", "period_ms"}
                                                      <- {"op": "subscribed", "id"}
                                                      <- {"op": "indication", "id", "indication": {...}}
  -> {"op": "unsubscribe", "id"}
  -> {"op": "control", "id", "action": "prb_quota", "ue_id", "min_prb_ratio",
      "max_prb_ratio", "dedicated_prb_ratio"}          <- {"op": "control_ack", "id", "ok", "detail"}

KPM values come from the same report the JSON transports just sent (kbit/s,
as TS 28.552 specifies), and a PRB-quota control caps that UE's throughput in
**both** sources — which is what lets the xApp's parity check and control
loop be tested on a laptop. E2 UE id N is the N-th UE of the report.

The cap applies to DL and UL alike. That is a modelling choice, not a fact
about srsRAN: whether its RC action limits uplink at all is calibration step
0.1 of docs/research/protocol.md, to be settled on the lab gNB.

Standard library plus ``websockets`` (>=12, the sync server).
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import os
import random
import signal
import socket
import sys
import threading
import time
from pathlib import Path

DEFAULT_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "collector"
    / "testdata"
    / "srsran_ws_metrics.synthetic.jsonl"
)


def log(msg: str) -> None:
    print(f"[gnb-sim] {msg}", flush=True)


def srsran_now() -> str:
    """srsRAN's timestamp format: ISO-8601, milliseconds, no zone (UTC here)."""
    now = dt.datetime.now(dt.timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}"


def split_hostport(value: str, default_host: str = "0.0.0.0") -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    return (host or default_host), int(port)


def load_fixture(path: Path) -> list[dict]:
    reports = []
    for n, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            reports.append(json.loads(line))
        except json.JSONDecodeError as e:
            raise SystemExit(f"{path}:{n}: not JSON: {e}") from e
    if not reports:
        raise SystemExit(f"{path}: no reports")
    return reports


def ue_lists(report: dict):
    """Every per-UE list in a report, old layout or new."""
    for cell in report.get("cells") or []:
        yield cell.get("ue_list") or []
    if "ue_list" in report:
        yield report["ue_list"]


def ue_fields(entry: dict) -> dict:
    # The old layout wraps each UE in {"ue_container": {...}}.
    return entry.get("ue_container", entry)


class Model:
    """A seeded random walk over the per-UE figures. Deterministic per seed."""

    def __init__(self, seed: int):
        self.rng = random.Random(seed)
        self.drift: dict[int, float] = {}

    def apply(self, report: dict) -> None:
        for ues in ue_lists(report):
            for entry in ues:
                ue = ue_fields(entry)
                rnti = int(ue.get("rnti", 0))
                d = self.drift.get(rnti, 0.0) + self.rng.gauss(0.0, 0.6)
                d = max(-6.0, min(3.0, d))  # degradations go deeper than gains
                self.drift[rnti] = d
                for key, lo, hi in (
                    ("dl_mcs", 0, 27),
                    ("ul_mcs", 0, 27),
                    ("cqi", 1, 15),
                ):
                    if key in ue:
                        ue[key] = int(max(lo, min(hi, round(ue[key] + d))))
                if "pusch_snr_db" in ue:
                    ue["pusch_snr_db"] = round(ue["pusch_snr_db"] + d, 1)
                scale = max(0.2, 1.0 + d / 10.0)
                for key in ("dl_brate", "ul_brate"):
                    if key in ue:
                        ue[key] = round(ue[key] * scale, 1)
                # Worse channel, more HARQ failures.
                for key in ("dl_nof_nok", "ul_nof_nok"):
                    if key in ue:
                        ue[key] = max(
                            0, int(ue[key] * (1.0 - d / 3.0)) + self.rng.randint(0, 3)
                        )


class Subscribers:
    """WebSocket clients that have sent metrics_subscribe."""

    def __init__(self):
        self.lock = threading.Lock()
        self.clients: set = set()

    def add(self, ws) -> None:
        with self.lock:
            self.clients.add(ws)

    def discard(self, ws) -> None:
        with self.lock:
            self.clients.discard(ws)

    def broadcast(self, text: str) -> int:
        with self.lock:
            clients = list(self.clients)
        sent = 0
        for ws in clients:
            try:
                ws.send(text)
                sent += 1
            except Exception:  # noqa: BLE001 - a dead client is just dropped
                self.discard(ws)
        return sent


#: KPM measurement -> how to derive it from one srsRAN UE, given the period in
#: seconds. Units as TS 28.552: throughput kbit/s, volume kbit.
KPM_FROM_UE = {
    "DRB.UEThpDl": lambda ue, s: ue.get("dl_brate", 0) / 1000.0,
    "DRB.UEThpUl": lambda ue, s: ue.get("ul_brate", 0) / 1000.0,
    "DRB.RlcSduTransmittedVolumeDL": lambda ue, s: ue.get("dl_brate", 0) * s / 1000.0,
    "DRB.RlcSduTransmittedVolumeUL": lambda ue, s: ue.get("ul_brate", 0) * s / 1000.0,
    "DRB.RlcPacketDropRateDl": lambda ue, s: 0,
    "DRB.PacketSuccessRateUlgNBUu": lambda ue, s: round(
        100.0
        * ue.get("ul_nof_ok", 0)
        / max(1, ue.get("ul_nof_ok", 0) + ue.get("ul_nof_nok", 0)),
        2,
    ),
    # srsRAN's own agent reports these as dummies; so does this one.
    "CQI": lambda ue, s: ue.get("cqi", 0),
    "RSRP": lambda ue, s: 0,
    "RSRQ": lambda ue, s: 0,
}


class E2Sim:
    """One E2 node's KPM view of the reports, and its PRB-quota control."""

    def __init__(self, node_id: str = "gnbd_001_001_00019b_0"):
        self.node_id = node_id
        self.lock = threading.Lock()
        self.latest: dict | None = None
        #: E2 UE id -> max PRB ratio in percent, from RC controls.
        self.caps: dict[int, int] = {}
        self.server = None

    # --- shared state -----------------------------------------------------

    def ues(self, report: dict | None = None) -> list[dict]:
        report = report if report is not None else self.latest
        out: list[dict] = []
        for lst in ue_lists(report or {}):
            out.extend(ue_fields(e) for e in lst)
        return out

    def apply_caps(self, report: dict) -> None:
        """Scale capped UEs' throughput, in place, before anything is sent."""
        with self.lock:
            caps = dict(self.caps)
        for idx, ue in enumerate(self.ues(report)):
            ratio = caps.get(idx)
            if ratio is None:
                continue
            for key in ("dl_brate", "ul_brate"):
                if key in ue:
                    ue[key] = round(ue[key] * ratio / 100.0, 1)

    def update(self, report: dict) -> None:
        with self.lock:
            self.latest = copy.deepcopy(report)

    def indication(
        self, style: int, metrics: list[str], ue_ids: list[int], period_ms: int
    ) -> dict:
        with self.lock:
            report = copy.deepcopy(self.latest)
        ues = self.ues(report) if report else []
        s = period_ms / 1000.0
        known = [m for m in metrics if m in KPM_FROM_UE]

        def per_ue(ue: dict) -> dict:
            return {m: [KPM_FROM_UE[m](ue, s)] for m in known}

        if style in (1, 2):
            if style == 2:
                idx = ue_ids[0] if ue_ids else 0
                data = per_ue(ues[idx]) if idx < len(ues) else {m: [0] for m in known}
            else:
                data = {m: [sum(KPM_FROM_UE[m](ue, s) for ue in ues)] for m in known}
            meas = {"measData": data, "granulPeriod": period_ms}
        else:
            wanted = ue_ids if style == 5 else list(range(len(ues)))
            meas = {
                "ueMeasData": {
                    str(i): {"measData": per_ue(ues[i]), "granulPeriod": period_ms}
                    for i in wanted
                    if i < len(ues)
                }
            }
        start = time.time_ns() - period_ms * 1_000_000
        return {"e2_node_id": self.node_id, "collect_start_ns": start, "meas": meas}

    def control(self, msg: dict) -> tuple[bool, str]:
        if msg.get("action") != "prb_quota":
            return False, f"unsupported action {msg.get('action')!r}"
        lo, hi = int(msg.get("min_prb_ratio", 0)), int(msg.get("max_prb_ratio", 100))
        if not 0 <= lo <= hi <= 100:
            return False, f"bad ratios min={lo} max={hi}"
        ue = int(msg.get("ue_id", 0))
        with self.lock:
            if hi >= 100:
                self.caps.pop(ue, None)
            else:
                self.caps[ue] = hi
        log(f"e2: PRB quota ue={ue} min={lo} max={hi}")
        return True, ""

    # --- the feed ---------------------------------------------------------

    def serve(self, bind: str):
        from websockets.sync.server import serve

        def handler(ws):
            subs: dict[str, threading.Event] = {}
            send_lock = threading.Lock()

            def send(obj: dict) -> None:
                with send_lock:
                    ws.send(json.dumps(obj, separators=(",", ":")))

            def emit(sid: str, req: dict, stop: threading.Event) -> None:
                period = max(1000, int(req.get("period_ms") or 1000))
                while not stop.wait(period / 1000.0):
                    if self.latest is None:
                        continue
                    ind = self.indication(
                        int(req.get("style") or 5),
                        list(req.get("metrics") or []),
                        [int(u) for u in (req.get("ue_ids") or [0])],
                        period,
                    )
                    try:
                        send({"op": "indication", "id": sid, "indication": ind})
                    except Exception:  # noqa: BLE001 - client gone
                        return

            try:
                for message in ws:
                    try:
                        msg = json.loads(message)
                    except ValueError:
                        continue
                    op = msg.get("op")
                    if op == "nodes":
                        send({"op": "nodes", "nodes": [self.node_id]})
                    elif op == "subscribe":
                        sid = str(msg.get("id"))
                        stop = threading.Event()
                        subs[sid] = stop
                        send({"op": "subscribed", "id": sid})
                        threading.Thread(
                            target=emit, args=(sid, msg, stop), daemon=True
                        ).start()
                        log(f"e2: subscription {sid} style={msg.get('style')}")
                    elif op == "unsubscribe":
                        stop = subs.pop(str(msg.get("id")), None)
                        if stop:
                            stop.set()
                    elif op == "control":
                        ok, detail = self.control(msg)
                        send(
                            {
                                "op": "control_ack",
                                "id": msg.get("id"),
                                "ok": ok,
                                "detail": detail,
                            }
                        )
            finally:
                for stop in subs.values():
                    stop.set()

        host, port = split_hostport(bind)
        self.server = serve(handler, host, port)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        log(
            f"e2 feed on {host}:{self.server.socket.getsockname()[1]} (node {self.node_id})"
        )
        return self.server


def serve_ws(bind: str, subscribers: Subscribers):
    from websockets.sync.server import serve

    def handler(ws):
        try:
            for message in ws:
                try:
                    cmd = json.loads(message).get("cmd")
                except (ValueError, AttributeError):
                    cmd = None
                if cmd == "metrics_subscribe":
                    ws.send(json.dumps({"cmd": "metrics_subscribe"}))
                    subscribers.add(ws)
                    log(f"ws subscriber {ws.remote_address}")
                elif cmd == "metrics_unsubscribe":
                    subscribers.discard(ws)
                    ws.send(json.dumps({"cmd": "metrics_unsubscribe"}))
        finally:
            subscribers.discard(ws)

    host, port = split_hostport(bind)
    server = serve(handler, host, port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log(f"ws remote_control on {host}:{port}")
    return server


def main() -> int:
    fixture = Path(os.environ.get("SIM_FIXTURE") or DEFAULT_FIXTURE)
    period = max(10, int(os.environ.get("SIM_PERIOD_MS") or 1000)) / 1000.0
    udp_target = os.environ.get("SIM_UDP_TARGET", "ran-collector:55555")
    ws_bind = os.environ.get("SIM_WS_BIND", "0.0.0.0:8001")
    mode = (os.environ.get("SIM_MODE") or "replay").lower()
    if mode not in ("replay", "model"):
        raise SystemExit(f"SIM_MODE={mode!r}: expected replay or model")
    model = Model(int(os.environ.get("SIM_SEED") or 42)) if mode == "model" else None

    reports = load_fixture(fixture)
    log(
        f"{len(reports)} reports from {fixture}, every {period * 1000:.0f} ms, mode={mode}"
    )

    subscribers = Subscribers()
    server = serve_ws(ws_bind, subscribers) if ws_bind else None
    e2 = E2Sim(os.environ.get("SIM_E2_NODE") or "gnbd_001_001_00019b_0")
    e2_bind = os.environ.get("SIM_E2_BIND", "0.0.0.0:8002")
    if e2_bind:
        e2.serve(e2_bind)

    udp = None
    if udp_target:
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        log(f"udp push to {udp_target}")
    udp_addr = split_hostport(udp_target, "127.0.0.1") if udp_target else None

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    sent = 0
    next_at = time.monotonic()
    while not stop.is_set():
        report = copy.deepcopy(reports[sent % len(reports)])
        stamp = srsran_now()
        if "timestamp" in report:
            report["timestamp"] = stamp
        for cell in report.get("cells") or []:
            if "timestamp" in cell:
                cell["timestamp"] = stamp
        if model:
            model.apply(report)
        e2.apply_caps(report)
        e2.update(report)
        text = json.dumps(report, separators=(",", ":"))

        if udp is not None:
            try:
                # Resolved per send: the collector may start after us.
                udp.sendto(
                    text.encode(), (socket.gethostbyname(udp_addr[0]), udp_addr[1])
                )
            except OSError:
                pass  # nobody listening yet is not an error for a push source
        subscribers.broadcast(text)
        sent += 1
        if sent % 30 == 0:
            log(f"sent {sent} reports")

        next_at += period
        stop.wait(max(0.0, next_at - time.monotonic()))

    if server is not None:
        server.shutdown()
    if e2.server is not None:
        e2.server.shutdown()
    log(f"stopped after {sent} reports")
    return 0


if __name__ == "__main__":
    sys.exit(main())
