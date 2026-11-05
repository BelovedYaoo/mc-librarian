"""MCP 端点（`/mcp`，streamable HTTP）。

对外只暴露一个 `mount_mcp(app, database, settings)`：把 MCP 子 app 挂到 `/mcp`，并在父 app 的
lifespan 里带上会话管理器的生命周期。设计见 `docs/01-api.md` §10。
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from mc_librarian.config import Settings
from mc_librarian.db import Database
from mc_librarian.mcp.auth import BearerAuthMiddleware
from mc_librarian.mcp.server import (
    NoTrailingSlashMiddleware,
    build_asgi_app,
    build_server,
)

__all__ = [
    "BearerAuthMiddleware",
    "NoTrailingSlashMiddleware",
    "build_server",
    "mount_mcp",
]


def mount_mcp(app: object, database: Database, settings: Settings) -> MCPServer:
    """把 MCP 挂到 `app` 的 `/mcp`；返回 server 供调用方在 lifespan 里跑会话管理器。

    `app` 由类型标注为 `object` 以避免在这里导入 FastAPI（本模块被 CLI 等路径导入时不必拉起
    web 栈）；调用点传的是 `FastAPI`。
    """
    server = build_server(database, settings)
    sub_app = build_asgi_app(server)

    # 鉴权只包 MCP 子 app：`/v1/*` 那套 cookie + PAT 双鉴权不能被这里拦掉。
    gated = BearerAuthMiddleware(sub_app, database)

    app.mount("/mcp", gated)  # type: ignore[attr-defined]
    app.add_middleware(NoTrailingSlashMiddleware)  # type: ignore[attr-defined]

    return server
