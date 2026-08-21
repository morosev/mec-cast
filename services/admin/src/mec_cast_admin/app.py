"""Application factory and lifecycle."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import health_router, runs_router, ws_router
from .config import Settings, get_settings
from .orchestrator import Orchestrator
from .store import JsonRunStore

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI application."""
    settings = settings or get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        orchestrator: Orchestrator = app.state.orchestrator
        await orchestrator.start()
        try:
            yield
        finally:
            await orchestrator.stop()

    app = FastAPI(
        title="mec-cast admin service",
        description=(
            "Run orchestration for mec-cast. Nodes subscribe over WebSocket, report "
            "status, and take start and stop commands; the operator page shows the run "
            "table and what to do when the workflow is not established."
        ),
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.orchestrator = Orchestrator(settings, JsonRunStore(settings.runs_dir))

    app.include_router(health_router)
    app.include_router(runs_router, prefix=settings.api_prefix)
    app.include_router(ws_router)

    # Plain files, no build step and nothing fetched at runtime, so the page
    # works in an air-gapped lab. Same arrangement as the logging dashboard.
    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/admin", include_in_schema=False)
        async def admin_page() -> FileResponse:
            return FileResponse(STATIC_DIR / "admin.html")

        @app.get("/", include_in_schema=False)
        async def index() -> RedirectResponse:
            return RedirectResponse(url="/admin")
    else:
        logger.warning("static assets missing at %s; operator page disabled", STATIC_DIR)

    return app


app = create_app()
