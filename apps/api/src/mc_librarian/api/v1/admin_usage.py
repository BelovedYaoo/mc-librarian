"""`GET /v1/admin/usage`：用量聚合（契约见 docs/openapi.yaml，规格见 issue #12 的规格补充 §2）。

只读 `usage` 表（#8 每次 ask 写一行），不写任何 usage 行；区间内没有数据时返回零值而不是报错。
`totals` 与 `buckets` 由 `usage.report` 用同一个 FROM/WHERE 算出来，页面直接用这些数字，
不需要（也不允许）在前端再算一遍。

调用时顺带触发一次告警检查（规格 §6.2）：管理员打开用量页也能把当天的告警发出去。
检查在后台线程里跑，**不等它**（告警不阻断请求）。
"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query

from mc_librarian import alerts, usage
from mc_librarian.auth.deps import CurrentAdmin, DatabaseDep, DbConn, SettingsDep
from mc_librarian.problems import problem_response
from mc_librarian.schemas import UsageReport

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get(
    "/usage",
    summary="用量统计",
    description=(
        "按 `group_by` 聚合 `usage` 表：`totals` 是区间合计、`buckets` 是分组明细，"
        "两者同源，页面直接用这里的数字。区间内没有数据时返回零值，不是报错。"
        "`day` 是本地日期（UTC+8）。`from`/`to` 缺省为最近 7 天（含今天）。"
        "调用时顺带做一次告警检查（不阻塞响应）。"
    ),
    operation_id="getUsage",
    responses={
        401: problem_response("未认证"),
        403: problem_response("需要管理员权限"),
        422: problem_response("查询参数不合法"),
    },
)
def get_usage(
    _admin: CurrentAdmin,
    conn: DbConn,
    database: DatabaseDep,
    settings: SettingsDep,
    from_: Annotated[date | None, Query(alias="from")] = None,
    to: Annotated[date | None, Query()] = None,
    user_id: Annotated[str | None, Query()] = None,
    group_by: Annotated[usage.GroupBy, Query()] = "day",
) -> UsageReport:
    start, end = usage.resolve_range(from_, to)
    report = usage.report(
        conn,
        start=start,
        end=end,
        user_id=user_id,
        group_by=group_by,
    )

    # 规格 §6.2：打开用量页也顺带检查一次（幂等，靠 `alert_events` 的唯一约束去重）。
    alerts.schedule(database, settings)

    return report
