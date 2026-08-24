"""mec-cast render node — the UE-side receiver that closes the loop.

Subscribes to `mec_cast/result`, the processed cloud the edge sends back,
stamps `recv_ns` on arrival, draws it, stamps `process_done_ns` when the draw
returns, and records the sample at site 2 (`runs/<RUN_ID>/render/samples.csv`).

Why this node earns its place beyond showing a picture: the envelope carries
the **original** `capture_ns`, stamped by the publisher on this same host, and
`process_done_ns` is stamped here. Both come off one `CLOCK_REALTIME`, so

    e2e_ns = process_done_ns - capture_ns

is a true round-trip glass-to-glass delay that owes nothing to PTP — the only
such number in the system. Every other one-way metric is only as good as the
clock discipline. Comparing the two gives an independent read on clock offset.

`network_ns` here is the downlink leg alone (edge send -> UE receive) and *is*
PTP-dependent, like every cross-host figure. `processing_ns` is draw time and
is local, so it is not.

Parameters:
    sink            (str,  default null)  null | rerun | ros
    serve           (bool, default true)  rerun only: host the web viewer
    web_port        (int,  default 9876)  the page
    grpc_port       (int,  default 9877)  the stream the page connects back to.
                    Both must be reachable from the operator's browser
    viewer_host     (str,  default localhost)  how the *browser* reaches this
                    host. Set it to the UE's address when browsing from
                    another machine, as in the lab
    reliability     (str,  default best_effort)  must match the edge's
                    `result_reliability`
    qos_depth       (int,  default 1)  display semantics: newest frame wins
    admin_url       (str,  default $ADMIN_URL)  empty = standalone
    admin_autostart (bool, default true)
    admin_instance  (int,  default 0)

Environment: RUN_ID, LOGGING_URL, RUNS_DIR, ADMIN_URL, RENDER_SINK.

The default sink is `null` on purpose. The node must import and run on a host
with no renderer installed, so the measurement path is never gated on a
graphics dependency; the compose file selects `rerun` for the lab.

KEEP_LAST(1) means a frame superseded before the callback runs is discarded by
the middleware and never seen here, so a slow renderer and a lossy downlink
both surface as `seq_gaps`. They are told apart afterwards, not by a counter:
`processing_ns` is the draw time, and gaps alongside a draw time near the frame
interval mean the renderer, not the network. A frame whose draw raises is still
recorded, with `process_done_ns` left at 0 — the recorder's rule that a derived
delay needs both stamps then leaves `e2e` empty for a frame that never reached
glass, which is the truthful answer.
"""

import os
import signal
import socket

import numpy as np
import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy

import mec_cast_telemetry as tel
from mec_cast_admin_client import AdminClient
from mec_cast_admin_client import protocol as ap
from mec_cast_msgs.msg import CloudWithTelemetry
from mec_cast_render.sinks import SINKS, build_sink

SITE_RENDER = 2
RESULT_TOPIC = "mec_cast/result"

ADMIN_POLL_S = 0.1
STATUS_PERIOD_S = 2.0
#: How often the node says whether it is receiving anything. It logs one line
#: per frame, so on a congested uplink it can go tens of seconds between
#: lines — which is indistinguishable from a dead process in a `compose up`
#: view where other services are scrolling. This makes silence explicit.
PROGRESS_PERIOD_S = 5.0


def cloud_qos(reliability: str, depth: int) -> QoSProfile:
    """QoS for the result subscription. Must match the edge's result QoS.

    Duplicated from the other nodes for the reason given there: they run on
    different hosts, so a shared import would misrepresent the deployment.
    The default differs from theirs and that is deliberate — a display wants
    the newest frame and never a stale one, so `best_effort` + KEEP_LAST(1)
    is the honest setting here even though the uplink runs reliable.
    """
    if reliability not in ("reliable", "best_effort"):
        raise ValueError(
            f"reliability must be 'reliable' or 'best_effort', got {reliability!r}"
        )
    return QoSProfile(
        reliability=(
            ReliabilityPolicy.RELIABLE
            if reliability == "reliable"
            else ReliabilityPolicy.BEST_EFFORT
        ),
        history=HistoryPolicy.KEEP_LAST,
        depth=depth,
    )


