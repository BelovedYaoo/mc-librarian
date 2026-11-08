"""独立索引 worker：只消费 `jobs` 队列，不提供 HTTP。

为什么要拆出来：

- 索引任务重（反编译峰值约 1.2 GB、单版本 15–30 分钟），和 API 同进程时，重启/发布
  会打断任务，内存也要和 API 抢；
- 反编译只需要一个进程，不需要复制 API 的部署形态；
- 拆分后 API 用 `MC_LIBRARIAN_INDEX_ENABLED=false`（不跑任务），worker 用 `true`，
  任务创建由 `MC_LIBRARIAN_INDEX_JOBS_ENABLED` 单独控制（API 仍能入队）。

进程模型与 API 内的 runner 完全一致（`JobRunner`，SQLite 队列，单 worker 认领）。
"""

from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import suppress
from typing import Final

from mc_librarian.config import get_settings
from mc_librarian.db import Database
from mc_librarian.jobs.runner import JobRunner

logger = logging.getLogger("mc_librarian.jobs.worker")

# 收到 SIGTERM 后等当前任务收尾的上限（与 API 的 runner.stop 一致）。
STOP_TIMEOUT_S: Final = 10.0


async def run_worker() -> None:
    """常驻消费任务队列，直到 SIGINT / SIGTERM。"""
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    database = Database.for_data_dir(settings.data_dir)
    version = database.initialize()
    logger.info("worker 启动：数据根 %s（schema v%d）", settings.data_dir, version)

    if not settings.index_enabled:
        logger.warning(
            "MC_LIBRARIAN_INDEX_ENABLED=false：本 worker 不会认领任何任务"
            "（把它设成 true 才会真正跑索引）"
        )

    runner = JobRunner(settings, database)
    await runner.start()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        # Windows 的事件循环不支持 add_signal_handler（本地调试用 Ctrl+C 结束即可）。
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    try:
        await stop.wait()
    finally:
        await runner.stop()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(run_worker())
