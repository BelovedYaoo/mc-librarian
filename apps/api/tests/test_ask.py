"""中文问答 `POST /v1/ask`（issue #8）。

索引与源码都写在 `tmp_path`（复用 #5/#6 的 fixture 源码树），LLM 由 `conftest.FakeChat` 脚本化，
不联网。覆盖规格 §1–§8：事件序列、`max_steps` 收尾、降级（含第一步失败先跑一次 search）、
上下文预算、`usage` 落库、并发与开关（含客户端中途断开）、`Accept: application/json`、模型未配置。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from urllib.error import URLError

import pytest
from fastapi.testclient import TestClient

from conftest import FakeChat, final_answer, session_cookie, tool_call, tool_calls
from mc_librarian import alerts
from mc_librarian.answer import client as answer_client
from mc_librarian.answer.client import ChatError, post_chat_completion
from mc_librarian.answer.tools import ToolBox, ToolOutcome
from mc_librarian.api.v1 import answer as answer_api
from mc_librarian.config import SESSION_COOKIE_NAME, Settings
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
                "SELECT user_id, kind, model, prompt_tokens, completion_tokens,"
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


def test_ask_emits_multiple_delta_events_in_order(
    user_client: TestClient, indexed: Path, fake_chat: FakeChat
) -> None:
    """一轮回答拆成多个 `delta` 逐块到达（不是最后一次性给整段），顺序与内容都正确。"""
    fake_chat.script = [final_answer("Mob 定义了刷怪实体的基类。")]

    events = parse_sse(ask(user_client).text)
    deltas = [event["text"] for event in events if event["type"] == "delta"]

    assert len(deltas) >= 3, "流式回答应当产出多个 delta 事件"
    assert "".join(deltas) == "Mob 定义了刷怪实体的基类。"


def test_ask_reports_tool_failure_to_the_model(
    user_client: TestClient, indexed: Path, fake_chat: FakeChat
) -> None:
    """工具失败（参数缺失）不 500：`ok=false` 的 step 事件 + 失败说明回给模型。"""
    fake_chat.script = [tool_call("search"), final_answer("好。")]

    events = parse_sse(ask(user_client).text)

    failed = events[1]

    assert failed["type"] == "step"
    assert failed["tool"] == "search"
    assert failed["ok"] is False
    assert failed["hits"] == 0
    # 失败原因要出现在事件里：界面据此解释「为什么失败」，否则只显示一个「失败」，
    # 排查时得把参数捞出来本地复放才知道原因（这次就是这么查出来的）。
    assert "参数不合法" in failed["error"]

    tool_messages = [
        message
        for message in fake_chat.calls[1]["messages"]
        if message["role"] == "tool"
    ]
    assert "参数不合法" in tool_messages[0]["content"]


def test_ask_streams_an_error_event_when_the_loop_breaks(
    user_client: TestClient, indexed: Path, fake_chat: FakeChat, monkeypatch: pytest.MonkeyPatch
) -> None:
    """流中途抛未预期异常：状态码已经发出去了，只能补一个 `error` 事件收尾（不静默断流）。"""
    fake_chat.script = [tool_call("search", query="Mob", detail="locate")]

    def explode(self: Any, name: str, args: dict[str, Any]) -> Any:
        raise RuntimeError("工具炸了")

    monkeypatch.setattr(ToolBox, "execute", explode)

    limiter = cast("Any", user_client.app).state.ask_limiter
    events = parse_sse(ask(user_client).text)

    assert events[-1]["type"] == "error"
    assert events[-1]["problem"]["detail"] == "工具炸了"
    assert not any(event["type"] == "done" for event in events)
    # 出错也必须归还槽位，否则一次失败就把并发上限永久压低一格。
    assert limiter.active == 0


# ----------------------------------------------------------------------
# 同一轮的多个工具调用（并发执行，结果按请求顺序）
# ----------------------------------------------------------------------


def test_ask_runs_every_tool_call_of_one_turn(
    user_client: TestClient, indexed: Path, fake_chat: FakeChat
) -> None:
    """一轮里模型要两个工具：两个都执行，两条结果都进下一轮的 messages。"""
    fake_chat.script = [
        tool_calls(
            tool_call("search", query="Mob", detail="locate"),
            tool_call("symbol", name="Mob"),
        ),
        final_answer("Mob 是刷怪实体的基类。"),
    ]

    events = parse_sse(ask(user_client).text)
    steps = [event for event in events if event["type"] == "step"]

    # 整批的「开始」先全部发出（各带 args），跑完再按同一顺序发「完成」（各带 ok/hits）。
    assert steps[:2] == [
        {"type": "step", "tool": "search", "args": {"query": "Mob", "detail": "locate"}},
        {"type": "step", "tool": "symbol", "args": {"name": "Mob"}},
    ]
    assert [event["tool"] for event in steps[2:]] == ["search", "symbol"]
    assert all(event["ok"] is True and event["hits"] >= 1 for event in steps[2:])

    # 下一轮请求里两条工具结果都在，且与各自的调用对得上（`tool_call_id` 不错位）。
    tool_messages = [
        message for message in fake_chat.calls[1]["messages"] if message["role"] == "tool"
    ]
    assert [message["tool_call_id"] for message in tool_messages] == [
        "call-search-0",
        "call-symbol-1",
    ]
    assert "命中" in tool_messages[0]["content"]
    assert "Mob" in tool_messages[1]["content"]


def test_ask_reports_batch_results_in_request_order(
    user_client: TestClient, indexed: Path, fake_chat: FakeChat, monkeypatch: pytest.MonkeyPatch
) -> None:
    """先发起的调用慢、后发起的快：「完成」事件仍按请求顺序发（按完成先后发前端列表会跳）。"""
    fake_chat.script = [
        tool_calls(
            tool_call("search", query="Mob", detail="locate"),
            tool_call("symbol", name="ZzzNoSuchSymbol"),
        ),
        final_answer("好。"),
    ]

    real_execute = ToolBox.execute

    def slow_first(self: ToolBox, name: str, args: dict[str, Any]) -> Any:
        if name == "search":
            time.sleep(0.05)  # 让同一批里的第二个调用先跑完

        return real_execute(self, name, args)

    monkeypatch.setattr(ToolBox, "execute", slow_first)

    events = parse_sse(ask(user_client).text)
    steps = [event for event in events if event["type"] == "step"]

    assert [event["tool"] for event in steps[:2]] == ["search", "symbol"]
    # search 命中、symbol 为 0：按完成先后发的话这里会反过来。
    assert steps[2]["hits"] >= 1
    assert steps[3]["hits"] == 0

    tool_messages = [
        message for message in fake_chat.calls[1]["messages"] if message["role"] == "tool"
    ]
    assert "命中" in tool_messages[0]["content"]
    assert "没有找到这个符号" in tool_messages[1]["content"]


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


def test_ask_degrades_when_the_model_times_out(
    user_client: TestClient, indexed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真超时：底层抛的是 `TimeoutError`（不是 `ChatError`），同样走降级，HTTP 仍 200。"""

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise TimeoutError("timed out")

    # 问答走流式，底层发送函数是 `stream_chat_completion`（`fake_chat` 已把它换成假实现，
    # 这里再换成直接抛超时的版本）。
    monkeypatch.setattr(answer_client, "stream_chat_completion", explode)

    response = ask(user_client)

    assert response.status_code == 200, response.text

    events = parse_sse(response.text)
    assert events[-1]["reason"] == "degraded"
    assert any(event["type"] == "citation" for event in events)