class RenderNode(Node):
    def __init__(self) -> None:
        super().__init__("mec_cast_render")
        self.declare_parameter("sink", os.environ.get("RENDER_SINK", "null"))
        self.declare_parameter("serve", True)
        self.declare_parameter("web_port", 9876)
        self.declare_parameter("grpc_port", 9877)
        self.declare_parameter("viewer_host", os.environ.get("VIEWER_HOST", "localhost"))
        self.declare_parameter("record_rrd", True)
        self.declare_parameter("reliability", "best_effort")
        self.declare_parameter("qos_depth", 1)
        self.declare_parameter("admin_url", os.environ.get("ADMIN_URL", ""))
        self.declare_parameter("admin_autostart", True)
        self.declare_parameter("admin_instance", 0)

        self.sink_kind = str(self.get_parameter("sink").value)
        if self.sink_kind not in SINKS:
            raise ValueError(f"unknown sink {self.sink_kind!r}, expected one of {SINKS}")
        self.serve = bool(self.get_parameter("serve").value)
        self.web_port = int(self.get_parameter("web_port").value)
        self.grpc_port = int(self.get_parameter("grpc_port").value)
        self.viewer_host = str(self.get_parameter("viewer_host").value)
        self.record_rrd = bool(self.get_parameter("record_rrd").value)
        self.reliability = str(self.get_parameter("reliability").value)
        self.qos_depth = int(self.get_parameter("qos_depth").value)

        self.runs_dir = os.environ.get("RUNS_DIR", "runs")
        self.logging_url = os.environ.get("LOGGING_URL") or None

        self.run_id: str | None = None
        self.recorder: tel.Recorder | None = None
        self.sub = None
        self.sink = None
        self.frames = 0
        self.drawn = 0
        self.seq_gaps = 0
        self.last_seq: int | None = None

        self.admin = AdminClient(
            node_type=ap.NodeType.RENDER,
            host=socket.gethostname(),
            url=str(self.get_parameter("admin_url").value),
            instance=int(self.get_parameter("admin_instance").value),
            version_sha=os.environ.get("VCS_REF", ""),
            version_tag=os.environ.get("VERSION", ""),
            pid=os.getpid(),
        )
        self.autostart = bool(self.get_parameter("admin_autostart").value)
        self._last_status: dict | None = None
        self._progress_mark = 0
        self.create_timer(PROGRESS_PERIOD_S, self._log_progress)

        if self.admin.enabled:
            self.admin.update_identity(autostart=self.autostart, params=self.params())
            self.admin.start()
            self.create_timer(ADMIN_POLL_S, self._drain_admin)
            self.create_timer(STATUS_PERIOD_S, self._report_status)
            self.get_logger().info(
                f"render up (admin={self.admin.url}, node_id={self.admin.node_id}, "
                f"sink={self.sink_kind}, qos={self.reliability}/KEEP_LAST({self.qos_depth}))"
            )
        else:
            self.start_run(os.environ.get("RUN_ID", "dev-run"))
            self.get_logger().info(
                f"render up (run_id={self.run_id}, sink={self.sink_kind}, "
                f"qos={self.reliability}/KEEP_LAST({self.qos_depth}))"
            )

    # --- run lifecycle ----------------------------------------------------

    def params(self) -> dict:
        return {
            "sink": self.sink_kind,
            "reliability": self.reliability,
            "qos_depth": self.qos_depth,
        }

    @property
    def running(self) -> bool:
        return self.recorder is not None

    def start_run(self, run_id: str) -> None:
        """Build the Recorder and the sink, then subscribe.

        The sink is built per run, not per process: a viewer session is scoped
        to one experiment, so switching runs cannot append to the previous
        one's timeline.
        """
        if self.running:
            if run_id == self.run_id:
                return
            self.stop_run()

        self.run_id = run_id
        self.frames = 0
        self.drawn = 0
        self.seq_gaps = 0
        self.last_seq = None
        self._progress_mark = 0
        out_dir = os.path.join(self.runs_dir, run_id, "render")
        self.recorder = tel.Recorder(
            run_id, "mec-cast-render", out_dir, logging_url=self.logging_url
        )
        self.sink = build_sink(
            self.sink_kind,
            node=self,
            run_id=run_id,
            serve=self.serve,
            web_port=self.web_port,
            grpc_port=self.grpc_port,
            viewer_host=self.viewer_host,
            rrd_path=(os.path.join(out_dir, "session.rrd") if self.record_rrd else None),
        )
        self.sub = self.create_subscription(
            CloudWithTelemetry,
            RESULT_TOPIC,
            self.on_result,
            cloud_qos(self.reliability, self.qos_depth),
        )
        where = getattr(self.sink, "url", None)
        self.get_logger().info(
            f"render recording run {run_id} (sink={self.sink_kind})"
            + (f" — viewer at {where}" if where else "")
        )

    def stop_run(self) -> dict:
        """Unsubscribe, close the sink, then drain the recorder. Order matters:
        a callback firing into a shut-down recorder would raise."""
        if not self.running:
            return {}
        if self.sub is not None:
            self.destroy_subscription(self.sub)
            self.sub = None
        if self.sink is not None:
            self.sink.close()
            self.sink = None
        report = self.recorder.shutdown()
        self.recorder = None
        self.get_logger().info(
            f"render stopped run {self.run_id}: frames={self.frames} "
            f"drawn={self.drawn} seq_gaps={self.seq_gaps} report={report}"
        )
        return report

    # --- admin ------------------------------------------------------------

    def _drain_admin(self) -> None:
        for frame in self.admin.poll():
            payload = frame.get("payload") or {}
            if frame["type"] == ap.MessageType.WELCOME:
                active = payload.get("active_run")
                if active and self.autostart:
                    self.start_run(active["run_id"])
                self._report_status(force=True)
            elif frame["type"] == ap.MessageType.COMMAND:
                self._apply_command(frame, payload)

    def _apply_command(self, frame: dict, payload: dict) -> None:
        command = payload.get("command")
        ok, error, stop_report = True, None, None
        try:
            if command in (ap.CommandType.RUN_START, ap.CommandType.STREAM_START):
                run_id = payload.get("run_id")
                if not run_id:
                    raise ValueError("run.start without a run_id")
                self.start_run(run_id)
            elif command in (ap.CommandType.RUN_STOP, ap.CommandType.STREAM_STOP):
                stop_report = self.stop_run()
            elif command != ap.CommandType.STATUS_REPORT:
                raise ValueError(f"unknown command {command!r}")
        except Exception as exc:  # a bad command must not kill the node
            ok, error = False, str(exc)
            self.get_logger().error(f"admin command {command} failed: {exc}")
        self.admin.publish_ack(frame["msg_id"], ok=ok, error=error)
        self._report_status(force=True, report=stop_report)

    def peers(self) -> list[dict]:
        """Who is publishing the result topic to us, from the ROS graph."""
        if not self.running:
            return []
        try:
            infos = self.get_publishers_info_by_topic(RESULT_TOPIC)
        except Exception:
            return []
        return [ap.peer(info.node_name or "unknown") for info in infos]

    def _report_status(self, force: bool = False, report: dict | None = None) -> None:
        if not self.admin.enabled:
            return
        payload = ap.status_payload(
            node_type=ap.NodeType.RENDER,
            state=ap.NodeState.RUNNING if self.running else ap.NodeState.IDLE,
            run_id=self.run_id,
            subscribed=self.sub is not None,
            peers=self.peers(),
            params=self.params(),
            counters={
                "frames": self.frames,
                "drawn": self.drawn,
                "seq_gaps": self.seq_gaps,
            },
            autostart=self.autostart,
            report=report or {},
        )
        if force or payload != self._last_status:
            self._last_status = payload
            self.admin.update_identity(
                state=payload["state"], run_id=self.run_id, params=self.params()
            )
            self.admin.publish_status(payload)

    def _log_progress(self) -> None:
        """Say whether frames are arriving, whether or not any did.

        A renderer with nothing to draw is silent, and silence is the one
        state an operator cannot tell from a crash. Rather than make them run
        `docker inspect` to find RestartCount=0, say it here — and when
        nothing is arriving, name the two things that actually cause it.
        """
        if not self.running:
            self.get_logger().info("render idle: no active run (waiting for the admin, or RUN_ID unset)")
            return
        delta = self.frames - self._progress_mark
        self._progress_mark = self.frames
        if delta:
            self.get_logger().info(
                f"render alive: {delta} frames in {PROGRESS_PERIOD_S:.0f}s "
                f"({delta / PROGRESS_PERIOD_S:.1f} Hz), total={self.frames} "
                f"drawn={self.drawn} seq_gaps={self.seq_gaps}"
            )
        else:
            self.get_logger().warning(
                f"render alive but received NOTHING in {PROGRESS_PERIOD_S:.0f}s "
                f"(total={self.frames}). The process is fine. Either the edge is not "
                f"sending — it needs publish_result:=true, off by default — or the "
                f"uplink is dropping frames before they reach it. Compare row counts: "
                f"wc -l runs/{self.run_id}/pub/samples.csv runs/{self.run_id}/edge/samples.csv"
            )

    # --- the hot path -----------------------------------------------------

    def on_result(self, msg: CloudWithTelemetry) -> None:
        recv_ns = tel.now_ns()  # first line: the arrival stamp
        env = msg.envelope

        if self.last_seq is not None and env.seq != self.last_seq + 1:
            self.seq_gaps += 1
        self.last_seq = env.seq
        self.frames += 1

        n = msg.cloud.width * msg.cloud.height
        points = np.frombuffer(msg.cloud.data, dtype=np.float32).reshape(n, 3)

        # A draw that raises leaves process_done_ns at 0, making e2e
        # undefined for that frame — it never reached glass. Measurement
        # continues either way: a renderer fault must not end the run.
        process_done_ns = 0
        try:
            self.sink.draw(
                env.seq,
                points,
                {
                    "e2e_ns": recv_ns - env.capture_ns,
                    "network_ns": recv_ns - env.send_ns,
                },
            )
            process_done_ns = tel.now_ns()
            self.drawn += 1
        except Exception as exc:
            self.get_logger().error(f"sink.draw failed on seq={env.seq}: {exc}")

        self.recorder.record(
            seq=env.seq,
            modality=env.modality,
            capture_ns=env.capture_ns,
            send_ns=env.send_ns,
            recv_ns=recv_ns,
            process_done_ns=process_done_ns,
            payload_bytes=len(msg.cloud.data),
            site=SITE_RENDER,
            trace_id=bytes(env.trace_id),
        )
        # Parseable progress line — the launch test and humans both read it.
        # round_trip_ns is the PTP-free number; downlink_ns is not.
        self.get_logger().info(
            f"rendered seq={env.seq} n={n} "
            f"round_trip_ns={process_done_ns - env.capture_ns if process_done_ns else 0} "
            f"downlink_ns={recv_ns - env.send_ns}"
        )

    def finish(self) -> None:
        report = self.stop_run()
        self.admin.goodbye(reason="shutdown", run_id=self.run_id, final_report=report)
        self.get_logger().info(f"render done: report={report}")


def main(args=None) -> None:
    rclpy.init(args=args)
    signal.signal(signal.SIGTERM, lambda *_: rclpy.shutdown())
    node = RenderNode()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.finish()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
