"""`/v1/setup` 与最小迁移机制的行为测试。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from conftest import ADMIN_PASSWORD, ADMIN_USERNAME, session_cookie
from mc_librarian.auth import passwords, sessions
from mc_librarian.config import SESSION_COOKIE_NAME, Settings
from mc_librarian.db import LATEST_VERSION, Database, migrate, schema_version, transaction
from mc_librarian.main import create_app


def test_setup_is_required_on_fresh_database(client: TestClient) -> None:
    response = client.get("/v1/setup")

    assert response.status_code == 200
    assert response.json() == {"required": True}


def test_setup_creates_admin_and_logs_in(client: TestClient) -> None:
    response = client.post(
        "/v1/setup",
        json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["username"] == ADMIN_USERNAME
    assert body["role"] == "admin"
    assert body["enabled"] is True
    assert "password_hash" not in body
    assert "password" not in body

    # 创建即登录：同一份 client 直接能读 /v1/auth/me。
    me = client.get("/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["username"] == ADMIN_USERNAME

    # 契约要求「首次创建后 setup 永久关闭」。
    assert client.get("/v1/setup").json() == {"required": False}


def test_session_cookie_attributes(client: TestClient) -> None:
    response = client.post(
        "/v1/setup",
        json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
    )

    set_cookie = response.headers["set-cookie"]
    lowered = set_cookie.lower()

    assert set_cookie.startswith(f"{SESSION_COOKIE_NAME}=")
    assert "httponly" in lowered
    assert "samesite=lax" in lowered
    assert "path=/" in lowered
    # 本地 http 默认不下发 Secure，否则浏览器会直接把 cookie 丢掉。
    assert lowered.count("secure") == 0


def test_setup_twice_returns_410(client: TestClient) -> None:
    first = client.post(
        "/v1/setup",
        json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
    )
    assert first.status_code == 201

    second = client.post(
        "/v1/setup",
        json={"username": "intruder", "password": "intruder-password"},
    )

    assert second.status_code == 410
    assert second.headers["content-type"].startswith("application/problem+json")

    problem = second.json()
    assert problem["status"] == 410
    assert problem["title"]

    # 第二次没建出用户：仍然只有原来那个管理员。
    assert client.get("/v1/auth/me").json()["username"] == ADMIN_USERNAME


def test_setup_rejects_weak_password(client: TestClient) -> None:
    response = client.post("/v1/setup", json={"username": ADMIN_USERNAME, "password": "short"})

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    # 422 里不能回显明文密码。
    assert '"short"' not in response.text


def test_password_is_hashed_not_plaintext(client: TestClient, conn: sqlite3.Connection) -> None:
    client.post("/v1/setup", json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD})

    row = conn.execute("SELECT password_hash FROM users WHERE username = ?", (ADMIN_USERNAME,))
    password_hash = row.fetchone()["password_hash"]

    assert password_hash != ADMIN_PASSWORD
    assert ADMIN_PASSWORD not in password_hash
    assert password_hash.startswith("$argon2id$")
    assert passwords.verify_password(ADMIN_PASSWORD, password_hash)


def test_session_token_is_not_stored_in_plaintext(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    client.post("/v1/setup", json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD})
    raw_token = session_cookie(client)

    row = conn.execute("SELECT token_hash FROM sessions").fetchone()
    token_hash = row["token_hash"]

    assert token_hash != raw_token
    assert raw_token not in token_hash
    assert token_hash == sessions.hash_token(raw_token)
    assert len(token_hash) == 64


def test_migration_creates_schema_and_is_idempotent(tmp_path: Path) -> None:
    database = Database.for_data_dir(tmp_path / "data")

    assert database.initialize() == LATEST_VERSION

    with database.connect() as conn:
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"users", "sessions", "schema_version"} <= tables
        assert schema_version(conn) == LATEST_VERSION

        # 再跑一遍（等价于重启进程）：不报错、版本不变、不重复记账。
        assert migrate(conn) == LATEST_VERSION
        applied = conn.execute("SELECT COUNT(*) AS total FROM schema_version").fetchone()["total"]
        assert applied == LATEST_VERSION


def test_schema_version_starts_at_zero(tmp_path: Path) -> None:
    database = Database.for_data_dir(tmp_path / "data")

    with database.connect() as conn:
        assert schema_version(conn) == 0


def test_transaction_rolls_back_on_error(tmp_path: Path) -> None:
    database = Database.for_data_dir(tmp_path / "data")
    database.initialize()

    with database.connect() as conn:
        try:
            with transaction(conn) as tx:
                tx.execute(
                    "INSERT INTO users (id, username, password_hash, role, enabled, created_at)"
                    " VALUES ('x', 'rolled-back', 'h', 'user', 1, '2026-01-01T00:00:00Z')"
                )
                raise RuntimeError("boom")
        except RuntimeError:
            pass

        total = conn.execute("SELECT COUNT(*) AS total FROM users").fetchone()["total"]

    assert total == 0, "事务内抛错必须整笔回滚"


def test_cookie_secure_flag_follows_config(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", cookie_secure=True)

    with TestClient(create_app(settings)) as secure_client:
        response = secure_client.post(
            "/v1/setup",
            json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
        )

    assert "secure" in response.headers["set-cookie"].lower()
