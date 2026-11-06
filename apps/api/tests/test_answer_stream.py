"""`/v1/ask` 的流式模型调用（`stream_chat_completion` 与客户端用量兜底）。

用假的 SSE 上游（monkeypatch `urlopen` 返回逐行可迭代的假响应）验证：多个文本增量按顺序产出、
分片的 `tool_calls.arguments` 被拼成合法 JSON、`[DONE]` 后不再产出、5xx / 非法 JSON 抛 `ChatError`、
网关不认 `stream_options` 时去掉该字段重试一次、上游没给 usage 时用 cl100k 真分词兜底。
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from email.message import Message
from typing import Any
from urllib.error import HTTPError

import pytest

from mc_librarian.answer import client as answer_client
from mc_librarian.answer.client import (
    ChatChunk,
    ChatError,
    OpenAiCompatChatClient,
    TokenUsage,
    ToolCall,
    stream_chat_completion,
)
from mc_librarian.config import LlmConfig
from mc_librarian.indexing.text import count_tokens

ENDPOINT = "http://fake-llm.invalid/v1/chat/completions"


class _FakeStreamResponse:
    """假的流式响应：可当上下文管理器，逐行迭代出 bytes（模拟 `urlopen` 的返回）。"""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines

    def __iter__(self) -> Iterator[bytes]:
        return iter(self._lines)

    def __enter__(self) -> _FakeStreamResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def sse_lines(*payloads: dict[str, Any] | None) -> list[bytes]:
    """把若干块拼成 SSE 行；`None` 表示结束标记 `data: [DONE]`。"""
    lines: list[bytes] = []

    for payload in payloads:
        if payload is None:
            lines.append(b"data: [DONE]\n")
        else:
            lines.append(f"data: {json.dumps(payload, ensure_ascii=False)}\n".encode())

        lines.append(b"\n")

    return lines


def respond_with(lines: list[bytes]) -> Any:
    """返回一个替换 `urlopen` 的假实现（每次调用都给同一批 SSE 行）。"""
    return lambda *args, **kwargs: _FakeStreamResponse(lines)


# ----------------------------------------------------------------------
# SSE 解析
# ----------------------------------------------------------------------


def test_stream_chat_completion_yields_content_deltas_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """多个文本增量按到达顺序逐块产出，末块带上 usage。"""
    chunks: list[dict[str, Any]] = [
        {"choices": [{"delta": {"content": "第一"}}]},
        {"choices": [{"delta": {"content": "第二"}}]},
        {"choices": [{"delta": {"content": "第三"}}]},
        {
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 9},
        },
    ]
    monkeypatch.setattr(answer_client, "urlopen", respond_with(sse_lines(*chunks, None)))

    got = list(stream_chat_completion(ENDPOINT, {"stream": True}, "k", 1.0))

    assert [chunk.content for chunk in got if chunk.content] == ["第一", "第二", "第三"]
    assert got[-1].usage == TokenUsage(prompt_tokens=5, completion_tokens=9)
    assert got[-1].tool_calls == ()


def test_stream_chat_completion_joins_fragmented_tool_call_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`arguments` 在流式里是分片字符串：必须拼接，不能覆盖。"""
    fragments: list[dict[str, Any]] = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-1",
                                "function": {"name": "search", "arguments": '{"que'},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'ry": "Mob", '}}]}}
            ]
        },
        {
            "choices": [
                {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"limit": 5}'}}]}}
            ]
        },
        {
            "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4},
        },
    ]
    monkeypatch.setattr(answer_client, "urlopen", respond_with(sse_lines(*fragments, None)))

    got = list(stream_chat_completion(ENDPOINT, {"stream": True}, "k", 1.0))
    calls = got[-1].tool_calls

    assert len(calls) == 1
    assert calls[0].id == "call-1"
    assert calls[0].name == "search"
    assert json.loads(calls[0].arguments) == {"query": "Mob", "limit": 5}


def test_stream_chat_completion_stops_at_done(monkeypatch: pytest.MonkeyPatch) -> None:
    """`data: [DONE]` 之后的内容不再产出。"""
    lines = sse_lines(
        {"choices": [{"delta": {"content": "before"}}]},
        None,
        {"choices": [{"delta": {"content": "after"}}]},
    )
    monkeypatch.setattr(answer_client, "urlopen", respond_with(lines))

    got = list(stream_chat_completion(ENDPOINT, {"stream": True}, "k", 1.0))

    assert [chunk.content for chunk in got if chunk.content] == ["before"]


def test_stream_chat_completion_turns_5xx_into_chat_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """上游 5xx：抛 `ChatError`（调用方据此降级），不是裸的 HTTPError。"""

    def fake_urlopen(request: Any, timeout: float) -> Any:
        del timeout
        raise HTTPError(
            request.full_url, 503, "Service Unavailable", Message(), io.BytesIO(b"boom")
        )

    monkeypatch.setattr(answer_client, "urlopen", fake_urlopen)

    with pytest.raises(ChatError, match="HTTP 503"):
        list(stream_chat_completion(ENDPOINT, {"stream": True}, "k", 1.0))


