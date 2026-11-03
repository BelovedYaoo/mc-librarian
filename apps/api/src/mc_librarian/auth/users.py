"""`users` 表读写。

写函数**不自己开事务**：调用方用 `db.transaction()` 包住「判定 + 写入」，
setup 的「只能成功一次」才是原子的。
"""

from __future__ import annotations

import sqlite3
import uuid

from mc_librarian.auth.passwords import UNUSABLE_PASSWORD_HASH, hash_password
from mc_librarian.db import utc_now
from mc_librarian.schemas import Role, User

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


def list_all(conn: sqlite3.Connection) -> list[User]:
    """全部用户，按创建时间升序（同秒创建时用 rowid 保持稳定顺序）。"""
    rows = conn.execute(f"SELECT {_COLUMNS} FROM users ORDER BY created_at, rowid").fetchall()

    return [row_to_user(row) for row in rows]


def count_enabled_admins(conn: sqlite3.Connection) -> int:
    """当前启用的管理员数量；用于「最后一个 admin 不能降级/停用」的判定。"""
    row = conn.execute(
        "SELECT COUNT(*) AS total FROM users WHERE role = 'admin' AND enabled = 1"
    ).fetchone()

    return int(row["total"])


def create_user(
    conn: sqlite3.Connection,
    *,
    username: str,
    role: Role,
    created_by: str,
) -> User:
    """建普通用户/管理员，密码留空占位——密码由被邀请人通过邀请链接自设。

    用户名冲突抛 `IntegrityError`（调用方在同事务内先查重，见 `api.v1.admin.create_user`）。
    """
    user_id = uuid.uuid4().hex

    conn.execute(
        f"INSERT INTO users ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, username, UNUSABLE_PASSWORD_HASH, role, 1, utc_now(), created_by, None),
    )

    created = find_by_id(conn, user_id)

    assert created is not None, "刚插入的用户必须能查回来"

    return row_to_user(created)


def set_role(conn: sqlite3.Connection, user_id: str, role: Role) -> User:
    conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))

    return _require(conn, user_id)


def set_enabled(conn: sqlite3.Connection, user_id: str, enabled: bool) -> User:
    conn.execute("UPDATE users SET enabled = ? WHERE id = ?", (1 if enabled else 0, user_id))

    return _require(conn, user_id)


def set_password(conn: sqlite3.Connection, user_id: str, password: str) -> User:
    """接受邀请时写入用户自设的密码，并顺带把账号置为启用。"""
    conn.execute(
        "UPDATE users SET password_hash = ?, enabled = 1 WHERE id = ?",
        (hash_password(password), user_id),
    )

    return _require(conn, user_id)


def _require(conn: sqlite3.Connection, user_id: str) -> User:
    row = find_by_id(conn, user_id)

    assert row is not None, "更新过的用户必须存在"

    return row_to_user(row)