def test_ask_degrades_when_the_total_timeout_expires(
    user_client: TestClient,
    indexed: Path,
    fake_chat: FakeChat,
    settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """总超时（`ask_timeout_s`）到点：一次模型都不调，直接用降级路径作答。"""
    # 直接改属性、绕过 ge=1 的下限：负数让 `_expired()` 一进循环就成立，不依赖时钟精度。
    settings.ask_timeout_s = -1

    with caplog.at_level(logging.WARNING, logger="mc_librarian.answer"):
        events = parse_sse(ask(user_client).text)

    assert fake_chat.calls == [], "超时到点后不该再调模型"
    assert events[1]["ok"] is True, "降级路径要用用户问题先跑一次 search"
    assert events[-1]["reason"] == "degraded"
    assert any(event["type"] == "citation" for event in events)
    assert "总超时" in caplog.text


@pytest.mark.parametrize(
    "failure",
    [TimeoutError("timed out"), URLError("name resolution failed"), OSError("connection reset")],
)
def test_post_chat_completion_turns_transport_failures_into_chat_error(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    """`urlopen` 的传输层异常（超时 / 网络 / OS）统一翻成 `ChatError`，`AskRun` 据此降级。"""

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise failure

    monkeypatch.setattr(answer_client, "urlopen", explode)

    # 直接调真实函数（`fake_chat` 已经把模块属性换成了假实现）。
    with pytest.raises(ChatError, match="模型请求失败") as raised:
        post_chat_completion(
            "http://fake-llm.invalid/v1/chat/completions", {"model": "fake-model"}, "fake-key", 1.0
        )

    assert raised.value.__cause__ is failure


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


def test_ask_compacts_context_once_per_tool_batch(
    user_client: TestClient,
    indexed: Path,
    fake_chat: FakeChat,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一轮的多个工具结果都追加完才检查一次预算：整批只压一次。

    每个工具结果固定 301 token（`"word " * 300`），预算 1500（目标水位 1050）。前 3 轮各一次
    调用（903 token）正好不超；第 4 轮一次要两个结果（总量 1505）。串行实现会在第一个结果后
    压一次、第二个结果后又压一次（界面上是「调两次 → 压缩 → 调两次 → 压缩」），整批只压一次
    时这两条会在**同一条**事件里一起丢。
    """
    settings.ask_max_context_tokens = 1500
    fake_chat.script = [
        tool_call("search", query="first", detail="locate"),
        tool_call("search", query="second", detail="locate"),
        tool_call("search", query="third", detail="locate"),
        tool_calls(
            tool_call("search", query="fourth", detail="locate"),
            tool_call("search", query="fifth", detail="locate"),
        ),
        final_answer("好。"),
    ]

    def fixed_size(self: ToolBox, name: str, args: dict[str, Any]) -> ToolOutcome:
        del self, name, args

        return ToolOutcome(ok=True, text="word " * 300, hits=1)

    monkeypatch.setattr(ToolBox, "execute", fixed_size)

    events = parse_sse(ask(user_client).text)
    budgets = [event for event in events if event.get("tool") == "context_budget"]

    # 标签按批前分配：第 4 轮的两条是 search#4 / search#5，被丢的只能是最旧的两条。
    assert [event["args"]["dropped"] for event in budgets] == [["search#1", "search#2"]]


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


def test_ask_returns_ok_when_alert_webhook_fails(
    user_client: TestClient,
    indexed: Path,
    fake_chat: FakeChat,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    usage_rows: Any,
) -> None:
    """#12 验收第 4 条：超阈值不阻断请求——Webhook 抛异常时 `/v1/ask` 仍正常返回并记账。"""
    settings.alerts_webhook_url = "https://hook.invalid/alerts"
    settings.alerts_daily_token_threshold = 1
    fake_chat.script = [final_answer("答案。")]

    def boom(url: str, payload: dict[str, object], timeout: float) -> None:
        raise alerts.AlertError("Webhook 请求失败：connection refused")

    monkeypatch.setattr(alerts, "post_webhook", boom)

    response = ask(user_client)

    assert response.status_code == 200
    assert "event: done" in response.text
    # 告警是旁路：用量照记，问答结果不受影响。
    assert len(usage_rows()) == 1


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
    limit = settings.max_concurrent_asks

    for _ in range(limit):
        assert limiter.try_acquire(limit)

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


def test_ask_keeps_generating_after_the_client_disconnects(
    user_client: TestClient, indexed: Path, fake_chat: FakeChat, control_db: Path
) -> None:
    """客户端中途断开：生成继续跑完、答案照样落库，槽位在**生成结束**时才归还。

    这是这次架构改动的核心不变量。之前生成由 HTTP 响应驱动，客户端一断 Starlette 就取消流
    任务，循环停在 `yield`——用户切到别的页面等于把这次问答掐死。现在生成是后台任务，
    连接只是订阅视图。

    httpx 的 ASGI transport 不会在流中途发 `http.disconnect`，所以直接驱动 ASGI 应用：
    收到 2 个 SSE 事件后从 `receive` 回 `http.disconnect`。
    """
    fake_chat.always_tools = tools_of(tool_call("search", query="Mob", detail="locate"))

    app = cast("Any", user_client.app)
    limiter = app.state.ask_limiter
    runs = app.state.ask_runs
    cookie = session_cookie(user_client)
    payload = json.dumps({"question": QUESTION, "version": VERSION}).encode()
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/ask",
        "raw_path": b"/v1/ask",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode()),
            (b"cookie", f"{SESSION_COOKIE_NAME}={cookie}".encode()),
        ],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }

    async def drive() -> dict[str, Any]:
        sent = 0
        pending = payload

        async def receive() -> dict[str, Any]:
            nonlocal pending

            if pending:
                body, pending = pending, b""

                return {"type": "http.request", "body": body, "more_body": False}

            while sent < 2:  # 等两个 SSE 事件发出去，再断开
                await asyncio.sleep(0)

            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            nonlocal sent

            if message["type"] == "http.response.body" and message.get("more_body"):
                sent += 1

            await asyncio.sleep(0)

        await app(scope, receive, send)

        # 响应结束（客户端已断开）：生成必须还在跑，槽位必须还占着——活儿没干完。
        in_flight = runs.in_flight()
        snapshot = {
            "sent": sent,
            "in_flight": len(in_flight),
            "active_after_disconnect": limiter.active,
        }

        # 等后台任务自然跑完（不再有观看者）。
        if in_flight:
            await asyncio.wait_for(in_flight[0].task, timeout=30)

        snapshot["active_after_finish"] = limiter.active
        snapshot["in_flight_after_finish"] = len(runs.in_flight())

        return snapshot

    snapshot = asyncio.run(drive())

    assert snapshot["sent"] >= 2, "断开前至少要有两个事件发出去"
    assert snapshot["in_flight"] == 1, "断开后生成必须继续（不能被连接取消）"
    assert snapshot["active_after_disconnect"] == 1, "生成没结束，槽位不该归还"
    assert snapshot["active_after_finish"] == 0, "生成结束后槽位必须归还"
    assert snapshot["in_flight_after_finish"] == 0

    # 没人观看也照样落库，而且不是「半截 + 中断标注」。
    with sqlite3.connect(control_db) as conn:
        rows = conn.execute(
            "SELECT content FROM messages WHERE role = 'assistant' ORDER BY created_at DESC LIMIT 1"
        ).fetchall()

    assert rows, "断开后跑完的答案必须落库"
    assert "连接中断" not in rows[0][0]


