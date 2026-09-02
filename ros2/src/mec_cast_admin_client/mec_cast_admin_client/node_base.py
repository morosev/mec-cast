"""The shared skeleton of every mec-cast measurement node.

Before this existed, ``publisher_node.py``, ``edge_node.py`` and
``render_node.py`` each carried a near-verbatim copy of the same ~120 lines:
the admin bootstrap, command draining and dispatch, status reporting, the
goodbye path, and ``main()``. None of that duplication was defended by a
comment, unlike ``cloud_qos()`` and ``make_pointcloud2()`` — those model the
UE/edge deployment boundary, are duplicated on purpose, and stay in the nodes.

What a subclass supplies:

* class attributes: ``NODE_TYPE`` (a ``protocol.NodeType`` value), ``SERVICE``
  (the logging-service identity, e.g. ``mec-cast-pub``), ``SITE`` (see
  ``sites.py``), and optionally ``PEER_TOPIC`` for graph-derived peers.
* ``params() -> dict`` — the workload knobs it reports.
* ``counters() -> dict`` — what its status counters say.
* ``start_run(run_id, args=None)`` / ``stop_run() -> dict`` — using
  ``_make_recorder()`` / ``_close_recorder()`` for the shared parts.
* a call to ``self._start()`` at the END of its ``__init__``, once its own
  publishers/parameters exist.

Instance identity (M1): every node carries an ``instance`` (the
``admin_instance`` parameter). It suffixes the node_id (via AdminClient), the
output directory (``pub-0``, ``pub-1``…) and the logging-service tag
(``mec-cast-pub-0``…), so N instances of one type on one host stay separate
end to end. The directory leaf travels to the admin inside ``params`` as
``out_leaf`` — a plain params entry, so the wire protocol is untouched.

Standalone first: with no ``admin_url`` the node starts immediately from its
``run_id`` parameter (default: ``$RUN_ID``, then ``dev-run``). Every
environment variable this base reads has a ``-p`` equivalent, so a laptop run
needs no exports at all.
"""

from __future__ import annotations

import os
import signal
import socket

import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node

import mec_cast_telemetry as tel

from .client import AdminClient
from . import protocol as ap

#: How often admin commands are applied. Everything the admin asks for
#: happens on the executor thread, in this callback.
ADMIN_POLL_S = 0.1
#: How often status is pushed when nothing else changed. Doubles as the
#: liveness signal, so the admin need not rely on pong alone.
STATUS_PERIOD_S = 2.0


