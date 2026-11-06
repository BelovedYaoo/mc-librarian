"""进行中的问答：让「生成」与「观看」解耦。

之前生成是由 HTTP 响应驱动的（`StreamingResponse` 用 `for` 拉生成器）：客户端一断开，
Starlette 取消流任务，`AskRun.stream()` 停在 `yield`，循环再也不会往下走——用户切到别的
页面就等于把这次问答掐死。这在架构上是错的：模型调用、工具检索、落库都属于**后端任务**，
前端只是看，看不看都不该影响结果。

这里的模型：

- `RunningAsk` 拥有一次生成。它跑在自己的 `asyncio.Task` 里，与任何 HTTP 连接无关。
- 每个事件进一份**缓冲**，同时推给当前所有订阅者。订阅者（SSE 响应）晚到也能从头上回放。
- 客户端断开 = 取消订阅；任务继续跑到结束，结束时统一落库、记用量、还并发槽位。
- 想接着看：按会话 id 找到还在跑的运行，`attach` 上去（回放 + 实时）。

事件总量有上限（`MAX_BUFFERED_EVENTS`）：一次深度问答约两千个 delta，几十 KB，真到上限说明
某次运行异常长——那时回放会截断，但**落库的内容不受影响**（落库读的是 `AskRun` 自己）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, Final

from mc_librarian.answer.agent import AskRun
from mc_librarian.problems import Problem

logger = logging.getLogger(__name__)

# 回放缓冲的事件上限。到顶后只停回放、不影响生成与落库。
MAX_BUFFERED_EVENTS: Final = 20_000


@dataclass
class _Subscriber:
    """一个正在观看的客户端（一条 SSE 连接）。"""

    queue: asyncio.Queue[dict[str, Any] | None] = field(default_factory=asyncio.Queue)


class RunningAsk:
    """一次进行中的问答：事件缓冲 + 订阅者集合 + 结束信号。"""

    def __init__(
        self,
        *,
        run: AskRun,
        conversation_id: str,
        on_finish: Callable[[AskRun], None],
    ) -> None:
        self.run = run
        self.conversation_id = conversation_id
        self._on_finish = on_finish
        self._events: list[dict[str, Any]] = []
        self._subscribers: list[_Subscriber] = []
        # 缓冲追加与订阅注册共用一把锁：否则「取快照」与「注册订阅」之间到的事件会丢。
        self._lock = asyncio.Lock()
        self._done = False
        self.task: asyncio.Task[None] | None = None

    @property
    def done(self) -> bool:
        return self._done

    async def _emit(self, event: dict[str, Any]) -> None:
        async with self._lock:
            if len(self._events) < MAX_BUFFERED_EVENTS:
                self._events.append(event)

            for subscriber in self._subscribers:
                subscriber.queue.put_nowait(event)

    async def drive(self) -> None:
        """后台跑完整个循环；任何退出路径都要走收尾（落库 / 还槽位）。"""
        try:
            async for event in self.run.stream():
                await self._emit(event)
        except asyncio.CancelledError:
            # 服务停止时被取消：已产出的部分照样落库（`_finalize` 会按未完成标注）。
            raise
        except Exception as error:  # 生成异常也要让观看方收到明确的收尾
            logger.exception("问答生成失败（%s）", self.run.version)
            await self._emit(
                {
                    "type": "error",
                    "problem": Problem(
                        status=500, title="问答失败", detail=str(error)
                    ).model_dump(exclude_none=True),
                }
            )
        finally:
            await self._close()

    async def _close(self) -> None:
        async with self._lock:
            self._done = True
            subscribers, self._subscribers = self._subscribers, []

            for subscriber in subscribers:
                subscriber.queue.put_nowait(None)

        try:
            self._on_finish(self.run)
        except Exception:  # 落库失败不能让任务变成「未处理异常」
            logger.exception("问答收尾失败（%s）", self.conversation_id)

    async def _subscribe(self) -> tuple[list[dict[str, Any]], _Subscriber | None]:
        async with self._lock:
            replay = list(self._events)

            if self._done:
                return replay, None

            subscriber = _Subscriber()
            self._subscribers.append(subscriber)

            return replay, subscriber

    async def _unsubscribe(self, subscriber: _Subscriber) -> None:
        async with self._lock:
            if subscriber in self._subscribers:
                self._subscribers.remove(subscriber)

    async def iter_events(self) -> AsyncIterator[dict[str, Any]]:
        """先回放已经发生的事件，再实时跟随；运行结束后自然收尾。

        取消这个生成器（客户端断开）只会退订，**不会**影响 `drive()`。
        """
        replay, subscriber = await self._subscribe()

        try:
            for event in replay:
                yield event

            if subscriber is None:
                return

            while True:
                item = await subscriber.queue.get()

                if item is None:
                    return

                yield item
        finally:
            if subscriber is not None:
                await self._unsubscribe(subscriber)

    async def wait(self) -> None:
        """等到生成结束（一次性响应 `Accept: application/json` 用它）。"""
        if self.task is not None:
            await asyncio.shield(self.task)


class AskRuns:
    """当前实例里正在跑的问答（按会话 id 索引，供「接着看」用）。"""

    def __init__(self) -> None:
        self._by_conversation: dict[str, RunningAsk] = {}

    def start(
        self,
        *,
        run: AskRun,
        conversation_id: str,
        on_finish: Callable[[AskRun], None],
    ) -> RunningAsk:
        """起一个后台任务。调用方拿到的对象可以订阅多次（包括断开后重新订阅）。"""
        running = RunningAsk(run=run, conversation_id=conversation_id, on_finish=on_finish)
        self._by_conversation[conversation_id] = running
        running.task = asyncio.create_task(running.drive())
        def drop(_task: asyncio.Task[None]) -> None:
            self._by_conversation.pop(conversation_id, None)

        running.task.add_done_callback(drop)

        return running

    def in_flight(self) -> list[RunningAsk]:
        """当前还在跑的运行（快照）。测试与运维观察用；不要拿它做业务判断。"""
        return [running for running in self._by_conversation.values() if not running.done]

    def active(self, conversation_id: str) -> RunningAsk | None:
        """该会话还在跑的运行；已结束的返回 None（那时应该读历史消息）。"""
        running = self._by_conversation.get(conversation_id)

        return None if running is None or running.done else running

    async def shutdown(self) -> None:
        """服务停止：取消所有运行。取消会走 `drive` 的 `finally`，已产出的部分照样落库。"""
        tasks = [
            running.task
            for running in self._by_conversation.values()
            if running.task is not None and not running.task.done()
        ]

        for task in tasks:
            task.cancel()

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        self._by_conversation.clear()


__all__ = ["MAX_BUFFERED_EVENTS", "AskRuns", "RunningAsk"]
