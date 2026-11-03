"""`/v1/health` 行为测试。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from mc_librarian import __version__


def test_health_returns_ok(client: TestClient) -> None:
    response = client.get("/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}


def test_health_is_public(client: TestClient) -> None:
    """契约里 security: []，前端空壳页面未登录也能读到状态。"""
    payload = client.get("/v1/health").json()

    assert payload["status"] == "ok"
    assert set(payload) == {"status", "version"}


def test_openapi_json_is_served(client: TestClient) -> None:
    response = client.get("/openapi.json")

    assert response.status_code == 200
    spec = response.json()
    assert spec["info"]["version"] == __version__
    assert "/v1/health" in spec["paths"]
    assert spec["paths"]["/v1/health"]["get"]["operationId"] == "getHealth"
