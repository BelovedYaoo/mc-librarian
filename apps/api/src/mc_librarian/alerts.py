"""用量阈值告警（issue #12）：当日 token 数超阈值就推一条 Webhook。

三条硬约束（规格补充 §3–§7）：

1. **阈值读取收敛在 `config.resolve_alerts_config`**（`#11` 之后带数据库覆盖：设置页写进
   `settings` 表的值优先于环境配置），本模块只调它，不自己拼阈值；
2. **同日同指标只告警一次**：`alert_events` 的 `UNIQUE(day, metric)` 配
   `INSERT ... ON CONFLICT DO NOTHING` + `rowcount` 判定，不先查后写；
3. **不阻断请求**：`schedule` 把检查丢到后台线程，调用方（`/v1/ask`、`GET /v1/admin/usage`）
   立刻返回；Webhook 失败只写 warn，**不重试**（避免告警风暴），超阈值也不做任何限制（D23）。

只审计 token，不算金额（`usage.cost_usd` 已在迁移 v11 删除）。
`day` 是**本地日期（UTC+8）**，与用量页的分天口径一致（`usage.local_day`）。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from typing import Final
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from mc_librarian import usage
from mc_librarian.config import Settings, resolve_alerts_config
from mc_librarian.db import Database, utc_now

logger = logging.getLogger("mc_librarian.alerts")

METRIC_DAILY_TOKENS: Final = "daily_tokens"

# Webhook 超时 5 秒（规格 §7）；失败不重试。
WEBHOOK_TIMEOUT_S: Final = 5.0


class AlertError(RuntimeError):
    """Webhook 投递失败；由 `fire` 记 warn，不冒泡到请求路径。"""


def schedule(database: Database, settings: Settings) -> None:
    """后台跑一次告警检查：调用方不等它（验收第 4 条：超阈值不阻断请求）。

    用后台线程而不是 `asyncio.create_task`：调用点既有 async 路由（`/v1/ask`），
    也有跑在线程池里的同步路由（`GET /v1/admin/usage`，那里没有事件循环）。检查里有
    阻塞 IO（sqlite + `urlopen`），放事件循环上会卡住整个进程。
    """
    threading.Thread(
        target=_run,
        args=(database, settings),
        name="mc-librarian-alert",
        daemon=True,
    ).start()


def _run(database: Database, settings: Settings) -> None:
    """后台线程的入口：告警是旁路，任何失败都只 warn，不影响已经发出去的响应。"""
    try:
        check(database, settings)
    except Exception:
        logger.warning("用量告警检查失败", exc_info=True)


def check(database: Database, settings: Settings) -> None:
    """检查当天的 token 数是否超过阈值；超过且当天该指标还没告警过就推 Webhook。"""
    day = usage.local_day()

    with database.connect() as conn:
        # 带连接解析：设置页写进 `settings` 表的阈值覆盖优先于环境配置（#11）。
        config = resolve_alerts_config(settings, conn)

        if config.webhook_url is None:
            # 没有投递目标：连检查都省了（阈值配着但没有 Webhook，等于没配）。
            logger.debug("未配置告警 Webhook，跳过用量告警检查")

            return

        totals = usage.day_totals(conn, day=day)
        value = float(totals.tokens)
        threshold = float(config.daily_token_threshold)

        # 阈值 ≤ 0 = 该指标不告警（规格 §3）；严格大于阈值才算超（「超过阈值」）。
        if threshold <= 0 or value <= threshold:
            return

        fire(
            conn,
            day=day,
            metric=METRIC_DAILY_TOKENS,
            value=value,
            threshold=threshold,
            webhook_url=config.webhook_url,
        )


def fire(
    conn: sqlite3.Connection,
    *,
    day: str,
    metric: str,
    value: float,
    threshold: float,
    webhook_url: str,
) -> bool:
    """记一次告警并（仅首次）推 Webhook；返回是否首次触发。

    去重靠 `alert_events` 的唯一约束：插入被冲突吃掉时 `rowcount` 为 0，说明今天这个指标
    已经告警过，直接跳过（不再发第二条 Webhook）。
    """
    cursor = conn.execute(
        "INSERT INTO alert_events (day, metric, value, threshold, fired_at) VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT (day, metric) DO NOTHING",
        (day, metric, float(value), float(threshold), utc_now()),
    )

    if cursor.rowcount == 0:
        logger.debug("今日已告警过（%s / %s），不再重复发送", day, metric)

        return False

    try:
        post_webhook(
            webhook_url,
            payload(day=day, metric=metric, value=value, threshold=threshold),
            WEBHOOK_TIMEOUT_S,
        )
    except AlertError as error:  # 告警发不出去不该把请求或任务带崩（验收第 4 条）
        logger.warning("推送用量告警失败（%s / %s）：%s", day, metric, error)
    else:
        logger.info("已推送用量告警（%s / %s = %s > %s）", day, metric, value, threshold)

    return True


def payload(*, day: str, metric: str, value: float, threshold: float) -> dict[str, object]:
    """Webhook 载荷（规格 §7）：token 是整数。"""
    amount: int | float = round(value)
    limit: int | float = round(threshold)
    message = f"今日 token 用量已达 {amount:,}，超过阈值 {limit:,}"

    return {
        "day": day,
        "metric": metric,
        "value": amount,
        "threshold": limit,
        "message": message,
    }


def post_webhook(url: str, payload: dict[str, object], timeout: float) -> None:
    """POST 一次 JSON（规格 §7）。失败抛 `AlertError`，由 `fire` 翻成 warn——**不重试**。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        # URL 来自部署配置（环境变量），不是用户输入拼进来的。
        with urlopen(request, timeout=timeout) as response:
            response.read()
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:500]
        raise AlertError(f"Webhook 返回 HTTP {error.code}：{detail}") from error
    except (URLError, TimeoutError, OSError) as error:
        raise AlertError(f"Webhook 请求失败：{error}") from error
