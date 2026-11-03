"""`/v1/auth/*` 行为测试：登录、会话持久、登出。"""

from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from conftest import ADMIN_PASSWORD, ADMIN_USERNAME, session_cookie
from mc_librarian.config import SESSION_COOKIE_NAME

LOGIN_FAILURE_DETAIL = "用户名或密码不正确"


def test_login_success_returns_user_and_cookie(admin_client: TestClient) -> None:
    admin_client.cookies.clear()  # 丢掉 setup 下发的 cookie，走一次真正的登录

    response = admin_client.post(
        "/v1/auth/login",
        json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["username"] == ADMIN_USERNAME
    assert body["role"] == "admin"
    assert body["last_login_at"] is not None
    assert "password_hash" not in body
    assert SESSION_COOKIE_NAME in response.headers["set-cookie"]


def test_login_updates_last_login_at(client: TestClient, conn: sqlite3.Connection) -> None:
    client.post("/v1/setup", json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD})

    before = conn.execute("SELECT last_login_at FROM users").fetchone()["last_login_at"]
    assert before is None

    client.post(
        "/v1/auth/login",
        json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
    )

    after = conn.execute("SELECT last_login_at FROM users").fetchone()["last_login_at"]
    assert after is not None
    assert after.endswith("Z")


def test_login_with_wrong_password_is_401(admin_client: TestClient) -> None:
    response = admin_client.post(
        "/v1/auth/login",
        json={"username": ADMIN_USERNAME, "password": "wrong-password-1"},
    )

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["detail"] == LOGIN_FAILURE_DETAIL
    assert SESSION_COOKIE_NAME not in response.headers.get("set-cookie", "")


def test_login_failure_text_does_not_reveal_user_existence(admin_client: TestClient) -> None:
    unknown = admin_client.post(
        "/v1/auth/login",
        json={"username": "nobody-here", "password": "whatever-password"},
    )
    wrong_password = admin_client.post(
        "/v1/auth/login",
        json={"username": ADMIN_USERNAME, "password": "wrong-password-1"},
    )

    assert unknown.status_code == wrong_password.status_code == 401
    assert unknown.json()["detail"] == wrong_password.json()["detail"] == LOGIN_FAILURE_DETAIL
    assert unknown.json()["title"] == wrong_password.json()["title"]


def test_session_persists_across_requests(admin_client: TestClient) -> None:
    """同一个 cookie 再请求 /v1/auth/me 仍然认得出来（刷新页面不掉线）。"""
    raw_token = session_cookie(admin_client)

    for _ in range(3):
        me = admin_client.get(
            "/v1/auth/me",
            headers={"Cookie": f"{SESSION_COOKIE_NAME}={raw_token}"},
        )
        assert me.status_code == 200
        assert me.json()["username"] == ADMIN_USERNAME


def test_me_without_cookie_is_401(client: TestClient) -> None:
    client.post("/v1/setup", json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD})
    client.cookies.clear()

    response = client.get("/v1/auth/me")

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")


def test_me_with_forged_cookie_is_401(client: TestClient) -> None:
    client.post("/v1/setup", json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD})

    response = client.get(
        "/v1/auth/me",
        headers={"Cookie": f"{SESSION_COOKIE_NAME}=forged-token"},
    )
    assert response.status_code == 401


def test_logout_invalidates_session(admin_client: TestClient) -> None:
    raw_token = session_cookie(admin_client)
    cookie_header = {"Cookie": f"{SESSION_COOKIE_NAME}={raw_token}"}

    assert admin_client.get("/v1/auth/me", headers=cookie_header).status_code == 200

    logout = admin_client.post("/v1/auth/logout", headers=cookie_header)
    assert logout.status_code == 204

    # 旧 cookie 再用即 401：服务端会话行已删除，不是靠前端清 cookie。
    assert admin_client.get("/v1/auth/me", headers=cookie_header).status_code == 401


def test_logout_without_session_is_401(client: TestClient) -> None:
    response = client.post("/v1/auth/logout")

    assert response.status_code == 401


def test_logout_twice_is_401(admin_client: TestClient) -> None:
    raw_token = session_cookie(admin_client)
    cookie_header = {"Cookie": f"{SESSION_COOKIE_NAME}={raw_token}"}

    assert admin_client.post("/v1/auth/logout", headers=cookie_header).status_code == 204
    assert admin_client.post("/v1/auth/logout", headers=cookie_header).status_code == 401


def test_login_after_logout_works(admin_client: TestClient) -> None:
    admin_client.post("/v1/auth/logout")
    admin_client.cookies.clear()

    response = admin_client.post(
        "/v1/auth/login",
        json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
    )

    assert response.status_code == 200
    assert admin_client.get("/v1/auth/me").status_code == 200


def test_disabled_user_cannot_login(client: TestClient, conn: sqlite3.Connection) -> None:
    client.post("/v1/setup", json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD})
    conn.execute("UPDATE users SET enabled = 0")
    conn.commit()

    response = client.post(
        "/v1/auth/login",
        json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
    )

    # 同样是 401 + 同一句文案：不暴露「账号被禁用」。
    assert response.status_code == 401
    assert response.json()["detail"] == LOGIN_FAILURE_DETAIL


def test_disabled_user_session_is_rejected(client: TestClient, conn: sqlite3.Connection) -> None:
    client.post("/v1/setup", json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD})
    raw_token = session_cookie(client)

    conn.execute("UPDATE users SET enabled = 0")
    conn.commit()

    response = client.get(
        "/v1/auth/me",
        headers={"Cookie": f"{SESSION_COOKIE_NAME}={raw_token}"},
    )

    assert response.status_code == 401


def test_expired_session_is_rejected(client: TestClient, conn: sqlite3.Connection) -> None:
    client.post("/v1/setup", json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD})
    raw_token = session_cookie(client)

    conn.execute("UPDATE sessions SET expires_at = '2000-01-01T00:00:00Z'")
    conn.commit()

    response = client.get(
        "/v1/auth/me",
        headers={"Cookie": f"{SESSION_COOKIE_NAME}={raw_token}"},
    )

    assert response.status_code == 401
