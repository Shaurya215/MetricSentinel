"""Settings load with safe defaults and never expose secrets by accident."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from core.settings import Settings


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests must not depend on the machine: CI sets APP_ENV, developers have a .env."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.setitem(Settings.model_config, "env_file", None)


@pytest.mark.unit
def test_defaults_are_usable() -> None:
    settings = Settings()
    assert settings.app_env == "local"
    assert settings.api_port == 8000
    assert settings.duckdb_path.name == "warehouse.duckdb"


@pytest.mark.unit
def test_database_url_is_built_from_parts() -> None:
    settings = Settings(postgres_host="postgres", postgres_db="ms_test")
    assert settings.database_url.startswith("postgresql+psycopg://")
    assert "@postgres:5432/ms_test" in settings.database_url


@pytest.mark.unit
def test_password_is_not_in_repr() -> None:
    settings = Settings(postgres_password=SecretStr("hunter2"))
    assert "hunter2" not in repr(settings)
    assert settings.postgres_password.get_secret_value() == "hunter2"


@pytest.mark.unit
def test_environment_overrides_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    assert Settings().log_level == "DEBUG"


@pytest.mark.unit
def test_password_is_not_in_model_dump() -> None:
    """A settings dump must not carry the password, directly or via a URL."""
    dumped = repr(Settings(postgres_password=SecretStr("hunter2")).model_dump())
    assert "hunter2" not in dumped
