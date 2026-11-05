"""`/v1/ask` 的服务端循环：工具调用 → 上下文预算 → 收尾（含降级）（issue #8 规格 §2–§5）。

一次问答就是一段异步事件流（`step` / `delta` / `citation` / `done`），HTTP 层把它翻成 SSE 或
一次性 `AskResponse`。三种收尾都有明确交代：

- 模型不再调用工具 → `done`（`reason=stop`）；
- 达到 `max_steps` → 不再给工具、要求基于已有信息作答，`reason=max_steps`；
- 模型超时 / 不可用 / 返回非法响应 → 用已召回的检索结果降级，`reason=degraded`（HTTP 仍 200）。

同一轮模型响应里的多个工具调用**并发**执行（工具全是只读检索，无共享写状态；共用的只读索引
连接因此关掉了语句缓存，见 `indexing.query.open_readonly`），事件仍按请求顺序成对发出：先为整批
发「开始」的 `step`，整批跑完再按原顺序发「完成」的 `step`。

上下文预算（`max_context_tokens`）超出时从最旧的工具结果开始丢弃（保留系统提示与用户问题），
并在轨迹里记一条 `step` 事件说明丢了哪些。预算是**每批工具调用之后**检查一次，不是每次调用
之后：否则同一轮里会连续产出多条「压缩上下文」，界面上表现为「调两次 → 压缩 → 调两次 → 压缩」。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Final, cast

from mc_librarian.answer.client import (
    ChatChunk,
    ChatClient,
    ChatCompletion,
    ChatError,
    TokenUsage,
    ToolCall,
)
from mc_librarian.answer.tools import SEARCH_TOOL, TOOL_SCHEMAS, ToolBox, ToolOutcome
from mc_librarian.indexing.text import count_tokens
from mc_librarian.schemas import AskReason, AskStep, Citation, Usage

logger = logging.getLogger("mc_librarian.answer")

SYSTEM_PROMPT: Final = (
    "你是 Minecraft 源码知识库的中文问答助手。基于工具检索到的源码用中文回答，"
    "不要凭记忆编造类名、方法名或行号。\n"
    "工作方式：先用 search 定位相关类与方法（detail=locate 省 token），再用 outline 看骨架、"
    "read 精读关键方法；需要时用 symbol 找符号、references 反查调用点。\n"
    "回答里提到代码位置时写成 `路径:起始行-结束行` 的形式；检索不到就直说检索不到。"
)

# 工具轨迹里表示「丢弃了超预算的工具结果」的伪工具名（规格 §3）。
CONTEXT_BUDGET_TOOL: Final = "context_budget"
# 降级时 `delta` 输出的说明（规格 §5）。
DEGRADED_NOTICE: Final = "模型暂不可用，以下是相关代码位置"
# 达到步数上限时答案开头的说明（规格 §2）。
# 失败原因写进事件的长度上限：够说清「行号越界 / 文件不存在 / 参数非法」，
# 又不至于把整段工具输出塞进事件。
STEP_ERROR_CHARS: Final = 300

MAX_STEPS_NOTICE: Final = "已达检索步数上限，以下基于已获取的信息"
# 收尾时追加给模型的指令：光撤掉工具不够——模型会继续「想调工具」而给出空文本，
# 必须明确要求它现在就答（实测 deepseek 这类推理模型在无工具时会返回空 content）。
FORCE_ANSWER_PROMPT: Final = (
    "已达到检索步数上限。请立即基于上面已经获取到的信息直接给出最终答案，"
    "不要再请求任何工具。如果信息不足，请明确指出还缺什么。"
)
# 引用去重后的条数上限：一次问答最多给前端这么多可点击位置。
MAX_CITATIONS: Final = 20
# 截断到剩余预算时，单个工具结果至少留这么多 token（否则模型看不到任何内容）。
MIN_RESULT_TOKENS: Final = 200

# 压缩的目标水位 = 预算 × 该比例。**不能只压到刚好低于预算**：那样压完就贴着上限，
# 下一次工具调用（一次 `read` 就是几千 token）立刻又超，于是出现「压缩 → 调一两次 →
# 又压缩」的抖动。留 30% 余量能把压缩间隔拉开到十几次调用一次。
COMPACT_TARGET_RATIO: Final = 0.7

# 流式桥的结束哨兵：独立对象而不是 `None`，避免与块本身混淆。
_STREAM_DONE: Final = object()


class AskRun:
    """一次 `/v1/ask` 的循环状态与事件流。`stream()` 是唯一的入口。"""

    def __init__(
        self,
        *,
        client: ChatClient,
        toolbox: ToolBox,
        question: str,
        version: str,
        max_steps: int,
        max_answer_tokens: int,
        max_context_tokens: int,
        timeout_s: float,
        llm_timeout_s: float,
        conversation_id: str | None = None,
    ) -> None:
        self.client = client
        self.toolbox = toolbox
        self.question = question
        self.version = version
        self.max_steps = max_steps
        self.max_answer_tokens = max_answer_tokens
        self.max_context_tokens = max_context_tokens
        self.llm_timeout_s = llm_timeout_s
        self.started = time.monotonic()
        self.deadline = self.started + timeout_s

        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.steps: list[AskStep] = []
        self.citations: list[Citation] = []
        self.answer = ""
        self.reason: AskReason = "stop"
        # 所属历史会话：HTTP 层在收尾时据此把答案落库，并把它回传给前端（`done` 事件）。
        self.conversation_id = conversation_id
        # 是否已经走到统一收尾（`_finish`）。客户端中途断开时生成器停在 `yield`，此标志为 False，
        # 收尾就只还槽位、不写半截答案。
        self.finished = False
        # 最近一次 `_call` 的结果（`_call` 写入，调用点读）。
        self._completion = ChatCompletion(
            content="", tool_calls=(), usage=TokenUsage(prompt_tokens=0, completion_tokens=0)
        )
        # `self.answer` 里已经作为 `delta` 发出去的长度。
        #
        # 用长度而不是布尔标志：正文是**边流边发**的，但工具轮也可能先吐几句话（「让我查一下」），
        # 那几句已经显示给用户了。只记「发过没发过」会让 `_finish` 要么丢掉已经显示的前缀、
        # 要么把整段再发一遍。按长度记，`_finish` 只补发剩下的部分，界面与落库内容始终一致。
        self._streamed_chars = 0

    @property
    def latency_ms(self) -> int:
        """端到端耗时（含所有工具调用）。"""
        return int((time.monotonic() - self.started) * 1000)

    @property
    def usage(self) -> Usage:
        return Usage(
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            latency_ms=self.latency_ms,
        )

    async def stream(self) -> AsyncIterator[dict[str, Any]]:
        """跑完一次问答，逐个产出事件 payload（`type` 字段区分）。"""
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": self.question},
        ]

        for _ in range(self.max_steps):
            if self._expired():
                async for event in self._degrade("总超时"):
                    yield event

                return

            try:
                async for event in self._call(messages, TOOL_SCHEMAS):
                    yield event
            except (ChatError, TimeoutError, OSError) as error:
                async for event in self._degrade(f"模型调用失败：{error}"):
                    yield event

                return

            completion = self._completion

            if not completion.tool_calls:
                # 正文已在 `_call` 里边收边累积进 `self.answer` 并作为 `delta` 流出，
                # 这里**不能**再赋值：覆盖会丢掉前面已经显示给用户的部分。
                async for event in self._finish("stop"):
                    yield event

                return

            messages.append(_assistant_message(completion))

            # 编号与参数解析都在并发**之前**定好：并发期间 `len(self.steps)` 不再按调用顺序增长，
            # 事后算会串号，压缩时的「丢弃标签」就指错对象。
            plan = _plan_tool_calls(completion.tool_calls, len(self.steps))

            # 先把整批的「开始」发出去（前端据此立刻显示在跑哪几次调用），再并发跑工具。
            for item in plan:
                yield {"type": "step", "tool": item.name, "args": item.args}
                self.steps.append(AskStep(tool=item.name, args=item.args))

            outcomes = await self._execute_batch(plan)

            # 结果按**请求顺序**消费，不按完成先后：按完成先后发出去，前端列表会跳。
            for item, outcome in zip(plan, outcomes, strict=True):
                step_event: dict[str, Any] = {
                    "type": "step",
                    "tool": item.name,
                    "ok": outcome.ok,
                    "hits": outcome.hits,
                }

                if not outcome.ok:
                    # 失败原因要能被看见：只回一个 `ok=false`，界面只能说「失败」，
                    # 排查时要像这次一样把参数捞出来本地复放才知道为什么。
                    step_event["error"] = outcome.text[:STEP_ERROR_CHARS]

                yield step_event
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": item.id,
                        "content": outcome.text,
                        "_tool": True,
                        "_label": item.label,
                    }
                )
                self._collect(outcome.citations)

            # 整批之后再检查一次预算：同一轮里压多次会让界面反复出现「压缩上下文」。
            dropped = self._fit_budget(messages)

            if dropped:
                yield {
                    "type": "step",
                    "tool": CONTEXT_BUDGET_TOOL,
                    "args": {"dropped": dropped},
                }

        async for event in self._forced_finish(messages):
            yield event

    async def _execute_batch(self, plan: list[_PlannedCall]) -> list[ToolOutcome]:
        """并发执行整批工具调用，返回与 `plan` 同序的结果（`gather` 保证按入参顺序）。"""
        return list(await asyncio.gather(*(self._execute_one(item) for item in plan)))

    async def _execute_one(self, item: _PlannedCall) -> ToolOutcome:
        """执行一条；参数不是合法 JSON 的那条不跑工具，直接用失败说明占位。"""
        if item.problem is not None:
            return ToolOutcome(ok=False, text=item.problem)

        return await asyncio.to_thread(self.toolbox.execute, item.name, item.args)

    async def _forced_finish(
        self, messages: list[dict[str, Any]]
    ) -> AsyncIterator[dict[str, Any]]:
        """达到 `max_steps`：不再给模型工具，要求它基于已有信息作答（规格 §2）。"""
        if self._expired():
            async for event in self._degrade("总超时"):
                yield event

            return

        prefix = f"{MAX_STEPS_NOTICE}。\n"
        notice_sent = False

        try:
            async for event in self._call(
                [*messages, {"role": "user", "content": FORCE_ANSWER_PROMPT}], []
            ):
                if not notice_sent and event["type"] == "delta":
                    # 步数上限说明是答案的一部分，必须在正文之前发出去；但模型可能一个字都不给
                    # （那会走降级），所以等第一段正文到了再发。它同样要累积并记账——
                    # 否则界面有这句、落库的答案没有。
                    notice_sent = True
                    self.answer += prefix
                    self._streamed_chars += len(prefix)
                    yield {"type": "delta", "text": prefix}
                yield event
        except (ChatError, TimeoutError, OSError) as error:
            async for event in self._degrade(f"模型调用失败：{error}"):
                yield event

            return

        # 强制收尾这一次就算模型还想调工具也不执行：拿它的文本作答。
        body = self._completion.content.strip()

        if not body:
            # 模型仍然没给文本（例如推理预算全花在思考上）：退化成「只给位置」，不要交一片空白。
            async for event in self._degrade("检索步数用尽且模型未给出文本答案"):
                yield event

            return
        # 正文与 prefix 都已在流出时累积进 `self.answer`，这里不再赋值。
        async for event in self._finish("max_steps"):
            yield event

    async def _degrade(self, detail: str) -> AsyncIterator[dict[str, Any]]:
        """模型不可用时的降级（规格 §5）：HTTP 仍 200，用已召回的检索结果作答。

        `detail` 只进日志：不把模型返回的内部错误原文回给调用方。
        """
        logger.warning("问答降级（%s）：%s", self.version, detail)

        # 第一步就失败：还没召回任何东西，先用用户问题跑一次 search 拿位置。
        if not self.citations:
            args: dict[str, Any] = {"query": self.question, "detail": "locate", "limit": 10}
            yield {"type": "step", "tool": SEARCH_TOOL, "args": args}
            self.steps.append(AskStep(tool=SEARCH_TOOL, args=args))
            outcome = await asyncio.to_thread(self.toolbox.execute, SEARCH_TOOL, args)
            yield {
                "type": "step",
                "tool": SEARCH_TOOL,
                "ok": outcome.ok,
                "hits": outcome.hits,
            }
            self._collect(outcome.citations)

        # 追加而不是覆盖：模型可能已经流出一部分正文给用户看到了，覆盖会让界面与落库内容不一致。
        # 用 `+` 拼接而不是 f-string 带 `\n`：多行 f-string 里写换行最容易出错。
        self.answer = self.answer + "\n\n" + DEGRADED_NOTICE if self.answer else DEGRADED_NOTICE
        async for event in self._finish("degraded"):
            yield event

    async def _finish(self, reason: AskReason) -> AsyncIterator[dict[str, Any]]:
        """统一收尾：`delta`* → `citation`* → `done`。"""
        self.finished = True
        self.reason = reason

        # 模型正文已经边收边作为 `delta` 发过了；这里只补发**还没发出去的尾巴**
        # （降级提示，或流式中途失败后缺的那一段）。
        remainder = self.answer[self._streamed_chars :]

        for chunk in _delta_chunks(remainder):
            yield {"type": "delta", "text": chunk}

        self._streamed_chars = len(self.answer)

        for citation in self.citations:
            yield {
                "type": "citation",
                "path": citation.path,
                "lines": citation.lines,
                "symbol": citation.symbol,
            }

        done: dict[str, Any] = {
            "type": "done",
            "usage": self.usage.model_dump(),
            "reason": reason,
        }

        # `conversation_id` 只在 HTTP 层用（`/v1/ask` 会自动建会话）；评测等直接构造 `AskRun`
        # 的调用方没有会话，就不带这个字段。
        if self.conversation_id is not None:
            done["conversation_id"] = self.conversation_id

        yield done

    async def _call(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> AsyncIterator[dict[str, Any]]:
        """调一次模型：文本增量边收边 yield `delta`，结束后结果放进 `self._completion`。

        与非流式路径同一套错误语义（网络 / 非 2xx / 响应结构不符都抛 `ChatError`）。工具调用的
        分片由客户端在流结束时组装（`self._completion.tool_calls`）。
        """
        remaining = max(self.deadline - time.monotonic(), 1.0)
        content_parts: list[str] = []
        final = ChatChunk()

        async for chunk in self._stream_chunks(
            _payload_messages(messages), tools, min(self.llm_timeout_s, remaining)
        ):
            if chunk.content:
                content_parts.append(chunk.content)
                self.answer += chunk.content
                self._streamed_chars += len(chunk.content)
                yield {"type": "delta", "text": chunk.content}

            if chunk.tool_calls or chunk.usage is not None:
                final = chunk

        self._completion = ChatCompletion(
            content="".join(content_parts),
            tool_calls=final.tool_calls,
            usage=final.usage or TokenUsage(prompt_tokens=0, completion_tokens=0),
        )
        self.prompt_tokens += self._completion.usage.prompt_tokens
        self.completion_tokens += self._completion.usage.completion_tokens

    async def _stream_chunks(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], timeout: float
    ) -> AsyncIterator[ChatChunk]:
        """把同步的流式生成器桥到事件循环：工作线程逐块读，主协程逐块 yield。

        底层 `urlopen` 是阻塞的，直接在事件循环里迭代会把整个进程卡住（其它请求与工具调用都
        停住），所以丢进工作线程，用 `asyncio.Queue` 把块送回主协程。
        """
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[object] = asyncio.Queue()

        def pump() -> None:
            try:
                for chunk in self.client.stream(
                    messages=messages,
                    tools=tools,
                    max_tokens=self.max_answer_tokens,
                    timeout=timeout,
                ):
                    loop.call_soon_threadsafe(queue.put_nowait, chunk)
            except BaseException as error:  # 原样带回主协程重抛
                loop.call_soon_threadsafe(queue.put_nowait, error)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, _STREAM_DONE)

        task = asyncio.create_task(asyncio.to_thread(pump))

        try:
            while True:
                item = await queue.get()

                if item is _STREAM_DONE:
                    break

                if isinstance(item, BaseException):
                    raise item

                yield cast("ChatChunk", item)
        finally:
            # 正常结束时线程已经跑完（`_STREAM_DONE` 是它最后推的）；异常 / 取消时不让等待拖住收尾。
            task.cancel()

    def _expired(self) -> bool:
        return time.monotonic() > self.deadline

    def _collect(self, citations: tuple[Citation, ...]) -> None:
        """收集引用：按 (路径, 行区间, 符号) 去重，最多留 `MAX_CITATIONS` 条。"""
        seen = {(item.path, tuple(item.lines), item.symbol) for item in self.citations}

        for item in citations:
            key = (item.path, tuple(item.lines), item.symbol)

            if key in seen or len(self.citations) >= MAX_CITATIONS:
                continue

            seen.add(key)
            self.citations.append(item)

    def _fit_budget(self, messages: list[dict[str, Any]]) -> list[str]:
        """把工具结果的总量压到**目标水位**，返回被丢弃的标签（规格 §3）。

        目标水位是 `max_context_tokens * COMPACT_TARGET_RATIO` 而不是预算本身：
        只压到刚好达标的话，压完整段上下文就在上限边缘，下一次工具调用立刻又超，
        表现为「压缩 → 调一两次 → 又压缩」的抖动，且每轮都要重压一次、白耗时间。
        """
        target = int(self.max_context_tokens * COMPACT_TARGET_RATIO)
        dropped: list[str] = []

        while _tool_tokens(messages) > target and _exchange_count(messages) > 1:
            start = _first_exchange(messages)

            if start is None:
                break

            end = start + 1

            while end < len(messages) and messages[end].get("_tool"):
                end += 1

            dropped.extend(str(item["_label"]) for item in messages[start:end] if "_label" in item)
            del messages[start:end]

        # 只剩最后一个工具结果仍超水位：按剩余空间把它截断（保留系统提示与用户问题）。
        if _tool_tokens(messages) > target:
            self._truncate_newest(messages, target)

        return dropped

    def _truncate_newest(self, messages: list[dict[str, Any]], target: int) -> None:
        index = next(
            (i for i in range(len(messages) - 1, -1, -1) if messages[i].get("_tool")), None
        )

        if index is None:
            return

        others = sum(
            count_tokens(str(item.get("content") or ""))
            for position, item in enumerate(messages)
            if item.get("_tool") and position != index
        )
        budget = max(target - others, MIN_RESULT_TOKENS)
        content = str(messages[index].get("content") or "")
        messages[index]["content"] = _truncate_to_tokens(content, budget)


def _payload_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """去掉内部标记（`_tool` / `_label`）后的消息，才是发给模型的。"""
    return [
        {key: value for key, value in item.items() if not key.startswith("_")}
        for item in messages
    ]


@dataclass(frozen=True)
class _PlannedCall:
    """一批里的一个工具调用：参数已解析、`_label` 已编号，等着并发执行。"""

    id: str
    name: str
    args: dict[str, Any]
    problem: str | None
    label: str


def _plan_tool_calls(calls: tuple[ToolCall, ...], taken_steps: int) -> list[_PlannedCall]:
    """把一轮的 `tool_calls` 摊平成执行计划；`taken_steps` 是这一批之前已有的步数。"""
    plan: list[_PlannedCall] = []

    for position, call in enumerate(calls):
        args, problem = _parse_args(call.arguments)
        plan.append(
            _PlannedCall(
                id=call.id,
                name=call.name,
                args=args,
                problem=problem,
                # 与串行时代一致：`call.name#<这一批里的第几个>`，序号跨批累加。
                label=f"{call.name}#{taken_steps + position + 1}",
            )
        )

    return plan


def _assistant_message(completion: ChatCompletion) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": completion.content or None,
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments},
            }
            for call in completion.tool_calls
        ],
        "_exchange": True,
    }


def _parse_args(arguments: str) -> tuple[dict[str, Any], str | None]:
    """解析工具调用参数；不是合法 JSON 对象时返回空参数 + 给模型的失败说明。"""
    try:
        loaded = json.loads(arguments or "{}")
    except json.JSONDecodeError as error:
        return {}, f"工具参数不是合法 JSON：{error}"

    if not isinstance(loaded, dict):
        return {}, "工具参数不是 JSON 对象"

    return loaded, None


def _delta_chunks(text: str) -> list[str]:
    """答案拆成逐行增量：前端按事件追加渲染，不必等整段答案。"""
    return text.splitlines(keepends=True)


def _tool_tokens(messages: list[dict[str, Any]]) -> int:
    return sum(
        count_tokens(str(item.get("content") or "")) for item in messages if item.get("_tool")
    )


def _exchange_count(messages: list[dict[str, Any]]) -> int:
    return sum(1 for item in messages if item.get("_exchange"))


def _first_exchange(messages: list[dict[str, Any]]) -> int | None:
    return next((i for i, item in enumerate(messages) if item.get("_exchange")), None)


TRUNCATION_NOTE: Final = "\n…（超出上下文预算，已截断）"


def _truncate_to_tokens(text: str, limit: int) -> str:
    """把文本截到**含截断提示在内**不超过 `limit` 个 token（按字符二分，与 `count_tokens` 同口径）。

    提示本身也占 token：先按 `limit` 截再拼提示，最终会比 `limit` 多出提示那几个 token，
    压缩完仍然贴着水位——`_fit_budget` 的「压到目标水位」就永远差一点（实测差 15：
    `word ` × 700 截到 700，实际得到 716）。所以先把提示的长度留出来再二分。
    """
    if limit <= 0:
        return ""

    if count_tokens(text) <= limit:
        return text

    budget = limit - count_tokens(TRUNCATION_NOTE)

    if budget <= 0:
        return TRUNCATION_NOTE.strip()

    low, high = 0, len(text)

    while low < high:
        middle = (low + high + 1) // 2

        if count_tokens(text[:middle]) <= budget:
            low = middle
        else:
            high = middle - 1

    return text[:low] + TRUNCATION_NOTE
