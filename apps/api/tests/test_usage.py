"""用量聚合与告警（issue #12）。

覆盖规格补充 §2–§9：`GET /v1/admin/usage` 的聚合（四种 `group_by`）、无数据区间返回零值、
`user_id` 过滤、默认区间、以及告警去重（同一 `(day, metric)` 只发一次 Webhook）、
阈值 ≤ 0 不告警、未配 Webhook 不检查、Webhook 失败只 warn。只审计 token，不含金额。

`/v1/ask` 侧「Webhook 抛异常仍正常返回」在 `tests/test_ask.py`（那里有现成的索引 fixture）。

时间口径：`usage.created_at` 存 UTC，分天按**本地日期（UTC+8）**。测试里的 `day` 都按本地日期写，
落库的 `created_at` 取当天 04:00 UTC（= 本地 12:00），确保换算后仍在同一天。
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from conftest import ADMIN_USERNAME
from mc_librarian import alerts, usage
from mc_librarian.config import Settings

VERSION = "26.3"

DAY_ONE = "2026-11-01"
DAY_TWO = "2026-11-02"


def insert_usage(
    conn: sqlite3.Connection,
    *,
    day: str,
    user_id: str | None = None,
    model: str | None = None,
    version: str | None = None,
    prompt: int = 0,
    completion: int = 0,
    latency: int = 0,
) -> None:
    """插一行用量；`day` 是本地日期，`created_at` 落成当天 04:00 UTC（本地 12:00）。"""
    conn.execute(
        "INSERT INTO usage"
        " (user_id, kind, model, prompt_tokens, completion_tokens, latency_ms,"
        "  version, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            user_id,
            usage.KIND_ASK,
            model,
            prompt,
            completion,
            latency,
            version,
            f"{day}T04:00:00Z",
        ),
    )
    conn.commit()


@pytest.fixture
def admin_id(admin_client: TestClient, conn: sqlite3.Connection) -> str:
    """已登录管理员的用户 id（`usage.user_id` 有外键，必须指向真实用户）。"""
    row = conn.execute("SELECT id FROM users WHERE username = ?", (ADMIN_USERNAME,)).fetchone()

    return str(row["id"])


def get_usage(client: TestClient, **params: Any) -> Any:
    return client.get("/v1/admin/usage", params=params)


# ----------------------------------------------------------------------
# 聚合（规格 §2、验收第 2 条）
# ----------------------------------------------------------------------


def test_report_totals_and_day_buckets(
    admin_client: TestClient, conn: sqlite3.Connection, admin_id: str
) -> None:
    insert_usage(
        conn, day=DAY_ONE, user_id=admin_id, prompt=100, completion=10, latency=1000
    )
    insert_usage(
        conn, day=DAY_ONE, user_id=admin_id, prompt=200, completion=20, latency=2000
    )
    insert_usage(
        conn, day=DAY_TWO, user_id=admin_id, prompt=300, completion=30, latency=3000
    )

    body = get_usage(admin_client, **{"from": DAY_ONE, "to": DAY_TWO, "group_by": "day"}).json()

    assert body["from"] == DAY_ONE
    assert body["to"] == DAY_TWO
    assert body["requests"] == 3
    assert body["totals"] == {
        "prompt_tokens": 600,
        "completion_tokens": 60,
        "latency_ms": 6000,
    }
    assert [bucket["key"] for bucket in body["buckets"]] == [DAY_ONE, DAY_TWO]
    assert body["buckets"][0] == {
        "key": DAY_ONE,
        "requests": 2,
        "prompt_tokens": 300,
        "completion_tokens": 30,
    }
    assert body["buckets"][1] == {
        "key": DAY_TWO,
        "requests": 1,
        "prompt_tokens": 300,
        "completion_tokens": 30,
    }


def test_report_totals_equal_sum_of_buckets(
    admin_client: TestClient, conn: sqlite3.Connection, admin_id: str
) -> None:
    """页面上的「总量卡片」与「分组明细」必须同源：合计 = 各桶之和。"""
    insert_usage(conn, day=DAY_ONE, user_id=admin_id, model="a", prompt=1, completion=2)
    insert_usage(conn, day=DAY_ONE, user_id=admin_id, model="b", prompt=3, completion=4)
    insert_usage(conn, day=DAY_TWO, user_id=admin_id, model="a", prompt=5, completion=6)

    body = get_usage(
        admin_client, **{"from": DAY_ONE, "to": DAY_TWO, "group_by": "model"}
    ).json()

    assert body["requests"] == sum(bucket["requests"] for bucket in body["buckets"])
    assert body["totals"]["prompt_tokens"] == sum(
        bucket["prompt_tokens"] for bucket in body["buckets"]
    )
    assert body["totals"]["completion_tokens"] == sum(
        bucket["completion_tokens"] for bucket in body["buckets"]
    )


@pytest.mark.parametrize(
    ("group_by", "expected"),
    [
        ("user", {ADMIN_USERNAME: 3}),
        ("version", {VERSION: 2, "1.20.1": 1}),
        ("model", {"deepseek": 2, "other": 1}),
    ],
)
def test_report_group_by_keys(
    admin_client: TestClient,
    conn: sqlite3.Connection,
    admin_id: str,
    group_by: str,
    expected: dict[str, int],
) -> None:
    """`group_by` 四种取值各一条（`day` 在上面两条里覆盖）。"""
    insert_usage(conn, day=DAY_ONE, user_id=admin_id, model="deepseek", version=VERSION)
    insert_usage(conn, day=DAY_ONE, user_id=admin_id, model="deepseek", version=VERSION)
    insert_usage(conn, day=DAY_TWO, user_id=admin_id, model="other", version="1.20.1")

    body = get_usage(admin_client, **{"from": DAY_ONE, "to": DAY_TWO, "group_by": group_by}).json()

    assert {bucket["key"]: bucket["requests"] for bucket in body["buckets"]} == expected


def test_report_user_bucket_falls_back_to_placeholder(
    admin_client: TestClient, conn: sqlite3.Connection
) -> None:
    """`user_id` 为空的行不能丢：分桶键显式标成「未知」，而不是被 JOIN 掉。"""
    insert_usage(conn, day=DAY_ONE)

    body = get_usage(admin_client, **{"from": DAY_ONE, "to": DAY_ONE, "group_by": "user"}).json()

    assert [bucket["key"] for bucket in body["buckets"]] == [usage.UNKNOWN_GROUP_KEY]
    assert body["requests"] == 1


def test_report_filters_by_user_id(
    admin_client: TestClient, conn: sqlite3.Connection, admin_id: str
) -> None:
    insert_usage(conn, day=DAY_ONE, user_id=admin_id, prompt=10)
    insert_usage(conn, day=DAY_ONE, user_id=None, prompt=999)

    body = get_usage(
        admin_client, **{"from": DAY_ONE, "to": DAY_ONE, "user_id": admin_id}
    ).json()

    assert body["requests"] == 1
    assert body["totals"]["prompt_tokens"] == 10


def test_report_empty_range_returns_zero_values(admin_client: TestClient) -> None:
    """验收第 5 条：无数据区间返回零值，不是报错。"""
    response = get_usage(admin_client, **{"from": DAY_ONE, "to": DAY_TWO})

    assert response.status_code == 200

    body = response.json()

    assert body["totals"] == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "latency_ms": 0,
    }
    assert body["requests"] == 0
    assert body["buckets"] == []


def test_report_range_excludes_other_days(
    admin_client: TestClient, conn: sqlite3.Connection, admin_id: str
) -> None:
    insert_usage(conn, day=DAY_ONE, user_id=admin_id, prompt=10)
    insert_usage(conn, day=DAY_TWO, user_id=admin_id, prompt=20)

    body = get_usage(admin_client, **{"from": DAY_ONE, "to": DAY_ONE}).json()

    assert body["requests"] == 1
    assert body["totals"]["prompt_tokens"] == 10


def test_report_defaults_to_last_seven_days(
    admin_client: TestClient, conn: sqlite3.Connection, admin_id: str
) -> None:
    """缺省区间 = 最近 7 天（含今天），与前端默认一致。"""
    today = usage.local_date()
    insert_usage(conn, day=today.isoformat(), user_id=admin_id, prompt=10)
    insert_usage(
        conn,
        day=(today - timedelta(days=10)).isoformat(),
        user_id=admin_id,
        prompt=999,
    )

    body = get_usage(admin_client).json()

    assert body["to"] == today.isoformat()
    assert body["from"] == (today - timedelta(days=6)).isoformat()
    assert body["requests"] == 1
    assert body["totals"]["prompt_tokens"] == 10


def test_report_omits_cost_fields(admin_client: TestClient, conn: sqlite3.Connection) -> None:
    """彻底移除金额：响应里没有 `cost_usd` / `cost_configured`，只统计 token。"""
    insert_usage(conn, day=DAY_ONE, prompt=10)

    body = get_usage(admin_client, **{"from": DAY_ONE, "to": DAY_ONE}).json()

    assert "cost_configured" not in body
    assert "cost_usd" not in body["totals"]
    assert all("cost_usd" not in bucket for bucket in body["buckets"])


def test_report_requires_admin(user_client: TestClient) -> None:
    assert get_usage(user_client).status_code == 403


def test_report_rejects_unknown_group_by(admin_client: TestClient) -> None:
    assert get_usage(admin_client, group_by="week").status_code == 422


# ----------------------------------------------------------------------
# 告警（规格 §3–§7、验收第 3、4 条）
# ----------------------------------------------------------------------


class RecordingWebhook:
    """替掉 `alerts.post_webhook`：记下每次投递，不联网。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, payload: dict[str, object], timeout: float) -> None:
        self.calls.append({"url": url, "payload": payload, "timeout": timeout})


