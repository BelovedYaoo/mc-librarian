"""`usage` 表读写（issue #8 建表并写入；`#12` 只做聚合与告警）。

每次 `/v1/ask` 写**一行**（含降级的情况），`latency_ms` 是端到端耗时（含所有工具调用）。
`cost_usd` 由单价算（`MC_LIBRARIAN_LLM_INPUT_PRICE_PER_M` / `..._OUTPUT_PRICE_PER_M`，每百万
token 的美元价）；**未配单价时写 0**——只记 token、不计成本，不编一个假单价。
"""

from __future__ import annotations

import sqlite3
from typing import Final

from mc_librarian.config import LlmPricing
from mc_librarian.db import utc_now

# `usage.kind` 的取值；`#12` 按它聚合（目前只有问答一种）。
KIND_ASK: Final = "ask"

_INSERT = (
    "INSERT INTO usage"
    " (user_id, kind, model, prompt_tokens, completion_tokens, cost_usd, latency_ms, version,"
    "  created_at)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def estimate_cost_usd(prompt_tokens: int, completion_tokens: int, pricing: LlmPricing) -> float:
    """按 token 数与单价算成本（美元）；单价未配置时对应项是 0。"""
    cost = (
        prompt_tokens / 1_000_000 * pricing.input_per_m
        + completion_tokens / 1_000_000 * pricing.output_per_m
    )

    return round(cost, 6)


def record(
    conn: sqlite3.Connection,
    *,
    user_id: str | None,
    kind: str,
    model: str | None,
    prompt_tokens: int,
    completion_tokens: int,
    cost_usd: float,
    latency_ms: int,
    version: str | None,
) -> None:
    """写一行用量。不自己开事务：单条 INSERT 在自动提交下就是一行的原子写。"""
    conn.execute(
        _INSERT,
        (
            user_id,
            kind,
            model,
            prompt_tokens,
            completion_tokens,
            cost_usd,
            latency_ms,
            version,
            utc_now(),
        ),
    )
