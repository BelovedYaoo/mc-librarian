"""`audit` 表读写：敏感操作的审计流水。

动作名集中在这里，避免各路由手写字符串漂移；`target` 是被操作对象的标识
（用户 id / 用户名），`detail` 存变更前后的值（JSON 文本）。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any, Final

from mc_librarian.db import utc_now
from mc_librarian.schemas import AuditEntry

ACTION_USER_CREATED: Final = "user.created"
ACTION_USER_ROLE_CHANGED: Final = "user.role_changed"
ACTION_USER_DISABLED: Final = "user.disabled"
ACTION_USER_ENABLED: Final = "user.enabled"
ACTION_INVITE_CREATED: Final = "invite.created"
ACTION_INVITE_ACCEPTED: Final = "invite.accepted"
ACTION_TOKEN_CREATED: Final = "token.created"
ACTION_TOKEN_REVOKED: Final = "token.revoked"
ACTION_JOB_CREATED: Final = "job.created"
ACTION_JOB_CANCELED: Final = "job.canceled"
ACTION_JOB_RETRIED: Final = "job.retried"
ACTION_VERSION_REGISTERED: Final = "version.registered"
ACTION_VERSION_RECORDED: Final = "version.recorded"
ACTION_SETTINGS_UPDATED: Final = "settings.updated"

# `actor` 对外用用户名（更好读），库里存 actor_id；用户不存在时退回 id。
_SELECT = """
SELECT a.id,
       COALESCE(u.username, a.actor_id) AS actor,
       a.action,
       a.target,
       a.detail_json,
       a.ip,
       a.created_at
  FROM audit a
  LEFT JOIN users u ON u.id = a.actor_id
"""


def record(
    conn: sqlite3.Connection,
    *,
    actor_id: str | None,
    action: str,
    target: str | None = None,
    detail: dict[str, Any] | None = None,
    ip: str | None = None,
) -> None:
    """写一条审计。不自己开事务：调用方把「改动 + 记账」放在同一个事务里。"""
    conn.execute(
        """
        INSERT INTO audit (id, actor_id, action, target, detail_json, ip, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            uuid.uuid4().hex,
            actor_id,
            action,
            target,
            json.dumps(detail, ensure_ascii=False) if detail else None,
            ip,
            utc_now(),
        ),
    )


def list_entries(
    conn: sqlite3.Connection,
    *,
    limit: int,
    actor: str | None = None,
) -> list[AuditEntry]:
    """最新在前；`actor` 给定时按用户名精确筛选。"""
    rows = conn.execute(
        f"{_SELECT} WHERE (? IS NULL OR COALESCE(u.username, a.actor_id) = ?)"
        " ORDER BY a.created_at DESC, a.rowid DESC LIMIT ?",
        (actor, actor, limit),
    ).fetchall()

    return [
        AuditEntry(
            id=str(row["id"]),
            # 系统动作没有 actor：保持 None，不要退化成字符串 "None"。
            actor=None if row["actor"] is None else str(row["actor"]),
            action=str(row["action"]),
            target=row["target"],
            detail=json.loads(row["detail_json"]) if row["detail_json"] else {},
            ip=row["ip"],
            created_at=row["created_at"],
        )
        for row in rows
    ]
