"""中文问答 `POST /v1/ask`（issue #8）。

索引与源码都写在 `tmp_path`（复用 #5/#6 的 fixture 源码树），LLM 由 `conftest.FakeChat` 脚本化，
不联网。覆盖规格 §1–§8：事件序列、`max_steps` 收尾、降级（含第一步失败先跑一次 search）、
上下文预算、`usage` 落库、并发与开关、`Accept: application/json`、模型未配置。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from conftest import FakeChat, final_answer, tool_call
from mc_librarian.answer.client import ChatError
from mc_librarian.config import Settings
from mc_librarian.indexing.build import build_index, place_source_tree
from mc_librarian.indexing.layout import index_path, source_root
from mc_librarian.jobs import store

FIXTURE_SOURCE = Path(__file__).parent / "fixtures" / "source"

VERSION = "26.3"
# fixture 索引里只有英文符号，中文问句匹配不到任何通道；降级回退的那次 search 用问题原文，
# 所以问题写成符号名，才能验证「第一步失败也先跑一次 search 拿到位置」。
QUESTION = "Mob"


def silent(_level: str, _message: str) -> None:
    pass


def register_version(settings: Settings, conn: sqlite3.Connection) -> Path:
    """就位 fixture 源码 → 建索引 → 直接注册版本（无 admin 会话也能用）。"""
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
def indexed(user_client: TestClient, settings: Settings, conn: sqlite3.Connection) -> Path:
    """数据根里已就位源码、已建索引、已注册版本的 26.3。"""
    return register_version(settings, conn)


@pytest.fixture
def usage_rows(conn: sqlite3.Connection) -> Callable[[], list[sqlite3.Row]]:
    """读 `usage` 表的行（调用时现查，不缓存）。"""

    def read() -> list[sqlite3.Row]:
        return list(
            conn.execute(
                "SELECT user_id, kind, model, prompt_tokens, completion_tokens, cost_usd,"
                " latency_ms, version FROM usage ORDER BY id"
            ).fetchall()
        )

    return read


def ask(client: TestClient, **body: Any) -> Any:
    return client.post("/v1/ask", json={"question": QUESTION, "version": VERSION, **body})


def parse_sse(text: str) -> list[dict[str, Any]]:
    """把 SSE 文本解析成事件 payload 列表，并校验 `event:` 与 `data.type` 一致。"""
    events: list[dict[str, Any]] = []

    for block in text.strip().split("\n\n"):
        if not block.strip():
            continue

        lines = block.splitlines()
        name = next(line[len("event: ") :] for line in lines if line.startswith("event: "))
        data = next(line[len("data: ") :] for line in lines if line.startswith("data: "))
        payload = json.loads(data)

        assert payload["type"] == name, block
        events.append(payload)

    return events


def tools_of(message: dict[str, Any]) -> list[dict[str, Any]]:
    return cast("list[dict[str, Any]]", message["tool_calls"])


# ----------------------------------------------------------------------
# 事件序列（规格 §4）
# ----------------------------------------------------------------------


def test_ask_streams_step_delta_citation_done(
    user_client: TestClient, indexed: Path, fake_chat: FakeChat
) -> None:
    fake_chat.script = [
        tool_call("search", query="Mob", detail="locate"),
        final_answer("Mob 定义了刷怪实体的基类。"),
    ]

    response = ask(user_client)

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")

    events = parse_sse(response.text)
    kinds = [event["type"] for event in events]

    assert kinds[0] == "step"
    assert kinds[-1] == "done"
    assert kinds.index("delta") < kinds.index("citation")

    before, after = events[0], events[1]
    assert before == {
        "type": "step",
        "tool": "search",
        "args": {"query": "Mob", "detail": "locate"},
    }
    assert after["tool"] == "search"
    assert after["ok"] is True
    assert after["hits"] >= 1

    citation = next(event for event in events if event["type"] == "citation")
    assert citation["path"]
    assert len(citation["lines"]) == 2

    done = events[-1]
    assert done["reason"] == "stop"
    assert done["usage"]["prompt_tokens"] == 11 * 2
    assert done["usage"]["completion_tokens"] == 7 * 2


def test_ask_reports_tool_failure_to_the_model(
    user_client: TestClient, indexed: Path, fake_chat: FakeChat
) -> None:
    """工具失败（参数缺失）不 500：`ok=false` 的 step 事件 + 失败说明回给模型。"""
    fake_chat.script = [tool_call("search"), final_answer("好。")]

    events = parse_sse(ask(user_client).text)

    assert events[1] == {"type": "step", "tool": "search", "ok": False, "hits": 0}

    tool_messages = [
        message
        for message in fake_chat.calls[1]["messages"]
        if message["role"] == "tool"
    ]
    assert "参数不合法" in tool_messages[0]["content"]


# ----------------------------------------------------------------------
# max_steps（规格 §2）
# ----------------------------------------------------------------------


def test_ask_forces_finish_at_max_steps(
    user_client: TestClient, indexed: Path, fake_chat: FakeChat
) -> None:
    """模型一直要调工具：跑满 6 步后不再给工具，强制收尾并注明原因。"""
    fake_chat.always_tools = tools_of(tool_call("search", query="Mob", detail="locate"))

    response = ask(user_client)
    events = parse_sse(response.text)

    executed = [event for event in events if event["type"] == "step" and "ok" in event]
    assert len(executed) == 6

    done = events[-1]
    assert done["reason"] == "max_steps"

    answer = "".join(event["text"] for event in events if event["type"] == "delta")
    assert answer.startswith("已达检索步数上限")

    # 强制收尾那一次调用不给工具（规格 §2）。
    assert "tools" not in fake_chat.calls[-1]
    assert len(fake_chat.calls) == 7


# ----------------------------------------------------------------------
# 降级（规格 §5）
# ----------------------------------------------------------------------


def test_ask_degrades_when_the_model_fails(
    user_client: TestClient, indexed: Path, fake_chat: FakeChat
) -> None:
    """模型第一步就失败：HTTP 仍 200，先用用户问题跑一次 search，再带上位置收尾。"""
    fake_chat.fail_with = ChatError("模型请求失败：timed out")

    response = ask(user_client)

    assert response.status_code == 200, response.text

    events = parse_sse(response.text)
    kinds = [event["type"] for event in events]

    assert kinds[0] == "step"
    assert kinds[-1] == "done"

    fallback = events[0]
    assert fallback["tool"] == "search"
    assert fallback["args"]["detail"] == "locate"
    assert fallback["args"]["limit"] == 10
    assert events[1]["tool"] == "search"
    assert events[1]["ok"] is True
    assert events[1]["hits"] >= 1

    answer = "".join(event["text"] for event in events if event["type"] == "delta")
    assert "模型暂不可用" in answer

    assert any(event["type"] == "citation" for event in events)
    assert events[-1]["reason"] == "degraded"


def test_ask_degrades_after_partial_recall(
    user_client: TestClient, indexed: Path, fake_chat: FakeChat
) -> None:
    """已经召回了一些位置后模型才失败：直接用它们降级，不再多跑一次 search。"""
    fake_chat.script = [tool_call("search", query="Mob", detail="locate")]
    fake_chat.fail_with = ChatError("模型请求失败：connection reset")
    fake_chat.fail_from = 2

    events = parse_sse(ask(user_client).text)
    steps = [event for event in events if event["type"] == "step"]

    assert len(steps) == 2, "不该再补一次 search"
    assert events[-1]["reason"] == "degraded"
    assert any(event["type"] == "citation" for event in events)


# ----------------------------------------------------------------------
# 上下文预算（规格 §3）
# ----------------------------------------------------------------------


def test_ask_drops_oldest_tool_results_over_context_budget(
    user_client: TestClient, indexed: Path, fake_chat: FakeChat, settings: Settings
) -> None:
    # 直接改属性、绕过 ge=1000 的下限：这里要的就是几十 token 的预算。
    settings.ask_max_context_tokens = 60
    fake_chat.always_tools = tools_of(tool_call("search", query="Mob", detail="snippet"))

    events = parse_sse(ask(user_client).text)
    budgets = [event for event in events if event.get("tool") == "context_budget"]

    assert budgets, "超出上下文预算时必须记一条 step 事件"
    assert budgets[0]["args"]["dropped"] == ["search#1"]

    # 被丢弃的结果不再出现在发给模型的后续请求里：跑满 6 步后只剩 1 条工具消息。
    last_messages = fake_chat.calls[-1]["messages"]
    tool_texts = [message["content"] for message in last_messages if message["role"] == "tool"]
    assert len(tool_texts) == 1


# ----------------------------------------------------------------------
# 用量落库（规格 §6）
# ----------------------------------------------------------------------


def test_ask_writes_one_usage_row_per_request(
    user_client: TestClient, indexed: Path, fake_chat: FakeChat, usage_rows: Any
) -> None:
    fake_chat.script = [
        tool_call("search", query="Mob", detail="locate"),
        final_answer("Mob 定义了刷怪实体的基类。"),
    ]

    ask(user_client)

    rows = usage_rows()
    assert len(rows) == 1

    row = rows[0]
    assert row["kind"] == "ask"
    assert row["model"] == "fake-model"
    assert row["prompt_tokens"] == 11 * 2
    assert row["completion_tokens"] == 7 * 2
    # 未配单价：只记 token，不计成本（不编假单价）。
    assert row["cost_usd"] == 0
    assert row["version"] == VERSION
    assert row["latency_ms"] >= 0


def test_ask_writes_usage_row_when_degraded(
    user_client: TestClient, indexed: Path, fake_chat: FakeChat, usage_rows: Any
) -> None:
    fake_chat.fail_with = ChatError("模型请求失败：timed out")

    ask(user_client)

    rows = usage_rows()
    assert len(rows) == 1
    assert rows[0]["kind"] == "ask"
    # 那次失败的调用没有 usage，因此记 0。
    assert rows[0]["prompt_tokens"] == 0


def test_ask_cost_uses_configured_prices(
    user_client: TestClient, indexed: Path, fake_chat: FakeChat, settings: Settings, usage_rows: Any
) -> None:
    settings.llm_input_price_per_m = 1.0
    settings.llm_output_price_per_m = 2.0
    fake_chat.script = [final_answer("答案。")]

    ask(user_client)

    row = usage_rows()[0]
    assert row["cost_usd"] == pytest.approx(11 / 1_000_000 * 1.0 + 7 / 1_000_000 * 2.0)


# ----------------------------------------------------------------------
# 并发与开关（规格 §7）
# ----------------------------------------------------------------------


def test_ask_returns_429_when_disabled(
    user_client: TestClient, settings: Settings
) -> None:
    settings.ask_enabled = False

    response = ask(user_client)

    assert response.status_code == 429
    assert "问答已被管理员暂停" in response.json()["detail"]


def test_ask_returns_429_when_concurrency_is_full(
    user_client: TestClient, indexed: Path, fake_chat: FakeChat, settings: Settings
) -> None:
    limiter = cast("Any", user_client.app).state.ask_limiter

    for _ in range(settings.max_concurrent_asks):
        assert limiter.try_acquire()

    fake_chat.script = [final_answer("答案。")]
    full = ask(user_client)

    assert full.status_code == 429
    assert str(settings.max_concurrent_asks) in full.json()["detail"]

    for _ in range(settings.max_concurrent_asks):
        limiter.release()

    # 槽位归还后照常可用。
    assert ask(user_client).status_code == 200


def test_ask_requires_login(client: TestClient) -> None:
    response = client.post("/v1/ask", json={"question": QUESTION, "version": VERSION})

    assert response.status_code == 401


# ----------------------------------------------------------------------
# Accept: application/json 与模型配置（规格 §4/§8）
# ----------------------------------------------------------------------


def test_ask_returns_one_shot_json_when_requested(
    user_client: TestClient, indexed: Path, fake_chat: FakeChat
) -> None:
    fake_chat.script = [
        tool_call("search", query="Mob", detail="locate"),
        final_answer("Mob 定义了刷怪实体的基类。"),
    ]

    response = user_client.post(
        "/v1/ask",
        json={"question": QUESTION, "version": VERSION},
        headers={"Accept": "application/json"},
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/json")

    body = response.json()
    assert body["answer"] == "Mob 定义了刷怪实体的基类。"
    assert body["reason"] == "stop"
    assert body["steps"] == [{"tool": "search", "args": {"query": "Mob", "detail": "locate"}}]
    assert body["citations"], "一次性响应也要带引用"
    assert body["usage"]["prompt_tokens"] == 11 * 2


def test_ask_returns_503_when_model_is_not_configured(
    user_client: TestClient, indexed: Path, settings: Settings
) -> None:
    settings.llm_base_url = None
    settings.llm_model = None
    settings.llm_api_key = None

    response = ask(user_client)

    assert response.status_code == 503
    assert "MC_LIBRARIAN_LLM_BASE_URL" in response.json()["detail"]


def test_ask_returns_404_when_version_is_unregistered(
    user_client: TestClient, fake_chat: FakeChat
) -> None:
    response = ask(user_client)

    assert response.status_code == 404
    assert "未注册" in response.json()["detail"]
