"""Shared fixtures."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app


@pytest.fixture
def client() -> Iterator[TestClient]:
    """A test client for the API application."""
    with TestClient(create_app()) as c:
        yield c
