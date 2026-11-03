"""测试公用 fixture。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mc_librarian.auth import passwords
from mc_librarian.config import SESSION_COOKIE_NAME, Settings
from mc_librarian.db import CONTROL_DB_NAME, utc_now
from mc_librarian.main import create_app

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "admin-password-1"
USER_USERNAME = "worker"
USER_PASSWORD = "worker-password-1"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data")


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def control_db(settings: Settings) -> Path:
    """控制面 sqlite 路径（lifespan 已建好）。"""
    return settings.data_dir / CONTROL_DB_NAME


@pytest.fixture
def conn(client: TestClient, control_db: Path) -> Iterator[sqlite3.Connection]:
    """直接读库用：测试要断言「库里存的不是明文」。`client` 保证库已被 lifespan 初始化。"""
    connection = sqlite3.connect(control_db)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def admin_client(client: TestClient) -> TestClient:
    """已经建好管理员并登录（cookie 在 client 的 cookie jar 里）的客户端。"""
    created = client.post(
        "/v1/setup",
        json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
    )

    assert created.status_code == 201, created.text

    return client


@pytest.fixture
def user_client(client: TestClient, conn: sqlite3.Connection) -> TestClient:
    """已登录的普通用户（role=user）：用来验证 `/v1/admin/*` 的 403。

    #9 落地用户管理接口前，直接往 `users` 表插一行再走正常登录。
    """
    created = client.post(
        "/v1/setup",
        json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
    )

    assert created.status_code == 201, created.text

    conn.execute(
        "INSERT INTO users (id, username, password_hash, role, enabled, created_at)"
        " VALUES (?, ?, ?, 'user', 1, ?)",
        ("user-1", USER_USERNAME, passwords.hash_password(USER_PASSWORD), utc_now()),
    )
    conn.commit()
    client.cookies.clear()

    logged_in = client.post(
        "/v1/auth/login",
        json={"username": USER_USERNAME, "password": USER_PASSWORD},
    )

    assert logged_in.status_code == 200, logged_in.text
    assert logged_in.json()["role"] == "user"

    return client


def session_cookie(client: TestClient) -> str:
    """取当前 client 手上的会话 cookie 明文（等价于浏览器里的 cookie）。"""
    value = client.cookies.get(SESSION_COOKIE_NAME)

    assert value, "应该已经下发会话 cookie"

    return str(value)
