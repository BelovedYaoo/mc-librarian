"""`invites` 表读写：一次性邀请链接（默认 48 小时）。

与会话 token 同一套处理：明文 token 只在创建时返回一次（拼进 `invite_url`），
库里只存 sha256，因此管理员与数据库都拿不到可用链接之外的秘密。

写函数不自己开事务，由调用方包 `db.transaction()`——「判定有效 + 标记已用」必须原子，
否则同一个链接可能被并发接受两次。
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import uuid
from typing import Final

from mc_librarian.db import utc_after, utc_now

# 与 `sessions.TOKEN_BYTES` 一致：32 字节随机数，抗暴力猜测。
TOKEN_BYTES: Final = 32

# 邀请 + 对应用户的联查结果，供校验与接受复用。
_SELECT = """
SELECT i.id, i.user_id, i.expires_at, i.used_at,
       u.username, u.enabled
  FROM invites i
  JOIN users u ON u.id = i.user_id
 WHERE i.token_hash = ?
"""


def hash_token(raw_token: str) -> str:
    """邀请链接里的明文 token 落库前先做 sha256（hex）。"""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def create(conn: sqlite3.Connection, user_id: str, *, ttl_hours: int) -> str:
    """建一条邀请并返回**明文** token（调用方负责拼成 invite_url 返回一次）。"""
    raw_token = secrets.token_urlsafe(TOKEN_BYTES)

    conn.execute(
        """
        INSERT INTO invites (id, user_id, token_hash, created_at, expires_at, used_at)
        VALUES (?, ?, ?, ?, ?, NULL)
        """,
        (uuid.uuid4().hex, user_id, hash_token(raw_token), utc_now(), utc_after(ttl_hours)),
    )

    return raw_token


def find(conn: sqlite3.Connection, raw_token: str) -> sqlite3.Row | None:
    """按明文 token 查邀请行（不判定有效性）；不存在返回 None。"""
    row: sqlite3.Row | None = conn.execute(_SELECT, (hash_token(raw_token),)).fetchone()

    return row


def is_valid(row: sqlite3.Row) -> bool:
    """未使用、未过期、且账号仍启用。"""
    return (
        row["used_at"] is None
        and str(row["expires_at"]) > utc_now()
        and bool(row["enabled"])
    )


def mark_used(conn: sqlite3.Connection, invite_id: str) -> None:
    """标记已使用：这是「一次性」的唯一开关。"""
    conn.execute("UPDATE invites SET used_at = ? WHERE id = ?", (utc_now(), invite_id))
