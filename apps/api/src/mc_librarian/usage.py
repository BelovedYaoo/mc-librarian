"""`usage` 表读写（issue #8 建表并写入；`#12` 做聚合与告警）。

每次 `/v1/ask` 写**一行**（含降级的情况），`latency_ms` 是端到端耗时（含所有工具调用）。
`cost_usd` 由单价算（`MC_LIBRARIAN_LLM_INPUT_PRICE_PER_M` / `..._OUTPUT_PRICE_PER_M`，每百万
token 的美元价）；**未配单价时写 0**——只记 token、不计成本，不编一个假单价。

`#12` 只读：`report` 按 `day`/`user`/`version`/`model` 聚合出用量页要的全部数字，
`day_totals` 给告警用。两个查询的**分天口径一致**：本地日期（UTC+8），
与 `alert_events.day` 同一个口径。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Final, Literal

from mc_librarian.config import LlmPricing
from mc_librarian.db import utc_now
from mc_librarian.schemas import Usage, UsageBucket, UsageReport

# `usage.kind` 的取值；`#12` 按它聚合（目前只有问答一种）。
KIND_ASK: Final = "ask"

# 聚合维度（契约 `getUsage` 的 `group_by`）。
GroupBy = Literal["day", "user", "version", "model"]

# 分天口径：**本地日期（UTC+8）**，与 `alert_events.day` 一致（规格 §4）。
# `usage.created_at` 存的是 UTC（`...Z`），所以按日期分组/过滤都要先换算。
LOCAL_UTC_OFFSET_HOURS: Final = 8
LOCAL_TIMEZONE: Final = timezone(timedelta(hours=LOCAL_UTC_OFFSET_HOURS))
_LOCAL_DAY_SQL: Final = f"date(usage.created_at, '+{LOCAL_UTC_OFFSET_HOURS} hours')"

# 用量页默认区间：最近 7 天（含今天），与前端默认一致。
DEFAULT_RANGE_DAYS: Final = 7

# 分组键缺值时的占位（`user_id`/`version`/`model` 都可为空）：显式标出，不拿空串冒充一个分组。
UNKNOWN_GROUP_KEY: Final = "（未知）"

_INSERT = (
    "INSERT INTO usage"
    " (user_id, kind, model, prompt_tokens, completion_tokens, cost_usd, latency_ms, version,"
    "  created_at)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

# 分桶键的 SQL 表达式。`user` 优先显示用户名（同 `audit` 的口径），取不到才退回 id。
# 非 `day` 的那几项带一个 `?`（缺值占位），拼参数时放在区间参数**之前**。
_KEY_SQL: Final[dict[str, str]] = {
    "day": _LOCAL_DAY_SQL,
    "user": "COALESCE(u.username, usage.user_id, ?)",
    "version": "COALESCE(usage.version, ?)",
    "model": "COALESCE(usage.model, ?)",
}

# `users` 是 LEFT JOIN：`usage.user_id` 可空（老行/已删用户），缺了不能把该行从统计里丢掉。
_FROM = "FROM usage LEFT JOIN users u ON u.id = usage.user_id"


def local_date(when: datetime | None = None) -> date:
    """本地（UTC+8）日期；不传则取当前时间。"""
    return (when or datetime.now(UTC)).astimezone(LOCAL_TIMEZONE).date()


def local_day(when: datetime | None = None) -> str:
    """本地日期文本（`YYYY-MM-DD`），与 `_LOCAL_DAY_SQL` 和 `alert_events.day` 同口径。"""
    return local_date(when).isoformat()


def resolve_range(start: date | None, end: date | None) -> tuple[date, date]:
    """补全查询区间：缺的端点用「今天（本地）」与「往前 7 天」补齐。"""
    resolved_end = end or local_date()
    resolved_start = start or resolved_end - timedelta(days=DEFAULT_RANGE_DAYS - 1)

    return resolved_start, resolved_end


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


def report(
    conn: sqlite3.Connection,
    *,
    start: date,
    end: date,
    user_id: str | None,
    group_by: GroupBy,
    cost_configured: bool,
) -> UsageReport:
    """`GET /v1/admin/usage` 的聚合（规格 §2）。

    `totals` 与 `buckets` 用同一个 FROM/WHERE，因此页面上的合计与明细必然同源。
    区间内没有数据时 `totals` 全 0、`buckets` 为空数组（验收第 5 条：不是报错）。
    `totals.latency_ms` 是区间内所有请求的耗时**之和**（与其余字段同为合计口径）。
    `cost_configured` 由调用方从单价配置派生，页面据此标注成本口径。
    """
    where_params: list[object] = [start.isoformat(), end.isoformat()]
    where = f"WHERE {_LOCAL_DAY_SQL} BETWEEN ? AND ?"

    if user_id:
        # `user_id` 按 `usage.user_id`（用户 id）过滤；分桶键则优先显示用户名（见 `_KEY_SQL`）。
        where += " AND usage.user_id = ?"
        where_params.append(user_id)

    totals_row = conn.execute(
        "SELECT COUNT(*) AS requests,"
        " COALESCE(SUM(usage.prompt_tokens), 0) AS prompt_tokens,"
        " COALESCE(SUM(usage.completion_tokens), 0) AS completion_tokens,"
        " COALESCE(SUM(usage.cost_usd), 0) AS cost_usd,"
        " COALESCE(SUM(usage.latency_ms), 0) AS latency_ms"
        f" {_FROM} {where}",
        where_params,
    ).fetchone()

    # 键表达式里的 `?`（缺值占位）在 SQL 文本里排在最前，参数顺序必须跟着走。
    key_params: list[object] = [] if group_by == "day" else [UNKNOWN_GROUP_KEY]
    bucket_rows = conn.execute(
        f"SELECT {_KEY_SQL[group_by]} AS key,"
        " COUNT(*) AS requests,"
        " COALESCE(SUM(usage.prompt_tokens), 0) AS prompt_tokens,"
        " COALESCE(SUM(usage.completion_tokens), 0) AS completion_tokens,"
        " COALESCE(SUM(usage.cost_usd), 0) AS cost_usd"
        f" {_FROM} {where}"
        " GROUP BY key ORDER BY key",
        [*key_params, *where_params],
    ).fetchall()

    return UsageReport(
        from_=start,
        to=end,
        requests=int(totals_row["requests"]),
        cost_configured=cost_configured,
        totals=Usage(
            prompt_tokens=int(totals_row["prompt_tokens"]),
            completion_tokens=int(totals_row["completion_tokens"]),
            cost_usd=float(totals_row["cost_usd"]),
            latency_ms=int(totals_row["latency_ms"]),
        ),
        buckets=[
            UsageBucket(
                key=str(row["key"]),
                requests=int(row["requests"]),
                prompt_tokens=int(row["prompt_tokens"]),
                completion_tokens=int(row["completion_tokens"]),
                cost_usd=float(row["cost_usd"]),
            )
            for row in bucket_rows
        ],
    )


@dataclass(frozen=True)
class DayTotals:
    """一天（本地日期）的用量合计，供告警判阈值。"""

    tokens: int
    cost_usd: float


def day_totals(conn: sqlite3.Connection, *, day: str) -> DayTotals:
    """某一天的 token 总数与成本合计；没有数据时全 0。"""
    row = conn.execute(
        "SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens,"
        " COALESCE(SUM(cost_usd), 0) AS cost_usd"
        f" FROM usage WHERE {_LOCAL_DAY_SQL} = ?",
        (day,),
    ).fetchone()

    return DayTotals(tokens=int(row["tokens"]), cost_usd=float(row["cost_usd"]))