def test_stream_chat_completion_turns_invalid_json_into_chat_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`data:` 不是合法 JSON：抛 `ChatError`。"""
    monkeypatch.setattr(
        answer_client, "urlopen", respond_with([b"data: {not json}\n", b"\n"])
    )

    with pytest.raises(ChatError, match="不是合法 JSON"):
        list(stream_chat_completion(ENDPOINT, {"stream": True}, "k", 1.0))


def test_stream_chat_completion_retries_without_stream_options_on_4xx(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """网关不认 `stream_options`（4xx）：去掉该字段重试一次并记 warn。"""
    payloads: list[dict[str, Any]] = []

    def fake_urlopen(request: Any, timeout: float) -> Any:
        del timeout
        payloads.append(json.loads(request.data.decode("utf-8")))

        if len(payloads) == 1:
            raise HTTPError(
                request.full_url,
                400,
                "Bad Request",
                Message(),
                io.BytesIO(b"unknown stream_options"),
            )

        return _FakeStreamResponse(sse_lines({"choices": [{"delta": {"content": "ok"}}]}, None))

    monkeypatch.setattr(answer_client, "urlopen", fake_urlopen)

    with caplog.at_level(logging.WARNING, logger="mc_librarian.answer.client"):
        got = list(
            stream_chat_completion(
                ENDPOINT,
                {"stream": True, "stream_options": {"include_usage": True}},
                "k",
                1.0,
            )
        )

    assert "stream_options" in payloads[0]
    assert "stream_options" not in payloads[1]
    assert "".join(chunk.content for chunk in got) == "ok"
    assert "stream_options" in caplog.text


# ----------------------------------------------------------------------
# 客户端用量兜底
# ----------------------------------------------------------------------


def test_openai_client_counts_tokens_when_upstream_omits_usage(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """上游末块没给 usage：用 cl100k 真分词对 messages 与累积正文计数，并记一条日志。"""

    def fake_stream(*args: Any, **kwargs: Any) -> Iterator[ChatChunk]:
        yield ChatChunk(content="你好")
        yield ChatChunk(content="，世界")
        yield ChatChunk(tool_calls=(), usage=None)

    monkeypatch.setattr(answer_client, "stream_chat_completion", fake_stream)
    client = OpenAiCompatChatClient(
        LlmConfig(base_url="http://fake-llm.invalid/v1", model="m", api_key="k", temperature=0.0)
    )

    with caplog.at_level(logging.INFO, logger="mc_librarian.answer.client"):
        got = list(
            client.stream(
                messages=[{"role": "user", "content": "问题"}],
                tools=[],
                max_tokens=10,
                timeout=1.0,
            )
        )

    usage = got[-1].usage
    assert usage is not None
    assert usage.prompt_tokens == count_tokens("问题")
    assert usage.completion_tokens == count_tokens("你好，世界")
    assert "cl100k" in caplog.text


# ----------------------------------------------------------------------
# 推理型模型：正文落在 reasoning_content
# ----------------------------------------------------------------------


def test_stream_parses_reasoning_deltas_separately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`delta.reasoning_content` 收进 `ChatChunk.reasoning`，不混进 `content`。"""
    lines = sse_lines(
        {"choices": [{"delta": {"reasoning_content": "先想想"}}]},
        {"choices": [{"delta": {"content": "答案"}}]},
        None,
    )
    monkeypatch.setattr(answer_client, "urlopen", respond_with(lines))

    chunks = list(stream_chat_completion(ENDPOINT, {}, "k", 10.0))

    assert [chunk.reasoning for chunk in chunks if chunk.reasoning] == ["先想想"]
    assert [chunk.content for chunk in chunks if chunk.content] == ["答案"]


def test_stream_falls_back_to_reasoning_when_content_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """整段 `content` 为空、只有推理字段时，用推理内容兜底，否则用户看到的是一条空答案。"""

    def fake_stream(*_args: Any, **_kwargs: Any) -> Any:
        # 同步生成器：`client.stream` 用 `for` 迭代它（不是 `async for`）。
        yield ChatChunk(reasoning="职责：甲")
        yield ChatChunk(reasoning="\n机制：乙")
        yield ChatChunk(tool_calls=(), usage=None)

    monkeypatch.setattr(answer_client, "stream_chat_completion", fake_stream)
    client = OpenAiCompatChatClient(
        LlmConfig(base_url="http://fake-llm.invalid/v1", model="m", api_key="k", temperature=0.0)
    )

    got = list(
        client.stream(
            messages=[{"role": "user", "content": "问题"}], tools=[], max_tokens=10, timeout=1.0
        )
    )

    assert "".join(chunk.content for chunk in got) == "职责：甲\n机制：乙"


def test_stream_does_not_leak_reasoning_on_tool_rounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """有工具调用时不兜底：那一轮的推理是「我接下来要查什么」，当正文就是泄漏思考过程。"""

    def fake_stream(*_args: Any, **_kwargs: Any) -> Any:
        yield ChatChunk(reasoning="我应该先查 NaturalSpawner")
        yield ChatChunk(tool_calls=(ToolCall(id="c1", name="search", arguments="{}"),), usage=None)

    monkeypatch.setattr(answer_client, "stream_chat_completion", fake_stream)
    client = OpenAiCompatChatClient(
        LlmConfig(base_url="http://fake-llm.invalid/v1", model="m", api_key="k", temperature=0.0)
    )

    got = list(
        client.stream(
            messages=[{"role": "user", "content": "问题"}], tools=[], max_tokens=10, timeout=1.0
        )
    )

    assert "".join(chunk.content for chunk in got) == ""
    assert got[-1].tool_calls[0].name == "search"
