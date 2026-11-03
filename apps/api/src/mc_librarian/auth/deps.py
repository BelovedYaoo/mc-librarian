"""FastAPI 依赖：控制面连接、当前用户、进程配置。

连接用 `async` 生成器依赖，保证「建连接 / 用连接 / 关连接」都在事件循环同一线程里完成，
不触发 sqlite3 的跨线程限制。
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request

from mc_librarian.auth import sessions
from mc_librarian.config import SESSION_COOKIE_NAME, Settings
from mc_librarian.db import Database
from mc_librarian.problems import ProblemException
from mc_librarian.schemas import User


def get_app_settings(request: Request) -> Settings:
    """进程配置取自 `app.state`，测试注入的临时数据根因此会被真正用上。"""
    settings: Settings = request.app.state.settings

    return settings


def get_database(request: Request) -> Database:
    database: Database = request.app.state.db

    return database


async def get_db(
    database: Annotated[Database, Depends(get_database)],
) -> AsyncIterator[sqlite3.Connection]:
    with database.connect() as conn:
        yield conn


def client_ip(request: Request) -> str | None:
    """会话表里记的客户端地址（本地开发时可能为 None）。"""
    return request.client.host if request.client else None


DbConn = Annotated[sqlite3.Connection, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]


async def get_current_user(
    request: Request,
    conn: DbConn,
) -> User:
    """从 cookie 解析当前用户；缺失/过期/已登出/被禁用统一 401。"""
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)

    if not raw_token:
        raise ProblemException(401, "未认证", "缺少会话 cookie，请先登录")

    user = sessions.resolve(conn, raw_token)

    if user is None:
        raise ProblemException(401, "未认证", "会话无效或已过期，请重新登录")

    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
