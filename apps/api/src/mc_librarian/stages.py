"""索引阶段序列的**唯一来源**。

同一份顺序原本写了两遍：`jobs/runner.py` 的 `STAGES`、`db.py` 里 `jobs.stage` 的 CHECK 约束
（v4 一份、v5 重建表又是另一份）。改一处忘另一处就会出现「数据库拒收某个合法阶段」，
而这种错只在真实跑到那一阶段时才暴露。

接口层的 `JobStage` 也由这里推导；前端 `Record<JobStage, string>` 一旦漏标签会直接编译失败。
"""

from __future__ import annotations

from typing import Final

# 五段流水线：前两段只在「在节点上下载并反编译」的任务里出现（issue #21）。
STAGES: Final[tuple[str, ...]] = ("download", "decompile", "parse", "summarize", "finalize")

# 从源码目录起跑的任务只走这三段（不需要下载 jar / 反编译）。
SOURCE_STAGES: Final[tuple[str, ...]] = STAGES[2:]

# 收尾阶段，取消时用来描述停在哪。
FINAL_STAGE: Final = STAGES[-1]


def sql_stage_check() -> str:
    """`jobs.stage` 的 CHECK 表达式；迁移与新库共用同一份。"""
    values = ", ".join(f"'{stage}'" for stage in STAGES)

    return f"stage IS NULL OR stage IN ({values})"


__all__ = ["FINAL_STAGE", "SOURCE_STAGES", "STAGES", "sql_stage_check"]
