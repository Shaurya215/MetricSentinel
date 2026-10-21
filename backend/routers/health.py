"""Health router (SYSTEM_ARCHITECTURE.md §6: `GET /health`, public)."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from core.settings import Settings, get_settings

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    """Liveness payload. Dependency checks are added with the database in Phase 6."""

    status: str
    app: str
    version: str
    env: str


@router.get("/health", response_model=HealthResponse, summary="Liveness check")
def health() -> HealthResponse:
    settings: Settings = get_settings()
    return HealthResponse(
        status="ok",
        app=settings.app_name,
        version=settings.app_version,
        env=settings.app_env,
    )