def test_stream_conversation_returns_404_when_nothing_is_running(
    user_client: TestClient, indexed: Path, fake_chat: FakeChat
) -> None:
    """没有在跑的运行：`/stream` 回 404，前端据此改读历史消息。"""
    events = parse_sse(ask(user_client, max_steps=1).text)
    conversation_id = next(event for event in events if event["type"] == "done")["conversation_id"]

    response = user_client.get(f"/v1/conversations/{conversation_id}/stream")

    assert response.status_code == 404, response.text


def test_ask_releases_the_slot_when_the_run_cannot_be_built(
    user_client: TestClient, indexed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """构造 `AskRun` 就失败：槽位也必须在受保护区间里归还（否则闸门永久收紧）。"""

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("构造炸了")

    monkeypatch.setattr(answer_api, "AskRun", explode)

    limiter = cast("Any", user_client.app).state.ask_limiter

    with pytest.raises(RuntimeError, match="构造炸了"):
        ask(user_client)

    assert limiter.active == 0


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

def test_fit_budget_leaves_headroom_below_the_cap() -> None:
    """压缩要压到**目标水位**（预算 × 比例），不是刚好卡在上限。

    只压到刚好达标的话，压完整段上下文就贴着上限，下一次工具调用立刻又超——表现为
    「压缩 → 调一两次 → 又压缩」的抖动。这条用例直接对 `_fit_budget` 断言水位。
    """
    from mc_librarian.answer.agent import COMPACT_TARGET_RATIO, AskRun, _tool_tokens

    budget = 1000
    run = AskRun.__new__(AskRun)  # `_fit_budget` 只用到 `max_context_tokens`
    run.max_context_tokens = budget

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "问题"},
    ]

    # 6 轮工具调用，每轮结果约 700 token；总量远超预算，但丢掉最旧两轮就够「刚好达标」。
    for index in range(6):
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": f"c{index}", "type": "function"}],
                # 压缩按「轮」丢：`_exchange_count` / `_first_exchange` 认的就是这个标记。
                "_exchange": True,
            }
        )
        messages.append(
            {
                "role": "tool",
                "tool_call_id": f"c{index}",
                # 用词而不是重复字符：`"x" * N` 会被 tokenizer 压得很低，量不出水位。
                "content": "word " * 700,  # 约 700 token
                "_tool": True,
                "_label": f"search#{index}",
            }
        )

    before = _tool_tokens(messages)
    dropped = run._fit_budget(messages)
    after = _tool_tokens(messages)
    target = int(budget * COMPACT_TARGET_RATIO)

    assert before > budget
    assert dropped, "超预算时必须丢东西"
    assert after <= target, f"压完应留余量：{after} 应 <= {target}（预算 {budget}）"
    # 丢掉的是最旧的那些，最新的那一轮必须还在（模型要继续用它作答）。
    assert "search#0" in dropped
    assert "search#5" not in dropped


