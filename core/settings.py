"""Application settings, loaded from the environment (see `.env.example`).

Every value has a safe local default, so the skeleton runs without a `.env`
file. Secrets are typed as `SecretStr` and are never logged.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Typed configuration for every layer of MetricSentinel."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- Application ----
    app_env: Literal["local", "ci", "demo"] = "local"
    app_name: str = "MetricSentinel"
    app_version: str = "0.0.1"
    debug: bool = False

    # ---- Logging (NFR-017: JSON logs with a run id on every line) ----
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "console"] = "json"

    # ---- API ----
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # ---- UI ----
    ui_port: int = 8501

    # ---- Application database: PostgreSQL + pgvector ----
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "metricsentinel"
    postgres_user: str = "metricsentinel"
    postgres_password: SecretStr = SecretStr("metricsentinel")

    # ---- Analytics warehouse: DuckDB (opened read-only by agent tools, NFR-009) ----
    duckdb_path: Path = REPO_ROOT / "data" / "warehouse" / "warehouse.duckdb"

    # ---- Experiment tracking (used from Phase 3) ----
    mlflow_tracking_uri: str = f"file://{(REPO_ROOT / 'mlruns').as_posix()}"

    # ---- Models (used from Phase 4; unset means that provider is unavailable) ----
    gemini_api_key: SecretStr | None = None
    ollama_base_url: str = "http://localhost:11434"
    offline_mode: bool = Field(
        default=False,
        description="NFR-003: run the whole loop with local models only.",
    )

    @property
    def database_url(self) -> str:
        """SQLAlchemy URL for the application database.

        Deliberately a plain property, not a `computed_field`: a computed field
        is included in `repr()` and `model_dump()`, which would leak the
        password into any log line that dumps the settings object.
        """
        pwd = self.postgres_password.get_secret_value()
        return (
            f"postgresql+psycopg://{self.postgres_user}:{pwd}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings, read from the environment once."""
    return Settings()