@pytest.fixture
def webhook(monkeypatch: pytest.MonkeyPatch) -> RecordingWebhook:
    recorder = RecordingWebhook()
    monkeypatch.setattr(alerts, "post_webhook", recorder)

    return recorder


def alert_settings(settings: Settings, **overrides: Any) -> Settings:
    """打开告警并把阈值调到测试值（默认只开 token 阈值）。"""
    settings.alerts_webhook_url = "https://hook.invalid/alerts"
    settings.alerts_daily_token_threshold = 100

    for key, value in overrides.items():
        setattr(settings, key, value)

    return settings


def database_of(client: TestClient) -> Any:
    return cast("Any", client.app).state.db


def test_alert_events_table_has_unique_day_metric(conn: sqlite3.Connection) -> None:
    """迁移 v7 的 `UNIQUE(day, metric)` 就是去重机制本身：同一天同一指标插不进第二行。"""
    conn.execute(
        "INSERT INTO alert_events (day, metric, value, threshold, fired_at)"
        " VALUES ('2026-11-04', 'daily_tokens', 1, 1, '2026-11-04T00:00:00Z')"
    )

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO alert_events (day, metric, value, threshold, fired_at)"
            " VALUES ('2026-11-04', 'daily_tokens', 2, 1, '2026-11-04T00:00:00Z')"
        )