def test_streamed_deltas_match_persisted_answer(
    user_client: TestClient, indexed: Path, fake_chat: FakeChat
) -> None:
    """界面所见 == 落库内容：把所有 `delta` 拼起来，必须等于历史里的助手消息。

    流式最容易出的两类偏差都在这里会被抓住：
    1. 正文边流边发、`_finish` 又整段补发一遍（重复）；
    2. 工具轮里模型先说了几句（「让我查一下」），那几句已经显示给用户，但 `answer` 只保留
       最后一轮的内容（丢失）。
    """
    fake_chat.script = [
        tool_call("search", query="Mob", detail="locate"),
        final_answer("刷怪上限按 MobCategory 的每区块上限算。"),
    ]

    events = parse_sse(ask(user_client).text)
    streamed = "".join(event["text"] for event in events if event["type"] == "delta")
    done = next(event for event in events if event["type"] == "done")

    assert streamed.strip() == "刷怪上限按 MobCategory 的每区块上限算。"

    detail = user_client.get(f"/v1/conversations/{done['conversation_id']}").json()
    assistant = next(m for m in detail["messages"] if m["role"] == "assistant")

    assert assistant["content"] == streamed


def test_read_patch_rebuilds_hunks_from_the_index(tmp_path: Path) -> None:
    """补丁正文从 `patches` 表按 hunk 顺序重建；不是补丁（或没这张表）返回 None。

    模型从 `search` 命中的 `path`（`xxx.java.patch`）直接调 `read` 是很自然的动作，
    但 `.patch` 不在源码树里——读不到就白花一轮（实测 4 次 `read` 失败里 2 次是这个）。
    """
    from mc_librarian.retrieval import read_patch

    db = sqlite3.connect(tmp_path / "index.sqlite")
    db.execute(
        "CREATE TABLE patches (id INTEGER PRIMARY KEY, path TEXT, hunk_index INTEGER,"
        " target TEXT, old_start INTEGER, old_lines INTEGER, new_start INTEGER,"
        " new_lines INTEGER, context TEXT, tokens INTEGER)"
    )
    # 用 `chr(10)` 拼换行：在脚本里写 `\\n` 的转义层数最容易数错（已被坑两次）。
    NL = chr(10)
    db.executemany(
        "INSERT INTO patches (path, hunk_index, context) VALUES (?, ?, ?)",
        [
            ("a/B.java.patch", 1, NL.join(["@@ -20,3 +_,4 @@", "-  old", "+  new"])),
            ("a/B.java.patch", 0, NL.join(["@@ -5,2 +_,2 @@", "   same", "+  added"])),
        ],
    )
    db.commit()

    text = read_patch(db, "a/B.java.patch")

    assert text is not None
    # 顺序按 hunk_index，而不是插入顺序。
    assert text.index("@@ -5,2") < text.index("@@ -20,3")
    assert read_patch(db, "a/Nope.java.patch") is None
    db.close()


