"""索引任务框架：`jobs` / `job_logs` / `versions` 三张表的读写与串行执行。

- `store` —— 三张表的读写函数（不自己开事务，调用方用 `db.transaction()` 包住）；
- `runner` —— 随 FastAPI lifespan 启动的 `JobRunner`，并发 1 地消费 `jobs` 队列。
"""

from __future__ import annotations

from .runner import FULL_STAGES, SOURCE_STAGES, JobContext, JobRunner, run_stage

__all__ = ["FULL_STAGES", "SOURCE_STAGES", "JobContext", "JobRunner", "run_stage"]