def test_alert_fires_once_per_day_and_metric(
    client: TestClient,
    conn: sqlite3.Connection,
    admin_id: str,
    settings: Settings,
    webhook: RecordingWebhook,
) -> None:
    """验收第 3 条：同日同指标只告警一次，第二次检查不再发 Webhook。"""
    insert_usage(conn, day=usage.local_day(), user_id=admin_id, prompt=80, completion=70)
    alert_settings(settings)

    alerts.check(database_of(client), settings)
    alerts.check(database_of(client), settings)

    assert len(webhook.calls) == 1
    call = webhook.calls[0]
    assert call["url"] == settings.alerts_webhook_url
    assert call["timeout"] == alerts.WEBHOOK_TIMEOUT_S
    assert call["payload"] == {
        "day": usage.local_day(),
        "metric": alerts.METRIC_DAILY_TOKENS,
        "value": 150,
        "threshold": 100,
        "message": "今日 token 用量已达 150，超过阈值 100",
    }

    rows = conn.execute("SELECT day, metric, value, threshold FROM alert_events").fetchall()
    assert [tuple(row) for row in rows] == [(usage.local_day(), "daily_tokens", 150.0, 100.0)]


def test_alert_threshold_from_settings_api_takes_effect(
    admin_client: TestClient,
    conn: sqlite3.Connection,
    admin_id: str,
    settings: Settings,
    webhook: RecordingWebhook,
) -> None:
    """端到端：`PUT /v1/admin/settings` 写阈值 → `alerts.check` 读到并触发。"""
    insert_usage(conn, day=usage.local_day(), user_id=admin_id, prompt=150)

    response = admin_client.put(
        "/v1/admin/settings",
        json={
            "alerts": {
                "webhook_url": "https://hook.invalid/alerts",
                "daily_token_threshold": 100,
            }
        },
    )

    assert response.status_code == 200, response.text

    alerts.check(database_of(admin_client), settings)

    assert [call["payload"]["metric"] for call in webhook.calls] == [alerts.METRIC_DAILY_TOKENS]
    assert webhook.calls[0]["payload"]["value"] == 150
    assert webhook.calls[0]["payload"]["threshold"] == 100


