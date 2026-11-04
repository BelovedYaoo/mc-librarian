"""历史会话（问答页左栏）：`/v1/conversations*` 与 `/v1/ask` 的落库行为。

覆盖已定死的语义：自动建会话、`done` 回传 `conversation_id`、续聊追加消息、严格按用户隔离
（别人的会话 404）、标题截断 30 字、每用户保留上限、删除级联、降级答案照常落库。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from conftest import FakeChat, final_answer, join_via_invite, tool_call
from mc_librarian import conversations
from mc_librarian.answer.agent import DEGRADED_NOTICE
from mc_librarian.answer.client import ChatError
from mc_librarian.config import Settings
from mc_librarian.indexing.build import build_index, place_source_tree
from mc_librarian.indexing.layout import index_path, source_root
from mc_librarian.jobs import store

FIXTURE_SOURCE = Path(__file__).parent / "fixtures" / "source"

VERSION = "26.3"
QUESTION = "Mob"


def silent(_level: str, _message: str) -> None:
    pass


def register_version(settings: Settings, conn: sqlite3.Connection) -> Path:
    """就位 fixture 源码 → 建索引 → 直接注册版本。"""
    root = source_root(settings.data_dir, VERSION)
    place_source_tree(FIXTURE_SOURCE, root)
    index = index_path(settings.data_dir, VERSION)
    result = build_index(root, index, log=silent)
    store.register_version(
        conn,
        version=VERSION,
        files=result.files,
        tokens=result.tokens,
        obfuscated=False,
        has_summaries=False,
        source_size_bytes=index.stat().st_size,
        db_path=str(index),
    )
    conn.commit()

    return root


@pytest.fixture
def indexed(admin_client: TestClient, settings: Settings, conn: sqlite3.Connection) -> Path:
    """数据根里已就位源码、已建索引、已注册版本的 26.3（管理员会话）。"""
    return register_version(settings, conn)


def ask(client: TestClient, **body: Any) -> Any:
    return client.post("/v1/ask", json={"question": QUESTION, "version": VERSION, **body})


def sse_events(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    for block in text.strip().split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[len("data: ") :]))

    return events


def done_event(text: str) -> dict[str, Any]:
    return next(event for event in sse_events(text) if event["type"] == "done")


# ----------------------------------------------------------------------
# 自动建会话 + `done` 回传 id + 落库内容
# ----------------------------------------------------------------------


def test_ask_creates_a_conversation_and_returns_its_id(
    admin_client: TestClient, indexed: Path, fake_chat: FakeChat
) -> None:
    fake_chat.script = [
        tool_call("search", query="Mob", detail="locate"),
        final_answer("Mob 定义了刷怪实体的基类。"),
    ]

    response = ask(admin_client)

    assert response.status_code == 200, response.text

    conversation_id = done_event(response.text)["conversation_id"]
    assert conversation_id

    listed = admin_client.get("/v1/conversations").json()["conversations"]

    assert len(listed) == 1
    assert listed[0]["id"] == conversation_id
    assert listed[0]["title"] == QUESTION
    assert listed[0]["message_count"] == 2

    detail = admin_client.get(f"/v1/conversations/{conversation_id}").json()

    assert detail["id"] == conversation_id
    assert [message["role"] for message in detail["messages"]] == ["user", "assistant"]
    assert detail["messages"][0]["content"] == QUESTION
    assert detail["messages"][1]["content"] == "Mob 定义了刷怪实体的基类。"
    assert detail["messages"][1]["citations"], "助手消息要带上引用"
    assert detail["messages"][1]["usage"]["prompt_tokens"] == 11 * 2
    # 用户消息没有引用与用量。
    assert detail["messages"][0]["citations"] == []
    assert detail["messages"][0]["usage"] is None


def test_ask_continues_an_existing_conversation(
    admin_client: TestClient, indexed: Path, fake_chat: FakeChat
) -> None:
    fake_chat.script = [final_answer("第一次回答。")]
    first = done_event(ask(admin_client).text)["conversation_id"]

    fake_chat.script = [final_answer("第二次回答。")]
    response = ask(admin_client, conversation_id=first)

    assert response.status_code == 200, response.text
    assert done_event(response.text)["conversation_id"] == first

    detail = admin_client.get(f"/v1/conversations/{first}").json()

    assert [message["role"] for message in detail["messages"]] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert [message["content"] for message in detail["messages"]] == [
        QUESTION,
        "第一次回答。",
        QUESTION,
        "第二次回答。",
    ]

    # 只有这一个会话（续聊不新建）。
    assert len(admin_client.get("/v1/conversations").json()["conversations"]) == 1


def test_ask_persists_the_degraded_answer(
    admin_client: TestClient, indexed: Path, fake_chat: FakeChat
) -> None:
    """降级照常落库：答案里已经含提示语。"""
    fake_chat.fail_with = ChatError("模型炸了")

    response = ask(admin_client)

    assert response.status_code == 200, response.text
    conversation_id = done_event(response.text)["conversation_id"]
    detail = admin_client.get(f"/v1/conversations/{conversation_id}").json()

    assert detail["messages"][1]["content"] == DEGRADED_NOTICE


# ----------------------------------------------------------------------
# 严格按用户隔离 + 删除
# ----------------------------------------------------------------------


def test_conversations_are_isolated_per_user(
    admin_client: TestClient, other_client: TestClient, indexed: Path, fake_chat: FakeChat
) -> None:
    fake_chat.script = [final_answer("只给管理员的回答。")]
    conversation_id = done_event(ask(admin_client).text)["conversation_id"]

    join_via_invite(
        admin_client, other_client, username="bob", password="bob-password-1"
    )

    # 别人的会话一律 404（不是 403——不泄露存在性）。
    assert other_client.get(f"/v1/conversations/{conversation_id}").status_code == 404
    assert other_client.delete(f"/v1/conversations/{conversation_id}").status_code == 404
    assert other_client.get("/v1/conversations").json()["conversations"] == []

    # 管理员自己的会话仍在。
    assert admin_client.get(f"/v1/conversations/{conversation_id}").status_code == 200


def test_ask_with_unknown_conversation_returns_404_and_releases_the_slot(
    admin_client: TestClient, indexed: Path, fake_chat: FakeChat
) -> None:
    fake_chat.script = [final_answer("不该被调用。")]

    response = ask(admin_client, conversation_id="does-not-exist")

    assert response.status_code == 404
    # 槽位必须归还（后续请求照常能拿到）。
    assert ask(admin_client).status_code == 200


def test_delete_conversation_cascades_messages(
    admin_client: TestClient, indexed: Path, conn: sqlite3.Connection, fake_chat: FakeChat
) -> None:
    fake_chat.script = [final_answer("回答。")]
    conversation_id = done_event(ask(admin_client).text)["conversation_id"]

    assert (
        conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE conversation_id = ?", (conversation_id,)
        ).fetchone()["n"]
        == 2
    )

    assert admin_client.delete(f"/v1/conversations/{conversation_id}").status_code == 204
    assert admin_client.get(f"/v1/conversations/{conversation_id}").status_code == 404
    assert admin_client.get("/v1/conversations").json()["conversations"] == []
    assert (
        conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE conversation_id = ?", (conversation_id,)
        ).fetchone()["n"]
        == 0
    )


def test_conversations_require_login(client: TestClient) -> None:
    assert client.get("/v1/conversations").status_code == 401
    assert client.get("/v1/conversations/x").status_code == 401
    assert client.delete("/v1/conversations/x").status_code == 401


# ----------------------------------------------------------------------
# 标题与保留策略（纯 store 层，不依赖索引）
# ----------------------------------------------------------------------


def test_title_from_question_truncates_to_30_chars() -> None:
    assert conversations.title_from_question("刷怪上限在哪定义？") == "刷怪上限在哪定义？"
    assert conversations.title_from_question("a" * 30) == "a" * 30
    assert conversations.title_from_question("a" * 31) == "a" * 30 + "…"
    # 首尾空白先去掉再截断。
    assert conversations.title_from_question("  " + "b" * 40 + "  ") == "b" * 30 + "…"


def test_ask_truncates_a_long_question_into_the_title(
    admin_client: TestClient, indexed: Path, fake_chat: FakeChat
) -> None:
    fake_chat.script = [final_answer("回答。")]
    long_question = "刷" * 40

    response = admin_client.post(
        "/v1/ask", json={"question": long_question, "version": VERSION}
    )

    assert response.status_code == 200, response.text
    conversation_id = done_event(response.text)["conversation_id"]
    detail = admin_client.get(f"/v1/conversations/{conversation_id}").json()

    assert detail["title"] == "刷" * 30 + "…"


def test_prune_keeps_only_the_newest(
    admin_client: TestClient, conn: sqlite3.Connection
) -> None:
    user_id = admin_client.get("/v1/auth/me").json()["id"]

    for index in range(3):
        conversations.create(conn, user_id=user_id, title=f"会话 {index}")
        conn.execute(
            "UPDATE conversations SET updated_at = ? WHERE title = ?",
            (f"2024-01-0{index + 1}T00:00:00Z", f"会话 {index}"),
        )
    conn.commit()

    removed = conversations.prune(conn, user_id=user_id, keep=2)
    conn.commit()

    assert removed == 1
    titles = [
        str(row["title"])
        for row in conn.execute(
            "SELECT title FROM conversations WHERE user_id = ? ORDER BY updated_at DESC",
            (user_id,),
        ).fetchall()
    ]
    assert titles == ["会话 2", "会话 1"]


def test_max_per_user_is_two_hundred() -> None:
    assert conversations.MAX_PER_USER == 200
