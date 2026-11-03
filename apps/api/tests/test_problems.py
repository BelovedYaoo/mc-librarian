"""错误格式：框架自己抛的 404 / 405 也必须统一成 RFC 7807 `application/problem+json`。"""

from __future__ import annotations

from fastapi.testclient import TestClient

PROBLEM_JSON = "application/problem+json"


def test_unknown_path_is_problem_json(client: TestClient) -> None:
    response = client.get("/v1/does-not-exist")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith(PROBLEM_JSON)

    problem = response.json()
    assert problem["status"] == 404
    assert problem["title"]
    assert problem["type"] == "about:blank"
    assert problem["instance"] == "/v1/does-not-exist"


def test_wrong_method_is_problem_json_and_keeps_allow_header(client: TestClient) -> None:
    """405 必须留 `Allow`，否则调用方不知道该用什么方法。"""
    response = client.get("/v1/auth/login")

    assert response.status_code == 405
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    assert "POST" in response.headers["allow"]

    problem = response.json()
    assert problem["status"] == 405
    assert problem["title"]


def test_validation_error_is_problem_json(client: TestClient) -> None:
    """422 走的是 RequestValidationError 处理器，且不能把请求体（可能含密码）回显。"""
    response = client.post(
        "/v1/auth/login",
        json={"username": "admin", "password": "leak-7c"},
    )

    assert response.status_code == 422
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    assert response.json()["status"] == 422
    assert "leak-7c" not in response.text


def test_our_own_problem_exception_keeps_its_own_title(client: TestClient) -> None:
    """`ProblemException` 的标题与 detail 不被框架默认文案覆盖。"""
    client.post("/v1/setup", json={"username": "admin", "password": "admin-password-1"})

    response = client.post(
        "/v1/setup",
        json={"username": "another", "password": "another-password"},
    )

    problem = response.json()
    assert problem["status"] == 410
    assert problem["title"] == "已完成初始化"
    assert problem["detail"]
