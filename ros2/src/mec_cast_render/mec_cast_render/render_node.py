"""mec-cast render node — the UE-side receiver that closes the loop.

Subscribes to `mec_cast/result`, the processed cloud the edge sends back,
stamps `recv_ns` on arrival, draws it, stamps `process_done_ns` when the draw
returns, and records the sample at site 2
(`runs/<RUN_ID>/render-<instance>/samples.csv`).

Why this node earns its place beyond showing a picture: the envelope carries
the **original** `capture_ns`, stamped by the publisher on this same host, and
`process_done_ns` is stamped here. Both come off one `CLOCK_REALTIME`, so

    e2e_ns = process_done_ns - capture_ns

is a true round-trip glass-to-glass delay that owes nothing to PTP — the only
such number in the system. Every other one-way metric is only as good as the
clock discipline. Comparing the two gives an independent read on clock offset.
**That property holds only while the paired lidar runs on this same host**;
a renderer on a different UE (ADR-0009, the split-renderer cell) loses it,
which the admin flags as WF_RENDER_CROSS_HOST.

`network_ns` here is the downlink leg alone (edge send -> UE receive) and *is*
PTP-dependent, like every cross-host figure. `processing_ns` is draw time and
is local, so it is not.

Parameters (shared set from MecCastNode: run_id, runs_dir, logging_url,
admin_url, admin_autostart, admin_instance — every environment variable is
demoted to a default):
    sink            (str,  default $RENDER_SINK or null)  null | rerun | ros
    serve           (bool, default true)  rerun only: host the web viewer
    web_port        (int,  default 9876)  the page
    grpc_port       (int,  default 9877)  the stream the page connects back to.
                    Both must be reachable from the operator's browser
    viewer_host     (str,  default $VIEWER_HOST or localhost)  how the
                    *browser* reaches this host. Set it to the UE's address
                    when browsing from another machine, as in the lab
    reliability     (str,  default best_effort)  must match the edge's
                    `result_reliability`
    qos_depth       (int,  default 1)  display semantics: newest frame wins

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

import numpy as np
from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy

import mec_cast_telemetry as tel
from mec_cast_admin_client import protocol as ap
from mec_cast_admin_client.node_base import MecCastNode, spin
from mec_cast_admin_client.sites import SITE_RENDER
from mec_cast_msgs.msg import CloudWithTelemetry
from mec_cast_render.sinks import SINKS, build_sink

RESULT_TOPIC = "mec_cast/result"

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


class RenderNode(MecCastNode):
    NODE_TYPE = ap.NodeType.RENDER
    SERVICE = "mec-cast-render"
    SITE = SITE_RENDER
    OUT_LEAF = "render"
    PEER_TOPIC = RESULT_TOPIC

    def __init__(self, *, node_name: str | None = None,
                 parameter_overrides: list | None = None) -> None:
        super().__init__(
            "mec_cast_render",
            node_name=node_name,
            parameter_overrides=parameter_overrides,
            autostart_default=True,
        )
        self.declare_parameter("sink", os.environ.get("RENDER_SINK", "null"))
        self.declare_parameter("serve", True)
        self.declare_parameter("web_port", 9876)
        self.declare_parameter("grpc_port", 9877)
        self.declare_parameter("viewer_host", os.environ.get("VIEWER_HOST", "localhost"))
        self.declare_parameter("record_rrd", True)
        # Cap the .rrd, in MB. 0 lifts the cap.
        #
        # It is the largest thing a long run writes -- 3.2 MB/min measured at
        # the 5,000-point default, so 4.6 GB/day against 0.37 GB/day for all
        # the CSVs. 500 MB is about 2.6 hours of recording, which is longer
        # than any run worth scrubbing through afterwards, and the cap costs
        # nothing measured: samples.csv and the telemetry snapshots continue.
        self.declare_parameter("record_rrd_max_mb", 500.0)
        self.declare_parameter("reliability", "best_effort")
        self.declare_parameter("qos_depth", 1)

        self.sink_kind = str(self.get_parameter("sink").value)
        if self.sink_kind not in SINKS:
            raise ValueError(f"unknown sink {self.sink_kind!r}, expected one of {SINKS}")
        self.serve = bool(self.get_parameter("serve").value)
        self.web_port = int(self.get_parameter("web_port").value)
        self.grpc_port = int(self.get_parameter("grpc_port").value)
        self.viewer_host = str(self.get_parameter("viewer_host").value)
        self.record_rrd = bool(self.get_parameter("record_rrd").value)
        self.record_rrd_max_mb = float(self.get_parameter("record_rrd_max_mb").value)
        self.reliability = str(self.get_parameter("reliability").value)
        self.qos_depth = int(self.get_parameter("qos_depth").value)

        self.sub = None
        self.sink = None
        self.frames = 0
        self.drawn = 0
        self.seq_gaps = 0
        self.last_seq: int | None = None
        self._progress_mark = 0
        self.create_timer(PROGRESS_PERIOD_S, self._log_progress)

        self._start()
        self.get_logger().info(
            f"render up ({'admin=' + self.admin.url if self.admin.enabled else 'run_id=' + str(self.run_id)}, "
            f"sink={self.sink_kind}, qos={self.reliability}/KEEP_LAST({self.qos_depth}))"
        )

    # --- run lifecycle ----------------------------------------------------

    def params(self) -> dict:
        p = {
            "sink": self.sink_kind,
            "reliability": self.reliability,
            "qos_depth": self.qos_depth,
        }
        # The node is the only thing that knows where its viewer actually is:
        # the address has to resolve in the operator's browser, which is why
        # it is built from viewer_host rather than from anything the admin
        # could infer about the container. Reported so the admin page can
        # link straight to it instead of asking the operator to read a log.
        url = getattr(self.sink, "url", None)
        if url:
            p["viewer_url"] = url
        return p

    def counters(self) -> dict:
        return {
            "frames": self.frames,
            "drawn": self.drawn,
            "seq_gaps": self.seq_gaps,
        }

    def _status_extra(self) -> dict:
        return {"subscribed": self.sub is not None}

    def start_run(self, run_id: str, args: dict | None = None) -> None:
        """Build the Recorder and the sink, then subscribe.

        The sink is built per run, not per process: a viewer session is scoped
        to one experiment, so switching runs cannot append to the previous
        one's timeline.
        """
        if self.running:
            if run_id == self.run_id:
                return
            self.stop_run()

        self.frames = 0
        self.drawn = 0
        self.seq_gaps = 0
        self.last_seq = None
        self._progress_mark = 0
        out_dir = self._make_recorder(run_id)
        self.sink = build_sink(
            self.sink_kind,
            node=self,
            run_id=run_id,
            serve=self.serve,
            web_port=self.web_port,
            grpc_port=self.grpc_port,
            viewer_host=self.viewer_host,
            rrd_path=(os.path.join(out_dir, "session.rrd") if self.record_rrd else None),
            rrd_max_mb=self.record_rrd_max_mb,
        )
        self.sub = self.create_subscription(
            CloudWithTelemetry,
            RESULT_TOPIC,
            self.on_result,
            cloud_qos(self.reliability, self.qos_depth),
        )
        # Both ways in, because the browser one is the fragile one: it needs
        # two ports reachable from the browser, WebGPU or WebGL, and the exact
        # query string. The native viewer needs a port and nothing else, so it
        # is named first — an operator reads this line and takes the first
        # thing that looks like an answer.
        where = getattr(self.sink, "url", None)
        lines = [f"render recording run {run_id} (sink={self.sink_kind})"]
        if where:
            grpc = getattr(self.sink, "grpc_uri", None)
            if grpc:
                lines.append(f"  native viewer:  rerun --port auto {grpc}")
            lines.append(f"  in a browser:   {where}")
        self.get_logger().info("\n".join(lines))

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
        report = self._close_recorder()
        self.get_logger().info(
            f"render stopped run {self.run_id}: frames={self.frames} "
            f"drawn={self.drawn} seq_gaps={self.seq_gaps} report={report}"
        )
        return report

    # --- progress ---------------------------------------------------------

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
                f"(total={self.frames}). The process is fine, and this is almost "
                f"always the edge not sending: the downlink is off by default. "
                f"Check the edge's log for '(result -> mec_cast/result)' — without "
                f"it, set PUBLISH_RESULT=1 on the EDGE role and RECREATE the "
                f"container; the flag is read once at startup, so restarting the "
                f"run cannot turn it on. If the edge IS sending, the uplink is "
                f"dropping frames — compare row counts: "
                f"wc -l runs/{self.run_id}/pub-*/samples.csv runs/{self.run_id}/edge-*/samples.csv"
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
            site=self.SITE,
            trace_id=bytes(env.trace_id),
        )
        # Parseable progress line — the launch test and humans both read it.
        # round_trip_ns is the PTP-free number; downlink_ns is not.
        self.get_logger().info(
            f"rendered seq={env.seq} n={n} "
            f"round_trip_ns={process_done_ns - env.capture_ns if process_done_ns else 0} "
            f"downlink_ns={recv_ns - env.send_ns}"
        )


def main(args=None) -> None:
    spin(RenderNode, args)


if __name__ == "__main__":
    main()
