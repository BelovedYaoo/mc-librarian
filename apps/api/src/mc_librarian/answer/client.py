"""`/v1/ask` 的模型客户端：OpenAI 兼容的 chat completions（含 tool calls 与 usage）。

与摘要层（`indexing/summarize.py`）同一取舍：用标准库 `urllib` 发请求，不引入额外的 HTTP 运行时
依赖；调用点用 `asyncio.to_thread` 包一层，因此它本身是**同步阻塞**的（测试只替换
`post_chat_completion` / `stream_chat_completion` 这两个函数，不联网）。

问答默认走**流式**（`stream: true`）：模型一有增量就转发出去，前端因此是「边生成边显示」，
而不是等整段回答返回后一次性出现。非流式的 `post_chat_completion` 保留给其它调用路径。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Final, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from mc_librarian.config import LlmConfig
from mc_librarian.indexing.text import count_tokens

logger = logging.getLogger("mc_librarian.answer.client")

# 流式响应的 SSE 行前缀与结束标记（OpenAI 兼容端点）。
SSE_DATA_PREFIX: Final = "data:"
SSE_DONE: Final = "[DONE]"


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


@dataclass(frozen=True)
class ChatChunk:
    """流式响应的一块增量。

    `content` 是文本增量（边收边给，前端据此逐字渲染）；`tool_calls` 只在流结束时给一次
    （已按 `index` 把分片拼成完整调用）；`usage` 只在末块给（网关支持
    `stream_options.include_usage` 时）。
    """

    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    usage: TokenUsage | None = None


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

    def stream(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
        timeout: float,
    ) -> Iterator[ChatChunk]: ...


def post_chat_completion(
    url: str, payload: dict[str, object], api_key: str, timeout: float
) -> dict[str, Any]:
    """发一次 chat completion 请求，返回解析后的完整响应体。

    失败（网络错误、非 2xx、响应不是 JSON 对象）统一抛 `ChatError`，由 `/v1/ask` 翻成降级。
    """
    request = _chat_request(url, payload, api_key)

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


def stream_chat_completion(
    url: str, payload: dict[str, object], api_key: str, timeout: float
) -> Iterator[ChatChunk]:
    """流式发一次 chat completion（请求体由调用方带 `stream: true`），逐块产出增量。

    - 文本增量（`choices[0].delta.content`）边收边 yield；
    - `tool_calls` 的分片按 `index` 累积（`arguments` 是分片字符串，必须拼接），流结束时
      组装成一块完整调用；
    - 末块带 `usage` 时一并给出；
    - 网关对 `stream_options` 返回 4xx 时**去掉该字段重试一次**（只一次）并记一条 warn。

    失败（网络错误、非 2xx、`data:` 不是合法 JSON）统一抛 `ChatError`，与非流式路径同一套
    错误语义，调用方据此降级。
    """
    request_payload = payload

    while True:
        try:
            response = _open_stream(url, request_payload, api_key, timeout)
            break
        except HTTPError as error:
            # 有些兼容网关不认 `stream_options`（4xx）。去掉它重试一次；再失败就按原样报错。
            if "stream_options" in request_payload and 400 <= error.code < 500:
                logger.warning(
                    "网关不支持 stream_options（HTTP %d），去掉该字段重试一次", error.code
                )
                error.close()
                request_payload = {
                    key: value
                    for key, value in request_payload.items()
                    if key != "stream_options"
                }
                continue

            detail = error.read().decode("utf-8", errors="replace")[:500]
            raise ChatError(f"模型返回 HTTP {error.code}：{detail}") from error
        except (URLError, TimeoutError, OSError) as error:
            raise ChatError(f"模型请求失败：{error}") from error

    try:
        with response:
            yield from _iter_stream_chunks(response)
    except HTTPError as error:  # 读响应体时可能才暴露的非 2xx
        detail = error.read().decode("utf-8", errors="replace")[:500]
        raise ChatError(f"模型返回 HTTP {error.code}：{detail}") from error
    except (URLError, TimeoutError, OSError) as error:
        raise ChatError(f"模型请求失败：{error}") from error


def _open_stream(url: str, payload: dict[str, object], api_key: str, timeout: float) -> Any:
    """发起流式请求并返回响应对象（body 懒读，逐行到达）。"""
    # 端点由配置给出（环境变量），不是用户输入拼进来的。
    return urlopen(_chat_request(url, payload, api_key), timeout=timeout)


def _iter_stream_chunks(response: Any) -> Iterator[ChatChunk]:
    """逐行读 SSE：文本增量边收边 yield，tool_calls 分片攒到流结束再组装。"""
    fragments: dict[int, dict[str, str]] = {}
    usage: TokenUsage | None = None

    for raw_line in response:
        line = raw_line.decode("utf-8", errors="replace").strip()

        if not line.startswith(SSE_DATA_PREFIX):
            continue

        data = line[len(SSE_DATA_PREFIX) :].strip()

        if data == SSE_DONE:
            break

        try:
            chunk = json.loads(data)
        except json.JSONDecodeError as error:
            raise ChatError(f"模型流式响应不是合法 JSON：{data[:500]}") from error

        if not isinstance(chunk, dict):
            raise ChatError(f"模型流式响应不是 JSON 对象：{data[:500]}")

        chunk_usage = _usage_of(chunk)

        if chunk_usage is not None:
            usage = chunk_usage

        content = _delta_content(chunk)

        if content:
            yield ChatChunk(content=content)

        _accumulate_tool_calls(fragments, chunk)

    yield ChatChunk(tool_calls=_assemble_tool_calls(fragments), usage=usage)


def _usage_of(chunk: dict[str, Any]) -> TokenUsage | None:
    raw = chunk.get("usage")

    if not isinstance(raw, dict):
        return None

    return TokenUsage(
        prompt_tokens=_as_int(raw.get("prompt_tokens")),
        completion_tokens=_as_int(raw.get("completion_tokens")),
    )


def _delta_content(chunk: dict[str, Any]) -> str:
    """取 `choices[0].delta.content`（缺失时返回空串）。"""
    choices = chunk.get("choices")

    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return ""

    delta = choices[0].get("delta")

    if not isinstance(delta, dict):
        return ""

    content = delta.get("content")

    return "" if content is None else str(content)


def _accumulate_tool_calls(fragments: dict[int, dict[str, str]], chunk: dict[str, Any]) -> None:
    """把一块 `choices[0].delta.tool_calls[]` 按 `index` 并进累积表。

    `arguments` 在 OpenAI 流式里是**分片字符串**（一个 JSON 被切成好几块），必须拼接而不是
    覆盖；`id` / `name` 通常是整块给的，取到就记下。
    """
    choices = chunk.get("choices")

    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return

    delta = choices[0].get("delta")

    if not isinstance(delta, dict):
        return

    raw_calls = delta.get("tool_calls")

    if not isinstance(raw_calls, list):
        return

    for position, raw in enumerate(raw_calls):
        if not isinstance(raw, dict):
            continue

        index = raw.get("index")
        index = index if isinstance(index, int) else position
        slot = fragments.setdefault(index, {"id": "", "name": "", "arguments": ""})

        if raw.get("id"):
            slot["id"] = str(raw["id"])

        function = raw.get("function")

        if isinstance(function, dict):
            if function.get("name"):
                slot["name"] = str(function["name"])

            arguments = function.get("arguments")

            if arguments:
                slot["arguments"] += str(arguments)


def _assemble_tool_calls(fragments: dict[int, dict[str, str]]) -> tuple[ToolCall, ...]:
    """把累积的分片按 `index` 组装成完整调用；缺 `function.name` 时抛 `ChatError`。"""
    calls: list[ToolCall] = []

    for index in sorted(fragments):
        slot = fragments[index]

        if not slot["name"]:
            raise ChatError("模型流式响应的 tool_call 缺少 function.name")

        calls.append(
            ToolCall(
                # 有些兼容端点不回 id；补一个稳定的序号，tool 消息要回填它。
                id=slot["id"] or f"call_{index}",
                name=slot["name"],
                arguments=slot["arguments"] or "{}",
            )
        )

    return tuple(calls)


def _chat_request(url: str, payload: dict[str, object], api_key: str) -> Request:
    """把 payload 编码成一次 POST 请求（非流式与流式共用）。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    return Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )


def _messages_text(messages: list[dict[str, Any]]) -> str:
    """把实际发出的 messages 拼成纯文本（上游没给 usage 时按 cl100k 真分词计数）。"""
    parts: list[str] = []

    for message in messages:
        content = message.get("content")

        if content:
            parts.append(str(content))

        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue

            function = call.get("function")

            if isinstance(function, dict):
                parts.append(str(function.get("name") or ""))
                parts.append(str(function.get("arguments") or ""))

    return "\n".join(parts)


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
        raw = post_chat_completion(
            f"{self._config.base_url}/chat/completions",
            self._payload(messages, tools, max_tokens),
            self._config.api_key,
            timeout,
        )

        return parse_completion(raw)

    def stream(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
        timeout: float,
    ) -> Iterator[ChatChunk]:
        """流式调用：文本增量边收边转发，末块带上组装好的 tool_calls 与 usage。

        上游末块没给 `usage` 时，用 `count_tokens`（cl100k 真分词）对**实际发出的 messages**
        与**累积的 completion 文本**重新计数——这是真分词，不是「字符 ÷ 3.5」那种估算；
        两条来源各记一条日志，方便排查网关是否支持 `stream_options.include_usage`。
        """
        payload = self._payload(messages, tools, max_tokens)
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}

        content_parts: list[str] = []
        final = ChatChunk()

        for chunk in stream_chat_completion(
            f"{self._config.base_url}/chat/completions", payload, self._config.api_key, timeout
        ):
            if chunk.content:
                content_parts.append(chunk.content)
                yield chunk
                continue

            final = chunk

        if final.usage is None:
            usage = TokenUsage(
                prompt_tokens=count_tokens(_messages_text(messages)),
                completion_tokens=count_tokens("".join(content_parts)),
            )
            logger.info(
                "问答用量：上游未给 usage（网关可能不支持 stream_options.include_usage），"
                "改用 cl100k 真分词计数（prompt=%d, completion=%d）",
                usage.prompt_tokens,
                usage.completion_tokens,
            )
        else:
            usage = final.usage
            logger.debug(
                "问答用量：采用上游 usage（prompt=%d, completion=%d）",
                usage.prompt_tokens,
                usage.completion_tokens,
            )

        yield ChatChunk(tool_calls=final.tool_calls, usage=usage)

    def _payload(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "model": self._config.model,
            "temperature": self._config.temperature,
            "messages": messages,
            "max_tokens": max_tokens,
        }

        # 不传空的 `tools`：强制收尾的那一次调用就是「不给工具」，让模型基于已有信息作答。
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        return payload
