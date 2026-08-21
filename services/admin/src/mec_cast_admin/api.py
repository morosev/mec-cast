"""HTTP routes and the two WebSocket endpoints."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect, status

from . import __version__
from . import protocol as p
from .dependencies import OrchestratorDep
from .orchestrator import Orchestrator, OrchestratorError
from .schemas import HealthResponse, RunCreate, StateResponse
from .state import Action

logger = logging.getLogger(__name__)

health_router = APIRouter(tags=["health"])
runs_router = APIRouter(tags=["runs"])
ws_router = APIRouter()


@health_router.get("/health", response_model=HealthResponse, summary="Liveness")
async def health() -> HealthResponse:
    return HealthResponse(status="ok", version=__version__, protocol=p.PROTOCOL_VERSION)


@health_router.get("/health/ready", response_model=HealthResponse, summary="Readiness")
async def readiness() -> HealthResponse:
    # Nothing external to be ready for: no database, and nodes are expected to
    # come and go. The service is ready as soon as it is listening.
    return HealthResponse(status="ok", version=__version__, protocol=p.PROTOCOL_VERSION)


@runs_router.get("/state", response_model=StateResponse, summary="The whole operator view")
async def get_state(orchestrator: OrchestratorDep) -> StateResponse:
    """Polling fallback for the UI when its WebSocket is down."""
    return StateResponse(**orchestrator.snapshot())


@runs_router.post(
    "/runs",
    status_code=status.HTTP_201_CREATED,
    summary="Create a run",
)
async def create_run(body: RunCreate, orchestrator: OrchestratorDep) -> dict:
    run = orchestrator.create_run(label=body.label, params=body.params)
    return {**run.to_dict(), "allowed": ["start", "remove"]}


async def _act(orchestrator: Orchestrator, run_id: str, action: Action) -> dict:
    try:
        run = await orchestrator.act(run_id, action)
    except OrchestratorError as exc:
        # 409, not 400: the request is well-formed, the run is in the wrong
        # state. The body carries the reason so the page can show it verbatim.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    from .state import allowed_actions

    return {**run.to_dict(), "allowed": allowed_actions(run.state)}


@runs_router.post("/runs/{run_id}/start", summary="Start a run")
async def start_run(run_id: str, orchestrator: OrchestratorDep) -> dict:
    return await _act(orchestrator, run_id, Action.START)


@runs_router.post("/runs/{run_id}/stop", summary="Stop a run")
async def stop_run(run_id: str, orchestrator: OrchestratorDep) -> dict:
    return await _act(orchestrator, run_id, Action.STOP)


@runs_router.delete("/runs/{run_id}", summary="Remove a run from the table")
async def remove_run(run_id: str, orchestrator: OrchestratorDep) -> dict:
    """Removes the row. Measurement data on disk is never deleted."""
    return await _act(orchestrator, run_id, Action.REMOVE)


# --- WebSocket -------------------------------------------------------------


def _orchestrator_of(websocket: WebSocket) -> Orchestrator:
    return websocket.app.state.orchestrator


@ws_router.websocket("/ws/node")
async def node_socket(websocket: WebSocket) -> None:
    """A node's connection. The first frame must be `hello`.

    Errors are answered with an `error` frame rather than a silent close: a
    node that cannot be understood must be able to say so in its own log.
    """
    orchestrator = _orchestrator_of(websocket)
    await websocket.accept()
    node_id: str | None = None

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                envelope, payload = p.parse(json.loads(raw))
            except (ValueError, p.ProtocolError) as exc:
                await websocket.send_text(
                    json.dumps(
                        p.build(
                            p.MessageType.ERROR,
                            p.ErrorPayload(code="bad_frame", message=str(exc)),
                        )
                    )
                )
                if isinstance(exc, p.ProtocolError) and "version" in str(exc):
                    await websocket.close(code=1008)
                    return
                continue

            if envelope.type not in p.NODE_TO_ADMIN:
                await websocket.send_text(
                    json.dumps(
                        p.build(
                            p.MessageType.ERROR,
                            p.ErrorPayload(
                                code="wrong_direction",
                                message=f"{envelope.type} is an admin-to-node message",
                            ),
                        )
                    )
                )
                continue

            if envelope.type is p.MessageType.HELLO:
                node_id = payload.node_id
                welcome = await orchestrator.on_hello(payload, websocket)
                await websocket.send_text(json.dumps(welcome))
                continue

            if node_id is None:
                await websocket.send_text(
                    json.dumps(
                        p.build(
                            p.MessageType.ERROR,
                            p.ErrorPayload(code="no_hello", message="send hello first"),
                        )
                    )
                )
                continue

            orchestrator.registry.touch(node_id)

            if envelope.type is p.MessageType.STATUS:
                await orchestrator.on_status(node_id, payload)
            elif envelope.type is p.MessageType.GOODBYE:
                await orchestrator.on_goodbye(node_id, payload)
                break
            # ack and pong need nothing beyond the touch above.

    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("node socket failed (node_id=%s)", node_id)
    finally:
        if node_id is not None:
            await orchestrator.detach_node(node_id, websocket)


@ws_router.websocket("/ws/ui")
async def ui_socket(websocket: WebSocket) -> None:
    """The browser's connection. Push-only: full snapshots, no diffing."""
    orchestrator = _orchestrator_of(websocket)
    await websocket.accept()
    await orchestrator.attach_ui(websocket)
    try:
        await websocket.send_text(json.dumps(orchestrator.snapshot()))
        while True:
            # The UI sends nothing; this is how we notice it has gone.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.debug("ui socket closed", exc_info=True)
    finally:
        await orchestrator.detach_ui(websocket)
