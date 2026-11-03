"""`users` 表读写。

写函数**不自己开事务**：调用方用 `db.transaction()` 包住「判定 + 写入」，
setup 的「只能成功一次」才是原子的。
"""

from __future__ import annotations

import sqlite3
import uuid

from mc_librarian.auth.passwords import hash_password
from mc_librarian.db import utc_now
from mc_librarian.schemas import User

_COLUMNS = "id, username, password_hash, role, enabled, created_at, created_by, last_login_at"


def row_to_user(row: sqlite3.Row) -> User:
    """`users` 行 → API 模型（不含 `password_hash`）。"""
    return User(
        id=row["id"],
        username=row["username"],
        role=row["role"],
        enabled=bool(row["enabled"]),
        created_at=row["created_at"],
        created_by=row["created_by"],
        last_login_at=row["last_login_at"],
    )


def count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS total FROM users").fetchone()

    return int(row["total"])


def any_user(conn: sqlite3.Connection) -> bool:
    """控制面是否已经有账号；false 表示需要走 setup。"""
    return count(conn) > 0


def find_by_username(conn: sqlite3.Connection, username: str) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        f"SELECT {_COLUMNS} FROM users WHERE username = ?", (username,)
    ).fetchone()

    return row


def find_by_id(conn: sqlite3.Connection, user_id: str) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        f"SELECT {_COLUMNS} FROM users WHERE id = ?", (user_id,)
    ).fetchone()

    return row


def create_admin(conn: sqlite3.Connection, *, username: str, password: str) -> User:
    """建唯一管理员（`created_by` 为空，表示首个账号）。用户名冲突抛 `IntegrityError`。"""
    user_id = uuid.uuid4().hex
    now = utc_now()

    conn.execute(
        f"INSERT INTO users ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, username, hash_password(password), "admin", 1, now, None, None),
    )

    created = find_by_id(conn, user_id)

    assert created is not None, "刚插入的用户必须能查回来"

    return row_to_user(created)


def touch_last_login(conn: sqlite3.Connection, user_id: str) -> User:
    """登录成功时更新 `last_login_at` 并返回最新用户。"""
    conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (utc_now(), user_id))

    updated = find_by_id(conn, user_id)

    assert updated is not None, "更新过的用户必须存在"

    return row_to_user(updated)
