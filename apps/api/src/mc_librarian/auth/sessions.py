"""服务端会话：库里只存 token 的 sha256，明文 token 只出现在 cookie 里。

和 `users.py` 一样，写函数不自己开事务，由调用方包 `db.transaction()`。
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import uuid
from typing import Final

from mc_librarian.auth.users import row_to_user
from mc_librarian.db import utc_after, utc_now
from mc_librarian.schemas import User

# 32 字节随机数 → 43 字符的 urlsafe base64，足够抗暴力猜测。
TOKEN_BYTES: Final = 32


def hash_token(raw_token: str) -> str:
    """cookie 里的明文 token 落库前先做 sha256（hex）。"""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def create(
    conn: sqlite3.Connection,
    user_id: str,
    *,
    ttl_hours: int,
    ip: str | None = None,
    user_agent: str | None = None,
) -> str:
    """新建会话并返回**明文** token（调用方负责把它塞进 cookie）。"""
    raw_token = secrets.token_urlsafe(TOKEN_BYTES)

    conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (utc_now(),))
    conn.execute(
        """
        INSERT INTO sessions (id, user_id, token_hash, created_at, expires_at, ip, user_agent)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            uuid.uuid4().hex,
            user_id,
            hash_token(raw_token),
            utc_now(),
            utc_after(ttl_hours),
            ip,
            user_agent,
        ),
    )

    return raw_token


def resolve(conn: sqlite3.Connection, raw_token: str) -> User | None:
    """用 cookie 里的明文 token 换当前用户；过期、不存在、账号被禁用都返回 None。"""
    row: sqlite3.Row | None = conn.execute(
        """
        SELECT u.id, u.username, u.password_hash, u.role, u.enabled,
               u.created_at, u.created_by, u.last_login_at
          FROM sessions s
          JOIN users u ON u.id = s.user_id
         WHERE s.token_hash = ? AND s.expires_at > ?
        """,
        (hash_token(raw_token), utc_now()),
    ).fetchone()

    if row is None or not bool(row["enabled"]):
        return None

    return row_to_user(row)


def delete(conn: sqlite3.Connection, raw_token: str) -> bool:
    """删除会话；返回是否真的删掉了一行（用于判定「有没有可注销的会话」）。"""
    cursor = conn.execute("DELETE FROM sessions WHERE token_hash = ?", (hash_token(raw_token),))

    return cursor.rowcount > 0


def delete_for_user(conn: sqlite3.Connection, user_id: str) -> int:
    """删除某用户的全部会话（禁用账号时立即踢下线）；返回删除行数。"""
    cursor = conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    return cursor.rowcount