def test_alert_skips_non_positive_thresholds(
    client: TestClient,
    conn: sqlite3.Connection,
    admin_id: str,
    settings: Settings,
    webhook: RecordingWebhook,
) -> None:
    """规格 §3：阈值 ≤ 0 表示该指标不告警。"""
    insert_usage(conn, day=usage.local_day(), user_id=admin_id, prompt=10**9)
    settings.alerts_webhook_url = "https://hook.invalid/alerts"
    settings.alerts_daily_token_threshold = 0

    alerts.check(database_of(client), settings)

    assert webhook.calls == []
    assert conn.execute("SELECT COUNT(*) AS n FROM alert_events").fetchone()["n"] == 0


def test_alert_skips_below_threshold(
    client: TestClient,
    conn: sqlite3.Connection,
    admin_id: str,
    settings: Settings,
    webhook: RecordingWebhook,
) -> None:
    """没超阈值不告警（阈值本身不算超）。"""
    insert_usage(conn, day=usage.local_day(), user_id=admin_id, prompt=100)
    alert_settings(settings)

    alerts.check(database_of(client), settings)

    assert webhook.calls == []


def test_alert_skips_when_webhook_missing(
    client: TestClient,
    conn: sqlite3.Connection,
    admin_id: str,
    settings: Settings,
    webhook: RecordingWebhook,
) -> None:
    """阈值配了但没有 Webhook 地址：没有投递目标，不发也不记。"""
    insert_usage(conn, day=usage.local_day(), user_id=admin_id, prompt=150)
    settings.alerts_webhook_url = "   "
    settings.alerts_daily_token_threshold = 100

    alerts.check(database_of(client), settings)

    assert webhook.calls == []
    assert conn.execute("SELECT COUNT(*) AS n FROM alert_events").fetchone()["n"] == 0


def test_alert_survives_webhook_failure(
    client: TestClient,
    conn: sqlite3.Connection,
    admin_id: str,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Webhook 失败只 warn：检查不抛异常，也不重试（当天该指标已记账）。"""
    insert_usage(conn, day=usage.local_day(), user_id=admin_id, prompt=150)
    alert_settings(settings)

    def boom(url: str, payload: dict[str, object], timeout: float) -> None:
        raise alerts.AlertError("Webhook 请求失败：connection refused")

    monkeypatch.setattr(alerts, "post_webhook", boom)

    alerts.check(database_of(client), settings)
    alerts.check(database_of(client), settings)

    assert conn.execute("SELECT COUNT(*) AS n FROM alert_events").fetchone()["n"] == 1


def test_alert_ignores_other_days(
    client: TestClient,
    conn: sqlite3.Connection,
    admin_id: str,
    settings: Settings,
    webhook: RecordingWebhook,
) -> None:
    """只算当天：昨天超阈值不该触发今天的告警。"""
    insert_usage(conn, day="2026-11-01", user_id=admin_id, prompt=10**6)
    alert_settings(settings)

    alerts.check(database_of(client), settings)

    assert webhook.calls == []


def test_usage_endpoint_triggers_alert_check(
    admin_client: TestClient,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """规格 §6.2：管理员打开用量页也顺带检查一次。"""
    calls: list[tuple[Any, Settings]] = []

    def record(database: Any, config: Settings) -> None:
        calls.append((database, config))

    monkeypatch.setattr(alerts, "schedule", record)

    assert get_usage(admin_client).status_code == 200

    assert calls == [(database_of(admin_client), settings)]
