"""FastAPI 依赖：控制面连接、当前用户、进程配置。

`get_db` 是 async 生成器（建/关连接在事件循环线程），其余依赖与路由都是同步函数，
由 FastAPI 丢进线程池执行——它们里面有 argon2 与 sqlite 这类阻塞调用，
放事件循环里会卡住所有并发请求。连接本身已开 `check_same_thread=False`（见 `db.connect`）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request

from mc_librarian.auth import sessions, tokens
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
DatabaseDep = Annotated[Database, Depends(get_database)]


def get_current_user(
    request: Request,
    conn: DbConn,
) -> User:
    """解析当前用户：优先 `Authorization: Bearer mcl_...`（脚本），其次 session cookie。

    缺失/过期/已登出/已吊销/被禁用统一 401。刻意写成同步 `def`：里面是同步 sqlite
    查询，FastAPI 会把它也放进线程池。
    """
    authorization = request.headers.get("authorization")

    if authorization is not None:
        scheme, _, credentials = authorization.partition(" ")

        if scheme.lower() == "bearer" and credentials.startswith(tokens.TOKEN_PREFIX):
            user = tokens.resolve(conn, credentials)

            if user is None:
                raise ProblemException(401, "未认证", "访问令牌无效、已过期或已吊销")

            return user

    raw_token = request.cookies.get(SESSION_COOKIE_NAME)

    if not raw_token:
        raise ProblemException(401, "未认证", "缺少会话 cookie，请先登录")

    user = sessions.resolve(conn, raw_token)

    if user is None:
        raise ProblemException(401, "未认证", "会话无效或已过期，请重新登录")

    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def get_current_admin(user: CurrentUser) -> User:
    """`/v1/admin/*` 的授权：已认证但非 admin 一律 403。

    前端隐藏菜单只是体验，每个管理端点都得独立走这道检查。
    """
    if user.role != "admin":
        raise ProblemException(403, "无权访问", "需要管理员权限")

    return user


CurrentAdmin = Annotated[User, Depends(get_current_admin)]
