"""The /health endpoint must return HTTP 200 (Phase 1 Definition of Done)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.mark.unit
def test_health_returns_200(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200


@pytest.mark.unit
def test_health_payload(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["app"] == "MetricSentinel"
    assert body["env"] in {"local", "ci", "demo"}
