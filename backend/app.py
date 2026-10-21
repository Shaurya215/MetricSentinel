"""FastAPI application factory.

Phase 1 skeleton: only the `health` router exists. The `auth`, `kpis`,
`anomalies`, `investigations`, `reports` and `admin` routers listed in
SYSTEM_ARCHITECTURE.md §6 are added in Phase 6.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from backend.routers import health
from core.logging import configure_logging, get_logger, new_run_id, run_context
from core.settings import get_settings

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging()
    with run_context(new_run_id(), component="api"):
        log.info("api_started", env=settings.app_env, version=settings.app_version)
        yield
        log.info("api_stopped")


def create_app() -> FastAPI:
    """Build the API application."""
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        summary="Autonomous KPI anomaly detection and root-cause investigation.",
        lifespan=lifespan,
    )
    app.include_router(health.router)
    return app


app = create_app()
