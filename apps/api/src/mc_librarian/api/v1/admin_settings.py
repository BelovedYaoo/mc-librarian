"""`GET` / `PUT /v1/admin/settings`：实例设置的读取与部分更新（契约见 docs/openapi.yaml）。

**值的来源是「配置派生 + 数据库覆盖」**（issue #11）：`settings` 表里有覆盖的键用库里的，
没有的退回 `MC_LIBRARIAN_*` 运行时配置；清掉一行覆盖就回退环境配置。读取**每次实时查库**
（`settings_store.read_overrides`），所以保存后立刻生效，不需要重启（验收第 1 条）。

**密钥只写不读**：`llm.api_key` / `alerts.webhook_url` 请求里可以传，响应里只有
`api_key_set` / `webhook_url_set`；库里存的是 AES-GCM 密文（主密钥 `MC_LIBRARIAN_SECRET`，
缺失时服务启动失败）。任何路径都不会把明文或密文回给调用方。

**只返回真实存在的值**：未配置的 `llm.base_url` / `llm.model` / 告警阈值整字段省略
（`response_model_exclude_none`），不拿契约里的示例默认值填充——返回假值等于告诉前端
「模型已配置、密钥已设置」这类服务端自己都不知道的事。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import APIRouter, Request

from mc_librarian import settings_store
from mc_librarian.auth.deps import CurrentAdmin, DbConn, SettingsDep, client_ip
from mc_librarian.config import Settings as RuntimeSettings
from mc_librarian.db import transaction
from mc_librarian.problems import problem_response
from mc_librarian.schemas import (
    AlertsSettings,
    AskSettings,
    IndexSettings,
    LlmSettings,
    SecuritySettings,
    Settings,
    SettingsUpdate,
)

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get(
    "/settings",
    summary="读取设置",
    description=(
        "值 = `settings` 表里的覆盖 + `MC_LIBRARIAN_*` 运行时配置兜底（每次实时查库）。"
        "密钥只回 `api_key_set` / `webhook_url_set`；未配置的可选字段整字段省略。"
    ),
    operation_id="getSettings",
    response_model_exclude_none=True,
    responses={403: problem_response("需要管理员权限")},
)
def get_settings(_admin: CurrentAdmin, conn: DbConn, settings: SettingsDep) -> Settings:
    return _view(_effective(conn, settings))


@router.put(
    "/settings",
    summary="更新设置",
    description=(
        "部分更新：只改请求里给出的字段，未给的保持原值。"
        "值给 `null`（密钥给空字符串）= 清除该覆盖，回退环境配置。"
        "保存后立即生效；密钥只写不读，响应里只有 `*_set`。"
    ),
    operation_id="putSettings",
    response_model_exclude_none=True,
    responses={
        403: problem_response("需要管理员权限"),
        422: problem_response("请求不合法"),
    },
)
def put_settings(
    payload: SettingsUpdate,
    request: Request,
    admin: CurrentAdmin,
    conn: DbConn,
    settings: SettingsDep,
) -> Settings:
    """写入与审计在同一个事务里：不会出现改了一半、账没记的情况。"""
    with transaction(conn):
        settings_store.apply_update(
            conn,
            payload,
            settings=settings,
            actor_id=admin.id,
            ip=client_ip(request),
        )

    return _view(_effective(conn, settings))


def _effective(conn: sqlite3.Connection, settings: RuntimeSettings) -> RuntimeSettings:
    """实时读库：数据库覆盖 + 环境配置兜底。"""
    return settings_store.effective_settings(
        settings, settings_store.read_overrides(conn, settings)
    )


def _view(settings: RuntimeSettings) -> Settings:
    """运行时配置 → 契约 `Settings`。空串与未配置等价（环境变量设成空串是常见笔误）。"""
    return Settings(
        llm=LlmSettings(
            base_url=_blank_to_none(settings.llm_base_url),
            model=_blank_to_none(settings.llm_model),
            temperature=settings.llm_temperature,
            timeout_s=settings.llm_timeout_s,
            api_key_set=_is_set(settings.llm_api_key),
        ),
        ask=AskSettings(
            max_steps=settings.ask_max_steps,
            max_answer_tokens=settings.ask_max_answer_tokens,
            max_context_tokens=settings.ask_max_context_tokens,
            timeout_s=settings.ask_timeout_s,
            enabled=settings.ask_enabled,
        ),
        index=IndexSettings(
            # `Settings.index.java_xmx` 就是反编译时传给 java 的 `-Xmx`。
            enabled=settings.index_enabled,
            java_xmx=settings.decompile_memory,
            java=settings.java,
            # 路径类字段在契约里是 `string`：运行时是 `Path | None`，空字符串 = 未配置。
            vineflower_jar=_path_to_str(settings.vineflower_jar),
            specialsource_jar=_path_to_str(settings.specialsource_jar),
            # 数据根的**绝对路径**：注册索引时把 `db/<版本>.sqlite` 放进这里。
            data_dir=str(settings.data_dir),
            summary_batch_size=settings.summary_batch_size,
            summary_concurrency=settings.summary_concurrency,
        ),
        security=SecuritySettings(
            session_ttl_hours=settings.session_ttl_hours,
            invite_ttl_hours=settings.invite_ttl_hours,
            max_concurrent_asks=settings.max_concurrent_asks,
        ),
        alerts=AlertsSettings(
            webhook_url_set=_is_set(settings.alerts_webhook_url),
            daily_token_threshold=settings.alerts_daily_token_threshold,
        ),
    )


def _is_set(value: str | None) -> bool:
    """密钥「是否已设置」：只回布尔值，永远不回值本身。"""
    return bool(value and value.strip())


def _blank_to_none(value: str | None) -> str | None:
    return value if value and value.strip() else None


def _path_to_str(value: Path | None) -> str:
    """路径类配置对外的形式：未配置回空字符串（前端留空 = 回退环境变量）。"""
    return str(value) if value is not None else ""
