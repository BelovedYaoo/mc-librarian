"""`/v1/ask` 的模型客户端：OpenAI 兼容的 chat completions（含 tool calls 与 usage）。

与摘要层（`indexing/summarize.py`）同一取舍：用标准库 `urllib` 发请求，不引入额外的 HTTP 运行时
依赖；调用点用 `asyncio.to_thread` 包一层，因此它本身是**同步阻塞**的（测试只替换
`post_chat_completion` 这一个函数，不联网）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from mc_librarian.config import LlmConfig


class ChatError(RuntimeError):
    """模型调用可预期的失败（网络 / 非 2xx / 响应结构不符）；调用方据此走降级。"""


@dataclass(frozen=True)
class ToolCall:
    """模型请求的一次工具调用；`arguments` 是原始 JSON 文本（可能不合法，调用方解析）。"""

    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class TokenUsage:
    """一次模型调用的 token 用量。"""

    prompt_tokens: int
    completion_tokens: int


@dataclass(frozen=True)
class ChatCompletion:
    """一次模型调用的结果：文本、工具调用与用量。"""

    content: str
    tool_calls: tuple[ToolCall, ...]
    usage: TokenUsage


class ChatClient(Protocol):
    """`/v1/ask` 的模型客户端接口；测试注入假实现。"""

    def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
        timeout: float,
    ) -> ChatCompletion: ...


def post_chat_completion(
    url: str, payload: dict[str, object], api_key: str, timeout: float
) -> dict[str, Any]:
    """发一次 chat completion 请求，返回解析后的完整响应体。

    失败（网络错误、非 2xx、响应不是 JSON 对象）统一抛 `ChatError`，由 `/v1/ask` 翻成降级。
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    try:
        # 端点由配置给出（环境变量），不是用户输入拼进来的。
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:500]
        raise ChatError(f"模型返回 HTTP {error.code}：{detail}") from error
    except (URLError, TimeoutError, OSError) as error:
        raise ChatError(f"模型请求失败：{error}") from error

    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ChatError(f"模型响应不是合法 JSON：{raw[:500]}") from error

    if not isinstance(loaded, dict):
        raise ChatError(f"模型响应不是 JSON 对象：{raw[:500]}")

    return loaded


def parse_completion(raw: dict[str, Any]) -> ChatCompletion:
    """把响应体解析成 `ChatCompletion`；结构不符时抛 `ChatError`。"""
    try:
        message = raw["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as error:
        raise ChatError(f"模型响应结构不符：{json.dumps(raw, ensure_ascii=False)[:500]}") from error

    if not isinstance(message, dict):
        raise ChatError("模型响应的 message 不是对象")

    content = message.get("content")
    content = "" if content is None else str(content)
    usage_raw = raw.get("usage") or {}
    usage = TokenUsage(
        prompt_tokens=_as_int(usage_raw.get("prompt_tokens")),
        completion_tokens=_as_int(usage_raw.get("completion_tokens")),
    )

    return ChatCompletion(content=content, tool_calls=_tool_calls(message), usage=usage)


def _tool_calls(message: dict[str, Any]) -> tuple[ToolCall, ...]:
    raw_calls = message.get("tool_calls") or []

    if not isinstance(raw_calls, list):
        raise ChatError("模型响应的 tool_calls 不是数组")

    calls: list[ToolCall] = []

    for index, raw in enumerate(raw_calls):
        if not isinstance(raw, dict):
            raise ChatError("模型响应里有一条 tool_call 不是对象")

        function = raw.get("function") or {}

        if not isinstance(function, dict) or not function.get("name"):
            raise ChatError("模型响应里有一条 tool_call 缺少 function.name")

        calls.append(
            ToolCall(
                # 有些兼容端点不回 id；补一个稳定的序号，tool 消息要回填它。
                id=str(raw.get("id") or f"call_{index}"),
                name=str(function["name"]),
                arguments=str(function.get("arguments") or "{}"),
            )
        )

    return tuple(calls)


def _as_int(value: object) -> int:
    if isinstance(value, (int, float, str)):
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    return 0


class OpenAiCompatChatClient:
    """OpenAI 兼容的 `POST {base_url}/chat/completions` 客户端。"""

    def __init__(self, config: LlmConfig) -> None:
        self._config = config

    def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
        timeout: float,
    ) -> ChatCompletion:
        payload: dict[str, object] = {
            "model": self._config.model,
            "temperature": 0,
            "messages": messages,
            "max_tokens": max_tokens,
        }

        # 不传空的 `tools`：强制收尾的那一次调用就是「不给工具」，让模型基于已有信息作答。
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        raw = post_chat_completion(
            f"{self._config.base_url}/chat/completions", payload, self._config.api_key, timeout
        )

        return parse_completion(raw)
