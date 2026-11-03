"""`pats` 表读写与 Bearer 令牌校验。

和 `users.py` / `sessions.py` 一样：写函数不自己开事务，由调用方包 `db.transaction()`。
唯一例外是 `resolve` 顺带更新 `last_used_at`（单条 UPDATE，autocommit 下原子）——
它由 `get_current_user` 依赖调用，而 FastAPI 缓存依赖结果，所以同一请求里只会写一次。

令牌明文形如 `mcl_<43 字符 urlsafe base64>`，库里只存 sha256；`prefix` 是明文前 8 位，
用于列表里辨认，泄露它也无法还原令牌。
"""

from __future__ import annotations

import secrets
import sqlite3
import uuid
from typing import Final

from mc_librarian.auth.hashing import hash_token as hash_token
from mc_librarian.auth.users import row_to_user
from mc_librarian.db import utc_after, utc_now
from mc_librarian.schemas import TokenInfo, User

TOKEN_PREFIX: Final = "mcl_"
# 32 字节随机数 → 43 字符 urlsafe base64，与 session token 同强度。
TOKEN_BYTES: Final = 32
# 明文前 8 位（含 `mcl_`）作为列表里辨认用的 prefix。
PREFIX_LENGTH: Final = 8

_COLUMNS = "id, user_id, name, token_hash, prefix, created_at, expires_at, last_used_at, revoked_at"


def _new_token() -> str:
    return f"{TOKEN_PREFIX}{secrets.token_urlsafe(TOKEN_BYTES)}"


def row_to_info(row: sqlite3.Row) -> TokenInfo:
    """`pats` 行 → API 模型（**不含** `token_hash`，所以列表不会泄露明文）。"""
    return TokenInfo(
        id=row["id"],
        name=row["name"],
        prefix=row["prefix"],
        created_at=row["created_at"],
        last_used_at=row["last_used_at"],
        expires_at=row["expires_at"],
        revoked_at=row["revoked_at"],
    )


def find_by_id(conn: sqlite3.Connection, token_id: str) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        f"SELECT {_COLUMNS} FROM pats WHERE id = ?", (token_id,)
    ).fetchone()

    return row


def list_for_user(conn: sqlite3.Connection, user_id: str) -> list[sqlite3.Row]:
    """该用户未吊销的令牌，最新的在前（含已过期，便于前端提示「已过期」）。"""
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM pats WHERE user_id = ? AND revoked_at IS NULL"
        " ORDER BY created_at DESC, id DESC",
        (user_id,),
    ).fetchall()

    return list(rows)


def create(
    conn: sqlite3.Connection, user_id: str, *, name: str, expires_in_days: int
) -> tuple[str, sqlite3.Row]:
    """签发令牌并返回（**明文**、行）；明文只在此刻存在，库里只落 sha256。"""
    raw_token = _new_token()
    token_id = uuid.uuid4().hex

    conn.execute(
        f"INSERT INTO pats ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL)",
        (
            token_id,
            user_id,
            name,
            hash_token(raw_token),
            raw_token[:PREFIX_LENGTH],
            utc_now(),
            utc_after(expires_in_days * 24),
        ),
    )

    created = find_by_id(conn, token_id)

    assert created is not None, "刚插入的令牌必须能查回来"

    return raw_token, created


def revoke(conn: sqlite3.Connection, user_id: str, token_id: str) -> sqlite3.Row | None:
    """吊销自己的令牌并返回被吊销的行；不存在、非本人、已吊销都返回 None（调用方据此回 404）。

    返回整行是为了让调用方能拿 `prefix` 记审计：`target` 用可辨认的前缀而不是不透明的 id。
    """
    row: sqlite3.Row | None = conn.execute(
        f"SELECT {_COLUMNS} FROM pats WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
        (token_id, user_id),
    ).fetchone()

    if row is None:
        return None

    conn.execute("UPDATE pats SET revoked_at = ? WHERE id = ?", (utc_now(), token_id))

    return row


def resolve(conn: sqlite3.Connection, raw_token: str) -> User | None:
    """用 Bearer 里的明文令牌换当前用户；不存在/过期/吊销/账号禁用都返回 None。

    命中时更新 `last_used_at`：单条 UPDATE 在 autocommit 下原子，且依赖缓存保证每请求一次。
    """
    row: sqlite3.Row | None = conn.execute(
        """
        SELECT p.id AS token_id, p.expires_at, p.revoked_at,
               u.id, u.username, u.password_hash, u.role, u.enabled,
               u.created_at, u.created_by, u.last_login_at
          FROM pats p
          JOIN users u ON u.id = p.user_id
         WHERE p.token_hash = ?
        """,
        (hash_token(raw_token),),
    ).fetchone()

    if row is None or row["revoked_at"] is not None or not bool(row["enabled"]):
        return None

    expires_at = row["expires_at"]
    if expires_at is not None and str(expires_at) <= utc_now():
        return None

    conn.execute("UPDATE pats SET last_used_at = ? WHERE id = ?", (utc_now(), row["token_id"]))

    return row_to_user(row)