def test_read_tool_clamps_end_to_file_length(
    user_client: TestClient, indexed: Path, fake_chat: FakeChat
) -> None:
    """`end` 超出文件长度时**夹到末尾**并返回实际区间，不报错。

    模型无从知道文件有多少行，猜大很常见；报错只会让它再试一次。
    这里用 fixture 里真实存在的 `Mob.java`，故意要 `end=9999`。
    """
    fake_chat.script = [
        tool_call("read", path="net/minecraft/world/Mob.java", start=1, end=9999),
        final_answer("读到了。"),
    ]

    events = parse_sse(ask(user_client).text)
    step = next(e for e in events if e.get("type") == "step" and e.get("ok") is not None)

    assert step["ok"] is True, step.get("error")


def test_history_keeps_ordered_segments_and_separates_rounds(
    user_client: TestClient, indexed: Path, fake_chat: FakeChat
) -> None:
    """历史里要能还原「正文 → 工具批次 → 正文」的顺序，且两段正文之间不能粘在一起。

    两个真实反馈都落在这一条上：

    1. 离开问答页再回来，工具轨迹全没了，只剩一大段正文——因为只存了 `content`；
    2. 模型在两次调工具之间说的那句话，和最终答案拼成了
       `…相关的源码。## 一、整体链路`，连 Markdown 标题都被吃掉。

    所以断言两件事：`segments` 的顺序是 text/steps/text，`content` 里两段之间有分隔。
    """
    first_round = tool_call("search", query="Mob", detail="locate")
    first_round["content"] = "我先看一下刷怪相关的类。"

    fake_chat.script = [first_round, final_answer("## 结论\n\n刷怪上限在 NaturalSpawner。")]

    response = ask(user_client)
    events = parse_sse(response.text)
    done = next(event for event in events if event["type"] == "done")
    conversation_id = done["conversation_id"]

    detail = user_client.get(f"/v1/conversations/{conversation_id}")

    assert detail.status_code == 200, detail.text

    messages = detail.json()["messages"]
    assistant = next(item for item in messages if item["role"] == "assistant")
    segments = assistant["segments"]

    assert [segment["kind"] for segment in segments] == ["text", "steps", "text"]
    assert segments[0]["text"] == "我先看一下刷怪相关的类。"
    assert segments[1]["steps"][0]["tool"] == "search"
    assert segments[1]["steps"][0]["ok"] is True
    assert "## 结论" in segments[2]["text"]

    # 完整正文仍要能独立使用（检索、导出都读它），且两段之间有分隔。
    assert "我先看一下刷怪相关的类。\n\n## 结论" in assistant["content"]


