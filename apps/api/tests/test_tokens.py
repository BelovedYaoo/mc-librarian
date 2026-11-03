"""`/v1/auth/tokens`：个人访问令牌的签发、列表、吊销与 Bearer 鉴权。"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi.testclient import TestClient

from conftest import ADMIN_USERNAME
from mc_librarian.auth import tokens


def _create_token(client: TestClient, *, name: str = "ci", days: int = 30) -> dict[str, Any]:
    response = client.post("/v1/auth/tokens", json={"name": name, "expires_in_days": days})

    assert response.status_code == 201, response.text

    data: dict[str, Any] = response.json()

    return data


def _bearer(raw_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw_token}"}


def test_create_returns_plaintext_and_stores_only_hash(
    admin_client: TestClient, conn: sqlite3.Connection
) -> None:
    created = _create_token(admin_client)
    raw_token = created["token"]

    assert isinstance(raw_token, str)
    assert raw_token.startswith("mcl_")
    assert created["info"]["prefix"] == raw_token[:8]

    row = conn.execute("SELECT token_hash FROM pats").fetchone()

    assert row["token_hash"] == tokens.hash_token(raw_token)
    assert raw_token not in row["token_hash"]


def test_list_never_returns_plaintext(admin_client: TestClient) -> None:
    created = _create_token(admin_client, name="脚本")
    raw_token = created["token"]

    response = admin_client.get("/v1/auth/tokens")

    assert response.status_code == 200
    assert raw_token not in response.text

    listed = response.json()["tokens"]
    assert len(listed) == 1

    entry = listed[0]
    assert entry["name"] == "脚本"
    assert entry["prefix"] == raw_token[:8]
    assert "token" not in entry
    assert "token_hash" not in entry


def test_pat_authenticates_as_the_same_user(admin_client: TestClient) -> None:
    created = _create_token(admin_client)

    admin_client.cookies.clear()  # 只留 Bearer，证明 PAT 能单独使用

    response = admin_client.get("/v1/auth/me", headers=_bearer(created["token"]))

    assert response.status_code == 200
    assert response.json()["username"] == ADMIN_USERNAME
    # 角色跟人走：admin 的 PAT 仍是 admin（可调用 admin 接口）。
    assert response.json()["role"] == "admin"


def test_revoked_token_is_immediately_401(admin_client: TestClient) -> None:
    created = _create_token(admin_client)
    raw_token = created["token"]
    token_id = created["info"]["id"]

    assert admin_client.get("/v1/auth/me", headers=_bearer(raw_token)).status_code == 200

    revoked = admin_client.delete(f"/v1/auth/tokens/{token_id}")

    assert revoked.status_code == 204
    assert admin_client.get("/v1/auth/me", headers=_bearer(raw_token)).status_code == 401
    # 吊销后不再出现在列表里。
    assert admin_client.get("/v1/auth/tokens").json()["tokens"] == []


def test_expired_token_is_401(admin_client: TestClient, conn: sqlite3.Connection) -> None:
    created = _create_token(admin_client)

    conn.execute("UPDATE pats SET expires_at = '2000-01-01T00:00:00Z'")
    conn.commit()

    assert admin_client.get("/v1/auth/me", headers=_bearer(created["token"])).status_code == 401


def test_disabled_user_token_is_401(admin_client: TestClient, conn: sqlite3.Connection) -> None:
    created = _create_token(admin_client)

    conn.execute("UPDATE users SET enabled = 0")
    conn.commit()

    assert admin_client.get("/v1/auth/me", headers=_bearer(created["token"])).status_code == 401


def test_using_token_updates_last_used_at(admin_client: TestClient) -> None:
    created = _create_token(admin_client)
    token_id = created["info"]["id"]

    before = admin_client.get("/v1/auth/tokens").json()["tokens"]
    assert next(item for item in before if item["id"] == token_id)["last_used_at"] is None

    assert admin_client.get("/v1/auth/me", headers=_bearer(created["token"])).status_code == 200

    after = admin_client.get("/v1/auth/tokens").json()["tokens"]
    assert next(item for item in after if item["id"] == token_id)["last_used_at"] is not None


def test_token_lifecycle_records_audit(admin_client: TestClient, conn: sqlite3.Connection) -> None:
    """PAT 签发/吊销各落一条审计，`target` 用可辨认的前缀（不是不透明的 id）。"""
    admin = admin_client.get("/v1/auth/me").json()
    created = _create_token(admin_client, name="脚本")

    assert admin_client.delete(f"/v1/auth/tokens/{created['info']['id']}").status_code == 204

    rows = conn.execute(
        "SELECT actor_id, action, target, detail_json FROM audit ORDER BY created_at, rowid"
    ).fetchall()

    assert [row["action"] for row in rows] == ["token.created", "token.revoked"]
    assert all(row["actor_id"] == admin["id"] for row in rows)
    assert all(row["target"] == created["info"]["prefix"] for row in rows)
    assert rows[0]["detail_json"] == '{"name": "脚本", "expires_in_days": 30}'


def test_unknown_token_is_401(admin_client: TestClient) -> None:
    assert admin_client.get("/v1/auth/me", headers=_bearer("mcl_forged")).status_code == 401


def test_cannot_revoke_another_users_token(
    admin_client: TestClient, conn: sqlite3.Connection
) -> None:
    conn.execute(
        "INSERT INTO users (id, username, password_hash, role, enabled, created_at)"
        " VALUES ('u2', 'bob', 'h', 'user', 1, '2026-01-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO pats (id, user_id, name, token_hash, prefix, created_at, expires_at)"
        " VALUES ('t2', 'u2', 'other', ?, 'mcl_', '2026-01-01T00:00:00Z', '2099-01-01T00:00:00Z')",
        (tokens.hash_token("mcl_other"),),
    )
    conn.commit()

    response = admin_client.delete("/v1/auth/tokens/t2")

    assert response.status_code == 404
    row = conn.execute("SELECT revoked_at FROM pats WHERE id = 't2'").fetchone()
    assert row["revoked_at"] is None
