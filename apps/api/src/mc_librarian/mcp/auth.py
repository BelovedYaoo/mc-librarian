"""MCP 端点的鉴权：`Authorization: Bearer mcl_...`（个人访问令牌）。

为什么不给 `MCPServer` 传 `token_verifier`：SDK 的那个参数属于**完整 OAuth 资源服务器**这一套
（`AuthSettings` 强制 `issuer_url`），配上之后客户端会先去做 OAuth 发现（`/.well-known/...`），
而本服务只发静态 PAT、没有授权服务器。所以这里自己包一层 ASGI 中间件，只做「校验 PAT → 放进
上下文」这一件事，鉴权逻辑仍复用 `mc_librarian.auth.tokens.resolve`，不另造一套。

中间件**只包 MCP 子 app**，不挂到全局 FastAPI——否则 `/v1/*` 那套 session cookie + PAT 的双
鉴权会被这里拦掉。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextvars import ContextVar
from dataclasses import dataclass

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mc_librarian.auth import tokens
from mc_librarian.db import Database

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Caller:
    """这次 MCP 请求的调用者。工具的副作用（用量记录）按它归属。"""

    user_id: str
    username: str


# 当前请求的调用者。中间件在调用子 app 之前 `set`，之后 `reset`。
#
# 用 ContextVar 而不是按 session id 查表：MCP 的工具调用是**从处理该请求的 asyncio 任务派生的**
# 子任务，`contextvars` 在创建子任务时会被复制，所以工具里读得到中间件设的值；而且客户端断开
# 时不需要清理任何残留（作用域随请求结束）。按会话查表反而要处理「会话关闭时清理」。
current_caller: ContextVar[Caller | None] = ContextVar("mcp_caller", default=None)


def _unauthorized_headers(body: bytes) -> list[tuple[bytes, bytes]]:
    return [
        (b"content-type", b"application/json"),
        # 401 必须带 WWW-Authenticate，否则客户端不知道该用哪种凭据（RFC 7235）。
        (b"www-authenticate", b"Bearer"),
        (b"content-length", str(len(body)).encode()),
    ]


class BearerAuthMiddleware:
    """校验 `Authorization: Bearer mcl_...`，通过则把调用者放进 `current_caller`。"""

    def __init__(self, app: ASGIApp, database: Database) -> None:
        self.app = app
        self.database = database

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # 子 app 也会收到 lifespan / websocket 之类；只处理真正的 HTTP 请求。
        if scope["type"] != "http":
            await self.app(scope, receive, send)

            return

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        authorization = headers.get("authorization", "")
        scheme, _, credential = authorization.partition(" ")

        caller = (
            self._resolve(credential)
            if scheme.lower() == "bearer" and credential.startswith(tokens.TOKEN_PREFIX)
            else None
        )

        if caller is None:
            # 缺失 / 格式不对 / 无效 / 过期 / 已吊销 / 被禁用**统一同一条响应**，
            # 不区分「令牌不存在」与「令牌已吊销」——那会变成令牌探测的口子。
            body = json.dumps({"error": "未认证：需要 Authorization: Bearer <PAT>"}).encode()

            start: Message = {
                "type": "http.response.start",
                "status": 401,
                "headers": _unauthorized_headers(body),
            }
            await send(start)
            await send({"type": "http.response.body", "body": body})

            return

        token = current_caller.set(caller)

        try:
            await self.app(scope, receive, send)
        finally:
            current_caller.reset(token)

    def _resolve(self, credential: str) -> Caller | None:
        """查控制面库校验 PAT。每次请求一条只读连接，用完即关。"""
        try:
            with self.database.connect() as connection:
                user = tokens.resolve(connection, credential)
        except sqlite3.Error:
            # 库打不开/被锁时按未认证处理：MCP 是旁路，不该让一次查询错误冒泡成 500 掩盖原因。
            logger.exception("MCP 鉴权时读取控制面失败")

            return None

        if user is None:
            return None

        return Caller(user_id=user.id, username=user.username)


__all__ = ["BearerAuthMiddleware", "Caller", "current_caller"]


# 让 `Message` 参与类型检查（`Send` 的签名里用到），避免 linter 把它当未使用导入。
_ = Message
