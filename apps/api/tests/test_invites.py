"""`/v1/invites/{token}`：一次性、48 小时过期、自设密码后直接登录。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from conftest import invite_token
from mc_librarian.auth import invites
from mc_librarian.config import SESSION_COOKIE_NAME

MEMBER_PASSWORD = "member-password-1"
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _create_user(admin_client: TestClient, username: str = "alice") -> str:
    """管理员建号并返回邀请链接里的明文 token。"""
    created = admin_client.post("/v1/admin/users", json={"username": username})

    assert created.status_code == 201, created.text

    return invite_token(created.json()["invite_url"])


def test_invite_status_is_valid_before_use(admin_client: TestClient) -> None:
    token = _create_user(admin_client)

    response = admin_client.get(f"/v1/invites/{token}")

    assert response.status_code == 200
    assert response.json() == {"valid": True, "username": "alice"}


def test_invite_status_is_invalid_for_unknown_token(admin_client: TestClient) -> None:
    response = admin_client.get("/v1/invites/not-a-real-token")

    assert response.status_code == 200
    assert response.json() == {"valid": False}


def test_invite_expires_after_48_hours(admin_client: TestClient, conn: sqlite3.Connection) -> None:
    _create_user(admin_client)

    row = conn.execute("SELECT created_at, expires_at FROM invites").fetchone()
    created = datetime.strptime(row["created_at"], TIMESTAMP_FORMAT)
    expires = datetime.strptime(row["expires_at"], TIMESTAMP_FORMAT)

    assert expires - created == timedelta(hours=48)


def test_invite_token_is_not_stored_in_plaintext(
    admin_client: TestClient, conn: sqlite3.Connection
) -> None:
    token = _create_user(admin_client)

    row = conn.execute("SELECT token_hash FROM invites").fetchone()

    assert row["token_hash"] != token
    assert token not in row["token_hash"]
    assert row["token_hash"] == invites.hash_token(token)
    assert len(row["token_hash"]) == 64


def test_accept_sets_password_and_logs_in(
    admin_client: TestClient, other_client: TestClient, conn: sqlite3.Connection
) -> None:
    token = _create_user(admin_client)

    response = other_client.post(
        f"/v1/invites/{token}/accept", json={"password": MEMBER_PASSWORD}
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["username"] == "alice"
    assert body["role"] == "user"
    assert body["enabled"] is True
    assert "password_hash" not in body
    assert SESSION_COOKIE_NAME in response.headers["set-cookie"]

    # 设完密码即已登录。
    assert other_client.get("/v1/auth/me").json()["username"] == "alice"

    # 密码是用户自设的 argon2id 哈希，明文不落库。
    row = conn.execute("SELECT password_hash FROM users WHERE username = 'alice'").fetchone()
    assert row["password_hash"].startswith("$argon2id$")
    assert MEMBER_PASSWORD not in row["password_hash"]

    # 用自设的密码能重新登录。
    other_client.cookies.clear()
    relogin = other_client.post(
        "/v1/auth/login", json={"username": "alice", "password": MEMBER_PASSWORD}
    )
    assert relogin.status_code == 200


def test_invite_is_one_time(admin_client: TestClient, other_client: TestClient) -> None:
    token = _create_user(admin_client)

    first = other_client.post(f"/v1/invites/{token}/accept", json={"password": MEMBER_PASSWORD})
    assert first.status_code == 201

    other_client.cookies.clear()
    replay = other_client.post(
        f"/v1/invites/{token}/accept", json={"password": "another-password-1"}
    )

    assert replay.status_code == 404
    assert replay.headers["content-type"].startswith("application/problem+json")
    assert admin_client.get(f"/v1/invites/{token}").json() == {"valid": False}


def test_expired_invite_is_rejected(
    admin_client: TestClient, other_client: TestClient, conn: sqlite3.Connection
) -> None:
    token = _create_user(admin_client)
    conn.execute("UPDATE invites SET expires_at = '2000-01-01T00:00:00Z'")
    conn.commit()

    assert admin_client.get(f"/v1/invites/{token}").json() == {"valid": False}

    response = other_client.post(
        f"/v1/invites/{token}/accept", json={"password": MEMBER_PASSWORD}
    )

    assert response.status_code == 404
    # 过期的链接不会写密码：账号仍是不可登录的占位哈希。
    assert (
        other_client.post(
            "/v1/auth/login", json={"username": "alice", "password": MEMBER_PASSWORD}
        ).status_code
        == 401
    )


def test_accept_rejects_weak_password(admin_client: TestClient, other_client: TestClient) -> None:
    token = _create_user(admin_client)

    response = other_client.post(f"/v1/invites/{token}/accept", json={"password": "short"})

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    assert '"short"' not in response.text


def test_accept_records_audit(admin_client: TestClient, other_client: TestClient) -> None:
    token = _create_user(admin_client)
    other_client.post(f"/v1/invites/{token}/accept", json={"password": MEMBER_PASSWORD})

    entries = admin_client.get("/v1/admin/audit").json()["entries"]
    accepted = [entry for entry in entries if entry["action"] == "invite.accepted"]

    assert len(accepted) == 1
    assert accepted[0]["actor"] == "alice"
    assert accepted[0]["target"] == "alice"
    assert accepted[0]["ip"]


def test_invite_of_disabled_user_is_invalid(
    admin_client: TestClient, other_client: TestClient
) -> None:
    created = admin_client.post("/v1/admin/users", json={"username": "alice"}).json()
    token = invite_token(created["invite_url"])

    admin_client.patch(f"/v1/admin/users/{created['user']['id']}", json={"enabled": False})

    assert admin_client.get(f"/v1/invites/{token}").json() == {"valid": False}
    assert (
        other_client.post(
            f"/v1/invites/{token}/accept", json={"password": MEMBER_PASSWORD}
        ).status_code
        == 404
    )