class MecCastNode(Node):
    """Base for lidar / edge / render nodes. See the module docstring."""

    NODE_TYPE: str = ""
    SERVICE: str = ""
    SITE: int = -1
    #: Topic whose publishers count as this node's peers, or None.
    PEER_TOPIC: str | None = None
    #: Directory leaf before the instance suffix ("pub", "edge", "render").
    OUT_LEAF: str = ""

    def __init__(
        self,
        default_name: str,
        *,
        node_name: str | None = None,
        parameter_overrides: list | None = None,
        autostart_default: bool = True,
    ) -> None:
        super().__init__(
            node_name or default_name, parameter_overrides=parameter_overrides
        )
        # Shared parameters. Environment variables are demoted to defaults so
        # everything is settable from the command line (`-p run_id:=...`).
        # `or` rather than a get() default: these arrive SET BUT EMPTY from
        # compose (`RUN_ID: ${RUN_ID:-}`), and get() only substitutes when a
        # variable is absent. An empty run_id put the recorder in
        # `runs/pub-0` — outside any run directory, quietly merging every
        # unnamed run into one. Empty is meaningless for both of these, so
        # treat it as unset. It is NOT meaningless for admin_url, logging_url
        # or cell below, where empty is a real choice.
        self.declare_parameter("run_id", os.environ.get("RUN_ID") or "dev-run")
        self.declare_parameter("runs_dir", os.environ.get("RUNS_DIR") or "runs")
        self.declare_parameter("logging_url", os.environ.get("LOGGING_URL", ""))
        self.declare_parameter("admin_url", os.environ.get("ADMIN_URL", ""))
        self.declare_parameter("cell", os.environ.get("CELL", ""))
        # Which PHC to judge clock health against. NOT hardcoded to /dev/ptp0:
        # index 0 is whichever NIC the driver registered first, which on a
        # multi-NIC host is routinely not the one ptp4l disciplines. Empty
        # disables the monitor honestly rather than reporting on an unrelated
        # free-running crystal.
        self.declare_parameter("ptp_device", os.environ.get("PTP_DEVICE", ""))
        self.declare_parameter("admin_autostart", autostart_default)
        self.declare_parameter("admin_instance", 0)

        self.runs_dir = str(self.get_parameter("runs_dir").value)
        self.ptp_device = str(self.get_parameter("ptp_device").value)
        self.logging_url = str(self.get_parameter("logging_url").value) or None
        self.instance = int(self.get_parameter("admin_instance").value)
        self.autostart = bool(self.get_parameter("admin_autostart").value)

        self.run_id: str | None = None
        self.recorder: tel.Recorder | None = None
        self._last_status: dict | None = None

        self.admin = AdminClient(
            node_type=self.NODE_TYPE,
            host=socket.gethostname(),
            url=str(self.get_parameter("admin_url").value),
            cell=str(self.get_parameter("cell").value),
            instance=self.instance,
            version_sha=os.environ.get("VCS_REF", ""),
            version_tag=os.environ.get("VERSION", ""),
            pid=os.getpid(),
        )

    # --- identity ---------------------------------------------------------

    @property
    def out_leaf(self) -> str:
        """`pub-0`, `edge-0`, `render-1`… — always instance-suffixed, so the
        layout is uniform whether one instance runs or ten."""
        return f"{self.OUT_LEAF}-{self.instance}"

    @property
    def service_tag(self) -> str:
        """The logging-service `service` identity, instance-suffixed for the
        same reason: sibling instances must stay distinguishable in Postgres."""
        return f"{self.SERVICE}-{self.instance}"

    def params(self) -> dict:  # override
        return {}

    def counters(self) -> dict:  # override
        return {}

    def _params_full(self) -> dict:
        """What actually travels to the admin: the node's own knobs plus its
        output leaf. `out_leaf` rides inside params deliberately — params is
        an open dict on the wire, so the admin learns the CSV path without a
        protocol change (the alternative, deriving the path from node_type,
        silently dropped every instance after the first)."""
        return {**self.params(), "out_leaf": self.out_leaf}

    # --- run lifecycle helpers -------------------------------------------

    @property
    def running(self) -> bool:
        return self.recorder is not None

    def _make_recorder(self, run_id: str) -> str:
        """Create the per-run Recorder; returns the out_dir used."""
        self.run_id = run_id
        out_dir = os.path.join(self.runs_dir, run_id, self.out_leaf)
        self.recorder = tel.Recorder(
            run_id,
            self.service_tag,
            out_dir,
            logging_url=self.logging_url,
            ptp_device=self.ptp_device or None,
        )
        # A configured device that did not open leaves `ptp.reliable` false
        # for a reason the snapshot cannot express, and an unannotated false
        # reads as "no PTP here" rather than "misconfigured". Say which.
        if self.ptp_device and not self.recorder.ptp_enabled:
            self.get_logger().warn(
                f"PTP device {self.ptp_device!r} could not be opened — clock "
                "quality will be reported as unavailable. Check the device "
                "exists, is the one ptp4l disciplines (ethtool -T <iface>), "
                "and is readable in this container."
            )
        return out_dir

    def _close_recorder(self) -> dict:
        report = self.recorder.shutdown()
        self.recorder = None
        return report

    def start_run(self, run_id: str, args: dict | None = None) -> None:  # override
        raise NotImplementedError

    def stop_run(self) -> dict:  # override
        raise NotImplementedError

    # --- bootstrap --------------------------------------------------------

    def _start(self) -> None:
        """Called by the subclass at the end of its __init__.

        With an admin: register, start the socket thread, poll for commands.
        Without: the run_id parameter names the run and recording starts now —
        the standalone path, which must keep working with no admin and no
        environment at all.
        """
        if self.admin.enabled:
            self.admin.update_identity(
                autostart=self.autostart, params=self._params_full()
            )
            self.admin.start()
            self.create_timer(ADMIN_POLL_S, self._drain_admin)
            self.create_timer(STATUS_PERIOD_S, self._report_status)
            self.get_logger().info(
                f"{self.get_name()} up, "
                f"{'idle' if not self.autostart else 'joining active run'} "
                f"(admin={self.admin.url}, node_id={self.admin.node_id})"
            )
        else:
            # Belt and braces: a `-p run_id:=` with an empty value would land
            # here too, and the failure is silent rather than loud.
            run_id = str(self.get_parameter("run_id").value) or "dev-run"
            self.start_run(run_id)

    # --- admin plumbing ---------------------------------------------------

    def _drain_admin(self) -> None:
        """Apply whatever the admin asked for, on the executor thread."""
        for frame in self.admin.poll():
            payload = frame.get("payload") or {}
            if frame["type"] == ap.MessageType.WELCOME:
                active = payload.get("active_run")
                if active and self.autostart:
                    self.start_run(active["run_id"], active.get("params"))
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
                self.start_run(run_id, payload.get("args"))
            elif command in (ap.CommandType.RUN_STOP, ap.CommandType.STREAM_STOP):
                stop_report = self.stop_run()
            elif command != ap.CommandType.STATUS_REPORT:
                raise ValueError(f"unknown command {command!r}")
        except Exception as exc:  # a bad command must not kill the node
            ok, error = False, str(exc)
            self.get_logger().error(f"admin command {command} failed: {exc}")
        self.admin.publish_ack(frame["msg_id"], ok=ok, error=error)
        # The report travels with the status: an admin-driven stop leaves this
        # process alive, so it must not wait for the goodbye frame.
        self._report_status(force=True, report=stop_report)

    def peers(self) -> list[dict]:
        """Who is publishing PEER_TOPIC to us, from the ROS graph.

        The graph already knows, so this needs no change to the wire format —
        `CloudWithTelemetry` carries no sender identity by design, since
        `TimingEnvelope` is a pinned 64-byte contract shared with the C ABI.
        """
        if self.PEER_TOPIC is None or not self.running:
            return []
        try:
            infos = self.get_publishers_info_by_topic(self.PEER_TOPIC)
        except Exception:
            return []
        return [ap.peer(info.node_name or "unknown") for info in infos]

    def _status_extra(self) -> dict:  # override for streaming/subscribed
        return {}

    def _clock_counters(self) -> dict:
        """Negative derived delays seen by this node's recorder.

        A one-way delay below zero means the sending host's clock is AHEAD of
        this one's, so every cross-host figure is wrong by the skew. Reported
        for every node type rather than added to each `counters()`, because a
        node that forgot to include it would look healthy while its numbers
        were nonsense -- and this is the one signal that says they are.
        """
        if self.recorder is None:
            return {}
        try:
            return {"negative_delays": int(self.recorder.negative_delays())}
        except AttributeError:
            # An older telemetry wheel without the counter: report nothing
            # rather than crash a measuring node over a diagnostic.
            return {}

    def _report_status(self, force: bool = False, report: dict | None = None) -> None:
        if not self.admin.enabled:
            return
        payload = ap.status_payload(
            node_type=self.NODE_TYPE,
            state=ap.NodeState.RUNNING if self.running else ap.NodeState.IDLE,
            run_id=self.run_id,
            peers=self.peers(),
            params=self._params_full(),
            counters={**self.counters(), **self._clock_counters()},
            autostart=self.autostart,
            report=report or {},
            **self._status_extra(),
        )
        # Status on every change, per the protocol — but a periodic resend
        # doubles as liveness, so an unchanged payload still goes out on the
        # timer rather than being suppressed entirely.
        if force or payload != self._last_status:
            self._last_status = payload
            self.admin.update_identity(
                state=payload["state"], run_id=self.run_id, params=self._params_full()
            )
            self.admin.publish_status(payload)

    # --- shutdown ---------------------------------------------------------

    def finish(self) -> None:
        """Stop producing, drain, then say goodbye. Tolerant of no active run:
        an admin-driven node may be idle when it is told to exit."""
        report = self.stop_run()
        self.admin.goodbye(reason="shutdown", run_id=self.run_id, final_report=report)
        self.get_logger().info(f"{self.get_name()} done: report={report}")


def spin(make_node, args=None) -> None:
    """The shared main(): one node, one SIGTERM handler, a drain on the way
    out. `make_node` is a zero-arg callable so construction happens after
    rclpy.init."""
    rclpy.init(args=args)
    # docker stop sends SIGTERM: exit spin cleanly so the recorder flushes.
    signal.signal(signal.SIGTERM, lambda *_: rclpy.shutdown())
    node = make_node()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.finish()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