def test_adjacent_tool_batches_merge_into_one_segment(
    user_client: TestClient, indexed: Path, fake_chat: FakeChat
) -> None:
    """连续几轮工具调用（中间不说话）要合并成一个片段。

    一个批次一个片段时，历史恢复会显示成一叠「已调用 2 次工具」的卡片——实测一次问答 8 张，
    而且每张都不到 5 行，所以「最多显示 5 行 + 滚动」的上限根本用不上，看着像没生效。
    """
    fake_chat.script = [
        tool_calls(
            tool_call("search", query="Mob", detail="locate"),
            tool_call("symbol", name="Mob"),
        ),
        tool_call("search", query="Mob", detail="locate"),
        final_answer("## 结论\n\n刷怪上限在 NaturalSpawner。"),
    ]

    events = parse_sse(ask(user_client).text)
    done = next(event for event in events if event["type"] == "done")
    detail = user_client.get(f"/v1/conversations/{done['conversation_id']}").json()
    assistant = next(item for item in detail["messages"] if item["role"] == "assistant")

    segments = assistant["segments"]

    assert [segment["kind"] for segment in segments] == ["steps", "text"], segments
    steps = segments[0]["steps"]
    # 两批共 3 次调用，全在同一条片段里，且每条都配上了结果。
    assert [step["tool"] for step in steps] == ["search", "symbol", "search"]
    assert all(step["ok"] is True for step in steps), steps


def test_search_tool_uses_smart_retrieval(
    user_client: TestClient, indexed: Path, fake_chat: FakeChat, monkeypatch: pytest.MonkeyPatch
) -> None:
    """问答的 `search` 工具**默认走智能检索**：模型自己写的查询未必是最优召回词。

    纯字面 43% → 智能 93%（30 题实测），所以这里不接受「省两次 LLM 调用」的优化。
    """
    from mc_librarian.retrieval import search_smart as original

    calls: list[str] = []

    def spy(*args: Any, **kwargs: Any) -> Any:
        calls.append("smart")
        return original(*args, **kwargs)

    # 用字符串路径 patch：`tools.search_smart` 是从 `retrieval` 导入的名字，
    # 在 mypy 的 no-implicit-reexport 下不算“本模块导出”。
    monkeypatch.setattr("mc_librarian.answer.tools.search_smart", spy)
    fake_chat.script = [tool_call("search", query="Mob", detail="locate"), final_answer("好。")]

    ask(user_client)

    assert calls == ["smart"], "search 工具必须调用智能检索"


def test_smart_search_skips_llm_for_identifier_queries(
    user_client: TestClient, settings: Settings, conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """纯英文标识符查询直通字面：字面通道本来就是精确匹配，不该白花两次 LLM 调用。"""
    from mc_librarian.indexing import query as query_module
    from mc_librarian.indexing.layout import index_path
    from mc_librarian.retrieval import service as service_module
    from mc_librarian.schemas import SearchRequest

    register_version(settings, conn)

    def boom(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("纯标识符查询不应调用 LLM 扩展")

    monkeypatch.setattr(service_module, "expand_query", boom)
    index = query_module.open_readonly(index_path(settings.data_dir, VERSION))

    try:
        response = service_module.search_smart(
            index, settings, SearchRequest(version=VERSION, query="Mob", mode="smart", limit=5)
        )
    finally:
        index.close()

    assert response.hits, response.hits
