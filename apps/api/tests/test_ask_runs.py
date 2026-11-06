"""进行中的问答（`answer.runs`）：生成与观看解耦。

这一组守住的核心不变量只有一条：**观看方退出不影响生成**。它对应一个真实的架构缺陷——
之前生成由 HTTP 响应驱动，用户切到别的页面就把这次问答掐死了。
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

from mc_librarian.answer.agent import AskRun
from mc_librarian.answer.runs import AskRuns


class _FakeRun:
    """只提供 `RunningAsk` 用到的那部分接口（`stream()` / `version` / `answer`）。"""

    def __init__(self, total: int, *, delay: float = 0.0) -> None:
        self.version = "26.3"
        self.answer = ""
        self.total = total
        self.delay = delay
        self.yielded = 0

    async def stream(self) -> Any:
        for index in range(self.total):
            if self.delay:
                await asyncio.sleep(self.delay)

            self.yielded += 1
            self.answer += f"块{index}"
            yield {"type": "delta", "text": f"块{index}"}


async def _started(total: int, *, delay: float = 0.0) -> tuple[AskRuns, _FakeRun, list[str]]:
    """在事件循环里起一个运行（`asyncio.create_task` 需要 running loop）。"""
    runs = AskRuns()
    finished: list[str] = []
    run = _FakeRun(total, delay=delay)
    runs.start(
        run=cast("AskRun", run),
        conversation_id="c1",
        on_finish=lambda finished_run: finished.append(finished_run.answer),
    )

    return runs, run, finished


def test_consumer_quitting_early_does_not_stop_the_run() -> None:
    """订阅者读两个事件就走：生成继续跑完，收尾照常执行（落库/还槽位都挂在收尾上）。"""

    async def main() -> tuple[int, list[str]]:
        runs, run, finished = await _started(6, delay=0)

        stream = runs.active("c1")
        assert stream is not None

        seen = 0

        async for _event in stream.iter_events():
            seen += 1

            if seen == 2:
                break  # 客户端断开

        assert stream.task is not None
        await asyncio.wait_for(stream.task, timeout=5)

        return run.yielded, finished

    yielded, finished = asyncio.run(main())

    assert yielded == 6, "订阅者退出后生成必须继续到结束"
    assert finished == ["块0块1块2块3块4块5"], "收尾必须基于完整答案"


def test_late_subscriber_gets_the_whole_replay() -> None:
    """生成结束后才订阅：能拿到全部事件（回放），不会挂住等新事件。"""

    async def main() -> list[str]:
        runs, _run, _finished = await _started(3)
        running = runs.active("c1")
        assert running is not None
        assert running.task is not None

        await asyncio.wait_for(running.task, timeout=5)  # 先跑完

        return [event["text"] for event in [item async for item in running.iter_events()]]

    assert asyncio.run(main()) == ["块0", "块1", "块2"]


def test_shutdown_cancels_runs_and_still_finalizes() -> None:
    """服务停止：取消在跑的生成，但收尾照样执行（已产出部分落库，槽位归还）。"""

    async def main() -> list[str]:
        runs, _run, finished = await _started(1000, delay=0.01)
        await asyncio.sleep(0.02)  # 让任务真正跑起来再取消

        await runs.shutdown()

        return finished

    finished = asyncio.run(main())

    assert len(finished) == 1, "被取消的运行也必须走收尾（部分答案落库、槽位归还）"


def test_attaching_to_a_missing_conversation_returns_none() -> None:
    assert AskRuns().active("不存在") is None
