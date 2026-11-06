"""`conversations` / `messages` 两张表的读写（问答页左栏的历史会话，迁移 v9）。

语义（issue #8 后续票已定死）：

- **会话归属**：严格按用户隔离。`find_owned` 同时按 `id` 与 `user_id` 查，别人的会话查不到，
  端点因此回 404（不是 403——不泄露会话是否存在）。
- **保留策略**：每用户最多 `MAX_PER_USER` 个会话，新建时 `prune` 掉最旧的（消息级联删）。
- **标题**：取首条问题，`title_from_question` 截断到 30 字，不调 LLM。

与其余 store 一样，写函数**不自己开事务**：调用方用 `db.transaction()` 包住「建会话 + 写消息 +
裁剪」这组判定与写入。消息顺序按 `created_at` 再按 `rowid`：`created_at` 只到秒，同一秒内
「问题 + 答案」靠插入顺序（rowid）区分，不靠 uuid 的字典序。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Sequence
from typing import Any, Final

from pydantic import TypeAdapter, ValidationError

from mc_librarian.db import utc_now
from mc_librarian.schemas import (
    Citation,
    ConversationMessage,
    ConversationSummary,
    MessageSegment,
    Usage,
)

# 每用户最多保留的会话数：新建时超出就删最旧的（级联删消息）。
MAX_PER_USER: Final = 200

# 片段是联合类型（`kind` 判别），校验要一个 TypeAdapter（每次构造都新建会慢）。
_SEGMENT_ADAPTER = TypeAdapter(MessageSegment)
# 标题长度上限：取首条问题，超出截断并加省略号。
TITLE_LIMIT: Final = 30
ELLIPSIS: Final = "…"


def title_from_question(question: str, limit: int = TITLE_LIMIT) -> str:
    """标题 = 首条问题，截断到 `limit` 字（超出加 `…`）；不调 LLM。"""
    text = question.strip()

    return text if len(text) <= limit else text[:limit] + ELLIPSIS


def create(conn: sqlite3.Connection, *, user_id: str, title: str) -> str:
    """新建一个会话，返回它的 id。"""
    conversation_id = uuid.uuid4().hex
    now = utc_now()

    conn.execute(
        "INSERT INTO conversations (id, user_id, title, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (conversation_id, user_id, title, now, now),
    )

    return conversation_id


def find_owned(
    conn: sqlite3.Connection, *, conversation_id: str, user_id: str
) -> sqlite3.Row | None:
    """按 id + user_id 查会话；别人的会话返回 `None`（端点回 404）。"""
    row: sqlite3.Row | None = conn.execute(
        "SELECT id, title, created_at, updated_at FROM conversations WHERE id = ? AND user_id = ?",
        (conversation_id, user_id),
    ).fetchone()

    return row


def append_message(
    conn: sqlite3.Connection,
    *,
    conversation_id: str,
    role: str,
    content: str,
    citations: tuple[Citation, ...] | list[Citation] = (),
    usage: Usage | None = None,
    version: str | None = None,
    segments: Sequence[dict[str, Any]] | None = None,
) -> None:
    """追加一条消息（用户问题或助手答案）。流式增量不落库，助手消息只在收尾时写一次。

    `version` 是这一轮提问用的版本（引用的 `path` 是版本相关的，历史引用要按它打开）。
    """
    conn.execute(
        "INSERT INTO messages"
        " (id, conversation_id, role, content, citations_json, usage_json, version,"
        " segments_json, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            uuid.uuid4().hex,
            conversation_id,
            role,
            content,
            json.dumps([item.model_dump() for item in citations], ensure_ascii=False),
            None if usage is None else json.dumps(usage.model_dump(), ensure_ascii=False),
            version,
            None
            if segments is None
            else json.dumps(list(segments), ensure_ascii=False),
            utc_now(),
        ),
    )


def touch(conn: sqlite3.Connection, *, conversation_id: str) -> None:
    """刷新 `updated_at`（列表按它降序，续聊要顶到最前）。"""
    conn.execute(
        "UPDATE conversations SET updated_at = ? WHERE id = ?", (utc_now(), conversation_id)
    )


def prune(conn: sqlite3.Connection, *, user_id: str, keep: int = MAX_PER_USER) -> int:
    """只保留最近 `keep` 个会话，删掉更旧的（消息级联删）；返回删除条数。"""
    cursor = conn.execute(
        "DELETE FROM conversations WHERE user_id = ? AND id NOT IN ("
        " SELECT id FROM conversations WHERE user_id = ?"
        " ORDER BY updated_at DESC, id DESC LIMIT ?)",
        (user_id, user_id, keep),
    )

    return int(cursor.rowcount)


def list_summaries(
    conn: sqlite3.Connection, *, user_id: str, limit: int
) -> list[ConversationSummary]:
    """当前用户的会话列表，按 `updated_at` 降序，附消息条数。"""
    rows = conn.execute(
        "SELECT c.id, c.title, c.created_at, c.updated_at,"
        " (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS message_count"
        " FROM conversations c WHERE c.user_id = ?"
        " ORDER BY c.updated_at DESC, c.id DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()

    return [
        ConversationSummary(
            id=str(row["id"]),
            title=str(row["title"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            message_count=int(row["message_count"]),
        )
        for row in rows
    ]


def messages(conn: sqlite3.Connection, *, conversation_id: str) -> list[ConversationMessage]:
    """一个会话的全部消息，按写入顺序（`created_at` 再按 `rowid`）。"""
    rows = conn.execute(
        "SELECT role, content, citations_json, usage_json, version, segments_json, created_at"
        " FROM messages"
        " WHERE conversation_id = ? ORDER BY created_at, rowid",
        (conversation_id,),
    ).fetchall()

    return [_row_to_message(row) for row in rows]


def remove(conn: sqlite3.Connection, *, conversation_id: str) -> None:
    """删除会话（消息由外键 `ON DELETE CASCADE` 一起删）。"""
    conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))


def _row_to_message(row: sqlite3.Row) -> ConversationMessage:
    return ConversationMessage(
        role=row["role"],
        content=str(row["content"]),
        citations=_load_citations(row["citations_json"]),
        usage=_load_usage(row["usage_json"]),
        version=row["version"],
        segments=_load_segments(row["segments_json"]),
        created_at=row["created_at"],
    )


def _load_segments(raw: object) -> list[MessageSegment] | None:
    """`segments_json` → 片段列表；`null` / 全部无效时退回 `None`（前端按「整段正文」渲染）。

    逐段校验、**丢弃坏段**而不是整条失败：片段只用于渲染，一个读不懂的段不该让整条会话报错。
    """
    loaded = _loads(raw)

    if not isinstance(loaded, list):
        return None

    segments: list[MessageSegment] = []

    for item in loaded:
        try:
            segments.append(_SEGMENT_ADAPTER.validate_python(item))
        except ValidationError:
            continue

    return segments or None


def _load_citations(raw: object) -> list[Citation]:
    """`citations_json` → `Citation` 列表；脏数据/空值退回空列表，不让一行坏数据打断读取。"""
    loaded = _loads(raw)

    if not isinstance(loaded, list):
        return []

    citations: list[Citation] = []

    for item in loaded:
        if not isinstance(item, dict):
            continue

        try:
            citations.append(Citation.model_validate(item))
        except ValueError:
            continue

    return citations


def _load_usage(raw: object) -> Usage | None:
    loaded = _loads(raw)

    if not isinstance(loaded, dict):
        return None

    try:
        return Usage.model_validate(loaded)
    except ValueError:
        return None


def _loads(raw: object) -> object:
    if not isinstance(raw, str) or not raw:
        return None

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None
